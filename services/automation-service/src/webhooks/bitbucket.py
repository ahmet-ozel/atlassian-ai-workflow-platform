"""Bitbucket webhook handler — sequential guard chain → Temporal dispatch.

Implements the ``POST /webhooks/bitbucket`` endpoint with the same
ordered guard chain as ``webhooks/jira.py`` (each step short-circuits
with the appropriate HTTP response and emits a ``structlog`` JSON
audit entry; see Error Handling §5.1):

  (a) Read the raw request body.
  (b) ``hmac_verify.verify(...)`` — 401 ``unauthorized`` on failure.
  (c) ``replay.check_and_insert(...)`` — 200 ``duplicate`` on dup.
  (d) ``loop_guard.is_self_actor(...)`` — 200 ``loop_guard`` on self.
  (e) Classify event type via ``loop_guard.route(...)`` — 200 ``ignored``
      for unsupported event types.
  (f) ``pullrequest:reviewer_added`` → reviewer is bot?  If not, 200
      ``not_bot_reviewer``.  Otherwise:
        - INSERT INTO ``automation.work_items`` with status ``pending``.
        - ``temporal.start_workflow("AutomationWorkflow", ...)`` with
          idempotent ID ``automation_workflow_id_bb(workspace, repo, pr_id)``
          and ``workflow_type='pr_review'``.
        - 200 ``{"status": "accepted", "workflow_id": "..."}``.
  (g) ``pullrequest:comment_created`` → ``temporal.signal_workflow(...)``
      ``new_comment`` signal forwarded to the existing PR-review
      workflow → 200 ``comment_signaled`` (or ``ignored`` when no
      workflow is running for this PR).

A ``WorkflowAlreadyStartedError`` raised from the workflow start
collapses to a 200 ``duplicate`` with the same ``workflow_id``
(Temporal native idempotency on top of the SHA-256 replay guard).

Validates Requirements: 3.1, 3.2, 3.3, 3.4, 3.5
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Header, Request, Response

from temporal_shared.identifiers import (
    InvalidSlugError,
    automation_workflow_id_bb,
)
from temporal_shared.workflow_registry import task_queue_for

from ..decision import hmac_verify, loop_guard, replay
from ..decision.credential_resolver import CredentialResolver, DeptBotRow
from ..temporal_client import TemporalClient, WorkflowAlreadyStartedError

__all__ = ["router"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default replay-guard TTL (matches the 7-day policy in Requirement 1.6).
_REPLAY_TTL: timedelta = timedelta(days=7)

#: Temporal workflow type name.
_AUTOMATION_WORKFLOW_TYPE: str = "AutomationWorkflow"

#: Temporal task queue for the top-level ``AutomationWorkflow``.
_AUTOMATION_TASK_QUEUE: str = task_queue_for(_AUTOMATION_WORKFLOW_TYPE)

#: Bitbucket Cloud event type strings.
_EVENT_REVIEWER_ADDED: str = "pullrequest:reviewer_added"
_EVENT_COMMENT_CREATED: str = "pullrequest:comment_created"

_log = structlog.get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Dependency provider placeholders
# ---------------------------------------------------------------------------
#
# Mirrors the pattern used by ``webhooks/jira.py``: the actual
# ``asyncpg.Pool`` / ``TemporalClient`` / ``CredentialResolver`` instances
# do not exist at import time, so each ``Depends`` placeholder raises
# unless ``main.py`` overrides it via ``app.dependency_overrides`` once
# the lifespan handler has built the resources (task 5.3).


def get_db_pool() -> asyncpg.Pool:  # pragma: no cover - replaced by app
    raise RuntimeError(
        "asyncpg.Pool dependency not configured. "
        "Override `get_db_pool` via FastAPI dependency_overrides."
    )


def get_temporal_client() -> TemporalClient:  # pragma: no cover
    raise RuntimeError(
        "TemporalClient dependency not configured. "
        "Override `get_temporal_client` via FastAPI dependency_overrides."
    )


def get_credential_resolver() -> CredentialResolver:  # pragma: no cover
    raise RuntimeError(
        "CredentialResolver dependency not configured. "
        "Override `get_credential_resolver` via FastAPI dependency_overrides."
    )


def get_webhook_secret() -> bytes:  # pragma: no cover
    raise RuntimeError(
        "Webhook secret dependency not configured. "
        "Override `get_webhook_secret` via FastAPI dependency_overrides."
    )


def get_bot_account_ids() -> frozenset[str]:  # pragma: no cover
    """Return the union of all Bitbucket bot ``account_id`` values.

    The default override pulls this from
    :meth:`CredentialResolver.list_dept_bots`, filtered to
    ``service == "bitbucket"``.  Returning a frozen set keeps the
    fast-path ``loop_guard.is_self_actor`` lookup O(1).
    """
    raise RuntimeError(
        "Bot registry dependency not configured. "
        "Override `get_bot_account_ids` via FastAPI dependency_overrides."
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _json_response(body: dict[str, Any], status: int = 200) -> Response:
    """Return a ``Response`` whose body is canonical JSON.

    Mirrors ``webhooks.jira._json_response`` so that audit-traffic
    captured at a reverse proxy can be diffed byte-for-byte across both
    handlers.
    """

    payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    return Response(
        content=payload,
        status_code=status,
        media_type="application/json",
    )


def _safe_json_loads(raw: bytes) -> dict[str, Any] | None:
    """Return parsed JSON object, or ``None`` if invalid / not an object."""
    try:
        decoded = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


# ---------------------------------------------------------------------------
# Pure payload-extraction helpers
# ---------------------------------------------------------------------------


def _extract_repository_slugs(
    payload: dict[str, Any],
) -> tuple[str, str] | None:
    """Return ``(workspace_slug, repo_slug)`` from a Bitbucket payload.

    Bitbucket Cloud payloads have ``repository.workspace.slug`` and
    ``repository.name``; ``repository.full_name`` (``"workspace/repo"``)
    is also commonly present.  Both shapes are supported.  Returns
    ``None`` when neither shape yields a usable pair (the handler then
    treats the event as ignorable rather than crashing).
    """
    repo = payload.get("repository")
    if not isinstance(repo, dict):
        return None

    workspace_obj = repo.get("workspace")
    workspace = (
        workspace_obj.get("slug")
        if isinstance(workspace_obj, dict)
        else None
    )
    name = repo.get("name") or repo.get("slug")

    full_name = repo.get("full_name")
    if (not workspace or not name) and isinstance(full_name, str) and "/" in full_name:
        ws_part, _, name_part = full_name.partition("/")
        workspace = workspace or ws_part
        name = name or name_part

    if not isinstance(workspace, str) or not isinstance(name, str):
        return None
    if not workspace or not name:
        return None
    return workspace.lower(), name.lower()


def _extract_pr_id(payload: dict[str, Any]) -> int | None:
    """Return ``pullrequest.id`` as ``int`` (``None`` when absent)."""
    pr = payload.get("pullrequest")
    if not isinstance(pr, dict):
        return None
    pr_id = pr.get("id")
    if isinstance(pr_id, bool):  # ``bool`` is an ``int`` subclass
        return None
    if isinstance(pr_id, int):
        return pr_id if pr_id > 0 else None
    if isinstance(pr_id, str) and pr_id.isdigit():
        as_int = int(pr_id)
        return as_int if as_int > 0 else None
    return None


def _extract_actor_account_id(payload: dict[str, Any]) -> str | None:
    """Return the actor's Bitbucket ``account_id`` (or ``None``)."""
    actor = payload.get("actor")
    if not isinstance(actor, dict):
        return None
    for key in ("account_id", "accountId", "uuid"):
        value = actor.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _extract_reviewer_account_id(payload: dict[str, Any]) -> str | None:
    """Return the added reviewer's ``account_id``.

    ``pullrequest:reviewer_added`` payloads carry the new reviewer
    under ``reviewer`` (single user) — fall back to the last entry of
    ``pullrequest.reviewers`` if necessary.
    """
    reviewer = payload.get("reviewer")
    if isinstance(reviewer, dict):
        for key in ("account_id", "accountId", "uuid"):
            value = reviewer.get(key)
            if isinstance(value, str) and value:
                return value

    pr = payload.get("pullrequest")
    if isinstance(pr, dict):
        reviewers = pr.get("reviewers")
        if isinstance(reviewers, list) and reviewers:
            last = reviewers[-1]
            if isinstance(last, dict):
                for key in ("account_id", "accountId", "uuid"):
                    value = last.get(key)
                    if isinstance(value, str) and value:
                        return value
    return None


