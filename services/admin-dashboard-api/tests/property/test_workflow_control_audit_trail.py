# Feature: platform-gap-fill
# Property 18: Workflow control audit trail
# Validates: Requirements 6.4
"""Property test: Workflow control audit trail (Property 18).

**Property 18: Workflow control audit trail**
**Validates: Requirements 6.4**

*For any* workflow control action (``cancel`` / ``retry`` / ``signal``),
the :mod:`src.routers.workflow_control` router SHALL emit **exactly one**
``workflow_control`` audit event whose ``payload.action_kind`` matches
the action that was attempted and whose ``result`` records the outcome:

- ``"ok"``     when the underlying Temporal call succeeds.
- ``"denied"`` when the workflow does not exist (``WorkflowNotFoundError``
  surfaces the request as ``HTTP 404``; the audit row records the denial).
- ``"error"``  when Temporal reports a generic
  :class:`WorkflowControlError` (``HTTP 502``).

Per Requirement 6.4 ("WHEN herhangi bir workflow control aksiyonu
gerçekleştirildiğinde, THE Admin_Dashboard_API SHALL audit log'a
``workflow_control`` event'i ... yazmalıdır") **every** mutating call
must produce a single audit row, including the denial / error paths.

Strategy
--------
Hypothesis generates random ``(action, outcome, workflow_id, …)``
quadruples and drives the corresponding endpoint through
:class:`fastapi.testclient.TestClient`. The :class:`_RecordingAuditSink`
captures every emitted :class:`AuditEvent`, and each test asserts:

1. Exactly **one** ``workflow_control`` event is recorded.
2. ``event.action == "workflow_control"``.
3. ``event.payload["action_kind"]`` equals the requested action
   (``"cancel"`` / ``"retry"`` / ``"signal"``).
4. ``event.result`` equals the expected outcome label
   (``"ok"`` / ``"denied"`` / ``"error"``).
5. ``event.resource`` references the requested workflow id.
6. ``event.actor_role == "admin"`` and ``event.actor_id`` matches the
   ``require_admin`` override.

The test is self-contained: it does **not** depend on the real Temporal
SDK, only on the :class:`SupportsTemporalControl` protocol the router
declares.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap (mirror the pattern other tests in this package use).
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
    router as workflow_control_router,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The audit ``action`` label used by the router for every control event.
_AUDIT_ACTION: str = "workflow_control"

#: The three control actions the router exposes.
Action = Literal["cancel", "retry", "signal"]

#: The three audit outcomes the router records.
Outcome = Literal["ok", "denied", "error"]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTemporalControl:
    """Minimal :class:`SupportsTemporalControl` stub.

    The describe / mutating call results are scripted by setting
    ``describe_result``, ``cancel_result``, ``signal_result``, and
    ``restart_result``. Any value that is an :class:`Exception` is
    re-raised; otherwise it is returned as-is. Calls are recorded so
    the test can verify the router actually invoked the underlying RPC
    on the success path (and skipped it on the denied / error paths).
    """

    def __init__(self) -> None:
        self.describe_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.signal_calls: list[tuple[str, str, Any]] = []
        self.restart_calls: list[str] = []

        # Default: every workflow is found and every mutation succeeds.
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
            new_workflow_id="auto-retry-1",
            workflow_type="AutomationWorkflow",
            run_id="run-2",
        )

    async def get_workflow_description(
        self, workflow_id: str
    ) -> WorkflowDescription:
        self.describe_calls.append(workflow_id)
        if isinstance(self.describe_result, Exception):
            raise self.describe_result
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
    ) -> WorkflowPage:  # pragma: no cover - not exercised here
        return WorkflowPage(items=[], page=page, page_size=page_size)


@dataclass
class _RecordingAuditSink:
    """Audit sink that captures every event for assertions."""

    events: list[AuditEvent] = field(default_factory=list)

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


_ACTOR_SUB = "admin-prop-test"


def _build_app(
    *,
    temporal: _FakeTemporalControl,
    audit_sink: _RecordingAuditSink,
) -> FastAPI:
    """Build a minimal FastAPI app wired to the workflow_control router."""

    app = FastAPI()
    app.include_router(workflow_control_router)
    app.state.temporal_workflow_client = temporal
    app.state.workflow_control_audit_sink = audit_sink
    app.state.admin_proxy = None

    app.dependency_overrides[require_admin] = lambda: AuthClaims(
        sub=_ACTOR_SUB,
        groups=("admin",),
    )
    return app


def _configure_outcome(
    temporal: _FakeTemporalControl,
    *,
    action: Action,
    outcome: Outcome,
) -> None:
    """Wire ``temporal`` to deliver ``outcome`` for ``action``.

    The router checks workflow existence before each mutation; the
    ``"denied"`` outcome is therefore plumbed via ``describe_result``
    (raises :class:`WorkflowNotFoundError`), and the ``"error"`` outcome
    is plumbed via the matching ``*_result`` so we exercise the
    *post-describe* failure path. Both shapes hit the same audit
    branches in the router.
    """

    if outcome == "ok":
        return  # defaults already represent the success path

    if outcome == "denied":
        temporal.describe_result = WorkflowNotFoundError("missing-wf")
        return

    # outcome == "error"
    if action == "cancel":
        temporal.cancel_result = WorkflowControlError("rpc unavailable")
    elif action == "retry":
        temporal.restart_result = WorkflowControlError("rpc unavailable")
    else:  # action == "signal"
        temporal.signal_result = WorkflowControlError("rpc unavailable")


def _expected_http_status(*, action: Action, outcome: Outcome) -> int:
    """Return the HTTP status the router should produce.

    * ``"ok"``     → 200
    * ``"denied"`` → 404 (``WorkflowNotFoundError`` from describe)
    * ``"error"``  → for ``cancel`` / ``signal`` the *describe* succeeds
      and the mutation raises :class:`WorkflowControlError`, which the
      endpoint maps to 502. For ``retry`` the same shape applies.
    """

    if outcome == "ok":
        return 200
    if outcome == "denied":
        return 404
    return 502


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Workflow ids: short Jira-flavoured strings — enough variety to shrink
#: into a meaningful counterexample without spending all our entropy on
#: the id itself.
_WORKFLOW_ID_STRATEGY = st.from_regex(
    r"auto-[A-Z]{2,6}-[0-9]{1,4}",
    fullmatch=True,
)

#: Signal names: 1-30 chars matching the router's Pydantic validator
#: (``min_length=1, max_length=200``). We keep the upper bound shorter
#: so individual examples stay readable in the Hypothesis output.
_SIGNAL_NAME_STRATEGY = st.from_regex(
    r"[A-Za-z][A-Za-z0-9_]{0,29}",
    fullmatch=True,
)

#: Free-form JSON payload — exercises the ``payload: Any`` field on
#: ``SignalRequest`` without bias toward any single shape.
_SIGNAL_PAYLOAD_STRATEGY = st.recursive(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-1_000, max_value=1_000),
        st.text(max_size=20),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=4),
    ),
    max_leaves=8,
)

_ACTION_STRATEGY: st.SearchStrategy[Action] = st.sampled_from(
    ["cancel", "retry", "signal"]
)
_OUTCOME_STRATEGY: st.SearchStrategy[Outcome] = st.sampled_from(
    ["ok", "denied", "error"]
)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _invoke_action(
    client: TestClient,
    *,
    action: Action,
    workflow_id: str,
    signal_name: str,
    signal_payload: Any,
) -> int:
    """Invoke the endpoint for ``action`` and return the HTTP status."""

    if action == "cancel":
        return client.post(f"/api/v1/workflows/{workflow_id}/cancel").status_code
    if action == "retry":
        return client.post(f"/api/v1/workflows/{workflow_id}/retry").status_code
    return client.post(
        f"/api/v1/workflows/{workflow_id}/signal",
        json={"signal_name": signal_name, "payload": signal_payload},
    ).status_code


def _assert_audit_invariants(
    sink: _RecordingAuditSink,
    *,
    action: Action,
    outcome: Outcome,
    workflow_id: str,
) -> None:
    """Assert the audit-trail invariants for one control action.

    * Exactly one event was emitted.
    * ``action == "workflow_control"``.
    * ``payload["action_kind"]`` matches ``action``.
    * ``result`` matches ``outcome``.
    * ``resource`` references ``workflow_id``.
    * ``actor_role == "admin"`` and ``actor_id`` matches the override.
    """

    assert len(sink.events) == 1, (
        f"expected exactly one workflow_control audit event for "
        f"action={action!r} outcome={outcome!r} workflow_id={workflow_id!r}, "
        f"got {len(sink.events)}: {sink.events!r}"
    )

    event = sink.events[0]

    assert event.action == _AUDIT_ACTION, (
        f"audit event.action must be {_AUDIT_ACTION!r}; got {event.action!r}"
    )

    payload = event.payload or {}
    assert payload.get("action_kind") == action, (
        f"audit payload.action_kind must be {action!r}; "
        f"got {payload.get('action_kind')!r}. Full payload: {payload!r}"
    )

    assert event.result == outcome, (
        f"audit event.result must be {outcome!r} for outcome={outcome!r}; "
        f"got {event.result!r}. Payload: {payload!r}"
    )

    assert event.resource == f"workflow:{workflow_id}", (
        f"audit event.resource must reference workflow_id={workflow_id!r}; "
        f"got {event.resource!r}"
    )

    assert event.actor_role == "admin", (
        f"audit event.actor_role must be 'admin'; got {event.actor_role!r}"
    )
    assert event.actor_id == _ACTOR_SUB, (
        f"audit event.actor_id must be {_ACTOR_SUB!r}; got {event.actor_id!r}"
    )

    # The audit row must reference the same workflow_id inside the payload —
    # the router builds the payload from ``{"action_kind", "workflow_id"}``
    # so this is guaranteed by the implementation, but the property is
    # part of the contract so we assert it explicitly.
    assert payload.get("workflow_id") == workflow_id, (
        f"audit payload.workflow_id must be {workflow_id!r}; "
        f"got {payload.get('workflow_id')!r}"
    )


# ---------------------------------------------------------------------------
# Property 18 — exactly one workflow_control audit event per action
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    action=_ACTION_STRATEGY,
    outcome=_OUTCOME_STRATEGY,
    workflow_id=_WORKFLOW_ID_STRATEGY,
    signal_name=_SIGNAL_NAME_STRATEGY,
    signal_payload=_SIGNAL_PAYLOAD_STRATEGY,
)
def test_every_control_action_emits_one_audit_event(
    action: Action,
    outcome: Outcome,
    workflow_id: str,
    signal_name: str,
    signal_payload: Any,
) -> None:
    """Property 18 — every control action emits exactly one audit event.

    **Validates: Requirements 6.4**

    For any random ``(action, outcome, workflow_id, …)`` tuple, the
    router writes exactly one ``workflow_control`` audit event whose
    ``action_kind`` and ``result`` match the requested action and the
    observed outcome. Holds for all three actions and all three
    outcomes — including the denied path (404) and the error path
    (502) where the underlying Temporal mutation never executes.
    """

    temporal = _FakeTemporalControl()
    _configure_outcome(temporal, action=action, outcome=outcome)

    audit_sink = _RecordingAuditSink()
    app = _build_app(temporal=temporal, audit_sink=audit_sink)
    client = TestClient(app)

    status_code = _invoke_action(
        client,
        action=action,
        workflow_id=workflow_id,
        signal_name=signal_name,
        signal_payload=signal_payload,
    )

    expected_status = _expected_http_status(action=action, outcome=outcome)
    assert status_code == expected_status, (
        f"unexpected HTTP status for action={action!r} outcome={outcome!r}: "
        f"expected {expected_status}, got {status_code}"
    )

    _assert_audit_invariants(
        audit_sink,
        action=action,
        outcome=outcome,
        workflow_id=workflow_id,
    )


# ---------------------------------------------------------------------------
# Property 18a — happy-path action_kind / result mapping is total
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    action=_ACTION_STRATEGY,
    workflow_id=_WORKFLOW_ID_STRATEGY,
    signal_name=_SIGNAL_NAME_STRATEGY,
    signal_payload=_SIGNAL_PAYLOAD_STRATEGY,
)
def test_happy_path_audit_records_action_kind_and_ok_result(
    action: Action,
    workflow_id: str,
    signal_name: str,
    signal_payload: Any,
) -> None:
    """Property 18a — happy-path action_kind / result mapping is total.

    **Validates: Requirements 6.4**

    For every successful invocation the audit row carries
    ``payload.action_kind == action`` and ``result == "ok"``. The
    router must not collapse ``cancel`` / ``retry`` / ``signal`` onto
    the same label or omit the success row.
    """

    temporal = _FakeTemporalControl()
    audit_sink = _RecordingAuditSink()
    app = _build_app(temporal=temporal, audit_sink=audit_sink)
    client = TestClient(app)

    status_code = _invoke_action(
        client,
        action=action,
        workflow_id=workflow_id,
        signal_name=signal_name,
        signal_payload=signal_payload,
    )

    assert status_code == 200, (
        f"happy-path action={action!r} must return 200; got {status_code}"
    )
    _assert_audit_invariants(
        audit_sink,
        action=action,
        outcome="ok",
        workflow_id=workflow_id,
    )


# ---------------------------------------------------------------------------
# Property 18b — denied path always emits one denied event (no double-write)
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    action=_ACTION_STRATEGY,
    workflow_id=_WORKFLOW_ID_STRATEGY,
    signal_name=_SIGNAL_NAME_STRATEGY,
    signal_payload=_SIGNAL_PAYLOAD_STRATEGY,
)
def test_denied_path_emits_single_denied_event(
    action: Action,
    workflow_id: str,
    signal_name: str,
    signal_payload: Any,
) -> None:
    """Property 18b — denied path emits exactly one ``denied`` audit event.

    **Validates: Requirements 6.4**

    When Temporal reports the workflow does not exist, the router must
    write **one** audit event with ``result="denied"`` (never two,
    never zero) and never invoke the mutation. This is the property
    that prevents an attacker from flooding the audit log with denied
    rows by retrying against a missing workflow.
    """

    temporal = _FakeTemporalControl()
    temporal.describe_result = WorkflowNotFoundError(workflow_id)

    audit_sink = _RecordingAuditSink()
    app = _build_app(temporal=temporal, audit_sink=audit_sink)
    client = TestClient(app)

    status_code = _invoke_action(
        client,
        action=action,
        workflow_id=workflow_id,
        signal_name=signal_name,
        signal_payload=signal_payload,
    )

    assert status_code == 404
    # The mutation must NOT have run on the denied path.
    assert temporal.cancel_calls == []
    assert temporal.restart_calls == []
    assert temporal.signal_calls == []

    _assert_audit_invariants(
        audit_sink,
        action=action,
        outcome="denied",
        workflow_id=workflow_id,
    )
