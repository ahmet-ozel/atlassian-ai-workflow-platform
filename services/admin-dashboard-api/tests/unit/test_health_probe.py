"""Unit tests for ``src.lifecycle.health_probe``.

Validates the kind-aware probe contract from design §3.6 and tasks.md
task 5.3 — Requirements 4.7, 7.4, 7.5, 7.6.

Strategy
--------
* HTTP probe paths exercise a real :class:`httpx.AsyncClient` wired to
  an :class:`httpx.MockTransport`. The transport records every request
  so we can assert URL shape (``http://{compose_service_name}:{port}{path}``)
  and per-request timeout behaviour without any network access.
* The worker path stubs ``temporalio.client.Client`` with a tiny fake
  whose ``connect`` classmethod is configurable per test (return,
  raise, sleep). The stub is installed into ``sys.modules`` so the
  module's lazy ``from temporalio.client import Client`` import picks
  it up.
* No fixtures from the wider workspace are needed; each test is
  independent and side-effect free.
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import httpx
import pytest

# Bootstrap ``sys.path`` so ``import src.lifecycle.health_probe``
# resolves when pytest is invoked directly under ``tests/unit/``.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.health_probe import (  # noqa: E402
    HealthProbe,
    HealthSnapshot,
)
from src.manifest import ManagedServiceEntry  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    *,
    name: str = "automation-service",
    kind: str = "http_service",
    compose_service_name: str | None = None,
    health_endpoint: str | None = "/healthz",
) -> ManagedServiceEntry:
    """Construct a :class:`ManagedServiceEntry` with sane defaults."""

    return ManagedServiceEntry(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        compose_service_name=compose_service_name or name,
        compose_profile=name,
        env_example_path=f"services/{name}/.env.example",
        health_endpoint=health_endpoint,
        test_command=None,
    )


def _make_probe(
    transport: httpx.MockTransport,
    *,
    ports: dict[str, int] | None = None,
    temporal_host: str = "temporal:7233",
) -> tuple[HealthProbe, httpx.AsyncClient]:
    """Wire a :class:`HealthProbe` against an in-process mock transport."""

    client = httpx.AsyncClient(transport=transport)
    probe = HealthProbe(
        http_client=client,
        temporal_host=temporal_host,
        compose_internal_ports=ports,
    )
    return probe, client


def _install_temporal_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    behaviour: str,
    sleep_seconds: float = 0.0,
) -> list[str]:
    """Replace ``temporalio.client`` with a stub and return the call log.

    ``behaviour`` is one of:

    * ``"ok"`` — ``Client.connect(host)`` returns immediately.
    * ``"raise"`` — ``Client.connect`` raises ``ConnectionError``.
    * ``"sleep"`` — ``Client.connect`` sleeps for ``sleep_seconds``
      seconds before returning, used to exercise the 5 s timeout cap.
    """

    calls: list[str] = []

    class _StubClient:
        @classmethod
        async def connect(cls, host: str) -> "_StubClient":
            calls.append(host)
            if behaviour == "raise":
                raise ConnectionError("temporal unreachable")
            if behaviour == "sleep":
                await asyncio.sleep(sleep_seconds)
            return cls()

    fake_module = types.ModuleType("temporalio.client")
    fake_module.Client = _StubClient  # type: ignore[attr-defined]
    fake_pkg = types.ModuleType("temporalio")
    fake_pkg.client = fake_module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "temporalio", fake_pkg)
    monkeypatch.setitem(sys.modules, "temporalio.client", fake_module)
    return calls


def _stub_running_worker_container(
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_running: bool = True,
    body: str = "running container(s): infra-agent-runner-worker-1",
) -> None:
    """Avoid shelling out to Docker in worker-probe unit tests."""

    async def _fake_check(
        self: HealthProbe,
        compose_service_name: str,
    ) -> tuple[bool, str]:
        del self, compose_service_name
        return is_running, body

    monkeypatch.setattr(
        HealthProbe,
        "_compose_service_has_running_container",
        _fake_check,
        raising=True,
    )


# ---------------------------------------------------------------------------
# HTTP probe (Requirement 7.6, 4.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_probe_healthy_when_both_endpoints_return_200() -> None:
    """``/healthz`` 200 + ``/readyz`` 200 → ``state == "healthy"``."""

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/healthz":
            return httpx.Response(200, text="ok")
        if request.url.path == "/readyz":
            return httpx.Response(200, text="ready")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    probe, client = _make_probe(
        transport, ports={"automation-service": 8080}
    )
    try:
        snap = await probe.probe(_entry())
    finally:
        await client.aclose()

    assert isinstance(snap, HealthSnapshot)
    assert snap.state == "healthy"
    assert snap.healthz_status == 200
    assert snap.healthz_body == "ok"
    assert snap.readyz_status == 200
    assert snap.readyz_body == "ready"

    # URL composition: Compose internal hostname + supplied port + path.
    assert "http://automation-service:8080/healthz" in seen
    assert "http://automation-service:8080/readyz" in seen


@pytest.mark.asyncio
async def test_http_probe_uses_default_port_80_when_unmapped() -> None:
    """Unknown ``compose_service_name`` falls back to port 80 (task contract)."""

    seen_ports: list[int | None] = []
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        # ``request.url.port`` is ``None`` when the URL uses the
        # protocol-default port (httpx normalises ``:80`` away on HTTP
        # URLs); we treat that as confirmation that port 80 was used.
        seen_ports.append(request.url.port)
        seen_hosts.append(request.url.host)
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    probe, client = _make_probe(transport, ports={})  # no entries
    try:
        snap = await probe.probe(_entry(name="assistant-service"))
    finally:
        await client.aclose()

    assert snap.state == "healthy"
    assert all(host == "assistant-service" for host in seen_hosts), seen_hosts
    # ``None`` (httpx normalised default) or explicit ``80`` both
    # indicate the probe targeted the default HTTP port.
    assert all(port in (None, 80) for port in seen_ports), seen_ports


@pytest.mark.asyncio
async def test_http_probe_unhealthy_when_healthz_non_200() -> None:
    """Non-200 ``/healthz`` flips the snapshot to ``unhealthy``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(503, text="boom")
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    probe, client = _make_probe(transport)
    try:
        snap = await probe.probe(_entry())
    finally:
        await client.aclose()

    assert snap.state == "unhealthy"
    assert snap.healthz_status == 503
    assert snap.healthz_body == "boom"
    assert snap.readyz_status == 200


