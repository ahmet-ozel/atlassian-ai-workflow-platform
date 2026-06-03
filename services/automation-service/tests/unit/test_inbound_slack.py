"""Integration-style tests for ``POST /webhooks/inbound/slack``.

Exercises the FastAPI route end-to-end using ``TestClient`` plus
hand-rolled fakes for every collaborator (dept resolver, signature
verifier, workflow client, audit logger). The tests cover:

* The happy path (signed mention → 202 ``accepted`` + workflow
  started + audit ``inbound_workflow_started``).
* Idempotent retry (same external id → ``was_existing=True`` +
  ``inbound_workflow_already_started``).
* Bad signature → 401 + ``inbound_slack_hmac_failed``.
* Unknown dept → 400 + ``inbound_dept_unresolved``.
* URL verification handshake → 200 with the challenge.
* Empty mention → 200 ``ignored``, **no** workflow started.
* Unsupported event type → 200 ``ignored``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from temporalio.exceptions import WorkflowAlreadyStartedError

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _AUTOMATION_ROOT.parents[1]

for path in (
    _AUTOMATION_ROOT / "src",
    _AUTOMATION_ROOT,
    _PLATFORM_ROOT / "libs" / "audit_logger" / "src",
    _PLATFORM_ROOT / "libs" / "http-shared" / "src",
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from audit_logger import AuditEvent, AuditLogger  # noqa: E402

from automation_service.app import create_app  # noqa: E402
from automation_service.inbound.common import (  # noqa: E402
    InboundContext,
    build_inbound_workflow_id,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingAuditWriter:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def insert_audit(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class _FakeDeptResolver:
    slack_map: dict[tuple[str | None, str | None], str] = field(default_factory=dict)
    email_map: dict[str, str] = field(default_factory=dict)

    async def resolve_for_slack(
        self, *, team_id: str | None, channel_id: str | None
    ) -> str | None:
        return self.slack_map.get((team_id, channel_id))

    async def resolve_for_email(self, *, recipient: str) -> str | None:
        return self.email_map.get(recipient.lower())


@dataclass
class _FakeSlackVerifier:
    accept: bool = True
    seen_dept_ids: list[str | None] = field(default_factory=list)

    async def verify(
        self,
        *,
        dept_id: str | None,
        timestamp: str,
        raw_body: bytes,
        signature: str,
        now: datetime,
    ) -> bool:
        self.seen_dept_ids.append(dept_id)
        return self.accept


@dataclass
class _FakeWorkflowClient:
    """Records ``start_workflow`` calls.

    By default each call succeeds; passing ``raise_already_started=True``
    makes every subsequent call raise the Temporal duplicate exception
    so :func:`start_workflow_idempotent` exercises its
    ``was_existing=True`` branch.
    """

    raise_already_started: bool = False
    raise_other: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)
    _started_ids: set[str] = field(default_factory=set)

    async def start_workflow(
        self,
        workflow: str,
        *args: Any,
        id: str,
        task_queue: str,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {
                "workflow": workflow,
                "args": list(args),
                "id": id,
                "task_queue": task_queue,
                "kwargs": dict(kwargs),
            }
        )
        if self.raise_other is not None:
            raise self.raise_other
        if self.raise_already_started or id in self._started_ids:
            raise WorkflowAlreadyStartedError(
                workflow_id=id,
                workflow_type=workflow,
                run_id=None,
            )
        self._started_ids.add(id)
        return object()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_FROZEN_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def audit() -> tuple[AuditLogger, _RecordingAuditWriter]:
    sink = _RecordingAuditWriter()
    return AuditLogger(writer=sink), sink


@pytest.fixture
def dept_resolver() -> _FakeDeptResolver:
    return _FakeDeptResolver(
        slack_map={("T123", "C99"): "payment"},
        email_map={"bot@example.com": "payment"},
    )


@pytest.fixture
def workflow_client() -> _FakeWorkflowClient:
    return _FakeWorkflowClient()


@pytest.fixture
def signature_verifier() -> _FakeSlackVerifier:
    return _FakeSlackVerifier(accept=True)


@pytest.fixture
def app_with_inbound(
    audit: tuple[AuditLogger, _RecordingAuditWriter],
    dept_resolver: _FakeDeptResolver,
    workflow_client: _FakeWorkflowClient,
    signature_verifier: _FakeSlackVerifier,
):
    audit_logger, _ = audit
    app = create_app()
    app.state.inbound = InboundContext(
        dept_resolver=dept_resolver,
        workflow_client=workflow_client,
        slack_verifier=signature_verifier,
        audit_logger=audit_logger,
        env={},
        now_fn=lambda: _FROZEN_NOW,
    )
    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slack_payload(
    *,
    text: str = "<@U07BOT> open a ticket for the API",
    team_id: str = "T123",
    channel_id: str = "C99",
    user_id: str = "U07USER",
    client_msg_id: str | None = "msg-uuid-1",
    event_type: str = "app_mention",
    envelope_type: str = "event_callback",
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": event_type,
        "user": user_id,
        "channel": channel_id,
        "text": text,
        "ts": "1700000000.000123",
    }
    if client_msg_id:
        event["client_msg_id"] = client_msg_id
    return {
        "type": envelope_type,
        "team_id": team_id,
        "event": event,
    }


def _send(client: TestClient, payload: dict[str, Any]):
    return client.post(
        "/webhooks/inbound/slack",
        content=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Slack-Signature": "v0=" + "a" * 64,
            "X-Slack-Request-Timestamp": str(int(_FROZEN_NOW.timestamp())),
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSlackInboundHappyPath:
    def test_starts_workflow_with_auto_assign_smart_defaults(
        self,
        app_with_inbound,
        workflow_client: _FakeWorkflowClient,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
    ) -> None:
        client = TestClient(app_with_inbound)
        resp = _send(client, _slack_payload())

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["channel"] == "slack"
        assert body["was_existing"] is False
        # Workflow id is deterministic.
        assert body["workflow_id"] == build_inbound_workflow_id("slack", "msg-uuid-1")

        # Workflow client was invoked exactly once with the
        # auto-assign + smart-defaults shape.
        assert len(workflow_client.calls) == 1
        call = workflow_client.calls[0]
        assert call["workflow"] == "AutomationWorkflow"
        assert call["task_queue"] == "automation-tq"
        wf_input = call["args"][0]
        assert wf_input["auto_assign"] is True
        assert wf_input["smart_defaults"] is True
        assert wf_input["trigger"] == "inbound_slack"
        assert wf_input["channel"] == "slack"
        assert wf_input["department_id"] == "payment"
        assert wf_input["intent_text"] == "open a ticket for the API"

        # Audit event recorded.
        _, sink = audit
        actions = [e.action for e in sink.events]
        assert "inbound_workflow_started" in actions
        started = next(
            e for e in sink.events if e.action == "inbound_workflow_started"
        )
        assert started.actor_role == "system"
        assert started.dept_id == "payment"
        assert started.payload is not None
        assert started.payload["auto_assign"] is True
        assert started.payload["smart_defaults"] is True


class TestSlackInboundIdempotency:
    def test_retry_collapses_to_was_existing(
        self,
        app_with_inbound,
        workflow_client: _FakeWorkflowClient,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
    ) -> None:
        client = TestClient(app_with_inbound)
        first = _send(client, _slack_payload())
        second = _send(client, _slack_payload())

        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["workflow_id"] == second.json()["workflow_id"]
        assert first.json()["was_existing"] is False
        assert second.json()["was_existing"] is True

        # Audit log records both flavours.
        _, sink = audit
        actions = [e.action for e in sink.events]
        assert actions.count("inbound_workflow_started") == 1
        assert actions.count("inbound_workflow_already_started") == 1


class TestSlackInboundHmacFailure:
    def test_invalid_signature_returns_401(
        self,
        app_with_inbound,
        signature_verifier: _FakeSlackVerifier,
        workflow_client: _FakeWorkflowClient,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
    ) -> None:
        signature_verifier.accept = False
        client = TestClient(app_with_inbound)
        resp = _send(client, _slack_payload())
        assert resp.status_code == 401
        assert resp.json()["status"] == "unauthorized"
        # No workflow started.
        assert workflow_client.calls == []
        _, sink = audit
        assert any(
            e.action == "inbound_slack_hmac_failed" and e.result == "denied"
            for e in sink.events
        )


class TestSlackInboundDeptUnresolved:
    def test_unknown_team_returns_400(
        self,
        app_with_inbound,
        dept_resolver: _FakeDeptResolver,
        workflow_client: _FakeWorkflowClient,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
    ) -> None:
        # No mapping for ("UNKNOWN_TEAM", "C99").
        client = TestClient(app_with_inbound)
        resp = _send(client, _slack_payload(team_id="UNKNOWN_TEAM"))
        assert resp.status_code == 400
        assert resp.json()["reason"] == "inbound_dept_unresolved"
        assert workflow_client.calls == []
        _, sink = audit
        assert any(
            e.action == "inbound_dept_unresolved" and e.result == "denied"
            for e in sink.events
        )


class TestSlackInboundUrlVerification:
    def test_challenge_echo_with_valid_signature(
        self, app_with_inbound, signature_verifier: _FakeSlackVerifier
    ) -> None:
        client = TestClient(app_with_inbound)
        resp = client.post(
            "/webhooks/inbound/slack",
            content=json.dumps(
                {"type": "url_verification", "challenge": "abc123"}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Slack-Signature": "v0=" + "a" * 64,
                "X-Slack-Request-Timestamp": str(int(_FROZEN_NOW.timestamp())),
            },
        )
        assert resp.status_code == 200
        assert resp.json() == {"challenge": "abc123"}
        # Verifier was called with dept_id=None (platform default).
        assert signature_verifier.seen_dept_ids[-1] is None

    def test_challenge_with_invalid_signature_returns_401(
        self, app_with_inbound, signature_verifier: _FakeSlackVerifier
    ) -> None:
        signature_verifier.accept = False
        client = TestClient(app_with_inbound)
        resp = client.post(
            "/webhooks/inbound/slack",
            content=json.dumps(
                {"type": "url_verification", "challenge": "abc123"}
            ).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Slack-Signature": "v0=" + "a" * 64,
                "X-Slack-Request-Timestamp": str(int(_FROZEN_NOW.timestamp())),
            },
        )
        assert resp.status_code == 401


class TestSlackInboundEdgeCases:
    def test_empty_mention_is_ignored(
        self,
        app_with_inbound,
        workflow_client: _FakeWorkflowClient,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
    ) -> None:
        client = TestClient(app_with_inbound)
        resp = _send(client, _slack_payload(text="<@U07BOT>"))
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"
        assert resp.json()["reason"] == "empty_mention"
        assert workflow_client.calls == []
        _, sink = audit
        assert any(e.action == "inbound_empty_mention" for e in sink.events)

    def test_unsupported_event_is_ignored(
        self, app_with_inbound, workflow_client: _FakeWorkflowClient
    ) -> None:
        client = TestClient(app_with_inbound)
        payload = _slack_payload(event_type="reaction_added")
        resp = _send(client, payload)
        assert resp.status_code == 200
        assert resp.json()["reason"] == "unsupported_event"
        assert workflow_client.calls == []

    def test_invalid_json_returns_400(
        self, app_with_inbound, workflow_client: _FakeWorkflowClient
    ) -> None:
        client = TestClient(app_with_inbound)
        resp = client.post(
            "/webhooks/inbound/slack",
            content=b"not json{",
            headers={
                "Content-Type": "application/json",
                "X-Slack-Signature": "v0=" + "a" * 64,
                "X-Slack-Request-Timestamp": str(int(_FROZEN_NOW.timestamp())),
            },
        )
        assert resp.status_code == 400
        assert workflow_client.calls == []

    def test_no_inbound_context_returns_503(self) -> None:
        # Build a clean app whose ``app.state.inbound`` is unset.
        app = create_app()
        # Do NOT populate ``app.state.inbound``.
        client = TestClient(app)
        resp = client.post(
            "/webhooks/inbound/slack",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 503
        assert resp.json()["reason"] == "inbound_not_wired"

    def test_missing_external_id_returns_400(
        self,
        app_with_inbound,
        workflow_client: _FakeWorkflowClient,
        audit: tuple[AuditLogger, _RecordingAuditWriter],
    ) -> None:
        client = TestClient(app_with_inbound)
        payload = _slack_payload(client_msg_id=None)
        # Also drop ``ts`` so neither id source is present.
        payload["event"].pop("ts", None)
        resp = _send(client, payload)
        assert resp.status_code == 400
        assert resp.json()["reason"] == "missing_external_id"
        assert workflow_client.calls == []
