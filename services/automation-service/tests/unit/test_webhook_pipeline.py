"""Unit tests for the WebhookPipeline orchestrator.

Tests verify:
- Sequential stage execution (dedup  loop_guard  dispatcher)
- Pipeline stops on non-PASS actions (DROP, SIGNALED, etc.)
- Audit logging for each stage result
- Payload extraction from raw webhook data
- FastAPI endpoint integration
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.webhooks.pipeline import (
    PipelineResult,
    PipelineStage,
    StageAction,
    StageResult,
    WebhookPayload,
    WebhookPipeline,
    _derive_event_id,
    extract_webhook_payload,
    router,
)


# ---------------------------------------------------------------------------
# Test fixtures / helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeStage:
    """A fake pipeline stage for testing."""

    _name: str
    _result: StageResult
    called: bool = False
    received_payload: WebhookPayload | None = None

    @property
    def name(self) -> str:
        return self._name

    async def check(self, payload: WebhookPayload) -> StageResult:
        self.called = True
        self.received_payload = payload
        return self._result


@dataclass
class ErrorStage:
    """A stage that raises an exception."""

    _name: str

    @property
    def name(self) -> str:
        return self._name

    async def check(self, payload: WebhookPayload) -> StageResult:
        raise RuntimeError("stage exploded")


def _make_payload(
    *,
    event_type: str = "jira:issue_updated",
    issue_key: str = "PROJ-123",
    actor_account_id: str | None = "user-abc",
    assignee_account_id: str | None = "bot-xyz",
    event_id: str | None = "evt-001",
    trace_id: str | None = None,
) -> WebhookPayload:
    return WebhookPayload(
        event_id=event_id,
        event_type=event_type,
        issue_key=issue_key,
        actor_account_id=actor_account_id,
        assignee_account_id=assignee_account_id,
        trace_id=trace_id,
        raw_payload={},
        headers={},
    )


# ---------------------------------------------------------------------------
# WebhookPipeline tests
# ---------------------------------------------------------------------------


class TestWebhookPipeline:
    """Tests for the WebhookPipeline orchestrator."""

    @pytest.mark.asyncio
    async def test_all_stages_pass(self) -> None:
        """When all stages return PASS, pipeline completes with last action."""
        dedup = FakeStage("dedup", StageResult(action=StageAction.PASS))
        loop_guard = FakeStage("loop_guard", StageResult(action=StageAction.PASS))
        dispatcher = FakeStage(
            "dispatcher",
            StageResult(
                action=StageAction.WORKFLOW_STARTED,
                trace_id="trace-123",
                metadata={"workflow_id": "wf-1"},
            ),
        )

        pipeline = WebhookPipeline(stages=[dedup, loop_guard, dispatcher])
        payload = _make_payload()

        result = await pipeline.process(payload)

        # All stages were called
        assert dedup.called
        assert loop_guard.called
        assert dispatcher.called

        # Final result reflects the dispatcher's terminal action
        assert result.final_action == StageAction.WORKFLOW_STARTED
        assert result.trace_id == "trace-123"
        assert len(result.stage_results) == 3
        assert result.dropped_at == "dispatcher"

    @pytest.mark.asyncio
    async def test_dedup_drops(self) -> None:
        """When dedup drops, loop_guard and dispatcher are not called."""
        dedup = FakeStage(
            "dedup",
            StageResult(action=StageAction.DROP, reason="duplicate"),
        )
        loop_guard = FakeStage("loop_guard", StageResult(action=StageAction.PASS))
        dispatcher = FakeStage(
            "dispatcher",
            StageResult(action=StageAction.WORKFLOW_STARTED),
        )

        pipeline = WebhookPipeline(stages=[dedup, loop_guard, dispatcher])
        payload = _make_payload()

        result = await pipeline.process(payload)

        assert dedup.called
        assert not loop_guard.called
        assert not dispatcher.called
        assert result.final_action == StageAction.DROP
        assert result.dropped_at == "dedup"
        assert len(result.stage_results) == 1

    @pytest.mark.asyncio
    async def test_loop_guard_drops(self) -> None:
        """When loop_guard drops, dispatcher is not called."""
        dedup = FakeStage("dedup", StageResult(action=StageAction.PASS))
        loop_guard = FakeStage(
            "loop_guard",
            StageResult(action=StageAction.DROP, reason="loop_guard"),
        )
        dispatcher = FakeStage(
            "dispatcher",
            StageResult(action=StageAction.WORKFLOW_STARTED),
        )

        pipeline = WebhookPipeline(stages=[dedup, loop_guard, dispatcher])
        payload = _make_payload()

        result = await pipeline.process(payload)

        assert dedup.called
        assert loop_guard.called
        assert not dispatcher.called
        assert result.final_action == StageAction.DROP
        assert result.dropped_at == "loop_guard"
        assert len(result.stage_results) == 2

    @pytest.mark.asyncio
    async def test_dispatcher_signals(self) -> None:
        """Dispatcher can return SIGNALED for needs_info comments."""
        dedup = FakeStage("dedup", StageResult(action=StageAction.PASS))
        loop_guard = FakeStage("loop_guard", StageResult(action=StageAction.PASS))
        dispatcher = FakeStage(
            "dispatcher",
            StageResult(action=StageAction.SIGNALED, reason="info_received"),
        )

        pipeline = WebhookPipeline(stages=[dedup, loop_guard, dispatcher])
        payload = _make_payload(event_type="jira:comment_created")

        result = await pipeline.process(payload)

        assert result.final_action == StageAction.SIGNALED
        assert result.dropped_at == "dispatcher"

    @pytest.mark.asyncio
    async def test_stage_error_treated_as_pass(self) -> None:
        """A stage that raises is treated as PASS (at-least-once semantics)."""
        error_stage = ErrorStage("dedup")
        loop_guard = FakeStage("loop_guard", StageResult(action=StageAction.PASS))
        dispatcher = FakeStage(
            "dispatcher",
            StageResult(action=StageAction.WORKFLOW_STARTED),
        )

        pipeline = WebhookPipeline(stages=[error_stage, loop_guard, dispatcher])
        payload = _make_payload()

        result = await pipeline.process(payload)

        # Pipeline continued past the error
        assert loop_guard.called
        assert dispatcher.called
        assert result.final_action == StageAction.WORKFLOW_STARTED

    @pytest.mark.asyncio
    async def test_trace_id_propagation(self) -> None:
        """Trace ID from a stage result is propagated to the final result."""
        dedup = FakeStage(
            "dedup",
            StageResult(action=StageAction.PASS, trace_id="trace-from-dedup"),
        )
        loop_guard = FakeStage("loop_guard", StageResult(action=StageAction.PASS))
        dispatcher = FakeStage(
            "dispatcher",
            StageResult(
                action=StageAction.WORKFLOW_STARTED,
                trace_id="trace-from-dispatcher",
            ),
        )

        pipeline = WebhookPipeline(stages=[dedup, loop_guard, dispatcher])
        payload = _make_payload()

        result = await pipeline.process(payload)

        # Last trace_id wins
        assert result.trace_id == "trace-from-dispatcher"

    @pytest.mark.asyncio
    async def test_payload_trace_id_used_when_no_stage_provides(self) -> None:
        """Payload's trace_id is used when no stage overrides it."""
        dedup = FakeStage("dedup", StageResult(action=StageAction.PASS))
        dispatcher = FakeStage(
            "dispatcher",
            StageResult(action=StageAction.WORKFLOW_STARTED),
        )

        pipeline = WebhookPipeline(stages=[dedup, dispatcher])
        payload = _make_payload(trace_id="original-trace")

        result = await pipeline.process(payload)

        assert result.trace_id == "original-trace"

    @pytest.mark.asyncio
    async def test_empty_pipeline(self) -> None:
        """An empty pipeline returns PASS with no stage results."""
        pipeline = WebhookPipeline(stages=[])
        payload = _make_payload()

        result = await pipeline.process(payload)

        assert result.final_action == StageAction.PASS
        assert result.stage_results == []
        assert result.dropped_at is None

    @pytest.mark.asyncio
    async def test_stages_receive_same_payload(self) -> None:
        """All stages receive the same payload object."""
        dedup = FakeStage("dedup", StageResult(action=StageAction.PASS))
        loop_guard = FakeStage("loop_guard", StageResult(action=StageAction.PASS))
        dispatcher = FakeStage(
            "dispatcher",
            StageResult(action=StageAction.WORKFLOW_STARTED),
        )

        pipeline = WebhookPipeline(stages=[dedup, loop_guard, dispatcher])
        payload = _make_payload(issue_key="TEST-456")

        await pipeline.process(payload)

        assert dedup.received_payload is payload
        assert loop_guard.received_payload is payload
        assert dispatcher.received_payload is payload

    def test_stages_property(self) -> None:
        """The stages property returns a copy of the stage list."""
        dedup = FakeStage("dedup", StageResult(action=StageAction.PASS))
        pipeline = WebhookPipeline(stages=[dedup])

        stages = pipeline.stages
        assert len(stages) == 1
        assert stages[0] is dedup

        # Modifying the returned list doesn't affect the pipeline
        stages.append(FakeStage("extra", StageResult(action=StageAction.PASS)))
        assert len(pipeline.stages) == 1