@pytest.mark.asyncio
async def test_http_probe_unhealthy_when_readyz_non_200() -> None:
    """``/readyz`` failure surfaces the body verbatim (Requirement 7.6)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(500, text="db down")
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    probe, client = _make_probe(transport)
    try:
        snap = await probe.probe(_entry())
    finally:
        await client.aclose()

    assert snap.state == "unhealthy"
    assert snap.healthz_status == 200
    assert snap.readyz_status == 500
    assert snap.readyz_body == "db down"


@pytest.mark.asyncio
async def test_infra_http_probe_uses_healthz_only() -> None:
    """Infra helpers with health endpoints do not need ``/readyz``."""

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/healthz":
            return httpx.Response(200, text="ok")
        return httpx.Response(404, text="not found")

    transport = httpx.MockTransport(handler)
    probe, client = _make_probe(transport, ports={"atlassian-mcp": 8090})
    try:
        snap = await probe.probe(
            _entry(
                name="atlassian-mcp",
                kind="infra",
                health_endpoint="/healthz",
            )
        )
    finally:
        await client.aclose()

    assert snap.state == "healthy"
    assert snap.healthz_status == 200
    assert snap.readyz_status is None
    assert seen == ["/healthz"]


@pytest.mark.asyncio
async def test_http_probe_prefers_docker_health_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime wiring can trust Docker health before falling back to HTTP."""

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        raise AssertionError("HTTP probe should not run when Docker is healthy")

    async def _fake_docker_health(
        self: HealthProbe,
        compose_service_name: str,
    ) -> tuple[str, str]:
        del self
        assert compose_service_name == "atlassian-mcp"
        return "healthy", "docker healthcheck status: healthy"

    monkeypatch.setattr(
        HealthProbe,
        "_docker_inspect_compose_service_health_status",
        _fake_docker_health,
        raising=True,
    )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    probe = HealthProbe(
        http_client=client,
        temporal_host="temporal:7233",
        compose_internal_ports={"atlassian-mcp": 8090},
        prefer_docker_health=True,
    )
    try:
        snap = await probe.probe(
            _entry(
                name="atlassian-mcp",
                kind="infra",
                health_endpoint="/healthz",
            )
        )
    finally:
        await client.aclose()

    assert snap.state == "healthy"
    assert snap.healthz_status == 200
    assert snap.healthz_body == "docker healthcheck status: healthy"
    assert snap.readyz_status is None
    assert seen == []


