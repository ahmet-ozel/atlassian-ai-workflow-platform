"""Jira webhook handler - ``POST /webhooks/jira``.

Receives Jira Cloud webhook events (``jira:issue_created``,
``jira:issue_assigned``, ``jira:issue_updated``, ``jira:comment_created``)
and either starts an ``AutomationWorkflow`` or signals an existing
workflow with a ``new_comment`` payload.

Guard chain (sequential - each step short-circuits with the documented
response shape and audit-log entry):

  (a) Read the raw request body.
  (b) ``hmac_verify.verify(...)`` - 401 ``unauthorized`` on failure.
  (c) ``replay.check_and_insert(...)`` SHA-256 dedup - 200 ``duplicate``
      on dup.
  (d) ``loop_guard.is_self_actor(...)`` - 200 ``loop_guard`` when the
      actor is a registered bot.
  (e) ``loop_guard.route(...)`` event-type classification - 200
      ``ignored`` for unsupported event types.
  (f) ``jira:comment_created``  ``temporal.signal_workflow(...)``
      ``new_comment`` signal, 200 ``signal_forwarded``.
  (g) ``jira:issue_created`` / ``jira:issue_assigned``
      ``loop_guard.is_bot_assignee``? Otherwise 200
      ``not_bot_assignee``.
  (h) ``jira:issue_updated``  ``loop_guard.assignee_changed_to_bot``?
      Otherwise 200 ``not_bot_assignee``.
  (i) ``capability_gate.has_jira_credential(...)`` - 200
      ``missing_capability`` if the resolved department lacks a Jira
      bot credential.
  (j) INSERT INTO ``automation.work_items`` with ``status='pending'``.
  (k) ``temporal.start_workflow("AutomationWorkflow", id=...)`` with
      ``automation_workflow_id_jira(issue_key)``.
  (l) Best-effort acknowledgement comment via the Atlassian MCP
      (Turkish per default ``departments.default_language='tr'``).
  (m) 200 ``{"status": "accepted", "workflow_id": ...}``.

A ``WorkflowAlreadyStartedError`` raised from step (k) collapses to a
200 ``duplicate`` response - Temporal native idempotency layered on top
of the SHA-256 replay guard.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Awaitable, Callable

import structlog
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from temporal_shared.identifiers import (
    InvalidIssueKeyError,
    automation_workflow_id_jira,
)
from temporal_shared.workflow_registry import task_queue_for

from ..decision import hmac_verify, loop_guard, replay
from ..decision.capability_gate import has_jira_credential
from ..decision.credential_resolver import CredentialResolver, DeptBotRow
from ..temporal_client import (
    TemporalClient,
    WorkflowAlreadyStartedError,
    WorkflowNotFoundError,
)

__all__ = ["router"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Replay-hash TTL for duplicate delivery protection.
_REPLAY_TTL: timedelta = timedelta(days=7)

#: Workflow type constant for ``AutomationWorkflow``.
_WORKFLOW_NAME: str = "AutomationWorkflow"

#: Temporal task queue for ``AutomationWorkflow``.
_AUTOMATION_TASK_QUEUE: str = task_queue_for(_WORKFLOW_NAME)

#: Jira sets ``X-Hub-Signature`` (``sha256=...`` envelope).
_HEADER_SIGNATURE: str = "X-Hub-Signature"

#: Supported Jira event types.
_EVENT_ISSUE_CREATED: str = "jira:issue_created"
_EVENT_ISSUE_ASSIGNED: str = "jira:issue_assigned"
_EVENT_ISSUE_UPDATED: str = "jira:issue_updated"
_EVENT_COMMENT_CREATED: str = "jira:comment_created"

#: Default acknowledgement comment posted on workflow start.  Turkish
#: by default; the department-specific ``default_language`` override
#: is applied by the activity layer if/when a non-``tr`` department is
#: added.
_ACK_COMMENT_TR: str = " Task alındı, analiz ediliyor..."

#: Comment posted as best-effort acknowledgement when Phase 1
#: capability gate denies (no Jira bot credential for the department).
_MISSING_CAPABILITY_COMMENT_TR: str = (
    " Bu departman için Jira bot credential'ı bulunamadı; "
    "otomasyon başlatılamadı."
)

#: Default Jira status names that allow restarting a workflow from a
#: ``comment_created`` event when no execution is currently running for
#: the issue. The list is
#: overridable per-department via
#: ``departments.config_json.task_status_mapping.retrigger_eligible``.
_DEFAULT_RETRIGGER_ELIGIBLE_STATUSES: tuple[str, ...] = ("To Do", "Open")

# ---------------------------------------------------------------------------
# Logger + Router
# ---------------------------------------------------------------------------

_logger = structlog.get_logger(__name__)

#: APIRouter mounted under ``/webhooks`` by ``main.py``.
#: Final URL: ``POST /webhooks/jira``.
router = APIRouter(tags=["webhooks-jira"])


#: Type of the optional best-effort ack-comment callable.  Implementations
#: typically call ``jira_add_comment_via_mcp(dept_id, issue_key, body)``.
AckCommentFn = Callable[[str, str, str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Dependency accessors (set up in main.py lifespan)
# ---------------------------------------------------------------------------


def _get_db(request: Request) -> Any:
    """Fetch the asyncpg pool bound to ``app.state.db`` at startup."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return None
    return db


