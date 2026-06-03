"""Audit search data classes used by the operations surface.

Defines the frozen dataclasses used so the
``LokiSearchProxy`` and the :class:`MinIOArchiveIndex` exchange
strongly-typed values rather than ad-hoc dicts.

All dataclasses are ``frozen=True, slots=True`` to keep equality /
hashability semantics intact for use as Hypothesis-derived test
values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Inclusive lower / exclusive upper time window.

    The ``end`` boundary is **exclusive** to match the half-open
    convention used by Loki's ``range_query`` API; this also makes
    set-difference reasoning (``cutoff < end``) straightforward.

    Both ``start`` and ``end`` MUST be timezone-aware (UTC by
    convention). Naive datetimes are rejected at construction time.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("TimeRange.start and .end must be tz-aware")
        if self.end <= self.start:
            raise ValueError(
                f"TimeRange end must be > start; got start={self.start!r}, "
                f"end={self.end!r}"
            )


@dataclass(frozen=True, slots=True)
class AuditQuery:
    """Filter parameters passed from ``LokiSearchProxy`` to the archive."""

    actor_id: str | None
    dept_id: str | None
    action: str | None
    time_range: TimeRange


@dataclass(frozen=True, slots=True)
class ArchivedAuditHit:
    """A single archived audit row materialised from MinIO.

    ``archived`` is hard-coded to ``True`` for archived rows;
    the field is kept for symmetry with future ``archived: False``
    Loki hits in the unified ``AuditResult.loki`` tuple.
    """

    id: str
    archived: Literal[True]
    archive_uri: str  # ``s3://audit-archive/{Y}/{M}/{D}/audit-N.jsonl.gz``
    summary: str


# AuditHit (Loki side) is not part of archive index wiring's scope, but the
# union type is required by ``AuditResult``. We define
# it here as a structural placeholder so the package is internally
# consistent; the LokiSearchProxy implementation will refine it with concrete
# fields.
@dataclass(frozen=True, slots=True)
class AuditHit:
    """Loki-side audit hit (placeholder for LokiSearchProxy results).

    Kept minimal here so the archive index wiring archive_index module compiles
    standalone; the full shape lands when the LokiSearchProxy router
    is implemented.
    """

    id: str
    actor_id: str
    dept_id: str
    action: str
    at: datetime
    summary: str


@dataclass(frozen=True, slots=True)
class AuditResult:
    """Unified search result returned by ``LokiSearchProxy.search``."""

    loki: tuple[AuditHit, ...]
    archived: tuple[ArchivedAuditHit, ...]


__all__ = [
    "ArchivedAuditHit",
    "AuditHit",
    "AuditQuery",
    "AuditResult",
    "TimeRange",
]
