"""audit_logger — frozen ``AuditEvent`` dataclass and write surface.

Re-exports the public API so callers can simply do::

    from audit_logger import AuditEvent, AuditLogger

The package mirrors the schema in
``.kiro/specs/platform-mimari-foundation/design.md`` §`libs/audit_logger`
and Requirement 7.7 (audit_role mandatory, enforced both at the
application layer and the Postgres `CHECK` constraint).
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