# ---------------------------------------------------------------------------
# Payload extraction tests
# ---------------------------------------------------------------------------


class TestExtractWebhookPayload:
    """Tests for the extract_webhook_payload helper."""

    def test_extracts_from_header(self) -> None:
        """Uses X-Atlassian-Webhook-Identifier when available."""
        raw = {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "PROJ-1", "id": "10001", "fields": {}},
            "user": {"accountId": "user-1"},
        }
        headers = {"x-atlassian-webhook-identifier": "header-id-123"}

        payload = extract_webhook_payload(raw, headers)

        assert payload.event_id == "header-id-123"
        assert payload.event_type == "jira:issue_updated"
        assert payload.issue_key == "PROJ-1"
        assert payload.actor_account_id == "user-1"

    def test_derives_event_id_when_no_header(self) -> None:
        """Derives event_id from payload when header is missing."""
        raw = {
            "webhookEvent": "jira:issue_created",
            "timestamp": 1700000000,
            "issue": {"key": "PROJ-2", "id": "10002", "fields": {}},
        }
        headers: dict[str, str] = {}

        payload = extract_webhook_payload(raw, headers)

        assert payload.event_id is not None
        assert len(payload.event_id) == 64  # SHA-256 hex

    def test_extracts_assignee(self) -> None:
        """Extracts assignee account ID from issue fields."""
        raw = {
            "webhookEvent": "jira:issue_assigned",
            "issue": {
                "key": "PROJ-3",
                "id": "10003",
                "fields": {"assignee": {"accountId": "bot-abc"}},
            },
        }
        headers: dict[str, str] = {}

        payload = extract_webhook_payload(raw, headers)

        assert payload.assignee_account_id == "bot-abc"

    def test_extracts_comment_body(self) -> None:
        """Extracts comment body for comment_created events."""
        raw = {
            "webhookEvent": "jira:comment_created",
            "issue": {"key": "PROJ-4", "id": "10004", "fields": {}},
            "comment": {"body": "Here is the missing info"},
        }
        headers: dict[str, str] = {}

        payload = extract_webhook_payload(raw, headers)

        assert payload.comment_body == "Here is the missing info"

    def test_extracts_trace_id_from_headers(self) -> None:
        """Extracts trace_id from X-Trace-Id header."""
        raw = {
            "webhookEvent": "jira:issue_updated",
            "issue": {"key": "PROJ-5", "id": "10005", "fields": {}},
        }
        headers = {"x-trace-id": "trace-abc-123"}

        payload = extract_webhook_payload(raw, headers)

        assert payload.trace_id == "trace-abc-123"

    def test_handles_missing_fields_gracefully(self) -> None:
        """Handles payloads with missing optional fields."""
        raw = {"webhookEvent": "jira:issue_updated"}
        headers: dict[str, str] = {}

        payload = extract_webhook_payload(raw, headers)

        assert payload.event_type == "jira:issue_updated"
        assert payload.issue_key == ""
        assert payload.actor_account_id is None
        assert payload.assignee_account_id is None
        assert payload.comment_body is None