def _get_temporal(request: Request) -> TemporalClient | None:
    """Fetch the TemporalClient bound to ``app.state.temporal``."""
    temporal: TemporalClient | None = getattr(request.app.state, "temporal", None)
    return temporal


def _get_creds(request: Request) -> CredentialResolver | None:
    """Fetch the CredentialResolver bound to ``app.state.creds``."""
    creds: CredentialResolver | None = getattr(request.app.state, "creds", None)
    return creds


def _get_ack_comment_fn(request: Request) -> AckCommentFn | None:
    """Return ``app.state.jira_ack_comment`` (or ``None`` if unset)."""
    fn = getattr(request.app.state, "jira_ack_comment", None)
    if fn is None:
        return None
    if not callable(fn):  # pragma: no cover - misconfiguration
        return None
    return fn  # type: ignore[no-any-return]


#: Optional callable on ``app.state.jira_fetch_issue`` that returns
#: ``(status_name, assignee_account_id)`` for a given Jira issue.  The
#: comment-restart branch uses it to read the current
#: state of an issue when ``signal_workflow`` reports
#: ``WorkflowNotFound``.  The handler treats ``None`` as "no restart
#: possible" - the request degrades gracefully to ``ignored``.
JiraFetchIssueFn = Callable[[str, str], Awaitable[tuple[str | None, str | None]]]


def _get_jira_fetch_issue_fn(request: Request) -> JiraFetchIssueFn | None:
    """Return ``app.state.jira_fetch_issue`` (or ``None`` if unset)."""
    fn = getattr(request.app.state, "jira_fetch_issue", None)
    if fn is None:
        return None
    if not callable(fn):  # pragma: no cover - misconfiguration
        return None
    return fn  # type: ignore[no-any-return]


async def _resolve_webhook_secret(
    request: Request, dept_id: str | None
) -> bytes | None:
    """Resolve the Jira webhook secret for *dept_id*.

    Looks up ``app.state.jira_webhook_secret_resolver`` first (an async
    callable from Vault), then falls back to a process-wide
    ``app.state.jira_webhook_secret`` for the dev-loop scenario.

    The resolver is expected to map ``dept_id`` to one of:

    * a real department id (e.g. ``"payment"``)
      ``secret/webhook/{dept_id}/secret`` in Vault,
    * the sentinel ``"__global__"``
      ``secret/webhook/global/secret`` in Vault (two-stage fallback),
    * ``None`` (legacy, single-secret callers)  process-wide default.

    Returns ``None`` when neither resolver nor fallback yields a value
    (the handler maps that to either a 503 or the next-stage probe).
    """
    resolver = getattr(request.app.state, "jira_webhook_secret_resolver", None)
    if resolver is not None:
        secret = await resolver(dept_id)
        if isinstance(secret, str):
            return secret.encode("utf-8")
        return bytes(secret) if secret is not None else None

    fallback = getattr(request.app.state, "jira_webhook_secret", None)
    if fallback is None:
        return None
    if isinstance(fallback, str):
        return fallback.encode("utf-8")
    return bytes(fallback)


