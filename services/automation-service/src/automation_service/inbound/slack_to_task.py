"""Slack  Jira task adapter.

Exposes ``POST /webhooks/inbound/slack`` - the public surface that
Slack's incoming-webhook / Events API delivers to. The chain mirrors
the Jira webhook handler (:mod:`automation_service.webhooks_handlers`)
to keep the audit / RBAC / loop-guard behaviour consistent across
trigger types:

1. Read the raw body and the Slack signing headers.
2. Parse the JSON envelope (Slack ``url_verification`` challenges
   are answered immediately without further processing).
3. Resolve the dept id from the ``team_id`` / ``channel`` claim;
   400 ``inbound_dept_unresolved`` if no mapping exists.
4. Verify the Slack signature via the injected
   :class:`SlackSignatureVerifier`. The verifier resolves the
   per-dept signing secret from Vault and uses
   :func:`verify_slack_signature` under the hood. A missing or stale
   signature  401 ``unauthorized`` + audit ``inbound_slack_hmac_failed``.
5. Extract the user mention text and build an
   :class:`InboundTaskRequest`.
6. Start an idempotent ``AutomationWorkflow`` via
   :func:`start_workflow_idempotent`, audit
   ``inbound_workflow_started`` (or ``inbound_workflow_already_started``
   for retries), and respond 202 with the resulting workflow id.

The handler intentionally does **not** call the Atlassian MCP
directly - Jira issue creation is the workflow's job. This keeps the
single-source-of-truth contract for the bot loop guard and capability
gate, just like the Jira webhook handler.

"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from audit_logger import AuditEvent, AuditLogger
from temporal_shared.start_helper import (
    StartResult,
    start_workflow_idempotent,
)

from .common import (
    INBOUND_TASK_QUEUE,
    INBOUND_WORKFLOW_NAME,
    InboundContext,
    InboundTaskRequest,
    auto_assign_workflow_input,
    build_inbound_workflow_id,
    extract_slack_command_text,
)

__all__ = ["router"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HEADER_SIGNATURE: str = "X-Slack-Signature"
_HEADER_TIMESTAMP: str = "X-Slack-Request-Timestamp"

#: Slack event types we accept. ``app_mention`` is the canonical
#: signal that the bot was @-mentioned in a channel; ``message`` is
#: accepted only when the body contains a leading mention prefix
#: (the parser strips it). All other event types are ignored with a
#: 200 response so Slack does not retry.
_ACCEPTED_EVENT_TYPES: frozenset[str] = frozenset({"app_mention", "message"})

#: Audit ``actor_id`` for inbound-handler-emitted events.
_AUDIT_ACTOR_ID: str = "automation-service.inbound.slack"

#: Audit resource discriminator.
_AUDIT_RESOURCE: str = "webhook:inbound/slack"


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["inbound-slack"])


# ---------------------------------------------------------------------------
# Audit + JSON helpers
# ---------------------------------------------------------------------------


def _make_audit_event(
    *,
    action: str,
    result: str,
    dept_id: str | None,
    payload: dict[str, Any] | None,
    ctx: InboundContext,
) -> AuditEvent:
    """Construct an :class:`AuditEvent` with ``actor_role='system'``."""

    return AuditEvent(
        actor_id=_AUDIT_ACTOR_ID,
        actor_role="system",
        dept_id=dept_id,
        action=action,
        resource=_AUDIT_RESOURCE,
        result=result,  # type: ignore[arg-type]
        timestamp=ctx.now_fn(),
        payload=payload,
    )


async def _emit_audit(audit_logger: AuditLogger, event: AuditEvent) -> None:
    """Best-effort audit write - log the failure locally and continue."""

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
    return JSONResponse(status_code=status_code, content=body)


# ---------------------------------------------------------------------------
# Payload helpers
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


def _extract_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return the inner ``event`` block or ``None``."""

    event = payload.get("event")
    if isinstance(event, dict):
        return event
    return None


