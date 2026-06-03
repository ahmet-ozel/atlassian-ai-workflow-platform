"""``AuditEvent`` dataclass — the canonical audit row shape.

The schema follows the audit table shape:

.. code-block:: python

    @dataclass(frozen=True)
    class AuditEvent:
        actor_id: str
        actor_role: Literal["viewer", "lead", "admin", "dept_admin", "system"]
        dept_id: str | None
        action: str          # "capability_denied", "loop_guard_dropped", ...
        resource: str
        result: Literal["ok", "denied", "error"]
        timestamp: datetime
        payload: dict | None  # optional structured detail

The ``payload`` field is added on top of the design pseudocode to
capture optional structured detail (eg. ``{"missing": ["bitbucket_write"]}``
for ``capability_denied``). It is serialised to the Postgres
``payload jsonb NULL`` column declared in ``10_automation.sql``.

Rationale
---------

* ``frozen=True`` — audit rows are append-only; mutating an in-flight
  event would defeat the purpose of the log. The dataclass is
  effectively a value object.
* ``Literal`` types — they mirror the Postgres ``CHECK`` columns so a
  typo at the application layer is caught at type-check time rather
  than only at INSERT time.
* No defaults — every column on the audit table is mandatory by
  design; making the dataclass mirror that shape
  forces callers to populate each field explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

# ---------------------------------------------------------------------------
# Enum-like literals
# ---------------------------------------------------------------------------

#: The four RBAC roles plus the synthetic
#: ``"system"`` role used by background processes (webhook handlers,
#: probe runner, capability gate). These values mirror the
#: ``actor_role`` ``CHECK`` constraint declared in
#: ``infra/postgres/init/10_automation.sql``.
AuditRole = Literal["viewer", "lead", "admin", "dept_admin", "system"]

#: Mirror of ``AuditRole`` for runtime introspection (eg. for
#: argument validation in tests). Kept in sync with the ``Literal``
#: above by the unit test ``test_audit_event_role_enum_in_sync``.
AUDIT_ACTOR_ROLES: Final[frozenset[str]] = frozenset(
    {"viewer", "lead", "admin", "dept_admin", "system"}
)

#: Outcome values mirror the ``result`` ``CHECK`` constraint.
AuditResult = Literal["ok", "denied", "error"]

#: Runtime mirror of ``AuditResult``.
AUDIT_RESULTS: Final[frozenset[str]] = frozenset({"ok", "denied", "error"})


# ---------------------------------------------------------------------------
# AuditEvent
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditEvent:
    """Single append-only audit row.

    Attributes mirror the columns of the ``audit_events`` Postgres
    table:

    Args:
        actor_id: The ``account_id`` (Atlassian) or OIDC ``sub`` of the
            user / bot that performed the action. For background
            processes this is the bot's account_id (eg.
            ``"bot.payment.jira"``).
        actor_role: One of the four RBAC roles or ``"system"`` for
            background processes. Required — Postgres rejects
            ``NULL`` and :class:`AuditLogger` rejects empty values.
        dept_id: Optional department id; ``None`` for cross-department
            system events (eg. global prompt change).
        action: Short string identifier for the event type
            (eg. ``"capability_denied"``, ``"rbac_denied"``,
            ``"loop_guard_dropped"``, ``"webhook_dept_unresolved"``,
            ``"dept_duplicate_id"``).
        resource: Identifier of the affected resource — typically a
            ``"workflow:<type>"`` or ``"department:<id>"`` URN-like
            string. The schema does not enforce a particular shape;
            callers should pick a stable convention per action.
        result: ``"ok"`` for successful actions, ``"denied"`` for
            policy / RBAC / capability rejections, ``"error"`` for
            unexpected failures.
        timestamp: When the event happened. Should be timezone-aware
            (UTC); naive datetimes are accepted but the writer will
            attach the row's database-side ``DEFAULT now()`` if
            this column is omitted at the SQL layer.
        payload: Optional JSON-serialisable mapping carrying
            structured detail. ``None`` is acceptable for events that
            need no extra context.
    """

    actor_id: str
    actor_role: AuditRole
    dept_id: str | None
    action: str
    resource: str
    result: AuditResult
    timestamp: datetime
    payload: dict[str, Any] | None = None