async def _resolve_webhook_secret_two_stage(
    request: Request, db: Any, payload: dict[str, Any]
) -> tuple[bytes | None, str | None, str]:
    """Two-stage HMAC secret lookup (per-dept then global).

    Stage 1 - best-effort body parse for ``project_key``
    ``automation.department_project_keys`` lookup  resolver call with
    the resolved ``dept_id``. The body parse happens *before* HMAC
    verification, so any parse failure is silently absorbed and we fall
    through to the global secret.

    Stage 2 - resolver call with the sentinel ``"__global__"``
    (Vault path ``secret/webhook/global/secret``) used when stage 1
    cannot resolve a dept-specific secret.

    Returns ``(secret_bytes, dept_id, source)`` where ``source`` is one
    of:

    * ``"dept"``  - stage 1 succeeded; ``dept_id`` is the resolved id.
      The handler MUST NOT fall back to global when the dept secret
      yields a verify failure (security isolation: a dept's misuse
      can't be laundered through the global secret).
    * ``"global"`` - stage 2 succeeded; ``dept_id`` is ``None``.
    * ``"missing"`` - neither stage produced a secret; ``dept_id`` is
      ``None`` and the handler returns 503.
    """
    dept_id_resolved: str | None = None
    project_key = _get_project_key(payload)
    if project_key:
        try:
            dept_id_resolved = await _resolve_dept_id(db, project_key)
        except Exception:  # noqa: BLE001 - best-effort dept resolve
            _logger.exception(
                "two_stage_hmac_dept_resolve_failed",
                project_key=project_key,
            )
            dept_id_resolved = None

    # Stage 1: dept-specific secret (only when dept resolved cleanly).
    if dept_id_resolved is not None:
        secret = await _resolve_webhook_secret(
            request, dept_id=dept_id_resolved
        )
        if secret is not None:
            return secret, dept_id_resolved, "dept"

    # Stage 2: global fallback. The ``"__global__"`` sentinel keeps the
    # resolver contract uniform with stage 1 - callers that still pass
    # ``dept_id=None`` (legacy / dev-loop) keep working because the
    # resolver-less branch falls back to ``app.state.jira_webhook_secret``.
    global_secret = await _resolve_webhook_secret(
        request, dept_id="__global__"
    )
    if global_secret is not None:
        return global_secret, None, "global"

    return None, None, "missing"


# ---------------------------------------------------------------------------
# Payload extraction helpers (pure)
# ---------------------------------------------------------------------------


def _get_actor_id(payload: dict[str, Any]) -> str | None:
    """Extract ``user.accountId`` from a Jira webhook payload."""
    user = payload.get("user")
    if isinstance(user, dict):
        account_id = user.get("accountId")
        if isinstance(account_id, str):
            return account_id
    return None


def _get_issue(payload: dict[str, Any]) -> dict[str, Any]:
    issue = payload.get("issue")
    return issue if isinstance(issue, dict) else {}


def _get_issue_key(payload: dict[str, Any]) -> str | None:
    issue = _get_issue(payload)
    key = issue.get("key")
    return key if isinstance(key, str) else None


def _get_project_key(payload: dict[str, Any]) -> str | None:
    issue = _get_issue(payload)
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        return None
    project = fields.get("project")
    if not isinstance(project, dict):
        return None
    key = project.get("key")
    return key if isinstance(key, str) else None


def _get_assignee_id(payload: dict[str, Any]) -> str | None:
    issue = _get_issue(payload)
    fields = issue.get("fields")
    if not isinstance(fields, dict):
        return None
    assignee = fields.get("assignee")
    if not isinstance(assignee, dict):
        return None
    account_id = assignee.get("accountId")
    return account_id if isinstance(account_id, str) else None


def _get_changelog(payload: dict[str, Any]) -> dict[str, Any] | None:
    changelog = payload.get("changelog")
    return changelog if isinstance(changelog, dict) else None


# ---------------------------------------------------------------------------
# Postgres / Vault lookups
# ---------------------------------------------------------------------------


