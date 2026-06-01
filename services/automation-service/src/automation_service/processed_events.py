"""Webhook ``delivery_id`` replay-dedup repository (task 3.2).

The :class:`ProcessedEventsRepo` is the HTTP-layer idempotency
ledger for webhook deliveries. It backs the ``replay_dedup`` stage of
the :class:`automation_service.webhook_filters.WebhookFilterChain`
and the rollback-on-503 retry path of the
``signalWithStart`` dispatcher (R2.4).

Design contract (design.md → "Postgres şeması — yeni / değişen
tablolar", ``processed_events`` block; tasks.md task 3.2; design.md
"Property 18: processed_events idempotent dedup at HTTP layer")::

    claim(delivery_id, provider) -> bool
        INSERT ... ON CONFLICT DO NOTHING into automation.processed_events.
        Returns True iff this is the first insert (the caller owns the
        webhook → workflow start path); False iff the row already exists
        (the caller emits a ``duplicate_event_dropped`` HTTP 200).

    is_processed(delivery_id) -> bool
        SELECT 1 FROM automation.processed_events WHERE delivery_id = $1.
        Returns True iff a claim row exists for the given delivery id;
        used by the webhook filter chain's ``replay_dedup`` callback.

Rollback contract (R2.4): when ``signalWithStart`` fails with HTTP
503 (Temporal cluster unavailable), the surrounding webhook handler
SHALL roll back the ``processed_events`` row so the webhook provider's
retry can re-claim the same ``delivery_id``. The repo does NOT own
the rollback transaction itself — it exposes the explicit
:meth:`release` helper which the handler calls inside its except
block. Tests cover the ``claim → release → claim`` round-trip as
part of Property 18.

Schema reference: ``platform/infra/postgres/11_workflows.sql`` block
1 (``automation.processed_events``):

* ``delivery_id``  TEXT PRIMARY KEY
* ``provider``     TEXT NOT NULL CHECK (provider IN ('jira','bitbucket'))
* ``received_at``  TIMESTAMPTZ NOT NULL DEFAULT now()

The repo is intentionally minimal — every public method maps 1:1
onto a SQL statement against ``automation.processed_events``. Audit
emission, the burst-debounce window, and the ``signalWithStart``
dispatch itself are all the webhook handler's responsibility and
consume this repo through its narrow boolean contract.

Validates: Requirements 1.8, 2.4, 2.5, 2.6.
"""

from __future__ import annotations

import logging
from typing import Final, Literal, Protocol

import asyncpg

__all__ = ["ProcessedEventsRepo", "PoolLike", "Provider"]

_LOG = logging.getLogger(__name__)


# ``provider`` mirrors the SQL CHECK constraint. Keeping it as a Literal
# lets static type checkers reject unknown providers at the call site
# before the database raises a constraint violation. The set is closed
# by design (R3.1: gateway exposes exactly two webhook endpoints).
Provider = Literal["jira", "bitbucket"]


# Single-source SQL strings — held at module scope so contract tests
# can assert on the exact statement shape (the ``ON CONFLICT DO NOTHING``
# clause IS the idempotency contract; mutating it without updating the
# Property 18 invariants would be a silent regression on R1.8 / R2.5).
_CLAIM_SQL: Final[str] = """
INSERT INTO automation.processed_events
    (delivery_id, provider)
VALUES ($1, $2)
ON CONFLICT (delivery_id) DO NOTHING
RETURNING delivery_id
"""

_IS_PROCESSED_SQL: Final[str] = """
SELECT 1
FROM automation.processed_events
WHERE delivery_id = $1
"""

_RELEASE_SQL: Final[str] = """
DELETE FROM automation.processed_events
WHERE delivery_id = $1
"""


class PoolLike(Protocol):
    """Structural type matching the slice of ``asyncpg.Pool`` used here.

    Declaring a ``Protocol`` lets unit and property tests pass an
    in-memory fake (the ``_FakePool`` pattern shared by
    ``test_replay.py`` and ``test_webhook_to_work_item.py``) without
    constructing a real connection pool. Production callers pass
    ``asyncpg.Pool`` instances bound to ``app.state.db`` at startup.
    """

    def acquire(self) -> object:  # pragma: no cover - structural typing
        ...


