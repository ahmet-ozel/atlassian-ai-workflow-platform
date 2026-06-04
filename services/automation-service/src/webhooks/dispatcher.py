"""Webhook Dispatcher — routes webhooks to the correct department workflow.

Resolves ``assignee.accountId`` → ``dept_id`` via the
``department_bot_identity`` cache (backed by ``automation.department_bots``
in Postgres). Applies routing rules:

1. Assignee null (unassign event) → DROP + audit ``dispatch_unassigned``
2. Assignee not in bot identity table → DROP + audit ``dispatch_not_bot``
3. Department mode == disabled → DROP + audit ``webhook_dept_disabled``
4. Comment on needs_info issue → Temporal signal ``info_received``
5. ``[iterate]`` comment → Iteration Manager start
6. Normal assign/update → workflow start (trace_id generated)

Cache refresh: 5-minute interval + instant Vault query on cache miss.

"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from temporal_shared.workflow_registry import task_queue_for

from .loop_guard import StageResult, WebhookPayload

# Budget enforcement runtime guard. Imports are deferred to
# :meth:`WebhookDispatcher._check_budget_pre_start` so a sys.path
# bootstrap performed by a test harness *after* this module loads
# (the canonical pattern used by the unit tests) still picks up
# the budget package. A module-level import would lock in the
# resolution at import time and silently degrade to a no-op when
# ``automation_service`` was not yet on the path.

# Concurrency gate — imported eagerly because the dispatcher is the
# only public caller of :func:`check_dept_concurrency`. Keeping the
# import at module load time also lets type checkers verify the
# protocol contract for ``self._temporal`` (which doubles as the
# Visibility client when a real :class:`temporalio.client.Client` is
# wired). When ``concurrency`` is unavailable for any reason the
# dispatcher continues to start workflows uncapped (graceful
# degradation) — the cap is an advisory throttle, not a security
# control.
try:
    from concurrency import (  # type: ignore[import-not-found]
        ConcurrencyLimitExceeded,
        check_dept_concurrency,
        extract_max_concurrent,
    )
except ImportError:  # pragma: no cover - defensive
    ConcurrencyLimitExceeded = None  # type: ignore[assignment,misc]
    check_dept_concurrency = None  # type: ignore[assignment]
    extract_max_concurrent = None  # type: ignore[assignment]

__all__ = [
    "WebhookDispatcher",
    "DispatchResult",
    "DepartmentConfig",
    "BotIdentityEntry",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Cache refresh interval in seconds (5 minutes).
_CACHE_REFRESH_INTERVAL_S: int = 300

#: Workflow type constant.
_WORKFLOW_NAME: str = "AutomationWorkflow"

#: Temporal task queue for AutomationWorkflow.
_AUTOMATION_TASK_QUEUE: str = task_queue_for(_WORKFLOW_NAME)

#: Regex pattern for [iterate] command detection (case-insensitive).
_ITERATE_PATTERN: re.Pattern[str] = re.compile(
    r"\[iterate\]", re.IGNORECASE
)

#: Regex pattern for ``[approve]``/``[reject]`` Approval Gate signals
#: in comment bodies (case-insensitive). Mirrors the loop guard's
#: ``_APPROVAL_GATE_PATTERN`` so the two surfaces stay in lockstep.
_APPROVAL_PATTERN: re.Pattern[str] = re.compile(
    r"\[(?:approve|reject)\]", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_approvers(config_json: Any) -> tuple[str, ...]:
    """Extract the ``approvers`` list from a department's ``config_json``.

    The ``automation.departments.config_json`` column mirrors the
    ``departments.json`` entry. asyncpg decodes ``jsonb`` to Python
    objects in production; some test fakes pass raw JSON strings so
    this helper accepts both shapes.

    Returns an empty tuple when:
    - ``config_json`` is ``None`` or not a mapping after decoding,
    - ``approvers`` is missing, ``None``, or not a list,
    - any list entry is not a non-empty string.
    """
    if config_json is None:
        return ()

    decoded: Any = config_json
    if isinstance(config_json, (str, bytes, bytearray)):
        try:
            decoded = json.loads(config_json)
        except (ValueError, TypeError):
            return ()

    if not isinstance(decoded, dict):
        return ()

    raw = decoded.get("approvers")
    if not isinstance(raw, list):
        return ()

    cleaned: list[str] = []
    for entry in raw:
        if isinstance(entry, str) and entry:
            cleaned.append(entry)
    return tuple(cleaned)


def _decode_config_json(config_json: Any) -> dict[str, Any] | None:
    """Decode a ``config_json`` value to a dict, or return ``None``.

    Mirrors the loose contract used by :func:`_extract_approvers`:
    asyncpg returns ``jsonb`` as a decoded Python object in
    production, but some test fakes pass raw JSON strings — so we
    accept both.
    """
    if config_json is None:
        return None
    decoded: Any = config_json
    if isinstance(config_json, (str, bytes, bytearray)):
        try:
            decoded = json.loads(config_json)
        except (ValueError, TypeError):
            return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _extract_max_concurrent_from_config(config_json: Any) -> int | None:
    """Extract ``max_concurrent_workflows`` from a dept's ``config_json``.

    Returns ``None`` for any of:
    - ``config_json`` is missing / unparseable / not a mapping,
    - the key is absent or JSON ``null``,
    - the value is not a positive integer.

    Defers to :func:`concurrency.extract_max_concurrent` for the
    integer-parsing logic so the two surfaces agree byte-for-byte.
    """
    decoded = _decode_config_json(config_json)
    if decoded is None:
        return None
    if extract_max_concurrent is None:
        # Concurrency module unavailable; do the parse inline so we
        # don't accidentally drop the cap in production.
        raw = decoded.get("max_concurrent_workflows")
        if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
            return None
        return raw if raw >= 1 else None
    return extract_max_concurrent(decoded)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Result of the dispatcher stage execution.

    Extends the pipeline's StageResult concept with dispatcher-specific
    fields (trace_id, dept_id).

    Attributes
    ----------
    action:
        One of: ``"drop"``, ``"pass"``, ``"signaled"``,
        ``"iteration_started"``, ``"workflow_started"``,
        ``"budget_exceeded"``.
    reason:
        Human-readable reason for the action (used in audit logs).
    trace_id:
        Trace ID for workflow_started actions.
    dept_id:
        Resolved department ID (when applicable).
    status_code:
        Optional HTTP status code (e.g. 429 for ``budget_exceeded``).
        ``None`` lets the orchestrator pick a default.
    body:
        Optional JSON-serialisable body to return to the HTTP layer
        when ``action`` requires a custom response shape (e.g. the
        ``budget_exceeded`` HTTP 429 body produced by
        :func:`automation_service.budget.policy.deny_response_body`).
    """

    action: str
    reason: str | None = None
    trace_id: str | None = None
    dept_id: str | None = None
    status_code: int | None = None
    body: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DepartmentConfig:
    """Minimal department configuration for routing decisions.

    Attributes
    ----------
    dept_id:
        Department identifier.
    mode:
        Department mode: ``"active"``, ``"shadow"``, or ``"disabled"``.
    approvers:
        Tuple of authorized Jira account IDs who can issue ``[iterate]``
        commands. Sourced from ``config_json.approvers`` in
        ``automation.departments``.
    max_concurrent_workflows:
        Optional hard cap on simultaneously running
        ``AutomationWorkflow`` executions for this department. Sourced
        from ``config_json.max_concurrent_workflows``. ``None``
        disables the per-dept cap; the global license-tier cap in
        :mod:`middleware.license_cap` still applies.
    """

    dept_id: str
    mode: str
    approvers: tuple[str, ...] = ()
    max_concurrent_workflows: int | None = None