async def _resolve_dept_id(
    db: Any,
    project_key: str,
) -> str | None:
    """Resolve ``department_id`` from the issue's ``project_key``.

    Reads ``automation.department_project_keys`` (UNIQUE on
    ``project_key``).  Returns ``None`` if no mapping exists.
    """
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT department_id
            FROM automation.department_project_keys
            WHERE project_key = $1
            """,
            project_key,
        )
    if row is None:
        return None
    return str(row["department_id"])


async def _jira_bot_account_ids(creds: CredentialResolver) -> frozenset[str]:
    """Return the frozen set of all Jira bot ``account_id`` values."""
    bots: list[DeptBotRow] = await creds.list_dept_bots()
    return frozenset(
        str(b.account_id) for b in bots if b.service == "jira" and b.account_id
    )


async def _jira_bot_account_ids_for_dept(
    creds: CredentialResolver, dept_id: str
) -> frozenset[str]:
    """Return the Jira bot ``account_id`` values for one department.

    Used by the ``comment_created`` restart branch so the eligibility
    check is scoped to the dept that owns the
    issue's project key - a cross-dept bot must not trigger a restart
    on another dept's issue.
    """
    bots: list[DeptBotRow] = await creds.list_dept_bots()
    return frozenset(
        str(b.account_id)
        for b in bots
        if b.service == "jira" and b.account_id and b.department_id == dept_id
    )


async def _resolve_dept_for_issue_key(
    db: Any, issue_key: str
) -> str | None:
    """Resolve ``department_id`` from a Jira issue key (``PROJ-123``
    ``payment``).

    Reads ``automation.department_project_keys`` (UNIQUE on
    ``project_key``) using the issue key's project prefix. Returns
    ``None`` when no mapping exists, when the issue key is malformed,
    or when the database raises.
    """
    if "-" not in issue_key:
        return None
    project_key = issue_key.rsplit("-", 1)[0]
    if not project_key:
        return None
    try:
        return await _resolve_dept_id(db, project_key)
    except Exception:  # noqa: BLE001 - graceful fallback to "ignored"
        return None


async def _retrigger_eligible_statuses(
    db: Any, dept_id: str
) -> frozenset[str]:
    """Return the set of Jira status names that allow a comment-driven
    workflow restart for *dept_id*.

    Reads ``automation.departments.config_json.task_status_mapping
    .retrigger_eligible``; falls back to
    ``_DEFAULT_RETRIGGER_ELIGIBLE_STATUSES`` (``"To Do"``, ``"Open"``)
    when the column is missing, the JSON path is absent, or the value
    is malformed (non-list, non-string entries are dropped).
    """
    default = frozenset(_DEFAULT_RETRIGGER_ELIGIBLE_STATUSES)
    try:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT config_json
                FROM automation.departments
                WHERE id = $1
                """,
                dept_id,
            )
    except Exception:  # noqa: BLE001 - graceful fallback
        return default
    if row is None:
        return default
    config = row["config_json"]
    # asyncpg returns ``jsonb`` columns as already-decoded Python
    # objects in production; some test fakes pass through a raw string.
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (ValueError, TypeError):
            return default
    if not isinstance(config, dict):
        return default
    mapping = config.get("task_status_mapping")
    if not isinstance(mapping, dict):
        return default
    raw = mapping.get("retrigger_eligible")
    if not isinstance(raw, list):
        return default
    cleaned = frozenset(s for s in raw if isinstance(s, str) and s)
    return cleaned if cleaned else default


