"""Unit tests for the PO Review API endpoints.

Validates: Requirements 10.3, 10.4 (workflows spec, task 14.2 — Orphan
Branches + PO Review Inbox + per-PR action endpoints).

Exercises the FastAPI router end-to-end via :class:`TestClient` plus
hand-rolled fakes for every collaborator (OIDC validator, Bitbucket
branch / PR scanners, bot-id resolver, diff-summary cache, Bitbucket
action adapter, audit logger). Four properties are covered, mirroring
the bullet list in the task description:

* **403 when ``dept_admin`` requests another dept** — a dept_admin
  token whose ``dept_ids`` does not include the requested ``dept_id``
  receives HTTP 403 with an ``rbac_denied`` audit row; the scanners
  and the Bitbucket adapter are never called.
* **200 when ``admin`` requests any dept** — a global admin token
  bypasses the dept-scope check and the orphan / inbox lists are
  returned for any ``dept_id``.
* **Orphan list shape matches ``compute_orphan_branches`` output** —
  the response carries exactly the orphans the pure helper would
  return, sorted oldest-first, with cached LLM diff summaries
  attached via the injected :class:`DiffSummaryProvider`.
* **Action endpoints call the right MCP methods** — each of the three
  POST endpoints (``open-draft``, ``request-changes``,
  ``approve-note``) invokes exactly one method on the injected
  :class:`PoReviewActions` adapter and emits exactly one matching
  audit row.

The tests do not stand up Postgres, Vault, Temporal, or an IdP. The
fakes mirror the same pattern used by the sibling repo-sync test
(``tests/unit/test_repo_sync_api.py``).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Path setup — mirrors the sibling repo-sync test so the unit suite can
# run without an editable install of every libs/* package.
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _AUTOMATION_ROOT.parents[1]

for _path in (
    _AUTOMATION_ROOT / "src",
    _AUTOMATION_ROOT,
    _PLATFORM_ROOT / "libs" / "audit_logger" / "src",
    _PLATFORM_ROOT / "libs" / "auth-shared" / "src",
    _PLATFORM_ROOT / "libs" / "http-shared" / "src",
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src",
):
    _path_str = str(_path)
    if _path.is_dir() and _path_str not in sys.path:
        sys.path.insert(0, _path_str)


from audit_logger import AuditEvent, AuditLogger  # noqa: E402

from automation_service.api.po_review import (  # noqa: E402
    PoReviewEndpointDeps,
)
from automation_service.app import create_app  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingAuditWriter:
    """In-memory audit writer; records every :class:`AuditEvent`."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def insert_audit(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class _FakeOIDCValidator:
    """Stand-in for :class:`auth_shared.OIDCValidator`."""

    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)

    def validate(self, token: str) -> dict[str, Any]:
        from auth_shared import InvalidTokenError

        if token in self.tokens:
            return dict(self.tokens[token])
        raise InvalidTokenError(f"unknown token {token!r}")


@dataclass
class _FakeBranchScanner:
    """Records every call and returns canned branch descriptors."""

    branches: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    calls: list[str] = field(default_factory=list)

    async def __call__(
        self, dept_id: str
    ) -> Sequence[Mapping[str, Any]]:
        self.calls.append(dept_id)
        return list(self.branches)


@dataclass
class _FakePullRequestScanner:
    """Records every call and returns canned PR descriptors."""

    pull_requests: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    calls: list[str] = field(default_factory=list)

    async def __call__(
        self, dept_id: str
    ) -> Sequence[Mapping[str, Any]]:
        self.calls.append(dept_id)
        return list(self.pull_requests)


