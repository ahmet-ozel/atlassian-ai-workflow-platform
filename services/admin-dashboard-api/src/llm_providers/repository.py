"""asyncpg-backed repository for ``automation.llm_providers``.

Implements the design's "Components › repository.py" contract: a single
class with narrow, single-purpose methods so the
:mod:`llm_providers.service` layer stays focused on business logic
(transaction orchestration, Vault writes, audit emission) and the
router stays a thin HTTP shim.

The repository holds no state of its own — every method accepts an
asyncpg ``Connection`` (or ``Transaction`` member connection) so the
service layer can wrap mutations in a single transaction.  This makes
the repository trivially mockable in unit tests that bypass the
database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from .schemas import ProviderUpdate


__all__ = ["LLMProviderRow", "LLMProviderRepository", "VAULT_PATH_PREFIX"]


#: Canonical KV-v2 mount prefix for LLM provider credentials. The
#: ``vault_path`` column is computed at INSERT time from this prefix +
#: the row's ``id``; the service layer also reuses this constant to
#: validate that persisted rows reference the expected Vault location.
VAULT_PATH_PREFIX: str = "secret/data/llm-providers"


@dataclass(frozen=True)
class LLMProviderRow:
    """Read-side projection of an ``automation.llm_providers`` row.

    The repository converts asyncpg ``Record`` rows into this dataclass
    so the service layer never sees the raw DB tuple shape; tests
    construct instances directly with hand-built values.
    """

    id: UUID
    provider_type: str
    name: str
    model: str
    context_length: int
    base_url: str | None
    vault_path: str
    status: str
    reasoning_effort: str | None
    verbosity: str | None
    last_tested_at: datetime | None
    last_test_error: str | None
    created_at: datetime
    updated_at: datetime


class LLMProviderRepository:
    """Single-purpose asyncpg methods over ``automation.llm_providers``.

    Every method takes an asyncpg ``Connection`` so the service layer
    can wrap mutations in a single transaction; the repository owns
    no connection lifecycle.
    """

    async def insert(
        self,
        conn: Any,
        *,
        provider_id: UUID,
        provider_type: str,
        name: str,
        model: str,
        context_length: int,
        base_url: str | None,
        reasoning_effort: str | None = None,
        verbosity: str | None = None,
    ) -> LLMProviderRow:
        """INSERT a fresh provider row and return the persisted shape.

        ``vault_path`` is computed from :data:`VAULT_PATH_PREFIX` so the
        service layer never has to thread the path through manually.
        """

        vault_path = f"{VAULT_PATH_PREFIX}/{provider_id}/credentials"
        row = await conn.fetchrow(
            """
            INSERT INTO automation.llm_providers
                (id, provider_type, name, model, context_length,
                 base_url, vault_path, reasoning_effort, verbosity)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            RETURNING id, provider_type, name, model, context_length,
                      base_url, vault_path, status, reasoning_effort,
                      verbosity, last_tested_at, last_test_error,
                      created_at, updated_at
            """,
            provider_id,
            provider_type,
            name,
            model,
            context_length,
            base_url,
            vault_path,
            reasoning_effort,
            verbosity,
        )
        return _row_to_dataclass(row)

    async def list_all(self, conn: Any) -> list[LLMProviderRow]:
        """Return every provider row, newest first."""

        rows = await conn.fetch(
            """
            SELECT id, provider_type, name, model, context_length,
                   base_url, vault_path, status, reasoning_effort,
                   verbosity, last_tested_at, last_test_error,
                   created_at, updated_at
            FROM automation.llm_providers
            ORDER BY created_at DESC
            """
        )
        return [_row_to_dataclass(r) for r in rows]

    async def get(
        self, conn: Any, provider_id: UUID
    ) -> LLMProviderRow | None:
        """Fetch the row for *provider_id* or ``None`` if absent."""

        row = await conn.fetchrow(
            """
            SELECT id, provider_type, name, model, context_length,
                   base_url, vault_path, status, reasoning_effort,
                   verbosity, last_tested_at, last_test_error,
                   created_at, updated_at
            FROM automation.llm_providers
            WHERE id = $1
            """,
            provider_id,
        )
        return _row_to_dataclass(row) if row is not None else None

    async def update(
        self,
        conn: Any,
        provider_id: UUID,
        patch: ProviderUpdate,
    ) -> LLMProviderRow | None:
        """Merge *patch* over the persisted row.

        Only non-``None`` patch fields are written; the SQL ``UPDATE``
        uses ``COALESCE($N, column)`` so the existing value is preserved
        when the patch leaves a field unset. ``updated_at`` is bumped
        to ``now()`` on every call.

        Returns the post-update row, or ``None`` if no row exists for
        *provider_id*.
        """

        row = await conn.fetchrow(
            """
            UPDATE automation.llm_providers
            SET name             = COALESCE($2, name),
                model            = COALESCE($3, model),
                context_length   = COALESCE($4, context_length),
                base_url         = COALESCE($5, base_url),
                status           = COALESCE($6, status),
                reasoning_effort = COALESCE($7, reasoning_effort),
                verbosity        = COALESCE($8, verbosity),
                updated_at       = now()
            WHERE id = $1
            RETURNING id, provider_type, name, model, context_length,
                      base_url, vault_path, status, reasoning_effort,
                      verbosity, last_tested_at, last_test_error,
                      created_at, updated_at
            """,
            provider_id,
            patch.name,
            patch.model,
            patch.context_length,
            str(patch.base_url) if patch.base_url is not None else None,
            patch.status,
            patch.reasoning_effort,
            patch.verbosity,
        )
        return _row_to_dataclass(row) if row is not None else None

    async def delete(self, conn: Any, provider_id: UUID) -> bool:
        """DELETE the row. Returns ``True`` iff a row was removed."""

        result = await conn.execute(
            "DELETE FROM automation.llm_providers WHERE id = $1",
            provider_id,
        )
        # ``execute`` returns ``"DELETE <n>"`` — parse out the count.
        try:
            return int(result.split()[-1]) > 0
        except (ValueError, IndexError):
            return False

    async def update_test_result(
        self,
        conn: Any,
        provider_id: UUID,
        *,
        last_tested_at: datetime,
        last_test_error: str | None,
    ) -> None:
        """Persist the result of a connection test.

        Touches only the two test-result columns; ``updated_at`` is
        deliberately NOT bumped here so the operator can see the
        latest *configuration* timestamp separately from the latest
        *test* timestamp on the read endpoints (R5.3).
        """

        await conn.execute(
            """
            UPDATE automation.llm_providers
            SET last_tested_at = $2,
                last_test_error = $3
            WHERE id = $1
            """,
            provider_id,
            last_tested_at,
            last_test_error,
        )

    async def overrides_referencing(
        self, conn: Any, provider_id: UUID
    ) -> list[str]:
        """Return the ``dept_id`` set pinning to *provider_id* (R1.7).

        Used by :meth:`llm_providers.service.ProviderService.delete` as
        a precondition for the ``provider_in_use`` 409 surface — the
        service refuses to delete a provider any dept still references.
        Returns ``[]`` when no dept pins to the provider.
        """

        rows = await conn.fetch(
            """
            SELECT dept_id
            FROM automation.dept_llm_provider_overrides
            WHERE provider_id = $1
            ORDER BY dept_id
            """,
            provider_id,
        )
        return [str(r["dept_id"]) for r in rows]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _row_to_dataclass(record: Any) -> LLMProviderRow:
    """Convert an asyncpg ``Record`` into :class:`LLMProviderRow`."""

    return LLMProviderRow(
        id=record["id"],
        provider_type=str(record["provider_type"]),
        name=str(record["name"]),
        model=str(record["model"]),
        context_length=int(record["context_length"]),
        base_url=(
            str(record["base_url"]) if record["base_url"] is not None else None
        ),
        vault_path=str(record["vault_path"]),
        status=str(record["status"]),
        reasoning_effort=_optional_str(_record_get(record, "reasoning_effort")),
        verbosity=_optional_str(_record_get(record, "verbosity")),
        last_tested_at=record["last_tested_at"],
        last_test_error=(
            str(record["last_test_error"])
            if record["last_test_error"] is not None
            else None
        ),
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


def _record_get(record: Any, key: str) -> Any:
    """Read *key* from an asyncpg ``Record`` or mapping, tolerating absence.

    Hand-built test records may predate the tuning columns; treat a
    missing key as ``None`` rather than raising ``KeyError``.
    """

    try:
        return record[key]
    except (KeyError, IndexError):
        return None


def _optional_str(value: Any) -> str | None:
    """Coerce a non-``None`` value to ``str``; pass ``None`` through."""

    return str(value) if value is not None else None
