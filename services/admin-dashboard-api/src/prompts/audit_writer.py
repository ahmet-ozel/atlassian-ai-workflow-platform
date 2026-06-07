"""Asyncpg-backed audit writer for prompt mutation events.

Provides an asyncpg-backed audit sink that persists
``prompt_draft_created``, ``prompt_pr_opened``, ``prompt_render_failed``
and ``prompt_pr_conflict`` rows to ``automation.audit_events``
(including the ``actor_role NOT NULL`` CHECK).
This module provides that adapter.

Storage references
------------------
* infra/postgres/init/10_automation.sql - column list, CHECK
  constraints, RLS policy ``audit_dept_isolation``.
* libs/audit_logger - :class:`audit_logger.AuditEvent` and
  :class:`audit_logger.AuditLogger` (writer protocol).

The writer is intentionally narrow:

* :class:`AsyncpgAuditEventsWriter` implements the ``insert_audit``
  protocol expected by :class:`audit_logger.AuditLogger`. The
  ``write(event)`` shape consumed by the prompts router is
  satisfied by wrapping ``AsyncpgAuditEventsWriter`` in
  :class:`audit_logger.AuditLogger`; the convenience
  :class:`AsyncpgAuditSink` does that wrapping in one place so the
  router does not have to know about either layer.

* Failures **never** propagate out of ``insert_audit``. The router's
  ``_safe_audit`` helper already swallows exceptions for resilience,
  but we additionally classify connection-level failures here as a
  best-effort signal so callers (and the readiness probe) can decide
  whether to flip a ``not_ready`` reason. The classification reuses
  the shared list from :mod:`src.lifecycle.audit_writer` so both
  writers agree on what counts as a "DB unreachable" condition.

* The pool is **owned externally** - the writer accepts a
  :class:`asyncpg.Pool`-shaped object via ``acquire``, never opens
  one of its own. That keeps lifecycle (start / close) in
  :func:`src.main.lifespan` where the rest of the asyncpg wiring
  lives, and lets unit tests inject a fake pool that records every
  ``execute`` call.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol, runtime_checkable

from audit_logger import AuditEvent, AuditLogger

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pool / connection protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class _ConnectionLike(Protocol):
    """Minimal asyncpg ``Connection`` surface used by the writer.

    We depend only on ``execute`` so a unit-test fake can satisfy the
    protocol without importing :mod:`asyncpg`.
    """

    async def execute(self, query: str, *args: Any) -> Any:
        ...


@runtime_checkable
class _PoolLike(Protocol):
    """Minimal asyncpg ``Pool`` surface used by the writer.

    ``acquire()`` must return an async context manager whose
    ``__aenter__`` yields a :class:`_ConnectionLike`. The shape
    matches both production :class:`asyncpg.Pool` instances and the
    pool fakes used by the lifecycle audit writer's unit tests.
    """

    def acquire(self) -> Any:  # async context manager
        ...


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


#: Single ``INSERT`` against ``automation.audit_events``. Column order
#: mirrors ``infra/postgres/init/10_automation.sql`` so a future
#: column reordering is caught by the integration tests instead of
#: silently corrupting the JSONB payload.
#:
#: ``payload`` is cast to ``jsonb`` so asyncpg sees a string parameter
#: and Postgres parses it on insertion - that lets us call
#: ``json.dumps`` here without bringing the asyncpg ``json`` codec
#: registration into scope.
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
    """Return ``True`` when ``exc`` looks like a DB-unreachable failure.

    Mirrors :func:`src.lifecycle.audit_writer._is_connection_error`
    so both writers agree on the boundary between "transient outage"
    and "programming error". Imported lazily through duck typing
    rather than directly so this module stays usable when the
    lifecycle writer's import graph is not loaded.
    """

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
    """Implements the ``insert_audit`` protocol against ``audit_events``.

    Wraps an externally-owned :class:`asyncpg.Pool` and performs a
    single ``INSERT`` per audit event. Connection-level failures are
    logged at WARNING and swallowed so the writer always honours the
    "audit failures must not mask the underlying request outcome"
    invariant (mirrors :class:`src.audit_sink.LoggingAuditSink`).

    The class is the canonical asyncpg wiring for prompt mutation
    events (audit sink wiring). It can be reused for any other ``audit_events``
    writer call site without modification - the SQL is parameterised
    on the entire event shape.
    """

    def __init__(
        self,
        *,
        pool: _PoolLike,
        logger_name: str = "admin_dashboard_api.audit_events",
    ) -> None:
        """Bind the writer to an existing pool.

        Args:
            pool: An asyncpg-pool-shaped object. The writer never calls
                ``close`` on the pool - lifecycle is the caller's
                responsibility (typically :func:`src.main.lifespan`).
            logger_name: Name of the logger used for diagnostics. The
                default keeps prompt audit logs adjacent to the
                LoggingAuditSink's namespace so log filters apply
                uniformly.
        """

        self._pool = pool
        self._logger = logging.getLogger(logger_name)

    async def insert_audit(self, event: AuditEvent) -> None:
        """Insert ``event`` into ``automation.audit_events``.

        Implements the :class:`audit_logger.AuditWriter` protocol so
        :class:`audit_logger.AuditLogger` can wrap this writer and
        enforce its application-layer ``actor_role`` validation
        (behavior 7.7) before a row ever reaches Postgres.

        Failures are classified into two buckets:

        * Connection-level (``OSError``, ``asyncpg.InterfaceError``,
          etc.) - logged at WARNING. The writer swallows the
          exception so the originating request returns its real
          outcome instead of a 5xx triggered by audit plumbing.
        * Programming errors (e.g. CHECK constraint violation from
          a malformed ``actor_role``) - logged at ERROR with the
          full exception type. Still swallowed so the request
          completes; the application-layer guard in
          :class:`audit_logger.AuditLogger` prevents most of these
          from reaching the SQL layer in the first place.
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
        except BaseException as exc:  # noqa: BLE001
            if _is_connection_error(exc):
                # Transient - log at WARNING. A retry queue lives on
                # the lifecycle writer but is intentionally NOT shared
                # here: prompt mutation events are low-volume and the
                # router emits a structured log line so a follow-up
                # ingestion pipeline can reconstruct the event from
                # the application logs if Postgres recovers.
                self._logger.warning(
                    "audit_events insert failed (connection-level): "
                    "action=%s actor=%s err=%s",
                    event.action,
                    event.actor_id,
                    exc,
                )
                return
            # Programming error - log at ERROR but still swallow.
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

        ``None`` is preserved (the column is nullable) so events that
        carry no structured detail end up with SQL ``NULL`` rather
        than the literal string ``"null"``.
        """

        if payload is None:
            return None
        # ``default=str`` so non-JSON-native objects (UUID, datetime)
        # round-trip without raising. ``sort_keys`` keeps the on-disk
        # JSONB byte-stable for tests that diff payloads.
        return json.dumps(payload, default=str, sort_keys=True)


# ---------------------------------------------------------------------------
# AsyncpgAuditSink - convenience wrapper matching the router's protocol
# ---------------------------------------------------------------------------


class AsyncpgAuditSink:
    """Minimal sink the prompts router consumes.

    The router (and the AdminProxy) expect an object with a single
    ``async write(event)`` method. The :class:`AuditLogger` from
    :mod:`audit_logger` exposes exactly that, but only when wired to
    a writer. This convenience class composes the two so callers can
    drop a single object onto ``app.state.prompts_audit_sink``.

    Example wiring (typically inside :func:`src.main.lifespan`):

    .. code-block:: python

        sink = AsyncpgAuditSink(pool=pg_pool)
        app.state.prompts_audit_sink = sink
    """

    def __init__(self, *, pool: _PoolLike) -> None:
        self._writer = AsyncpgAuditEventsWriter(pool=pool)
        self._logger = AuditLogger(writer=self._writer)

    async def write(self, event: AuditEvent) -> None:
        """Validate + persist ``event``.

        :class:`audit_logger.AuditLogger.write` enforces the
        ``actor_role IS NOT NULL`` invariant (behavior 7.7) before
        delegating to ``insert_audit``. Validation failures
        (``ValueError``) are NOT swallowed - the router's
        ``_safe_audit`` already wraps every call and logs them, so
        we let the application guard reach the call site as a
        proper exception. Connection-level failures are swallowed
        inside :class:`AsyncpgAuditEventsWriter`.
        """

        await self._logger.write(event)


__all__ = [
    "AsyncpgAuditEventsWriter",
    "AsyncpgAuditSink",
]