# ---------------------------------------------------------------------------
# Postgres lookups
# ---------------------------------------------------------------------------


async def _resolve_department_for_repo(
    db: asyncpg.Pool, workspace: str, repo: str
) -> str | None:
    """Resolve ``department_id`` for a ``(workspace, repo)`` pair.

    Reads ``automation.repo_mappings`` (case-insensitive match).
    Returns ``None`` when the repo is not mapped to any department.
    """
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT department_id
            FROM automation.repo_mappings
            WHERE lower(bitbucket_workspace) = $1
              AND lower(bitbucket_repo) = $2
            """,
            workspace,
            repo,
        )
    if row is None:
        return None
    return str(row["department_id"])


async def _dept_bitbucket_account_id(
    creds: CredentialResolver, dept_id: str
) -> str | None:
    """Return the Bitbucket bot ``account_id`` for *dept_id* (or ``None``).

    Uses the cached ``list_dept_bots()`` result.
    """
    bots: list[DeptBotRow] = await creds.list_dept_bots()
    for bot in bots:
        if bot.department_id == dept_id and bot.service == "bitbucket":
            return bot.account_id
    return None


async def _insert_work_item(
    db: asyncpg.Pool,
    *,
    workflow_id: str,
    department_id: str,
    issue_key: str,
    workflow_type: str,
) -> None:
    """Insert a ``work_items`` row in ``pending`` state.

    The ``workflow_id`` column is ``UNIQUE`` so duplicate inserts (which
    can happen if the replay guard is bypassed for any reason) collapse
    to a no-op via ``ON CONFLICT DO NOTHING``.
    """
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO automation.work_items
                (workflow_id, department_id, issue_key, status, workflow_type)
            VALUES ($1, $2, $3, 'pending', $4)
            ON CONFLICT (workflow_id) DO NOTHING
            """,
            workflow_id,
            department_id,
            issue_key,
            workflow_type,
        )