@pytest.mark.asyncio
async def test_http_probe_falls_back_when_docker_health_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing Docker healthcheck keeps the original HTTP probe behaviour."""

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="ok")

    async def _fake_docker_health(
        self: HealthProbe,
        compose_service_name: str,
    ) -> tuple[str, str]:
        del self, compose_service_name
        return "", "container has no healthcheck"

    monkeypatch.setattr(
        HealthProbe,
        "_docker_inspect_compose_service_health_status",
        _fake_docker_health,
        raising=True,
    )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    probe = HealthProbe(
        http_client=client,
        temporal_host="temporal:7233",
        compose_internal_ports={"atlassian-mcp": 8090},
        prefer_docker_health=True,
    )
    try:
        snap = await probe.probe(
            _entry(
                name="atlassian-mcp",
                kind="infra",
                health_endpoint="/healthz",
            )
        )
    finally:
        await client.aclose()

    assert snap.state == "healthy"
    assert snap.healthz_status == 200
    assert snap.healthz_body == "ok"
    assert seen == ["http://atlassian-mcp:8090/healthz"]


@pytest.mark.asyncio
async def test_http_probe_truncates_body_to_200_chars() -> None:
    """Bodies > 200 chars are truncated (Requirement 4.7)."""

    big = "x" * 1000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=big)

    transport = httpx.MockTransport(handler)
    probe, client = _make_probe(transport)
    try:
        snap = await probe.probe(_entry())
    finally:
        await client.aclose()

    assert len(snap.healthz_body) == 200
    assert len(snap.readyz_body or "") == 200


@pytest.mark.asyncio
async def test_http_probe_connection_failure_yields_status_minus_one() -> None:
    """A connect-side error maps to ``status=-1`` and a diagnostic body."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    probe, client = _make_probe(transport)
    try:
        snap = await probe.probe(_entry())
    finally:
        await client.aclose()

    assert snap.state == "unhealthy"
    assert snap.healthz_status == -1
    assert "ConnectError" in snap.healthz_body
    assert snap.readyz_status == -1
    assert snap.readyz_body is not None and "ConnectError" in snap.readyz_body


# ---------------------------------------------------------------------------
# Worker probe (Requirement 7.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_probe_healthy_on_successful_temporal_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Client.connect`` success → ``healthz_status=200, state="healthy"``."""

    calls = _install_temporal_stub(monkeypatch, behaviour="ok")
    _stub_running_worker_container(monkeypatch)

    transport = httpx.MockTransport(lambda req: httpx.Response(599))
    probe, client = _make_probe(transport, temporal_host="temporal:7233")
    try:
        snap = await probe.probe(_entry(name="agent-runner-worker", kind="worker", health_endpoint=None))
    finally:
        await client.aclose()

    assert snap.state == "healthy"
    assert snap.healthz_status == 200
    assert snap.readyz_status is None
    assert snap.readyz_body is None
    assert calls == ["temporal:7233"]


