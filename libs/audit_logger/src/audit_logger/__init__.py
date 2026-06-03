"""audit_logger — frozen ``AuditEvent`` dataclass and write surface.

Re-exports the public API so callers can simply do::

    from audit_logger import AuditEvent, AuditLogger

The package mirrors the audit table schema. ``audit_role`` is mandatory
and is enforced both at the application layer and by the Postgres
`CHECK` constraint.
"""

from .event import (
    AUDIT_ACTOR_ROLES,
    AUDIT_RESULTS,
    AuditEvent,
    AuditResult,
    AuditRole,
)
from .writer import AuditLogger, AuditWriter

__all__ = [
    "AUDIT_ACTOR_ROLES",
    "AUDIT_RESULTS",
    "AuditEvent",
    "AuditLogger",
    "AuditResult",
    "AuditRole",
    "AuditWriter",
]