class ProcessedEventsRepo:
    """Postgres-backed repository for webhook ``delivery_id`` dedup.

    Constructed once per service instance and held on
    ``app.state.processed_events_repo``. The webhook handler resolves
    the repo through dependency injection rather than building a
    fresh instance per call so connection-pool acquisition stays
    cheap.

    Concurrency invariant: the ``delivery_id`` PRIMARY KEY combined
    with ``ON CONFLICT DO NOTHING`` makes :meth:`claim` linearisable
    against concurrent callers. At most one caller observes ``True``
    for any given ``delivery_id``; every other caller observes
    ``False``. Property 18 (a) covers this invariant under
    ``RuleBasedStateMachine`` exercise.
    """

    __slots__ = ("_pool",)

    def __init__(self, pool: asyncpg.Pool | PoolLike) -> None:
        """Bind the repo to an existing connection pool.

        Args:
            pool: Connection pool already connected to the
                ``automation`` schema. The repo holds a reference
                but never owns the pool's lifecycle (close /
                reconnect is the host application's responsibility).
        """

        self._pool = pool

    async def claim(self, delivery_id: str, provider: str) -> bool:
        """Atomically claim a webhook ``delivery_id`` for processing.

        Implementation strategy: a single
        ``INSERT ... ON CONFLICT DO NOTHING RETURNING delivery_id``.
        When the row is new the ``RETURNING`` clause yields the
        inserted ``delivery_id`` and we report ``True`` — the caller
        owns the workflow-start path. When the row already exists,
        ``RETURNING`` yields no rows and we report ``False`` — the
        caller emits ``duplicate_event_dropped`` and HTTP 200.
        Collapsing the lookup and the insert into a single
        round-trip closes the race window where a concurrent caller
        could otherwise also observe ``True``.

        Args:
            delivery_id: The provider-assigned webhook delivery id.
                For Jira this is the value of the
                ``X-Atlassian-Webhook-Delivery`` header; for
                Bitbucket the ``X-Request-UUID`` header. Must be a
                non-empty string; the column is ``TEXT`` so any
                length is accepted but the caller is expected to
                pass the raw header value verbatim.
            provider: ``"jira"`` or ``"bitbucket"``. The SQL CHECK
                constraint rejects any other value with a
                ``CheckViolationError``; the Python side surfaces the
                error unchanged so the webhook handler can return
                HTTP 400 with the original DB diagnostic.

        Returns:
            ``True`` when a fresh row was inserted (this caller owns
            the webhook → workflow start path); ``False`` when the
            same ``delivery_id`` was already claimed (replay).

        Raises:
            asyncpg.PostgresError: Propagated unchanged on
                connection / query failures so the webhook handler
                can map them to the right HTTP status (constraint
                violations → 400, transient pool errors → 503).
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_CLAIM_SQL, delivery_id, provider)

        inserted = row is not None
        if inserted:
            _LOG.debug(
                "processed_events.claimed delivery_id=%s provider=%s",
                delivery_id,
                provider,
            )
        else:
            _LOG.debug(
                "processed_events.duplicate delivery_id=%s "
                "(idempotent no-op)",
                delivery_id,
            )
        return inserted

    async def is_processed(self, delivery_id: str) -> bool:
        """Check whether a ``delivery_id`` has been claimed.

        Used by the webhook filter chain's ``replay_dedup`` stage as a
        read-only predicate. Property 18 (b) requires that every
        successful :meth:`claim` is observable through this method
        for the lifetime of the row.

        Args:
            delivery_id: Same shape as :meth:`claim`'s argument.

        Returns:
            ``True`` iff a row with the given ``delivery_id`` exists
            in ``automation.processed_events``.
        """

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(_IS_PROCESSED_SQL, delivery_id)

        return row is not None

    async def release(self, delivery_id: str) -> bool:
        """Roll back a previously-claimed ``delivery_id`` (R2.4).

        Called from the webhook handler's exception path when
        ``signalWithStart`` fails with HTTP 503 (Temporal cluster
        unavailable). Removing the row lets the webhook provider's
        retry observe ``True`` again on the next :meth:`claim` so the
        same delivery is processed exactly once across the full
        retry envelope.

        The operation is itself idempotent — releasing a
        ``delivery_id`` that was already released (or never claimed)
        is a no-op and returns ``False``. Property 18 (c) covers the
        round-trip ``claim → release → claim → True`` invariant.

        Args:
            delivery_id: The id originally passed to :meth:`claim`.

        Returns:
            ``True`` when a row was removed; ``False`` when no row
            matched (idempotent no-op — also the case after a
            successful prior release).
        """

        async with self._pool.acquire() as conn:
            status = await conn.execute(_RELEASE_SQL, delivery_id)

        # asyncpg returns a status string of the form ``DELETE <count>``;
        # we report True iff the count is non-zero. The trailing token
        # is always present for ``DELETE`` so the split is safe; we
        # defensively handle malformed strings by treating them as
        # zero-row deletes (consistent with the idempotent contract).
        try:
            removed = int(status.split()[-1]) > 0
        except (IndexError, ValueError):  # pragma: no cover - defensive
            removed = False

        if removed:
            _LOG.info(
                "processed_events.released delivery_id=%s "
                "(signalWithStart rollback)",
                delivery_id,
            )
        return removed