@pytest.mark.asyncio
async def test_worker_probe_unhealthy_when_temporal_connect_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Client.connect`` raise → ``healthz_status=-1, state="unhealthy"``."""

    _install_temporal_stub(monkeypatch, behaviour="raise")
    _stub_running_worker_container(monkeypatch)

    transport = httpx.MockTransport(lambda req: httpx.Response(599))
    probe, client = _make_probe(transport)
    try:
        snap = await probe.probe(_entry(name="agent-runner-worker", kind="worker", health_endpoint=None))
    finally:
        await client.aclose()

    assert snap.state == "unhealthy"
    assert snap.healthz_status == -1
    assert "ConnectionError" in snap.healthz_body
    assert "temporal unreachable" in snap.healthz_body
    assert snap.readyz_status is None


@pytest.mark.asyncio
async def test_worker_probe_unhealthy_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect that exceeds the probe timeout flips to ``unhealthy``."""

    # Patch the timeout constant down to a fast value so the test
    # finishes promptly. The stub then sleeps slightly longer than that
    # so :func:`asyncio.wait_for` raises ``TimeoutError``.
    import src.lifecycle.health_probe as hp

    monkeypatch.setattr(hp, "_PROBE_TIMEOUT_SECONDS", 0.05, raising=True)

    _install_temporal_stub(monkeypatch, behaviour="sleep", sleep_seconds=0.5)
    _stub_running_worker_container(monkeypatch)

    transport = httpx.MockTransport(lambda req: httpx.Response(599))
    probe, client = _make_probe(transport)
    try:
        snap = await probe.probe(_entry(name="agent-runner-worker", kind="worker", health_endpoint=None))
    finally:
        await client.aclose()

    assert snap.state == "unhealthy"
    assert snap.healthz_status == -1
    assert "timed out" in snap.healthz_body


@pytest.mark.asyncio
async def test_worker_probe_unhealthy_when_container_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker health must not be green when its Compose container is absent."""

    _install_temporal_stub(monkeypatch, behaviour="ok")
    _stub_running_worker_container(
        monkeypatch,
        is_running=False,
        body="no running Docker container found for Compose service 'agent-runner-worker'",
    )

    transport = httpx.MockTransport(lambda req: httpx.Response(599))
    probe, client = _make_probe(transport)
    try:
        snap = await probe.probe(
            _entry(name="agent-runner-worker", kind="worker", health_endpoint=None)
        )
    finally:
        await client.aclose()

    assert snap.state == "unhealthy"
    assert snap.healthz_status == -1
    assert "no running Docker container" in snap.healthz_body


# ---------------------------------------------------------------------------
# Assume-running probe (kind in {infra, ui} with no endpoint) — R12 / Q14
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assume_running_probe_running_unmonitored_when_docker_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``docker`` binary on PATH → ``state == "running_unmonitored"``.

    R12 / Q14 contract: subprocess failures (FileNotFoundError on
    spawn) must be classified as ``running_unmonitored`` rather than
    raising — the lifecycle state cache needs a deterministic reading
    per cycle.
    """

    async def _raise_file_not_found(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise FileNotFoundError(2, "No such file or directory: 'docker'")

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _raise_file_not_found
    )

    transport = httpx.MockTransport(lambda req: httpx.Response(599))
    probe, client = _make_probe(transport)
    try:
        snap = await probe.probe(
            _entry(name="redis", kind="infra", health_endpoint=None)
        )
    finally:
        await client.aclose()

    assert snap.state == "running_unmonitored"
    assert snap.healthz_status == -1
    assert snap.readyz_status is None
    assert snap.readyz_body is None
    assert "docker inspect unavailable" in snap.healthz_body


@pytest.mark.parametrize(
    ("docker_status", "expected_state"),
    [
        ("healthy", "healthy"),
        ("unhealthy", "unhealthy"),
        ("starting", "starting"),
        ("", "running_unmonitored"),
        ("<no value>", "running_unmonitored"),
        ("garbage", "running_unmonitored"),
    ],
)
@pytest.mark.asyncio
async def test_assume_running_probe_maps_docker_inspect_status(
    monkeypatch: pytest.MonkeyPatch,
    docker_status: str,
    expected_state: str,
) -> None:
    """Map every ``docker inspect`` ``.State.Health.Status`` deterministically.

    R12 / Q14 — the contract is:

    * ``"healthy"`` → ``state="healthy"``
    * ``"unhealthy"`` → ``state="unhealthy"``
    * ``"starting"`` → ``state="starting"``
    * ``""`` / ``"<no value>"`` / unknown → ``state="running_unmonitored"``
    """

    captured_cmd: list[tuple] = []

    class _FakeProc:
        def __init__(self, stdout_text: str) -> None:
            self._stdout_text = stdout_text
            self.returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return self._stdout_text.encode("utf-8") + b"\n", b""

        def kill(self) -> None:  # pragma: no cover - never timed out
            pass

    async def _fake_create_subprocess_exec(
        *args, **kwargs  # noqa: ANN001, ANN002, ANN003
    ) -> _FakeProc:
        captured_cmd.append(args)
        return _FakeProc(stdout_text=docker_status)

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
    )

    transport = httpx.MockTransport(lambda req: httpx.Response(599))
    probe, client = _make_probe(transport)
    try:
        snap = await probe.probe(
            _entry(
                name="redis",
                kind="infra",
                compose_service_name="redis",
                health_endpoint=None,
            )
        )
    finally:
        await client.aclose()

    assert snap.state == expected_state
    assert snap.healthz_status == -1
    assert snap.readyz_status is None
    assert snap.readyz_body is None

    # The command shape must match the design contract:
    # ``docker inspect <container_name> --format '{{.State.Health.Status}}'``
    assert captured_cmd, "docker inspect was never invoked"
    assert captured_cmd[0] == (
        "docker",
        "inspect",
        "redis",
        "--format",
        "{{.State.Health.Status}}",
    )


