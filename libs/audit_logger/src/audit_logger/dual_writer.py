"""Audit dual-write writer (`platform-mimari-ops` task 14.4).

**Validates: Requirement 6.1 / R6.6**

Wraps a primary asyncpg-backed audit writer with a best-effort
Loki side-channel so every audit event is observable from both
the structured query path (Postgres + RLS) and the operator's
log-search path (Loki via the Grafana Explore tab).

The Loki write is **best-effort**: a Loki outage MUST NOT cause
the primary Postgres insert to fail (the Postgres path is the
durable record). Failures are logged at WARNING and the row still
lands in Postgres.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .event import AuditEvent

__all__ = ["AuditDualWriter", "LokiPushClient"]


_LOG = logging.getLogger(__name__)


@runtime_checkable
class _AuditInserter(Protocol):
    """Minimal :class:`AuditWriter` surface."""

    async def insert_audit(self, event: AuditEvent) -> None: ...


@runtime_checkable
class LokiPushClient(Protocol):
    """Minimal Loki push surface.

    Production wiring uses the ``/loki/api/v1/push`` HTTP endpoint;
    tests inject a list-backed fake that records every emitted
    label set + line.
    """

    async def push(
        self,
        *,
        labels: dict[str, str],
        line: str,
        ts_ns: int | None = None,
    ) -> None: ...


@dataclass
class AuditDualWriter:
    """Compose a primary audit writer with a Loki side-channel.

    Args:
        primary: The durable audit writer (typically
            :class:`AsyncpgAuditEventsWriter`). The dual writer
            forwards every call to it; failures here re-raise so
            the caller's RLS contract is preserved.
        loki: Best-effort Loki client. May be ``None`` to disable
            the side-channel; a missing client makes the dual
            writer a thin pass-through.
    """

    primary: _AuditInserter
    loki: LokiPushClient | None = None

    async def insert_audit(self, event: AuditEvent) -> None:
        # Primary path FIRST so a Loki outage cannot block the
        # durable write. Re-raise on failure so the caller's audit
        # contract still surfaces the error.
        await self.primary.insert_audit(event)

        if self.loki is None:
            return

        labels = {
            "service": "audit",
            "action": event.action,
            "dept_id": event.dept_id or "_global",
            "actor_role": str(event.actor_role),
            "result": str(event.result),
        }
        line = json.dumps(
            {
                "actor_id": event.actor_id,
                "actor_role": event.actor_role,
                "dept_id": event.dept_id,
                "action": event.action,
                "resource": event.resource,
                "result": event.result,
                "payload": event.payload,
                "timestamp": event.timestamp.isoformat()
                if event.timestamp
                else None,
            },
            sort_keys=True,
            default=str,
            ensure_ascii=False,
        )
        try:
            ts_ns = (
                int(event.timestamp.timestamp() * 1_000_000_000)
                if event.timestamp is not None
                else None
            )
            await self.loki.push(labels=labels, line=line, ts_ns=ts_ns)
        except Exception as exc:  # noqa: BLE001 — best-effort
            _LOG.warning(
                "Loki push failed for audit event (action=%s): %s",
                event.action,
                exc,
            )