@dataclass
class _FakeDiffSummaryCache:
    """In-memory cache; records every (hash → summary) hit + miss."""

    summaries: dict[str, str] = field(default_factory=dict)
    get_calls: list[str] = field(default_factory=list)
    compute_calls: list[str] = field(default_factory=list)

    async def get(self, diff_hash: str) -> str | None:
        self.get_calls.append(diff_hash)
        return self.summaries.get(diff_hash)

    async def get_or_compute(self, diff_hash, llm_callback):
        self.compute_calls.append(diff_hash)
        if diff_hash in self.summaries:
            return self.summaries[diff_hash]
        summary = await llm_callback(diff_hash)
        self.summaries[diff_hash] = summary
        return summary


@dataclass
class _FakePoReviewActions:
    """Records every action call so the tests can assert on call shape."""

    open_draft_calls: list[tuple[str, int]] = field(default_factory=list)
    request_changes_calls: list[tuple[str, int, str]] = field(
        default_factory=list
    )
    approve_note_calls: list[tuple[str, int, str]] = field(
        default_factory=list
    )

    async def open_draft(self, dept_id: str, pr_id: int) -> None:
        self.open_draft_calls.append((dept_id, pr_id))

    async def request_changes(
        self, dept_id: str, pr_id: int, *, comment: str
    ) -> None:
        self.request_changes_calls.append((dept_id, pr_id, comment))

    async def approve_note(
        self, dept_id: str, pr_id: int, *, comment: str
    ) -> None:
        self.approve_note_calls.append((dept_id, pr_id, comment))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_FROZEN_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
_DEPT_ID = "payments"
_OTHER_DEPT_ID = "fraud"
_BOT_ACCOUNT_ID = "bot-account"


@pytest.fixture
def audit() -> tuple[AuditLogger, _RecordingAuditWriter]:
    sink = _RecordingAuditWriter()
    return AuditLogger(writer=sink), sink


@pytest.fixture
def oidc_validator() -> _FakeOIDCValidator:
    """Roles: admin (carol), viewer (bob), lead (alice), dept_admin (dave)."""

    return _FakeOIDCValidator(
        tokens={
            "token-admin": {
                "sub": "carol",
                "account_id": "carol",
                "role": "admin",
            },
            "token-viewer": {
                "sub": "bob",
                "account_id": "bob",
                "role": "viewer",
                "dept_ids": [_DEPT_ID],
            },
            "token-lead": {
                "sub": "alice",
                "account_id": "alice",
                "role": "lead",
                "dept_ids": [_DEPT_ID],
            },
            "token-dept-admin": {
                "sub": "dave",
                "account_id": "dave",
                "role": "dept_admin",
                "dept_ids": [_DEPT_ID],
            },
            "token-dept-admin-other": {
                "sub": "eve",
                "account_id": "eve",
                "role": "dept_admin",
                "dept_ids": [_OTHER_DEPT_ID],
            },
        }
    )


@pytest.fixture
def branch_scanner() -> _FakeBranchScanner:
    """Three branches:

    * ``ai/PAY-1`` — bot, has matching PR (NOT orphan).
    * ``ai/PAY-2`` — bot, no PR (orphan, last commit 5 days ago).
    * ``ai/PAY-3`` — bot, no PR (orphan, last commit 20 days ago — older).
    * ``feature/manual`` — non-bot branch, ignored by helper.
    """

    return _FakeBranchScanner(
        branches=(
            {
                "name": "ai/PAY-1",
                "last_commit_at": _FROZEN_NOW - timedelta(days=2),
                "diff_hash": "hash-pay-1",
            },
            {
                "name": "ai/PAY-2",
                "last_commit_at": _FROZEN_NOW - timedelta(days=5),
                "diff_hash": "hash-pay-2",
            },
            {
                "name": "ai/PAY-3",
                "last_commit_at": _FROZEN_NOW - timedelta(days=20),
                "diff_hash": "hash-pay-3",
            },
            {
                "name": "feature/manual",
                "last_commit_at": _FROZEN_NOW - timedelta(days=1),
                "diff_hash": "hash-manual",
            },
        ),
    )


