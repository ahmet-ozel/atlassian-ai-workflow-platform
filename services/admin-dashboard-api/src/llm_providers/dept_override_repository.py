"""asyncpg-backed repository for ``automation.dept_llm_provider_overrides``.

Single class with three methods (:meth:`get`, :meth:`upsert`,
:meth:`delete`) that the
:meth:`llm_providers.service.ProviderService.set_override` /
:meth:`get_override` flows compose into the documented PUT / GET
endpoint behaviour (Requirements 10.2 — 10.5).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


__all__ = ["DeptOverrideRow", "DeptOverrideRepository"]


@dataclass(frozen=True)
class DeptOverrideRow:
    """One row from ``automation.dept_llm_provider_overrides``."""

    dept_id: str
    provider_id: UUID
    created_at: datetime


class DeptOverrideRepository:
    """asyncpg methods over the per-dept LLM override table."""

    async def get(
        self, conn: Any, dept_id: str
    ) -> DeptOverrideRow | None:
        """Return the override pinned to *dept_id* or ``None``."""

        row = await conn.fetchrow(
            """
            SELECT dept_id, provider_id, created_at
            FROM automation.dept_llm_provider_overrides
            WHERE dept_id = $1
            """,
            dept_id,
        )
        if row is None:
            return None
        return DeptOverrideRow(
            dept_id=str(row["dept_id"]),
            provider_id=row["provider_id"],
            created_at=row["created_at"],
        )

    async def upsert(
        self, conn: Any, dept_id: str, provider_id: UUID
    ) -> DeptOverrideRow:
        """INSERT … ON CONFLICT UPDATE to (re)pin *dept_id* to *provider_id*.

        The ON CONFLICT path swaps the ``provider_id`` and refreshes
        ``created_at`` so the read endpoint surfaces the moment the
        operator pinned the *current* provider — not the first one
        they ever assigned to the dept.
        """

        row = await conn.fetchrow(
            """
            INSERT INTO automation.dept_llm_provider_overrides
                (dept_id, provider_id, created_at)
            VALUES ($1, $2, now())
            ON CONFLICT (dept_id) DO UPDATE
                SET provider_id = EXCLUDED.provider_id,
                    created_at  = EXCLUDED.created_at
            RETURNING dept_id, provider_id, created_at
            """,
            dept_id,
            provider_id,
        )
        return DeptOverrideRow(
            dept_id=str(row["dept_id"]),
            provider_id=row["provider_id"],
            created_at=row["created_at"],
        )

    async def delete(self, conn: Any, dept_id: str) -> None:
        """Remove the override row for *dept_id* (no-op if absent)."""

        await conn.execute(
            """
            DELETE FROM automation.dept_llm_provider_overrides
            WHERE dept_id = $1
            """,
            dept_id,
        )