async def _insert_work_item(
    db: Any,
    *,
    workflow_id: str,
    department_id: str,
    issue_key: str,
) -> bool:
    """Insert a ``work_items`` row in ``pending`` state.

    ``workflow_id`` is ``UNIQUE`` so the ``ON CONFLICT DO NOTHING``
    clause makes the insert idempotent - returns ``True`` when a new
    row was created, ``False`` when it already existed.
    """
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO automation.work_items
                (workflow_id, department_id, issue_key, status)
            VALUES ($1, $2, $3, 'pending')
            ON CONFLICT (workflow_id) DO NOTHING
            RETURNING id
            """,
            workflow_id,
            department_id,
            issue_key,
        )
    return row is not None


# ---------------------------------------------------------------------------
# Audit-log helper
# ---------------------------------------------------------------------------


def _audit(event_name: str, **fields: Any) -> None:
    """Emit a single structured audit-log entry.

    Field names follow the webhook audit schema.  ``None`` values are
    silently dropped so the JSON output is compact and stable.  The
    helper's first positional parameter is named ``event_name`` because
    structlog reserves ``event`` for the log message slot - call sites
    therefore use ``event_type=...`` for the original Atlassian event
    string.
    """
    payload = {k: v for k, v in fields.items() if v is not None}
    _logger.info(event_name, **payload)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/jira")
async def post_jira_webhook(request: Request) -> JSONResponse:  # noqa: PLR0911, PLR0912, PLR0915
    """Handle ``POST /webhooks/jira`` - see module docstring for the chain."""

    # ---- (a) raw body --------------------------------------------------
    raw_body: bytes = await request.body()
    signature = request.headers.get(_HEADER_SIGNATURE, "") or ""

    # Check infrastructure dependencies up front; structuring 503s here
    # mirrors the Bitbucket handler's webhook error responses.
    db = _get_db(request)
    if db is None:
        _audit("webhook_db_error", source="jira")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "db_unavailable"},
        )
    temporal = _get_temporal(request)
    if temporal is None:
        _audit("webhook_temporal_error", source="jira")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "temporal_unavailable"},
        )
    creds = _get_creds(request)
    if creds is None:
        _audit("webhook_vault_error", source="jira")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "vault_unavailable"},
        )

    ack_comment_fn = _get_ack_comment_fn(request)

    # ---- (b) two-stage HMAC verify -------------------------------------
    # Best-effort parse first - we need ``project_key`` to look up the
    # dept-specific secret (Stage 1) BEFORE HMAC verify. Any parse
    # error/non-dict body silently degrades to the global secret
    # (Stage 2). The "real" parse error response is still emitted
    # later, after HMAC has authenticated the request.
    pre_verify_payload: dict[str, Any] = {}
    try:
        pre_decoded = json.loads(raw_body or b"{}")
        if isinstance(pre_decoded, dict):
            pre_verify_payload = pre_decoded
    except (ValueError, TypeError):
        pre_verify_payload = {}
    except Exception:  # noqa: BLE001 - never break verify on parse
        pre_verify_payload = {}

    secret, hmac_dept_id, secret_source = await _resolve_webhook_secret_two_stage(
        request, db, pre_verify_payload
    )
    if secret is None:
        _audit(
            "webhook_vault_error",
            source="jira",
            reason="webhook_secret_missing",
            secret_source=secret_source,
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "vault_unavailable"},
        )

    if not hmac_verify.verify(raw_body, signature, secret):
        _audit(
            "webhook_hmac_failed",
            source="jira",
            secret_source=secret_source,
            department_id=hmac_dept_id,
        )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"status": "unauthorized"},
        )

    # Parse JSON.  Real Atlassian deliveries are always valid JSON; for
    # malformed payloads we return a quiet 400 response.
    try:
        decoded = json.loads(raw_body or b"{}")
    except (ValueError, TypeError):
        _audit("webhook_bad_request", source="jira", reason="json_parse_error")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "bad_request"},
        )
    if not isinstance(decoded, dict):
        _audit(
            "webhook_bad_request", source="jira", reason="payload_not_object"
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "bad_request"},
        )
    payload: dict[str, Any] = decoded

    raw_event_type = payload.get("webhookEvent")
    if not isinstance(raw_event_type, str) or not raw_event_type:
        _audit(
            "webhook_bad_request", source="jira", reason="missing_event_type"
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "bad_request"},
        )
    event_type: str = raw_event_type

    # ---- (c) replay guard ----------------------------------------------
    payload_hash = replay.compute_payload_hash(raw_body)
    if not await replay.check_and_insert(db, payload_hash, _REPLAY_TTL):
        _audit(
            "webhook_replay_skipped",
            source="jira",
            event_type=event_type,
            payload_hash=payload_hash,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "duplicate", "action": "skipped"},
        )

    # ---- (d) loop guard (self-actor) -----------------------------------
    bot_account_ids = await _jira_bot_account_ids(creds)
    actor_id = _get_actor_id(payload)
    if loop_guard.is_self_actor(actor_id, bot_account_ids):
        _audit(
            "webhook_loop_guard_skipped",
            source="jira",
            event_type=event_type,
            actor_id=actor_id,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "loop_guard", "action": "skipped"},
        )

    # ---- (e) event-type classification ---------------------------------
    if loop_guard.route(event_type) == "ignored":
        _audit(
            "webhook_event_ignored",
            source="jira",
            event_type=event_type,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ignored", "reason": "unsupported_event"},
        )

    issue_key = _get_issue_key(payload)
    if issue_key is None:
        _audit(
            "webhook_bad_request",
            source="jira",
            event_type=event_type,
            reason="missing_issue_key",
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "bad_request"},
        )

    # ---- (f) jira:comment_created  signal existing workflow -----------
    if event_type == _EVENT_COMMENT_CREATED:
        try:
            workflow_id = automation_workflow_id_jira(issue_key)
        except InvalidIssueKeyError:
            _audit(
                "webhook_bad_request",
                source="jira",
                event_type=event_type,
                reason="invalid_issue_key",
                issue_key=issue_key,
            )
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"status": "bad_request"},
            )

        return await _handle_comment_created(
            temporal=temporal,
            workflow_id=workflow_id,
            payload=payload,
            issue_key=issue_key,
            db=db,
            creds=creds,
            jira_fetch_issue=_get_jira_fetch_issue_fn(request),
        )

    # ---- (g)/(h) bot-assignee predicate --------------------------------
    if event_type in (_EVENT_ISSUE_CREATED, _EVENT_ISSUE_ASSIGNED):
        assignee_id = _get_assignee_id(payload)
        if not loop_guard.is_bot_assignee(assignee_id, bot_account_ids):
            _audit(
                "webhook_not_bot_assignee",
                source="jira",
                event_type=event_type,
                issue_key=issue_key,
                assignee_id=assignee_id,
            )
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"status": "not_bot_assignee", "action": "skipped"},
            )
    elif event_type == _EVENT_ISSUE_UPDATED:
        changelog = _get_changelog(payload)
        if not loop_guard.assignee_changed_to_bot(changelog, bot_account_ids):
            _audit(
                "webhook_not_bot_assignee",
                source="jira",
                event_type=event_type,
                issue_key=issue_key,
                reason="assignee_change_not_to_bot",
            )
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={"status": "not_bot_assignee", "action": "skipped"},
            )
    else:  # pragma: no cover - guarded by route() above
        _audit(
            "webhook_event_ignored",
            source="jira",
            event_type=event_type,
            reason="unhandled_event_type",
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ignored", "reason": "unsupported_event"},
        )

    # ---- (i) Phase 1 capability gate -----------------------------------
    project_key = _get_project_key(payload)
    if project_key is None:
        _audit(
            "webhook_bad_request",
            source="jira",
            event_type=event_type,
            issue_key=issue_key,
            reason="missing_project_key",
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "bad_request"},
        )

    dept_id = await _resolve_dept_id(db, project_key)
    if dept_id is None:
        _audit(
            "capability_denied_phase1",
            source="jira",
            event_type=event_type,
            issue_key=issue_key,
            project_key=project_key,
            reason="no_department_for_project_key",
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "missing_capability", "missing": ["jira"]},
        )

    if not await has_jira_credential(db, dept_id):
        _audit(
            "capability_denied_phase1",
            source="jira",
            event_type=event_type,
            issue_key=issue_key,
            department_id=dept_id,
            reason="missing_jira_credential",
        )
        # Best-effort ack comment in Turkish.
        if ack_comment_fn is not None:
            try:
                await ack_comment_fn(
                    dept_id, issue_key, _MISSING_CAPABILITY_COMMENT_TR
                )
            except Exception as exc:  # noqa: BLE001 - best-effort
                _audit(
                    "ack_comment_failed",
                    source="jira",
                    issue_key=issue_key,
                    department_id=dept_id,
                    reason="missing_capability_ack",
                    error=type(exc).__name__,
                )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "missing_capability", "missing": ["jira"]},
        )

    # ---- (j) work_items insert + (k) workflow start --------------------
    try:
        workflow_id = automation_workflow_id_jira(issue_key)
    except InvalidIssueKeyError:
        _audit(
            "webhook_bad_request",
            source="jira",
            event_type=event_type,
            reason="invalid_issue_key",
            issue_key=issue_key,
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "bad_request"},
        )

    inserted = await _insert_work_item(
        db,
        workflow_id=workflow_id,
        department_id=dept_id,
        issue_key=issue_key,
    )

    workflow_input = {
        "trigger": "jira",
        "event_type": event_type,
        "issue_key": issue_key,
        "project_key": project_key,
        "department_id": dept_id,
    }
    try:
        await temporal.start_workflow(
            workflow_type=_WORKFLOW_NAME,
            workflow_id=workflow_id,
            task_queue=_AUTOMATION_TASK_QUEUE,
            args=[workflow_input],
        )
    except WorkflowAlreadyStartedError:
        _audit(
            "webhook_workflow_already_started",
            source="jira",
            event_type=event_type,
            issue_key=issue_key,
            workflow_id=workflow_id,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "duplicate",
                "reason": "workflow_running",
                "workflow_id": workflow_id,
            },
        )

    _audit(
        "webhook_accepted",
        source="jira",
        event_type=event_type,
        issue_key=issue_key,
        department_id=dept_id,
        workflow_id=workflow_id,
        work_item_inserted=inserted,
    )

    # ---- (l) ack comment (best-effort) ---------------------------------
    if ack_comment_fn is not None:
        try:
            await ack_comment_fn(dept_id, issue_key, _ACK_COMMENT_TR)
        except Exception as exc:  # noqa: BLE001 - best-effort
            _audit(
                "ack_comment_failed",
                source="jira",
                issue_key=issue_key,
                department_id=dept_id,
                reason="task_received_ack",
                error=type(exc).__name__,
            )

    # ---- (m) success ---------------------------------------------------
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "accepted", "workflow_id": workflow_id},
    )


# ---------------------------------------------------------------------------
# Per-event handlers
# ---------------------------------------------------------------------------


async def _handle_comment_created(
    *,
    temporal: TemporalClient,
    workflow_id: str,
    payload: dict[str, Any],
    issue_key: str,
    db: Any,
    creds: CredentialResolver,
    jira_fetch_issue: JiraFetchIssueFn | None,
) -> JSONResponse:
    """Handle ``jira:comment_created``.

    Fast path: forward a minimal ``new_comment`` payload to the
    existing ``AutomationWorkflow`` via :meth:`signal_workflow`. The
    happy-path response remains 200 ``signal_forwarded``.

    Restart branch: when Temporal raises :class:`WorkflowNotFoundError`
    (no execution exists for the issue's workflow id), we look up the
    issue's current Jira state.
    If the status is in the dept's ``retrigger_eligible`` set
    (default ``["To Do", "Open"]``) **and** the assignee is one of
    the dept's registered Jira bots, we ``signal_with_start`` a fresh
    ``AutomationWorkflow`` with the same workflow id and forward the
    comment as the ``new_comment`` start signal - 200
    ``restarted``. Any other shape (different status, non-bot
    assignee, missing dept config, fetch failure) degrades silently
    to 200 ``ignored`` (``comment_ignored_no_pending_workflow``).
    Generic transport / RPC failures from the initial ``signal_workflow``
    fall back to the legacy 200 ``no_active_workflow`` branch - they
    are best-effort.
    """
    comment = payload.get("comment")
    body_text: str = ""
    author_id: str | None = None
    if isinstance(comment, dict):
        body = comment.get("body")
        if isinstance(body, str):
            body_text = body
        author = comment.get("author")
        if isinstance(author, dict):
            account_id = author.get("accountId")
            if isinstance(account_id, str):
                author_id = account_id

    signal_payload = {
        "source": "jira",
        "event_type": _EVENT_COMMENT_CREATED,
        "issue_key": issue_key,
        "text": body_text,
        "author_id": author_id,
    }

    try:
        await temporal.signal_workflow(
            workflow_id=workflow_id,
            signal_name="new_comment",
            payload=signal_payload,
        )
    except WorkflowNotFoundError:
        # No execution exists for this issue  consider restart.
        return await _maybe_restart_workflow_from_comment(
            temporal=temporal,
            workflow_id=workflow_id,
            payload=payload,
            issue_key=issue_key,
            signal_payload=signal_payload,
            db=db,
            creds=creds,
            jira_fetch_issue=jira_fetch_issue,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort signal forward
        _audit(
            "webhook_signal_failed",
            source="jira",
            event_type=_EVENT_COMMENT_CREATED,
            issue_key=issue_key,
            workflow_id=workflow_id,
            error=type(exc).__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ignored", "reason": "no_active_workflow"},
        )

    _audit(
        "webhook_signal_forwarded",
        source="jira",
        event_type=_EVENT_COMMENT_CREATED,
        issue_key=issue_key,
        workflow_id=workflow_id,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "signal_forwarded", "workflow_id": workflow_id},
    )


async def _maybe_restart_workflow_from_comment(
    *,
    temporal: TemporalClient,
    workflow_id: str,
    payload: dict[str, Any],
    issue_key: str,
    signal_payload: dict[str, Any],
    db: Any,
    creds: CredentialResolver,
    jira_fetch_issue: JiraFetchIssueFn | None,
) -> JSONResponse:
    """Decide whether a comment on an issue with no live workflow
    should restart one.

    The decision is intentionally conservative - every "unknown" or
    "missing data" branch falls through to ``ignored`` so we never
    spawn a workflow we can't justify.
    """

    actor_account_id: str | None = None
    author = (payload.get("comment") or {}).get("author")
    if isinstance(author, dict):
        candidate = author.get("accountId")
        if isinstance(candidate, str):
            actor_account_id = candidate

    def _ignored(
        reason: str, *, current_status: str | None = None
    ) -> JSONResponse:
        _audit(
            "comment_ignored_no_pending_workflow",
            source="jira",
            event_type=_EVENT_COMMENT_CREATED,
            issue_key=issue_key,
            workflow_id=workflow_id,
            actor_account_id=actor_account_id,
            current_status=current_status,
            reason=reason,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ignored", "reason": "no_pending_workflow"},
        )

    # 1. Resolve the dept that owns this issue's project key.
    dept_id = await _resolve_dept_for_issue_key(db, issue_key)
    if dept_id is None:
        return _ignored("dept_unresolved")

    # 2. Pull the issue's current status + assignee from Jira.
    if jira_fetch_issue is None:
        return _ignored("jira_fetch_unavailable")
    try:
        current_status, assignee_id = await jira_fetch_issue(issue_key, dept_id)
    except Exception as exc:  # noqa: BLE001 - graceful fallback
        _audit(
            "comment_ignored_no_pending_workflow",
            source="jira",
            event_type=_EVENT_COMMENT_CREATED,
            issue_key=issue_key,
            workflow_id=workflow_id,
            actor_account_id=actor_account_id,
            current_status=None,
            reason="jira_fetch_failed",
            error=type(exc).__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ignored", "reason": "no_pending_workflow"},
        )

    if not current_status:
        return _ignored("status_unknown")

    # 3. Status must be in the dept's retrigger_eligible set.
    eligible = await _retrigger_eligible_statuses(db, dept_id)
    if current_status not in eligible:
        return _ignored(
            "status_not_retrigger_eligible", current_status=current_status
        )

    # 4. Assignee must be one of *this dept's* Jira bots.
    if not assignee_id:
        return _ignored("assignee_missing", current_status=current_status)
    bot_ids = await _jira_bot_account_ids_for_dept(creds, dept_id)
    if assignee_id not in bot_ids:
        return _ignored(
            "assignee_not_dept_bot", current_status=current_status
        )

    # 5. All gates passed  atomically (re)start the workflow with
    # the comment buffered as the first signal.
    workflow_input = {
        "trigger": "jira",
        "event_type": _EVENT_COMMENT_CREATED,
        "issue_key": issue_key,
        "department_id": dept_id,
        "restart_from_comment": True,
    }
    try:
        await temporal.signal_with_start(
            workflow_type=_WORKFLOW_NAME,
            workflow_id=workflow_id,
            task_queue=_AUTOMATION_TASK_QUEUE,
            signal_name="new_comment",
            signal_payload=signal_payload,
            args=[workflow_input],
        )
    except Exception as exc:  # noqa: BLE001 - best-effort restart
        _audit(
            "comment_ignored_no_pending_workflow",
            source="jira",
            event_type=_EVENT_COMMENT_CREATED,
            issue_key=issue_key,
            workflow_id=workflow_id,
            actor_account_id=actor_account_id,
            current_status=current_status,
            reason="signal_with_start_failed",
            error=type(exc).__name__,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ignored", "reason": "no_pending_workflow"},
        )

    _audit(
        "workflow_restarted_from_comment",
        source="jira",
        event_type=_EVENT_COMMENT_CREATED,
        issue_key=issue_key,
        workflow_id=workflow_id,
        actor_account_id=actor_account_id,
        current_status=current_status,
        department_id=dept_id,
    )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "restarted", "workflow_id": workflow_id},
    )
