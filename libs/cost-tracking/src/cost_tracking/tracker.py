"""``CostTracker`` - idempotent insert into ``shared.cost_tracking``.

The tracker is the single point through which every LLM-consuming service writes cost
rows; it owes its callers two invariants:

1. **Idempotency** - two ``record`` calls with the same ``activity_id``
   produce exactly one row. Enforced by the Postgres
   ``UNIQUE(activity_id)`` constraint (``20_ops.sql``) plus
   ``INSERT ... ON CONFLICT (activity_id) DO NOTHING``.
2. **Schema discipline** - ``cost_tag``, ``provider`` and the
   non-negative invariants on ``token_in`` / ``token_out`` /
   ``cost_usd`` are validated at the application layer too so the
   ``CHECK`` constraints never surface as opaque
   :class:`asyncpg.exceptions.CheckViolationError` to callers.

Every conflict path emits a ``cost_tracking_duplicate_dropped`` audit
event so retries remain observable through
the audit ledger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from .types import CostTag, ProviderName

__all__ = [
    "CostEntry",
    "CostTracker",
    "CostTrackerStore",
    "InMemoryCostStore",
    "build_asyncpg_store",
]


_LOG = logging.getLogger(__name__)


#: Audit action emitted on the conflict path. Mirrors the constant
#: pinned by ``platform/tests/property/test_cost_tracking_idempotent.py``.
DUPLICATE_AUDIT_ACTION = "cost_tracking_duplicate_dropped"


@dataclass(frozen=True, slots=True)
class CostEntry:
    """Row shape inserted by :meth:`CostTracker.record`.

    Mirrors the column set declared in ``shared.cost_tracking`` minus
    the auto-populated ``id`` and ``created_at`` columns.

    Validation rules enforced by :meth:`CostTracker.record`:

    * ``activity_id`` must be a non-empty string (matches the Postgres
      ``UNIQUE`` constraint key).
    * ``provider`` must be one of :data:`PROVIDER_NAMES`.
    * ``cost_tag`` must be one of :data:`COST_TAGS`.
    * ``token_in``, ``token_out`` and ``cost_usd`` must be non-negative.
    """

    activity_id: str
    dept_id: str
    user_id: str | None
    workflow_id: str | None
    model: str
    provider: ProviderName
    token_in: int
    token_out: int
    cost_usd: float
    cost_tag: CostTag = "production"
    created_at: datetime | None = None  # ``None`` ⇒ Postgres defaults to ``now()``


@runtime_checkable
class CostTrackerStore(Protocol):
    """Persistence surface ``CostTracker`` writes through.

    Two compatible shapes are accepted:

    * A boolean :meth:`insert` that returns ``True`` when a row landed
      and ``False`` when the ``UNIQUE`` constraint rejected the insert.
    * The reference store used by the property test, exposing
      :meth:`insert_with_on_conflict` returning ``True`` *on conflict*
      (note: inverted polarity).

    :meth:`CostTracker.record` adapts to whichever method is available
    so the production asyncpg-backed store and the test fake share the
    same call site.
    """

    async def insert(self, entry: CostEntry) -> bool: ...


@runtime_checkable
class _AuditEmitter(Protocol):
    """Subset of the audit logger needed by the conflict path.

    The property test ships a list-backed fake whose ``emit(action,
    payload)`` signature exactly matches; production wires a thin
    adapter around the foundation :class:`audit_logger.AuditLogger`.
    """

    def emit(self, action: str, payload: dict[str, Any]) -> None: ...


@dataclass
class CostTracker:
    """Idempotent cost insert.

    Records one LLM activity cost row per ``activity_id``. A unique
    ``activity_id`` collapses retries to a single row, and every conflict
    emits a ``cost_tracking_duplicate_dropped`` audit event.

    Args:
        db: Persistence surface (production: asyncpg-backed; tests:
            list-backed fake). Either :meth:`insert` or
            :meth:`insert_with_on_conflict` is consulted (see
            :meth:`record`).
        audit: Audit emitter - receives ``cost_tracking_duplicate_dropped``
            on every conflict drop. ``None`` disables the emit (used
            by services that already audit the cost activity itself).
    """

    db: Any
    audit: _AuditEmitter | None = None

    async def record(self, entry: CostEntry) -> bool:
        """Persist ``entry`` to ``shared.cost_tracking`` exactly once.

        Args:
            entry: Fully populated :class:`CostEntry`.

        Returns:
            ``True`` if a new row landed, ``False`` if an earlier call
            with the same ``activity_id`` already wrote a row
            (idempotent retry - the store's ``ON CONFLICT DO NOTHING``
            consumed the duplicate).

        Raises:
            ValueError: ``entry`` violates one of the application-layer
                invariants (negative tokens / cost, unknown
                provider / cost_tag, empty ``activity_id``).
        """

        _validate(entry)

        # Adapt to either store shape. The reference store used by the
        # property test exposes ``insert_with_on_conflict`` whose return
        # value is *inverted* (``True`` ⇒ conflict). The production
        # asyncpg store exposes ``insert`` returning ``True`` on a
        # successful insert. Branch defensively so the same tracker
        # works in both contexts.
        inserted: bool
        if hasattr(self.db, "insert_with_on_conflict"):
            conflict = self.db.insert_with_on_conflict(entry)
            inserted = not conflict
        else:
            inserted = await self.db.insert(entry)

        if not inserted:
            _LOG.info(
                "cost_tracking duplicate_dropped",
                extra={
                    "activity_id_prefix": entry.activity_id[:8],
                    "dept_id": entry.dept_id,
                    "cost_tag": entry.cost_tag,
                },
            )
            if self.audit is not None:
                self.audit.emit(
                    DUPLICATE_AUDIT_ACTION,
                    {
                        "activity_id": entry.activity_id,
                        "dept_id": entry.dept_id,
                    },
                )
        return inserted


# ---------------------------------------------------------------------------
# In-memory store (used by property tests when production store unavailable)
# ---------------------------------------------------------------------------


@dataclass
class InMemoryCostStore:
    """List-backed store. Enforces ``UNIQUE(activity_id)`` in pure Python.

    Convenience for unit tests that don't need the property test's
    full reference store but want to exercise the ``CostTracker`` API
    end-to-end.
    """

    rows: list[CostEntry] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)

    async def insert(self, entry: CostEntry) -> bool:
        if entry.activity_id in self._seen:
            return False
        self._seen.add(entry.activity_id)
        self.rows.append(entry)
        return True


# ---------------------------------------------------------------------------
# Production asyncpg store
# ---------------------------------------------------------------------------


def build_asyncpg_store(pool: Any) -> CostTrackerStore:
    """Wrap an ``asyncpg.Pool`` so it satisfies :class:`CostTrackerStore`.

    Returns an object whose :meth:`insert` issues ``INSERT ... ON
    CONFLICT (activity_id) DO NOTHING RETURNING id`` and projects the
    result to ``True`` / ``False``.
    """

    class _AsyncpgStore:
        def __init__(self, _pool: Any) -> None:
            self._pool = _pool

        async def insert(self, entry: CostEntry) -> bool:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    INSERT INTO shared.cost_tracking (
                        activity_id, workflow_id, dept_id, user_id,
                        model, provider, token_in, token_out,
                        cost_usd, cost_tag, created_at
                    )
                    VALUES (
                        $1, $2, $3, $4,
                        $5, $6, $7, $8,
                        $9, $10, COALESCE($11, now())
                    )
                    ON CONFLICT (activity_id) DO NOTHING
                    RETURNING id
                    """,
                    entry.activity_id,
                    entry.workflow_id,
                    entry.dept_id,
                    entry.user_id,
                    entry.model,
                    entry.provider,
                    entry.token_in,
                    entry.token_out,
                    entry.cost_usd,
                    entry.cost_tag,
                    entry.created_at,
                )
            return row is not None

    return _AsyncpgStore(pool)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate(entry: CostEntry) -> None:
    if not entry.activity_id:
        raise ValueError("CostEntry.activity_id must be non-empty")
    if not entry.dept_id:
        raise ValueError("CostEntry.dept_id must be non-empty")
    if entry.token_in < 0:
        raise ValueError("CostEntry.token_in must be >= 0")
    if entry.token_out < 0:
        raise ValueError("CostEntry.token_out must be >= 0")
    if entry.cost_usd < 0:
        raise ValueError("CostEntry.cost_usd must be >= 0")
    if entry.provider not in {"vllm", "openai", "anthropic"}:
        raise ValueError(
            f"CostEntry.provider {entry.provider!r} not in "
            "{'vllm','openai','anthropic'}"
        )
    if entry.cost_tag not in {"production", "sandbox", "probe"}:
        raise ValueError(
            f"CostEntry.cost_tag {entry.cost_tag!r} not in "
            "{'production','sandbox','probe'}"
        )
    if entry.created_at is not None and entry.created_at.tzinfo is None:
        # asyncpg requires timezone-aware datetimes for TIMESTAMPTZ.
        raise ValueError(
            "CostEntry.created_at must be timezone-aware (use UTC)"
        )
    # Suppress unused-import / lint noise for the ``timezone`` import.
    _ = timezone
