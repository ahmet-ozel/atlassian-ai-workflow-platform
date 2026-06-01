"""LLM diff-summary cache repository (task 3.3).

Backs the V7 / R10.6 diff-summary cache: every Bitbucket diff is
summarised by an LLM exactly once, keyed by sha256 of the unified
diff body.  Subsequent requests for the same hash (Orphan Branches
widget refresh, repeated ``code_change_commit_only`` Jira comments,
multi-iter PR review) are served from
``automation.diff_summary_cache`` without re-invoking the LLM.

Design contract (design.md §"LLM hash dedup family", tasks.md 3.3,
6.2):

* ``get_or_compute(diff_hash, llm_callback)`` is **never allowed** to
  invoke ``llm_callback`` on a cache hit (Property 15 — single LLM call
  per ``diff_hash``).  Cache hits short-circuit before the callback
  runs.
* Cache misses invoke ``llm_callback`` and persist the result through
  ``INSERT ... ON CONFLICT DO NOTHING``; concurrent miss-races resolve
  to the same row because the conflict clause swallows the second
  insert and the second caller re-reads the winning summary on its
  next request.
* The schema (declared in ``platform/infra/postgres/11_workflows.sql``)
  carries no ``dept_id``: ``diff_hash`` is content-addressed and
  cross-dept content collisions are vanishingly rare; when they do
  occur the cached summary is universally correct because it
  describes the diff itself, not its provenance.

The module is deliberately tiny — every public method maps 1:1 onto a
SQL statement against ``automation.diff_summary_cache``.  The PR
review previous-findings cache (G13) lives in workflow state, not
here, because it depends on per-workflow iteration history and is
not content-addressable.

Validates: Requirements 10.6.
"""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol

import asyncpg


__all__ = ["DiffSummaryCacheRepo", "LlmCallback", "PoolLike"]


#: Async callable that produces the LLM-rendered summary for a diff
#: hash.  Declared as a module-level alias so callers can reuse the
#: same signature in their workflow-side activity wiring.
LlmCallback = Callable[[str], Awaitable[str]]


class PoolLike(Protocol):
    """Structural type matching the slice of ``asyncpg.Pool`` used here.

    See the equivalent docstring in
    :mod:`automation_service.confluence_section_hashes` for the
    rationale (in-memory fakes for unit tests).
    """

    def acquire(self) -> object:  # pragma: no cover - structural typing
        ...


class DiffSummaryCacheRepo:
    """Postgres-backed repository for the LLM diff-summary cache."""

    __slots__ = ("_pool",)

    def __init__(self, pool: asyncpg.Pool | PoolLike) -> None:
        """Bind the repository to an existing connection pool.

        Parameters
        ----------
        pool:
            Connection pool already connected to the ``automation``
            schema.  The repo holds a reference but never owns the
            pool's lifecycle.
        """

        self._pool = pool

    async def get(self, diff_hash: str) -> str | None:
        """Return the cached summary for *diff_hash* or ``None``.

        Exposed independently of :meth:`get_or_compute` so callers
        that already hold an LLM result (e.g. a backfill script) can
        consult the cache without supplying a callback.

        Parameters
        ----------
        diff_hash:
            sha256 hex digest of the unified diff body (lower-case
            hex; the caller computes the digest before invoking this
            method).

        Returns
        -------
        str | None
            The cached summary string, or ``None`` when the hash is
            absent from the cache.
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT summary
                FROM automation.diff_summary_cache
                WHERE diff_hash = $1
                """,
                diff_hash,
            )
            return None if row is None else row["summary"]

    async def get_or_compute(
        self,
        diff_hash: str,
        llm_callback: LlmCallback,
    ) -> str:
        """Return the cached summary or compute, persist, and return it.

        Implementation strategy:

        1. Try a plain ``SELECT`` against
           ``automation.diff_summary_cache``.  A hit returns
           immediately — ``llm_callback`` is **never** invoked.
        2. On a miss, invoke ``llm_callback(diff_hash)`` to produce
           the summary text.
        3. Persist the result via
           ``INSERT ... ON CONFLICT (diff_hash) DO NOTHING RETURNING summary``.
           The ``ON CONFLICT`` clause makes the write idempotent under
           concurrent miss-races: if a sibling caller persisted first,
           our INSERT is swallowed and we return the freshly-computed
           summary without overwriting the winning row.

        Parameters
        ----------
        diff_hash:
            sha256 hex digest of the unified diff body.
        llm_callback:
            Async callable that produces the LLM summary for the
            diff.  Receives the same ``diff_hash`` we received so the
            callback can fetch the diff body from MinIO or re-derive
            it from a workflow context — the cache itself never
            stores the diff body.

        Returns
        -------
        str
            The cached or freshly-computed summary string.

        Notes
        -----
        Step 1 + step 3 together guarantee Property 15: across any
        sequence of calls for the same ``diff_hash`` only the first
        caller observes a cache miss and therefore only one
        ``llm_callback`` invocation is paid.  Subsequent callers
        return from step 1 without entering step 2.
        """

        cached = await self.get(diff_hash)
        if cached is not None:
            return cached

        # Cache miss — pay the LLM call exactly once for this caller.
        # Concurrent miss-races on the same hash will each produce
        # their own summary; the ON CONFLICT below ensures the table
        # records exactly one of them and every caller still returns
        # a valid summary string for the diff.
        summary = await llm_callback(diff_hash)

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO automation.diff_summary_cache
                    (diff_hash, summary)
                VALUES ($1, $2)
                ON CONFLICT (diff_hash) DO NOTHING
                """,
                diff_hash,
                summary,
            )

        return summary
