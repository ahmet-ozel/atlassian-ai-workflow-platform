"""Integration tests for ``POST /api/workflows/{workflow_id}/cancel``.

Exercises the full FastAPI router end-to-end using ``TestClient`` plus
hand-rolled fakes for every collaborator (OIDC validator, issue
lookup, Temporal client, audit logger). Two end-to-end paths are
covered:

* **403 path** - authenticated caller is neither the issue reporter
  nor a past assignee. The endpoint emits a single ``rbac_denied``
  audit row and responds 403; ``WorkflowHandle.cancel()`` is **not**
  called.
* **202 path** - authenticated caller is the issue reporter (or
  a past assignee). The endpoint calls
  ``temporal_client.get_workflow_handle(workflow_id).cancel()``,
  emits a ``workflow_cancel_requested`` audit row, and responds 202
  with ``{"workflow_id": ..., "cancel_requested": true}``.

The tests do **not** stand up Postgres, Vault, Temporal or an IdP.
They use the same fake-collaborator pattern as the sibling Slack
inbound tests (``tests/unit/test_inbound_slack.py``), wiring the
fakes into ``app.state.cancel`` so the router's runtime contract is
exercised without external infrastructure.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _AUTOMATION_ROOT.parents[1]

for _path in (
    _AUTOMATION_ROOT / "src",
    _AUTOMATION_ROOT,
    _PLATFORM_ROOT / "libs" / "audit_logger" / "src",
    _PLATFORM_ROOT / "libs" / "auth-shared" / "src",
    _PLATFORM_ROOT / "libs" / "http-shared" / "src",
):
    _path_str = str(_path)
    if _path.is_dir() and _path_str not in sys.path:
        sys.path.insert(0, _path_str)


from audit_logger import AuditEvent, AuditLogger  # noqa: E402

from automation_service.api.cancel import (  # noqa: E402
    CancelEndpointDeps,
    IssueRef,
)
from automation_service.app import create_app  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingAuditWriter:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def insert_audit(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class _FakeOIDCValidator:
    """Stand-in for :class:`auth_shared.OIDCValidator`.

    Maps bearer tokens to the canned claim dict the production
    validator would return after a successful JWKS check. Tokens
    absent from the map are treated as invalid (the production
    validator raises :class:`InvalidTokenError`; we re-raise the same
    exception so the router's ``except`` clause is exercised).
    """

    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)

    def validate(self, token: str) -> dict[str, Any]:
        from auth_shared import InvalidTokenError

        if token in self.tokens:
            return dict(self.tokens[token])
        raise InvalidTokenError(f"unknown token {token!r}")


@dataclass
class _FakeWorkflowHandle:
    cancel_calls: int = 0
    raise_on_cancel: Exception | None = None

    async def cancel(self) -> None:
        self.cancel_calls += 1
        if self.raise_on_cancel is not None:
            raise self.raise_on_cancel


@dataclass
class _FakeTemporalClient:
    """Stand-in for :class:`temporalio.client.Client`.

    The cancel endpoint only calls ``get_workflow_handle(workflow_id)``
    and ``await handle.cancel()``; we record both for assertions.
    """

    handles: dict[str, _FakeWorkflowHandle] = field(default_factory=dict)
    seen_workflow_ids: list[str] = field(default_factory=list)

    def get_workflow_handle(self, workflow_id: str) -> _FakeWorkflowHandle:
        self.seen_workflow_ids.append(workflow_id)
        handle = self.handles.get(workflow_id)
        if handle is None:
            handle = _FakeWorkflowHandle()
            self.handles[workflow_id] = handle
        return handle


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_FROZEN_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
_WORKFLOW_ID = "automation-jira-PAY-4211"


@pytest.fixture
def audit() -> tuple[AuditLogger, _RecordingAuditWriter]:
    sink = _RecordingAuditWriter()
    return AuditLogger(writer=sink), sink


@pytest.fixture
def oidc_validator() -> _FakeOIDCValidator:
    return _FakeOIDCValidator(
        tokens={
            "token-alice": {
                "sub": "alice",
                "account_id": "alice",
                "role": "lead",
            },
            "token-bob": {
                "sub": "bob",
                "account_id": "bob",
                "role": "viewer",
            },
            "token-dave": {
                "sub": "dave",
                "account_id": "dave",
                "role": "viewer",
            },
        }
    )


@pytest.fixture
def temporal_client() -> _FakeTemporalClient:
    return _FakeTemporalClient()


@pytest.fixture
def issue_ref() -> IssueRef:
    """Issue: reporter=alice, past_assignees={bob, carol}."""

    return IssueRef(
        reporter="alice",
        past_assignees=frozenset({"bob", "carol"}),
    )


@pytest.fixture
def app_with_cancel(
    audit: tuple[AuditLogger, _RecordingAuditWriter],
    oidc_validator: _FakeOIDCValidator,
    temporal_client: _FakeTemporalClient,
    issue_ref: IssueRef,
):
    audit_logger, _ = audit

    async def issue_lookup(workflow_id: str) -> IssueRef | None:
        # Single workflow_id we know about - anything else => 404.
        if workflow_id == _WORKFLOW_ID:
            return issue_ref
        return None

    app = create_app()
    app.state.cancel = CancelEndpointDeps(
        oidc_validator=oidc_validator,  # type: ignore[arg-type]
        issue_lookup=issue_lookup,
        temporal_client=temporal_client,
        audit_logger=audit_logger,
        clock=lambda: _FROZEN_NOW,
    )
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _post_cancel(client: TestClient, workflow_id: str, token: str | None):
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(f"/api/workflows/{workflow_id}/cancel", headers=headers)


class TestCancelEndpoint403Path:
    """Authenticated but unauthorized actor => 403 + rbac_denied audit."""

    def test_returns_403_when_actor_is_not_reporter_or_past_assignee(
        self,
        app_with_cancel,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
        temporal_client: _FakeTemporalClient,
    ) -> None:
        _, sink = audit
        client = TestClient(app_with_cancel)

        # Dave is neither the reporter (alice) nor in the past
        # assignees ({bob, carol}); the predicate returns False.
        resp = _post_cancel(client, _WORKFLOW_ID, "token-dave")

        assert resp.status_code == 403
        body = resp.json()
        assert body["detail"] == "not authorized to cancel this workflow"

        # Temporal cancel must NOT have been invoked.
        assert _WORKFLOW_ID not in temporal_client.handles or all(
            h.cancel_calls == 0 for h in temporal_client.handles.values()
        )

        # A single ``rbac_denied`` audit row was written.
        rbac_denied_events = [e for e in sink.events if e.action == "rbac_denied"]
        assert len(rbac_denied_events) == 1
        ev = rbac_denied_events[0]
        assert ev.actor_id == "dave"
        assert ev.resource == f"workflow:{_WORKFLOW_ID}"
        assert ev.result == "denied"
        assert ev.timestamp == _FROZEN_NOW
        assert ev.payload is not None
        assert ev.payload["reason"] == (
            "actor is neither reporter nor past assignee"
        )

    def test_missing_authorization_header_returns_401(
        self,
        app_with_cancel,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
        temporal_client: _FakeTemporalClient,
    ) -> None:
        _, sink = audit
        client = TestClient(app_with_cancel)

        resp = _post_cancel(client, _WORKFLOW_ID, token=None)

        assert resp.status_code == 401
        # Cancel never reached.
        assert temporal_client.handles == {}
        # No rbac_denied audit because the request never authenticated.
        assert not any(e.action == "rbac_denied" for e in sink.events)


class TestCancelEndpoint202Path:
    """Authorized actor => Temporal cancel called + 202 response."""

    def test_reporter_can_cancel(
        self,
        app_with_cancel,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
        temporal_client: _FakeTemporalClient,
    ) -> None:
        _, sink = audit
        client = TestClient(app_with_cancel)

        # Alice is the issue reporter → predicate returns True.
        resp = _post_cancel(client, _WORKFLOW_ID, "token-alice")

        assert resp.status_code == 202
        assert resp.json() == {
            "workflow_id": _WORKFLOW_ID,
            "cancel_requested": True,
        }

        # Temporal handle resolved and cancelled exactly once.
        assert temporal_client.seen_workflow_ids == [_WORKFLOW_ID]
        handle = temporal_client.handles[_WORKFLOW_ID]
        assert handle.cancel_calls == 1

        # Audit row records the success.
        success_events = [
            e for e in sink.events if e.action == "workflow_cancel_requested"
        ]
        assert len(success_events) == 1
        ev = success_events[0]
        assert ev.actor_id == "alice"
        assert ev.resource == f"workflow:{_WORKFLOW_ID}"
        assert ev.result == "ok"
        assert ev.timestamp == _FROZEN_NOW

        # No rbac_denied row was emitted.
        assert not any(e.action == "rbac_denied" for e in sink.events)

    def test_past_assignee_can_cancel(
        self,
        app_with_cancel,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
        temporal_client: _FakeTemporalClient,
    ) -> None:
        _, sink = audit
        client = TestClient(app_with_cancel)

        # Bob is in past_assignees → predicate returns True.
        resp = _post_cancel(client, _WORKFLOW_ID, "token-bob")

        assert resp.status_code == 202
        assert resp.json() == {
            "workflow_id": _WORKFLOW_ID,
            "cancel_requested": True,
        }

        handle = temporal_client.handles[_WORKFLOW_ID]
        assert handle.cancel_calls == 1

        success_events = [
            e for e in sink.events if e.action == "workflow_cancel_requested"
        ]
        assert len(success_events) == 1
        assert success_events[0].actor_id == "bob"

    def test_unknown_workflow_returns_404(
        self,
        app_with_cancel,
        temporal_client: _FakeTemporalClient,
    ) -> None:
        client = TestClient(app_with_cancel)

        # The fake issue_lookup only knows about _WORKFLOW_ID.
        resp = _post_cancel(client, "automation-jira-OTHER-1", "token-alice")

        assert resp.status_code == 404
        # Temporal cancel never called.
        assert temporal_client.handles == {}
