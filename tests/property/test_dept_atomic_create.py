"""Property tests for atomic department-create rollback (task 5.9, Property 6).

**Validates: Requirements 3.4, 3.6, 5.10, 9.3**

Property 6 — atomic department creation rolls back cleanly under arbitrary
DB failures. The companion module ``test_credential_inject.py`` covers the
plain-text leak invariants on the *success* path (response body, log
records, DB parameters, on-disk Vault store, heap scrub). This module
covers the *failure* path:

* **6f — staging keys deleted on DB INSERT failure**: when any
  ``connection.execute`` call raises a generic ``Exception`` (RDBMS
  outage, connection reset, constraint violation that isn't a duplicate
  key, etc.) the orchestrator must:
    1. delete every staging key it wrote in Vault, AND
    2. emit a ``dept_create_failed`` audit row carrying the offending
       request's ``actor_role`` (Requirement 3.6, 7.7), AND
    3. NOT promote any staging path to the final ``vault:atlassian/<dept>/<service>``
       location (no half-committed Vault state — Requirement 3.6).

* **6g — duplicate id surfaces as ``DepartmentAlreadyExistsError``**: when
  the dept INSERT raises a unique-violation, the orchestrator emits
  ``dept_duplicate_id`` audit (Requirement 3.9) and the staging keys are
  still deleted.

* **6h — failure during Vault staging→final promotion rolls Vault forward
  back**: when the final-path write itself fails mid-promotion, any
  already-promoted final paths are deleted before the surrounding
  transaction rolls back (Requirement 3.6 — no partially promoted
  Vault tree on the failure path).

The tests use Hypothesis to vary three inputs:

  * the plain-text token bytes (``_p6_plain_token_text``),
  * the index of the ``execute`` call that should fail
    (``failure_call_index``), and
  * (for 6h) the index of the bot whose Vault promotion should fail.

A small in-memory ``_FakeVaultBackend`` replaces the real Vault client so
the test suite can introspect every read/write/delete operation. The
orchestrator and the underlying ``with_dept_session`` context manager
remain unmocked: we exercise the actual rollback ordering rather than
asserting a synthetic shape.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from hypothesis import HealthCheck, given, settings, strategies as st


# ---------------------------------------------------------------------------
# sys.path bootstrapping (mirrors services/automation-service/tests/unit/test_app.py)
# ---------------------------------------------------------------------------

# We add **both** the ``services/automation-service/`` directory (so
# ``automation_service.app``'s ``from src.config import Settings`` resolves)
# and the inner ``src/`` directory (so the top-level ``automation_service``
# package import works).
_AUTOMATION_ROOT = (
    Path(__file__).resolve().parents[1].parent
    / "services"
    / "automation-service"
)
_AUTOMATION_SRC = _AUTOMATION_ROOT / "src"
for _p in (str(_AUTOMATION_SRC), str(_AUTOMATION_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


from audit_logger import AuditEvent  # noqa: E402

# ``automation_service.admin.__init__`` eagerly imports the FastAPI
# router from ``automation_service.admin.router``. The real router
# module now ships with task 5.3, so no stub is needed — the import
# below resolves to the production router.
from automation_service.admin.dept_create import (  # noqa: E402
    DepartmentAlreadyExistsError,
    DepartmentCreateOrchestrator,
    DepartmentCreateRequest,
    _BotCredential,
    _zero_bytearray,
)
from automation_service.probe import ProbeTargets  # noqa: E402


# ---------------------------------------------------------------------------
# The orchestrator ships ``_zero_all_tokens`` as a real method (task
# 5.3); no compatibility shim is needed.
# ---------------------------------------------------------------------------

from vault_client import VaultPath  # noqa: E402


# ---------------------------------------------------------------------------
# Shared event loop helper (mirrors test_credential_inject.py)
# ---------------------------------------------------------------------------

_LOOP: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP


def _run_async(coro: Any) -> Any:
    return _get_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeVaultBackend:
    """Tiny in-memory ``VaultClient``-shaped backend.

    Records every read/write/delete so the test can assert which
    paths exist after the orchestrator returns. We do **not** use
    :class:`LocalDevBackend` here — the secrecy invariants (encrypted
    on disk) are covered by Property 6d in the sibling module; this
    module focuses on *path lifecycle* (staging deleted, no final
    promotion on failure).
    """

    backend: str = "in-memory"
    store: dict[str, dict[str, str]] = field(default_factory=dict)
    write_calls: list[tuple[str, Mapping[str, str]]] = field(default_factory=list)
    delete_calls: list[str] = field(default_factory=list)
    write_failure_paths: frozenset[str] = field(default_factory=frozenset)

    def read(self, path: VaultPath) -> Mapping[str, str]:
        if path.raw not in self.store:
            raise KeyError(path.raw)
        return dict(self.store[path.raw])

    def write(self, path: VaultPath, data: Mapping[str, str]) -> None:
        if path.raw in self.write_failure_paths:
            raise RuntimeError(
                f"injected vault write failure for path {path.raw!r}"
            )
        self.write_calls.append((path.raw, dict(data)))
        self.store[path.raw] = dict(data)

    def delete(self, path: VaultPath) -> None:
        self.delete_calls.append(path.raw)
        self.store.pop(path.raw, None)

    # The orchestrator only calls read/write/delete on the create
    # path; rotation helpers are not exercised here. We still expose
    # the symbols so the runtime ``isinstance(VaultClient)`` check
    # in any future caller does not blow up.
    def rotate_ssh_key(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def clear_previous_ssh_slot(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    def rotate_webhook_secret(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError


class _FailingConnection:
    """asyncpg-shaped connection that raises on the *N*-th execute call.

    The orchestrator opens a transaction by issuing ``BEGIN``,
    setting two ``SET LOCAL`` GUCs, then running its
    INSERT statements. We let every call through except the one at
    ``failure_index`` (0-indexed against the full sequence beginning
    with ``BEGIN``) where we raise the supplied exception.

    Successful ``ROLLBACK`` is allowed regardless of ``failure_index``
    so the transaction unwinds cleanly.
    """

    def __init__(
        self,
        *,
        failure_index: int,
        failure_exc: BaseException,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._failure_index = failure_index
        self._failure_exc = failure_exc
        self._call_count = 0

    async def execute(self, query: str, *args: Any) -> Any:
        self.calls.append((query, args))
        # Always allow ROLLBACK / COMMIT to run so the context
        # manager terminates cleanly. The orchestrator's own
        # try/except must do the staging cleanup before the
        # transaction unwind anyway.
        if query.strip().upper() in {"ROLLBACK", "COMMIT"}:
            return None
        if self._call_count == self._failure_index:
            self._call_count += 1
            raise self._failure_exc
        self._call_count += 1
        return None


class _FakeProbeClient:
    """Probe client that always returns ``state=ok`` with auto-fetched ids.

    Method signatures preserve the keyword-argument names the
    :class:`ProbeRunner` uses internally so the fake matches the
    protocol contract exactly.
    """

    async def jira_myself(self, cred: Any) -> dict[str, Any]:
        return {"accountId": "auto-jira", "displayName": "Probe Bot"}

    async def jira_search_self_comments(
        self, cred: Any, author_account_id: str
    ) -> list[dict[str, Any]]:
        return []

    async def jira_create_self_comment(
        self, cred: Any, body: str
    ) -> dict[str, Any]:
        return {"id": "c1", "issue_key": "PROBE-1"}

    async def jira_delete_comment(
        self, cred: Any, *, issue_key: str, comment_id: str
    ) -> None:
        return None

    async def bitbucket_user(self, cred: Any) -> dict[str, Any]:
        return {"account_id": "auto-bb", "username": "probe-bot"}

    async def bitbucket_list_probe_branches(
        self, cred: Any, *, workspace: str, repo: str
    ) -> list[str]:
        return []

    async def bitbucket_create_branch(
        self, cred: Any, *, workspace: str, repo: str, branch_name: str
    ) -> str:
        return "deadbeef"

    async def bitbucket_delete_branch(
        self, cred: Any, *, workspace: str, repo: str, branch_name: str
    ) -> None:
        return None

    async def confluence_user(self, cred: Any) -> dict[str, Any]:
        return {"accountId": "auto-conf", "displayName": "Probe Bot"}

    async def confluence_list_probe_pages(
        self, cred: Any, *, space_key: str
    ) -> list[dict[str, Any]]:
        return []

    async def confluence_create_draft_page(
        self, cred: Any, *, space_key: str, title: str
    ) -> dict[str, Any]:
        return {"id": "p1", "title": title}

    async def confluence_delete_page(self, cred: Any, *, page_id: str) -> None:
        return None


class _RecordingAuditLogger:
    """Captures every ``AuditEvent`` for post-run inspection."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Plain-text token alphabet (utf-8 safe, distinctive). Same shape as