def _extract_comment_signal_payload(
    payload: dict[str, Any],
    *,
    workspace: str,
    repo: str,
    pr_id: int,
    department_id: str,
) -> dict[str, Any]:
    """Build a minimal, JSON-serialisable ``new_comment`` signal payload.

    Mirrors the shape used by the Jira handler so the workflow
    ``new_comment`` signal handler can consume both sources uniformly:
    ``{"text": str, "author_id": str | None, "event_type": str, ...}``.
    """
    comment = payload.get("comment")
    body_text: str | None = None
    author_id: str | None = None
    comment_id: Any = None
    if isinstance(comment, dict):
        comment_id = comment.get("id")
        content = comment.get("content")
        if isinstance(content, dict):
            raw = content.get("raw")
            if isinstance(raw, str):
                body_text = raw
        if body_text is None:
            text = comment.get("text")
            if isinstance(text, str):
                body_text = text
        user = comment.get("user")
        if isinstance(user, dict):
            for key in ("account_id", "accountId", "uuid"):
                value = user.get(key)
                if isinstance(value, str) and value:
                    author_id = value
                    break

    return {
        "text": body_text or "",
        "author_id": author_id,
        "event_type": _EVENT_COMMENT_CREATED,
        "source": "bitbucket",
        "workspace": workspace,
        "repo": repo,
        "pr_id": pr_id,
        "comment_id": comment_id,
        "department_id": department_id,
    }


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/bitbucket")
async def handle_bitbucket_webhook(  # noqa: PLR0911 - sequential guard chain
    request: Request,
    x_hub_signature: str | None = Header(default=None, alias="X-Hub-Signature"),
    x_event_key: str | None = Header(default=None, alias="X-Event-Key"),
    db: asyncpg.Pool = Depends(get_db_pool),
    temporal: TemporalClient = Depends(get_temporal_client),
    creds: CredentialResolver = Depends(get_credential_resolver),
    webhook_secret: bytes = Depends(get_webhook_secret),
    bot_account_ids: frozenset[str] = Depends(get_bot_account_ids),
) -> Response:
    """Receive a Bitbucket webhook event and dispatch it through the chain."""

    log = _log.bind(component="webhook.bitbucket")

    # ----- (a) raw body --------------------------------------------------
    raw_body: bytes = await request.body()

    # ----- (b) HMAC verification (Requirement 3.1, 3.2) -----------------
    if not hmac_verify.verify(raw_body, x_hub_signature or "", webhook_secret):
        log.warning(
            "webhook_hmac_failed",
            decision="reject",
            reason="hmac_mismatch",
        )
        return _json_response({"status": "unauthorized"}, status=401)

    # Parse JSON.  Real Atlassian deliveries are always valid JSON; for
    # malformed payloads we return 400 (silent — see design §5.1).
    payload = _safe_json_loads(raw_body)
    if payload is None:
        log.warning(
            "webhook_bad_request",
            decision="reject",
            reason="json_parse_error",
        )
        return _json_response({"status": "bad_request"}, status=400)

    # Bitbucket carries the event type in the ``X-Event-Key`` header
    # rather than the payload body.  Fall back to a body field for
    # tests that synthesise both shapes.
    event_type: str | None = x_event_key
    if not isinstance(event_type, str) or not event_type:
        body_event = payload.get("event") or payload.get("eventKey")
        event_type = body_event if isinstance(body_event, str) else ""
    log = log.bind(event_type=event_type)

    # ----- (c) replay guard ---------------------------------------------
    payload_hash = replay.compute_payload_hash(raw_body)
    log = log.bind(payload_hash=payload_hash)
    inserted = await replay.check_and_insert(db, payload_hash, _REPLAY_TTL)
    if not inserted:
        log.info(
            "webhook_replay_skipped",
            decision="skip",
            reason="duplicate_payload_hash",
        )
        return _json_response({"status": "duplicate", "action": "skipped"})

    # ----- (d) loop guard (self actor — Requirement 3.5) ---------------
    actor_id = _extract_actor_account_id(payload)
    if loop_guard.is_self_actor(actor_id, bot_account_ids):
        log.info(
            "webhook_loop_guard_skipped",
            decision="skip",
            reason="actor_is_bot",
            actor_id=actor_id,
        )
        return _json_response({"status": "loop_guard", "action": "skipped"})

    # ----- (e) event-type classification --------------------------------
    if loop_guard.route(event_type) == "ignored":
        log.info(
            "webhook_event_ignored",
            decision="skip",
            reason="unsupported_event_type",
        )
        return _json_response({"status": "ignored", "action": "skipped"})

    # Only Bitbucket-source events are valid on this endpoint.
    if event_type not in (_EVENT_REVIEWER_ADDED, _EVENT_COMMENT_CREATED):
        log.info(
            "webhook_event_ignored",
            decision="skip",
            reason="cross_source_event",
        )
        return _json_response({"status": "ignored", "action": "skipped"})

    # ----- (f) extract repo / PR identifiers ----------------------------
    slugs = _extract_repository_slugs(payload)
    pr_id = _extract_pr_id(payload)
    if slugs is None or pr_id is None:
        log.warning(
            "webhook_bad_request",
            decision="reject",
            reason="missing_pr_context",
        )
        return _json_response({"status": "bad_request"}, status=400)
    workspace, repo = slugs
    log = log.bind(workspace=workspace, repo=repo, pr_id=pr_id)

    try:
        workflow_id = automation_workflow_id_bb(workspace, repo, pr_id)
    except InvalidSlugError as exc:
        log.warning(
            "webhook_bad_request",
            decision="reject",
            reason="invalid_slug",
            field=exc.field,
            value=exc.value,
        )
        return _json_response({"status": "bad_request"}, status=400)
    log = log.bind(workflow_id=workflow_id)

    # ----- (g) resolve department from repo mapping --------------------
    department_id = await _resolve_department_for_repo(db, workspace, repo)
    if department_id is None:
        log.info(
            "webhook_repo_unmapped",
            decision="skip",
            reason="repo_not_mapped",
        )
        return _json_response({"status": "ignored", "action": "skipped"})
    log = log.bind(department_id=department_id)

    # ``automation.work_items.issue_key`` is ``NOT NULL`` but Bitbucket
    # events do not carry a Jira issue key — synthesise a stable
    # repo-scoped identifier so the row is unambiguous.
    work_item_issue_key = f"{workspace}/{repo}#{pr_id}"

    # ----- (h) per-event branching --------------------------------------
    if event_type == _EVENT_COMMENT_CREATED:
        return await _handle_comment_created(
            log=log,
            temporal=temporal,
            workflow_id=workflow_id,
            payload=payload,
            workspace=workspace,
            repo=repo,
            pr_id=pr_id,
            department_id=department_id,
        )

    # event_type == _EVENT_REVIEWER_ADDED (only remaining option).
    return await _handle_reviewer_added(
        log=log,
        db=db,
        temporal=temporal,
        creds=creds,
        payload=payload,
        workflow_id=workflow_id,
        workspace=workspace,
        repo=repo,
        pr_id=pr_id,
        department_id=department_id,
        work_item_issue_key=work_item_issue_key,
    )


