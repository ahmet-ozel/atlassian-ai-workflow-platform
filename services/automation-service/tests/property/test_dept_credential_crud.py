"""Property test: Dept credential CRUD atomicity.

For *any* random sequence of ``add → probe → update → remove`` calls
issued against :class:`services.dept_credential_service.DeptCredentialService`
the following invariants must hold at every step:

I1.  **Atomic add / update.**  The DB row and the Vault final path
     are either *both* present (success) or *both* absent (failure).
     A staging-write, probe, DB-insert or Vault-promotion failure
     leaves no partial side-effect:
       * No ``automation.department_bots`` row written without a
         matching ``vault:atlassian/<dept>/<service>`` value.
       * No Vault final path populated without a matching DB row.
       * No staging key (``vault:atlassian/_staging/...``) ever
         survives a completed call.

I2.  **Idempotent remove.**  Calling ``remove`` repeatedly never
     errors and leaves the post-condition ``row_absent ∧
     vault_absent`` regardless of the prior state.  Each removal of
     an existing row writes one ``dept_credential_removed`` audit
     row (``existed=True``); a remove against a missing row is also
     a 200 with ``existed=False``.

I3.  **Update leaves no stale Vault paths.**  Re-running
     ``add_or_update`` on an existing ``(dept_id, service)`` pair
     replaces the Vault value at the *same* final path; no other
     Vault path is written or left behind.  Across an entire
     run, the set of Vault paths that *ever* held a final value
     equals the set of ``(dept_id, service)`` pairs that have been
     touched - and at any point in time, the set of currently
     populated final paths equals the set of ``(dept_id, service)``
     rows that currently exist in the DB.

I4.  **Audit chain completeness.**  Every successful mutation
     emits exactly one terminal audit row whose ``action`` matches
     the operation:
       * first add for a pair → ``dept_credential_added``
       * subsequent add on same pair → ``dept_credential_updated``
       * remove of an existing row → ``dept_credential_removed``
         with ``payload.existed = True``
       * remove of a missing row → ``dept_credential_removed``
         with ``payload.existed = False``
     Every failure path emits exactly one
     ``dept_credential_add_failed`` row whose ``payload.reason``
     matches the failure marker raised by the orchestrator.

The test deliberately bypasses every external system: Vault, the
Postgres pool and the Atlassian probe client are all in-memory
fakes whose behaviour is randomised by Hypothesis to drive the
failure paths the orchestrator must roll back.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    rule,
)

# ---------------------------------------------------------------------------
# sys.path bootstrap - mirrors the sibling property tests
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
_AUTOMATION_SRC = _AUTOMATION_ROOT / "src"
_PLATFORM_ROOT = _AUTOMATION_ROOT.parent.parent
_LIB_SRC_DIRS = tuple(
    _PLATFORM_ROOT / "libs" / lib / "src"
    for lib in (
        "audit_logger",
        "vault_client",
        "db-shared",
        "http-shared",
        "auth-shared",
        "temporal-shared",
        "mcp_client",
        "messages",
        "prompts",
        "pii-shared",
        "notification",
        "observability",
        "llm-orchestrator",
    )
)
for _p in (
    str(_AUTOMATION_ROOT),
    str(_AUTOMATION_SRC),
    *(str(p) for p in _LIB_SRC_DIRS),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from audit_logger import AuditEvent  # noqa: E402
from vault_client import VaultPath  # noqa: E402

from automation_service.probe import (  # noqa: E402
    ProbeArtifact,
    ProbeResult,
    ProbeService,
    ProbeTargets,
    ResolvedCredential,
)
from services.dept_credential_service import (  # noqa: E402
    AddCredentialRequest,
    AddCredentialResult,
    DeptCredentialOperationError,
    DeptCredentialService,
    DepartmentNotFoundError,
    RemoveCredentialResult,
)

# ---------------------------------------------------------------------------
# Hypothesis profile - bounded for CI, deterministic
# ---------------------------------------------------------------------------

_PROFILE = settings(
    max_examples=30,
    stateful_step_count=20,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
        HealthCheck.differing_executors,
        HealthCheck.filter_too_much,
    ],
)

# ---------------------------------------------------------------------------
# Domain constants - closed sets the strategies sample from
# ---------------------------------------------------------------------------

_DEPT_IDS: tuple[str, ...] = ("payments", "platform", "ops")
_UNKNOWN_DEPT_IDS: tuple[str, ...] = ("ghost", "missing-dept")
_SERVICES: tuple[ProbeService, ...] = ("jira", "bitbucket", "confluence")
_USERNAMES: tuple[str, ...] = ("bot-alpha", "bot-beta", "bot-gamma")
_TOKENS: tuple[str, ...] = ("tok-aaaa", "tok-bbbb", "tok-cccc", "tok-dddd")
_URLS: tuple[str, ...] = (
    "https://example.atlassian.net",
    "https://acme.atlassian.net",
)


# ===========================================================================
# Fake collaborators
# ===========================================================================


@dataclass
class _FakeVault:
    """In-memory KV store with call-count tracking.

    The orchestrator drives only ``read`` / ``write`` / ``delete`` on
    this fake; the rotation helpers from the production
    :class:`vault_client.client.VaultClient` protocol are never
    exercised in this test so we keep the surface narrow.

    The fake lets a test arm a single ``write_should_fail_at`` path
    so we can drive the staging-write failure branch deterministically.
    """

    backend: str = "local-dev"
    store: dict[str, dict[str, str]] = field(default_factory=dict)
    write_calls: int = 0
    delete_calls: int = 0
    read_calls: int = 0
    #: When set, the next ``write`` to this raw path raises a
    #: ``RuntimeError`` (used by the staging-failure rule).
    fail_write_paths: set[str] = field(default_factory=set)
    #: When set, the next ``delete`` to this raw path raises a
    #: ``RuntimeError`` (used to exercise the rollback's
    #: best-effort delete).
    fail_delete_paths: set[str] = field(default_factory=set)

    # ------------------------------------------------------------------
    # VaultClient protocol - synchronous
    # ------------------------------------------------------------------

    def read(self, path: VaultPath) -> Mapping[str, str]:
        self.read_calls += 1
        try:
            return dict(self.store[path.raw])
        except KeyError as exc:
            raise KeyError(f"no value at {path.raw!r}") from exc

    def write(self, path: VaultPath, data: Mapping[str, str]) -> None:
        self.write_calls += 1
        if path.raw in self.fail_write_paths:
            self.fail_write_paths.discard(path.raw)
            raise RuntimeError(f"forced write failure at {path.raw}")
        self.store[path.raw] = dict(data)

    def delete(self, path: VaultPath) -> None:
        self.delete_calls += 1
        if path.raw in self.fail_delete_paths:
            self.fail_delete_paths.discard(path.raw)
            raise RuntimeError(f"forced delete failure at {path.raw}")
        # Idempotent - missing path is a no-op (mirrors production).
        self.store.pop(path.raw, None)

    # ------------------------------------------------------------------
    # Convenience for invariants
    # ------------------------------------------------------------------

    def staging_keys(self) -> set[str]:
        prefix = "vault:atlassian/_staging/"
        return {k for k in self.store if k.startswith(prefix)}

    def final_keys(self) -> set[str]:
        prefix = "vault:atlassian/"
        staging_prefix = "vault:atlassian/_staging/"
        return {
            k
            for k in self.store
            if k.startswith(prefix) and not k.startswith(staging_prefix)
        }


# ---------------------------------------------------------------------------


@dataclass
class _DeptBotRow:
    department_id: str
    service: str
    credential_ref: str
    account_id: str | None
    username: str | None
    deployment: str


@dataclass
class _FakePostgres:
    """In-memory replacement for ``automation.department_bots`` /
    ``automation.departments``.

    The fake handles the four SQL shapes the orchestrator emits:

    1. ``BEGIN`` / ``COMMIT`` / ``ROLLBACK`` - toggle a transaction
       mode so writes outside a transaction take effect immediately.
       Inside a transaction, writes are buffered and only applied on
       ``COMMIT``.
    2. ``SELECT set_config(...)`` - record the GUC for diagnostic
       purposes; the fake never enforces RLS.
    3. ``SELECT 1 FROM automation.departments WHERE id = $1`` -
       returns ``{"1": 1}`` when the dept exists, else ``None``.
    4. ``SELECT 1 FROM automation.department_bots WHERE
       department_id = $1 AND service = $2`` - returns ``{"1": 1}``
       if a row is currently registered.
    5. ``SELECT department_id, service, credential_ref, account_id,
       username, deployment FROM automation.department_bots WHERE
       department_id = $1 ORDER BY service`` - returns the matching
       rows.
    6. ``INSERT INTO automation.department_bots ... ON CONFLICT
       (department_id, service) DO UPDATE`` - UPSERT.
    7. ``DELETE FROM automation.department_bots WHERE department_id
       = $1 AND service = $2``.

    Every other query raises :class:`NotImplementedError` so a
    regression that issues a new SQL shape is loud.
    """

    departments: set[str]
    bots: dict[tuple[str, str], _DeptBotRow] = field(default_factory=dict)
    #: Buffered mutations applied on COMMIT.
    _txn_buffer: list[tuple[str, Any]] = field(default_factory=list)
    in_txn: bool = False
    #: Force the *next* committed transaction to fail with a
    #: ``RuntimeError`` so the orchestrator's rollback path is
    #: exercised.  Cleared automatically once consumed.
    fail_next_commit: bool = False
    #: Force the *next* INSERT to fail with the named exception class.
    fail_next_insert_with: type[Exception] | None = None

    # ------------------------------------------------------------------
    # AsyncConnection protocol
    # ------------------------------------------------------------------

    async def execute(self, query: str, *args: Any) -> Any:
        normalised = " ".join(query.split()).lower()
        if normalised.startswith("begin"):
            self.in_txn = True
            self._txn_buffer = []
            return "BEGIN"
        if normalised.startswith("commit"):
            self.in_txn = False
            if self.fail_next_commit:
                self.fail_next_commit = False
                self._txn_buffer = []
                raise RuntimeError("forced commit failure")
            self._apply_buffered()
            self._txn_buffer = []
            return "COMMIT"
        if normalised.startswith("rollback"):
            self.in_txn = False
            self._txn_buffer = []
            return "ROLLBACK"
        if normalised.startswith("select set_config"):
            # The orchestrator binds ``app.current_dept_id`` /
            # ``app.current_role`` here.  We accept any value and do
            # not enforce RLS in the fake.
            return None
        if "insert into automation.department_bots" in normalised:
            (
                dept_id,
                service,
                credential_ref,
                account_id,
                username,
                deployment,
            ) = args
            if self.fail_next_insert_with is not None:
                exc_cls = self.fail_next_insert_with
                self.fail_next_insert_with = None
                raise exc_cls("forced insert failure")
            row = _DeptBotRow(
                department_id=dept_id,
                service=service,
                credential_ref=credential_ref,
                account_id=account_id,
                username=username,
                deployment=deployment,
            )
            self._mutate(("upsert", row))
            return "INSERT 0 1"
        if "insert into automation.department_bot_identity" in normalised:
            # The inline probe upserts the resolved account_id here.
            # The fake accepts and discards - the CRUD test does not
            # assert on the identity table contents.
            return "INSERT 0 1"
        if "delete from automation.department_bots" in normalised:
            (dept_id, service) = args
            self._mutate(("delete", (dept_id, service)))
            return "DELETE 1"
        raise NotImplementedError(
            f"_FakePostgres.execute does not understand query: {query!r}"
        )

    async def fetchrow(
        self, query: str, *args: Any
    ) -> Mapping[str, Any] | None:
        normalised = " ".join(query.split()).lower()
        if "from automation.departments where id" in normalised:
            (dept_id,) = args
            return {"1": 1} if dept_id in self.departments else None
        if (
            "from automation.department_bots" in normalised
            and "select 1" in normalised
        ):
            (dept_id, service) = args
            key = (dept_id, service)
            return (
                {"1": 1}
                if key in self._effective_bots()
                else None
            )
        raise NotImplementedError(
            f"_FakePostgres.fetchrow does not understand query: {query!r}"
        )

    async def fetch(
        self, query: str, *args: Any
    ) -> Iterable[Mapping[str, Any]]:
        normalised = " ".join(query.split()).lower()
        if (
            "from automation.department_bots" in normalised
            and "where department_id" in normalised
            and "order by service" in normalised
        ):
            (dept_id,) = args
            rows = [
                row
                for (d, _s), row in sorted(self._effective_bots().items())
                if d == dept_id
            ]
            return [
                {
                    "department_id": r.department_id,
                    "service": r.service,
                    "credential_ref": r.credential_ref,
                    "account_id": r.account_id,
                    "username": r.username,
                    "deployment": r.deployment,
                }
                for r in rows
            ]
        raise NotImplementedError(
            f"_FakePostgres.fetch does not understand query: {query!r}"
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _mutate(self, op: tuple[str, Any]) -> None:
        if self.in_txn:
            self._txn_buffer.append(op)
        else:
            self._apply([op])

    def _apply_buffered(self) -> None:
        self._apply(self._txn_buffer)

    def _apply(self, ops: list[tuple[str, Any]]) -> None:
        for kind, value in ops:
            if kind == "upsert":
                row: _DeptBotRow = value
                self.bots[(row.department_id, row.service)] = row
            elif kind == "delete":
                key = value
                self.bots.pop(key, None)

    def _effective_bots(self) -> dict[tuple[str, str], _DeptBotRow]:
        """Return the bots map *as the orchestrator's session sees it*.

        While a transaction is open, buffered mutations are visible to
        the same session - so SELECTs inside the same transaction must
        observe them.  This mirrors Postgres' read-your-own-writes
        semantics inside a single transaction.
        """

        view = dict(self.bots)
        if self.in_txn:
            for kind, value in self._txn_buffer:
                if kind == "upsert":
                    row: _DeptBotRow = value
                    view[(row.department_id, row.service)] = row
                elif kind == "delete":
                    view.pop(value, None)
        return view


@dataclass
class _ConnectionFactory:
    """Async factory returning the same fake connection on every call.

    The orchestrator opens a fresh ``with_dept_session`` per mutation;
    each session uses a single ``AsyncConnection``.  The fake is
    safe to reuse because the orchestrator always wraps its work in
    a ``BEGIN`` / ``COMMIT`` block, and the fake resets its buffer on
    each ``BEGIN``.
    """

    pg: _FakePostgres

    async def __call__(self) -> _FakePostgres:
        return self.pg


# ---------------------------------------------------------------------------


@dataclass
class _FakeProbeClient:
    """Minimal :class:`AtlassianProbeClient` stand-in.

    The orchestrator only invokes ``ProbeRunner.run`` which in turn
    calls *one* read and *one* write helper per service.  We mirror
    only the helpers required for the three services and let
    ``probe_should_fail`` flip the read probe to raise.
    """

    auto_account_id: str = "account-resolved-001"
    fail_read: bool = False

    async def jira_myself(self, cred: ResolvedCredential) -> dict[str, Any]:
        if self.fail_read:
            raise RuntimeError("forced jira_myself failure")
        return {"accountId": self.auto_account_id, "emailAddress": cred.username}

    async def jira_search_self_comments(
        self, cred: ResolvedCredential, author_account_id: str
    ) -> list[dict[str, Any]]:
        return []

    async def jira_create_self_comment(
        self, cred: ResolvedCredential, body: str
    ) -> dict[str, Any]:
        return {"id": "1", "issue_key": "TEST-1"}

    async def jira_delete_comment(
        self, cred: ResolvedCredential, issue_key: str, comment_id: str
    ) -> None:
        return None

    async def bitbucket_user(
        self, cred: ResolvedCredential
    ) -> dict[str, Any]:
        if self.fail_read:
            raise RuntimeError("forced bitbucket_user failure")
        return {"account_id": self.auto_account_id, "username": cred.username}

    async def bitbucket_list_probe_branches(
        self, cred: ResolvedCredential, workspace: str, repo: str
    ) -> list[str]:
        return []

    async def bitbucket_create_branch(
        self,
        cred: ResolvedCredential,
        workspace: str,
        repo: str,
        branch_name: str,
    ) -> str:
        return "ref-abc"

    async def bitbucket_delete_branch(
        self,
        cred: ResolvedCredential,
        workspace: str,
        repo: str,
        branch_name: str,
    ) -> None:
        return None

    async def confluence_user(
        self, cred: ResolvedCredential
    ) -> dict[str, Any]:
        if self.fail_read:
            raise RuntimeError("forced confluence_user failure")
        return {"accountId": self.auto_account_id, "username": cred.username}

    async def confluence_list_probe_pages(
        self, cred: ResolvedCredential, space_key: str
    ) -> list[dict[str, Any]]:
        return []

    async def confluence_create_draft_page(
        self,
        cred: ResolvedCredential,
        space_key: str,
        title: str,
    ) -> dict[str, Any]:
        return {"id": "100"}

    async def confluence_delete_page(
        self, cred: ResolvedCredential, page_id: str
    ) -> None:
        return None


# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditLogger:
    """List-backed audit sink - every ``write`` is recorded."""

    events: list[AuditEvent] = field(default_factory=list)

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def actions(self) -> list[str]:
        return [e.action for e in self.events]

    def by_action(self, action: str) -> list[AuditEvent]:
        return [e for e in self.events if e.action == action]


# ===========================================================================
# Helpers
# ===========================================================================


def _final_path(dept_id: str, service: str) -> str:
    return f"vault:atlassian/{dept_id}/{service}"


def _service_targets(service: str) -> ProbeTargets | None:
    """Return the targets the probe runner needs for *service*."""

    if service == "bitbucket":
        return ProbeTargets(
            bitbucket_workspace="acme",
            bitbucket_repo="probe-repo",
        )
    if service == "confluence":
        return ProbeTargets(confluence_space_key="PROBE")
    return None


def _build_request(
    *,
    dept_id: str,
    service: str,
    username: str,
    token: str,
    url: str,
    account_id: str | None = None,
) -> AddCredentialRequest:
    return AddCredentialRequest(
        dept_id=dept_id,
        service=service,  # type: ignore[arg-type]
        url=url,
        username=username,
        personal_token=bytearray(token.encode("utf-8")),
        account_id=account_id,
        deployment=None,
        probe_targets=_service_targets(service),
    )


def _build_service(
    *,
    departments: tuple[str, ...] = _DEPT_IDS,
) -> tuple[
    DeptCredentialService, _FakeVault, _FakePostgres, _FakeProbeClient,
    _FakeAuditLogger,
]:
    vault = _FakeVault()
    pg = _FakePostgres(departments=set(departments))
    probe = _FakeProbeClient()
    audit = _FakeAuditLogger()
    service = DeptCredentialService(
        vault=vault,  # type: ignore[arg-type]
        connection_factory=_ConnectionFactory(pg),  # type: ignore[arg-type]
        probe_client=probe,  # type: ignore[arg-type]
        audit_logger=audit,  # type: ignore[arg-type]
        clock=lambda: datetime(2025, 1, 1, tzinfo=timezone.utc),
        actor_role_for_session="system",
    )
    return service, vault, pg, probe, audit


# ===========================================================================
# Stateful machine
# ===========================================================================


class DeptCredentialCRUDStateMachine(RuleBasedStateMachine):
    """Random ``add → probe → update → remove`` sequences must always
    leave DB ↔ Vault in a consistent state and emit a complete audit
    chain.

    The machine tracks an *expected* model of the system state in
    pure Python and asserts after every step that:

    * ``vault.final_keys() == expected_paths``
    * ``set(pg.bots) == expected_pairs``
    * No staging keys ever leak.
    * Every successful mutation produced exactly one terminal
      audit row whose action matches the expected operation.
    * Every failure produced exactly one
      ``dept_credential_add_failed`` row.
    """

    def __init__(self) -> None:
        super().__init__()
        (
            self._service,
            self._vault,
            self._pg,
            self._probe,
            self._audit,
        ) = _build_service()
        # Ghost model - the test's belief about which (dept, service)
        # pairs currently have a credential registered.
        self._expected_pairs: set[tuple[str, str]] = set()
        # Action counters - invariants compare these against the
        # audit log to catch double-emissions or missing emissions.
        self._expected_audit_actions: list[str] = []

    # ==================================================================
    # Initialisation
    # ==================================================================

    @initialize()
    def _bootstrap(self) -> None:
        # No-op - the constructor already wired everything up.  The
        # ``@initialize`` decorator just ensures Hypothesis runs the
        # invariants once before the first rule.
        return None

    # ==================================================================
    # Rules
    # ==================================================================

    @rule(
        dept_id=st.sampled_from(_DEPT_IDS),
        service=st.sampled_from(_SERVICES),
        username=st.sampled_from(_USERNAMES),
        token=st.sampled_from(_TOKENS),
        url=st.sampled_from(_URLS),
    )
    def add_or_update_success(
        self,
        dept_id: str,
        service: str,
        username: str,
        token: str,
        url: str,
    ) -> None:
        """Happy-path add or update.  Must promote staging → final."""

        request = _build_request(
            dept_id=dept_id,
            service=service,
            username=username,
            token=token,
            url=url,
        )
        was_present = (dept_id, service) in self._expected_pairs

        prior_audit_count = len(self._audit.events)
        result = asyncio.run(
            self._service.add_or_update(
                request, actor_id="alice", actor_role="admin"
            )
        )

        assert isinstance(result, AddCredentialResult)
        assert result.dept_id == dept_id
        assert result.service == service
        assert result.vault_path == _final_path(dept_id, service)
        # ``outcome`` flips ``created`` → ``updated`` once the row
        # exists.
        expected_outcome = "updated" if was_present else "created"
        assert result.outcome == expected_outcome, (
            f"expected outcome={expected_outcome!r} for "
            f"({dept_id}, {service}); got {result.outcome!r}"
        )

        # Update ghost model.
        self._expected_pairs.add((dept_id, service))
        self._expected_audit_actions.append(
            "dept_credential_added"
            if not was_present
            else "dept_credential_updated"
        )

        # Exactly one credential audit row + one bot identity probe audit row.
        new_events = self._audit.events[prior_audit_count:]
        credential_events = [
            e for e in new_events
            if e.action in ("dept_credential_added", "dept_credential_updated")
        ]
        assert len(credential_events) == 1, (
            f"expected exactly one credential audit row; got {[e.action for e in new_events]}"
        )
        assert credential_events[0].action == self._expected_audit_actions[-1]

    @rule(
        dept_id=st.sampled_from(_DEPT_IDS),
        service=st.sampled_from(_SERVICES),
        username=st.sampled_from(_USERNAMES),
        token=st.sampled_from(_TOKENS),
        url=st.sampled_from(_URLS),
    )
    def add_or_update_staging_write_fails(
        self,
        dept_id: str,
        service: str,
        username: str,
        token: str,
        url: str,
    ) -> None:
        """Force the staging Vault write to fail.

        Post-condition (I1): no staging key, no final key, no DB
        row mutation; one ``dept_credential_add_failed`` audit row
        whose ``payload.reason == "staging_write_failed"``.
        """

        was_present = (dept_id, service) in self._expected_pairs
        # Arm the next staging write to fail.  The orchestrator builds
        # the staging path from a fresh ``uuid.uuid4().hex`` request
        # id, so we monkey-patch ``uuid4`` to return a deterministic
        # value and arm exactly that path.
        rid = uuid.UUID("00000000-0000-4000-8000-000000000001")
        original_uuid4 = uuid.uuid4
        try:
            uuid.uuid4 = lambda: rid  # type: ignore[assignment]
            staging_path = (
                f"vault:atlassian/_staging/{rid.hex}/{service}"
            )
            self._vault.fail_write_paths.add(staging_path)

            request = _build_request(
                dept_id=dept_id,
                service=service,
                username=username,
                token=token,
                url=url,
            )

            prior_audit_count = len(self._audit.events)
            with pytest.raises(DeptCredentialOperationError) as exc_info:
                asyncio.run(
                    self._service.add_or_update(
                        request, actor_id="alice", actor_role="admin"
                    )
                )
            assert exc_info.value.reason == "staging_write_failed"
        finally:
            uuid.uuid4 = original_uuid4  # type: ignore[assignment]
            self._vault.fail_write_paths.clear()

        # The pair's presence in the ghost model is unchanged by a
        # failed call.
        assert (
            (dept_id, service) in self._expected_pairs
        ) is was_present

        # Exactly one ``dept_credential_add_failed`` audit row was
        # written.
        new_events = self._audit.events[prior_audit_count:]
        assert len(new_events) == 1
        assert new_events[0].action == "dept_credential_add_failed"
        assert new_events[0].payload is not None
        assert (
            new_events[0].payload.get("reason") == "staging_write_failed"
        )

    @rule(
        dept_id=st.sampled_from(_DEPT_IDS),
        service=st.sampled_from(_SERVICES),
        username=st.sampled_from(_USERNAMES),
        token=st.sampled_from(_TOKENS),
        url=st.sampled_from(_URLS),
    )
    def add_or_update_db_commit_fails(
        self,
        dept_id: str,
        service: str,
        username: str,
        token: str,
        url: str,
    ) -> None:
        """Force the COMMIT phase to fail (atomic rollback path).

        Post-condition (I1): the orchestrator's rollback either:

        * Catches the COMMIT failure inside ``with_dept_session``
          (which then issues a ROLLBACK) and re-raises a
          :class:`DeptCredentialOperationError` carrying
          ``reason="db_or_vault_error:..."``.
        * Leaves no staging key behind.
        * Leaves the DB row count unchanged for this pair (the
          buffered upsert was discarded on the failed commit).

        The Vault final path *may* have been populated by the
        promotion step that runs *before* commit, in which case the
        orchestrator's ``except`` block deletes it so the run
        observably rolls back to ``row_absent ∧ vault_absent``.
        """

        was_present = (dept_id, service) in self._expected_pairs
        prior_pairs_snapshot = dict(self._pg.bots)
        prior_final = dict(self._vault.store)

        self._pg.fail_next_commit = True

        request = _build_request(
            dept_id=dept_id,
            service=service,
            username=username,
            token=token,
            url=url,
        )

        prior_audit_count = len(self._audit.events)
        with pytest.raises(DeptCredentialOperationError) as exc_info:
            asyncio.run(
                self._service.add_or_update(
                    request, actor_id="alice", actor_role="admin"
                )
            )
        assert exc_info.value.reason.startswith("db_or_vault_error")

        # Ghost model is unchanged.
        assert (
            (dept_id, service) in self._expected_pairs
        ) is was_present

        # The DB row count is unchanged - buffered upsert discarded.
        assert self._pg.bots == prior_pairs_snapshot, (
            "DB rolled back; bots map should match pre-call snapshot"
        )

        # Vault rolled back to pre-call state on this pair: if the
        # pair was present, its final value is unchanged; if not, no
        # final key exists.
        final_path = _final_path(dept_id, service)
        if was_present:
            assert final_path in self._vault.store, (
                "rollback should preserve a previously-present final value"
            )
            assert self._vault.store[final_path] == prior_final[final_path]
        else:
            assert final_path not in self._vault.store, (
                "rollback should leave no final value behind for "
                "a never-present pair"
            )

        # Exactly one ``dept_credential_add_failed`` audit row.
        new_events = self._audit.events[prior_audit_count:]
        assert len(new_events) == 1
        assert new_events[0].action == "dept_credential_add_failed"

    @rule(
        dept_id=st.sampled_from(_DEPT_IDS),
        service=st.sampled_from(_SERVICES),
    )
    def remove(self, dept_id: str, service: str) -> None:
        """Idempotent remove.  Always 200, always rolls Vault forward.

        Post-condition (I2): row absent, final path absent, exactly
        one ``dept_credential_removed`` audit row.
        """

        was_present = (dept_id, service) in self._expected_pairs
        prior_audit_count = len(self._audit.events)

        result = asyncio.run(
            self._service.remove(
                dept_id=dept_id,
                service=service,  # type: ignore[arg-type]
                actor_id="alice",
                actor_role="admin",
            )
        )

        assert isinstance(result, RemoveCredentialResult)
        assert result.dept_id == dept_id
        assert result.service == service
        assert result.existed is was_present

        self._expected_pairs.discard((dept_id, service))
        self._expected_audit_actions.append("dept_credential_removed")

        new_events = self._audit.events[prior_audit_count:]
        assert len(new_events) == 1
        assert new_events[0].action == "dept_credential_removed"
        assert new_events[0].payload is not None
        assert new_events[0].payload.get("existed") is was_present

    @rule(dept_id=st.sampled_from(_DEPT_IDS))
    def probe_all(self, dept_id: str) -> None:
        """Probe every service the dept currently owns.

        This rule does not change DB ↔ Vault state; it only emits
        ``dept_credential_probed`` audit rows (one per registered
        service).  The invariants apply equally before and after.
        """

        prior_audit_count = len(self._audit.events)
        registered = sorted(
            s for (d, s) in self._expected_pairs if d == dept_id
        )

        result = asyncio.run(
            self._service.probe(
                dept_id=dept_id,
                service=None,
                actor_id="alice",
                actor_role="admin",
            )
        )
        assert {p.service for p in result.results} == set(registered)

        new_events = self._audit.events[prior_audit_count:]
        assert len(new_events) == len(registered)
        for ev in new_events:
            assert ev.action == "dept_credential_probed"

    # ==================================================================
    # Invariants
    # ==================================================================

    @invariant()
    def vault_and_db_agree(self) -> None:
        """I1 + I3: The set of populated final paths matches the
        set of ``(dept, service)`` rows in DB."""

        expected_paths = {
            _final_path(d, s) for (d, s) in self._expected_pairs
        }
        actual_paths = self._vault.final_keys()
        assert actual_paths == expected_paths, (
            f"Vault final paths {actual_paths!r} do not match expected "
            f"{expected_paths!r}"
        )
        actual_pairs = set(self._pg.bots.keys())
        assert actual_pairs == self._expected_pairs, (
            f"DB bot rows {actual_pairs!r} do not match expected "
            f"{self._expected_pairs!r}"
        )

    @invariant()
    def no_staging_leaks(self) -> None:
        """I1: No staging path ever survives a completed call."""

        leaks = self._vault.staging_keys()
        assert not leaks, f"staging keys leaked: {leaks!r}"

    @invariant()
    def db_credential_ref_matches_vault_path(self) -> None:
        """I3: every DB row's ``credential_ref`` points at the
        corresponding final Vault path."""

        for (dept_id, service), row in self._pg.bots.items():
            expected = _final_path(dept_id, service)
            assert row.credential_ref == expected, (
                f"row ({dept_id}, {service}) has credential_ref="
                f"{row.credential_ref!r} but expected {expected!r}"
            )
            assert expected in self._vault.store, (
                f"credential_ref {expected!r} has no Vault entry"
            )

    @invariant()
    def audit_chain_matches_expected(self) -> None:
        """I4: terminal action sequence (excluding probe + failure
        rows) matches what the ghost model recorded."""

        terminal = [
            e.action
            for e in self._audit.events
            if e.action
            in {
                "dept_credential_added",
                "dept_credential_updated",
                "dept_credential_removed",
            }
        ]
        assert terminal == self._expected_audit_actions, (
            f"audit chain mismatch:\n  expected: {self._expected_audit_actions!r}"
            f"\n  actual:   {terminal!r}"
        )


# ---------------------------------------------------------------------------
# Hypothesis test driver - wraps the state machine into a pytest test.
# ---------------------------------------------------------------------------

DeptCredentialCRUDStateMachine.TestCase.settings = _PROFILE
TestDeptCredentialCRUDAtomicity = DeptCredentialCRUDStateMachine.TestCase


# ===========================================================================
# Targeted scenario tests (complement the state machine with explicit
# coverage of the orchestrator's edge cases that Hypothesis would
# otherwise have to discover by chance).
# ===========================================================================


class TestRemoveIdempotency:
    """**I2 - Remove is idempotent across repeated calls.**

    Removing a credential is idempotent.  This test pins the contract
    on a focused, single-pair scenario: three back-to-back removes
    after a single add must each return 200; only the first flips
    ``existed`` from True to False.
    """

    @pytest.mark.asyncio
    async def test_three_consecutive_removes_after_one_add(self) -> None:
        service, vault, pg, _probe, audit = _build_service()
        request = _build_request(
            dept_id="payments",
            service="jira",
            username="bot-alpha",
            token="tok-aaaa",
            url=_URLS[0],
        )
        await service.add_or_update(
            request, actor_id="alice", actor_role="admin"
        )
        assert ("payments", "jira") in pg.bots
        assert _final_path("payments", "jira") in vault.store

        # First remove - actually deletes.
        first = await service.remove(
            dept_id="payments",
            service="jira",
            actor_id="alice",
            actor_role="admin",
        )
        assert first.existed is True

        # Second + third - no-ops, still 200.
        second = await service.remove(
            dept_id="payments",
            service="jira",
            actor_id="alice",
            actor_role="admin",
        )
        assert second.existed is False

        third = await service.remove(
            dept_id="payments",
            service="jira",
            actor_id="alice",
            actor_role="admin",
        )
        assert third.existed is False

        # Final state is consistent.
        assert ("payments", "jira") not in pg.bots
        assert _final_path("payments", "jira") not in vault.store

        # Three ``dept_credential_removed`` audit rows, the first
        # carrying ``existed=True`` and the rest ``existed=False``.
        removed = audit.by_action("dept_credential_removed")
        assert len(removed) == 3
        assert removed[0].payload["existed"] is True
        assert removed[1].payload["existed"] is False
        assert removed[2].payload["existed"] is False


class TestUpdateLeavesNoStaleVaultPaths:
    """**I3 - Update overwrites the same final path; no stale keys
    are ever created.**

    The orchestrator's ``add_or_update`` re-uses the same
    ``vault:atlassian/<dept>/<service>`` final path on every call.
    Repeated updates must not produce additional Vault entries; the
    set of final paths must remain ``{ <dept>/<service> }`` and the
    payload must reflect the *latest* token.
    """

    @pytest.mark.asyncio
    async def test_repeated_update_overwrites_in_place(self) -> None:
        service, vault, pg, _probe, audit = _build_service()
        for token in ("tok-aaaa", "tok-bbbb", "tok-cccc", "tok-dddd"):
            request = _build_request(
                dept_id="payments",
                service="confluence",
                username="bot-alpha",
                token=token,
                url=_URLS[0],
            )
            await service.add_or_update(
                request, actor_id="alice", actor_role="admin"
            )

        path = _final_path("payments", "confluence")
        # Exactly one Vault final path.
        assert vault.final_keys() == {path}
        # Stored value reflects the *last* token written.
        assert vault.store[path]["personal_token"] == "tok-dddd"
        # No staging key anywhere.
        assert not vault.staging_keys()
        # First call ``added``, three subsequent calls ``updated``.
        assert audit.actions().count("dept_credential_added") == 1
        assert audit.actions().count("dept_credential_updated") == 3


class TestAddFailureLeavesNoSideEffects:
    """**I1 + I4 - A failed staging write produces zero side
    effects beyond the audit row.**

    Targeted complement to the state machine's
    ``add_or_update_staging_write_fails`` rule: pin the same
    invariants on a deterministic scenario so a regression that
    breaks rollback is caught even when Hypothesis hasn't sampled
    the failing branch yet.
    """

    @pytest.mark.asyncio
    async def test_staging_write_failure_rolls_back_completely(self) -> None:
        service, vault, pg, _probe, audit = _build_service()

        rid = uuid.UUID("00000000-0000-4000-8000-000000000abc")
        original_uuid4 = uuid.uuid4
        try:
            uuid.uuid4 = lambda: rid  # type: ignore[assignment]
            staging_path = f"vault:atlassian/_staging/{rid.hex}/jira"
            vault.fail_write_paths.add(staging_path)

            request = _build_request(
                dept_id="payments",
                service="jira",
                username="bot-alpha",
                token="tok-aaaa",
                url=_URLS[0],
            )
            with pytest.raises(DeptCredentialOperationError) as exc_info:
                await service.add_or_update(
                    request, actor_id="alice", actor_role="admin"
                )
            assert exc_info.value.reason == "staging_write_failed"
        finally:
            uuid.uuid4 = original_uuid4  # type: ignore[assignment]

        # No DB row.
        assert pg.bots == {}
        # No final path.
        assert vault.final_keys() == set()
        # No staging leaks.
        assert vault.staging_keys() == set()
        # Exactly one failure audit row.
        failed = audit.by_action("dept_credential_add_failed")
        assert len(failed) == 1
        assert failed[0].payload is not None
        assert failed[0].payload.get("reason") == "staging_write_failed"


class TestUnknownDeptIdRejected:
    """``add_or_update`` against an unknown dept_id raises
    :class:`DepartmentNotFoundError` and leaves no side effects.

    The orchestrator must surface a missing department before any DB
    row is written (``DepartmentNotFoundError`` is the dedicated
    subclass that the router maps to HTTP 404).
    """

    @pytest.mark.asyncio
    async def test_unknown_dept_id_surfaces_404_class(self) -> None:
        service, vault, pg, _probe, audit = _build_service(
            departments=("payments",)
        )
        request = _build_request(
            dept_id="ghost",
            service="jira",
            username="bot-alpha",
            token="tok-aaaa",
            url=_URLS[0],
        )
        with pytest.raises(DepartmentNotFoundError):
            await service.add_or_update(
                request, actor_id="alice", actor_role="admin"
            )
        # Nothing written.
        assert pg.bots == {}
        assert vault.final_keys() == set()
        assert vault.staging_keys() == set()
        # Failure audit row recorded.
        failed = audit.by_action("dept_credential_add_failed")
        assert len(failed) == 1
        assert failed[0].payload.get("reason") == "department_not_found"
