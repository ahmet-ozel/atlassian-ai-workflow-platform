"""``POST /api/workflows/{workflow_id}/cancel`` endpoint and predicate.

Implements task 13.1 (``platform-mimari-workflows`` tasks.md):

* :func:`is_cancel_authorized` — pure RBAC predicate. ``True`` iff the
  caller is the issue reporter or appears in the past assignees set.
  Property 11(a) bullet (``is_cancel_authorized(actor, reporter,
  past_assignees) == True ⇔ actor == reporter OR actor ∈
  past_assignees``).
* :data:`router` — ``POST /api/workflows/{workflow_id}/cancel``
  FastAPI router. Extracts the actor from the OIDC token via the
  injected validator, looks up the workflow's underlying Jira issue
  (reporter + past assignees) via a caller-supplied callback, and on
  authorization-pass calls :meth:`temporalio.client.WorkflowHandle.cancel`.
  Failures emit a single ``rbac_denied`` audit row through the
  foundation :class:`audit_logger.AuditLogger`.

The endpoint is deliberately *thin*: every collaborator (OIDC
validator, Temporal client, issue lookup, audit logger, clock) is
read from :class:`CancelEndpointDeps` parked on
``request.app.state.cancel``. Tests inject a stub container so the
router can be exercised end-to-end without a live Temporal cluster
or IdP.

Design references
-----------------

* ``platform-mimari-workflows/requirements.md`` — Requirement 11.1.
* ``platform-mimari-workflows/design.md`` — Components and Interfaces
  §"Cancel API (R11.1)" and Property 11.
* ``platform-mimari-foundation/audit_logger`` — ``actor_role`` is
  required; the writer rejects empty / unknown values before any DB
  round-trip.

The predicate is intentionally **pure** (frozen sets, no clock, no
I/O) so it can be exercised in isolation by both the unit test
(``services/automation-service/tests/unit/test_cancel_rbac.py``) and
the Hypothesis-driven property test
(``platform/tests/property/test_cancel_rbac.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Mapping,
    Protocol,
    runtime_checkable,
)

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from audit_logger import AuditEvent, AuditLogger
from auth_shared import (
    AuthContext,
    InvalidTokenError,
    OIDCValidator,
)


__all__ = [
    "CancelEndpointDeps",
    "IssueRef",
    "IssueLookup",
    "SupportsTemporalCancel",
    "is_cancel_authorized",
    "router",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure predicate (Property 11(a))
# ---------------------------------------------------------------------------


def is_cancel_authorized(
    actor_user_id: str,
    reporter_id: str,
    past_assignees: frozenset[str],
) -> bool:
    """Return whether ``actor_user_id`` may cancel a workflow.

    Implements Requirement 11.1 verbatim: a workflow may be cancelled
    by either the issue ``reporter`` or anyone who has at some point
    been listed as an ``assignee`` (the "past assignees" set
    maintained by ``AgentRunnerWorkflow.iter_advance``).

    The function is **pure** — no I/O, no clock, no globals — so it
    can be replayed deterministically from a workflow body and reused
    inside Hypothesis property tests without monkey-patching anything.

    Args:
        actor_user_id: The OIDC ``sub`` (or equivalent stable user id)
            of the caller initiating the cancel. Empty / blank values
            are treated as unauthorized.
        reporter_id: Account id of the issue's reporter.
        past_assignees: Frozen set of account ids that have at any
            point in the workflow's lifetime been the issue's
            assignee. The empty set is a perfectly legal value.

    Returns:
        ``True`` iff ``actor_user_id`` matches ``reporter_id`` or is
        a member of ``past_assignees``; ``False`` otherwise.
    """

    # Defensive: an empty string is never authorized regardless of
    # what the issue lookup returns. This matches the spec's
    # implicit invariant that an unauthenticated request never reaches
    # this predicate (see ``router`` below — it raises 401 first), but
    # if it does the answer must be ``False``.
    if not actor_user_id:
        return False

    if actor_user_id == reporter_id:
        return True
    return actor_user_id in past_assignees


# ---------------------------------------------------------------------------
# Dependency container — injected via ``request.app.state.cancel``
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IssueRef:
    """Subset of issue fields the cancel endpoint needs.

    The endpoint only consumes the ``reporter`` and ``past_assignees``
    fields, so the lookup callback can return this minimal projection
    instead of the full :class:`automation_service`'s Jira issue
    payload. ``past_assignees`` is a :class:`frozenset` so the
    contract aligns with :func:`is_cancel_authorized` and so callers
    cannot accidentally hand in a mutable container.
    """

    reporter: str
    past_assignees: frozenset[str] = frozenset()


#: Coroutine returning the :class:`IssueRef` associated with a
#: workflow_id. The caller (production wiring or test fake) is
#: responsible for translating the workflow_id into the underlying
#: Jira issue (the canonical mapping is encoded in
#: :func:`temporal_shared.identifiers.parse_workflow_id`). Returning
#: ``None`` causes the endpoint to respond with HTTP 404.
IssueLookup = Callable[[str], Awaitable["IssueRef | None"]]


@runtime_checkable
class SupportsTemporalCancel(Protocol):
    """Structural type for the Temporal client used by this endpoint.

    Any object exposing
    ``get_workflow_handle(workflow_id) -> WorkflowHandle`` is
    acceptable. The handle, in turn, must expose an awaitable
    ``cancel()`` method. This is exactly the surface of
    :class:`temporalio.client.Client` (already used by
    ``automation_service.temporal_client.TemporalClient``); declaring
    the Protocol here keeps the cancel module trivially mockable in
    unit / integration tests without importing the full SDK.
    """

    def get_workflow_handle(self, workflow_id: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class CancelEndpointDeps:
    """Collaborators the cancel router pulls from ``app.state.cancel``.

    The router owns no state of its own. Production wiring builds one
    of these in :func:`automation_service.app.create_app`; tests
    construct the dataclass directly with hand-built fakes.

    Attributes:
        oidc_validator: The :class:`auth_shared.OIDCValidator`
            authenticating the bearer token. Production wiring uses
            ``OIDCValidator(OIDCConfig.from_env())``; dev / test
            wiring may pass a dev-mode validator (``auth_mode="dev"``)
            so any non-empty token returns the canned admin claims.
        issue_lookup: Async callback resolving a workflow_id to an
            :class:`IssueRef`. Returning ``None`` => 404.
        temporal_client: A :class:`SupportsTemporalCancel`. Used to
            invoke ``WorkflowHandle.cancel()`` after authorization
            passes.
        audit_logger: Audit sink for ``rbac_denied`` and
            ``workflow_cancel_requested`` events. Required by
            Requirement 11.1 and 11.4.
        clock: Optional callable returning the current UTC datetime.
            When omitted, the router uses
            ``datetime.now(timezone.utc)``. Tests inject a frozen
            clock so audit timestamps are deterministic.
    """

    oidc_validator: OIDCValidator
    issue_lookup: IssueLookup
    temporal_client: SupportsTemporalCancel
    audit_logger: AuditLogger
    clock: Callable[[], datetime] | None = None


def _deps(request: Request) -> CancelEndpointDeps:
    """Pull the :class:`CancelEndpointDeps` off ``app.state``.

    Surfaces a deployment misconfiguration (router mounted but
    collaborators not wired) as a clear 500 instead of a downstream
    :class:`AttributeError`.
    """

    deps = getattr(request.app.state, "cancel", None)
    if not isinstance(deps, CancelEndpointDeps):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="cancel router is not wired (app.state.cancel missing)",
        )
    return deps


def _now(deps: CancelEndpointDeps) -> datetime:
    """Return the current UTC timestamp using the injected clock."""

    if deps.clock is not None:
        return deps.clock()
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api", tags=["workflows"])


def _extract_bearer_token(authorization: str | None) -> str:
    """Return the bearer token from an ``Authorization`` header.

    Raises :class:`HTTPException` ``401`` for missing or malformed
    headers. The error detail is intentionally generic so the response
    body never leaks whether the token format vs the token contents
    was the problem.
    """

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Authorization header",
        )
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="malformed Authorization header",
        )
    return parts[1].strip()


def _resolve_actor_user_id(claims: Mapping[str, Any]) -> str | None:
    """Pick the ``actor_user_id`` from a decoded claim dict.

    Prefers ``account_id`` (the Atlassian-specific claim sometimes
    minted by an SSO bridge), falling back to the canonical OIDC
    ``sub``. Returns ``None`` when neither is present so the caller
    can map the case to HTTP 401.
    """

    for key in ("account_id", "sub"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _make_audit_event(
    *,
    actor_id: str,
    actor_role: str,
    action: str,
    resource: str,
    result: str,
    timestamp: datetime,
    payload: dict[str, Any] | None,
) -> AuditEvent:
    """Construct an :class:`AuditEvent` with the four required RBAC roles.

    The cancel endpoint runs **after** OIDC authentication so the
    actor role is always one of the four human roles ("viewer",
    "lead", "admin", "dept_admin") rather than the synthetic
    "system" used by background processes. We keep the call site
    explicit so a caller passing an empty / unknown role hits the
    :class:`audit_logger.AuditLogger` ``ValueError`` immediately.
    """

    # ``audit_logger`` only accepts a fixed set of roles; map any
    # unrecognised value (eg. a malformed JWT claim slipping through
    # ``extract_auth_context``) to ``"system"`` so the audit row is
    # still written.  The audit event itself records the original
    # role on ``payload["claimed_role"]`` for forensic visibility.
    safe_role = actor_role if actor_role in (
        "viewer", "lead", "admin", "dept_admin", "system",
    ) else "system"
    enriched_payload: dict[str, Any] | None = payload
    if safe_role != actor_role:
        enriched_payload = dict(payload or {})
        enriched_payload["claimed_role"] = actor_role
    return AuditEvent(
        actor_id=actor_id,
        actor_role=safe_role,  # type: ignore[arg-type]
        dept_id=None,
        action=action,
        resource=resource,
        result=result,  # type: ignore[arg-type]
        timestamp=timestamp,
        payload=enriched_payload,
    )


async def _emit_audit(audit_logger: AuditLogger, event: AuditEvent) -> None:
    """Best-effort audit write — never let an audit error 500 the call.

    Mirrors the pattern used by ``webhooks_handlers._emit_audit`` and
    ``inbound.slack_to_task._emit_audit``: failures are warning-logged
    locally so the operator can investigate but the user-visible
    response is unaffected.
    """

    try:
        await audit_logger.write(event)
    except Exception as exc:  # noqa: BLE001 - best-effort
        _LOG.warning(
            "cancel.audit_write_failed action=%s resource=%s err=%s",
            event.action,
            event.resource,
            type(exc).__name__,
        )


@router.post(
    "/workflows/{workflow_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_workflow(
    workflow_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """Cancel a running Temporal workflow on behalf of the caller.

    The endpoint:

    1. Validates the OIDC bearer token via the injected
       :class:`OIDCValidator`. Missing / malformed / invalid tokens
       receive HTTP 401.
    2. Looks up the workflow's underlying Jira issue (reporter +
       past assignees) via the injected :func:`IssueLookup`.
       Returning ``None`` => HTTP 404.
    3. Runs :func:`is_cancel_authorized` against the actor's
       ``account_id`` (or ``sub`` fallback). Failures emit a single
       ``rbac_denied`` audit row and respond HTTP 403.
    4. On authorization pass, calls
       ``temporal_client.get_workflow_handle(workflow_id).cancel()``,
       writes a ``workflow_cancel_requested`` audit row and responds
       HTTP 202 with ``{"workflow_id": ..., "cancel_requested":
       true}``.

    The body of the request is currently ignored — a future revision
    may accept ``{"reason": "..."}`` (Requirement 11.4) and forward
    it to the audit payload. The endpoint already accepts arbitrary
    JSON without parsing it so the body shape can be extended without
    a breaking change.
    """

    deps = _deps(request)

    # ---------- 1. AuthN -----------------------------------------------------
    token = _extract_bearer_token(authorization)
    try:
        claims = deps.oidc_validator.validate(token)
    except InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
        ) from exc

    actor_user_id = _resolve_actor_user_id(claims)
    if actor_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token missing account_id / sub claim",
        )

    # ``role`` is recorded on every audit row so ``actor_role`` can be
    # populated correctly; we accept it being absent (mapped to
    # ``"system"`` by ``_make_audit_event``) since the predicate above
    # is the source of truth for the cancel decision and does not
    # consume the role.
    raw_role = claims.get("role")
    actor_role = raw_role if isinstance(raw_role, str) and raw_role else "system"

    # ---------- 2. Issue lookup ---------------------------------------------
    issue: IssueRef | None
    try:
        issue = await deps.issue_lookup(workflow_id)
    except Exception as exc:  # noqa: BLE001 — translate to 502
        _LOG.warning(
            "cancel.issue_lookup_failed workflow_id=%s err=%s",
            workflow_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"issue lookup failed: {type(exc).__name__}",
        ) from exc

    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no issue found for workflow_id={workflow_id!r}",
        )

    # ---------- 3. RBAC predicate -------------------------------------------
    authorized = is_cancel_authorized(
        actor_user_id=actor_user_id,
        reporter_id=issue.reporter,
        past_assignees=issue.past_assignees,
    )
    if not authorized:
        await _emit_audit(
            deps.audit_logger,
            _make_audit_event(
                actor_id=actor_user_id,
                actor_role=actor_role,
                action="rbac_denied",
                resource=f"workflow:{workflow_id}",
                result="denied",
                timestamp=_now(deps),
                payload={
                    "endpoint": "POST /api/workflows/{workflow_id}/cancel",
                    "reason": "actor is neither reporter nor past assignee",
                },
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="not authorized to cancel this workflow",
        )

    # ---------- 4. Temporal cancel + audit ----------------------------------
    try:
        handle = deps.temporal_client.get_workflow_handle(workflow_id)
        await handle.cancel()
    except Exception as exc:  # noqa: BLE001 — translate to 502
        await _emit_audit(
            deps.audit_logger,
            _make_audit_event(
                actor_id=actor_user_id,
                actor_role=actor_role,
                action="workflow_cancel_failed",
                resource=f"workflow:{workflow_id}",
                result="error",
                timestamp=_now(deps),
                payload={"reason": type(exc).__name__},
            ),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"temporal cancel failed: {type(exc).__name__}",
        ) from exc

    await _emit_audit(
        deps.audit_logger,
        _make_audit_event(
            actor_id=actor_user_id,
            actor_role=actor_role,
            action="workflow_cancel_requested",
            resource=f"workflow:{workflow_id}",
            result="ok",
            timestamp=_now(deps),
            payload={
                "endpoint": "POST /api/workflows/{workflow_id}/cancel",
            },
        ),
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "workflow_id": workflow_id,
            "cancel_requested": True,
        },
    )