# ---------------------------------------------------------------------------
# Per-event handlers
# ---------------------------------------------------------------------------


async def _handle_reviewer_added(
    *,
    log: Any,
    db: asyncpg.Pool,
    temporal: TemporalClient,
    creds: CredentialResolver,
    payload: dict[str, Any],
    workflow_id: str,
    workspace: str,
    repo: str,
    pr_id: int,
    department_id: str,
    work_item_issue_key: str,
) -> Response:
    """Handle ``pullrequest:reviewer_added`` (Requirement 3.3).

    The added reviewer must match the dept's Bitbucket bot ``account_id``;
    otherwise the event is silently skipped.  When matched, a
    ``work_items`` row is inserted in ``pending`` state and an
    ``AutomationWorkflow`` (``workflow_type='pr_review'``) is started.
    """

    reviewer_id = _extract_reviewer_account_id(payload)
    dept_bot_id = await _dept_bitbucket_account_id(creds, department_id)

    if (
        reviewer_id is None
        or dept_bot_id is None
        or reviewer_id != dept_bot_id
    ):
        log.info(
            "webhook_skipped_not_bot_reviewer",
            decision="skip",
            reason="reviewer_not_bot",
            reviewer_id=reviewer_id,
        )
        return _json_response(
            {"status": "not_bot_reviewer", "action": "skipped"}
        )

    # ----- (j) work_items insert + (k) workflow start ------------------
    await _insert_work_item(
        db,
        workflow_id=workflow_id,
        department_id=department_id,
        issue_key=work_item_issue_key,
        workflow_type="pr_review",
    )

    workflow_input = {
        "trigger_event": _EVENT_REVIEWER_ADDED,
        "workflow_type": "pr_review",
        "department_id": department_id,
        "workspace": workspace,
        "repo": repo,
        "pr_id": pr_id,
    }
    try:
        await temporal.start_workflow(
            _AUTOMATION_WORKFLOW_TYPE,
            workflow_id,
            task_queue=_AUTOMATION_TASK_QUEUE,
            args=[workflow_input],
        )
    except WorkflowAlreadyStartedError:
        # Temporal native idempotency on top of the replay guard.
        log.info(
            "workflow_already_started",
            decision="skip",
            reason="workflow_running",
        )
        return _json_response(
            {
                "status": "duplicate",
                "reason": "workflow_running",
                "workflow_id": workflow_id,
            }
        )

    log.info(
        "workflow_started",
        decision="accept",
        reason="all_guards_passed",
    )
    return _json_response(
        {"status": "accepted", "workflow_id": workflow_id}
    )


