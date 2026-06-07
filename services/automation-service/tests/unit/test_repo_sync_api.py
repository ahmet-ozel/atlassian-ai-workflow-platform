"""Unit tests for ``POST /admin/departments/{id}/repo-mappings/sync``.

Exercises the FastAPI router end-to-end via :class:`TestClient` plus
hand-rolled fakes for every collaborator (OIDC validator, Bitbucket
scanner, departments registry, audit logger). Three endpoint behaviors
are covered:

* **Dry-run does not mutate** - POST without ``?apply=true`` runs
  the scan + diff and returns the partition JSON, but
  :meth:`SupportsDepartmentsRepo.update_repo_mappings` is **never**
  called.
* **Apply mode mutates + audits** - POST with ``?apply=true`` calls
  ``update_repo_mappings`` exactly once with the new mapping list
  and emits a single ``repo_mapping_synced`` audit row carrying the
  diff in the payload.
* **Non-admin → 403** - an authenticated viewer (or any non-admin
  role) receives HTTP 403 with an ``rbac_denied`` audit row; the
  scanner and the registry writer are **never** called.

The tests do not stand up Postgres, Vault, Temporal, or an IdP. The
fakes mirror the same pattern used by the sibling cancel-endpoint
test (``tests/integration/test_cancel_endpoint.py``).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Path setup - mirrors the sibling cancel-endpoint test so the unit
# suite can run without an editable install of every libs/* package.
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

from automation_service.api.repo_sync import (  # noqa: E402
    RepoSyncEndpointDeps,
)
from automation_service.app import create_app  # noqa: E402
from temporal_shared import RepoMapping  # noqa: E402


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
    """Stand-in for :class:`auth_shared.OIDCValidator`.

    Maps bearer tokens to the claim dict the production validator
    would emit after a JWKS check. Tokens absent from the map are
    treated as invalid (raise :class:`InvalidTokenError`).
    """

    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)

    def validate(self, token: str) -> dict[str, Any]:
        from auth_shared import InvalidTokenError

        if token in self.tokens:
            return dict(self.tokens[token])
        raise InvalidTokenError(f"unknown token {token!r}")


@dataclass
class _FakeBitbucketScanner:
    """Records every call and returns a canned repo descriptor list."""

    repos: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    calls: list[str] = field(default_factory=list)
    raise_on_call: Exception | None = None

    async def __call__(
        self, dept_id: str
    ) -> Sequence[Mapping[str, Any]]:
        self.calls.append(dept_id)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return list(self.repos)


@dataclass
class _FakeDepartmentsRepo:
    """Records reads + writes; never raises on the happy path."""

    initial_mappings: tuple[RepoMapping, ...] = ()
    list_calls: list[str] = field(default_factory=list)
    update_calls: list[tuple[str, tuple[RepoMapping, ...]]] = field(
        default_factory=list
    )

    async def list_repo_mappings(
        self, dept_id: str
    ) -> tuple[RepoMapping, ...]:
        self.list_calls.append(dept_id)
        return self.initial_mappings

    async def update_repo_mappings(
        self, dept_id: str, new_mappings: tuple[RepoMapping, ...]
    ) -> None:
        self.update_calls.append((dept_id, new_mappings))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_FROZEN_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
_DEPT_ID = "payments"


@pytest.fixture
def audit() -> tuple[AuditLogger, _RecordingAuditWriter]:
    sink = _RecordingAuditWriter()
    return AuditLogger(writer=sink), sink


@pytest.fixture
def oidc_validator() -> _FakeOIDCValidator:
    """Three roles: admin (carol), viewer (bob), dept_admin (dave)."""

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
            },
            "token-dept-admin": {
                "sub": "dave",
                "account_id": "dave",
                "role": "dept_admin",
                "dept_ids": [_DEPT_ID],
            },
        }
    )


@pytest.fixture
def scanner() -> _FakeBitbucketScanner:
    """Canned MCP response: scanned slugs = {payment-callbacks, fraud-rules, new-repo}.

    Combined with the registry's initial mappings ({payment-callbacks,
    legacy-repo}) this yields the diff:

    * added     = {fraud-rules, new-repo}
    * removed   = {legacy-repo}
    * unchanged = {payment-callbacks}
    """

    return _FakeBitbucketScanner(
        repos=(
            {"name": "Payment Callbacks", "slug": "payment-callbacks"},
            {"name": "Fraud Rules", "slug": "fraud-rules"},
            {"name": "New Repo", "slug": "new-repo"},
        ),
    )


@pytest.fixture
def departments_repo() -> _FakeDepartmentsRepo:
    return _FakeDepartmentsRepo(
        initial_mappings=(
            RepoMapping(
                name="Payment Callbacks", slug="payment-callbacks"
            ),
            RepoMapping(name="Legacy Repo", slug="legacy-repo"),
        ),
    )


@pytest.fixture
def app_with_repo_sync(
    audit: tuple[AuditLogger, _RecordingAuditWriter],
    oidc_validator: _FakeOIDCValidator,
    scanner: _FakeBitbucketScanner,
    departments_repo: _FakeDepartmentsRepo,
):
    audit_logger, _ = audit

    app = create_app()
    app.state.repo_sync = RepoSyncEndpointDeps(
        oidc_validator=oidc_validator,  # type: ignore[arg-type]
        bitbucket_scanner=scanner,
        departments_repo=departments_repo,
        audit_logger=audit_logger,
        clock=lambda: _FROZEN_NOW,
    )
    return app


def _post_sync(
    client: TestClient,
    *,
    dept_id: str = _DEPT_ID,
    token: str | None = "token-admin",
    apply: bool = False,
):
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    url = f"/admin/departments/{dept_id}/repo-mappings/sync"
    if apply:
        url += "?apply=true"
    return client.post(url, headers=headers)


# ---------------------------------------------------------------------------
# Tests - dry-run mode
# ---------------------------------------------------------------------------


class TestDryRunDoesNotMutate:
    """Dry-run returns diff, no writes."""

    def test_dry_run_returns_diff_partitions(
        self,
        app_with_repo_sync,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
    ) -> None:
        with TestClient(app_with_repo_sync) as client:
            resp = _post_sync(client, apply=False)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert sorted(body["added"]) == ["fraud-rules", "new-repo"]
        assert body["removed"] == ["legacy-repo"]
        assert body["unchanged"] == ["payment-callbacks"]
        # ``applied`` is the explicit signal that no write happened.
        assert body["applied"] is False

    def test_dry_run_does_not_call_update_repo_mappings(
        self,
        app_with_repo_sync,
        departments_repo: _FakeDepartmentsRepo,
    ) -> None:
        with TestClient(app_with_repo_sync) as client:
            resp = _post_sync(client, apply=False)

        assert resp.status_code == 200, resp.text
        # Read happened once (the dry-run still needs the current
        # mappings to compute the diff), the write never.
        assert departments_repo.list_calls == [_DEPT_ID]
        assert departments_repo.update_calls == []

    def test_dry_run_emits_synced_audit_with_dry_run_mode(
        self,
        app_with_repo_sync,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
    ) -> None:
        _, sink = audit
        with TestClient(app_with_repo_sync) as client:
            resp = _post_sync(client, apply=False)

        assert resp.status_code == 200, resp.text
        synced = [e for e in sink.events if e.action == "repo_mapping_synced"]
        assert len(synced) == 1, sink.events
        event = synced[0]
        assert event.result == "ok"
        assert event.dept_id == _DEPT_ID
        assert event.actor_id == "carol"
        assert event.actor_role == "admin"
        assert event.payload is not None
        assert event.payload["mode"] == "dry_run"
        assert sorted(event.payload["diff"]["added"]) == [
            "fraud-rules",
            "new-repo",
        ]
        assert event.payload["diff"]["removed"] == ["legacy-repo"]
        assert event.payload["diff"]["unchanged"] == ["payment-callbacks"]


# ---------------------------------------------------------------------------
# Tests - apply mode
# ---------------------------------------------------------------------------


class TestApplyModeMutatesAndAudits:
    """Apply persists and emits an audit row."""

    def test_apply_calls_update_repo_mappings_with_new_list(
        self,
        app_with_repo_sync,
        departments_repo: _FakeDepartmentsRepo,
    ) -> None:
        with TestClient(app_with_repo_sync) as client:
            resp = _post_sync(client, apply=True)

        assert resp.status_code == 200, resp.text
        assert resp.json()["applied"] is True
        # update_repo_mappings was called exactly once with the
        # composed new list: surviving entries first (payment-callbacks
        # only - legacy-repo dropped), then added entries in slug
        # order (fraud-rules, new-repo).
        assert len(departments_repo.update_calls) == 1
        called_dept_id, new_mappings = departments_repo.update_calls[0]
        assert called_dept_id == _DEPT_ID
        slugs = [m.slug for m in new_mappings]
        assert slugs == ["payment-callbacks", "fraud-rules", "new-repo"]
        # The surviving entry preserves its original ``name`` (the
        # operator may have customised it); the added entries take
        # the MCP-supplied name.
        names_by_slug = {m.slug: m.name for m in new_mappings}
        assert names_by_slug["payment-callbacks"] == "Payment Callbacks"
        assert names_by_slug["fraud-rules"] == "Fraud Rules"
        assert names_by_slug["new-repo"] == "New Repo"

    def test_apply_emits_synced_audit_with_apply_mode(
        self,
        app_with_repo_sync,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
    ) -> None:
        _, sink = audit
        with TestClient(app_with_repo_sync) as client:
            resp = _post_sync(client, apply=True)

        assert resp.status_code == 200, resp.text
        synced = [e for e in sink.events if e.action == "repo_mapping_synced"]
        assert len(synced) == 1
        event = synced[0]
        assert event.payload is not None
        assert event.payload["mode"] == "apply"
        assert sorted(event.payload["diff"]["added"]) == [
            "fraud-rules",
            "new-repo",
        ]

    def test_apply_response_contains_diff_and_applied_flag(
        self,
        app_with_repo_sync,
    ) -> None:
        with TestClient(app_with_repo_sync) as client:
            resp = _post_sync(client, apply=True)

        body = resp.json()
        assert body["applied"] is True
        # Same diff fields as dry-run - apply mode does not change
        # the response shape, only the side effect.
        assert sorted(body["added"]) == ["fraud-rules", "new-repo"]
        assert body["removed"] == ["legacy-repo"]
        assert body["unchanged"] == ["payment-callbacks"]


# ---------------------------------------------------------------------------
# Tests - RBAC: non-admin → 403
# ---------------------------------------------------------------------------


class TestNonAdminForbidden:
    """Admin role is required."""

    @pytest.mark.parametrize(
        "token,expected_actor_id,expected_role",
        [
            ("token-viewer", "bob", "viewer"),
            # dept_admin role is *not* the same as global admin - even
            # for the dept's own ``dept_id`` the global guard rejects
            # it because ``required_role="admin"`` only admits the
            # ``"admin"`` role.
            ("token-dept-admin", "dave", "dept_admin"),
        ],
    )
    def test_non_admin_returns_403(
        self,
        app_with_repo_sync,
        token: str,
        expected_actor_id: str,
        expected_role: str,
    ) -> None:
        with TestClient(app_with_repo_sync) as client:
            resp = _post_sync(client, token=token, apply=False)

        assert resp.status_code == 403

    def test_non_admin_emits_rbac_denied_audit(
        self,
        app_with_repo_sync,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
    ) -> None:
        _, sink = audit
        with TestClient(app_with_repo_sync) as client:
            resp = _post_sync(
                client, token="token-viewer", apply=False
            )

        assert resp.status_code == 403
        denied = [e for e in sink.events if e.action == "rbac_denied"]
        assert len(denied) == 1, sink.events
        event = denied[0]
        assert event.result == "denied"
        assert event.dept_id == _DEPT_ID
        assert event.actor_id == "bob"
        assert event.actor_role == "viewer"
        assert event.payload is not None
        assert event.payload["required_role"] == "admin"

    def test_non_admin_does_not_call_scanner_or_writer(
        self,
        app_with_repo_sync,
        scanner: _FakeBitbucketScanner,
        departments_repo: _FakeDepartmentsRepo,
    ) -> None:
        with TestClient(app_with_repo_sync) as client:
            # Even with apply=true, a non-admin must not reach the
            # scanner or the registry writer.
            resp = _post_sync(
                client, token="token-viewer", apply=True
            )

        assert resp.status_code == 403
        assert scanner.calls == []
        assert departments_repo.list_calls == []
        assert departments_repo.update_calls == []

    def test_no_synced_audit_emitted_on_403(
        self,
        app_with_repo_sync,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
    ) -> None:
        _, sink = audit
        with TestClient(app_with_repo_sync) as client:
            resp = _post_sync(client, token="token-viewer", apply=False)

        assert resp.status_code == 403
        # Authorization failure must not also emit a "synced" event -
        # the audit trail should make the denial unambiguous.
        synced = [e for e in sink.events if e.action == "repo_mapping_synced"]
        assert synced == []


# ---------------------------------------------------------------------------
# Tests - token-level authentication failures (401)
# ---------------------------------------------------------------------------


class TestAuthenticationFailures:
    """Defence-in-depth checks for the AuthN layer that gates AuthZ."""

    def test_missing_authorization_header_returns_401(
        self, app_with_repo_sync
    ) -> None:
        with TestClient(app_with_repo_sync) as client:
            resp = _post_sync(client, token=None, apply=False)
        assert resp.status_code == 401

    def test_invalid_bearer_token_returns_401(
        self, app_with_repo_sync
    ) -> None:
        with TestClient(app_with_repo_sync) as client:
            resp = _post_sync(client, token="totally-bogus", apply=False)
        assert resp.status_code == 401