@pytest.mark.asyncio
async def test_assume_running_probe_running_unmonitored_on_subprocess_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung ``docker inspect`` is bounded by the 5 s timeout.

    The subprocess exceeding :data:`_DOCKER_INSPECT_TIMEOUT_SECONDS`
    must yield ``state="running_unmonitored"`` with a diagnostic body
    that pinpoints the timeout, not raise.
    """

    import src.lifecycle.health_probe as hp

    # Shrink the timeout so the test finishes promptly.
    monkeypatch.setattr(hp, "_DOCKER_INSPECT_TIMEOUT_SECONDS", 0.05)

    class _HangingProc:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(1.0)
            return b"", b""

        def kill(self) -> None:
            # No-op for the test stub.
            self.returncode = -9

    async def _fake_create_subprocess_exec(
        *args, **kwargs  # noqa: ANN001, ANN002, ANN003
    ) -> _HangingProc:
        return _HangingProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
    )

    transport = httpx.MockTransport(lambda req: httpx.Response(599))
    probe, client = _make_probe(transport)
    try:
        snap = await probe.probe(
            _entry(name="redis", kind="infra", health_endpoint=None)
        )
    finally:
        await client.aclose()

    assert snap.state == "running_unmonitored"
    assert "timed out" in snap.healthz_body


@pytest.mark.asyncio
async def test_assume_running_probe_running_unmonitored_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-zero ``docker inspect`` exit (e.g. unknown container) →
    ``running_unmonitored`` with stderr surfaced in the diagnostic
    body."""

    class _FailingProc:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"Error: No such object: redis\n"

        def kill(self) -> None:  # pragma: no cover - not timed out
            pass

    async def _fake_create_subprocess_exec(
        *args, **kwargs  # noqa: ANN001, ANN002, ANN003
    ) -> _FailingProc:
        return _FailingProc()

    monkeypatch.setattr(
        asyncio, "create_subprocess_exec", _fake_create_subprocess_exec
    )

    transport = httpx.MockTransport(lambda req: httpx.Response(599))
    probe, client = _make_probe(transport)
    try:
        snap = await probe.probe(
            _entry(name="redis", kind="infra", health_endpoint=None)
        )
    finally:
        await client.aclose()

    assert snap.state == "running_unmonitored"
    assert "exited 1" in snap.healthz_body
    assert "No such object" in snap.healthz_body


# ---------------------------------------------------------------------------
# Snapshot shape (design §4.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_is_immutable() -> None:
    """:class:`HealthSnapshot` is frozen so callers cannot mutate it."""

    import dataclasses

    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="ok"))
    probe, client = _make_probe(transport)
    try:
        snap = await probe.probe(_entry())
    finally:
        await client.aclose()

    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.state = "unhealthy"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_snapshot_timestamp_is_utc_aware() -> None:
    """``ts`` is a timezone-aware UTC ``datetime``."""

    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="ok"))
    probe, client = _make_probe(transport)
    try:
        snap = await probe.probe(_entry())
    finally:
        await client.aclose()

    assert snap.ts.tzinfo is not None
    assert snap.ts.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
