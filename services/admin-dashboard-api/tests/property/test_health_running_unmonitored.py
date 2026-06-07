"""Docker Inspect Health State Mapping (Q14).
Docker Inspect Health State Mapping (Q14)**
For any docker inspect `.State.Health.Status` output `S`, the
`_probe_assume_running` behaviour is deterministic:
- ``S == "healthy"``    snapshot.state = ``"healthy"``
- ``S == "unhealthy"``  snapshot.state = ``"unhealthy"``
- ``S == "starting"``   snapshot.state = ``"starting"``
- ``S == ""`` or ``S == "<no value>"`` or subprocess fail
  (timeout, FileNotFoundError)  snapshot.state = ``"running_unmonitored"``
- Any unknown value    snapshot.state = ``"running_unmonitored"``
The old ``unknown`` literal is **not** emitted by ``_probe_assume_running``;
it is retained in ``HealthState`` for backwards compatibility only.
Strategy
--------
We mock ``asyncio.create_subprocess_exec`` to return a fake process that
yields a configurable stdout string. Hypothesis generates:
1. **Known-good statuses** - drawn from the three mapped values
   (``"healthy"``, ``"unhealthy"``, ``"starting"``); the expected state
   is the same string.
2. **Unmonitored statuses** - drawn from the empty string, the Go-template
   sentinel ``"<no value>"``, and arbitrary strings that are *not* in the
   known-good set; the expected state is always ``"running_unmonitored"``.
3. **Subprocess failure modes** - ``FileNotFoundError`` on spawn and a
   simulated timeout; both must yield ``"running_unmonitored"``.
All three groups are exercised as separate ``@given`` properties so that
Hypothesis can shrink counterexamples independently."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap - allow running directly under tests/property/
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.health_probe import (  # noqa: E402
    HealthProbe,
    HealthSnapshot,
    _DOCKER_HEALTH_STATUS_MAP,
)
from src.manifest import ManagedServiceEntry  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _infra_entry(name: str = "redis") -> ManagedServiceEntry:
    """Return a minimal infra entry with no HTTP health endpoint."""
    return ManagedServiceEntry(
        name=name,
        kind="infra",
        compose_service_name=name,
        compose_profile=name,
        env_example_path=f"services/{name}/.env.example",
        health_endpoint=None,
        test_command=None,
    )


def _make_probe() -> tuple[HealthProbe, httpx.AsyncClient]:
    """Wire a HealthProbe with a no-op HTTP transport (never used for infra)."""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(599))
    )
    probe = HealthProbe(
        http_client=client,
        temporal_host="temporal:7233",
    )
    return probe, client


# ---------------------------------------------------------------------------
# Fake subprocess helpers
# ---------------------------------------------------------------------------


def _make_fake_proc(stdout_text: str, returncode: int = 0):
    """Return a fake asyncio subprocess that yields ``stdout_text``."""

    class _FakeProc:
        def __init__(self) -> None:
            self.returncode = returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            return stdout_text.encode("utf-8") + b"\n", b""

        def kill(self) -> None:  # pragma: no cover
            pass

    return _FakeProc()


def _make_hanging_proc():
    """Return a fake asyncio subprocess that hangs forever (simulates timeout)."""

    class _HangingProc:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(9999)
            return b"", b""

        def kill(self) -> None:
            self.returncode = -9

    return _HangingProc()


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# The three docker health statuses that map to a non-unmonitored state.
_KNOWN_STATUSES = sorted(_DOCKER_HEALTH_STATUS_MAP.keys())  # healthy, starting, unhealthy

# Statuses that must always yield running_unmonitored.
_UNMONITORED_SENTINELS = st.sampled_from(["", "<no value>"])

# Arbitrary strings that are NOT in the known-good set.
_UNKNOWN_STATUS = st.text(
    alphabet=st.characters(
        whitelist_categories=("Lu", "Ll", "Nd"),
        whitelist_characters="-_",
    ),
    min_size=1,
    max_size=32,
).filter(lambda s: s not in _DOCKER_HEALTH_STATUS_MAP and s not in ("", "<no value>"))


# ---------------------------------------------------------------------------
# - known-good statuses map deterministically
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(docker_status=st.sampled_from(_KNOWN_STATUSES))
def test_known_docker_status_maps_deterministically(
    docker_status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """- known docker health statuses map to the correct HealthState.
    For every ``S ∈ {"healthy", "unhealthy", "starting"}``, calling
    ``_probe_assume_running`` with a mocked ``docker inspect`` that returns
    ``S`` must yield ``snapshot.state == S`` deterministically."""
    expected_state = _DOCKER_HEALTH_STATUS_MAP[docker_status]

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return _make_fake_proc(docker_status)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    probe, client = _make_probe()

    async def _run() -> HealthSnapshot:
        try:
            return await probe.probe(_infra_entry())
        finally:
            await client.aclose()

    snap = asyncio.run(_run())

    assert snap.state == expected_state, (
        f"docker status {docker_status!r} should map to {expected_state!r}, "
        f"got {snap.state!r}"
    )
    assert snap.healthz_status == -1
    assert snap.readyz_status is None
    assert snap.readyz_body is None
    # The state must never be the legacy "unknown" literal from _probe_assume_running.
    assert snap.state != "unknown", (
        "_probe_assume_running must not emit 'unknown'; "
        "use 'running_unmonitored' for unobservable containers"
    )


# ---------------------------------------------------------------------------
# - sentinel / empty / unknown statuses  running_unmonitored
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    docker_status=st.one_of(
        _UNMONITORED_SENTINELS,
        _UNKNOWN_STATUS,
    )
)
def test_unmonitored_docker_status_yields_running_unmonitored(
    docker_status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """- empty / sentinel / unknown statuses  running_unmonitored.
    For any ``S ∉ {"healthy", "unhealthy", "starting"}``, including the
    empty string and the Go-template ``"<no value>"`` sentinel,
    ``_probe_assume_running`` must yield ``snapshot.state == "running_unmonitored"``."""

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        return _make_fake_proc(docker_status)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    probe, client = _make_probe()

    async def _run() -> HealthSnapshot:
        try:
            return await probe.probe(_infra_entry())
        finally:
            await client.aclose()

    snap = asyncio.run(_run())

    assert snap.state == "running_unmonitored", (
        f"docker status {docker_status!r} should yield 'running_unmonitored', "
        f"got {snap.state!r}"
    )
    assert snap.state != "unknown", (
        "_probe_assume_running must not emit 'unknown'"
    )


# ---------------------------------------------------------------------------
# - subprocess failure modes  running_unmonitored
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(failure_mode=st.sampled_from(["file_not_found", "timeout", "nonzero_exit"]))
def test_subprocess_failure_yields_running_unmonitored(
    failure_mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """- subprocess failures always yield running_unmonitored.
    Three subprocess failure modes must all map to ``"running_unmonitored"``:
    * ``FileNotFoundError`` on spawn (no ``docker`` binary on PATH).
    * Timeout (``asyncio.TimeoutError`` from ``wait_for``).
    * Non-zero exit code (container not found, Docker daemon error)."""
    import src.lifecycle.health_probe as hp

    if failure_mode == "file_not_found":
        async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            raise FileNotFoundError(2, "No such file or directory: 'docker'")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    elif failure_mode == "timeout":
        # Shrink the timeout so the test finishes quickly.
        monkeypatch.setattr(hp, "_DOCKER_INSPECT_TIMEOUT_SECONDS", 0.02)

        async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            return _make_hanging_proc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    else:  # nonzero_exit
        async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            return _make_fake_proc("", returncode=1)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    probe, client = _make_probe()

    async def _run() -> HealthSnapshot:
        try:
            return await probe.probe(_infra_entry())
        finally:
            await client.aclose()

    snap = asyncio.run(_run())

    assert snap.state == "running_unmonitored", (
        f"failure_mode={failure_mode!r} should yield 'running_unmonitored', "
        f"got {snap.state!r}"
    )
    assert snap.healthz_status == -1
    assert snap.readyz_status is None
    assert snap.state != "unknown", (
        "_probe_assume_running must not emit 'unknown'"
    )


# ---------------------------------------------------------------------------
# same input  same output (idempotency)
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    docker_status=st.one_of(
        st.sampled_from(_KNOWN_STATUSES),
        _UNMONITORED_SENTINELS,
        _UNKNOWN_STATUS,
    )
)
def test_probe_assume_running_is_deterministic(
    docker_status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """- _probe_assume_running is deterministic for any input.
    Calling ``_probe_assume_running`` twice with the same ``docker inspect``
    output must yield the same ``state`` both times. This confirms the
    mapping is a pure function of the docker status string."""

    call_count = 0

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        nonlocal call_count
        call_count += 1
        return _make_fake_proc(docker_status)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    probe, client = _make_probe()

    async def _run_twice() -> tuple[str, str]:
        try:
            snap1 = await probe.probe(_infra_entry())
            snap2 = await probe.probe(_infra_entry())
            return snap1.state, snap2.state
        finally:
            await client.aclose()

    state1, state2 = asyncio.run(_run_twice())

    assert state1 == state2, (
        f"docker status {docker_status!r}: first call returned {state1!r}, "
        f"second call returned {state2!r} - mapping must be deterministic"
    )
    assert call_count == 2, (
        "Expected exactly 2 docker inspect calls (one per probe), "
        f"got {call_count}"
    )
