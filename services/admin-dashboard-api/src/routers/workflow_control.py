"""``WorkflowControlRouter``.

Admin-only Temporal workflow control surface. Lets a platform admin
cancel a running workflow, retry a failed workflow with the same input,
deliver an arbitrary signal, and list workflows with department / status
filtering and pagination.

Endpoints
---------

* ``POST /api/v1/workflows/{workflow_id}/cancel`` — Temporal cancel
  signal.
* ``POST /api/v1/workflows/{workflow_id}/retry`` — start a new workflow
  with the original ``workflow_type``, ``task_queue``, and input args
  read from the failed workflow's history.
* ``POST /api/v1/workflows/{workflow_id}/signal`` — send a named signal
  (``signal_name`` + ``payload``) to the workflow.
* ``GET /api/v1/workflows`` — list workflows with optional ``dept_id``
  / ``status`` filters and a ``page`` cursor; capped at 50 entries per
  page.

All endpoints are gated by :func:`require_admin`.

Every mutating action emits one ``workflow_control`` audit event
through the app's audit sink **before** the action is executed.
Audit failures are swallowed so a Postgres hiccup
cannot block a legitimate cancel / retry / signal — but the event
shape mirrors the ``automation.audit_events`` row layout so the
foundation audit pipeline can ingest the same envelope.

Temporal access
---------------

The router resolves its Temporal entry point through
``request.app.state.temporal_workflow_client`` — an object that
implements :class:`SupportsTemporalControl` (described below). When
the slot is ``None`` (Temporal unreachable, lifespan wiring still in
flight) the endpoints return ``HTTP 503`` with
``reason="temporal_unavailable"`` so the FE renders a clear "service
not ready" state instead of a stack trace.

The :class:`SupportsTemporalControl` protocol mirrors the small set
of operations this router needs:

* ``get_workflow_description(workflow_id)`` → ``WorkflowDescription`` —
  raises :class:`WorkflowNotFoundError` when the workflow is unknown.
* ``cancel_workflow(workflow_id)`` — issue a Temporal cancel signal.
* ``signal_workflow(workflow_id, name, payload)`` — deliver an
  arbitrary signal.
* ``restart_workflow(workflow_id)`` → ``RestartedWorkflow`` — read
  the original input from history and start a new workflow with the
  same ``workflow_type`` / ``task_queue`` / args.
* ``list_workflows(dept_id, status, page, page_size, page_token)`` →
  ``WorkflowPage`` — paginated visibility query.

The protocol is intentionally narrow so unit tests can ship a tiny
in-memory stub without depending on the ``temporalio`` SDK.

404 semantics
-------------

The cancel / retry / signal endpoints map :class:`WorkflowNotFoundError`
(raised by the implementation when Temporal returns ``NOT_FOUND``) to
``HTTP 404`` with body ``{"detail": "workflow_not_found"}``
Any other :class:`WorkflowControlError` is mapped
to ``HTTP 502`` so a Temporal RPC failure does not look like a missing
workflow to the FE.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from audit_logger import AuditEvent

from ..auth.dependencies import AuthClaims, require_admin

__all__ = [
    "router",
    "SupportsTemporalControl",
    "WorkflowControlError",
    "WorkflowNotFoundError",
    "WorkflowDescription",
    "WorkflowSummary",
    "WorkflowPage",
    "RestartedWorkflow",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Maximum number of workflow rows returned per page.
_MAX_PAGE_SIZE: int = 50

#: Default page size when the caller does not supply ``page_size``.
_DEFAULT_PAGE_SIZE: int = 50

#: Audit event action label written for every mutating action
#: (``workflow_control``).
_AUDIT_ACTION: str = "workflow_control"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkflowControlError(Exception):
    """Base class for control-plane errors raised by the implementation.

    The router maps any :class:`WorkflowControlError` (other than
    :class:`WorkflowNotFoundError`) to ``HTTP 502`` so transient
    Temporal RPC failures do not look like missing workflows to the
    FE.
    """


class WorkflowNotFoundError(WorkflowControlError):
    """Raised by the implementation when Temporal reports ``NOT_FOUND``.

    The router maps this to ``HTTP 404`` with body
    ``{"detail": "workflow_not_found"}``. Implementations
    should wrap the underlying ``RPCError(NOT_FOUND)`` from the
    ``temporalio`` SDK with this exception so the router stays
    SDK-agnostic.
    """

    def __init__(self, workflow_id: str) -> None:
        super().__init__(f"workflow_not_found: {workflow_id!r}")
        self.workflow_id = workflow_id


# ---------------------------------------------------------------------------
# Data classes returned by the implementation protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowDescription:
    """Subset of Temporal ``WorkflowExecutionDescription`` fields.

    Returned by :meth:`SupportsTemporalControl.get_workflow_description`.
    Used by the router to perform existence checks (404 mapping) and
    to surface basic metadata in the list endpoint.
    """

    workflow_id: str
    workflow_type: str
    task_queue: str
    status: str
    dept_id: str | None = None
    started_at: datetime | None = None
    closed_at: datetime | None = None


@dataclass(frozen=True)
class WorkflowSummary:
    """Minimal workflow record returned by the list endpoint."""

    workflow_id: str
    workflow_type: str
    status: str
    dept_id: str | None
    started_at: datetime | None
    closed_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""

        return {
            "workflow_id": self.workflow_id,
            "workflow_type": self.workflow_type,
            "status": self.status,
            "dept_id": self.dept_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }


@dataclass(frozen=True)
class WorkflowPage:
    """One page of workflow summaries plus an optional next-page cursor."""

    items: list[WorkflowSummary]
    page: int
    page_size: int
    next_page_token: str | None = None


@dataclass(frozen=True)
class RestartedWorkflow:
    """Result of a :meth:`SupportsTemporalControl.restart_workflow` call."""

    new_workflow_id: str
    workflow_type: str
    run_id: str | None = None


# ---------------------------------------------------------------------------
# Protocol (the router's only contract with Temporal)
# ---------------------------------------------------------------------------


@runtime_checkable
class SupportsTemporalControl(Protocol):
    """Narrow Temporal control-plane surface consumed by this router.

    Production wires :class:`SupportsTemporalControl` against an
    adapter built on ``temporalio.client.Client``; tests inject an
    in-memory fake. The protocol is intentionally small — every
    method maps 1:1 to a router endpoint so the boundary is crisp.
    """

    async def get_workflow_description(
        self, workflow_id: str
    ) -> WorkflowDescription:
        """Return metadata for a single workflow.

        Raises:
            WorkflowNotFoundError: When Temporal returns ``NOT_FOUND``.
            WorkflowControlError: For any other RPC failure.
        """

    async def cancel_workflow(self, workflow_id: str) -> None:
        """Issue a cancel signal to a running workflow."""

    async def signal_workflow(
        self,
        workflow_id: str,
        signal_name: str,
        payload: Any,
    ) -> None:
        """Deliver a named signal with ``payload`` to the workflow."""

    async def restart_workflow(self, workflow_id: str) -> RestartedWorkflow:
        """Read the original input from history and start a new run.

        Implementations typically:

        1. Fetch the workflow's first history event.
        2. Extract ``workflow_type``, ``task_queue``, and the original
           ``input`` payload from the
           ``WorkflowExecutionStartedEventAttributes``.
        3. Call ``client.start_workflow`` with the same parameters,
           using ``WorkflowIDReusePolicy.ALLOW_DUPLICATE`` so a fresh
           run is created.
        """

    async def list_workflows(
        self,
        *,
        dept_id: str | None,
        wf_status: str | None,
        page: int,
        page_size: int,
        page_token: str | None,
    ) -> WorkflowPage:
        """Return one page of workflow summaries."""


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class SignalRequest(BaseModel):
    """Request body for ``POST /api/v1/workflows/{workflow_id}/signal``.

    The ``payload`` field is a free-form JSON value (object, list,
    scalar, or ``None``) so the router can deliver any signal shape
    the workflow expects.
    """

    signal_name: str = Field(
        ..., min_length=1, max_length=200, description="Temporal signal name."
    )
    payload: Any = Field(
        default=None,
        description="JSON-serialisable signal payload (default ``None``).",
    )


# ---------------------------------------------------------------------------
# Router setup
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/api/v1/workflows",
    tags=["workflow-control"],
)


# ---------------------------------------------------------------------------
# Helpers — Temporal client + audit sink lookup
# ---------------------------------------------------------------------------


def _get_temporal_client(request: Request) -> SupportsTemporalControl:
    """Return the wired :class:`SupportsTemporalControl` instance.

    Raises:
        HTTPException(503): When the slot is ``None`` (Temporal not
            reachable, lifespan still building the client). The
            ``reason`` field tells the FE that the surface is
            otherwise healthy — no need to render a generic 5xx page.
    """

    client = getattr(request.app.state, "temporal_workflow_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "temporal_unavailable",
            },
        )
    return client


def _get_audit_sink(request: Request) -> Any | None:
    """Return the audit sink wired for workflow-control events.

    The router prefers an explicit ``app.state.workflow_control_audit_sink``
    when set, and falls back to the AdminProxy's audit sink (the same
    sink the :func:`feature_flags.toggle_flag` endpoint uses) so
    workflow-control events land in the same ``automation.audit_events``
    stream as other admin actions. When no sink is wired the helper
    returns ``None`` and :func:`_emit_audit` becomes a no-op.
    """

    explicit = getattr(request.app.state, "workflow_control_audit_sink", None)
    if explicit is not None:
        return explicit
    proxy = getattr(request.app.state, "admin_proxy", None)
    if proxy is not None:
        return getattr(proxy, "_audit", None)
    return None


async def _emit_audit(
    request: Request,
    *,
    actor: AuthClaims,
    action_kind: Literal["cancel", "retry", "signal"],
    workflow_id: str,
    result: Literal["ok", "denied", "error"],
    extra_payload: Mapping[str, Any] | None = None,
) -> None:
    """Write a single ``workflow_control`` audit event.

    Failures are swallowed — audit hiccups must not block the
    underlying control action. The envelope shape mirrors the
    ``automation.audit_events`` row layout so the foundation audit
    pipeline can ingest it directly.
    """

    sink = _get_audit_sink(request)
    if sink is None:
        return

    payload: dict[str, Any] = {
        "action_kind": action_kind,
        "workflow_id": workflow_id,
    }
    if extra_payload:
        payload.update(extra_payload)

    event = AuditEvent(
        actor_id=actor.sub,
        actor_role="admin",
        dept_id=None,
        action=_AUDIT_ACTION,
        resource=f"workflow:{workflow_id}",
        result=result,
        timestamp=datetime.now(tz=timezone.utc),
        payload=payload,
    )
    try:
        await sink.write(event)
    except Exception as exc:  # noqa: BLE001 — audit must never block
        logger.warning(
            "workflow_control audit write failed (action=%s, wf=%s): %s",
            action_kind,
            workflow_id,
            exc,
        )


def _map_control_exception(exc: Exception, workflow_id: str) -> HTTPException:
    """Translate an implementation exception into the right HTTP error.

    * :class:`WorkflowNotFoundError` → ``404`` with a stable
      ``"workflow_not_found"`` ``detail`` so the FE can recognise the
      case without parsing the message.
    * :class:`WorkflowControlError` → ``502`` (upstream failure).
    * Anything else is re-raised by the caller — FastAPI's default
      500 handler kicks in.
    """

    if isinstance(exc, WorkflowNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workflow_not_found",
        )
    if isinstance(exc, WorkflowControlError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "temporal_rpc_failed",
                "workflow_id": workflow_id,
                "message": str(exc),
            },
        )
    # Caller will let FastAPI's default exception handler take it.
    raise exc  # pragma: no cover - defensive


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/{workflow_id}/cancel",
    summary="Cancel a running Temporal workflow (admin only)",
    dependencies=[Depends(require_admin)],
)
async def cancel_workflow(
    workflow_id: str,
    request: Request,
    actor: AuthClaims = Depends(require_admin),
) -> dict[str, Any]:
    """Send a Temporal cancel signal to ``workflow_id``.

    Audit log: a single ``workflow_control`` event is emitted
    **before** the cancel is dispatched. The event records
    ``action_kind="cancel"`` and the ``workflow_id``. When Temporal
    reports the workflow does not exist the endpoint returns
    ``HTTP 404`` with ``detail="workflow_not_found"`` and the audit
    event records ``result="denied"`` so the failure is observable.
    """

    client = _get_temporal_client(request)

    # Verify the workflow exists first — gives us a clean 404 path
    # without needing to interpret the cancel RPC error itself.
    try:
        await client.get_workflow_description(workflow_id)
    except WorkflowNotFoundError as exc:
        await _emit_audit(
            request,
            actor=actor,
            action_kind="cancel",
            workflow_id=workflow_id,
            result="denied",
            extra_payload={"reason": "workflow_not_found"},
        )
        raise _map_control_exception(exc, workflow_id) from exc
    except WorkflowControlError as exc:
        await _emit_audit(
            request,
            actor=actor,
            action_kind="cancel",
            workflow_id=workflow_id,
            result="error",
            extra_payload={"reason": "describe_failed", "message": str(exc)},
        )
        raise _map_control_exception(exc, workflow_id) from exc

    # The audit row must reflect the actual outcome of the mutation,
    # so write it *after* the RPC
    # returns (or raises). Writing before would leave a permanent
    # ``result="ok"`` row even when the cancel RPC fails and the
    # request returns HTTP 502.
    try:
        await client.cancel_workflow(workflow_id)
    except WorkflowNotFoundError as exc:
        # The describe above succeeded but the workflow disappeared
        # between calls — surface the 404 cleanly and record the
        # denial so the audit trail still reflects the outcome.
        await _emit_audit(
            request,
            actor=actor,
            action_kind="cancel",
            workflow_id=workflow_id,
            result="denied",
            extra_payload={"reason": "workflow_not_found"},
        )
        raise _map_control_exception(exc, workflow_id) from exc
    except WorkflowControlError as exc:
        await _emit_audit(
            request,
            actor=actor,
            action_kind="cancel",
            workflow_id=workflow_id,
            result="error",
            extra_payload={"reason": "cancel_failed", "message": str(exc)},
        )
        raise _map_control_exception(exc, workflow_id) from exc

    await _emit_audit(
        request,
        actor=actor,
        action_kind="cancel",
        workflow_id=workflow_id,
        result="ok",
    )

    return {
        "status": "cancelled",
        "workflow_id": workflow_id,
    }


@router.post(
    "/{workflow_id}/retry",
    summary="Retry a workflow with the same input (admin only)",
    dependencies=[Depends(require_admin)],
)
async def retry_workflow(
    workflow_id: str,
    request: Request,
    actor: AuthClaims = Depends(require_admin),
) -> dict[str, Any]:
    """Restart ``workflow_id`` using its original input.

    The implementation reads ``workflow_type``, ``task_queue``, and
    the original args from the workflow's first history event, then
    calls ``client.start_workflow`` with ``ALLOW_DUPLICATE`` so a
    fresh run is created. The new workflow's id is returned in the
    response body so the FE can link to the drill-down page directly.
    """

    client = _get_temporal_client(request)

    # 404 if the original workflow is unknown.
    try:
        await client.get_workflow_description(workflow_id)
    except WorkflowNotFoundError as exc:
        await _emit_audit(
            request,
            actor=actor,
            action_kind="retry",
            workflow_id=workflow_id,
            result="denied",
            extra_payload={"reason": "workflow_not_found"},
        )
        raise _map_control_exception(exc, workflow_id) from exc
    except WorkflowControlError as exc:
        await _emit_audit(
            request,
            actor=actor,
            action_kind="retry",
            workflow_id=workflow_id,
            result="error",
            extra_payload={"reason": "describe_failed", "message": str(exc)},
        )
        raise _map_control_exception(exc, workflow_id) from exc

    # Emit the audit row *after* the restart RPC returns so ``result``
    # reflects the actual outcome.
    try:
        restarted = await client.restart_workflow(workflow_id)
    except WorkflowNotFoundError as exc:
        await _emit_audit(
            request,
            actor=actor,
            action_kind="retry",
            workflow_id=workflow_id,
            result="denied",
            extra_payload={"reason": "workflow_not_found"},
        )
        raise _map_control_exception(exc, workflow_id) from exc
    except WorkflowControlError as exc:
        await _emit_audit(
            request,
            actor=actor,
            action_kind="retry",
            workflow_id=workflow_id,
            result="error",
            extra_payload={"reason": "restart_failed", "message": str(exc)},
        )
        raise _map_control_exception(exc, workflow_id) from exc

    await _emit_audit(
        request,
        actor=actor,
        action_kind="retry",
        workflow_id=workflow_id,
        result="ok",
    )

    return {
        "status": "restarted",
        "workflow_id": workflow_id,
        "new_workflow_id": restarted.new_workflow_id,
        "workflow_type": restarted.workflow_type,
        "run_id": restarted.run_id,
    }


@router.post(
    "/{workflow_id}/signal",
    summary="Send a signal to a workflow (admin only)",
    dependencies=[Depends(require_admin)],
)
async def signal_workflow(
    workflow_id: str,
    request: Request,
    actor: AuthClaims = Depends(require_admin),
    body: SignalRequest = Body(...),
) -> dict[str, Any]:
    """Deliver an arbitrary signal to ``workflow_id``.

    Body shape: ``{"signal_name": str, "payload": <any JSON value>}``.
    The audit event records ``action_kind="signal"`` and includes the
    signal name in the payload (the body itself is **not** logged so
    a sensitive payload doesn't end up in the audit row by accident).
    """

    client = _get_temporal_client(request)

    try:
        await client.get_workflow_description(workflow_id)
    except WorkflowNotFoundError as exc:
        await _emit_audit(
            request,
            actor=actor,
            action_kind="signal",
            workflow_id=workflow_id,
            result="denied",
            extra_payload={
                "reason": "workflow_not_found",
                "signal_name": body.signal_name,
            },
        )
        raise _map_control_exception(exc, workflow_id) from exc
    except WorkflowControlError as exc:
        await _emit_audit(
            request,
            actor=actor,
            action_kind="signal",
            workflow_id=workflow_id,
            result="error",
            extra_payload={
                "reason": "describe_failed",
                "signal_name": body.signal_name,
                "message": str(exc),
            },
        )
        raise _map_control_exception(exc, workflow_id) from exc

    # Emit the audit row *after* the signal RPC returns so ``result``
    # reflects the actual outcome.
    # The signal name is logged but the body itself is **not**, so a
    # sensitive payload doesn't end up in the audit row by accident.
    try:
        await client.signal_workflow(workflow_id, body.signal_name, body.payload)
    except WorkflowNotFoundError as exc:
        await _emit_audit(
            request,
            actor=actor,
            action_kind="signal",
            workflow_id=workflow_id,
            result="denied",
            extra_payload={
                "reason": "workflow_not_found",
                "signal_name": body.signal_name,
            },
        )
        raise _map_control_exception(exc, workflow_id) from exc
    except WorkflowControlError as exc:
        await _emit_audit(
            request,
            actor=actor,
            action_kind="signal",
            workflow_id=workflow_id,
            result="error",
            extra_payload={
                "reason": "signal_failed",
                "signal_name": body.signal_name,
                "message": str(exc),
            },
        )
        raise _map_control_exception(exc, workflow_id) from exc

    await _emit_audit(
        request,
        actor=actor,
        action_kind="signal",
        workflow_id=workflow_id,
        result="ok",
        extra_payload={"signal_name": body.signal_name},
    )

    return {
        "status": "signalled",
        "workflow_id": workflow_id,
        "signal_name": body.signal_name,
    }


@router.get(
    "",
    summary="List workflows with dept / status filters and pagination",
    dependencies=[Depends(require_admin)],
)
async def list_workflows(
    request: Request,
    dept_id: str | None = Query(
        default=None,
        max_length=64,
        description="Filter by department id (search attribute).",
    ),
    wf_status: str | None = Query(
        default=None,
        alias="status",
        max_length=32,
        description="Filter by workflow status (eg. ``running``, ``failed``).",
    ),
    page: int = Query(default=1, ge=1, description="1-based page index."),
    page_size: int = Query(
        default=_DEFAULT_PAGE_SIZE,
        ge=1,
        le=_MAX_PAGE_SIZE,
        description=f"Page size (max {_MAX_PAGE_SIZE}).",
    ),
    page_token: str | None = Query(
        default=None,
        description=(
            "Optional opaque cursor returned by the previous page. When "
            "supplied the server uses it instead of recomputing offset."
        ),
    ),
) -> dict[str, Any]:
    """Return one page of workflows visible to the admin.

    Pagination is capped at 50 entries per page server-side
    so a malicious or curious caller cannot dump
    the entire workflow history in a single request. The server
    returns ``next_page_token`` when more pages exist; callers who
    prefer numeric pagination can keep incrementing ``page`` and
    ignore the cursor.
    """

    client = _get_temporal_client(request)

    # Defensive cap — the Query() ``le=_MAX_PAGE_SIZE`` already enforces
    # this, but the explicit ``min(...)`` keeps the contract obvious for
    # readers and protects against tests that override the dependency.
    effective_page_size = min(page_size, _MAX_PAGE_SIZE)

    try:
        result = await client.list_workflows(
            dept_id=dept_id,
            wf_status=wf_status,
            page=page,
            page_size=effective_page_size,
            page_token=page_token,
        )
    except WorkflowControlError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "temporal_rpc_failed",
                "message": str(exc),
            },
        ) from exc

    return {
        "items": [item.to_dict() for item in result.items],
        "page": result.page,
        "page_size": result.page_size,
        "next_page_token": result.next_page_token,
        "filters": {
            "dept_id": dept_id,
            "status": wf_status,
        },
    }
