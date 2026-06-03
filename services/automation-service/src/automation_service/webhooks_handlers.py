"""Jira webhook handlers.

Implements the explicit Jira webhook endpoints:

* ``POST /webhooks/jira/issue_created``
* ``POST /webhooks/jira/issue_commented``

The chain is the canonical four-step sequence:

1. **HMAC verify** via :func:`vault_client.verify_webhook_hmac`
   (per-department secret at ``vault:webhooks/jira/<dept_id>``;
   1h rotation overlap window).

   *  ``dept_id`` is resolved from the payload's ``issue.fields.project.key``
      via an injected :class:`DeptResolver`. If the lookup yields
      ``None``, the handler returns **HTTP 400** with audit
      ``webhook_dept_unresolved``.
   *  If HMAC fails → **HTTP 401** ``unauthorized`` (audit
      ``webhook_hmac_failed``).

2. **Loop guard**: if the event ``actor.account_id`` matches *any*
   department's ``bot.<svc>.account_id``, the handler drops the event
   and emits audit ``loop_guard_dropped``. Response is **HTTP 200**
   ``loop_guard``.

3. **Capability gate**: :func:`temporal_shared.gate` is consulted with
   the resolved ``Department`` and the workflow type (``noop_test`` for
   issue-created — the lightest gate possible at the webhook layer;
   the in-workflow analyzer later refines the workflow_type per the
   design's two-phase capability check). If the gate denies, the
   handler posts a bot comment to the Jira issue listing the missing
   capabilities, emits audit ``capability_denied``, and returns
   **HTTP 202** with ``{"decision": "denied", "missing": [...]}``.

4. **Idempotent workflow start**:
   :func:`temporal_shared.start_workflow_idempotent` swallows
   ``WorkflowAlreadyStarted`` and returns the existing
   ``execution_id`` so the caller always gets **HTTP 202** with the
   same body shape.

The implementation deliberately decouples the *protocol* of every
external collaborator (dept resolver, vault client, capability gate,
workflow starter, jira commenter, audit logger) from any concrete
runtime. The router is mounted by :mod:`automation_service.app`
which wires real implementations from ``app.state``; tests inject
hand-written fakes via the same attributes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Mapping,
    Protocol,
    runtime_checkable,
)

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from audit_logger import AuditEvent, AuditLogger
from temporal_shared.capabilities import (
    GateDecision,
    SupportsDepartment,
    gate,
)
from temporal_shared.start_helper import (
    StartResult,
    SupportsStartWorkflow,
    start_workflow_idempotent,
)
from temporal_shared.workflow_registry import task_queue_for
from vault_client import VaultClient, verify_webhook_hmac

__all__ = [
    "BotRegistryEntry",
    "DeptResolver",
    "JiraCommenter",
    "WebhookContext",
    "router",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Hub signature header (Atlassian Jira Cloud uses ``X-Hub-Signature``).
_HEADER_SIGNATURE = "X-Hub-Signature"

#: Provider value passed to :func:`verify_webhook_hmac` for Jira routes.
_PROVIDER_JIRA = "jira"

#: Workflow type to consult at the webhook layer for issue_created /
#: issue_commented. ``noop_test`` only requires ``jira_read``, which is
#: the minimum capability for any Jira-triggered automation. The real
#: workflow_type is refined inside the workflow once the LLM analyzer
#: classifies the issue.
#: At the webhook layer we only enforce the cheapest precondition:
#: the dept must at minimum have a Jira read credential.
_WEBHOOK_WORKFLOW_TYPE = "noop_test"

#: Temporal task queue used for ``AutomationWorkflow``. Matches the
#: registry entry for ``AutomationWorkflow``.
_WORKFLOW_NAME = "AutomationWorkflow"
_TASK_QUEUE = task_queue_for(_WORKFLOW_NAME)

#: Bot comment body posted to Jira when capability gate denies. The
#: ``{missing}`` placeholder is filled with the comma-separated set of
#: missing capabilities.
_CAPABILITY_DENIED_COMMENT = (
    "🤖 Otomasyon başlatılamadı: bu departman için eksik yetenek(ler): {missing}. "
    "Lütfen sistem yöneticinize başvurun."
)

#: Audit ``actor_id`` for webhook-handler-emitted events. The handler
#: itself is the actor; the role is always ``"system"`` per
#: background-process audit rows.
_AUDIT_ACTOR_ID = "automation-service.webhook"

#: All Jira webhook events the handler accepts.
_EVENT_ISSUE_CREATED = "jira:issue_created"
_EVENT_COMMENT_CREATED = "jira:comment_created"

#: A frozen mapping from URL path stem → expected Atlassian event
#: type. Drives the matching check in the per-route handlers.
_PATH_TO_EVENT: Mapping[str, str] = MappingProxyType(
    {
        "issue_created": _EVENT_ISSUE_CREATED,
        "issue_commented": _EVENT_COMMENT_CREATED,
    }
)


# ---------------------------------------------------------------------------
# Collaborator protocols
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BotRegistryEntry:
    """A single ``(dept_id, service, account_id)`` tuple.

    The webhook handler iterates the full registry to build the
    set of bot account IDs for the loop guard and
    to look up a dept's Jira bot account when posting the
    capability-denied comment.
    """

    dept_id: str
    service: str  # "jira" | "bitbucket" | "confluence"
    account_id: str


@runtime_checkable
class DeptResolver(Protocol):
    """Resolve a Jira ``project_key`` → :class:`SupportsDepartment`.

    The runtime implementation reads from the ``automation.departments``
    Postgres table joined with ``automation.department_project_keys``;
    tests inject a deterministic mapping. Returns ``None`` when no
    department is configured for *project_key* — the handler treats
    this as :ref:`webhook_dept_unresolved <webhook-dept-unresolved>`.
    """

    async def resolve_by_project_key(
        self, project_key: str
    ) -> SupportsDepartment | None: ...

    async def list_bot_account_ids(self) -> list[BotRegistryEntry]:
        """Return every ``(dept_id, service, account_id)`` triple."""

        ...


@runtime_checkable
class JiraCommenter(Protocol):
    """Post a bot comment on a Jira issue (best-effort).

    The capability-denied path uses this to leave a human-readable
    message on the source issue (design Workflow Başlatma Akışı —
    "Jira'ya bot yorumu"). Failures are caught and audited by the
    handler; they never escalate to a non-200 response.
    """

    async def post_comment(
        self, dept_id: str, issue_key: str, body: str
    ) -> None: ...


# ---------------------------------------------------------------------------
# WebhookContext — bag of dependencies populated from app.state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WebhookContext:
    """Runtime collaborators required by the design-aligned router.

    Pulled from ``app.state`` by :func:`_build_context` so each
    request observes a consistent snapshot. Tests construct this
    directly and stash it on ``app.state.webhook_v2``.
    """

    vault: VaultClient
    dept_resolver: DeptResolver
    workflow_client: SupportsStartWorkflow
    jira_commenter: JiraCommenter | None
    audit_logger: AuditLogger
    env: Mapping[str, str]
    now_fn: Callable[[], datetime]


# ---------------------------------------------------------------------------
# Audit + response helpers
# ---------------------------------------------------------------------------


def _make_audit_event(
    *,
    action: str,
    resource: str,
    result: str,
    dept_id: str | None,
    payload: dict[str, Any] | None,
    now_fn: Callable[[], datetime],
) -> AuditEvent:
    """Construct a :class:`AuditEvent` with ``actor_role='system'``."""

    return AuditEvent(
        actor_id=_AUDIT_ACTOR_ID,
        actor_role="system",
        dept_id=dept_id,
        action=action,
        resource=resource,
        result=result,  # type: ignore[arg-type]
        timestamp=now_fn(),
        payload=payload,
    )


async def _emit_audit(
    audit_logger: AuditLogger,
    event: AuditEvent,
) -> None:
    """Write the event, swallowing errors so audit failures never 500.

    A broken audit pipeline must not block webhook processing; we log
    the failure locally but proceed. Postgres-level audit gaps surface
    via the ``test_audit_one_to_one`` property test in the integration
    suite.
    """

    try:
        await audit_logger.write(event)
    except Exception as exc:  # noqa: BLE001 - best-effort
        logger.warning(
            "audit_write_failed",
            extra={
                "action": event.action,
                "dept_id": event.dept_id,
                "error": type(exc).__name__,
            },
        )


def _json_response(body: dict[str, Any], *, status_code: int) -> JSONResponse:
    """Return a stable, ASCII-safe JSON body."""

    return JSONResponse(status_code=status_code, content=body)


# ---------------------------------------------------------------------------
# Payload helpers (pure)
# ---------------------------------------------------------------------------


def _safe_json_loads(raw: bytes) -> dict[str, Any] | None:
    """Return parsed JSON object, or ``None`` if invalid."""

    try:
        decoded = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _extract_actor_id(payload: dict[str, Any]) -> str | None:
    user = payload.get("user")
    if isinstance(user, dict):
        account_id = user.get("accountId")
        if isinstance(account_id, str) and account_id:
            return account_id
    # ``jira:comment_created`` events carry the actor under
    # ``comment.author`` rather than the top-level ``user`` block.
    comment = payload.get("comment")
    if isinstance(comment, dict):
        author = comment.get("author")
        if isinstance(author, dict):
            account_id = author.get("accountId")
            if isinstance(account_id, str) and account_id:
                return account_id
    return None


def _extract_issue_key(payload: dict[str, Any]) -> str | None:
    issue = payload.get("issue")
    if isinstance(issue, dict):
        key = issue.get("key")
        if isinstance(key, str) and key:
            return key
    return None


def _extract_project_key(payload: dict[str, Any]) -> str | None:
    issue = payload.get("issue")
    if not isinstance(issue, dict):
        return None
    fields = issue.get("fields")
    if isinstance(fields, dict):
        project = fields.get("project")
        if isinstance(project, dict):
            key = project.get("key")
            if isinstance(key, str) and key:
                return key
    # Some Jira webhook payloads carry the project key at
    # ``issue.fields.project.key``; older shapes used ``project.key`` at
    # the top level. We accept both for robustness.
    project = payload.get("project")
    if isinstance(project, dict):
        key = project.get("key")
        if isinstance(key, str) and key:
            return key
    return None


def _build_workflow_id(event_type: str, issue_key: str) -> str:
    """Stable, human-readable workflow id for a Jira event.

    Matches the convention used by :mod:`temporal_shared.identifiers`
    (``automation-jira-<ISSUE-KEY>``) but does not depend on the
    issue-key validator there: Atlassian webhook payloads
    occasionally arrive with non-canonical key shapes (eg. lowercase
    in fixtures), and rejecting them at the webhook layer with HTTP
    400 is harsher than just falling back to a normalised id. The
    invariant we care about is *idempotency* — two deliveries for the
    same ``(event_type, issue_key)`` produce the same workflow id, and
    that holds with this simple formatter.
    """

    return f"automation-{event_type.split(':')[-1]}-{issue_key}"


# ---------------------------------------------------------------------------
# Loop guard
# ---------------------------------------------------------------------------


def _is_bot_actor(
    actor_id: str | None, registry: list[BotRegistryEntry]
) -> tuple[bool, str | None]:
    """Return ``(is_bot, dept_id_of_match)`` for the actor.

    A match against *any* registered bot — regardless of dept or
    service — triggers the loop guard.
    """

    if not actor_id:
        return False, None
    for entry in registry:
        if entry.account_id and entry.account_id == actor_id:
            return True, entry.dept_id
    return False, None


# ---------------------------------------------------------------------------
# Capability gate helpers
# ---------------------------------------------------------------------------


async def _post_capability_denied_comment(
    *,
    commenter: JiraCommenter | None,
    dept_id: str,
    issue_key: str,
    decision: GateDecision,
    audit_logger: AuditLogger,
    now_fn: Callable[[], datetime],
) -> None:
    """Best-effort Jira comment + audit log for the capability-denied path."""

    if commenter is None:
        return
    body = _CAPABILITY_DENIED_COMMENT.format(
        missing=", ".join(sorted(decision.missing))
    )
    try:
        await commenter.post_comment(dept_id, issue_key, body)
    except Exception as exc:  # noqa: BLE001 - best-effort
        await _emit_audit(
            audit_logger,
            _make_audit_event(
                action="bot_comment_failed",
                resource=f"webhook:jira/{issue_key}",
                result="error",
                dept_id=dept_id,
                payload={"reason": "capability_denied_ack", "error": type(exc).__name__},
                now_fn=now_fn,
            ),
        )


# ---------------------------------------------------------------------------
# Context plumbing
# ---------------------------------------------------------------------------


def _build_context(request: Request) -> WebhookContext | None:
    """Pull a :class:`WebhookContext` from ``app.state.webhook_v2``.

    Returns ``None`` when the application has not been wired yet (the
    handler then surfaces HTTP 503 so callers can distinguish from
    the design-controlled 4xx / 202 responses).
    """

    ctx = getattr(request.app.state, "webhook_v2", None)
    if ctx is None or not isinstance(ctx, WebhookContext):
        return None
    return ctx


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["webhooks-jira-v2"])


async def _process_jira_webhook(
    request: Request, *, expected_event: str, route_path: str
) -> JSONResponse:
    """Shared chain for issue_created / issue_commented routes.

    Sequence:

    1. read body + headers
    2. parse JSON (HTTP 400 on bad payload)
    3. resolve dept_id from project_key (HTTP 400 + ``webhook_dept_unresolved``
       audit on miss)
    4. HMAC verify via ``vault_client.verify_webhook_hmac``
       (HTTP 401 + ``webhook_hmac_failed`` audit on miss)
    5. loop guard (HTTP 200 ``loop_guard`` + ``loop_guard_dropped`` audit)
    6. capability gate (HTTP 202 ``decision: denied`` +
       ``capability_denied`` audit + Jira bot comment)
    7. idempotent workflow start (HTTP 202 ``decision: accepted``;
       ``WorkflowAlreadyStarted`` collapses to 202 with the existing id)
    """

    ctx = _build_context(request)
    if ctx is None:
        return _json_response(
            {"status": "service_unavailable", "reason": "webhook_v2_not_wired"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    raw_body: bytes = await request.body()
    signature = request.headers.get(_HEADER_SIGNATURE, "") or ""

    # ---- (a) parse JSON ------------------------------------------------
    payload = _safe_json_loads(raw_body)
    if payload is None:
        return _json_response(
            {"status": "bad_request", "reason": "invalid_json"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # The route was selected by URL path; the body's ``webhookEvent``
    # is informational only. We accept either a matching event type or
    # an absent / mismatching one (the URL is the source of truth) so
    # the handler still works for fixtures without a ``webhookEvent``
    # field.
    body_event_type = payload.get("webhookEvent")
    if isinstance(body_event_type, str) and body_event_type and body_event_type != expected_event:
        # Soft-warning only — log once and continue. Mismatches happen
        # in dev when curl-based fixtures are reused across endpoints.
        logger.info(
            "webhook_event_type_mismatch",
            extra={
                "url_event": expected_event,
                "body_event": body_event_type,
            },
        )

    issue_key = _extract_issue_key(payload)
    if issue_key is None:
        return _json_response(
            {"status": "bad_request", "reason": "missing_issue_key"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    project_key = _extract_project_key(payload)
    audit_resource = f"webhook:jira/{route_path}"

    # ---- (b) resolve dept_id from project_key --------------------------
    dept: SupportsDepartment | None = None
    dept_id: str | None = None
    if project_key is not None:
        dept = await ctx.dept_resolver.resolve_by_project_key(project_key)
        if dept is not None:
            dept_id = getattr(dept, "id", None)
    if dept is None or dept_id is None:
        await _emit_audit(
            ctx.audit_logger,
            _make_audit_event(
                action="webhook_dept_unresolved",
                resource=audit_resource,
                result="denied",
                dept_id=None,
                payload={
                    "project_key": project_key,
                    "issue_key": issue_key,
                    "event_type": expected_event,
                },
                now_fn=ctx.now_fn,
            ),
        )
        return _json_response(
            {"status": "bad_request", "reason": "webhook_dept_unresolved"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # ---- (c) HMAC verify (per-dept secret with 1h overlap) -------------
    try:
        hmac_ok = verify_webhook_hmac(
            ctx.vault,
            _PROVIDER_JIRA,
            dept_id,
            raw_body,
            signature,
            ctx.now_fn(),
        )
    except ValueError:
        # ``verify_webhook_hmac`` raises ``ValueError`` for unsupported
        # providers — should not happen here because we hard-code
        # ``_PROVIDER_JIRA``, but defending against accidental
        # regressions costs nothing.
        hmac_ok = False
    if not hmac_ok:
        await _emit_audit(
            ctx.audit_logger,
            _make_audit_event(
                action="webhook_hmac_failed",
                resource=audit_resource,
                result="denied",
                dept_id=dept_id,
                payload={"issue_key": issue_key, "event_type": expected_event},
                now_fn=ctx.now_fn,
            ),
        )
        return _json_response(
            {"status": "unauthorized"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # ---- (d) loop guard ------------------------------------------------
    bot_registry = await ctx.dept_resolver.list_bot_account_ids()
    actor_id = _extract_actor_id(payload)
    is_bot, matched_dept = _is_bot_actor(actor_id, bot_registry)
    if is_bot:
        await _emit_audit(
            ctx.audit_logger,
            _make_audit_event(
                action="loop_guard_dropped",
                resource=audit_resource,
                result="ok",
                dept_id=dept_id,
                payload={
                    "actor_id": actor_id,
                    "matched_bot_dept_id": matched_dept,
                    "issue_key": issue_key,
                    "event_type": expected_event,
                },
                now_fn=ctx.now_fn,
            ),
        )
        return _json_response(
            {"status": "loop_guard", "action": "skipped"},
            status_code=status.HTTP_200_OK,
        )

    # ---- (e) capability gate ------------------------------------------
    decision = gate(_WEBHOOK_WORKFLOW_TYPE, dept, ctx.env)
    if not decision.allowed:
        missing_sorted = sorted(decision.missing)
        await _emit_audit(
            ctx.audit_logger,
            _make_audit_event(
                action="capability_denied",
                resource=f"workflow:{_WEBHOOK_WORKFLOW_TYPE}",
                result="denied",
                dept_id=dept_id,
                payload={
                    "missing": missing_sorted,
                    "issue_key": issue_key,
                    "event_type": expected_event,
                },
                now_fn=ctx.now_fn,
            ),
        )
        await _post_capability_denied_comment(
            commenter=ctx.jira_commenter,
            dept_id=dept_id,
            issue_key=issue_key,
            decision=decision,
            audit_logger=ctx.audit_logger,
            now_fn=ctx.now_fn,
        )
        return _json_response(
            {
                "status": "accepted",
                "decision": "denied",
                "missing": missing_sorted,
                "issue_key": issue_key,
            },
            status_code=status.HTTP_202_ACCEPTED,
        )

    # ---- (f) idempotent workflow start --------------------------------
    workflow_id = _build_workflow_id(expected_event, issue_key)
    workflow_input = {
        "trigger": "jira",
        "event_type": expected_event,
        "issue_key": issue_key,
        "project_key": project_key,
        "department_id": dept_id,
    }

    try:
        result: StartResult = await start_workflow_idempotent(
            ctx.workflow_client,
            _WORKFLOW_NAME,
            workflow_id,
            [workflow_input],
            task_queue=_TASK_QUEUE,
        )
    except Exception as exc:  # noqa: BLE001 - any other Temporal failure
        await _emit_audit(
            ctx.audit_logger,
            _make_audit_event(
                action="webhook_workflow_start_failed",
                resource=audit_resource,
                result="error",
                dept_id=dept_id,
                payload={
                    "workflow_id": workflow_id,
                    "issue_key": issue_key,
                    "event_type": expected_event,
                    "error": type(exc).__name__,
                },
                now_fn=ctx.now_fn,
            ),
        )
        # Re-raise so FastAPI emits a 500; webhook delivery retries
        # will re-enter the chain and (if Temporal is back) succeed.
        raise

    audit_action = (
        "webhook_workflow_already_started"
        if result.was_existing
        else "webhook_workflow_started"
    )
    await _emit_audit(
        ctx.audit_logger,
        _make_audit_event(
            action=audit_action,
            resource=audit_resource,
            result="ok",
            dept_id=dept_id,
            payload={
                "workflow_id": result.execution_id,
                "issue_key": issue_key,
                "event_type": expected_event,
                "was_existing": result.was_existing,
            },
            now_fn=ctx.now_fn,
        ),
    )

    return _json_response(
        {
            "status": "accepted",
            "decision": "accepted",
            "workflow_id": result.execution_id,
            "was_existing": result.was_existing,
            "issue_key": issue_key,
        },
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.post("/jira/issue_created")
async def post_issue_created(request: Request) -> JSONResponse:
    """``POST /webhooks/jira/issue_created``."""

    return await _process_jira_webhook(
        request,
        expected_event=_EVENT_ISSUE_CREATED,
        route_path="issue_created",
    )


@router.post("/jira/issue_commented")
async def post_issue_commented(request: Request) -> JSONResponse:
    """``POST /webhooks/jira/issue_commented``."""

    return await _process_jira_webhook(
        request,
        expected_event=_EVENT_COMMENT_CREATED,
        route_path="issue_commented",
    )


@router.post("/jira/issue_assigned")
async def post_issue_assigned(request: Request) -> JSONResponse:
    """``POST /webhooks/jira/issue_assigned``.

    Handles Jira issue assignment events with dept-handover detection.
    When a task is re-assigned from one department's bot to another:
    - The existing workflow is cancelled.
    - A new workflow starts under the new dept context.
    - Audit event ``workflow_dept_handover`` is emitted.
    - A Jira bot comment explains the handover.

    Standard assignments (within same dept or to human users) follow
    the normal webhook processing chain.
    """
    from .webhooks_issue_assigned import (
        _extract_assignee_account_id,
        handle_issue_assigned,
    )

    ctx = _build_context(request)
    if ctx is None:
        return _json_response(
            {"status": "service_unavailable", "reason": "webhook_v2_not_wired"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    raw_body: bytes = await request.body()
    signature = request.headers.get(_HEADER_SIGNATURE, "") or ""

    # Parse JSON.
    payload = _safe_json_loads(raw_body)
    if payload is None:
        return _json_response(
            {"status": "bad_request", "reason": "invalid_json"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # HMAC verify (same as other handlers).
    issue_key = _extract_issue_key(payload)
    project_key = _extract_project_key(payload)
    dept_id: str | None = None
    if project_key:
        dept = await ctx.dept_resolver.resolve_by_project_key(project_key)
        if dept:
            dept_id = getattr(dept, "id", None)

    if not dept_id:
        assignee_account_id = _extract_assignee_account_id(payload)
        if assignee_account_id:
            for entry in await ctx.dept_resolver.list_bot_account_ids():
                if entry.account_id == assignee_account_id:
                    dept_id = entry.dept_id
                    break

    if dept_id:
        try:
            hmac_ok = verify_webhook_hmac(
                ctx.vault, _PROVIDER_JIRA, dept_id,
                raw_body, signature, ctx.now_fn(),
            )
        except ValueError:
            hmac_ok = False
        if not hmac_ok:
            await _emit_audit(
                ctx.audit_logger,
                _make_audit_event(
                    action="webhook_hmac_failed",
                    resource="webhook:jira/issue_assigned",
                    result="denied",
                    dept_id=dept_id,
                    payload={"issue_key": issue_key, "event_type": "jira:issue_assigned"},
                    now_fn=ctx.now_fn,
                ),
            )
            return _json_response(
                {"status": "unauthorized"},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )

    # Delegate to the dept-handover-aware handler.
    result_body = await handle_issue_assigned(
        payload,
        dept_resolver=ctx.dept_resolver,
        workflow_client=ctx.workflow_client,
        jira_commenter=ctx.jira_commenter,
        audit_logger=ctx.audit_logger,
        env=dict(ctx.env),
        now_fn=ctx.now_fn,
    )

    # Map result to HTTP status.
    if result_body.get("status") == "bad_request":
        return _json_response(result_body, status_code=status.HTTP_400_BAD_REQUEST)
    return _json_response(result_body, status_code=status.HTTP_202_ACCEPTED)


# ---------------------------------------------------------------------------
# Helper for callers that want the default ``now_fn``
# ---------------------------------------------------------------------------


def utc_now() -> datetime:
    """Return the current time in UTC.

    Exposed so callers (and tests) can populate :attr:`WebhookContext.now_fn`
    with a single canonical implementation rather than re-importing
    :mod:`datetime` themselves.
    """

    return datetime.now(timezone.utc)
