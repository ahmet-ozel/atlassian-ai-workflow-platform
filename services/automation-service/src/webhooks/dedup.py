"""Webhook event deduplication - ``Event_Dedup`` pipeline stage.

Prevents duplicate processing of the same webhook delivery by deriving a
unique ``event_id`` from either the ``X-Atlassian-Webhook-Identifier``
header or a hash of ``webhookEvent + timestamp + issue.id``, then checking
against the ``shared.webhook_dedup`` table (24h TTL).

Pipeline position: **first** stage - runs before Loop_Guard and Dispatcher.

Design decisions:
  - At-least-once semantics: if the DB write fails, the event passes
    through anyway (we log ``dedup_write_failed`` but never block).
  - Hourly cleanup job deletes expired rows (``expires_at < NOW()``).
  - The ``X-Atlassian-Webhook-Identifier`` header is preferred when
    present because it is Atlassian's canonical delivery identifier.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import structlog

__all__ = ["EventDedup", "StageResult", "WebhookPayload"]

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

_logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebhookPayload:
    """Normalised webhook payload passed through the pipeline stages.

    Attributes
    ----------
    headers : dict[str, str]
        HTTP request headers (lowercased keys).
    body : dict[str, Any]
        Parsed JSON body of the webhook request.
    raw_body : bytes
        Original raw request body bytes (used for hashing).
    """

    headers: dict[str, str]
    body: dict[str, Any]
    raw_body: bytes

    # Convenience accessors ------------------------------------------------

    @property
    def webhook_event(self) -> str | None:
        """The ``webhookEvent`` field from the payload body."""
        val = self.body.get("webhookEvent")
        return val if isinstance(val, str) else None

    @property
    def timestamp(self) -> int | None:
        """The ``timestamp`` field from the payload body (epoch ms)."""
        val = self.body.get("timestamp")
        return val if isinstance(val, int) else None

    @property
    def issue_id(self) -> str | None:
        """The ``issue.id`` field from the payload body."""
        issue = self.body.get("issue")
        if isinstance(issue, dict):
            issue_id = issue.get("id")
            return str(issue_id) if issue_id is not None else None
        return None

    @property
    def issue_key(self) -> str | None:
        """The ``issue.key`` field from the payload body."""
        issue = self.body.get("issue")
        if isinstance(issue, dict):
            key = issue.get("key")
            return key if isinstance(key, str) else None
        return None

    @property
    def atlassian_webhook_identifier(self) -> str | None:
        """The ``X-Atlassian-Webhook-Identifier`` header value."""
        return self.headers.get("x-atlassian-webhook-identifier")


@dataclass(frozen=True)
class StageResult:
    """Result returned by a pipeline stage.

    Attributes
    ----------
    action : str
        One of ``"pass"`` (continue to next stage) or ``"drop"``
        (stop processing, return 200 OK to caller).
    reason : str | None
        Human-readable reason when action is ``"drop"``.
    """

    action: str  # "pass" | "drop"
    reason: str | None = None


# ---------------------------------------------------------------------------
# DB protocol (for dependency injection / testing)
# ---------------------------------------------------------------------------


class AsyncDBPool(Protocol):
    """Minimal protocol matching asyncpg.Pool.acquire() context manager."""

    def acquire(self) -> Any: ...  # noqa: E704


# ---------------------------------------------------------------------------
# EventDedup
# ---------------------------------------------------------------------------


class EventDedup:
    """Deduplicates webhook events using event_id + 24h TTL.

    Parameters
    ----------
    db : AsyncDBPool
        asyncpg connection pool connected to the platform database.

    Usage
    -----
    >>> dedup = EventDedup(db=pool)
    >>> result = await dedup.check(payload)
    >>> if result.action == "drop":
    ...     return JSONResponse(200, {"status": "duplicate"})
    """

    TABLE: str = "shared.webhook_dedup"
    TTL_HOURS: int = 24

    def __init__(self, db: AsyncDBPool) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check(self, payload: WebhookPayload) -> StageResult:
        """Check whether the event is a duplicate.

        1. Derive ``event_id`` from header or payload hash.
        2. If ``event_id`` already exists in the dedup table → DROP.
        3. Otherwise insert with 24h TTL → PASS.
        4. On DB write failure → PASS (at-least-once) + error log.

        Returns
        -------
        StageResult
            ``action="drop"`` if duplicate, ``action="pass"`` otherwise.
        """
        event_id = self._derive_event_id(payload)

        try:
            if await self._exists(event_id):
                _logger.info(
                    "dedup_dropped",
                    event_id=event_id,
                    issue_key=payload.issue_key,
                )
                return StageResult(action="drop", reason="duplicate")
        except Exception as exc:  # noqa: BLE001
            # DB read failure - pass through (at-least-once)
            _logger.error(
                "dedup_read_failed",
                event_id=event_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return StageResult(action="pass")

        try:
            await self._insert(event_id, payload)
        except Exception as exc:  # noqa: BLE001
            # DB write failure - log dedup_write_failed and continue
            # with at-least-once semantics.
            _logger.error(
                "dedup_write_failed",
                event_id=event_id,
                issue_key=payload.issue_key,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        return StageResult(action="pass")

    async def cleanup_expired(self, now: datetime | None = None) -> int:
        """Delete expired entries from the dedup table.

        Called by the hourly cleanup job.

        Parameters
        ----------
        now : datetime | None
            Current timestamp. Defaults to ``datetime.now(timezone.utc)``.

        Returns
        -------
        int
            Number of rows deleted.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        async with self._db.acquire() as conn:
            result = await conn.execute(
                f"""
                DELETE FROM {self.TABLE}
                WHERE expires_at < $1
                """,
                now,
            )
            # asyncpg execute returns a status string like "DELETE 5"
            return int(result.split()[-1])

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _derive_event_id(self, payload: WebhookPayload) -> str:
        """Derive a unique event_id for deduplication.

        Strategy:
          1. If ``X-Atlassian-Webhook-Identifier`` header is present,
             use it directly - this is Atlassian's canonical delivery ID.
          2. Otherwise, compute SHA-256 of
             ``webhookEvent + timestamp + issue.id``.
        """
        # Prefer Atlassian's canonical identifier
        atlassian_id = payload.atlassian_webhook_identifier
        if atlassian_id:
            return atlassian_id

        # Fallback: hash of event + timestamp + issue_id
        webhook_event = payload.webhook_event or ""
        timestamp = str(payload.timestamp or "")
        issue_id = payload.issue_id or ""

        composite = f"{webhook_event}:{timestamp}:{issue_id}"
        return hashlib.sha256(composite.encode("utf-8")).hexdigest()

    async def _exists(self, event_id: str) -> bool:
        """Check if event_id already exists in the dedup table."""
        async with self._db.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT 1 FROM {self.TABLE}
                WHERE event_id = $1
                  AND expires_at > NOW()
                """,
                event_id,
            )
            return row is not None

    async def _insert(self, event_id: str, payload: WebhookPayload) -> None:
        """Insert event_id into the dedup table with 24h TTL.

        Uses ``ON CONFLICT DO NOTHING`` to handle race conditions
        between concurrent requests gracefully.
        """
        ttl = timedelta(hours=self.TTL_HOURS)
        async with self._db.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self.TABLE} (event_id, issue_key, event_type, received_at, expires_at)
                VALUES ($1, $2, $3, NOW(), NOW() + $4::interval)
                ON CONFLICT (event_id) DO NOTHING
                """,
                event_id,
                payload.issue_key,
                payload.webhook_event,
                ttl,
            )