@pytest.fixture
def pr_scanner() -> _FakePullRequestScanner:
    """Three PRs:

    * id=1 — draft bot PR for ai/PAY-1 (claims branch, in inbox).
    * id=2 — open bot PR for ai/PAY-99 (NOT in inbox: not a draft).
    * id=3 — draft human PR for feature/manual (NOT in inbox: not bot).
    """

    return _FakePullRequestScanner(
        pull_requests=(
            {
                "id": 1,
                "source_branch": "ai/PAY-1",
                "is_draft": True,
                "author_account_id": _BOT_ACCOUNT_ID,
                "title": "PAY-1: refactor",
            },
            {
                "id": 2,
                "source_branch": "ai/PAY-99",
                "is_draft": False,
                "author_account_id": _BOT_ACCOUNT_ID,
                "title": "PAY-99: legacy",
            },
            {
                "id": 3,
                "source_branch": "feature/manual",
                "is_draft": True,
                "author_account_id": "human-1",
                "title": "manual change",
            },
        ),
    )


@pytest.fixture
def diff_summary_cache() -> _FakeDiffSummaryCache:
    """Pre-seed a hit for ai/PAY-2 so the orphan list exercises the
    cache-hit branch; ai/PAY-3 hits the cache-miss / compute branch.
    """

    return _FakeDiffSummaryCache(
        summaries={"hash-pay-2": "Cached: rename helper."}
    )


@pytest.fixture
def po_review_actions() -> _FakePoReviewActions:
    return _FakePoReviewActions()


@pytest.fixture
def app_with_po_review(
    audit: tuple[AuditLogger, _RecordingAuditWriter],
    oidc_validator: _FakeOIDCValidator,
    branch_scanner: _FakeBranchScanner,
    pr_scanner: _FakePullRequestScanner,
    diff_summary_cache: _FakeDiffSummaryCache,
    po_review_actions: _FakePoReviewActions,
):
    audit_logger, _ = audit

    async def bot_account_ids(dept_id: str) -> frozenset[str]:
        return frozenset({_BOT_ACCOUNT_ID})

    async def llm_diff_callback(diff_hash: str) -> str:
        return f"Computed: {diff_hash}"

    app = create_app()
    app.state.po_review = PoReviewEndpointDeps(
        oidc_validator=oidc_validator,  # type: ignore[arg-type]
        branch_scanner=branch_scanner,
        pr_scanner=pr_scanner,
        bot_account_ids=bot_account_ids,
        diff_summary_cache=diff_summary_cache,
        llm_diff_callback=llm_diff_callback,
        actions=po_review_actions,
        audit_logger=audit_logger,
        clock=lambda: _FROZEN_NOW,
    )
    return app


def _get_orphans(
    client: TestClient,
    *,
    dept_id: str = _DEPT_ID,
    token: str | None = "token-admin",
):
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.get(
        f"/api/orphan-branches?dept_id={dept_id}", headers=headers
    )


def _get_inbox(
    client: TestClient,
    *,
    dept_id: str = _DEPT_ID,
    token: str | None = "token-admin",
):
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.get(
        f"/api/po-review-inbox?dept_id={dept_id}", headers=headers
    )


def _post_action(
    client: TestClient,
    *,
    pr_id: int,
    action: str,
    dept_id: str = _DEPT_ID,
    token: str | None = "token-admin",
    body: dict[str, Any] | None = None,
):
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    url = f"/api/po-review-inbox/{pr_id}/{action}?dept_id={dept_id}"
    return client.post(url, headers=headers, json=body)


# ---------------------------------------------------------------------------
# Tests — orphan branches list shape
# ---------------------------------------------------------------------------