# ---------------------------------------------------------------------------
# derive_event_id tests
# ---------------------------------------------------------------------------


class TestDeriveEventId:
    """Tests for the _derive_event_id helper."""

    def test_deterministic(self) -> None:
        """Same payload produces same event_id."""
        payload = {
            "webhookEvent": "jira:issue_updated",
            "timestamp": 1700000000,
            "issue": {"id": "10001"},
        }
        id1 = _derive_event_id(payload)
        id2 = _derive_event_id(payload)
        assert id1 == id2

    def test_different_payloads_different_ids(self) -> None:
        """Different payloads produce different event_ids."""
        payload1 = {
            "webhookEvent": "jira:issue_updated",
            "timestamp": 1700000000,
            "issue": {"id": "10001"},
        }
        payload2 = {
            "webhookEvent": "jira:issue_updated",
            "timestamp": 1700000001,
            "issue": {"id": "10001"},
        }
        assert _derive_event_id(payload1) != _derive_event_id(payload2)


# ---------------------------------------------------------------------------
# FastAPI endpoint tests
# ---------------------------------------------------------------------------


class TestPipelineEndpoint:
    """Tests for the POST /webhooks/jira/pipeline endpoint."""

    def _create_app(self, pipeline: WebhookPipeline | None = None) -> FastAPI:
        """Create a test FastAPI app with the pipeline router."""
        app = FastAPI()
        app.include_router(router, prefix="/webhooks")
        if pipeline is not None:
            app.state.webhook_pipeline = pipeline
        return app

    def test_returns_503_when_pipeline_not_configured(self) -> None:
        """Returns 503 when webhook_pipeline is not on app.state."""
        app = self._create_app(pipeline=None)
        client = TestClient(app)

        response = client.post(
            "/webhooks/jira/pipeline",
            json={
                "webhookEvent": "jira:issue_updated",
                "issue": {"key": "PROJ-1", "id": "1", "fields": {}},
            },
        )

        assert response.status_code == 503
        assert response.json()["status"] == "pipeline_not_configured"

    def test_returns_400_for_invalid_json(self) -> None:
        """Returns 400 for non-JSON body."""
        dedup = FakeStage("dedup", StageResult(action=StageAction.PASS))
        pipeline = WebhookPipeline(stages=[dedup])
        app = self._create_app(pipeline=pipeline)
        client = TestClient(app)

        response = client.post(
            "/webhooks/jira/pipeline",
            content=b"not json",
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 400

    def test_returns_400_for_missing_event_type(self) -> None:
        """Returns 400 when webhookEvent is missing."""
        dedup = FakeStage("dedup", StageResult(action=StageAction.PASS))
        pipeline = WebhookPipeline(stages=[dedup])
        app = self._create_app(pipeline=pipeline)
        client = TestClient(app)

        response = client.post(
            "/webhooks/jira/pipeline",
            json={"issue": {"key": "PROJ-1", "id": "1", "fields": {}}},
        )

        assert response.status_code == 400
        assert response.json()["reason"] == "missing_event_type"

    def test_returns_400_for_missing_issue_key(self) -> None:
        """Returns 400 when issue key is missing."""
        dedup = FakeStage("dedup", StageResult(action=StageAction.PASS))
        pipeline = WebhookPipeline(stages=[dedup])
        app = self._create_app(pipeline=pipeline)
        client = TestClient(app)

        response = client.post(
            "/webhooks/jira/pipeline",
            json={"webhookEvent": "jira:issue_updated"},
        )

        assert response.status_code == 400
        assert response.json()["reason"] == "missing_issue_key"

    def test_successful_pipeline_execution(self) -> None:
        """Returns 200 with pipeline result on successful execution."""
        dispatcher = FakeStage(
            "dispatcher",
            StageResult(
                action=StageAction.WORKFLOW_STARTED,
                trace_id="trace-999",
                metadata={"workflow_id": "wf-abc"},
            ),
        )
        pipeline = WebhookPipeline(stages=[dispatcher])
        app = self._create_app(pipeline=pipeline)
        client = TestClient(app)

        response = client.post(
            "/webhooks/jira/pipeline",
            json={
                "webhookEvent": "jira:issue_updated",
                "issue": {"key": "PROJ-1", "id": "1", "fields": {}},
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "workflow_started"
        assert data["trace_id"] == "trace-999"
        assert data["workflow_id"] == "wf-abc"

    def test_dropped_pipeline_returns_200(self) -> None:
        """Returns 200 even when pipeline drops (Atlassian expects 200)."""
        dedup = FakeStage(
            "dedup",
            StageResult(action=StageAction.DROP, reason="duplicate"),
        )
        pipeline = WebhookPipeline(stages=[dedup])
        app = self._create_app(pipeline=pipeline)
        client = TestClient(app)

        response = client.post(
            "/webhooks/jira/pipeline",
            json={
                "webhookEvent": "jira:issue_updated",
                "issue": {"key": "PROJ-1", "id": "1", "fields": {}},
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "drop"
        assert data["reason"] == "duplicate"
        assert data["dropped_at"] == "dedup"
