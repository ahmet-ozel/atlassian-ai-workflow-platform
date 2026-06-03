"""Unit tests for ``src.lifecycle.compose_manager`` .
These tests exercise :class:`ComposeManager` as a black box with
:func:`asyncio.create_subprocess_exec` and :class:`httpx.AsyncClient`
patched out. Production behaviour we need to confirm:
* ``start_service`` / ``stop_service`` build the documented argv
  (``--profile {p} up -d`` and ``--profile {p} down``) and surface
  non-zero exits as :class:`ComposeManagerError`.
* The subprocess env is scrubbed to the allow-list (``PATH``,
  ``HOME``, ``DOCKER_HOST``) — host-side secrets must not leak
  .
* ``check_health`` polls until 200 lands or the budget elapses.
* ``get_running_services`` parses both the array and NDJSON layouts
  Compose ships across versions.
* ``StartedProfileStore`` is updated on success / cleared on stop.
* ``auto_start_persisted`` re-activates persisted profiles and
  tolerates per-profile failures.
* Profile names are validated and reject malformed inputs."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

# Match the convention used by ``test_compose_runner.py``: hook
# ``src.*`` imports under direct ``pytest tests/unit`` invocation.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.compose_manager import (  # noqa: E402
    ComposeManager,
    ComposeManagerError,
    InvalidProfileError,
    RunningService,
    ServiceStartResult,
    ServiceStopResult,
    StartedProfileStore,
    _parse_compose_ps_output,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
    ) -> None:
        self.returncode: int | None = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


class _SpawnRecorder:
    def __init__(self, processes: list[_FakeProcess]) -> None:
        self._queue = list(processes)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *argv: str, **kwargs: Any) -> _FakeProcess:
        self.calls.append({"argv": tuple(argv), "kwargs": kwargs})
        if not self._queue:
            return _FakeProcess()
        return self._queue.pop(0)


def _make_recorder(*processes: _FakeProcess) -> _SpawnRecorder:
    return _SpawnRecorder(list(processes))


class _FakeStore:
    """In-memory :class:`StartedProfileStore` implementation."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.start_calls: list[str] = []
        self.stop_calls: list[str] = []

    async def record_started(
        self, *, profile: str, services, started_at
    ) -> None:
        self.start_calls.append(profile)
        self.records[profile] = {
            "services": list(services),
            "started_at": started_at,
        }

    async def record_stopped(self, *, profile: str) -> None:
        self.stop_calls.append(profile)
        self.records.pop(profile, None)

    async def list_started_profiles(self) -> list[str]:
        return sorted(self.records.keys())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


COMPOSE_FILE = Path("/repo/infra/docker-compose.yml")


def _make_manager(
    *,
    transport: httpx.MockTransport | None = None,
    store: StartedProfileStore | None = None,
) -> ComposeManager:
    if transport is None:
        transport = httpx.MockTransport(
            lambda req: httpx.Response(503, text="not yet")
        )
    return ComposeManager(
        compose_file=COMPOSE_FILE,
        http_client=httpx.AsyncClient(transport=transport),
        store=store,
    )


# ---------------------------------------------------------------------------
# Profile validation
# ---------------------------------------------------------------------------


def test_start_service_rejects_empty_profile() -> None:
    """Empty profile string fails before the subprocess is spawned."""

    recorder = _make_recorder()
    with patch("asyncio.create_subprocess_exec", recorder):
        with pytest.raises(InvalidProfileError):
            asyncio.run(_make_manager().start_service(""))
    assert recorder.calls == []


@pytest.mark.parametrize(
    "bad_profile",
    [
        "-flag",  # leading hyphen would look like a flag to Compose
        "ab cd",  # whitespace
        "evil; rm -rf",  # shell metacharacters
        "",  # empty
        "x" * 65,  # too long (length cap is 64)
    ],
)
def test_start_service_rejects_malformed_profile(bad_profile: str) -> None:
    recorder = _make_recorder()
    with patch("asyncio.create_subprocess_exec", recorder):
        with pytest.raises(InvalidProfileError):
            asyncio.run(_make_manager().start_service(bad_profile))


# ---------------------------------------------------------------------------
# argv shape
# ---------------------------------------------------------------------------


