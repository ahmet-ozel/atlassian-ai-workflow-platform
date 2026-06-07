"""``AuditLogger`` - application-layer write surface for audit events.

The writer enforces the **mandatory ``actor_role``** invariant
at the application layer: if a caller hands us an :class:`AuditEvent` whose
``actor_role`` is ``None`` or the empty string, ``write()`` raises
:class:`ValueError` *before* any database round-trip.

Postgres also enforces this with a ``CHECK (actor_role IS NOT NULL ...)``
column on ``audit_events`` (declared in
``infra/postgres/init/10_automation.sql``). The application-layer
guard exists so callers fail fast with a clear traceback instead of
surfacing an opaque integrity error.

DB integration
--------------

The writer expects an injected session that conforms to a small
:class:`AuditWriter` :class:`~typing.Protocol`. In production this
will be a :mod:`db_shared`-backed session; the protocol shape lets
tests inject an in-memory fake without pulling Postgres into the
test path.

The actual ``INSERT INTO audit_events ...`` SQL is materialised
alongside the schema migration; this library calls
``writer.insert_audit(event)`` and lets the implementation fan out to
SQL. Splitting the validation surface from the SQL emitter keeps
this library framework-agnostic (``asyncpg`` vs ``SQLAlchemy``).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .event import AUDIT_ACTOR_ROLES, AuditEvent


@runtime_checkable
class AuditWriter(Protocol):
    """Minimal sink interface the :class:`AuditLogger` writes through.

    Production code wires this to a :mod:`db_shared` tenant-aware
    session whose ``insert_audit`` runs the canonical SQL emitted by
    the database layer. Tests can inject a list-backed fake whose
    ``insert_audit`` simply appends the event for later assertion.

    The protocol is intentionally tiny - only the single ``insert_audit``
    call is part of the contract. Any retry / deferred-queue policy
    lives in the underlying implementation, not here.
    """

    async def insert_audit(self, event: AuditEvent) -> None:
        """Persist ``event`` to the ``audit_events`` table."""

        ...


class AuditLogger:
    """Write surface for :class:`AuditEvent` rows.

    Args:
        writer: An :class:`AuditWriter` (typically a tenant-aware
            session from :mod:`db_shared`) responsible for the actual
            INSERT. The logger only validates inputs and delegates.

    Example::

        from audit_logger import AuditEvent, AuditLogger

        logger = AuditLogger(writer=session)
        await logger.write(event)
    """

    def __init__(self, writer: AuditWriter) -> None:
        self._writer = writer

    async def write(self, event: AuditEvent) -> None:
        """Validate the mandatory ``actor_role`` invariant and INSERT.

        Args:
            event: The :class:`AuditEvent` to persist.

        Raises:
            ValueError: If ``event.actor_role`` is ``None`` or an
                empty / whitespace-only string. The Postgres
                ``CHECK (actor_role IS NOT NULL ...)`` column enforces
                the same rule at the database layer; this guard
                catches the violation earlier with a clearer message.
            ValueError: If ``event.actor_role`` is not one of the
                values in :data:`AUDIT_ACTOR_ROLES`. The Postgres
                ``CHECK`` constraint also rejects unknown roles, but
                catching it here lets the caller see the offending
                value in the traceback.
        """

        role = event.actor_role
        # The dataclass annotation is a ``Literal``, but ``Literal`` is
        # a static-type hint only - at runtime callers can still pass
        # ``None``, an empty string, or a typo. We surface those cases
        # explicitly so the failure mode is clear.
        if role is None:
            raise ValueError(
                "AuditEvent.actor_role is required and must not be None "
                "(mirrored by the Postgres audit_events.actor_role "
                "CHECK constraint)."
            )
        if not isinstance(role, str) or not role.strip():
            raise ValueError(
                "AuditEvent.actor_role must be a non-empty string "
                f"(got {role!r}). Empty roles are not allowed."
            )
        if role not in AUDIT_ACTOR_ROLES:
            raise ValueError(
                f"AuditEvent.actor_role={role!r} is not one of the "
                f"allowed roles {sorted(AUDIT_ACTOR_ROLES)!r}. The "
                "Postgres audit_events.actor_role CHECK constraint "
                "would reject this value at INSERT time."
            )

        await self._writer.insert_audit(event)