class TestOrphanBranchesShape:
    """**Validates: Requirements 10.3** — orphan list matches helper output."""

    def test_orphan_list_matches_compute_orphan_branches_output(
        self,
        app_with_po_review,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _get_orphans(client)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        names = [b["name"] for b in body["branches"]]
        # ai/PAY-1 has a PR → not orphan.
        # feature/manual is non-bot → not orphan.
        # ai/PAY-2 + ai/PAY-3 are bot-authored, no PR → orphan.
        assert sorted(names) == ["ai/PAY-2", "ai/PAY-3"]

    def test_orphan_list_sorted_oldest_first_by_last_commit(
        self,
        app_with_po_review,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _get_orphans(client)

        assert resp.status_code == 200, resp.text
        names = [b["name"] for b in resp.json()["branches"]]
        # ai/PAY-3 is 20 days old (older), ai/PAY-2 is 5 days old.
        # Oldest-first means ai/PAY-3 should come before ai/PAY-2.
        assert names == ["ai/PAY-3", "ai/PAY-2"]

    def test_orphan_age_days_computed_from_last_commit_at(
        self,
        app_with_po_review,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _get_orphans(client)

        rows = resp.json()["branches"]
        ages = {row["name"]: row["age_days"] for row in rows}
        assert ages["ai/PAY-2"] == 5
        assert ages["ai/PAY-3"] == 20

    def test_orphan_diff_summary_uses_cache_hit_path(
        self,
        app_with_po_review,
        diff_summary_cache: _FakeDiffSummaryCache,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _get_orphans(client)

        rows = resp.json()["branches"]
        summaries = {row["name"]: row["diff_summary"] for row in rows}
        # ai/PAY-2 was pre-seeded; ai/PAY-3 hits the compute path.
        assert summaries["ai/PAY-2"] == "Cached: rename helper."
        assert summaries["ai/PAY-3"] == "Computed: hash-pay-3"
        # The cache was consulted exactly once per orphan.
        assert sorted(diff_summary_cache.compute_calls) == [
            "hash-pay-2",
            "hash-pay-3",
        ]


# ---------------------------------------------------------------------------
# Tests — PO Review Inbox list shape
# ---------------------------------------------------------------------------


class TestPoReviewInboxShape:
    """**Validates: Requirements 10.4** — inbox matches helper output."""

    def test_inbox_returns_only_draft_bot_authored_prs(
        self,
        app_with_po_review,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _get_inbox(client)

        assert resp.status_code == 200, resp.text
        ids = [pr["id"] for pr in resp.json()["pull_requests"]]
        # Only id=1 (draft + bot author) survives the filter.
        assert ids == [1]

    def test_inbox_row_carries_expected_fields(
        self,
        app_with_po_review,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _get_inbox(client)

        pr = resp.json()["pull_requests"][0]
        assert pr["id"] == 1
        assert pr["source_branch"] == "ai/PAY-1"
        assert pr["is_draft"] is True
        assert pr["author_account_id"] == _BOT_ACCOUNT_ID
        assert pr["title"] == "PAY-1: refactor"


# ---------------------------------------------------------------------------
# Tests — RBAC: dept_admin requesting another dept => 403
# ---------------------------------------------------------------------------


class TestDeptAdminCrossDeptForbidden:
    """**Validates: Requirements 10.3 + 10.4** — dept-scope is enforced."""

    def test_dept_admin_other_dept_returns_403_on_orphans(
        self,
        app_with_po_review,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            # eve is dept_admin for ``fraud``; she requests ``payments``.
            resp = _get_orphans(
                client, dept_id=_DEPT_ID, token="token-dept-admin-other"
            )
        assert resp.status_code == 403

    def test_dept_admin_other_dept_returns_403_on_inbox(
        self,
        app_with_po_review,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _get_inbox(
                client, dept_id=_DEPT_ID, token="token-dept-admin-other"
            )
        assert resp.status_code == 403

    def test_dept_admin_other_dept_emits_rbac_denied_audit(
        self,
        app_with_po_review,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
    ) -> None:
        _, sink = audit
        with TestClient(app_with_po_review) as client:
            resp = _get_orphans(
                client, dept_id=_DEPT_ID, token="token-dept-admin-other"
            )
        assert resp.status_code == 403
        denied = [e for e in sink.events if e.action == "rbac_denied"]
        assert len(denied) == 1
        ev = denied[0]
        assert ev.actor_id == "eve"
        assert ev.actor_role == "dept_admin"
        assert ev.dept_id == _DEPT_ID

    def test_dept_admin_other_dept_does_not_call_scanners(
        self,
        app_with_po_review,
        branch_scanner: _FakeBranchScanner,
        pr_scanner: _FakePullRequestScanner,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _get_orphans(
                client, dept_id=_DEPT_ID, token="token-dept-admin-other"
            )
        assert resp.status_code == 403
        # Authorisation gate fires before any scanner call.
        assert branch_scanner.calls == []
        assert pr_scanner.calls == []

    def test_dept_admin_own_dept_can_read_inbox(
        self,
        app_with_po_review,
    ) -> None:
        # dave is dept_admin for ``payments``; reading his own dept must
        # succeed (the dept-scope check passes when ``dept_ids``
        # contains the requested ``dept_id``).
        with TestClient(app_with_po_review) as client:
            resp = _get_inbox(
                client, dept_id=_DEPT_ID, token="token-dept-admin"
            )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Tests — RBAC: admin bypasses dept-scope
# ---------------------------------------------------------------------------


class TestAdminBypassesDeptScope:
    """**Validates: Requirements 10.3 + 10.4** — admin role passes globally."""

    def test_admin_can_read_orphans_for_any_dept(
        self,
        app_with_po_review,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            # carol is global admin (no ``dept_ids``) — must pass for
            # any ``dept_id``.
            resp_pay = _get_orphans(
                client, dept_id=_DEPT_ID, token="token-admin"
            )
            resp_fraud = _get_orphans(
                client, dept_id=_OTHER_DEPT_ID, token="token-admin"
            )

        assert resp_pay.status_code == 200, resp_pay.text
        assert resp_fraud.status_code == 200, resp_fraud.text

    def test_admin_can_read_inbox_for_any_dept(
        self,
        app_with_po_review,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp_pay = _get_inbox(
                client, dept_id=_DEPT_ID, token="token-admin"
            )
            resp_fraud = _get_inbox(
                client, dept_id=_OTHER_DEPT_ID, token="token-admin"
            )

        assert resp_pay.status_code == 200, resp_pay.text
        assert resp_fraud.status_code == 200, resp_fraud.text


# ---------------------------------------------------------------------------
# Tests — Action endpoints call the right MCP methods
# ---------------------------------------------------------------------------


class TestActionEndpointsCallActions:
    """**Validates: Requirement 10.4** — action endpoints invoke adapter."""

    def test_open_draft_calls_actions_open_draft_once(
        self,
        app_with_po_review,
        po_review_actions: _FakePoReviewActions,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _post_action(
                client, pr_id=42, action="open-draft", token="token-lead"
            )

        assert resp.status_code == 202, resp.text
        assert po_review_actions.open_draft_calls == [(_DEPT_ID, 42)]
        assert po_review_actions.request_changes_calls == []
        assert po_review_actions.approve_note_calls == []

    def test_request_changes_calls_actions_with_default_comment(
        self,
        app_with_po_review,
        po_review_actions: _FakePoReviewActions,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _post_action(
                client,
                pr_id=42,
                action="request-changes",
                token="token-lead",
            )

        assert resp.status_code == 202, resp.text
        assert len(po_review_actions.request_changes_calls) == 1
        called_dept, called_pr_id, called_comment = (
            po_review_actions.request_changes_calls[0]
        )
        assert called_dept == _DEPT_ID
        assert called_pr_id == 42
        # Default fallback message when caller omits ``comment``.
        assert called_comment

    def test_request_changes_passes_caller_supplied_comment(
        self,
        app_with_po_review,
        po_review_actions: _FakePoReviewActions,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _post_action(
                client,
                pr_id=42,
                action="request-changes",
                token="token-lead",
                body={"comment": "Lütfen testleri ekleyin."},
            )

        assert resp.status_code == 202, resp.text
        called = po_review_actions.request_changes_calls[0]
        assert called[2] == "Lütfen testleri ekleyin."

    def test_approve_note_calls_actions_approve_note_once(
        self,
        app_with_po_review,
        po_review_actions: _FakePoReviewActions,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _post_action(
                client,
                pr_id=42,
                action="approve-note",
                token="token-lead",
                body={"comment": "İyi gidiyor."},
            )

        assert resp.status_code == 202, resp.text
        assert len(po_review_actions.approve_note_calls) == 1
        called_dept, called_pr_id, called_comment = (
            po_review_actions.approve_note_calls[0]
        )
        assert called_dept == _DEPT_ID
        assert called_pr_id == 42
        assert called_comment == "İyi gidiyor."
        # The approve-note action must NOT call ``open_draft`` or
        # ``request_changes`` — Bitbucket-side approval is *not*
        # what this endpoint does.
        assert po_review_actions.open_draft_calls == []
        assert po_review_actions.request_changes_calls == []

    def test_action_emits_one_audit_row(
        self,
        app_with_po_review,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
    ) -> None:
        _, sink = audit
        with TestClient(app_with_po_review) as client:
            resp = _post_action(
                client, pr_id=42, action="open-draft", token="token-lead"
            )

        assert resp.status_code == 202
        action_events = [
            e for e in sink.events if e.action == "po_review_open_draft"
        ]
        assert len(action_events) == 1
        ev = action_events[0]
        assert ev.actor_id == "alice"
        assert ev.actor_role == "lead"
        assert ev.dept_id == _DEPT_ID
        assert ev.result == "ok"
        assert ev.payload is not None
        assert ev.payload["pr_id"] == 42

    def test_viewer_cannot_invoke_action_endpoints(
        self,
        app_with_po_review,
        po_review_actions: _FakePoReviewActions,
    ) -> None:
        # Viewer is below the ``lead`` threshold for action endpoints.
        with TestClient(app_with_po_review) as client:
            resp = _post_action(
                client,
                pr_id=42,
                action="open-draft",
                token="token-viewer",
            )

        assert resp.status_code == 403
        # The adapter must not have been called.
        assert po_review_actions.open_draft_calls == []

    def test_dept_admin_other_dept_cannot_invoke_actions(
        self,
        app_with_po_review,
        po_review_actions: _FakePoReviewActions,
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _post_action(
                client,
                pr_id=42,
                action="open-draft",
                dept_id=_DEPT_ID,
                token="token-dept-admin-other",
            )

        assert resp.status_code == 403
        assert po_review_actions.open_draft_calls == []


# ---------------------------------------------------------------------------
# Tests — token-level authentication failures (401)
# ---------------------------------------------------------------------------


class TestAuthenticationFailures:
    """Defence-in-depth checks for the AuthN layer that gates AuthZ."""

    def test_missing_authorization_header_on_orphans_returns_401(
        self, app_with_po_review
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _get_orphans(client, token=None)
        assert resp.status_code == 401

    def test_invalid_bearer_token_on_inbox_returns_401(
        self, app_with_po_review
    ) -> None:
        with TestClient(app_with_po_review) as client:
            resp = _get_inbox(client, token="totally-bogus")
        assert resp.status_code == 401

    def test_missing_dept_id_query_returns_422(
        self, app_with_po_review
    ) -> None:
        # FastAPI rejects the call before it reaches our handler when
        # the required query parameter is missing.
        with TestClient(app_with_po_review) as client:
            resp = client.get(
                "/api/orphan-branches",
                headers={"Authorization": "Bearer token-admin"},
            )
        assert resp.status_code == 422