async def _handle_comment_created(
    *,
    log: Any,
    temporal: TemporalClient,
    workflow_id: str,
    payload: dict[str, Any],
    workspace: str,
    repo: str,
    pr_id: int,
    department_id: str,
) -> Response:
    """Handle ``pullrequest:comment_created`` (Requirement 3.4).

    Forwards a minimal ``new_comment`` signal to the existing PR review
    workflow.  When no workflow is running for this PR the signal call
    raises (Temporal returns ``NOT_FOUND``), which we surface as
    ``200 ignored`` — comments on PRs without an active review are not
    actionable in P0.
    """

    signal_payload = _extract_comment_signal_payload(
        payload,
        workspace=workspace,
        repo=repo,
        pr_id=pr_id,
        department_id=department_id,
    )
    try:
        await temporal.signal_workflow(
            workflow_id, "new_comment", signal_payload
        )
    except Exception as exc:  # noqa: BLE001 - best-effort signal
        log.info(
            "webhook_signal_dropped",
            decision="skip",
            reason="signal_failed",
            error=str(exc),
        )
        return _json_response(
            {"status": "ignored", "action": "skipped"}
        )

    log.info(
        "webhook_comment_signaled",
        decision="accept",
        reason="comment_signal_forwarded",
    )
    return _json_response(
        {"status": "comment_signaled", "workflow_id": workflow_id}
    )