#: the sibling module so leak detection stays consistent.
import string as _string  # noqa: E402

_P6_TOKEN_ALPHABET = _string.ascii_letters + _string.digits + "+/=._-"

_p6_token_text = st.text(
    alphabet=_P6_TOKEN_ALPHABET, min_size=24, max_size=64
)


# The orchestrator issues this sequence of execute calls per bot:
#   0.  BEGIN
#   1.  SELECT set_config('app.current_dept_id', $1, true)
#   2.  SELECT set_config('app.current_role',    $1, true)
#   3.  INSERT INTO automation.departments ...
#   4.  INSERT INTO automation.department_bots ...
#   5.  INSERT INTO automation.department_project_keys ...
#   6.  COMMIT  (allowed through unconditionally)
#
# We inject the failure at index 3 (departments INSERT — realistic
# constraint violation surface), index 4 (department_bots INSERT),
# or index 5 (project_keys INSERT). Indices 0-2 are protocol /
# session-config calls that are not realistic failure surfaces; index
# 6 (COMMIT) is allowed through so we always reach the orchestrator's
# rollback paths.
_p6_failure_index = st.integers(min_value=3, max_value=5)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _build_request(plain_token: str) -> DepartmentCreateRequest:
    """Build a single-bot Jira-only create request (mirrors sibling tests)."""

    return DepartmentCreateRequest(
        dept_id="acme",
        display_name="Acme Engineering",
        default_language="en",
        web_search_enabled=False,
        mode="active",
        jira_project_keys=("ACME",),
        confluence_space_keys=(),
        bitbucket_workspace=None,
        config_json={"id": "acme"},
        bots=(
            _BotCredential(
                service="jira",
                url="https://acme.atlassian.net",
                username="bot@acme.test",
                personal_token=bytearray(plain_token.encode("utf-8")),
                account_id=None,
                deployment=None,
            ),
        ),
        probe_targets=ProbeTargets(),
    )


