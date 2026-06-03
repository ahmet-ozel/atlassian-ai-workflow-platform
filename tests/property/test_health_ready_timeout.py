"""invariant P1 — ``/healthz`` becomes 200 within ``HEALTH_READY_TIMEOUT_SECONDS``.



invariant
--------
For any drawn ``(delay, timeout_input)`` pair, after a successful
Compose ``up`` the:class:`LifecycleService.start` orchestrator
SHALL satisfy:

* ``state == "running"`` when the configured health probe transitions
 to ``healthy`` no later than the *clamped*
 ``HEALTH_READY_TIMEOUT_SECONDS`` (default ``60``, hard upper bound
 ``180``).
* ``state == "failed"`` when the health probe stays ``unhealthy``
 beyond the clamped timeout — independent of how far out-of-range
 the operator-supplied ``timeout_input`` was.

The property exercises both the normal case (``timeout_input`` inside
``[1, 180]``) and the **clamp** behaviour required by the operational rule: ``timeout_input <= 0`` clamps up to ``1`` second and any value
above ``180`` clamps down to ``180`` seconds. The clamp lives inside:class:`LifecycleService.__init__` (see
``services/admin-dashboard-api/src/lifecycle/service.py``); this test
asserts the *observable outcome* of the clamp rather than the
internal attribute, so the property keeps passing if the clamp is
later refactored as long as the contract is preserved.

Strategy
--------
``delay``: ``st.integers(min_value=0, max_value=180)``
 The "delay" in seconds before the stub ``HealthProbe.probe``
 starts returning ``healthy`` snapshots. The fake emits exactly
 ``2 * delay`` consecutive ``unhealthy`` snapshots (each polling
 step is:data:`_HEALTH_POLL_STEP_SECONDS` ``= 0.5`` seconds —
 see:func:`_wait_for_healthy`) before flipping to ``healthy``.
 With this mapping the orchestrator's polling loop reaches the
 healthy snapshot at virtual elapsed-time ``delay`` seconds.

``timeout_input``: ``st.integers(min_value=-30, max_value=300)``
 The raw value passed to ``LifecycleService(...,
 health_ready_timeout_seconds=...)``. Negative and zero values
 exercise the lower clamp; values above ``180`` exercise the
 upper clamp; values in ``[1, 180]`` flow through unchanged.

Predicate
---------
Let ``effective_timeout = min(max(1, timeout_input), 180)``.

The orchestrator's polling loop returns ``running`` iff
``delay <= effective_timeout`` and ``failed`` otherwise. This
follows from the loop body:

 while True:
 snap = await probe(entry)
 if snap.state in {"healthy", "unknown"}:
 return True
 if elapsed >= deadline:
 return False
 await sleep(0.5)
 elapsed += 0.5

For ``2 * delay`` unhealthy probes the most stringent timeout check
is ``(2*delay - 1) * 0.5 >= effective_timeout`` at the last
unhealthy probe, which simplifies (for integer arguments) to
``delay > effective_timeout`` ⇔ failure.

Stub fakes
----------
Mirror the patterns in
``services/admin-dashboard-api/tests/unit/test_lifecycle_service.py``
and the sister invariant
``tests/property/test_stop_idempotent.py``: Vault writes succeed,
Compose ``up`` exits ``0``, audit precheck/write all return cleanly,
and ``asyncio.sleep`` is replaced by a no-op so the polling loop
runs in O(unhealthy_count) without consuming wall-clock time.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up; pytest registers it as an
# importable module, but we add ``tests/`` to ``sys.path`` defensively
# so this file works under direct ``python -m pytest tests/property``
# invocations too (mirrors the pattern used by ``test_stop_idempotent``).
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

# The ``admin-dashboard-api`` package is not pip-installed inside the
# test environment, so we expose its source tree on ``sys.path`` the
# same way the sister invariant do. This lets us
# ``import src.lifecycle.service`` directly.
_SERVICE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "services"
    / "admin-dashboard-api"
)
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.audit_writer import (  # noqa: E402
    AuditEntry,
    AuditWriteOutcome,
)
from src.lifecycle.compose_runner import ComposeResult  # noqa: E402
from src.lifecycle.health_probe import HealthSnapshot  # noqa: E402
from src.lifecycle.service import (  # noqa: E402
    DEFAULT_HEALTH_READY_TIMEOUT_SECONDS,
    LifecycleService,
    MAX_HEALTH_READY_TIMEOUT_SECONDS,
)
from src.manifest import ManagedServiceEntry  # noqa: E402

# ---------------------------------------------------------------------------
# Fakes (mirror services/admin-dashboard-api/tests/unit/test_lifecycle_service.py
# and tests/property/test_stop_idempotent.py)
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditWriter:
    """Records every audit interaction; never fails for this property."""

    precheck_calls: int = 0
    write_calls: list[AuditEntry] = field(default_factory=list)
    write_with_retry_calls: list[AuditEntry] = field(default_factory=list)

    async def precheck(self) -> None:
        self.precheck_calls += 1

    async def write(self, entry: AuditEntry) -> None:
        self.write_calls.append(entry)

    async def write_with_retry(self, entry: AuditEntry) -> AuditWriteOutcome:
        self.write_with_retry_calls.append(entry)
        return AuditWriteOutcome(deferred=False)


@dataclass
class _FakeVaultClient:
    """In-memory Vault stub — KV writes succeed unconditionally."""

    writes: list[tuple[str, str, str]] = field(default_factory=list)
    stored: dict[str, dict[str, str]] = field(default_factory=dict)

    async def write_env_override(
        self, *, service_name: str, key: str, value: str
    ) -> None:
        self.writes.append((service_name, key, value))
        self.stored.setdefault(service_name, {})[key] = value

    async def read_env_overrides(self, *, service_name: str) -> dict[str, str]:
        return dict(self.stored.get(service_name, {}))

    async def delete_env_override(  # pragma: no cover - unused here
        self, *, service_name: str, key: str
    ) -> None:
        self.stored.get(service_name, {}).pop(key, None)


@dataclass
class _FakeComposeRunner:
    """Compose stub — every ``up`` call exits 0 (the precondition for
 invariant). ``stop`` / ``logs`` / ``exec_test`` are not
 exercised by this property but are stubbed so the orchestrator
 can be constructed end-to-end."""

    up_calls: list[dict[str, Any]] = field(default_factory=list)

    async def up(
        self,
        *,
        profile: str,
        service_name: str,
        env_overrides: dict[str, str] | None = None,
    ) -> ComposeResult:
        self.up_calls.append(
            {
                "profile": profile,
                "service_name": service_name,
                "env_overrides": dict(env_overrides or {}),
            }
        )
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "up", "-d", service_name),
        )

    async def stop(  # pragma: no cover - not exercised by P1
        self, *, service_name: str, remove_volumes: bool = False
    ) -> ComposeResult:
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "stop", service_name),
        )

    async def logs(  # pragma: no cover - not exercised by P1
        self, *, service_name: str, tail: int, follow: bool
    ) -> ComposeResult:
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "logs", service_name),
        )

    async def exec_test(  # pragma: no cover - not exercised by P1
        self,
        *,
        service_name: str,
        argv: Sequence[str],
        stream: bool = False,
    ) -> Any:
        raise NotImplementedError


@dataclass
class _FakeHealthProbe:
    """Returns ``unhealthy_count`` consecutive ``unhealthy`` snapshots,
 then ``healthy`` from then on.

 The orchestrator's polling loop drives ``probe`` until it sees a
 ``healthy`` snapshot or the timeout fires. Modelling the delay as
 "N unhealthy snapshots before the first healthy one" maps cleanly
 to the elapsed-time the loop reaches when it observes the healthy
 state — see the predicate analysis in the module docstring.
 """

    unhealthy_count: int
    calls: list[ManagedServiceEntry] = field(default_factory=list)
    _emitted: int = 0

    async def probe(self, entry: ManagedServiceEntry) -> HealthSnapshot:
        self.calls.append(entry)
        is_unhealthy = self._emitted < self.unhealthy_count
        self._emitted += 1
        if is_unhealthy:
            return HealthSnapshot(
                ts=datetime.now(timezone.utc),
                healthz_status=503,
                healthz_body="not ready",
                readyz_status=503,
                readyz_body="db down",
                state="unhealthy",
            )
        return HealthSnapshot(
            ts=datetime.now(timezone.utc),
            healthz_status=200,
            healthz_body="ok",
            readyz_status=200,
            readyz_body="ok",
            state="healthy",
        )


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


_HTTP_ENV_EXAMPLE = (
    "# Plain config knob\n"
    "PORT=8080\n"
    "# A sensitive token\n"
    'API_TOKEN=""\n'
)

#: ``HEALTH_POLL_STEP_SECONDS`` is the cadence at which
#::meth:`LifecycleService._wait_for_healthy` re-probes between
#: unhealthy snapshots. We mirror it here as a literal so the test's
#: predicate stays in sync with the implementation; if the constant
#: ever changes the regression will surface as a clean assertion
#: failure rather than a silently-passing test.
_HEALTH_POLL_STEP_SECONDS: float = 0.5


def _build_workspace(tmp_path: Path) -> Path:
    """Materialise a synthetic workspace with one ``.env.example`` file."""

    http_dir = tmp_path / "services" / "automation-service"
    http_dir.mkdir(parents=True)
    (http_dir / ".env.example").write_text(_HTTP_ENV_EXAMPLE, encoding="utf-8")
    return tmp_path


def _entry() -> ManagedServiceEntry:
    return ManagedServiceEntry(
        name="automation-service",
        kind="http_service",
        compose_service_name="automation-service",
        compose_profile="automation-service",
        env_example_path="services/automation-service/.env.example",
        health_endpoint="/healthz",
        test_command=None,
    )


def _make_service(
    *,
    workspace_root: Path,
    health_ready_timeout_seconds: float,
    unhealthy_count: int,
) -> tuple[
    LifecycleService,
    _FakeAuditWriter,
    _FakeVaultClient,
    _FakeComposeRunner,
    _FakeHealthProbe,
]:
    audit = _FakeAuditWriter()
    vault = _FakeVaultClient()
    compose = _FakeComposeRunner()
    health = _FakeHealthProbe(unhealthy_count=unhealthy_count)

    async def _no_sleep(_seconds: float) -> None:
        # The polling loop's wall-clock cost is replaced by a no-op
        # so Hypothesis can fuzz arbitrary delay/timeout pairs in
        # constant real time. The loop's *virtual* elapsed-time
        # (the ``elapsed`` accumulator inside ``_wait_for_healthy``)
        # advances independently of this hook.
        return None

    svc = LifecycleService(
        manifest=(_entry(),),
        state=None,
        audit=audit,  # type: ignore[arg-type]
        vault=vault,  # type: ignore[arg-type]
        compose=compose,  # type: ignore[arg-type]
        health=health,  # type: ignore[arg-type]
        workspace_root=workspace_root,
        health_ready_timeout_seconds=health_ready_timeout_seconds,
        sleep=_no_sleep,
    )
    return svc, audit, vault, compose, health


def _effective_timeout(timeout_input: float) -> float:
    """Mirror the clamp applied by:class:`LifecycleService.__init__`.: default ``60``, hard upper bound ``180``;
 out-of-range values clamp to ``[1, 180]`` (lower bound chosen by
 the implementation to keep the polling loop well-defined).
 """

    return min(max(1.0, timeout_input), MAX_HEALTH_READY_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# invariant — invariant
# ---------------------------------------------------------------------------


@settings(
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    delay=st.integers(min_value=0, max_value=180),
    timeout_input=st.integers(min_value=-30, max_value=300),
)
def test_health_ready_timeout_property(
    delay: int,
    timeout_input: int,
    tmp_path_factory: Any,
) -> None:
    """invariant — ``state`` flips to ``running`` iff ``delay`` ≤
 clamped ``HEALTH_READY_TIMEOUT_SECONDS``.


 """

    workspace = _build_workspace(tmp_path_factory.mktemp("ws"))

    # The fake probe emits ``2 * delay`` unhealthy snapshots before
    # flipping to healthy; with the polling step at 0.5 s the
    # orchestrator's loop reaches the healthy snapshot at virtual
    # elapsed-time ``delay`` seconds — see the module docstring's
    # predicate derivation.
    unhealthy_count = 2 * delay

    svc, _, _, compose, health = _make_service(
        workspace_root=workspace,
        health_ready_timeout_seconds=float(timeout_input),
        unhealthy_count=unhealthy_count,
    )

    async def run() -> Any:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    response = asyncio.run(run())

    # The Compose stub always succeeds; the only deciding factor for
    # ``state`` is the health-probe loop's race against the clamped
    # timeout.
    assert len(compose.up_calls) == 1

    effective_timeout = _effective_timeout(float(timeout_input))
    expected_running = delay <= effective_timeout

    if expected_running:
        assert response.state == "running", (
            f"expected state='running' for delay={delay}s and "
            f"timeout_input={timeout_input}s "
            f"(effective_timeout={effective_timeout}s); got "
            f"state={response.state!r} after {len(health.calls)} probe(s)"
        )
    else:
        assert response.state == "failed", (
            f"expected state='failed' for delay={delay}s and "
            f"timeout_input={timeout_input}s "
            f"(effective_timeout={effective_timeout}s); got "
            f"state={response.state!r} after {len(health.calls)} probe(s)"
        )

    # The state cache always converges to the response's state — this
    # guards against a subtle bug where ``StartResponse.state`` is
    # built from a stale snapshot of the cache.
    assert svc.state_cache["automation-service"].state == response.state


# ---------------------------------------------------------------------------
# Concrete clamp anchors — clamp behaviour)
# ---------------------------------------------------------------------------


def test_default_timeout_is_60_seconds(tmp_path: Path) -> None:
    """Spec anchor: the default ``HEALTH_READY_TIMEOUT_SECONDS`` is 60."""

    assert DEFAULT_HEALTH_READY_TIMEOUT_SECONDS == 60.0


def test_max_timeout_is_180_seconds(tmp_path: Path) -> None:
    """Spec anchor: the hard upper bound is 180 seconds."""

    assert MAX_HEALTH_READY_TIMEOUT_SECONDS == 180.0


def test_timeout_input_above_180_clamps_down(tmp_path: Path) -> None:
    """``timeout_input=300`` clamps to ``180``; a probe needing 200 s
 therefore times out (``failed``) even though ``200 < 300``."""

    workspace = _build_workspace(tmp_path)
    # 200 seconds of unhealthy delay => 400 unhealthy snapshots.
    svc, _, _, _, _ = _make_service(
        workspace_root=workspace,
        health_ready_timeout_seconds=300.0,
        unhealthy_count=400,
    )

    async def run() -> Any:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    response = asyncio.run(run())
    assert response.state == "failed", (
        "timeout_input=300 must clamp to 180; a 200-second probe must "
        f"therefore fail, but got state={response.state!r}"
    )


def test_timeout_input_at_or_below_zero_clamps_up(tmp_path: Path) -> None:
    """``timeout_input=0`` clamps to ``1``; a probe needing 0 s
 succeeds, but a probe needing 2 s fails."""

    workspace = _build_workspace(tmp_path)

    # delay=0 => first probe is healthy, always running.
    svc_zero, _, _, _, _ = _make_service(
        workspace_root=workspace,
        health_ready_timeout_seconds=0.0,
        unhealthy_count=0,
    )

    async def run_zero() -> Any:
        return await svc_zero.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    assert asyncio.run(run_zero()).state == "running"

    # delay=2 seconds => 4 unhealthy probes; clamped timeout is 1 s
    # so this must fail.
    svc_two, _, _, _, _ = _make_service(
        workspace_root=workspace,
        health_ready_timeout_seconds=0.0,
        unhealthy_count=4,
    )

    async def run_two() -> Any:
        return await svc_two.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    assert asyncio.run(run_two()).state == "failed", (
        "timeout_input=0 must clamp to 1; a 2-second probe must "
        "therefore fail"
    )


def test_within_default_timeout_succeeds(tmp_path: Path) -> None:
    """Concrete sanity case — a probe that flips healthy after 30 s
 must succeed against the default 60 s timeout."""

    workspace = _build_workspace(tmp_path)
    svc, _, _, _, _ = _make_service(
        workspace_root=workspace,
        health_ready_timeout_seconds=DEFAULT_HEALTH_READY_TIMEOUT_SECONDS,
        unhealthy_count=60,  # 30 s at 0.5 s/step
    )

    async def run() -> Any:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    assert asyncio.run(run()).state == "running"
