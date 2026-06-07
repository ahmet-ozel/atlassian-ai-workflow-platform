"""Audit entries are one-to-one with final action outcomes.



Behavior
--------
For any sequence of actions drawn from
``["start", "stop", "restart", "run_tests"]``, the lifecycle service
SHALL satisfy three simultaneous guarantees:

1. **One final outcome row per correlation_id**. Every successfully-attempted action results in exactly one
 audit row with ``outcome ∈ {"success", "failed"}`` carrying that
 action's ``correlation_id``. The ``start`` flow additionally writes
 one ``outcome="pending"`` row beforehand, but the property only
 counts the *final* row.

2. **Action count parity**. The number of distinct
 ``correlation_id``s that produced *any* audit row equals the number
 of attempted actions that did not raise a precondition error before
 any audit write happened (those raise before allocating a
 correlation_id). ``restart`` is internally implemented as ``stop ∘
 start`` so it produces **two** correlation_ids - one per leg.

3. **No Env_Override values in details_json**.
 For every audit row written across the whole trace, no value string
 from any ``env_overrides`` map (random ``st.text``) appears anywhere
 inside the row's serialised ``details_json``.

Strategy
--------
* ``actions`` - ``st.lists(st.sampled_from(["start", "stop", "restart",
 "run_tests"]), min_size=1, max_size=20)`` for the lifecycle action set.
* ``env_overrides`` - a fixed-key dict whose values are randomly
 generated ``st.text`` strings of length 8..40 from a printable
 alphabet (excluding ``"<"`` and ``">"`` so values cannot collide with
 the redaction sentinel and excluding whitespace so single-token
 serialisations stay tight).
* ``actions`` are interpreted against an in-memory ``LifecycleService``
 driven by deterministic fakes (mirrors ``test_stop_idempotent.py``
 and ``test_log_redaction.py``). The service's actual ``AuditWriter``
 is replaced by ``_FakeAuditWriter`` that stores every entry in a
 list - this acts as an in-memory test DB: no
 PostgreSQL is required and ``correlation_id``  rows mapping is
 observable directly.

Pre-condition handling
----------------------
``run_tests`` requires the service to be in the ``running`` state
 and ``start`` requires a form-schema-matching
``env_overrides`` dict. Both raise *before* writing any audit row, so
the property filters those actions out of the expected-count tally.
The implementation is:

* For ``run_tests`` we catch:class:`TestPreconditionError`. Those
 attempts produce *zero* audit rows, so they don't count toward the
 expected total.
* For other actions the fakes are unconditionally green, so no
 exception escapes.
"""

from __future__ import annotations

import asyncio
import json
import string
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up; pytest auto-loads it but we
# add ``tests/`` to ``sys.path`` defensively so this module also imports
# cleanly under a direct ``python -m pytest tests/property`` invocation
# (mirrors the pattern used by the other property tests in this folder).
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

# The ``admin-dashboard-api`` package is not pip-installed inside the
# test environment, so we expose its source tree on ``sys.path`` the
# same way the per-service unit tests do. This lets us
# ``import src.lifecycle.service`` directly (mirrors the other property
# tests in this folder).
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
from src.lifecycle.compose_runner import ComposeResult, TestResult  # noqa: E402
from src.lifecycle.health_probe import HealthSnapshot  # noqa: E402
from src.lifecycle.service import (  # noqa: E402
    LifecycleService,
    TestPreconditionError,
)
from src.manifest import ManagedServiceEntry  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes - unconditionally green; this property only inspects audit rows.
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditWriter:
    """In-memory replacement for:class:`AuditWriter`.

 Every audit interaction (``precheck``, ``write``,
 ``write_with_retry``) is recorded so the property can:

 * Count rows per ``correlation_id``.
 * Inspect ``outcome`` to identify "final" rows.
 * Walk every row's ``details_json`` and assert no Env_Override
 *value* string ever appears surface).

 The writer is intentionally always-green: no failures, no deferred
 queue. The property under test is *correctness of the audit row
 set*, not the deferred-queue retry semantics covered by the AuditWriter unit suite.
 """

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

    @property
    def all_entries(self) -> list[AuditEntry]:
        """Return every audit row written by the trace, in write order."""

        return [*self.write_calls, *self.write_with_retry_calls]


