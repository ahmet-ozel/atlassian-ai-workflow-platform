"""Smoke test for pipeline integration HTTP status mapping.

Validates that the FastAPI endpoint returns:
- 429 when the dispatcher returns DispatchResult with
  reason="concurrency_limit_exceeded"
- 200 for all other drops/passes (Atlassian acknowledge contract)

This pins the HTTP 429 mapping for the concurrency rejection branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.webhooks.pipeline import (
    StageAction,
    StageResult,
    WebhookPayload,
    WebhookPipeline,
    router,
)


@dataclass
class _FakeStage:
    _name: str
    _result: StageResult

    @property
    def name(self) -> str:
        return self._name

    async def check(self, payload: WebhookPayload) -> StageResult:
        return self._result


def _make_app(pipeline: WebhookPipeline) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/webhooks")
    app.state.webhook_pipeline = pipeline
    return TestClient(app)


def test_concurrency_limit_exceeded_returns_429() -> None:
    """``reason="concurrency_limit_exceeded"`` maps to HTTP 429."""

    dispatcher = _FakeStage(
        "dispatcher",
        StageResult(
            action=StageAction.DROP,
            reason="concurrency_limit_exceeded",
            metadata={"dept_id": "payments"},
        ),
    )
    pipeline = WebhookPipeline(stages=[dispatcher])
    client = _make_app(pipeline)

    resp = client.post(
        "/webhooks/jira/pipeline",
        json={
            "webhookEvent": "jira:issue_assigned",
            "issue": {"key": "PAY-1", "id": "1", "fields": {}},
        },
    )

    assert resp.status_code == 429, resp.text
    body = resp.json()
    assert body["status"] == "drop"
    assert body["reason"] == "concurrency_limit_exceeded"
    assert body["dept_id"] == "payments"


def test_normal_drop_returns_200() -> None:
    """Drops with other reasons keep the 200 acknowledge contract."""

    dedup = _FakeStage(
        "dedup",
        StageResult(action=StageAction.DROP, reason="duplicate"),
    )
    pipeline = WebhookPipeline(stages=[dedup])
    client = _make_app(pipeline)

    resp = client.post(
        "/webhooks/jira/pipeline",
        json={
            "webhookEvent": "jira:issue_assigned",
            "issue": {"key": "PAY-1", "id": "1", "fields": {}},
        },
    )

    assert resp.status_code == 200
    assert resp.json()["reason"] == "duplicate"


def test_workflow_started_returns_200() -> None:
    """Successful dispatches return 200 with workflow metadata."""

    dispatcher = _FakeStage(
        "dispatcher",
        StageResult(
            action=StageAction.WORKFLOW_STARTED,
            trace_id="trace-1",
            metadata={"dept_id": "payments"},
        ),
    )
    pipeline = WebhookPipeline(stages=[dispatcher])
    client = _make_app(pipeline)

    resp = client.post(
        "/webhooks/jira/pipeline",
        json={
            "webhookEvent": "jira:issue_assigned",
            "issue": {"key": "PAY-1", "id": "1", "fields": {}},
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "workflow_started"
    assert body["trace_id"] == "trace-1"
