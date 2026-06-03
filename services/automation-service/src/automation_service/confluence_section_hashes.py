"""Confluence section-hash dedup repository.

Backs the ``confluence_doc_update`` workflow's section-level write
dedup check. The :class:`ConfluenceSectionHashRepo`
exposes a single decision helper, :meth:`should_skip_section_update`,
which the AgentRunnerWorkflow consults before invoking the
``confluence_update_page`` MCP tool.

Operational contract:

* The four-tuple ``(workflow_id, page_id, section_path, content_hash)``
  is the natural idempotency key — its primary-key constraint is
  declared in ``platform/infra/postgres/11_workflows.sql``.
* ``should_skip_section_update`` returns ``True`` iff the same
  four-tuple is already in ``automation.confluence_section_hashes``;
  the caller skips the write **and** writes a
  ``confluence_section_dedup_skip`` audit row (audit emission lives in
  the workflow layer; this module only owns the idempotency lookup).
* ``False`` triggers an idempotent ``INSERT ... ON CONFLICT DO NOTHING``
  in the same transaction so a concurrent winner is recorded exactly
  once and replays of the activity never duplicate the row.
* No ``dept_id`` column is consulted: ``workflow_id`` already encodes
  the department boundary (workflow ids are formatted
  ``automation-jira-{PROJECT_KEY}-{ISSUE_NUM}`` /
  ``automation-bb-{REPO_SLUG}-pr-{PR_ID}`` per
  :mod:`temporal_shared.identifiers`).  The table therefore lives in
  the cross-dept idempotency segment of the ``automation`` schema and
  is not subject to RLS.

The module is intentionally tiny — every public method maps 1:1 onto a
SQL statement against ``automation.confluence_section_hashes``.  The
audit emission, overwrite-protection check, and ``_AI_PROBE_*`` filter
are deliberately *not* implemented here; they are
the AgentRunnerWorkflow's responsibility and consume this repo through
its narrow boolean contract.
"""

from __future__ import annotations

from typing import Protocol

import asyncpg


__all__ = ["ConfluenceSectionHashRepo", "PoolLike"]


class PoolLike(Protocol):
    """Structural type matching the slice of ``asyncpg.Pool`` used here.

    Declaring a ``Protocol`` lets unit tests pass an in-memory fake
    (the same ``_FakePool`` pattern already adopted by
    ``test_replay.py`` / ``test_webhook_to_work_item.py``) without
    constructing a real connection pool.  Production callers pass
    ``asyncpg.Pool`` instances bound to ``app.state.db`` at startup.
    """

    def acquire(self) -> object:  # pragma: no cover - structural typing
        ...


class ConfluenceSectionHashRepo:
    """Postgres-backed repository for section-hash dedup.

    Constructed once per service instance and held on
    ``app.state.confluence_section_hash_repo``.  Activities resolve
    the repo through dependency injection rather than building a
    fresh instance per call so connection-pool acquisition stays
    cheap.
    """

    __slots__ = ("_pool",)

    def __init__(self, pool: asyncpg.Pool | PoolLike) -> None:
        """Bind the repository to an existing connection pool.

        Parameters
        ----------
        pool:
            Connection pool already connected to the ``automation``
            schema.  The repo holds a reference but never owns the
            pool's lifecycle (close / reconnect is the host
            application's responsibility).
        """

        self._pool = pool

    async def should_skip_section_update(
        self,
        workflow_id: str,
        page_id: str,
        section_path: str,
        content_hash: str,
    ) -> bool:
        """Decide whether to skip a Confluence section update.

        Implementation strategy: a single
        ``INSERT ... ON CONFLICT DO NOTHING RETURNING workflow_id``.
        When the row already exists the ``RETURNING`` clause yields
        no rows and we report ``True`` (skip).  When the row is new,
        ``RETURNING`` yields the inserted ``workflow_id`` and we
        report ``False`` (proceed with the write).  This collapses
        the dedup lookup and the idempotent insert into a single
        round-trip, so the activity never observes a window where
        the lookup said ``False`` but a concurrent caller has since
        claimed the slot.

        Parameters
        ----------
        workflow_id:
            Workflow id of the AgentRunnerWorkflow that owns the
            update — formatted via
            :mod:`temporal_shared.identifiers`.  Encodes the
            department boundary.
        page_id:
            Confluence page id (string — Confluence ids are
            opaque tokens, not integers).
        section_path:
            Slash-delimited path of the section being updated
            (e.g. ``"§3.1/Implementation"``).  The Confluence
            update activity computes this from the page tree.
        content_hash:
            sha256 hex digest of the rendered section body.  The
            caller computes the digest before invoking this method
            so equivalent payloads with different formatting hash
            identically (the activity normalises whitespace before
            digesting).

        Returns
        -------
        bool
            ``True`` when the four-tuple is already in
            ``automation.confluence_section_hashes`` (skip the write
            and emit ``confluence_section_dedup_skip`` audit);
            ``False`` when the row was newly recorded (proceed with
            the Confluence update).
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO automation.confluence_section_hashes
                    (workflow_id, page_id, section_path, content_hash)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (workflow_id, page_id, section_path, content_hash)
                    DO NOTHING
                RETURNING workflow_id
                """,
                workflow_id,
                page_id,
                section_path,
                content_hash,
            )
            # A non-None row means the INSERT actually wrote — i.e. this
            # is the first time we see the four-tuple, so we proceed
            # with the Confluence update.  None means ON CONFLICT
            # swallowed the insert and the four-tuple is already
            # recorded, so we skip.
            return row is None