@dataclass
class _FakeVaultClient:
    """In-memory Vault stub - KV writes succeed and round-trip on read."""

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
    """Records every Compose call; all invocations succeed."""

    up_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_calls: list[dict[str, Any]] = field(default_factory=list)
    exec_test_calls: list[dict[str, Any]] = field(default_factory=list)

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

    async def logs(  # pragma: no cover - not exercised here
        self, *, service_name: str, tail: int, follow: bool
    ) -> ComposeResult:
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "logs", service_name),
        )

    async def exec_test(
        self,
        *,
        service_name: str,
        argv: Sequence[str],
        stream: bool = False,
    ) -> TestResult:
        self.exec_test_calls.append(
            {"service_name": service_name, "argv": tuple(argv), "stream": stream}
        )
        # Canonical pytest summary line so ``_parse_pytest_summary``
        # produces a structured ``TestSummary``. The numbers are fixed
        # but realistic - the property is on audit row counts, not the
        # summary content.
        stdout = "============== 3 passed in 0.42s ==============\n"
        return TestResult(
            exit_code=0,
            stdout=stdout,
            stderr="",
            argv=("docker", "compose", "exec", "-T", service_name, *argv),
        )


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
# Synthetic workspace
# ---------------------------------------------------------------------------


_MANIFEST_NAME = "automation-service"
_COMPOSE_SERVICE_NAME = "automation-service"
_ENV_EXAMPLE_RELPATH = f"services/{_MANIFEST_NAME}/.env.example"


# Single-key env example. Using a non-sensitive key (``PORT``) means
# ``_validate_env_overrides`` checks LHS-set parity but does NOT fire
# the "non-empty sensitive value" rule, so any random ``st.text`` value
# (including the empty string) is accepted. The fixed key set keeps the
# property focused on audit row semantics rather than form-schema
# matching; separate tests cover that behavior.
_ENV_EXAMPLE_TEXT = "# Plain config knob\nPORT=8080\n"

_ENV_KEYS: tuple[str, ...] = ("PORT",)


def _build_workspace(tmp_path: Path) -> Path:
    """Materialise a synthetic workspace with one ``.env.example`` file."""

    svc_dir = tmp_path / "services" / _MANIFEST_NAME
    svc_dir.mkdir(parents=True)
    (svc_dir / ".env.example").write_text(_ENV_EXAMPLE_TEXT, encoding="utf-8")
    return tmp_path


