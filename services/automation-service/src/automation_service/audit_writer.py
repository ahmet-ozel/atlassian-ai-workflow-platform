"""Asyncpg-backed :class:`audit_logger.AuditWriter` for automation-service.

The lifespan handler wraps an :class:`AuditLogger` around this writer so
every router that pulls an ``audit_logger`` collaborator off
``app.state`` lands its rows in ``automation.audit_events`` (the same
singleton is shared across containers; mandatory ``actor_role`` is
enforced by the application-layer
:class:`audit_logger.AuditLogger.write` guard before the SQL fires).

The shape mirrors the canonical asyncpg writer that already ships in
``admin-dashboard-api/src/prompts/audit_writer.py`` so cross-service
behaviour stays uniform; the implementation is kept service-local
because the :mod:`audit_logger` library deliberately stays
framework-agnostic (no ``asyncpg`` dependency).

Failure semantics
-----------------

Failures **never** propagate out of :meth:`insert_audit`. Audit
failures must not mask the underlying request outcome — the column-
level CHECK on ``actor_role`` is enforced by
:class:`audit_logger.AuditLogger` *before* the SQL runs, and any
connection-level / programming error here is logged and swallowed.

* Connection-level errors (``OSError``, ``asyncio.TimeoutError`` and
  the asyncpg-named connection exceptions enumerated in
  :func:`_is_connection_error`) are logged at WARNING — the audit row
  is dropped on the floor; an operator looking at "audit_events
  insert failed" log lines can correlate with the orchestrator's
  Postgres outage signal.
* Programming errors (CHECK violation, malformed payload, etc.) are
  logged at ERROR with the exception type so the operator gets a
  loud signal in structured logs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol, runtime_checkable

from audit_logger import AuditEvent

__all__ = ["AsyncpgAuditEventsWriter"]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pool / connection protocols (kept narrow so unit tests can plug fakes)
# ---------------------------------------------------------------------------


@runtime_checkable
class _ConnectionLike(Protocol):
    """Minimal asyncpg ``Connection`` surface used by the writer."""

    async def execute(self, query: str, *args: Any) -> Any:  # pragma: no cover - protocol
        ...


@runtime_checkable
class _PoolLike(Protocol):
    """Minimal asyncpg ``Pool`` surface used by the writer.

    ``acquire()`` must return an async context manager whose
    ``__aenter__`` yields a :class:`_ConnectionLike`.
    """

    def acquire(self) -> Any:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# SQL — column order mirrors infra/postgres/init/10_automation.sql
# ---------------------------------------------------------------------------


_INSERT_AUDIT_EVENT_SQL = (
    "INSERT INTO automation.audit_events "
    "(actor_id, actor_role, dept_id, action, resource, result, "
    "payload, created_at) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)"
)


# ---------------------------------------------------------------------------
# Connection-error classification
# ---------------------------------------------------------------------------


def _is_connection_error(exc: BaseException) -> bool:
    """Return ``True`` when ``exc`` looks like a DB-unreachable failure."""

    if isinstance(exc, (OSError, ConnectionError, asyncio.TimeoutError)):
        return True
    name = type(exc).__name__
    return name in {
        "PostgresConnectionError",
        "ConnectionDoesNotExistError",
        "ConnectionFailureError",
        "CannotConnectNowError",
        "InterfaceError",
        "ConnectionRefusedError",
        "TimeoutError",
    }


# ---------------------------------------------------------------------------
# AsyncpgAuditEventsWriter
# ---------------------------------------------------------------------------


class AsyncpgAuditEventsWriter:
    """Implements the :class:`audit_logger.AuditWriter` protocol.

    Wraps an externally-owned :class:`asyncpg.Pool` and performs a
    single ``INSERT INTO automation.audit_events`` per call.  The pool
    is **not** closed by the writer — lifecycle (open + close) belongs
    to the lifespan handler.
    """

    __slots__ = ("_pool", "_logger")

    def __init__(
        self,
        *,
        pool: _PoolLike,
        logger_name: str = "automation_service.audit_events",
    ) -> None:
        """Bind the writer to an existing pool.

        Args:
            pool: An asyncpg-pool-shaped object (anything implementing
                :class:`_PoolLike`). The writer never calls ``close``
                on it.
            logger_name: Name of the logger used for diagnostics.
        """

        self._pool = pool
        self._logger = logging.getLogger(logger_name)

    async def insert_audit(self, event: AuditEvent) -> None:
        """Persist ``event`` to ``automation.audit_events``.

        Implements the :class:`audit_logger.AuditWriter` protocol.
        Failures are classified into connection-level (logged at
        WARNING) and programming errors (logged at ERROR); both are
        swallowed so audit plumbing never masks the request outcome.
        """

        payload_json = self._encode_payload(event.payload)

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    _INSERT_AUDIT_EVENT_SQL,
                    event.actor_id,
                    event.actor_role,
                    event.dept_id,
                    event.action,
                    event.resource,
                    event.result,
                    payload_json,
                    event.timestamp,
                )
        except BaseException as exc:  # noqa: BLE001 - audit must not raise
            if _is_connection_error(exc):
                self._logger.warning(
                    "audit_events insert failed (connection-level): "
                    "action=%s actor=%s err=%s",
                    event.action,
                    event.actor_id,
                    exc,
                )
                return
            self._logger.error(
                "audit_events insert failed (non-connection): "
                "action=%s actor=%s err_type=%s err=%s",
                event.action,
                event.actor_id,
                type(exc).__name__,
                exc,
            )
            return

    @staticmethod
    def _encode_payload(payload: dict[str, Any] | None) -> str | None:
        """Serialise ``payload`` to JSON for the ``$7::jsonb`` cast.

        ``None`` is preserved (the column is nullable). ``default=str``
        round-trips non-JSON-native values (UUID, datetime). Keys are
        sorted so on-disk JSONB is byte-stable for diff-based tests.
        """

        if payload is None:
            return None
        return json.dumps(payload, default=str, sort_keys=True)