def _build_orchestrator(
    *,
    vault: _FakeVaultBackend,
    connection: _FailingConnection,
    audit: _RecordingAuditLogger,
) -> DepartmentCreateOrchestrator:
    async def _factory() -> _FailingConnection:
        return connection

    return DepartmentCreateOrchestrator(
        vault=vault,  # type: ignore[arg-type]
        connection_factory=_factory,
        probe_client=_FakeProbeClient(),
        audit_logger=audit,  # type: ignore[arg-type]
        clock=lambda: datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _staging_paths(vault: _FakeVaultBackend) -> list[str]:
    """Return all ``_staging`` paths currently held in the fake Vault."""

    return [p for p in vault.store if "_staging" in p]


def _final_paths(vault: _FakeVaultBackend) -> list[str]:
    """Return all final ``vault:atlassian/<dept>/<svc>`` paths."""

    return [
        p
        for p in vault.store
        if p.startswith("vault:atlassian/") and "_staging" not in p
    ]


# ---------------------------------------------------------------------------
# Property 6f — staging keys deleted on arbitrary DB INSERT failure
# ---------------------------------------------------------------------------


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(plain_token=_p6_token_text, failure_index=_p6_failure_index)
def test_p6f_staging_keys_deleted_on_db_insert_failure(
    plain_token: str,
    failure_index: int,
) -> None:
    """Property 6f — DB failure during INSERT triggers full staging cleanup.

    **Validates: Requirements 3.4, 3.6, 9.3**

    For every randomly chosen failure point inside the DB transaction
    (between staging-write and COMMIT), the orchestrator must:

    1. Re-raise the underlying exception (not silently swallow it).
    2. Delete every staging key it wrote so the Vault tree is clean.
    3. NOT have promoted any staging path to a final
       ``vault:atlassian/<dept>/<svc>`` location.
    4. Emit exactly one ``dept_create_failed`` audit row carrying the
       caller's ``actor_role`` (Requirement 7.7).
    5. Not leak the plain-text token into the audit payload.
    """

    vault = _FakeVaultBackend()
    failure_exc = RuntimeError("simulated DB outage")
    connection = _FailingConnection(
        failure_index=failure_index, failure_exc=failure_exc
    )
    audit = _RecordingAuditLogger()
    orchestrator = _build_orchestrator(
        vault=vault, connection=connection, audit=audit
    )

    request = _build_request(plain_token)

    async def _go() -> None:
        await orchestrator.run(
            request, actor_id="ops-1", actor_role="admin"
        )

    raised: BaseException | None = None
    try:
        _run_async(_go())
    except RuntimeError as exc:
        raised = exc
    except Exception as exc:  # pragma: no cover - defensive
        raised = exc

    # 1. The orchestrator must surface the underlying error.
    assert raised is not None, (
        "Property 6f violated: orchestrator swallowed an injected DB "
        "failure (Requirement 3.6 expects re-raise + rollback)."
    )

    # 2. Every staging path written must have been deleted.
    remaining_staging = _staging_paths(vault)
    assert remaining_staging == [], (
        f"Property 6f violated: staging keys still present after rollback "
        f"({remaining_staging!r}); Requirement 3.6 requires staging "
        f"cleanup on every failure path."
    )

    # 3. No staging path may have been promoted to a final path.
    remaining_finals = _final_paths(vault)
    assert remaining_finals == [], (
        f"Property 6f violated: final Vault paths exist after a failed "
        f"create ({remaining_finals!r}); the DB row was rolled back so "
        f"no final credential_ref may persist (Requirement 3.6)."
    )

    # Even though the store ended up empty, we should still have *seen*
    # at least one staging delete (the rollback). The orchestrator
    # iterates over every staging path in ``_best_effort_delete_staging``.
    assert vault.delete_calls, (
        "Property 6f violated: orchestrator did not invoke any vault.delete "
        "during rollback — Requirement 3.6 requires explicit cleanup."
    )
    for deleted_path in vault.delete_calls:
        assert "_staging" in deleted_path or deleted_path.startswith(
            "vault:atlassian/"
        ), (
            f"unexpected delete target {deleted_path!r} during rollback"
        )

    # 4. Exactly one ``dept_create_failed`` audit row carrying the
    #    actor_role.
    failed_events = [e for e in audit.events if e.action == "dept_create_failed"]
    assert len(failed_events) == 1, (
        f"Property 6f violated: expected exactly one dept_create_failed "
        f"audit event; got {len(failed_events)} (events="
        f"{[e.action for e in audit.events]!r})."
    )
    failed = failed_events[0]
    assert failed.actor_role == "admin", (
        f"audit event must carry actor_role='admin'; got "
        f"{failed.actor_role!r} (Requirement 7.7)."
    )
    assert failed.result == "error"
    assert failed.dept_id == request.dept_id

    # 5. Plain-text token must not appear in any audit payload.
    for event in audit.events:
        payload_repr = repr(event.payload or {})
        assert plain_token not in payload_repr, (
            f"Property 6f violated: token leaked into audit event "
            f"{event.action!r} payload (Requirement 3.4)."
        )
        # Also scan the resource / action strings (defence-in-depth).
        assert plain_token not in event.resource
        assert plain_token not in event.action


# ---------------------------------------------------------------------------
# Property 6g — duplicate id raises DepartmentAlreadyExistsError + cleanup
# ---------------------------------------------------------------------------


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(plain_token=_p6_token_text)
def test_p6g_duplicate_id_emits_audit_and_clears_staging(
    plain_token: str,
) -> None:
    """Property 6g — duplicate id surfaces ``DepartmentAlreadyExistsError``.

    **Validates: Requirements 3.6, 3.9, 7.7**

    When the dept INSERT fails with an asyncpg-style unique-violation
    error, the orchestrator must:

    1. Raise :class:`DepartmentAlreadyExistsError` (router → HTTP 409).
    2. Delete every staging key in Vault.
    3. Not promote any staging path to a final path.
    4. Emit exactly one ``dept_duplicate_id`` audit row with
       ``actor_role`` and ``result="denied"``.
    """

    vault = _FakeVaultBackend()
    # Mirror the substring pattern the orchestrator's
    # ``_looks_like_duplicate`` matcher recognises.
    failure_exc = Exception(
        "duplicate key value violates unique constraint \"departments_pkey\""
    )
    # Failure index 3 is the dept INSERT (BEGIN, SET LOCAL x2, INSERT
    # departments). The duplicate-id error must be raised by the
    # ``departments`` INSERT specifically — that's the table whose
    # primary key collides with an existing department.
    connection = _FailingConnection(failure_index=3, failure_exc=failure_exc)
    audit = _RecordingAuditLogger()
    orchestrator = _build_orchestrator(
        vault=vault, connection=connection, audit=audit
    )

    request = _build_request(plain_token)

    async def _go() -> None:
        await orchestrator.run(
            request, actor_id="ops-1", actor_role="admin"
        )

    raised: BaseException | None = None
    try:
        _run_async(_go())
    except DepartmentAlreadyExistsError as exc:
        raised = exc

    assert raised is not None, (
        "Property 6g violated: duplicate id was not surfaced as "
        "DepartmentAlreadyExistsError (Requirement 3.9)."
    )
    assert isinstance(raised, DepartmentAlreadyExistsError)
    assert raised.dept_id == request.dept_id

    # Staging cleanup invariant.
    assert _staging_paths(vault) == [], (
        "Property 6g violated: staging keys remain after duplicate-id "
        "rollback (Requirement 3.6)."
    )
    assert _final_paths(vault) == [], (
        "Property 6g violated: final paths exist despite duplicate-id "
        "failure (Requirement 3.6)."
    )

    # Audit invariants.
    duplicate_events = [
        e for e in audit.events if e.action == "dept_duplicate_id"
    ]
    assert len(duplicate_events) == 1, (
        f"expected exactly one dept_duplicate_id audit row; got "
        f"{[e.action for e in audit.events]!r} (Requirement 3.9)."
    )
    dup = duplicate_events[0]
    assert dup.actor_role == "admin"
    assert dup.result == "denied"
    assert dup.dept_id == request.dept_id


# ---------------------------------------------------------------------------
# Property 6h — failure during Vault staging→final promotion rolls back
# ---------------------------------------------------------------------------


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(plain_token=_p6_token_text)
def test_p6h_promotion_failure_rolls_back_vault_and_db(
    plain_token: str,
) -> None:
    """Property 6h — Vault promotion failure cleans up partial state.

    **Validates: Requirements 3.6, 9.3**

    When the staging → final ``write`` fails (eg. transient Vault
    HTTP 5xx during the move), the orchestrator must:

    1. Re-raise the underlying exception.
    2. Delete the staging key (rollback).
    3. NOT leave a final path behind (we never wrote the final path
       successfully because the write itself failed).
    4. Emit ``dept_create_failed`` with ``actor_role`` carried.

    The DB transaction itself is rolled back by ``with_dept_session``
    on exception, so even though the INSERT statements completed
    inside the fake connection, the property of "no half-committed
    state" is preserved end-to-end.
    """

    vault = _FakeVaultBackend(
        write_failure_paths=frozenset({"vault:atlassian/acme/jira"}),
    )
    # All execute calls succeed; the failure happens during the
    # Vault promotion (staging → final) call which lives inside the
    # try-block of ``_commit``.
    connection = _FailingConnection(
        failure_index=10**6,  # never trigger
        failure_exc=RuntimeError("unreachable"),
    )
    audit = _RecordingAuditLogger()
    orchestrator = _build_orchestrator(
        vault=vault, connection=connection, audit=audit
    )

    request = _build_request(plain_token)

    async def _go() -> None:
        await orchestrator.run(
            request, actor_id="ops-1", actor_role="admin"
        )

    raised: BaseException | None = None
    try:
        _run_async(_go())
    except RuntimeError as exc:
        raised = exc

    assert raised is not None, (
        "Property 6h violated: Vault promotion failure was not surfaced "
        "by the orchestrator (Requirement 3.6)."
    )

    # The staging path may or may not still be present depending on
    # rollback order — what we MUST guarantee is that no *final* path
    # leaks. The promotion itself failed, so writes to the final path
    # must have raised; if the write failure path also leaves the
    # staging untouched we still get the cleanup via the surrounding
    # ``except`` in ``run``.
    finals = _final_paths(vault)
    assert finals == [], (
        f"Property 6h violated: a final Vault path was committed "
        f"despite the promotion failure ({finals!r}); Requirement 3.6 "
        f"forbids any half-committed Vault state."
    )

    # Staging keys must be cleaned up by the outer rollback.
    assert _staging_paths(vault) == [], (
        f"Property 6h violated: staging keys remain after promotion "
        f"failure ({_staging_paths(vault)!r}); Requirement 3.6 requires "
        f"staging cleanup on every failure path."
    )

    # Audit invariants.
    failed_events = [e for e in audit.events if e.action == "dept_create_failed"]
    assert len(failed_events) == 1, (
        f"Property 6h violated: expected exactly one dept_create_failed "
        f"audit row; got {[e.action for e in audit.events]!r}."
    )
    assert failed_events[0].actor_role == "admin"
    assert failed_events[0].result == "error"
    assert failed_events[0].dept_id == request.dept_id

    # Plain-text leak parity (Requirement 3.4).
    for event in audit.events:
        assert plain_token not in repr(event.payload or {})
        assert plain_token not in event.resource
        assert plain_token not in event.action
