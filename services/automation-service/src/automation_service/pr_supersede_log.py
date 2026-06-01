"""PR supersede ledger — multi-iter PR transition log (task 3.4).

When ``AgentRunnerWorkflow`` advances to a new iteration and opens a
fresh draft PR, the ``iter_advance`` activity must record the
transition from the previous iteration's PR (``old_pr_id``) to the
new PR (``new_pr_id``) so the PO Review Inbox (R10.4) can render an
audit trail of multi-iter supersede events.

Design contract (design.md → "Postgres şeması — yeni / değişen
tablolar", `pr_supersede_log` block; tasks.md task 3.4)::

    record(workflow_id, old_pr_id, new_pr_id) -> bool
        Insert a row in automation.pr_supersede_log; idempotent via
        the (workflow_id, old_pr_id) PK so a retried iter_advance
        activity is a safe no-op on the second call.

Schema reference: ``platform/infra/postgres/11_workflows.sql`` block
4 (`automation.pr_supersede_log`):

* ``workflow_id`` text NOT NULL
* ``old_pr_id``   bigint NOT NULL
* ``new_pr_id``   bigint NOT NULL
* ``superseded_at`` timestamptz NOT NULL DEFAULT now()
* PRIMARY KEY (workflow_id, old_pr_id)

The repo is intentionally minimal — it owns exactly the one INSERT
the ``iter_advance`` activity needs. Read-side queries (PO Review
Inbox lookups) are owned by the API endpoint module that ships with
R10.4 and are out of scope for this task.

Validates: Requirement 10.1 (eski PR superseded etiketleme + log
satırı; ``iter_advance`` activity idempotent — PK constraint
guarantees no duplicate rows when the activity is retried).
"""

from __future__ import annotations

import logging
from typing import Final

import asyncpg

__all__ = ["PrSupersedeLogRepo"]

_LOG = logging.getLogger(__name__)

# Single-source SQL — kept at module scope so tests can assert on the
# exact statement shape (``ON CONFLICT DO NOTHING`` is the idempotency
# contract; mutating it without updating the contract test would be a
# silent regression on R10.1).
_INSERT_SQL: Final[
    str
] = """
INSERT INTO automation.pr_supersede_log
    (workflow_id, old_pr_id, new_pr_id)
VALUES ($1, $2, $3)
ON CONFLICT (workflow_id, old_pr_id) DO NOTHING
RETURNING workflow_id
"""


class PrSupersedeLogRepo:
    """Append-only ledger for multi-iter PR supersede transitions.

    The repo wraps an :class:`asyncpg.Pool` (or any object exposing
    the ``acquire()`` async context manager surface) and exposes a
    single :meth:`record` write API. The pool is injected so callers
    can share the application-level pool managed by ``main.py`` and
    so tests can swap in an in-memory fake without pulling Postgres
    into the unit-test path.

    Concurrency invariant: the ``(workflow_id, old_pr_id)`` PRIMARY
    KEY combined with ``ON CONFLICT DO NOTHING`` makes every retried
    or replayed call to :meth:`record` a safe no-op. The Temporal
    activity ``iter_advance`` may be retried under
    ``maximumAttempts <= 3`` (R1.6) without producing duplicate
    ledger rows.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Bind the repo to a connection pool.

        Args:
            pool: The asyncpg pool connected to the automation
                database. Stored by reference; the repo never
                acquires connections outside of the :meth:`record`
                call so there is no lifecycle to manage at the repo
                level.
        """

        self._pool = pool

    async def record(
        self,
        workflow_id: str,
        old_pr_id: int,
        new_pr_id: int,
    ) -> bool:
        """Insert a supersede log row; idempotent on (workflow_id, old_pr_id).

        The row layout maps 1:1 onto the
        ``automation.pr_supersede_log`` columns declared in
        ``platform/infra/postgres/11_workflows.sql``. The
        ``superseded_at`` column is intentionally not exposed on the
        Python side: the DB ``DEFAULT now()`` is the single source of
        truth so the workflow code (which must remain replay-safe)
        does not have to inject a timestamp.

        Args:
            workflow_id: The Temporal workflow id that owns the
                supersede transition (format
                ``automation-bb-{repo_slug}-pr-{pr_id}`` per
                ``temporal_shared.identifiers``). Forms half of the
                PK.
            old_pr_id: The Bitbucket PR id of the previous
                iteration's draft PR. Must be a positive integer;
                the column is ``bigint`` so any 64-bit value is
                accepted.
            new_pr_id: The Bitbucket PR id of the freshly opened
                iteration draft PR. Recorded for audit; not part of
                the PK so a re-issued ``iter_advance`` for the same
                ``(workflow_id, old_pr_id)`` keeps the original
                ``new_pr_id`` and reports ``False``.

        Returns:
            ``True`` when a new ledger row was inserted (first
            supersede for this ``(workflow_id, old_pr_id)`` pair).
            ``False`` when the same pair was already recorded — the
            activity is a no-op on the second call. Callers that
            need to distinguish "first transition" from "retry" can
            branch on this value; ``iter_advance`` ignores it
            because the side-effects (Bitbucket label + description
            prepend) are themselves idempotent.

        Raises:
            asyncpg.PostgresError: Propagated unchanged on
                connection / query failures so the Temporal activity
                retry loop sees the original error class. The repo
                does not swallow errors.
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                _INSERT_SQL,
                workflow_id,
                old_pr_id,
                new_pr_id,
            )

        inserted = row is not None
        if inserted:
            _LOG.info(
                "pr_supersede_log.recorded "
                "workflow_id=%s old_pr_id=%d new_pr_id=%d",
                workflow_id,
                old_pr_id,
                new_pr_id,
            )
        else:
            _LOG.debug(
                "pr_supersede_log.duplicate "
                "workflow_id=%s old_pr_id=%d (idempotent no-op)",
                workflow_id,
                old_pr_id,
            )
        return inserted
