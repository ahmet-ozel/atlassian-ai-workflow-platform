"""Behavioral tests for ``LifecycleService.stop`` idempotency.

For any sequence of actions drawn from ``["start", "stop", "stop",
"stop", "restart"]`` (with extra weight on ``stop`` so the strategy
exercises consecutive stop calls aggressively), the lifecycle service
SHALL satisfy the following invariants:

1. **No raise.** Every action in the trace returns normally — no
   ``Exception`` propagates out of :meth:`LifecycleService.stop` for
   any input sequence (the fakes are configured so Compose / Vault /
   Audit / Health all succeed unconditionally).
2. **Idempotent shape.** Whenever the trace ends with two consecutive
   ``"stop"`` actions, both calls return ``state="stopped"`` (a
   ``200 OK`` shape from the router's perspective) regardless of
   whether the first ``stop`` was a no-op or actually invoked
   Compose. The ``noop`` flag may differ between the two calls but
   the *state* must converge to ``"stopped"``.
3. **One audit row per action.** The fake :class:`AuditWriter`
   records exactly ``len(actions)`` rows in
   ``write_with_retry_calls`` — one per attempted action — for any
   trace of length 1..10. This ensures every attempted action records
   an audit entry.

Strategy
--------
``st.lists(st.sampled_from(["start", "stop", "stop", "stop",
"restart"]), min_size=1, max_size=10)``. The triple weighting on
``"stop"`` ensures Hypothesis frequently produces the
"two consecutive stops" tail we care about; the property still holds
when the sequence is e.g. ``["start", "restart", "stop", "stop"]`` or
``["stop", "stop", "stop"]`` (initial state is ``stopped``).

Stub fakes
----------
``_FakeVaultClient``, ``_FakeComposeRunner``, ``_FakeAuditWriter``,
``_FakeHealthProbe`` mirror the patterns in
``services/admin-dashboard-api/tests/unit/test_lifecycle_service.py``:
all calls succeed, the health probe returns ``healthy`` immediately,
and ``asyncio.sleep`` is replaced by a no-op so the start path does
not waste real wall-clock time inside Hypothesis examples.
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
# invocations too (mirrors the pattern used by ``test_compose_structure``).
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

# The ``admin-dashboard-api`` package is not pip-installed inside the
# test environment, so we expose its source tree on ``sys.path`` the
# same way the per-service unit tests do. This lets us
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
    LifecycleService,
    LifecycleStateCache,
)
from src.manifest import ManagedServiceEntry  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes (mirror services/admin-dashboard-api/tests/unit/test_lifecycle_service.py)
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditWriter:
    """Records every audit interaction and never fails for this test."""

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
    """In-memory Vault stub — KV writes succeed and round-trip on read."""

    writes: list[tuple[str, str, str]] = field(default_factory=list)
    stored: dict[str, dict[str, str]] = field(default_factory=dict)

    async def write_env_override(
        self, *, service_name: str, key: str, value: str
    ) -> None:
        self.writes.append((service_name, key, value))
        self.stored.setdefault(service_name, {})[key] = value

    async def read_env_overrides(self, *, service_name: str) -> dict[str, str]:
        return dict(self.stored.get(service_name, {}))

    async def delete_env_override(
        self, *, service_name: str, key: str
    ) -> None:  # pragma: no cover - unused here
        self.stored.get(service_name, {}).pop(key, None)


@dataclass
class _FakeComposeRunner:
    """Records every Compose call; all invocations succeed."""

    up_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_calls: list[dict[str, Any]] = field(default_factory=list)

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

    async def stop(
        self, *, service_name: str, remove_volumes: bool = False
    ) -> ComposeResult:
        self.stop_calls.append(
            {"service_name": service_name, "remove_volumes": remove_volumes}
        )
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "stop", service_name),
        )

    async def logs(  # pragma: no cover - not exercised by P3
        self, *, service_name: str, tail: int, follow: bool
    ) -> ComposeResult:
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "logs", service_name),
        )

    async def exec_test(  # pragma: no cover - not exercised by P3
        self,
        *,
        service_name: str,
        argv: Sequence[str],
        stream: bool = False,
    ) -> Any:  # type: ignore[override]
        raise NotImplementedError


@dataclass
class _FakeHealthProbe:
    """Always returns a ``healthy`` snapshot so ``start`` polls succeed."""

    calls: list[ManagedServiceEntry] = field(default_factory=list)

    async def probe(self, entry: ManagedServiceEntry) -> HealthSnapshot:
        self.calls.append(entry)
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
    workspace_root: Path,
) -> tuple[
    LifecycleService,
    _FakeAuditWriter,
    _FakeVaultClient,
    _FakeComposeRunner,
    _FakeHealthProbe,
]:
    audit = _FakeAuditWriter()
    vault = _FakeVaultClient()
    # Pre-seed Vault with the form-schema env_overrides for
    # ``automation-service`` so a ``restart`` action triggered before
    # any explicit ``start`` still finds matching overrides on the
    # Vault read path. Without this seed, the test would conflate
    # stop idempotency with form-schema matching.
    vault.stored.setdefault(
        "automation-service",
        {"PORT": "8080", "API_TOKEN": "secret"},
    )
    compose = _FakeComposeRunner()
    health = _FakeHealthProbe()

    async def _no_sleep(_seconds: float) -> None:
        return None

    svc = LifecycleService(
        manifest=(_entry(),),
        state=None,
        audit=audit,  # type: ignore[arg-type]
        vault=vault,  # type: ignore[arg-type]
        compose=compose,  # type: ignore[arg-type]
        health=health,  # type: ignore[arg-type]
        workspace_root=workspace_root,
        health_ready_timeout_seconds=1.0,
        sleep=_no_sleep,
    )
    return svc, audit, vault, compose, health


# ---------------------------------------------------------------------------
# Stop idempotency
# ---------------------------------------------------------------------------


_ACTIONS = ["start", "stop", "stop", "stop", "restart"]


async def _run_action(svc: LifecycleService, action: str) -> Any:
    """Execute a single named action against ``svc``.

    ``start`` requires a form-schema-matching env_overrides map; we
    always send the same fixed (PORT, API_TOKEN) pair so the schema
    check passes. ``restart`` reads the previous overrides from the
    fake Vault, which round-trips the same map (after the first
    ``start`` populates it).
    """

    if action == "start":
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )
    if action == "stop":
        return await svc.stop(
            name="automation-service", remove_volumes=False, actor="ops"
        )
    if action == "restart":
        return await svc.restart(name="automation-service", actor="ops")
    raise AssertionError(f"unsupported action: {action!r}")


@given(
    actions=st.lists(
        st.sampled_from(_ACTIONS),
        min_size=1,
        max_size=10,
    )
)
@settings(
    deadline=None,
    max_examples=30,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_stop_is_idempotent(actions: list[str], tmp_path_factory: Any) -> None:
    """``stop`` is idempotent across arbitrary action traces.

    * No call raises.
    * If the trace ends with two consecutive ``stop`` actions, both
      return ``state="stopped"``.
    * Exactly one ``write_with_retry`` audit row is recorded per
      attempted action.
    """

    workspace = _build_workspace(tmp_path_factory.mktemp("ws"))
    svc, audit, _, compose, _ = _make_service(workspace)

    async def run() -> list[Any]:
        results: list[Any] = []
        for action in actions:
            results.append(await _run_action(svc, action))
        return results

    # Invariant 1: no exception escapes any action invocation.
    results = asyncio.run(run())

    # Invariant 2: trailing consecutive ``stop`` calls converge to
    # ``state="stopped"``. The slice walks backwards from the tail
    # while the action is ``"stop"`` and asserts that every such
    # response carries ``state == "stopped"``. This covers the
    # single-trailing-stop case (length 1) as well as the
    # double-trailing-stop case the strategy weights heavily.
    for action, response in zip(reversed(actions), reversed(results)):
        if action != "stop":
            break
        # ``stop`` always returns a StopResponse with ``state`` field.
        assert response.state == "stopped", (
            f"trailing stop returned state={response.state!r} for trace {actions!r}"
        )

    # Invariant 2b: the final state in the cache is ``"stopped"`` whenever
    # the last action is ``stop``.
    if actions[-1] == "stop":
        slot = svc.state_cache["automation-service"]
        assert slot.state == "stopped", (
            f"final cache state {slot.state!r} for trace ending in stop: {actions!r}"
        )

    # Invariant 3: exactly one ``write_with_retry`` row per attempted
    # action. ``start`` writes one ``write_with_retry`` row in addition
    # to the ``write`` (pending) row; ``stop`` writes one
    # ``write_with_retry`` row; ``restart`` is stop+start so it writes
    # two ``write_with_retry`` rows. We count the expected total.
    expected_retry_rows = sum(
        2 if a == "restart" else 1 for a in actions
    )
    assert len(audit.write_with_retry_calls) == expected_retry_rows, (
        f"expected {expected_retry_rows} write_with_retry rows for trace "
        f"{actions!r}, got {len(audit.write_with_retry_calls)}"
    )

    # Invariant 3b: each row maps to one of the recorded actions and
    # carries an outcome of ``success``. (The trace never triggers
    # Compose/Vault/Audit failures because the fakes are unconditionally
    # green for this property.)
    for row in audit.write_with_retry_calls:
        assert row.action in {"start", "stop", "run_tests", "health_streak_alert"}, (
            f"unexpected audit action {row.action!r}"
        )
        assert row.outcome == "success", (
            f"unexpected audit outcome {row.outcome!r} for action {row.action!r}"
        )

    # Sanity: when the slot is already ``stopped`` the implementation
    # must short-circuit and never invoke ``compose.stop``. Count how
    # many consecutive trailing ``stop`` actions we have — only the
    # first one (if the prior state was non-stopped) calls Compose; the
    # rest must be no-ops.
    trailing_stops = 0
    for action in reversed(actions):
        if action == "stop":
            trailing_stops += 1
        else:
            break
    if trailing_stops >= 2:
        # The second (and any later) trailing stop is a guaranteed
        # no-op: state was already ``stopped`` after the first stop.
        # Each no-op stop must report ``noop=True``.
        for response in results[-(trailing_stops - 1):]:
            assert response.noop is True, (
                f"expected noop=True on trailing stop, got {response!r} "
                f"for trace {actions!r}"
            )


# ---------------------------------------------------------------------------
# Concrete examples (regression anchors for the property)
# ---------------------------------------------------------------------------


def test_stop_when_already_stopped_returns_noop(tmp_path: Path) -> None:
    """Concrete anchor: ``stop`` from the initial ``stopped`` state.

    The orchestrator returns ``state="stopped"``, ``noop=True`` and
    never invokes ``compose.stop``. Two consecutive calls write two
    audit rows.
    """

    workspace = _build_workspace(tmp_path)
    svc, audit, _, compose, _ = _make_service(workspace)

    async def run() -> tuple[Any, Any]:
        first = await svc.stop(
            name="automation-service", remove_volumes=False, actor="ops"
        )
        second = await svc.stop(
            name="automation-service", remove_volumes=False, actor="ops"
        )
        return first, second

    first, second = asyncio.run(run())

    assert first.state == "stopped"
    assert first.noop is True
    assert second.state == "stopped"
    assert second.noop is True

    # No Compose invocation when state was already ``stopped``.
    assert compose.stop_calls == []
    # Two write_with_retry rows — one per attempted stop.
    assert len(audit.write_with_retry_calls) == 2


def test_stop_after_start_then_stop_again_is_idempotent(tmp_path: Path) -> None:
    """Concrete anchor: ``start`` → ``stop`` → ``stop``.

    The first ``stop`` actually invokes Compose (state transitions
    ``running`` → ``stopped``); the second ``stop`` is a no-op.
    """

    workspace = _build_workspace(tmp_path)
    svc, audit, _, compose, _ = _make_service(workspace)

    async def run() -> tuple[Any, Any, Any]:
        started = await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )
        stopped_once = await svc.stop(
            name="automation-service", remove_volumes=False, actor="ops"
        )
        stopped_twice = await svc.stop(
            name="automation-service", remove_volumes=False, actor="ops"
        )
        return started, stopped_once, stopped_twice

    started, stopped_once, stopped_twice = asyncio.run(run())

    assert started.state == "running"
    assert stopped_once.state == "stopped"
    assert stopped_once.noop is False
    assert stopped_twice.state == "stopped"
    assert stopped_twice.noop is True

    # Compose.stop ran exactly once (the second stop short-circuited).
    assert len(compose.stop_calls) == 1

    # Audit rows: 1 from start (success) + 1 from stop (success) + 1
    # from stop-noop (success) = 3.
    assert len(audit.write_with_retry_calls) == 3
    assert all(row.outcome == "success" for row in audit.write_with_retry_calls)
