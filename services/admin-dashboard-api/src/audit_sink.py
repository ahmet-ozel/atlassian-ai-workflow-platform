"""Audit sink wiring for the admin-dashboard-api.

This module provides the audit-write surface consumed by
:class:`src.proxy.AdminProxy`. The proxy emits a single
:class:`audit_logger.AuditEvent` with ``action="rbac_denied"`` whenever
it rejects a request for RBAC reasons (Requirement 7.5 / 7.7).

The audit_logger library (``platform/libs/audit_logger``) exposes a
:class:`audit_logger.AuditLogger` write surface that delegates to a
duck-typed sink with a single ``insert_audit(event)`` async method.
The asyncpg-backed implementation that targets the ``audit_events``
table lives in task group 4 of ``platform-mimari-foundation`` (R7.7
``CHECK (actor_role IS NOT NULL)``) and is not yet wired into this
service. Until that lands we ship a **logging adapter** here:

* :class:`LoggingAuditSink` formats every event as a structured log
  line on the ``admin_dashboard_api.audit`` logger. The redaction
  filter from task 9.1 still applies to that logger, so any free-text
  ``payload`` field that accidentally embeds a credential pattern is
  scrubbed before it reaches stdout.

* The class implements the same ``write(event)`` shape that the proxy
  expects (see :class:`src.proxy._AuditSink`). When task 4.x lands we
  swap the constructor argument in :func:`src.main.lifespan` for the
  asyncpg-backed writer without touching the proxy.

The logging shape mirrors the columns of the ``audit_events`` Postgres
table so log-to-warehouse pipelines can ingest the same JSON envelope
that the database row carries (action, actor_id, actor_role, dept_id,
resource, result, timestamp, payload).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from audit_logger import AuditEvent

logger = logging.getLogger("admin_dashboard_api.audit")


class LoggingAuditSink:
    """In-process audit sink that writes events as structured log lines.

    Used as a stand-in for the asyncpg-backed
    ``audit_events`` writer until task 4.x lands. The sink's public
    ``write(event)`` method matches the
    :class:`src.proxy._AuditSink` protocol so the proxy can be wired
    against it without changes.

    The class is intentionally tiny — it holds no state and never
    raises. A failure to format the event is logged at ``WARNING``
    instead of being propagated, because audit-write failures must
    not mask the underlying HTTP 403 (Requirement 7.5; see
    :meth:`src.proxy.AdminProxy._emit_rbac_denied`).
    """

    def __init__(self, *, logger_name: str = "admin_dashboard_api.audit") -> None:
        self._logger = logging.getLogger(logger_name)

    async def write(self, event: AuditEvent) -> None:
        """Render ``event`` as a single structured log line.

        The output is one JSON object per call; it can be picked up
        by any log-aggregation pipeline. The keys mirror the
        ``audit_events`` Postgres columns so the wire shape is stable
        across the logging-only and asyncpg-backed writers.
        """

        try:
            line = self._render(event)
        except Exception as exc:  # noqa: BLE001 — never raise from a sink
            self._logger.warning(
                "LoggingAuditSink could not serialise audit event "
                "(action=%s, actor=%s): %s",
                event.action,
                event.actor_id,
                exc,
            )
            return

        # ``info`` so the audit row sits at a level above DEBUG (which
        # is reserved for noisy diagnostics). The redaction filter
        # installed by ``http_shared.install_redaction_filter`` runs
        # before this line reaches stdout, so a stray credential in
        # ``payload`` is scrubbed.
        self._logger.info(line)

    @staticmethod
    def _render(event: AuditEvent) -> str:
        """Serialise ``event`` to a single-line JSON string."""

        envelope: dict[str, Any] = {
            "actor_id": event.actor_id,
            "actor_role": event.actor_role,
            "dept_id": event.dept_id,
            "action": event.action,
            "resource": event.resource,
            "result": event.result,
            "timestamp": event.timestamp.isoformat(),
        }
        if event.payload is not None:
            envelope["payload"] = event.payload
        # ``default=str`` so non-JSON-native objects (eg. ``UUID``) on
        # the payload do not blow the writer up.
        return json.dumps(envelope, default=str, sort_keys=True)


class AsyncpgAuditSink:
    """asyncpg-backed audit sink writing rows into ``automation.audit_events``.

    Mirrors the writer used by the automation-service lifespan handler
    (``automation_service.audit_writer.AsyncpgAuditEventsWriter``):
    one ``INSERT`` per :meth:`write` call, JSON-encoded ``payload``,
    swallowing every database error so the audit pipeline never masks
    the request outcome (R12.7).

    Used by the ``llm-provider-management`` spec — task 7.1 wires this
    sink onto ``app.state.audit_logger`` in :mod:`src.main` so the
    :class:`llm_providers.service.ProviderService` constructed per
    request lands its events in the same Postgres table as the rest of
    the admin surface.

    The class accepts any object exposing
    ``acquire() -> async-context-manager-of-connection`` so tests can
    inject a hand-rolled pool fake without monkey-patching asyncpg.
    """

    #: SQL kept short and stable so a future schema migration that adds
    #: columns can append to the column list without breaking the
    #: ``$N`` parameter numbering.
    _INSERT_SQL: str = (
        "INSERT INTO automation.audit_events "
        "  (actor_id, actor_role, dept_id, action, resource, result, "
        "   payload, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)"
    )

    def __init__(
        self,
        *,
        pool: Any,
        logger_name: str = "admin_dashboard_api.audit",
    ) -> None:
        self._pool = pool
        self._logger = logging.getLogger(logger_name)

    async def write(self, event: AuditEvent) -> None:
        """Persist ``event`` to ``automation.audit_events``.

        Failures are logged at WARNING and swallowed — the service
        layer's outer ``try/except`` then surfaces the HTTP response
        regardless (R12.7).
        """

        payload_json: str | None
        if event.payload is None:
            payload_json = None
        else:
            try:
                payload_json = json.dumps(
                    event.payload, default=str, sort_keys=True
                )
            except Exception as exc:  # noqa: BLE001 - audit serialisation
                self._logger.warning(
                    "AsyncpgAuditSink could not serialise audit payload "
                    "(action=%s): %s",
                    event.action,
                    exc,
                )
                payload_json = None

        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    self._INSERT_SQL,
                    event.actor_id,
                    event.actor_role,
                    event.dept_id,
                    event.action,
                    event.resource,
                    event.result,
                    payload_json,
                    event.timestamp,
                )
        except Exception as exc:  # noqa: BLE001 - audit failures never escape
            self._logger.warning(
                "AsyncpgAuditSink write failed (action=%s actor=%s): %s",
                event.action,
                event.actor_id,
                exc,
            )


__all__ = ["LoggingAuditSink", "AsyncpgAuditSink"]