@dataclass(frozen=True, slots=True)
class BotIdentityEntry:
    """A cached bot identity entry mapping account_id → dept_id."""

    account_id: str
    department_id: str
    service: str


# ---------------------------------------------------------------------------
# Protocols for dependency injection
# ---------------------------------------------------------------------------


class DatabasePool(Protocol):
    """Minimal async database pool protocol (asyncpg-compatible)."""

    async def acquire(self) -> Any: ...  # noqa: E704


class TemporalClientProtocol(Protocol):
    """Minimal Temporal client protocol for dispatcher needs."""

    async def start_workflow(
        self,
        workflow_type: str,
        workflow_id: str,
        *,
        task_queue: str,
        args: Any = (),
        **kwargs: Any,
    ) -> Any: ...  # noqa: E704

    async def signal_workflow(
        self,
        workflow_id: str,
        signal_name: str,
        payload: Any = None,
    ) -> None: ...  # noqa: E704

    def get_workflow_handle(self, workflow_id: str) -> Any: ...  # noqa: E704


class AuditLoggerProtocol(Protocol):
    """Minimal audit logger protocol."""

    async def write(self, event: Any) -> None: ...  # noqa: E704


class VaultClientProtocol(Protocol):
    """Minimal Vault client protocol for cache-miss queries."""

    async def read_secret(self, path: str) -> dict[str, str] | None: ...  # noqa: E704


class JiraCommenterProtocol(Protocol):
    """Minimal Jira commenter protocol for concurrency-rejection notes.

    Implementations post a comment to the issue identifying that the
    department's parallel-work limit has been reached. Production
    binds this to the Atlassian MCP wrapper used elsewhere in the
    service; tests inject a recording stub.
    """

    async def post_comment(  # noqa: E704
        self, dept_id: str, issue_key: str, body: str
    ) -> None: ...


# ---------------------------------------------------------------------------
# WebhookDispatcher
# ---------------------------------------------------------------------------


