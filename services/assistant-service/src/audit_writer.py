"""Asyncpg audit writer for assistant-service chat events."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol, runtime_checkable

from audit_logger import AuditEvent

__all__ = ["AsyncpgAuditEventsWriter"]

_INSERT_AUDIT_EVENT_SQL = (
    "INSERT INTO automation.audit_events "
    "(actor_id, actor_role, dept_id, action, resource, result, "
    "payload, created_at) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)"
)


@runtime_checkable
class _ConnectionLike(Protocol):
    async def execute(self, query: str, *args: Any) -> Any: ...


@runtime_checkable
class _PoolLike(Protocol):
    def acquire(self) -> Any: ...


class AsyncpgAuditEventsWriter:
    """Persist audit events through an externally owned asyncpg pool."""

    def __init__(
        self,
        *,
        pool: _PoolLike,
        logger_name: str = "assistant_service.audit_events",
    ) -> None:
        self._pool = pool
        self._logger = logging.getLogger(logger_name)

    async def insert_audit(self, event: AuditEvent) -> None:
        payload_json = None
        if event.payload is not None:
            payload_json = json.dumps(event.payload, default=str, sort_keys=True)
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
                self._logger.warning(
                    "audit_events insert failed: action=%s actor=%s err=%s",
                    event.action,
                    event.actor_id,
                    type(exc).__name__,
                )
                return
            self._logger.error(
                "audit_events insert failed: action=%s actor=%s err_type=%s",
                event.action,
                event.actor_id,
                type(exc).__name__,
            )


def _is_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, (OSError, ConnectionError, asyncio.TimeoutError)):
        return True
    return type(exc).__name__ in {
        "PostgresConnectionError",
        "ConnectionDoesNotExistError",
        "ConnectionFailureError",
        "CannotConnectNowError",
        "InterfaceError",
        "TimeoutError",
    }