def test_start_service_argv_shape() -> None:
    recorder = _make_recorder(_FakeProcess(returncode=0, stdout=b"ok"))
    with patch("asyncio.create_subprocess_exec", recorder):
        # Provide the second response for the get_running_services call
        # that ``start_service`` performs after success.
        recorder._queue.append(
            _FakeProcess(returncode=0, stdout=b"[]")
        )
        result = asyncio.run(
            _make_manager().start_service("automation-service")
        )

    assert isinstance(result, ServiceStartResult)
    assert result.exit_code == 0
    assert recorder.calls[0]["argv"] == (
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "--profile",
        "automation-service",
        "up",
        "-d",
    )


def test_stop_service_argv_shape() -> None:
    recorder = _make_recorder(_FakeProcess(returncode=0))
    with patch("asyncio.create_subprocess_exec", recorder):
        result = asyncio.run(_make_manager().stop_service("automation-service"))

    assert isinstance(result, ServiceStopResult)
    assert recorder.calls[0]["argv"] == (
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "--profile",
        "automation-service",
        "down",
    )


def test_get_running_services_argv_shape() -> None:
    recorder = _make_recorder(_FakeProcess(returncode=0, stdout=b"[]"))
    with patch("asyncio.create_subprocess_exec", recorder):
        result = asyncio.run(_make_manager().get_running_services())

    assert result == []
    assert recorder.calls[0]["argv"] == (
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "ps",
        "--format",
        "json",
    )


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_start_service_non_zero_raises() -> None:
    recorder = _make_recorder(
        _FakeProcess(returncode=1, stderr=b"image not found")
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        with pytest.raises(ComposeManagerError) as excinfo:
            asyncio.run(_make_manager().start_service("svc"))

    assert excinfo.value.exit_code == 1
    assert excinfo.value.stderr == "image not found"
    assert excinfo.value.argv[:2] == ("docker", "compose")


def test_start_service_failure_does_not_persist() -> None:
    """A failed start must not leave a stale persistence row."""

    store = _FakeStore()
    recorder = _make_recorder(
        _FakeProcess(returncode=1, stderr=b"boom")
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        with pytest.raises(ComposeManagerError):
            asyncio.run(
                _make_manager(store=store).start_service("svc")
            )

    assert store.start_calls == []
    assert store.records == {}


def test_stop_service_non_zero_raises_and_keeps_persistence() -> None:
    store = _FakeStore()
    asyncio.run(
        store.record_started(
            profile="svc", services=["svc"], started_at=datetime.now(timezone.utc)
        )
    )
    recorder = _make_recorder(
        _FakeProcess(returncode=2, stderr=b"no such network")
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        with pytest.raises(ComposeManagerError):
            asyncio.run(_make_manager(store=store).stop_service("svc"))

    # Failed stop leaves the persistence row intact so a retry can
    # complete the teardown rather than silently un-registering the
    # profile from the auto-restart manifest.
    assert store.stop_calls == []
    assert "svc" in store.records


def test_missing_docker_binary_surfaces_error() -> None:
    async def _boom(*args: Any, **kwargs: Any):
        raise FileNotFoundError("docker not found")

    with patch("asyncio.create_subprocess_exec", _boom):
        with pytest.raises(ComposeManagerError) as excinfo:
            asyncio.run(_make_manager().start_service("svc"))

    assert excinfo.value.exit_code == -1


# ---------------------------------------------------------------------------
# Environment scrubbing — host secrets must not leak
# ---------------------------------------------------------------------------


def test_environ_is_scrubbed_to_allowlist() -> None:
    recorder = _make_recorder(
        _FakeProcess(returncode=0),  # up
        _FakeProcess(returncode=0, stdout=b"[]"),  # ps after success
    )
    fake_environ = {
        "PATH": "/usr/local/bin:/usr/bin",
        "HOME": "/home/admin",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "VAULT_TOKEN": "host-side-vault-token",
        "OPENAI_API_KEY": "sk-host-leak",
        "AWS_SECRET_ACCESS_KEY": "abc123",
    }
    with patch.dict(os.environ, fake_environ, clear=True):
        with patch("asyncio.create_subprocess_exec", recorder):
            asyncio.run(_make_manager().start_service("svc"))

    spawned_env = recorder.calls[0]["kwargs"]["env"]
    assert spawned_env["PATH"] == "/usr/local/bin:/usr/bin"
    assert spawned_env["HOME"] == "/home/admin"
    assert spawned_env["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert "VAULT_TOKEN" not in spawned_env
    assert "OPENAI_API_KEY" not in spawned_env
    assert "AWS_SECRET_ACCESS_KEY" not in spawned_env


def test_no_shell_kwarg_passed_to_create_subprocess_exec() -> None:
    """Structural guarantee that no shell expansion can occur."""

    recorder = _make_recorder(
        _FakeProcess(returncode=0),
        _FakeProcess(returncode=0, stdout=b"[]"),
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        asyncio.run(_make_manager().start_service("svc"))

    call = recorder.calls[0]
    assert "shell" not in call["kwargs"]
    assert call["kwargs"]["stdout"] == asyncio.subprocess.PIPE
    assert call["kwargs"]["stderr"] == asyncio.subprocess.PIPE


# ---------------------------------------------------------------------------
# Persistence integration
# ---------------------------------------------------------------------------


def test_start_service_records_persistence_row_on_success() -> None:
    store = _FakeStore()
    ps_payload = (
        b'[{"Name":"svc","State":"running","Health":"healthy","Image":"x:1"}]'
    )
    recorder = _make_recorder(
        _FakeProcess(returncode=0),  # up
        _FakeProcess(returncode=0, stdout=ps_payload),  # ps lookup
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        asyncio.run(_make_manager(store=store).start_service("svc"))

    assert store.start_calls == ["svc"]
    assert store.records["svc"]["services"] == ["svc"]


def test_stop_service_clears_persistence_row_on_success() -> None:
    store = _FakeStore()
    asyncio.run(
        store.record_started(
            profile="svc",
            services=["svc"],
            started_at=datetime.now(timezone.utc),
        )
    )
    recorder = _make_recorder(_FakeProcess(returncode=0))
    with patch("asyncio.create_subprocess_exec", recorder):
        asyncio.run(_make_manager(store=store).stop_service("svc"))

    assert store.stop_calls == ["svc"]
    assert "svc" not in store.records


# ---------------------------------------------------------------------------
# auto_start_persisted
# ---------------------------------------------------------------------------


def test_auto_start_persisted_replays_recorded_profiles() -> None:
    store = _FakeStore()
    started_at = datetime.now(timezone.utc)
    asyncio.run(
        store.record_started(
            profile="svc-a", services=["svc-a"], started_at=started_at
        )
    )
    asyncio.run(
        store.record_started(
            profile="svc-b", services=["svc-b"], started_at=started_at
        )
    )

    # Two ``up`` calls, each followed by a ``ps`` lookup.
    recorder = _make_recorder(
        _FakeProcess(returncode=0),  # up svc-a
        _FakeProcess(returncode=0, stdout=b"[]"),  # ps after svc-a
        _FakeProcess(returncode=0),  # up svc-b
        _FakeProcess(returncode=0, stdout=b"[]"),  # ps after svc-b
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        restarted = asyncio.run(
            _make_manager(store=store).auto_start_persisted()
        )

    assert sorted(restarted) == ["svc-a", "svc-b"]
    # Both ``up`` invocations should have been issued.
    profile_argv_indices = [
        idx for idx, c in enumerate(recorder.calls) if "up" in c["argv"]
    ]
    assert len(profile_argv_indices) == 2


def test_auto_start_persisted_continues_on_failure() -> None:
    """A single broken profile must not block the remaining restarts."""

    store = _FakeStore()
    started_at = datetime.now(timezone.utc)
    asyncio.run(
        store.record_started(
            profile="bad", services=["bad"], started_at=started_at
        )
    )
    asyncio.run(
        store.record_started(
            profile="good", services=["good"], started_at=started_at
        )
    )

    recorder = _make_recorder(
        _FakeProcess(returncode=1, stderr=b"bad image"),  # up bad → fail
        _FakeProcess(returncode=0),  # up good
        _FakeProcess(returncode=0, stdout=b"[]"),  # ps after good
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        restarted = asyncio.run(
            _make_manager(store=store).auto_start_persisted()
        )

    # ``bad`` failed; ``good`` succeeded.
    assert restarted == ["good"]


def test_auto_start_persisted_no_store_returns_empty() -> None:
    recorder = _make_recorder()
    with patch("asyncio.create_subprocess_exec", recorder):
        result = asyncio.run(_make_manager(store=None).auto_start_persisted())

    assert result == []
    # No subprocess spawned when there's nothing to replay.
    assert recorder.calls == []


# ---------------------------------------------------------------------------
# check_health
# ---------------------------------------------------------------------------


def test_check_health_returns_true_on_first_200() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text="ok"))
    mgr = ComposeManager(
        compose_file=COMPOSE_FILE,
        http_client=httpx.AsyncClient(transport=transport),
    )
    assert asyncio.run(mgr.check_health("svc", "/healthz", timeout=5)) is True


def test_check_health_returns_false_when_endpoint_keeps_failing() -> None:
    """No 200 within the budget → False, no exception."""

    transport = httpx.MockTransport(lambda req: httpx.Response(503, text="bad"))

    # Use an injectable clock + sleep so the test does not actually
    # wait 5 real seconds. The clock advances by 2 seconds per ``sleep``
    # call so the loop exhausts the budget after a few iterations.
    elapsed = {"now": 1000.0}

    class _FakeClock:
        def timestamp(self) -> float:
            return elapsed["now"]

    def _clock() -> Any:
        return _FakeClock()

    async def _fake_sleep(seconds: float) -> None:
        elapsed["now"] += max(seconds, 1.0)

    mgr = ComposeManager(
        compose_file=COMPOSE_FILE,
        http_client=httpx.AsyncClient(transport=transport),
        clock=_clock,
        sleep=_fake_sleep,
    )
    assert asyncio.run(mgr.check_health("svc", "/healthz", timeout=3)) is False


def test_check_health_recovers_after_initial_503() -> None:
    """A 200 landing mid-poll terminates the loop with True."""

    counter = {"n": 0}

    def _handler(req: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        if counter["n"] < 3:
            return httpx.Response(503, text="warming up")
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(_handler)

    elapsed = {"now": 0.0}

    class _FakeClock:
        def timestamp(self) -> float:
            return elapsed["now"]

    async def _fake_sleep(seconds: float) -> None:
        elapsed["now"] += seconds

    mgr = ComposeManager(
        compose_file=COMPOSE_FILE,
        http_client=httpx.AsyncClient(transport=transport),
        clock=lambda: _FakeClock(),
        sleep=_fake_sleep,
    )
    assert asyncio.run(mgr.check_health("svc", "/healthz", timeout=10)) is True
    assert counter["n"] == 3


def test_check_health_rejects_invalid_args() -> None:
    mgr = _make_manager()
    with pytest.raises(ValueError):
        asyncio.run(mgr.check_health("", "/healthz"))
    with pytest.raises(ValueError):
        asyncio.run(mgr.check_health("svc", "healthz"))  # missing leading /
    with pytest.raises(ValueError):
        asyncio.run(mgr.check_health("svc", "/healthz", timeout=0))


# ---------------------------------------------------------------------------
# get_running_services parsing
# ---------------------------------------------------------------------------


def test_parse_compose_ps_output_handles_array_layout() -> None:
    payload = (
        '[{"Name":"a","State":"running","Health":"healthy","Image":"x:1"},'
        '{"Name":"b","State":"exited","Image":"x:2"}]'
    )
    rows = _parse_compose_ps_output(payload)

    assert [r.name for r in rows] == ["a", "b"]
    assert rows[0].state == "running"
    assert rows[0].health == "healthy"
    assert rows[0].image == "x:1"
    assert rows[1].health is None  # missing key → None


def test_parse_compose_ps_output_handles_ndjson_layout() -> None:
    """Older Compose versions emit one JSON object per line."""

    payload = (
        '{"Name":"a","State":"running"}\n'
        '{"Name":"b","State":"running"}\n'
    )
    rows = _parse_compose_ps_output(payload)

    assert [r.name for r in rows] == ["a", "b"]


def test_parse_compose_ps_output_handles_empty_string() -> None:
    assert _parse_compose_ps_output("") == []
    assert _parse_compose_ps_output("   \n  ") == []


def test_parse_compose_ps_output_skips_malformed_ndjson_lines() -> None:
    payload = (
        '{"Name":"a","State":"running"}\n'
        "this is not json\n"
        '{"Name":"b","State":"running"}\n'
    )
    rows = _parse_compose_ps_output(payload)
    assert [r.name for r in rows] == ["a", "b"]


def test_get_running_services_returns_typed_rows() -> None:
    payload = (
        b'[{"Name":"a","State":"running","Health":"healthy","Image":"x:1"}]'
    )
    recorder = _make_recorder(_FakeProcess(returncode=0, stdout=payload))
    with patch("asyncio.create_subprocess_exec", recorder):
        rows = asyncio.run(_make_manager().get_running_services())

    assert isinstance(rows, list)
    assert isinstance(rows[0], RunningService)
    assert rows[0].name == "a"
    assert rows[0].state == "running"


def test_get_running_services_non_zero_raises() -> None:
    recorder = _make_recorder(
        _FakeProcess(returncode=1, stderr=b"compose down")
    )
    with patch("asyncio.create_subprocess_exec", recorder):
        with pytest.raises(ComposeManagerError):
            asyncio.run(_make_manager().get_running_services())
