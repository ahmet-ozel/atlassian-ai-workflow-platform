"""Audit search subpackage (platform-mimari-ops Requirements 6.5, 6.9).

Hosts the dataclasses (`types`) and the MinIO-backed
:class:`MinIOArchiveIndex` (`archive_index`) that the
``LokiSearchProxy`` (design §"LokiSearchProxy") consults when an audit
query's time range extends beyond ``RETENTION_DAYS``.

The archive index is read-only: writes to ``audit-archive`` are owned
by ``automation-worker.archive_audit_to_minio`` (task 13.2).
"""

from .archive_index import MinIOArchiveIndex
from .types import (
    ArchivedAuditHit,
    AuditQuery,
    AuditResult,
    TimeRange,
)

__all__ = [
    "ArchivedAuditHit",
    "AuditQuery",
    "AuditResult",
    "MinIOArchiveIndex",
    "TimeRange",
]