def _entry() -> ManagedServiceEntry:
    return ManagedServiceEntry(
        name=_MANIFEST_NAME,
        kind="http_service",
        compose_service_name=_COMPOSE_SERVICE_NAME,
        compose_profile=_MANIFEST_NAME,
        env_example_path=_ENV_EXAMPLE_RELPATH,
        health_endpoint="/healthz",
        # ``test_command`` must be present for ``run_tests`` to skip
        # the "no test_command" precondition error and proceed to the
        # state check (which is the precondition we *do* want to
        # exercise -.
        test_command=(
            "docker compose -f infra/docker-compose.yml exec "
            f"{_COMPOSE_SERVICE_NAME} pytest tests/integration/ -v"
        ),
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
    # Pre-seed Vault with the form-schema env_overrides so a
    # ``restart`` action triggered before any explicit ``start`` still
    # finds matching overrides on the Vault read path (the operational rule
    # 6.6). Without this seed, the first ``restart`` in a trace would
    # raise FormSchemaMismatchError and no audit row would be written
    # for that leg - conflating two distinct contracts.
    vault.stored.setdefault(_MANIFEST_NAME, {"PORT": "8080"})
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
# Strategies
# ---------------------------------------------------------------------------


_ACTIONS: tuple[str, ...] = ("start", "stop", "restart", "run_tests")


# Value alphabet for ``env_overrides`` values. We deliberately exclude:
# - ``<`` and ``>`` so values cannot collide with the literal
# ``<redacted>`` sentinel used by the log redactor.
# - whitespace so each value serialises as a single JSON string token
# and substring matching against ``details_json`` payloads is
# unambiguous.
# - control chars (the ``string.printable`` slice already excludes
# them via the ``[:-6]`` cut that drops ``\t\n\r\x0b\x0c``).
_VALUE_ALPHABET: str = "".join(
    c for c in string.printable[:-6] if c not in {"<", ">", " "}
)

_value_strategy: st.SearchStrategy[str] = st.text(
    alphabet=_VALUE_ALPHABET,
    min_size=8,
    max_size=40,
)


# ---------------------------------------------------------------------------
# Action runner
# ---------------------------------------------------------------------------


async def _run_action(
    svc: LifecycleService,
    action: str,
    env_overrides: dict[str, str],
) -> tuple[bool, Any]:
    """Execute a single named action against ``svc``.

 Returns ``(audit_written, response)``: ``audit_written`` is
 ``True`` when the action wrote at least one audit row (so the
 property's expected-count tally should include this attempt),
 ``False`` when a precondition error fired *before* any audit
 row was written.

 ``run_tests`` is the only action that raises a precondition
 error (state must be ``running``); when that fires we return
 ``(False, None)`` so the caller can exclude it from the count.
 Form-schema mismatches on ``start``/``restart`` would also raise
 pre-audit, but the property uses a fixed schema-matching key
 set so those never happen here.
 """

    if action == "start":
        resp = await svc.start(
            name=_MANIFEST_NAME,
            env_overrides=env_overrides,
            actor="ops",
        )
        return True, resp
    if action == "stop":
        resp = await svc.stop(
            name=_MANIFEST_NAME, remove_volumes=False, actor="ops"
        )
        return True, resp
    if action == "restart":
        resp = await svc.restart(name=_MANIFEST_NAME, actor="ops")
        return True, resp
    if action == "run_tests":
        try:
            resp = await svc.run_tests(name=_MANIFEST_NAME, actor="ops")
        except TestPreconditionError:
            # State was not ``running`` (or, theoretically, no
            # test_command) - the action raised before allocating a
            # correlation_id or writing any audit row. Excluded from
            # the expected count.
            return False, None
        return True, resp
    raise AssertionError(f"unsupported action: {action!r}")


# ---------------------------------------------------------------------------
# Property check
# ---------------------------------------------------------------------------


def _details_text(entry: AuditEntry) -> str:
    """Serialise ``entry.details_json`` to its on-the-wire string form.

 The actual ``AuditWriter`` writes ``json.dumps(details_json,
 default=str)`` to Postgres (see ``audit_writer._INSERT_SQL``); we
 reproduce that exact serialisation so the substring check matches
 what would land in the database.
 """

    return json.dumps(entry.details_json, default=str)


@given(
    actions=st.lists(
        st.sampled_from(_ACTIONS),
        min_size=1,
        max_size=20,
    ),
    # One value per fixed env key; keys are pinned so the form-schema
    # check passes deterministically.
    port_value=_value_strategy,
)
@settings(
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_audit_one_to_one(
    actions: list[str],
    port_value: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """One final audit row per correlation_id, with no values leaked.


 """

    workspace = _build_workspace(tmp_path_factory.mktemp("ws-audit"))
    svc, audit, _, _, _ = _make_service(workspace)

    env_overrides = {"PORT": port_value}

    async def run() -> int:
        attempted_with_audit = 0
        for action in actions:
            audited, _resp = await _run_action(svc, action, env_overrides)
            if audited:
                # ``restart`` is internally ``stop ∘ start`` - two
                # correlation_ids, two audit "actions" from the
                # auditing perspective.
                attempted_with_audit += 2 if action == "restart" else 1
        return attempted_with_audit

    expected_correlation_count = asyncio.run(run())

    # ----- Check 1: every correlation_id has exactly one final row -----
    # A "final" row has ``outcome ∈ {"success", "failed"}``. The
    # ``start`` flow additionally writes one ``outcome="pending"``
    # row beforehand - counted under ``write_calls`` but excluded
    # here because it is not the *final* outcome.
    by_corr: dict[UUID, list[AuditEntry]] = {}
    for row in audit.all_entries:
        by_corr.setdefault(row.correlation_id, []).append(row)

    for corr_id, rows in by_corr.items():
        finals = [r for r in rows if r.outcome in ("success", "failed")]
        assert len(finals) == 1, (
            f"correlation_id {corr_id} produced {len(finals)} final rows "
            f"(expected exactly 1); rows={rows!r} for trace {actions!r}"
        )
        # Sanity: the only non-final outcome we ever expect is
        # ``"pending"`` from the ``start`` flow.
        for r in rows:
            assert r.outcome in ("success", "failed", "pending"), (
                f"unexpected outcome {r.outcome!r} for correlation_id "
                f"{corr_id}"
            )

    # ----- Check 2: action count parity -----
    # Number of distinct correlation_ids with ≥1 audit row equals the
    # number of attempted actions whose lifecycle reached the
    # audit-allocating point. ``restart`` produces two correlation_ids
    # because it's internally ``stop ∘ start``.
    assert len(by_corr) == expected_correlation_count, (
        f"expected {expected_correlation_count} distinct correlation_ids "
        f"with audit rows for trace {actions!r}, got {len(by_corr)}"
    )

    # ----- Check 3: no Env_Override value leaks into details_json -----
    # forbids the *value* from ever appearing in any
    # audit field. We check the on-the-wire ``details_json``
    # serialisation across every row.
    leak_canaries = [v for v in env_overrides.values() if v]
    if leak_canaries:
        for row in audit.all_entries:
            payload_text = _details_text(row)
            for value in leak_canaries:
                assert value not in payload_text, (
                    f"Env_Override value {value!r} leaked into details_json "
                    f"for correlation_id {row.correlation_id} "
                    f"(action={row.action!r}, outcome={row.outcome!r}); "
                    f"payload={payload_text!r} for trace {actions!r}"
                )


# ---------------------------------------------------------------------------
# Concrete regression anchors (deterministic examples)
# ---------------------------------------------------------------------------


def test_start_writes_pending_then_one_final_row(tmp_path: Path) -> None:
    """Concrete anchor: a single ``start`` writes 1 pending + 1 final row.

 Both rows share the same ``correlation_id``; the final row carries
 ``outcome="success"`` because the fakes are unconditionally green.
 Pins the row-shape contract independent of the
 Hypothesis search order.
 """

    workspace = _build_workspace(tmp_path)
    svc, audit, _, _, _ = _make_service(workspace)

    async def run() -> Any:
        return await svc.start(
            name=_MANIFEST_NAME,
            env_overrides={"PORT": "8080"},
            actor="ops",
        )

    response = asyncio.run(run())

    # Exactly one pending row + one final success row, both same corr_id.
    assert len(audit.write_calls) == 1
    assert audit.write_calls[0].outcome == "pending"
    assert len(audit.write_with_retry_calls) == 1
    assert audit.write_with_retry_calls[0].outcome == "success"
    assert (
        audit.write_calls[0].correlation_id
        == audit.write_with_retry_calls[0].correlation_id
        == response.correlation_id
    )


def test_restart_emits_two_correlation_ids(tmp_path: Path) -> None:
    """Concrete anchor: ``restart`` writes audit rows under two corr_ids.

 ``restart`` is internally ``stop ∘ start``; each leg allocates its
 own ``correlation_id`` so the audit table holds rows under two
 distinct IDs after a single restart action.
 """

    workspace = _build_workspace(tmp_path)
    svc, audit, _, _, _ = _make_service(workspace)

    async def run() -> Any:
        return await svc.restart(name=_MANIFEST_NAME, actor="ops")

    asyncio.run(run())

    correlation_ids = {row.correlation_id for row in audit.all_entries}
    assert len(correlation_ids) == 2, (
        f"restart should produce 2 correlation_ids (stop + start), got "
        f"{len(correlation_ids)}: {correlation_ids!r}"
    )

    # Each correlation_id has exactly one final outcome row.
    by_corr: dict[UUID, list[AuditEntry]] = {}
    for row in audit.all_entries:
        by_corr.setdefault(row.correlation_id, []).append(row)
    for rows in by_corr.values():
        finals = [r for r in rows if r.outcome in ("success", "failed")]
        assert len(finals) == 1


def test_run_tests_without_running_state_writes_no_audit(tmp_path: Path) -> None:
    """Concrete anchor: ``run_tests`` from ``stopped`` is excluded from count.: ``run_tests`` requires ``state == "running"``. The
 precondition error fires *before* any audit row is written, so this
 attempt is correctly excluded from the expected-count
 tally.
 """

    workspace = _build_workspace(tmp_path)
    svc, audit, _, _, _ = _make_service(workspace)

    async def run() -> None:
        with pytest.raises(TestPreconditionError):
            await svc.run_tests(name=_MANIFEST_NAME, actor="ops")

    asyncio.run(run())

    # No audit rows at all.
    assert audit.write_calls == []
    assert audit.write_with_retry_calls == []


def test_env_override_value_never_appears_in_details_json(tmp_path: Path) -> None:
    """Concrete anchor: a uniquely-tagged value is absent from every row.

 Pins: ``details_json`` carries the *key list*
 (``env_keys``) but never the value. Independent of Hypothesis
 search order so a regression in the audit serialiser fails this
 test deterministically.
 """

    workspace = _build_workspace(tmp_path)
    svc, audit, _, _, _ = _make_service(workspace)

    canary = "CANARY-VALUE-MUST-NOT-LEAK-9f3a"

    async def run() -> None:
        await svc.start(
            name=_MANIFEST_NAME,
            env_overrides={"PORT": canary},
            actor="ops",
        )
        await svc.stop(
            name=_MANIFEST_NAME, remove_volumes=False, actor="ops"
        )

    asyncio.run(run())

    for row in audit.all_entries:
        payload = json.dumps(row.details_json, default=str)
        assert canary not in payload, (
            f"canary value leaked into details_json: action={row.action!r}, "
            f"outcome={row.outcome!r}, payload={payload!r}"
        )

    # Sanity: the start row's details_json carries the LHS key list.
    pending = next(r for r in audit.write_calls if r.outcome == "pending")
    assert pending.details_json.get("env_keys") == ["PORT"]


# ===========================================================================
# actor_role NOT NULL enforcement
# ===========================================================================
#
#
# Behavior statement:
#
# For every randomly-generated AuditEvent whose ``actor_role`` is
# NULL / empty / not one of the four RBAC roles, the application-
# layer ``AuditLogger.write`` MUST raise:class:`ValueError`
# BEFORE issuing the INSERT. The Postgres CHECK constraint
# (``audit_events.actor_role IS NOT NULL...`` declared in
# ``infra/postgres/init/10_automation.sql``) enforces the same
# rule at the database layer; the application guard exists so
# callers fail fast with a clear traceback.
#
# Strategy
# --------
# * ``invalid_role`` - ``st.one_of(st.none, st.just(""),
# whitespace-only strings, st.text filtered to NOT be in
# AUDIT_ACTOR_ROLES)``. Each variant exercises a different branch
# of:class:`AuditLogger.write`'s validation (None / empty /
# unknown role).
# * ``valid_role`` - ``st.sampled_from(AUDIT_ACTOR_ROLES)`` for the
# positive case: every accepted role MUST round-trip through to
# the underlying writer's ``insert_audit`` method, with no
# ValueError raised.
# * ``action`` / ``resource`` - short ASCII strings to avoid the
# policy's interaction with text encoding; the property is on
# ``actor_role`` enforcement, not payload validation.
# * ``dept_id`` - ``st.one_of(st.none, short_string)`` because
# ``audit_events.dept_id`` is nullable for system-wide events
# wording: "actor_role NULL değildir" - silently
# excluding dept_id from the rule).
#
# This test is **separate** from ``test_audit_one_to_one`` above, which
# checks the correlation_id / value-leak behavior for lifecycle actions.

from datetime import datetime, timezone  # noqa: E402

from audit_logger import (  # noqa: E402
    AUDIT_ACTOR_ROLES,
    AUDIT_RESULTS,
    AuditEvent,
    AuditLogger,
)


# ---------------------------------------------------------------------------
# In-memory writer for actor_role enforcement
# ---------------------------------------------------------------------------


@dataclass
class _RecordingAuditWriter:
    """Bare-bones:class:`AuditWriter` that just records every accepted row.

 The full ``_FakeAuditWriter`` defined earlier in this file is
 tied to the lifecycle service's pending/final shape. This check only needs to observe whether ``insert_audit`` is reached
 at all, so a tiny dedicated fake keeps the assertions sharp.
 """

    inserted: list[AuditEvent] = field(default_factory=list)

    async def insert_audit(self, event: AuditEvent) -> None:
        self.inserted.append(event)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


# Short printable strings - used for ``action`` / ``resource`` / etc.
# We exclude control characters via ``st.characters`` so the values
# are well-behaved for ``json.dumps`` if a future caller serialises
# them.
_SHORT_ASCII: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=24,
)

# Whitespace-only strings - these MUST be rejected by the writer
# (a role that is just whitespace is semantically empty).
_WHITESPACE_ONLY: st.SearchStrategy[str] = st.sampled_from(
    (" ", "\t", " ", "\n", " \t \n")
)

# Text strings that are NOT one of the four valid roles. We filter
# the standard ``st.text`` strategy and then bias towards "looks
# plausibly like a role" with a sampled-from of common typos.
_TYPO_ROLES: st.SearchStrategy[str] = st.sampled_from(
    (
        "Admin",        # case mismatch
        "ADMIN",        # case mismatch
        "DeptAdmin",    # camel case
        "dept-admin",   # hyphen instead of underscore
        "superuser",    # not a role
        "owner",        # not a role
        "user",         # not a role
        "guest",        # not a role
        "system",       # ``system`` IS allowed for AuditEvent but
                        # this entry is here for the test variant
                        # that strips it from AUDIT_ACTOR_ROLES (we
                        # filter explicitly so the strategy stays
                        # robust if the role enum shrinks).
    )
).filter(lambda v: v not in AUDIT_ACTOR_ROLES)

# Combined invalid-role strategy.
_INVALID_ROLE: st.SearchStrategy[Any] = st.one_of(
    st.none(),
    st.just(""),
    _WHITESPACE_ONLY,
    _TYPO_ROLES,
)

# Valid-role strategy - sampled from the runtime mirror.
_VALID_ROLE: st.SearchStrategy[str] = st.sampled_from(sorted(AUDIT_ACTOR_ROLES))

# Result strategy.
_VALID_RESULT: st.SearchStrategy[str] = st.sampled_from(sorted(AUDIT_RESULTS))


# ---------------------------------------------------------------------------
# Invalid actor_role MUST raise ValueError
# ---------------------------------------------------------------------------


@given(
    bad_role=_INVALID_ROLE,
    action=_SHORT_ASCII,
    resource=_SHORT_ASCII,
    result=_VALID_RESULT,
    dept_id=st.one_of(st.none(), _SHORT_ASCII),
)
@settings(
    deadline=None,
    max_examples=60,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_audit_logger_rejects_null_or_invalid_actor_role(
    bad_role: Any,
    action: str,
    resource: str,
    result: str,
    dept_id: str | None,
) -> None:
    """For every randomly-generated:class:`AuditEvent` whose
 ``actor_role`` is None / empty / whitespace-only / a known typo
 (``"Admin"``, ``"superuser"``,...),:meth:`AuditLogger.write`
 MUST raise:class:`ValueError` and MUST NOT call the underlying
 writer's ``insert_audit``. This pins the application-layer
 guard; the Postgres ``CHECK`` constraint enforces the same
 rule at the database layer.
 """

    writer = _RecordingAuditWriter()
    logger = AuditLogger(writer=writer)

    # ``AuditEvent`` is a frozen dataclass with a ``Literal``
    # annotation on ``actor_role``; the type is erased at runtime
    # so we can hand it any value via the ``# type: ignore``
    # construction below. This is the realistic failure mode: a
    # caller bypasses the type checker (or builds the event from
    # untrusted dict input) and trusts the writer to surface the
    # error.
    event = AuditEvent(
        actor_id="test-actor",
        actor_role=bad_role,  # type: ignore[arg-type]
        dept_id=dept_id,
        action=action,
        resource=resource,
        result=result,  # type: ignore[arg-type]
        timestamp=datetime.now(timezone.utc),
        payload=None,
    )

    async def run() -> None:
        with pytest.raises(ValueError) as exc_info:
            await logger.write(event)
        # The error message MUST mention OR the
        # offending value so the operator can pivot from the
        # traceback to the audit role rule.
        msg = str(exc_info.value)
        assert (
            "actor_role" in msg
            or "the operational rule" in msg
            or "audit" in msg.lower()
        ), (
            f"ValueError message {msg!r} should mention actor_role / "
            "audit / the operational rule to help operators triage"
        )

    asyncio.run(run())

    # Critically: NO row was inserted. The application-layer guard
    # prevents the bad event from ever reaching the underlying
    # writer (and therefore the database).
    assert writer.inserted == [], (
        "AuditLogger.write MUST raise ValueError BEFORE delegating "
        "to AuditWriter.insert_audit; instead, "
        f"{len(writer.inserted)} row(s) were forwarded for "
        f"actor_role={bad_role!r}"
    )


# ---------------------------------------------------------------------------
# Valid actor_role admits the event
# ---------------------------------------------------------------------------


@given(
    valid_role=_VALID_ROLE,
    action=_SHORT_ASCII,
    resource=_SHORT_ASCII,
    result=_VALID_RESULT,
    dept_id=st.one_of(st.none(), _SHORT_ASCII),
)
@settings(
    deadline=None,
    max_examples=40,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_audit_logger_admits_every_known_role(
    valid_role: str,
    action: str,
    resource: str,
    result: str,
    dept_id: str | None,
) -> None:
    """For every member of:data:`AUDIT_ACTOR_ROLES` (``viewer``,
 ``lead``, ``admin``, ``dept_admin``, ``system``), an otherwise
 well-formed:class:`AuditEvent` MUST round-trip to the
 underlying writer's ``insert_audit`` exactly once. This is the
 positive companion of the rejection test above.
 """

    writer = _RecordingAuditWriter()
    logger = AuditLogger(writer=writer)

    event = AuditEvent(
        actor_id="test-actor",
        actor_role=valid_role,  # type: ignore[arg-type]
        dept_id=dept_id,
        action=action,
        resource=resource,
        result=result,  # type: ignore[arg-type]
        timestamp=datetime.now(timezone.utc),
        payload=None,
    )

    async def run() -> None:
        await logger.write(event)

    asyncio.run(run())

    assert len(writer.inserted) == 1, (
        f"valid actor_role={valid_role!r} must produce exactly one "
        f"insert_audit call; got {len(writer.inserted)}"
    )
    assert writer.inserted[0] is event, (
        "The writer must receive the original AuditEvent unchanged "
        "- AuditLogger only validates, it does not transform"
    )


# ---------------------------------------------------------------------------
# Concrete regression anchors for actor_role enforcement
# ---------------------------------------------------------------------------


def test_audit_logger_rejects_none_actor_role_concrete() -> None:
    """Concrete anchor: ``actor_role=None`` MUST raise.

 Pins the most common failure mode (a caller forgetting to set
 ``actor_role``) outside of the Hypothesis search.
 """

    writer = _RecordingAuditWriter()
    logger = AuditLogger(writer=writer)

    event = AuditEvent(
        actor_id="bot.payments.jira",
        actor_role=None,  # type: ignore[arg-type]
        dept_id="payments",
        action="capability_denied",
        resource="workflow:code_change_with_test",
        result="denied",
        timestamp=datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc),
        payload={"missing": ["bitbucket_write"]},
    )

    async def run() -> None:
        with pytest.raises(ValueError) as exc_info:
            await logger.write(event)
        assert "actor_role" in str(exc_info.value)

    asyncio.run(run())
    assert writer.inserted == []


def test_audit_logger_rejects_empty_actor_role_concrete() -> None:
    """Concrete anchor: ``actor_role=''`` MUST raise.

 Empty string is semantically "no role" and the writer rejects
 it with the same error class as the None case so callers can
 catch ValueError once.
 """

    writer = _RecordingAuditWriter()
    logger = AuditLogger(writer=writer)

    event = AuditEvent(
        actor_id="user-1",
        actor_role="",  # type: ignore[arg-type]
        dept_id=None,
        action="rbac_denied",
        resource="department:risk",
        result="denied",
        timestamp=datetime.now(timezone.utc),
        payload=None,
    )

    async def run() -> None:
        with pytest.raises(ValueError):
            await logger.write(event)

    asyncio.run(run())
    assert writer.inserted == []


def test_audit_logger_admits_system_role_concrete() -> None:
    """Concrete anchor: the ``system`` role is admitted (background events).

 Background processes - webhook handlers, probe runner, capability
 gate - write audit rows under the synthetic ``system`` actor_role.
 The role is NOT in the four-role RBAC enumeration but IS in:data:`AUDIT_ACTOR_ROLES` for exactly this reason.
 """

    writer = _RecordingAuditWriter()
    logger = AuditLogger(writer=writer)

    event = AuditEvent(
        actor_id="webhook-handler",
        actor_role="system",
        dept_id="payments",
        action="loop_guard_dropped",
        resource="webhook:jira/issue_commented",
        result="ok",
        timestamp=datetime.now(timezone.utc),
        payload={"reason": "actor.account_id == bot.payments.jira.account_id"},
    )

    async def run() -> None:
        await logger.write(event)

    asyncio.run(run())
    assert len(writer.inserted) == 1
    assert writer.inserted[0].actor_role == "system"
