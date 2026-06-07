"""Loop Guard pipeline stage - drops webhooks triggered by bot's own actions.

This module implements the stateful ``LoopGuard`` class that participates
in the webhook processing pipeline (Event_Dedup  **Loop_Guard**
Webhook_Dispatcher). Unlike the pure predicates in
``decision/loop_guard.py``, this class performs I/O:

- Queries ``automation.department_bots`` to resolve bot account IDs.
- Records drops in ``shared.loop_guard_drops`` for storm detection.
- Checks/creates blocks in ``shared.loop_guard_blocks``.
- Emits audit events via :class:`audit_logger.AuditLogger`.
- Sends admin notifications on loop storm detection.

Approval Gate ``[approve]``/``[reject]`` comments are **exempt** from
the loop guard even when authored by a bot - but a bot cannot approve
its own work (the approval signal is only processed when the comment
author differs from the issue assignee).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Protocol

import structlog

__all__ = ["LoopGuard", "StageResult", "WebhookPayload"]

_logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Number of drops within the storm window that triggers a block.
STORM_THRESHOLD: int = 3

#: Duration of the storm window in seconds.
STORM_WINDOW_SECONDS: int = 60

#: Duration of the block imposed on a storming issue_key.
STORM_BLOCK_SECONDS: int = 300  # 5 minutes

#: Regex pattern matching Approval Gate signals in comment bodies.
#: Matches ``[approve]`` or ``[reject]`` (case-insensitive).
_APPROVAL_GATE_PATTERN: re.Pattern[str] = re.compile(
    r"\[(?:approve|reject)\]", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StageResult:
    """Result of a pipeline stage check.

    Attributes
    ----------
    action:
        ``"drop"`` if the webhook should be discarded,
        ``"pass"`` if it should continue to the next stage.
    reason:
        Human-readable reason for the action (used in audit logs).
    """

    action: Literal["drop", "pass"]
    reason: str = ""


@dataclass(frozen=True, slots=True)
class WebhookPayload:
    """Normalised webhook payload consumed by pipeline stages.

    Attributes
    ----------
    actor_account_id:
        The ``accountId`` of the user/bot that triggered the event.
    issue_key:
        The Jira issue key (e.g. ``"PROJ-123"``).
    event_type:
        The webhook event type (e.g. ``"jira:comment_created"``).
    comment_body:
        The comment text (for ``comment_created`` events). ``None``
        for non-comment events.
    assignee_account_id:
        The current assignee's ``accountId``. Used for self-approve
        detection.
    reporter_account_id:
        The issue reporter's ``accountId``. Used for iteration
        authorization; reporters can also iterate.
    dept_id:
        Resolved department ID (may be ``None`` if not yet resolved).
    trace_id:
        Trace ID for observability propagation.
    """

    actor_account_id: str | None = None
    issue_key: str = ""
    event_type: str = ""
    comment_body: str | None = None
    assignee_account_id: str | None = None
    reporter_account_id: str | None = None
    dept_id: str | None = None
    trace_id: str | None = None


# ---------------------------------------------------------------------------
# Protocols for dependency injection
# ---------------------------------------------------------------------------


class AdminNotifier(Protocol):
    """Protocol for sending admin notifications."""

    async def notify(self, event_name: str, issue_key: str, detail: str) -> None:
        """Send a notification to the admin dashboard."""
        ...


# ---------------------------------------------------------------------------
# LoopGuard
# ---------------------------------------------------------------------------


class LoopGuard:
    """Drops webhooks triggered by bot's own actions.

    This is a stateful pipeline stage that:
    1. Compares ``actor_account_id`` against all bot IDs in
       ``automation.department_bots``.
    2. If the actor is a bot  DROP + audit ``loop_guard_dropped``.
    3. Tracks drops per ``issue_key`` for storm detection.
    4. If 3+ drops in 60s for the same issue  block for 5 min +
       admin notification.
    5. Exempts Approval Gate ``[approve]``/``[reject]`` comments
       (but bot cannot approve itself).

    Parameters
    ----------
    db:
        An asyncpg connection pool.
    audit_logger:
        Optional :class:`audit_logger.AuditLogger` for audit writes.
    admin_notifier:
        Optional notifier for loop storm alerts.
    bot_ids_provider:
        Optional async callable that returns the current set of bot
        account IDs. If not provided, the guard queries the DB directly.
    clock:
        Injectable clock for deterministic testing.
    """

    STORM_THRESHOLD: int = STORM_THRESHOLD
    STORM_WINDOW_SECONDS: int = STORM_WINDOW_SECONDS
    STORM_BLOCK_SECONDS: int = STORM_BLOCK_SECONDS

    def __init__(
        self,
        db: Any,
        *,
        audit_logger: Any | None = None,
        admin_notifier: AdminNotifier | None = None,
        bot_ids_provider: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._audit_logger = audit_logger
        self._admin_notifier = admin_notifier
        self._bot_ids_provider = bot_ids_provider
        self._clock = clock or _utc_now
        # In-memory cache of bot account IDs (refreshed on cache miss).
        self._bot_ids_cache: frozenset[str] | None = None
        self._bot_ids_cache_at: datetime | None = None
        # Cache TTL: 5 minutes (matches Webhook_Dispatcher cache refresh).
        self._bot_ids_cache_ttl = timedelta(seconds=300)

    async def check(self, payload: WebhookPayload) -> StageResult:
        """Run the loop guard check on a webhook payload.

        Returns
        -------
        StageResult
            ``action="drop"`` if the webhook should be discarded,
            ``action="pass"`` if it should continue.
        """
        now = self._clock()

        # 0. Check if issue is currently blocked (storm block).
        if payload.issue_key and await self._is_blocked(payload.issue_key, now):
            await self._audit_drop(
                payload, reason="loop_guard_blocked_issue"
            )
            return StageResult(action="drop", reason="loop_guard_blocked")

        # 1. Resolve bot account IDs.
        bot_ids = await self._get_bot_ids()

        # 2. Check if actor is a bot.
        actor_id = payload.actor_account_id
        if actor_id is None or actor_id not in bot_ids:
            return StageResult(action="pass")

        # 3. Check Approval Gate exemption.
        if self._is_approval_gate_exempt(payload):
            _logger.debug(
                "loop_guard_approval_gate_exempt",
                actor_id=actor_id,
                issue_key=payload.issue_key,
                event_type=payload.event_type,
            )
            return StageResult(action="pass")

        # 4. Actor is a bot  DROP.
        await self._record_drop(payload, now)
        await self._audit_drop(payload, reason="loop_guard_dropped")

        # 5. Storm detection: check if threshold exceeded.
        if payload.issue_key:
            if await self._is_storm(payload.issue_key, now):
                await self._block_issue(payload.issue_key, now)
                await self._notify_admin(
                    "loop_storm_detected", payload.issue_key
                )

        return StageResult(action="drop", reason="loop_guard")

    # ------------------------------------------------------------------
    # Approval Gate exemption
    # ------------------------------------------------------------------

    def _is_approval_gate_exempt(self, payload: WebhookPayload) -> bool:
        """Check if this is an Approval Gate comment that should be exempt.

        Approval Gate ``[approve]``/``[reject]`` comments are exempt
        from the loop guard even when authored by a bot. However, a
        bot cannot approve **itself** - the comment author must differ
        from the issue assignee.

        Parameters
        ----------
        payload:
            The webhook payload to check.

        Returns
        -------
        bool
            ``True`` if the comment is an exempt Approval Gate signal.
        """
        # Only comment events can be Approval Gate signals.
        if payload.event_type != "jira:comment_created":
            return False

        # Must have a comment body with [approve] or [reject].
        if not payload.comment_body:
            return False

        if not _APPROVAL_GATE_PATTERN.search(payload.comment_body):
            return False

        # Bot cannot approve itself: actor must differ from assignee.
        if (
            payload.actor_account_id
            and payload.assignee_account_id
            and payload.actor_account_id == payload.assignee_account_id
        ):
            _logger.warning(
                "loop_guard_self_approve_blocked",
                actor_id=payload.actor_account_id,
                issue_key=payload.issue_key,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # Bot ID resolution
    # ------------------------------------------------------------------

    async def _get_bot_ids(self) -> frozenset[str]:
        """Return the current set of all bot account IDs.

        Uses an in-memory cache with 5-minute TTL. On cache miss,
        queries ``automation.department_bots`` or uses the injected
        provider.
        """
        now = self._clock()

        # Check cache validity.
        if (
            self._bot_ids_cache is not None
            and self._bot_ids_cache_at is not None
            and now - self._bot_ids_cache_at < self._bot_ids_cache_ttl
        ):
            return self._bot_ids_cache

        # Refresh from provider or DB.
        if self._bot_ids_provider is not None:
            result = self._bot_ids_provider()
            # Support both sync and async providers.
            if hasattr(result, "__await__"):
                bot_ids = await result
            else:
                bot_ids = result
            self._bot_ids_cache = frozenset(bot_ids)
        else:
            self._bot_ids_cache = await self._fetch_bot_ids_from_db()

        self._bot_ids_cache_at = now
        return self._bot_ids_cache

    async def _fetch_bot_ids_from_db(self) -> frozenset[str]:
        """Query all bot account_ids from automation.department_bots."""
        try:
            async with self._db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT account_id
                    FROM automation.department_bots
                    WHERE account_id IS NOT NULL
                      AND account_id != ''
                    """
                )
            return frozenset(row["account_id"] for row in rows)
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "loop_guard_bot_ids_fetch_failed",
                error=str(exc),
            )
            # On failure, return cached value if available, else empty.
            return self._bot_ids_cache or frozenset()

    # ------------------------------------------------------------------
    # Drop recording & storm detection
    # ------------------------------------------------------------------

    async def _record_drop(
        self, payload: WebhookPayload, now: datetime
    ) -> None:
        """Record a drop in ``shared.loop_guard_drops``."""
        if not self._db:
            return

        try:
            async with self._db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO shared.loop_guard_drops
                        (issue_key, dept_id, event_type, actor_account_id, dropped_at)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    payload.issue_key or "",
                    payload.dept_id,
                    payload.event_type,
                    payload.actor_account_id or "",
                    now,
                )
        except Exception as exc:  # noqa: BLE001
            # Best-effort: drop recording failure should not block the
            # pipeline. The webhook is still dropped (at-least-once
            # semantics for the guard itself).
            _logger.warning(
                "loop_guard_record_drop_failed",
                issue_key=payload.issue_key,
                error=str(exc),
            )

    async def _is_storm(self, issue_key: str, now: datetime) -> bool:
        """Check if the issue has exceeded the storm threshold.

        Counts drops for ``issue_key`` within the last
        ``STORM_WINDOW_SECONDS`` seconds. Returns ``True`` if the
        count meets or exceeds ``STORM_THRESHOLD``.
        """
        if not self._db:
            return False

        window_start = now - timedelta(seconds=self.STORM_WINDOW_SECONDS)

        try:
            async with self._db.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS drop_count
                    FROM shared.loop_guard_drops
                    WHERE issue_key = $1
                      AND dropped_at >= $2
                    """,
                    issue_key,
                    window_start,
                )
            count = row["drop_count"] if row else 0
            return count >= self.STORM_THRESHOLD
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "loop_guard_storm_check_failed",
                issue_key=issue_key,
                error=str(exc),
            )
            return False

    async def _is_blocked(self, issue_key: str, now: datetime) -> bool:
        """Check if an issue_key is currently blocked due to a storm."""
        if not self._db:
            return False

        try:
            async with self._db.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT blocked_until
                    FROM shared.loop_guard_blocks
                    WHERE issue_key = $1
                      AND blocked_until > $2
                    """,
                    issue_key,
                    now,
                )
            return row is not None
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "loop_guard_block_check_failed",
                issue_key=issue_key,
                error=str(exc),
            )
            return False

    async def _block_issue(self, issue_key: str, now: datetime) -> None:
        """Block an issue_key for ``STORM_BLOCK_SECONDS``.

        Uses INSERT ... ON CONFLICT to extend the block if already
        present (idempotent).
        """
        if not self._db:
            return

        blocked_until = now + timedelta(seconds=self.STORM_BLOCK_SECONDS)

        try:
            async with self._db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO shared.loop_guard_blocks
                        (issue_key, blocked_until, reason, created_at)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (issue_key) DO UPDATE
                        SET blocked_until = EXCLUDED.blocked_until,
                            reason = EXCLUDED.reason
                    """,
                    issue_key,
                    blocked_until,
                    "loop_storm_detected",
                    now,
                )
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "loop_guard_block_issue_failed",
                issue_key=issue_key,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Admin notification
    # ------------------------------------------------------------------

    async def _notify_admin(self, event_name: str, issue_key: str) -> None:
        """Send a loop storm notification to the admin dashboard.

        Best-effort: notification failures are logged but do not
        affect the pipeline result.
        """
        _logger.warning(
            event_name,
            issue_key=issue_key,
            block_seconds=self.STORM_BLOCK_SECONDS,
        )

        if self._admin_notifier is not None:
            try:
                await self._admin_notifier.notify(
                    event_name,
                    issue_key,
                    f"Issue {issue_key} blocked for {self.STORM_BLOCK_SECONDS}s "
                    f"due to loop storm ({self.STORM_THRESHOLD}+ drops in "
                    f"{self.STORM_WINDOW_SECONDS}s).",
                )
            except Exception as exc:  # noqa: BLE001
                _logger.error(
                    "loop_guard_admin_notify_failed",
                    issue_key=issue_key,
                    error=str(exc),
                )

    # ------------------------------------------------------------------
    # Audit logging
    # ------------------------------------------------------------------

    async def _audit_drop(
        self, payload: WebhookPayload, *, reason: str
    ) -> None:
        """Emit an audit event for a dropped webhook.

        Best-effort: audit failures do not affect the pipeline result.
        """
        if self._audit_logger is None:
            return

        try:
            from audit_logger import AuditEvent
        except ImportError:  # pragma: no cover
            return

        event = AuditEvent(
            actor_id=payload.actor_account_id or "unknown",
            actor_role="system",
            dept_id=payload.dept_id,
            action=reason,
            resource=f"webhook:{payload.issue_key}",
            result="denied",
            timestamp=self._clock(),
            payload={
                "event_type": payload.event_type,
                "issue_key": payload.issue_key,
                "actor_account_id": payload.actor_account_id,
            },
        )

        try:
            await self._audit_logger.write(event)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "loop_guard_audit_write_failed",
                reason=reason,
                issue_key=payload.issue_key,
                error=str(exc),
            )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """Return the current UTC time (timezone-aware)."""
    return datetime.now(timezone.utc)
