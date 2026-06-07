"""Unit tests for ``src.routers.workflow_control``.

The router exposes admin-only Temporal control endpoints:

* ``POST /api/v1/workflows/{workflow_id}/cancel``
* ``POST /api/v1/workflows/{workflow_id}/retry``
* ``POST /api/v1/workflows/{workflow_id}/signal``
* ``GET  /api/v1/workflows``

These tests inject:

* A :class:`_FakeTemporalControl` that records every call and lets each
  test script the response (success / WorkflowNotFoundError /
  WorkflowControlError).
* A :class:`_RecordingAuditSink` so we can assert each mutating action
  emits exactly one ``workflow_control`` audit event with the right
  ``action_kind`` / ``result`` / ``payload``.
* An override on :func:`require_admin` so the OIDC layer can be bypassed
  while still exercising the FastAPI request pipeline through
  :class:`fastapi.testclient.TestClient`.

The tests do not depend on the ``temporalio`` SDK - the router is wired
against the :class:`SupportsTemporalControl` protocol so a tiny in-memory
stub is enough.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# sys.path bootstrap (mirror the pattern other tests use).
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for _lib in ("audit_logger", "auth-shared", "http-shared"):
    _src = _WORKSPACE_ROOT / "libs" / _lib / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))


from audit_logger import AuditEvent  # noqa: E402

from src.auth.dependencies import AuthClaims, require_admin  # noqa: E402
from src.routers.workflow_control import (  # noqa: E402
    RestartedWorkflow,
    WorkflowControlError,
    WorkflowDescription,
    WorkflowNotFoundError,
    WorkflowPage,
    WorkflowSummary,
    router as workflow_control_router,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTemporalControl:
    """In-memory :class:`SupportsTemporalControl` stub.

    Tests configure the per-method behaviour by setting the
    ``describe_*``, ``cancel_*``, ``signal_*``, ``restart_*``,
    ``list_*`` attributes. Every call is recorded on the matching
    ``calls`` list so assertions can verify what the router did.
    """

    def __init__(self) -> None:
        self.describe_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.signal_calls: list[tuple[str, str, Any]] = []
        self.restart_calls: list[str] = []
        self.list_calls: list[dict[str, Any]] = []

        # Default: every workflow exists with status="running".
        self.describe_result: WorkflowDescription | Exception = WorkflowDescription(
            workflow_id="<unset>",
            workflow_type="AutomationWorkflow",
            task_queue="agent-runner",
            status="running",
            dept_id="ops",
        )
        self.cancel_result: None | Exception = None
        self.signal_result: None | Exception = None
        self.restart_result: RestartedWorkflow | Exception = RestartedWorkflow(
            new_workflow_id="auto-PAY-1-retry-1",
            workflow_type="AutomationWorkflow",
            run_id="run-2",
        )
        self.list_result: WorkflowPage | Exception = WorkflowPage(
            items=[], page=1, page_size=50, next_page_token=None
        )

    async def get_workflow_description(
        self, workflow_id: str
    ) -> WorkflowDescription:
        self.describe_calls.append(workflow_id)
        if isinstance(self.describe_result, Exception):
            raise self.describe_result
        # Substitute the requested workflow_id so the stub behaves
        # consistently across calls without per-test customisation.
        base = self.describe_result
        return WorkflowDescription(
            workflow_id=workflow_id,
            workflow_type=base.workflow_type,
            task_queue=base.task_queue,
            status=base.status,
            dept_id=base.dept_id,
            started_at=base.started_at,
            closed_at=base.closed_at,
        )

    async def cancel_workflow(self, workflow_id: str) -> None:
        self.cancel_calls.append(workflow_id)
        if isinstance(self.cancel_result, Exception):
            raise self.cancel_result

    async def signal_workflow(
        self, workflow_id: str, signal_name: str, payload: Any
    ) -> None:
        self.signal_calls.append((workflow_id, signal_name, payload))
        if isinstance(self.signal_result, Exception):
            raise self.signal_result

    async def restart_workflow(self, workflow_id: str) -> RestartedWorkflow:
        self.restart_calls.append(workflow_id)
        if isinstance(self.restart_result, Exception):
            raise self.restart_result
        return self.restart_result

    async def list_workflows(
        self,
        *,
        dept_id: str | None,
        wf_status: str | None,
        page: int,
        page_size: int,
        page_token: str | None,
    ) -> WorkflowPage:
        self.list_calls.append(
            {
                "dept_id": dept_id,
                "wf_status": wf_status,
                "page": page,
                "page_size": page_size,
                "page_token": page_token,
            }
        )
        if isinstance(self.list_result, Exception):
            raise self.list_result
        return self.list_result


class _RecordingAuditSink:
    """Audit sink that records every event for assertions."""

    def __init__(self, *, raise_on_write: bool = False) -> None:
        self.events: list[AuditEvent] = []
        self.raise_on_write = raise_on_write

    async def write(self, event: AuditEvent) -> None:
        if self.raise_on_write:
            raise RuntimeError("simulated audit failure")
        self.events.append(event)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(
    *,
    temporal: _FakeTemporalControl | None,
    audit_sink: _RecordingAuditSink | None = None,
    actor_sub: str = "admin-user-1",
) -> FastAPI:
    """Build a minimal FastAPI app wired to the workflow_control router."""

    app = FastAPI()
    app.include_router(workflow_control_router)
    app.state.temporal_workflow_client = temporal
    app.state.workflow_control_audit_sink = audit_sink
    app.state.admin_proxy = None

    app.dependency_overrides[require_admin] = lambda: AuthClaims(
        sub=actor_sub,
        groups=("admin",),
    )
    return app


def _audit_actions(sink: _RecordingAuditSink) -> list[tuple[str, str, str]]:
    """Return ``[(action, action_kind, result), ...]`` from the sink."""

    out: list[tuple[str, str, str]] = []
    for ev in sink.events:
        kind = (ev.payload or {}).get("action_kind", "")
        out.append((ev.action, kind, ev.result))
    return out


# ---------------------------------------------------------------------------
# Cancel endpoint
# ---------------------------------------------------------------------------


def test_cancel_workflow_happy_path_emits_audit_and_calls_temporal() -> None:
    """``POST /cancel`` happy path: 200, audit row, Temporal call."""

    temporal = _FakeTemporalControl()
    audit = _RecordingAuditSink()
    app = _build_app(temporal=temporal, audit_sink=audit)
    client = TestClient(app)

    response = client.post("/api/v1/workflows/auto-PAY-1/cancel")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["workflow_id"] == "auto-PAY-1"

    # Temporal got the cancel call.
    assert temporal.cancel_calls == ["auto-PAY-1"]

    # Exactly one audit event was emitted, with the right shape.
    actions = _audit_actions(audit)
    assert actions == [("workflow_control", "cancel", "ok")]
    ev = audit.events[0]
    assert ev.actor_id == "admin-user-1"
    assert ev.actor_role == "admin"
    assert ev.resource == "workflow:auto-PAY-1"
    assert ev.payload == {
        "action_kind": "cancel",
        "workflow_id": "auto-PAY-1",
    }


def test_cancel_workflow_returns_404_when_not_found() -> None:
    """``WorkflowNotFoundError`` from describe  404 with stable detail."""

    temporal = _FakeTemporalControl()
    temporal.describe_result = WorkflowNotFoundError("auto-PAY-missing")
    audit = _RecordingAuditSink()
    app = _build_app(temporal=temporal, audit_sink=audit)
    client = TestClient(app)

    response = client.post("/api/v1/workflows/auto-PAY-missing/cancel")

    assert response.status_code == 404, response.text
    assert response.json() == {"detail": "workflow_not_found"}

    # No cancel call happened.
    assert temporal.cancel_calls == []

    # Audit recorded the denied attempt.
    actions = _audit_actions(audit)
    assert actions == [("workflow_control", "cancel", "denied")]
    assert (audit.events[0].payload or {}).get("reason") == "workflow_not_found"


def test_cancel_workflow_returns_502_on_temporal_failure() -> None:
    """Generic ``WorkflowControlError`` from describe  502."""

    temporal = _FakeTemporalControl()
    temporal.describe_result = WorkflowControlError("rpc unavailable")
    app = _build_app(temporal=temporal, audit_sink=_RecordingAuditSink())
    client = TestClient(app)

    response = client.post("/api/v1/workflows/auto-1/cancel")

    assert response.status_code == 502, response.text
    body = response.json()
    assert body["detail"]["error"] == "temporal_rpc_failed"
    assert body["detail"]["workflow_id"] == "auto-1"


def test_cancel_workflow_returns_503_when_temporal_not_wired() -> None:
    """``temporal_workflow_client=None``  503 with clear reason."""

    app = _build_app(temporal=None, audit_sink=_RecordingAuditSink())
    client = TestClient(app)

    response = client.post("/api/v1/workflows/auto-1/cancel")

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["reason"] == "temporal_unavailable"


def test_cancel_workflow_audit_failure_does_not_block_request() -> None:
    """Audit sink raising must not turn a 200 into a 500.

    The endpoint only requires that an audit row be **written**; it
    does not abort the cancel on audit failure.
    """

    temporal = _FakeTemporalControl()
    audit = _RecordingAuditSink(raise_on_write=True)
    app = _build_app(temporal=temporal, audit_sink=audit)
    client = TestClient(app)

    response = client.post("/api/v1/workflows/auto-1/cancel")

    assert response.status_code == 200
    assert temporal.cancel_calls == ["auto-1"]


# ---------------------------------------------------------------------------
# Retry endpoint
# ---------------------------------------------------------------------------


def test_retry_workflow_happy_path_returns_new_workflow_id() -> None:
    """``POST /retry`` returns the new workflow id from ``RestartedWorkflow``."""

    temporal = _FakeTemporalControl()
    temporal.restart_result = RestartedWorkflow(
        new_workflow_id="auto-PAY-1-retry-3",
        workflow_type="AutomationWorkflow",
        run_id="run-9",
    )
    audit = _RecordingAuditSink()
    app = _build_app(temporal=temporal, audit_sink=audit)
    client = TestClient(app)

    response = client.post("/api/v1/workflows/auto-PAY-1/retry")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "status": "restarted",
        "workflow_id": "auto-PAY-1",
        "new_workflow_id": "auto-PAY-1-retry-3",
        "workflow_type": "AutomationWorkflow",
        "run_id": "run-9",
    }
    assert temporal.restart_calls == ["auto-PAY-1"]

    actions = _audit_actions(audit)
    assert actions == [("workflow_control", "retry", "ok")]


def test_retry_workflow_returns_404_when_unknown() -> None:
    temporal = _FakeTemporalControl()
    temporal.describe_result = WorkflowNotFoundError("auto-missing")
    app = _build_app(temporal=temporal, audit_sink=_RecordingAuditSink())
    client = TestClient(app)

    response = client.post("/api/v1/workflows/auto-missing/retry")

    assert response.status_code == 404
    assert response.json() == {"detail": "workflow_not_found"}
    assert temporal.restart_calls == []


# ---------------------------------------------------------------------------
# Signal endpoint
# ---------------------------------------------------------------------------


def test_signal_workflow_delivers_payload_and_audits_signal_name() -> None:
    """``POST /signal`` delivers the payload and audits the signal name."""

    temporal = _FakeTemporalControl()
    audit = _RecordingAuditSink()
    app = _build_app(temporal=temporal, audit_sink=audit)
    client = TestClient(app)

    response = client.post(
        "/api/v1/workflows/auto-PAY-1/signal",
        json={
            "signal_name": "info_received",
            "payload": {"comment": "lgtm", "author": "U-1"},
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {
        "status": "signalled",
        "workflow_id": "auto-PAY-1",
        "signal_name": "info_received",
    }

    # Temporal received the full payload.
    assert temporal.signal_calls == [
        ("auto-PAY-1", "info_received", {"comment": "lgtm", "author": "U-1"})
    ]

    # Audit row carries the signal name but **not** the payload itself
    # (privacy by default - the signal body may carry sensitive data).
    actions = _audit_actions(audit)
    assert actions == [("workflow_control", "signal", "ok")]
    payload = audit.events[0].payload or {}
    assert payload.get("signal_name") == "info_received"
    assert "payload" not in payload
    assert "comment" not in payload


def test_signal_workflow_rejects_empty_signal_name() -> None:
    """Empty ``signal_name``  422 from Pydantic validation."""

    temporal = _FakeTemporalControl()
    app = _build_app(temporal=temporal, audit_sink=_RecordingAuditSink())
    client = TestClient(app)

    response = client.post(
        "/api/v1/workflows/auto-1/signal",
        json={"signal_name": "", "payload": None},
    )

    assert response.status_code == 422
    assert temporal.signal_calls == []


def test_signal_workflow_returns_404_when_unknown() -> None:
    temporal = _FakeTemporalControl()
    temporal.describe_result = WorkflowNotFoundError("auto-missing")
    audit = _RecordingAuditSink()
    app = _build_app(temporal=temporal, audit_sink=audit)
    client = TestClient(app)

    response = client.post(
        "/api/v1/workflows/auto-missing/signal",
        json={"signal_name": "info_received", "payload": "hi"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "workflow_not_found"}
    assert temporal.signal_calls == []
    actions = _audit_actions(audit)
    assert actions == [("workflow_control", "signal", "denied")]


# ---------------------------------------------------------------------------
# List endpoint
# ---------------------------------------------------------------------------


def _summary(workflow_id: str, *, status: str = "running") -> WorkflowSummary:
    return WorkflowSummary(
        workflow_id=workflow_id,
        workflow_type="AutomationWorkflow",
        status=status,
        dept_id="ops",
        started_at=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
        closed_at=None,
    )


def test_list_workflows_returns_serialised_summaries() -> None:
    temporal = _FakeTemporalControl()
    temporal.list_result = WorkflowPage(
        items=[_summary("auto-1"), _summary("auto-2", status="failed")],
        page=1,
        page_size=50,
        next_page_token="cursor-page-2",
    )
    app = _build_app(temporal=temporal, audit_sink=_RecordingAuditSink())
    client = TestClient(app)

    response = client.get(
        "/api/v1/workflows", params={"dept_id": "ops", "status": "running"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 50
    assert body["next_page_token"] == "cursor-page-2"
    assert body["filters"] == {"dept_id": "ops", "status": "running"}
    assert len(body["items"]) == 2
    assert body["items"][0]["workflow_id"] == "auto-1"
    assert body["items"][1]["status"] == "failed"

    # Filters were forwarded.
    assert temporal.list_calls == [
        {
            "dept_id": "ops",
            "wf_status": "running",
            "page": 1,
            "page_size": 50,
            "page_token": None,
        }
    ]


def test_list_workflows_caps_page_size_at_50() -> None:
    """Max 50 entries per page."""

    temporal = _FakeTemporalControl()
    app = _build_app(temporal=temporal, audit_sink=_RecordingAuditSink())
    client = TestClient(app)

    # Page sizes above 50 must be rejected by the Query() validation.
    response = client.get("/api/v1/workflows", params={"page_size": 200})
    assert response.status_code == 422

    # Page size = 50 is the documented cap and must be accepted.
    response = client.get("/api/v1/workflows", params={"page_size": 50})
    assert response.status_code == 200
    assert temporal.list_calls[-1]["page_size"] == 50


def test_list_workflows_rejects_zero_page() -> None:
    """``page=0`` is invalid (1-based)."""

    temporal = _FakeTemporalControl()
    app = _build_app(temporal=temporal, audit_sink=_RecordingAuditSink())
    client = TestClient(app)

    response = client.get("/api/v1/workflows", params={"page": 0})
    assert response.status_code == 422


def test_list_workflows_returns_503_when_temporal_missing() -> None:
    app = _build_app(temporal=None, audit_sink=_RecordingAuditSink())
    client = TestClient(app)

    response = client.get("/api/v1/workflows")
    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "temporal_unavailable"


# ---------------------------------------------------------------------------
# Admin role enforcement
# ---------------------------------------------------------------------------


def test_endpoints_require_admin_role() -> None:
    """Without the dependency override every route returns 401.

    The default ``require_admin`` dependency reads the
    ``Authorization`` header, which the TestClient does not provide,
    so we get the bearer-token-missing 401 - proof that the dependency
    is actually wired.
    """

    app = FastAPI()
    app.include_router(workflow_control_router)
    app.state.temporal_workflow_client = _FakeTemporalControl()
    app.state.workflow_control_audit_sink = _RecordingAuditSink()
    app.state.admin_proxy = None
    # Intentionally no ``dependency_overrides`` so the real
    # ``require_admin`` runs.

    client = TestClient(app)

    for method, path, payload in [
        ("POST", "/api/v1/workflows/auto-1/cancel", None),
        ("POST", "/api/v1/workflows/auto-1/retry", None),
        (
            "POST",
            "/api/v1/workflows/auto-1/signal",
            {"signal_name": "x", "payload": None},
        ),
        ("GET", "/api/v1/workflows", None),
    ]:
        if method == "POST":
            response = client.post(path, json=payload)
        else:
            response = client.get(path)
        assert response.status_code == 401, (
            f"{method} {path} should require admin auth; got "
            f"{response.status_code}: {response.text}"
        )