class WebhookDispatcher:
    """Routes webhook to the correct department workflow.

    Implements the dispatcher stage of the webhook pipeline:
    - Resolves assignee.accountId → dept_id from bot identity cache
    - Applies routing rules (unassign, not_bot, disabled, needs_info, iterate)
    - Starts workflows or sends Temporal signals

    Parameters
    ----------
    db:
        Async database pool (asyncpg).
    temporal:
        Temporal client for workflow operations.
    audit_logger:
        Audit logger for recording dispatch decisions.
    vault:
        Vault client for cache-miss credential lookups.
    """

    def __init__(
        self,
        db: DatabasePool,
        temporal: TemporalClientProtocol,
        audit_logger: AuditLoggerProtocol | None = None,
        vault: VaultClientProtocol | None = None,
        jira_commenter: JiraCommenterProtocol | None = None,
        budget_policy: Any | None = None,
    ) -> None:
        self._db = db
        self._temporal = temporal
        self._audit_logger = audit_logger
        self._vault = vault
        self._jira_commenter = jira_commenter
        # Optional :class:`BudgetCapPolicy`. When wired, the dispatcher
        # calls :func:`check_budget` before issuing a workflow start
        # RPC (both normal and ``[iterate]`` paths). Cap aşıldıysa
        # workflow başlatılmaz; HTTP 429 + ``deny_response_body``
        # döndürülür ve audit ``budget_exceeded`` rivayetiyle Jira'ya
        # yorum yazılır (zaten check_budget içinde).
        self._budget_policy = budget_policy

        # Bot identity cache: account_id → BotIdentityEntry
        self._bot_identity_cache: dict[str, BotIdentityEntry] = {}
        # Department config cache: dept_id → DepartmentConfig
        self._dept_config_cache: dict[str, DepartmentConfig] = {}
        # Last cache refresh timestamp (monotonic seconds)
        self._last_cache_refresh: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def dispatch(self, payload: WebhookPayload) -> DispatchResult:
        """Route a webhook payload to the appropriate action.

        This is the main entry point called by the pipeline orchestrator.

        Parameters
        ----------
        payload:
            Normalised webhook payload.

        Returns
        -------
        DispatchResult
            The routing decision with action and metadata.
        """
        # Unassign event (assignee null) → DROP
        if payload.assignee_account_id is None:
            await self._audit(
                action="dispatch_unassigned",
                issue_key=payload.issue_key,
                event_type=payload.event_type,
            )
            return DispatchResult(action="drop", reason="dispatch_unassigned")

        # Ensure cache is fresh
        await self._maybe_refresh_cache()

        # Resolve assignee.accountId → dept_id
        dept_id = await self._resolve_dept(payload.assignee_account_id)

        # Not in bot identity table → DROP
        if dept_id is None:
            await self._audit(
                action="dispatch_not_bot",
                issue_key=payload.issue_key,
                event_type=payload.event_type,
                assignee_account_id=payload.assignee_account_id,
            )
            return DispatchResult(action="drop", reason="not_bot")

        # Department mode check
        dept_config = await self._get_dept_config(dept_id)
        if dept_config is not None and dept_config.mode == "disabled":
            await self._audit(
                action="webhook_dept_disabled",
                issue_key=payload.issue_key,
                event_type=payload.event_type,
                dept_id=dept_id,
            )
            return DispatchResult(
                action="drop", reason="dept_disabled", dept_id=dept_id
            )

        # Comment on needs_info issue → signal existing workflow
        # Order matters: iterate → needs_info → approval → workflow
        # start. The three are mutually exclusive — at most one
        # branch fires per comment.

        # [iterate] comment → Iteration Manager
        if (
            payload.event_type == "comment_created"
            and payload.comment_body is not None
            and self._is_iterate_command(payload.comment_body)
        ):
            # Only approvers OR the issue reporter may iterate.
            if not self._is_iterate_authorized(payload, dept_config):
                await self._audit(
                    action="dispatch_iteration_unauthorized",
                    issue_key=payload.issue_key,
                    event_type=payload.event_type,
                    dept_id=dept_id,
                    actor_account_id=payload.actor_account_id,
                )
                return DispatchResult(
                    action="drop",
                    reason="iteration_unauthorized",
                    dept_id=dept_id,
                )

            # Budget enforcement runtime guard. Check before
            # starting the iteration workflow so we never issue an RPC
            # we'd immediately cancel. ``check_budget`` writes the
            # ``budget_exceeded`` audit row (via the policy) and posts
            # the Jira comment itself; we just translate the deny into
            # a 429 ``DispatchResult``.
            budget_rejection = await self._check_budget_pre_start(payload, dept_id)
            if budget_rejection is not None:
                return budget_rejection

            await self._start_iteration(payload, dept_id)
            await self._audit(
                action="dispatch_iteration_started",
                issue_key=payload.issue_key,
                event_type=payload.event_type,
                dept_id=dept_id,
            )
            return DispatchResult(action="iteration_started", dept_id=dept_id)

        # Comment on needs_info issue → signal existing workflow
        if (
            payload.event_type == "comment_created"
            and await self._is_needs_info(payload.issue_key)
        ):
            await self._signal_workflow(
                payload.issue_key, "info_received", payload.comment_body
            )
            await self._audit(
                action="dispatch_signaled",
                issue_key=payload.issue_key,
                event_type=payload.event_type,
                dept_id=dept_id,
                signal="info_received",
            )
            return DispatchResult(action="signaled", dept_id=dept_id)

        # Approval Gate forwarding: a ``[approve]`` or
        # ``[reject]`` comment on an issue with a running
        # :class:`ApprovalGateWorkflow` child must be forwarded as an
        # ``approval_received`` signal so the child resumes (or
        # rejects) instead of timing out at 24h. The child workflow id
        # mirrors the helper that started it
        # (``ApprovalGateWorkflow-{parent}-{issue_key}`` — see
        # ``automation_worker.workflows.workflow_helpers
        # .maybe_run_approval_gate``). Forwarding is best-effort: a
        # missing child or a Temporal RPC failure is audited but does
        # not break the dispatch flow — the comment is then treated
        # as a normal update so the workflow's normal restart path
        # still runs.
        if (
            payload.event_type == "comment_created"
            and payload.comment_body is not None
            and self._is_approval_comment(payload.comment_body)
        ):
            await self._forward_approval_signal(payload, dept_id)
            return DispatchResult(
                action="approval_forwarded", dept_id=dept_id
            )

        # Normal assign/update → workflow start
        # Per-dept concurrency cap. Run before the
        # workflow start so we never issue an RPC we'd immediately
        # cancel. ``check_dept_concurrency`` is None when the
        # ``concurrency`` module failed to import; in that case we
        # skip the gate (graceful degradation — see module docstring).
        if (
            check_dept_concurrency is not None
            and dept_config is not None
            and dept_config.max_concurrent_workflows is not None
        ):
            try:
                await check_dept_concurrency(
                    dept_id,
                    dept_config.max_concurrent_workflows,
                    db=self._db,
                    temporal=self._temporal,  # type: ignore[arg-type]
                )
            except ConcurrencyLimitExceeded as exc:  # type: ignore[misc]
                await self._handle_concurrency_rejection(
                    payload=payload,
                    dept_id=dept_id,
                    exc=exc,
                )
                return DispatchResult(
                    action="drop",
                    reason="concurrency_limit_exceeded",
                    dept_id=dept_id,
                )

        # Budget enforcement runtime guard. Run after the
        # concurrency cap and before the workflow start RPC so we
        # never burn a Temporal start when the dept is over budget.
        # ``check_budget`` is the high-level helper that wraps
        # :meth:`BudgetCapPolicy.enforce` with 90% threshold warnings
        # and Jira comment posting. On deny it has already written
        # the ``budget_exceeded`` audit row and posted the Jira
        # rejection comment — the dispatcher just maps the deny to
        # a 429 ``DispatchResult``.
        budget_rejection = await self._check_budget_pre_start(payload, dept_id)
        if budget_rejection is not None:
            return budget_rejection

        trace_id = payload.trace_id or str(uuid.uuid4())
        await self._start_workflow(payload.issue_key, dept_id, trace_id)
        await self._audit(
            action="dispatch_workflow_started",
            issue_key=payload.issue_key,
            event_type=payload.event_type,
            dept_id=dept_id,
            trace_id=trace_id,
        )
        return DispatchResult(
            action="workflow_started", trace_id=trace_id, dept_id=dept_id
        )

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    async def _maybe_refresh_cache(self) -> None:
        """Refresh the bot identity cache if the interval has elapsed."""
        now = time.monotonic()
        if now - self._last_cache_refresh >= _CACHE_REFRESH_INTERVAL_S:
            await self._refresh_cache()

    async def _refresh_cache(self) -> None:
        """Reload the bot identity and department config caches from DB.

        Reads ``automation.department_bots`` for bot identity mappings
        and ``automation.departments`` for department mode configuration.
        """
        try:
            async with self._db.acquire() as conn:
                # Load bot identities
                bot_rows = await conn.fetch(
                    """
                    SELECT account_id, department_id, service
                    FROM automation.department_bots
                    WHERE account_id IS NOT NULL AND account_id != ''
                    """
                )
                # Load department configs
                dept_rows = await conn.fetch(
                    """
                    SELECT id, mode, config_json
                    FROM automation.departments
                    """
                )

            # Rebuild bot identity cache
            new_bot_cache: dict[str, BotIdentityEntry] = {}
            for row in bot_rows:
                entry = BotIdentityEntry(
                    account_id=row["account_id"],
                    department_id=row["department_id"],
                    service=row["service"],
                )
                new_bot_cache[row["account_id"]] = entry

            # Rebuild department config cache
            new_dept_cache: dict[str, DepartmentConfig] = {}
            for row in dept_rows:
                cfg_json = row.get("config_json")
                new_dept_cache[row["id"]] = DepartmentConfig(
                    dept_id=row["id"],
                    mode=row["mode"],
                    approvers=_extract_approvers(cfg_json),
                    max_concurrent_workflows=(
                        _extract_max_concurrent_from_config(cfg_json)
                    ),
                )

            self._bot_identity_cache = new_bot_cache
            self._dept_config_cache = new_dept_cache
            self._last_cache_refresh = time.monotonic()

            logger.debug(
                "Bot identity cache refreshed: %d entries, %d departments",
                len(new_bot_cache),
                len(new_dept_cache),
            )
        except Exception:
            logger.exception("Failed to refresh bot identity cache")
            # Keep stale cache on failure — better stale than empty

    async def _resolve_dept(self, account_id: str) -> str | None:
        """Resolve an assignee account_id to a department_id.

        First checks the in-memory cache. On cache miss, performs an
        instant DB query.

        Parameters
        ----------
        account_id:
            The Jira assignee's accountId.

        Returns
        -------
        str | None
            The department_id if the account is a registered bot,
            ``None`` otherwise.
        """
        # Check cache first
        entry = self._bot_identity_cache.get(account_id)
        if entry is not None:
            return entry.department_id

        # Cache miss: query DB directly
        try:
            async with self._db.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT department_id, service
                    FROM automation.department_bots
                    WHERE account_id = $1
                    LIMIT 1
                    """,
                    account_id,
                )
            if row is not None:
                # Update cache with the newly discovered entry
                new_entry = BotIdentityEntry(
                    account_id=account_id,
                    department_id=row["department_id"],
                    service=row["service"],
                )
                self._bot_identity_cache[account_id] = new_entry
                return row["department_id"]
        except Exception:
            logger.exception(
                "Failed to resolve dept for account_id=%s", account_id
            )

        return None

    async def _resolve_available_capabilities(self, dept_id: str) -> tuple[str, ...]:
        """Derive the dept's capability set for the workflow envelope.

        Capabilities are sourced from the registered bot credentials
        (``automation.department_bots`` — one service row grants the
        matching ``jira`` / ``bitbucket`` / ``confluence`` capability),
        plus ``web_search`` when the department opts in and Firecrawl is
        enabled, and ``execution`` when an SSH runner host is configured.
        Mirrors ``temporal_shared.capabilities.derive_capabilities`` but
        runs against the live DB the dispatcher already holds.
        """

        caps: set[str] = set()
        try:
            async with self._db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT service
                    FROM automation.department_bots
                    WHERE department_id = $1
                      AND credential_ref IS NOT NULL
                      AND credential_ref != ''
                    """,
                    dept_id,
                )
                dept_row = await conn.fetchrow(
                    """
                    SELECT web_search_enabled
                    FROM automation.departments
                    WHERE id = $1
                    """,
                    dept_id,
                )
                # ``execution`` is granted when the department has at least
                # one active SSH runner assigned in the admin-managed pool.
                # This is the canonical source; the ``SSH_HOST`` env below
                # is a legacy single-runner fallback.
                runner_count = await conn.fetchval(
                    """
                    SELECT COUNT(*)
                    FROM infrastructure.dept_ssh_assignments a
                    JOIN infrastructure.ssh_runners r
                      ON r.runner_id = a.runner_id
                    WHERE a.dept_id = $1 AND r.status = 'active'
                    """,
                    dept_id,
                )
        except Exception:
            logger.exception(
                "Failed to resolve capabilities for dept=%s", dept_id
            )
            return ()

        for row in rows:
            service = str(row["service"]).strip().lower()
            if service in {"jira", "bitbucket", "confluence"}:
                caps.add(service)

        web_search_enabled = bool(dept_row and dept_row["web_search_enabled"])
        if web_search_enabled and os.environ.get(
            "FIRECRAWL_ENABLED", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}:
            caps.add("web_search")

        if runner_count and int(runner_count) > 0:
            caps.add("execution")
        elif any(
            (key == "SSH_HOST" or key.startswith("SSH_HOST_"))
            and str(value).strip()
            for key, value in os.environ.items()
        ):
            caps.add("execution")

        return tuple(sorted(caps))

    async def _get_dept_config(self, dept_id: str) -> DepartmentConfig | None:
        """Get department configuration, with cache-miss fallback to DB.

        Parameters
        ----------
        dept_id:
            Department identifier.

        Returns
        -------
        DepartmentConfig | None
            The department config, or None if not found.
        """
        # Check cache first
        config = self._dept_config_cache.get(dept_id)
        if config is not None:
            return config

        # Cache miss: query DB
        try:
            async with self._db.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT id, mode, config_json
                    FROM automation.departments
                    WHERE id = $1
                    """,
                    dept_id,
                )
            if row is not None:
                cfg_json = row.get("config_json")
                config = DepartmentConfig(
                    dept_id=row["id"],
                    mode=row["mode"],
                    approvers=_extract_approvers(cfg_json),
                    max_concurrent_workflows=(
                        _extract_max_concurrent_from_config(cfg_json)
                    ),
                )
                self._dept_config_cache[dept_id] = config
                return config
        except Exception:
            logger.exception(
                "Failed to get dept config for dept_id=%s", dept_id
            )

        return None

    # ------------------------------------------------------------------
    # Needs-info detection
    # ------------------------------------------------------------------

    async def _is_needs_info(self, issue_key: str) -> bool:
        """Check if the issue's workflow is awaiting a clarification reply.

        The gateway ``AutomationWorkflow`` is dispatch-and-forget for
        every routing decision *except* the needs_info loop: once it
        dispatches a child (or stops) it completes within seconds. The
        only reason its execution stays open is the needs_info
        ``wait_condition``, which blocks on the ``info_received``
        signal. A still-running gateway execution for the issue is
        therefore a reliable signal that the workflow is parked waiting
        for the user's reply.

        The ``work_items`` table is consulted as a fallback for
        deployments that surface the status there.

        Parameters
        ----------
        issue_key:
            Jira issue key.

        Returns
        -------
        bool
            True if the issue's workflow is awaiting a reply.
        """
        if await self._is_gateway_running(issue_key):
            return True

        try:
            async with self._db.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT status
                    FROM automation.work_items
                    WHERE issue_key = $1
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    issue_key,
                )
            if row is not None and row["status"] == "needs_info":
                return True
        except Exception:
            logger.exception(
                "Failed to check needs_info status for issue_key=%s",
                issue_key,
            )
        return False

    async def _is_gateway_running(self, issue_key: str) -> bool:
        """Return True when the issue's gateway workflow is still open.

        Uses ``get_workflow_handle(...).describe()`` and inspects the
        execution status. Any error (no handle support, RPC failure,
        workflow not found) degrades to ``False`` so the dispatcher
        falls back to the normal routing path.
        """
        get_handle = getattr(self._temporal, "get_workflow_handle", None)
        if not callable(get_handle):
            return False
        workflow_id = f"automation-jira-{issue_key}"
        try:
            handle = get_handle(workflow_id)
            if hasattr(handle, "__await__"):
                handle = await handle
            description = await handle.describe()
        except Exception:
            logger.debug(
                "describe() unavailable for %s; assuming no parked workflow",
                workflow_id,
            )
            return False
        status = getattr(description, "status", None)
        # ``WorkflowExecutionStatus.RUNNING`` has value 1; compare by
        # name so the check does not depend on importing the enum.
        status_name = getattr(status, "name", None)
        if status_name is not None:
            return status_name == "RUNNING"
        return status == 1

    # ------------------------------------------------------------------
    # Temporal operations
    # ------------------------------------------------------------------

    async def _signal_workflow(
        self,
        issue_key: str,
        signal_name: str,
        payload: str | None,
    ) -> None:
        """Send a Temporal signal to the workflow for the given issue.

        Parameters
        ----------
        issue_key:
            Jira issue key used to derive the workflow ID.
        signal_name:
            Name of the signal (e.g. ``"info_received"``).
        payload:
            Signal payload (e.g. comment body text).
        """
        workflow_id = f"automation-jira-{issue_key}"
        try:
            await self._temporal.signal_workflow(
                workflow_id, signal_name, payload
            )
            logger.info(
                "Sent signal %s to workflow %s",
                signal_name,
                workflow_id,
            )
        except Exception:
            logger.exception(
                "Failed to signal workflow %s with %s",
                workflow_id,
                signal_name,
            )

    async def _start_iteration(
        self, payload: WebhookPayload, dept_id: str
    ) -> None:
        """Start an iteration workflow for the [iterate] command.

        Extracts extra instructions from the comment body and starts
        a new iteration workflow via Temporal.

        Parameters
        ----------
        payload:
            The webhook payload containing the [iterate] comment.
        dept_id:
            The resolved department ID.
        """
        # Extract extra instructions after [iterate] keyword
        extra_instructions: str | None = None
        if payload.comment_body:
            match = _ITERATE_PATTERN.search(payload.comment_body)
            if match:
                remainder = payload.comment_body[match.end():].strip()
                if remainder:
                    extra_instructions = remainder

        workflow_id = f"iteration-{payload.issue_key}-{uuid.uuid4().hex[:8]}"
        workflow_input = {
            "trigger": "iterate",
            "issue_key": payload.issue_key,
            "department_id": dept_id,
            "extra_instructions": extra_instructions,
            "comment_body": payload.comment_body,
            "actor_account_id": payload.actor_account_id,
        }

        try:
            await self._temporal.start_workflow(
                workflow_type="IterationWorkflow",
                workflow_id=workflow_id,
                task_queue=_AUTOMATION_TASK_QUEUE,
                args=[workflow_input],
            )
            logger.info(
                "Started iteration workflow %s for %s",
                workflow_id,
                payload.issue_key,
            )
        except Exception:
            logger.exception(
                "Failed to start iteration workflow for %s",
                payload.issue_key,
            )

    async def _start_workflow(
        self, issue_key: str, dept_id: str, trace_id: str
    ) -> None:
        """Start the main automation workflow for an issue.

        Parameters
        ----------
        issue_key:
            Jira issue key.
        dept_id:
            Resolved department ID.
        trace_id:
            Trace ID for observability.
        """
        workflow_id = f"automation-jira-{issue_key}"
        available_capabilities = await self._resolve_available_capabilities(
            dept_id
        )
        workflow_input = {
            "trigger": "jira",
            "issue_key": issue_key,
            "department_id": dept_id,
            "trace_id": trace_id,
            "available_capabilities": list(available_capabilities),
        }

        try:
            await self._temporal.start_workflow(
                workflow_type=_WORKFLOW_NAME,
                workflow_id=workflow_id,
                task_queue=_AUTOMATION_TASK_QUEUE,
                args=[workflow_input],
            )
            logger.info(
                "Started workflow %s (dept=%s, trace=%s)",
                workflow_id,
                dept_id,
                trace_id,
            )
        except Exception:
            logger.exception(
                "Failed to start workflow for %s (dept=%s)",
                issue_key,
                dept_id,
            )

    # ------------------------------------------------------------------
    # Iterate detection
    # ------------------------------------------------------------------

    @staticmethod
    def _is_iterate_command(comment_body: str) -> bool:
        """Check if a comment contains the [iterate] command.

        Parameters
        ----------
        comment_body:
            The comment text to check.

        Returns
        -------
        bool
            True if the comment contains ``[iterate]`` (case-insensitive).
        """
        return bool(_ITERATE_PATTERN.search(comment_body))

    # ------------------------------------------------------------------
    # Approval Gate detection & forwarding
    # ------------------------------------------------------------------

    @staticmethod
    def _is_approval_comment(comment_body: str) -> bool:
        """Check if a comment carries an Approval Gate signal.

        Matches ``[approve]`` or ``[reject]`` anywhere in the body
        (case-insensitive). Mirrors the loop guard's exemption regex
        so the two surfaces agree byte-for-byte: anything the loop
        guard exempts here gets forwarded as a signal.

        Parameters
        ----------
        comment_body:
            The comment text to check.

        Returns
        -------
        bool
            True if the comment contains ``[approve]`` or ``[reject]``.
        """
        return bool(_APPROVAL_PATTERN.search(comment_body))

    async def _forward_approval_signal(
        self, payload: WebhookPayload, dept_id: str
    ) -> None:
        """Forward an ``[approve]``/``[reject]`` comment to the child gate.

        The :class:`ApprovalGateWorkflow` child id mirrors the helper
        that started it
        (``ApprovalGateWorkflow-{parent_workflow_id}-{issue_key}`` —
        see ``automation_worker.workflows.workflow_helpers
        .maybe_run_approval_gate``). The parent id is the
        deterministic ``automation-jira-{issue_key}`` that the
        dispatcher uses for ``AutomationWorkflow``.

        Best-effort: a Temporal RPC failure (e.g. no running child)
        is audited as ``approval_signal_forwarding_failed`` but does
        not raise — the dispatch flow always succeeds so the webhook
        layer returns 200 to Atlassian.
        """

        parent_workflow_id = f"automation-jira-{payload.issue_key}"
        child_workflow_id = (
            f"ApprovalGateWorkflow-{parent_workflow_id}-{payload.issue_key}"
        )
        signal_payload = {
            "user_id": payload.actor_account_id,
            "decision": payload.comment_body,
        }

        # Parse the decision token (``approve``/``reject``) for the
        # audit row. Falls back to ``"unknown"`` if the regex match
        # somehow disagrees with the parsed token (defensive — the
        # caller already checked :meth:`_is_approval_comment`).
        decision_token = "unknown"
        if payload.comment_body:
            m = _APPROVAL_PATTERN.search(payload.comment_body)
            if m is not None:
                decision_token = m.group(0).strip("[]").lower()

        try:
            await self._temporal.signal_workflow(
                child_workflow_id, "approval_received", signal_payload
            )
        except Exception:  # noqa: BLE001 — best-effort
            logger.exception(
                "approval_signal_forwarding_failed: child=%s issue=%s",
                child_workflow_id,
                payload.issue_key,
            )
            await self._audit(
                action="approval_signal_forwarding_failed",
                issue_key=payload.issue_key,
                event_type=payload.event_type,
                dept_id=dept_id,
                decision=decision_token,
                actor_account_id=payload.actor_account_id,
                child_workflow_id=child_workflow_id,
            )
            return

        logger.info(
            "Forwarded approval signal to %s (decision=%s)",
            child_workflow_id,
            decision_token,
        )
        await self._audit(
            action="approval_signal_forwarded",
            issue_key=payload.issue_key,
            event_type=payload.event_type,
            dept_id=dept_id,
            decision=decision_token,
            actor_account_id=payload.actor_account_id,
            child_workflow_id=child_workflow_id,
        )

    @staticmethod
    def _is_iterate_authorized(
        payload: WebhookPayload,
        dept_config: DepartmentConfig | None,
    ) -> bool:
        """Check if the comment author is allowed to issue ``[iterate]``.

        ``[iterate]`` is processed only when the comment author is in
        the department's ``approvers`` list OR is the issue reporter.

        Parameters
        ----------
        payload:
            The webhook payload (comment author lives in
            ``actor_account_id``; reporter in ``reporter_account_id``).
        dept_config:
            The resolved department config carrying the approvers
            tuple. ``None`` is treated as "no approvers".

        Returns
        -------
        bool
            ``True`` when the actor is authorized to iterate.
        """
        actor = payload.actor_account_id
        if not actor:
            return False

        if (
            payload.reporter_account_id
            and actor == payload.reporter_account_id
        ):
            return True

        approvers = dept_config.approvers if dept_config else ()
        return actor in approvers

    # ------------------------------------------------------------------
    # Concurrency rejection
    # ------------------------------------------------------------------

    async def _handle_concurrency_rejection(
        self,
        *,
        payload: WebhookPayload,
        dept_id: str,
        exc: Any,  # ConcurrencyLimitExceeded — typed as Any to keep the
                   # optional-import contract clean.
    ) -> None:
        """Side effects for a concurrency-cap rejection.

        Three things happen, in order:

        1. **Audit row** ``dispatch_concurrency_rejected`` — captures
           dept, issue_key, current count, max, and the source
           (Temporal vs Postgres). Foundation audit pipeline picks
           this up for the operations dashboard.
        2. **Best-effort Jira comment** — only fires when a
           ``jira_commenter`` was wired. Posts a short Turkish note
           identifying that the dept's parallel-work limit was hit
           and the request will be retried (the webhook layer above
           returns 200 to Atlassian which may or may not redeliver;
           the comment makes the throttle visible to the human
           reporter regardless).
        3. (Caller renders the HTTP 429 response — handled by the
           webhook orchestrator above the dispatcher.)
        """

        await self._audit(
            action="dispatch_concurrency_rejected",
            issue_key=payload.issue_key,
            event_type=payload.event_type,
            dept_id=dept_id,
            current=getattr(exc, "current", None),
            max_allowed=getattr(exc, "max_allowed", None),
            source=getattr(exc, "source", None),
        )

        commenter = self._jira_commenter
        if commenter is not None and payload.issue_key:
            current = getattr(exc, "current", "?")
            max_allowed = getattr(exc, "max_allowed", "?")
            body = (
                "🤖 Departman paralel iş limiti aşıldı "
                f"({current}/{max_allowed}). "
                "Mevcut iş akışları tamamlandığında tekrar denenecek."
            )
            try:
                await commenter.post_comment(
                    dept_id, payload.issue_key, body
                )
            except Exception:  # noqa: BLE001 — best-effort
                logger.warning(
                    "concurrency_rejection_comment_failed: dept_id=%s "
                    "issue_key=%s",
                    dept_id,
                    payload.issue_key,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Budget enforcement runtime guard
    # ------------------------------------------------------------------

    async def _check_budget_pre_start(
        self,
        payload: WebhookPayload,
        dept_id: str,
    ) -> DispatchResult | None:
        """Runtime budget gate invoked before any workflow start.

        Wraps :func:`automation_service.budget.policy.check_budget`
        so the dispatcher gets a single decision point regardless of
        whether the policy was wired (production) or omitted (legacy
        test paths). Returns:

        * ``None`` when the workflow may proceed — either the policy
          is not configured, or :func:`check_budget` returned
          ``allowed=True``. Any 90% threshold warnings are surfaced
          as Jira comments inside ``check_budget`` itself; the
          dispatcher does not need to inspect the warning list.
        * A :class:`DispatchResult` with ``action="budget_exceeded"``,
          ``status_code=429``, and ``body=deny_response_body(...)`` on
          deny. The caller short-circuits the workflow start and the
          orchestrator translates the result into HTTP 429 (see
          :mod:`webhooks.pipeline`).

        The helper is best-effort w.r.t. wiring availability: when
        the budget package failed to import (``check_budget is None``
        — only happens in the dispatcher's standalone unit-test
        environments) the gate is a no-op. The same is true when the
        caller did not pass ``budget_policy`` to ``__init__`` — the
        cap is then enforced at the workflow start handler one layer
        up (legacy compatibility).

        Audit + Jira comment side-effects are owned by
        :func:`check_budget` so the dispatcher does not duplicate the
        ``budget_exceeded`` audit row or the rejection comment. The
        ``actor_role="system"`` audit row format and the Jira
        comment body match the policy's existing implementation.
        """

        if self._budget_policy is None:
            return None

        # Lazy import — the budget package depends on
        # ``automation_service.__init__`` which in turn pulls in
        # ``http_shared``. Test harnesses commonly bootstrap their
        # ``sys.path`` after this module has already loaded, so a
        # module-level import would silently degrade to a no-op when
        # the path was not yet wired. Importing on first use lets
        # production wiring (where the package is always importable)
        # and tests (where the path is added to ``sys.path`` before
        # the dispatcher fires) both succeed.
        try:
            from automation_service.budget.policy import (
                BudgetDecision,
                check_budget,
                configuration_error_response,
                deny_response_body,
            )
        except ImportError:
            logger.warning(
                "budget_pre_start_check_skipped: "
                "automation_service.budget.policy not importable"
            )
            return None

        try:
            result = await check_budget(
                dept_id,
                payload.actor_account_id,
                payload.issue_key,
                policy=self._budget_policy,
                jira_comment_callback=self._budget_jira_callback(dept_id),
            )
        except Exception:  # noqa: BLE001 - fail-open is unsafe but
            # propagating would 500 the webhook; log + audit and
            # let the workflow start (the policy itself fails
            # closed at the LLM activity, which is the second
            # gate at the LLM activity).
            logger.exception(
                "budget_pre_start_check_failed: dept_id=%s issue_key=%s",
                dept_id,
                payload.issue_key,
            )
            await self._audit(
                action="dispatch_budget_check_error",
                issue_key=payload.issue_key,
                event_type=payload.event_type,
                dept_id=dept_id,
            )
            return None

        if result.allowed:
            return None

        # Configuration error (dept_id missing from caps provider).
        # Returns 422 with a configuration_error body so admins
        # can spot the misconfiguration. The audit row is not
        # written by ``check_budget`` for this branch (the policy
        # only writes ``budget_exceeded`` for real cap breaches), so
        # we record a dispatch-level audit here.
        if result.exceeded_scope == "configuration_error":
            await self._audit(
                action="dispatch_budget_configuration_error",
                issue_key=payload.issue_key,
                event_type=payload.event_type,
                dept_id=dept_id,
            )
            return DispatchResult(
                action="budget_configuration_error",
                reason="budget_configuration_error",
                dept_id=dept_id,
                status_code=422,
                body=configuration_error_response(dept_id=dept_id),
            )

        # Real cap breach. ``check_budget`` has already written the
        # ``budget_exceeded`` audit row (via the policy) and posted
        # the Jira rejection comment. Surface the 429 body via the
        # canonical helper so the wire shape stays in lockstep with
        # ``BudgetCapPolicy.enforce``.
        decision = BudgetDecision.deny(result.exceeded_scope)  # type: ignore[arg-type]
        body = deny_response_body(decision, dept_id=dept_id)

        return DispatchResult(
            action="budget_exceeded",
            reason="budget_exceeded",
            dept_id=dept_id,
            status_code=429,
            body=body,
        )

    def _budget_jira_callback(
        self, dept_id: str
    ) -> Any | None:
        """Adapt :attr:`_jira_commenter` to the ``check_budget`` callback.

        :func:`check_budget` expects a 2-arg ``async (issue_key,
        body)`` callable; the dispatcher's :class:`JiraCommenterProtocol`
        takes 3 args (``dept_id, issue_key, body``). This adapter
        injects ``dept_id`` so the commenter can resolve the dept's
        Atlassian credentials.
        """

        commenter = self._jira_commenter
        if commenter is None:
            return None

        async def _post(issue_key: str, body: str) -> None:
            await commenter.post_comment(dept_id, issue_key, body)

        return _post

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------

    async def _audit(self, *, action: str, **fields: Any) -> None:
        """Write an audit event for a dispatch decision.

        Uses the AuditLogger if available, otherwise falls back to
        structured logging. Audit writes are best-effort — failures
        are logged but do not block the dispatch pipeline.

        Parameters
        ----------
        action:
            The audit action name (e.g. ``"dispatch_not_bot"``).
        **fields:
            Additional context fields for the audit event.
        """
        if self._audit_logger is not None:
            try:
                from audit_logger import AuditEvent

                # Audit ``result`` reflects whether the action
                # represents a successful runtime decision (workflow
                # actually started, info signal delivered, approval
                # signal forwarded) versus a denial / drop. Anything
                # else collapses to ``"denied"`` so the audit dashboard
                # can colour-code the row consistently.
                is_ok = (
                    "workflow_started" in action
                    or "signaled" in action
                    or action == "approval_signal_forwarded"
                )
                event = AuditEvent(
                    actor_id=fields.get("assignee_account_id", "webhook-dispatcher"),
                    actor_role="system",
                    dept_id=fields.get("dept_id"),
                    action=action,
                    resource=f"issue:{fields.get('issue_key', 'unknown')}",
                    result="ok" if is_ok else "denied",
                    timestamp=datetime.now(tz=timezone.utc),
                    payload={k: v for k, v in fields.items() if v is not None},
                )
                await self._audit_logger.write(event)
            except Exception:
                logger.exception("Failed to write audit event: %s", action)
        else:
            # Fallback: structured log
            logger.info(
                "audit:%s",
                action,
                extra={k: v for k, v in fields.items() if v is not None},
            )

    # ------------------------------------------------------------------
    # Cache invalidation (for testing and hot-reload)
    # ------------------------------------------------------------------

    def invalidate_cache(self) -> None:
        """Force cache invalidation — next dispatch will refresh."""
        self._last_cache_refresh = 0.0

    @property
    def bot_identity_cache(self) -> dict[str, BotIdentityEntry]:
        """Read-only access to the bot identity cache (for testing)."""
        return self._bot_identity_cache

    @property
    def dept_config_cache(self) -> dict[str, DepartmentConfig]:
        """Read-only access to the department config cache (for testing)."""
        return self._dept_config_cache