def _extract_team_id(payload: dict[str, Any]) -> str | None:
    """Extract ``team_id`` from the Slack envelope (top-level field)."""

    team_id = payload.get("team_id")
    if isinstance(team_id, str) and team_id:
        return team_id
    # Some Slack delivery shapes nest the team identifier under ``team``.
    team = payload.get("team")
    if isinstance(team, dict):
        candidate = team.get("id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _extract_channel_id(event: dict[str, Any]) -> str | None:
    channel = event.get("channel")
    if isinstance(channel, str) and channel:
        return channel
    if isinstance(channel, dict):
        candidate = channel.get("id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _extract_user_id(event: dict[str, Any]) -> str | None:
    user = event.get("user")
    if isinstance(user, str) and user:
        return user
    if isinstance(user, dict):
        candidate = user.get("id")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _extract_external_id(event: dict[str, Any]) -> str | None:
    """Pick a stable id for the workflow.

    Prefers ``client_msg_id`` (a UUID Slack assigns to user messages);
    falls back to ``ts`` (the Unix-epoch message timestamp) which is
    unique within a channel.
    """

    candidate = event.get("client_msg_id")
    if isinstance(candidate, str) and candidate:
        return candidate
    candidate = event.get("ts")
    if isinstance(candidate, str) and candidate:
        return candidate
    return None


def _extract_text(event: dict[str, Any]) -> str:
    text = event.get("text")
    return text if isinstance(text, str) else ""


# ---------------------------------------------------------------------------
# Context plumbing
# ---------------------------------------------------------------------------


def _build_context(request: Request) -> InboundContext | None:
    """Pull an :class:`InboundContext` from ``app.state.inbound``."""

    ctx = getattr(request.app.state, "inbound", None)
    if ctx is None or not isinstance(ctx, InboundContext):
        return None
    return ctx


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/inbound/slack")
async def post_slack_inbound(request: Request) -> JSONResponse:  # noqa: PLR0911, PLR0915
    """Handle ``POST /webhooks/inbound/slack``.

    See the module docstring for the step-by-step chain.
    """

    ctx = _build_context(request)
    if ctx is None:
        return _json_response(
            {"status": "service_unavailable", "reason": "inbound_not_wired"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    raw_body: bytes = await request.body()
    signature = request.headers.get(_HEADER_SIGNATURE, "") or ""
    timestamp = request.headers.get(_HEADER_TIMESTAMP, "") or ""

    payload = _safe_json_loads(raw_body)
    if payload is None:
        return _json_response(
            {"status": "bad_request", "reason": "invalid_json"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # Slack URL-verification handshake - respond with the challenge
    # immediately. We still verify the signature first so an attacker
    # cannot use the handshake path to bypass HMAC.
    envelope_type = payload.get("type")
    if envelope_type == "url_verification":
        # Verify against the global (no-dept) secret. The verifier
        # treats ``dept_id=None`` as "use the platform-default secret"
        # - production wires this to ``vault:notifications/slack_inbound/_default``.
        ok = await ctx.slack_verifier.verify(
            dept_id=None,
            timestamp=timestamp,
            raw_body=raw_body,
            signature=signature,
            now=ctx.now_fn(),
        )
        if not ok:
            await _emit_audit(
                ctx.audit_logger,
                _make_audit_event(
                    action="inbound_slack_hmac_failed",
                    result="denied",
                    dept_id=None,
                    payload={"phase": "url_verification"},
                    ctx=ctx,
                ),
            )
            return _json_response(
                {"status": "unauthorized"},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        challenge = payload.get("challenge")
        if not isinstance(challenge, str):
            return _json_response(
                {"status": "bad_request", "reason": "missing_challenge"},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return _json_response({"challenge": challenge}, status_code=status.HTTP_200_OK)

    event = _extract_event(payload)
    if event is None:
        # Some envelope without an ``event`` block (eg. Slack
        # ``app_uninstalled`` notifications). Acknowledge without
        # processing so Slack does not retry.
        return _json_response(
            {"status": "ignored", "reason": "no_event_block"},
            status_code=status.HTTP_200_OK,
        )

    event_type = event.get("type")
    if event_type not in _ACCEPTED_EVENT_TYPES:
        return _json_response(
            {"status": "ignored", "reason": "unsupported_event"},
            status_code=status.HTTP_200_OK,
        )

    # ---- (1) resolve dept ---------------------------------------------
    team_id = _extract_team_id(payload)
    channel_id = _extract_channel_id(event)
    dept_id = await ctx.dept_resolver.resolve_for_slack(
        team_id=team_id, channel_id=channel_id
    )
    if dept_id is None:
        await _emit_audit(
            ctx.audit_logger,
            _make_audit_event(
                action="inbound_dept_unresolved",
                result="denied",
                dept_id=None,
                payload={
                    "channel": "slack",
                    "team_id": team_id,
                    "channel_id": channel_id,
                },
                ctx=ctx,
            ),
        )
        return _json_response(
            {"status": "bad_request", "reason": "inbound_dept_unresolved"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # ---- (2) HMAC verify (per-dept) -----------------------------------
    ok = await ctx.slack_verifier.verify(
        dept_id=dept_id,
        timestamp=timestamp,
        raw_body=raw_body,
        signature=signature,
        now=ctx.now_fn(),
    )
    if not ok:
        await _emit_audit(
            ctx.audit_logger,
            _make_audit_event(
                action="inbound_slack_hmac_failed",
                result="denied",
                dept_id=dept_id,
                payload={
                    "channel": "slack",
                    "team_id": team_id,
                    "channel_id": channel_id,
                },
                ctx=ctx,
            ),
        )
        return _json_response(
            {"status": "unauthorized"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # ---- (3) parse mention --------------------------------------------
    external_id = _extract_external_id(event)
    if external_id is None:
        await _emit_audit(
            ctx.audit_logger,
            _make_audit_event(
                action="inbound_bad_request",
                result="denied",
                dept_id=dept_id,
                payload={"channel": "slack", "reason": "missing_external_id"},
                ctx=ctx,
            ),
        )
        return _json_response(
            {"status": "bad_request", "reason": "missing_external_id"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    actor_handle = _extract_user_id(event) or "slack:unknown"
    raw_text = _extract_text(event)
    intent_text = extract_slack_command_text(raw_text)

    if not intent_text:
        # An empty mention (``@bot``) carries no actionable content.
        # Acknowledge so Slack does not retry, but do not start a
        # workflow.
        await _emit_audit(
            ctx.audit_logger,
            _make_audit_event(
                action="inbound_empty_mention",
                result="ok",
                dept_id=dept_id,
                payload={
                    "channel": "slack",
                    "external_id": external_id,
                    "actor_handle": actor_handle,
                },
                ctx=ctx,
            ),
        )
        return _json_response(
            {"status": "ignored", "reason": "empty_mention"},
            status_code=status.HTTP_200_OK,
        )

    # ---- (4) build request + start workflow ---------------------------
    try:
        req = InboundTaskRequest(
            channel="slack",
            external_id=external_id,
            dept_id=dept_id,
            actor_handle=actor_handle,
            intent_text=intent_text,
            title_hint=None,
        )
    except ValueError as exc:
        await _emit_audit(
            ctx.audit_logger,
            _make_audit_event(
                action="inbound_bad_request",
                result="denied",
                dept_id=dept_id,
                payload={
                    "channel": "slack",
                    "reason": "request_validation_failed",
                    "error": str(exc),
                },
                ctx=ctx,
            ),
        )
        return _json_response(
            {"status": "bad_request", "reason": "invalid_payload"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    workflow_id = build_inbound_workflow_id(req.channel, req.external_id)
    workflow_input = auto_assign_workflow_input(req)

    try:
        result: StartResult = await start_workflow_idempotent(
            ctx.workflow_client,
            INBOUND_WORKFLOW_NAME,
            workflow_id,
            [workflow_input],
            task_queue=INBOUND_TASK_QUEUE,
        )
    except Exception as exc:  # noqa: BLE001
        await _emit_audit(
            ctx.audit_logger,
            _make_audit_event(
                action="inbound_workflow_start_failed",
                result="error",
                dept_id=dept_id,
                payload={
                    "channel": "slack",
                    "external_id": req.external_id,
                    "workflow_id": workflow_id,
                    "error": type(exc).__name__,
                },
                ctx=ctx,
            ),
        )
        # Slack will retry on 5xx - surface a 500 so the platform's
        # health monitoring catches the upstream failure.
        raise

    audit_action = (
        "inbound_workflow_already_started"
        if result.was_existing
        else "inbound_workflow_started"
    )
    await _emit_audit(
        ctx.audit_logger,
        _make_audit_event(
            action=audit_action,
            result="ok",
            dept_id=dept_id,
            payload={
                "channel": "slack",
                "external_id": req.external_id,
                "workflow_id": result.execution_id,
                "was_existing": result.was_existing,
                "actor_handle": req.actor_handle,
                "auto_assign": True,
                "smart_defaults": True,
            },
            ctx=ctx,
        ),
    )

    return _json_response(
        {
            "status": "accepted",
            "decision": "accepted",
            "channel": "slack",
            "workflow_id": result.execution_id,
            "was_existing": result.was_existing,
        },
        status_code=status.HTTP_202_ACCEPTED,
    )
