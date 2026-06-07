"""Unit tests for ``automation_service.pr_supersede_log``.

Validates the contract documented on
:class:`automation_service.pr_supersede_log.PrSupersedeLogRepo`:

* :meth:`record` issues an ``INSERT ... ON CONFLICT DO NOTHING``
  against ``automation.pr_supersede_log`` with the
  ``(workflow_id, old_pr_id, new_pr_id)`` parameter order;
* the method returns ``True`` on a fresh insert and ``False`` on a
  duplicate ``(workflow_id, old_pr_id)`` pair (idempotency contract
  required by R10.1 - the ``iter_advance`` activity is retried under
  ``maximumAttempts <= 3``);
* the SQL uses the schema-qualified table name and the exact ON
  CONFLICT target so the PK constraint declared in
  ``platform/infra/postgres/11_workflows.sql`` is the single source
  of truth for idempotency.

The pool is mocked with the same shape used by
``tests/unit/test_replay.py`` so the test suite stays consistent and
does not require a live Postgres instance.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Ensure the automation-service src is importable (mirrors the
# bootstrap used by sibling unit tests in this directory).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from automation_service.pr_supersede_log import PrSupersedeLogRepo


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_pool() -> AsyncMock:
    """Build an ``asyncpg.Pool``-shaped mock with an ``acquire()`` CM.

    The mock matches the surface ``PrSupersedeLogRepo.record`` uses:
    ``async with pool.acquire() as conn: await conn.fetchrow(...)``.
    The acquired ``conn`` is exposed on ``pool._conn`` for assertion
    convenience.
    """

    pool = AsyncMock(spec=["acquire"])
    conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    pool._conn = conn  # exposed for direct assertion
    return pool


# =============================================================================
# record() - return-value contract
# =============================================================================


class TestRecord:
    """Tests for :meth:`PrSupersedeLogRepo.record`."""

    @pytest.mark.asyncio
    async def test_returns_true_when_row_inserted(
        self, mock_pool: AsyncMock
    ) -> None:
        """Fresh ``(workflow_id, old_pr_id)`` → ``RETURNING`` row → True."""

        mock_pool._conn.fetchrow.return_value = {
            "workflow_id": "automation-bb-payment-callbacks-pr-127"
        }
        repo = PrSupersedeLogRepo(mock_pool)

        result = await repo.record(
            "automation-bb-payment-callbacks-pr-127",
            old_pr_id=126,
            new_pr_id=127,
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_duplicate(
        self, mock_pool: AsyncMock
    ) -> None:
        """Duplicate PK → ``ON CONFLICT DO NOTHING`` swallows → False."""

        mock_pool._conn.fetchrow.return_value = None  # no RETURNING row
        repo = PrSupersedeLogRepo(mock_pool)

        result = await repo.record(
            "automation-bb-payment-callbacks-pr-127",
            old_pr_id=126,
            new_pr_id=127,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_idempotent_second_call_no_op(
        self, mock_pool: AsyncMock
    ) -> None:
        """Two calls with the same key - second returns False (R10.1)."""

        # First call simulates the fresh insert; second simulates the
        # ``ON CONFLICT DO NOTHING`` path.
        mock_pool._conn.fetchrow.side_effect = [
            {"workflow_id": "automation-bb-repo-pr-99"},
            None,
        ]
        repo = PrSupersedeLogRepo(mock_pool)

        first = await repo.record(
            "automation-bb-repo-pr-99", old_pr_id=98, new_pr_id=99
        )
        second = await repo.record(
            "automation-bb-repo-pr-99", old_pr_id=98, new_pr_id=99
        )

        assert first is True
        assert second is False
        assert mock_pool._conn.fetchrow.await_count == 2


# =============================================================================
# record() - SQL / parameter binding contract
# =============================================================================


class TestRecordSql:
    """Verify the SQL statement and parameter order shipped to asyncpg."""

    @pytest.mark.asyncio
    async def test_targets_qualified_table_and_columns(
        self, mock_pool: AsyncMock
    ) -> None:
        """SQL targets ``automation.pr_supersede_log`` with the exact columns."""

        mock_pool._conn.fetchrow.return_value = None
        repo = PrSupersedeLogRepo(mock_pool)

        await repo.record(
            "automation-bb-svc-pr-1", old_pr_id=10, new_pr_id=11
        )

        sql = mock_pool._conn.fetchrow.await_args.args[0]
        assert "automation.pr_supersede_log" in sql
        assert "workflow_id" in sql
        assert "old_pr_id" in sql
        assert "new_pr_id" in sql

    @pytest.mark.asyncio
    async def test_uses_on_conflict_do_nothing_on_pk(
        self, mock_pool: AsyncMock
    ) -> None:
        """Idempotency contract: ``ON CONFLICT (workflow_id, old_pr_id)``."""

        mock_pool._conn.fetchrow.return_value = None
        repo = PrSupersedeLogRepo(mock_pool)

        await repo.record(
            "automation-bb-svc-pr-1", old_pr_id=10, new_pr_id=11
        )

        sql = mock_pool._conn.fetchrow.await_args.args[0]
        normalised = " ".join(sql.split()).lower()
        assert "on conflict (workflow_id, old_pr_id) do nothing" in normalised
        assert "returning" in normalised  # needed for True/False signal

    @pytest.mark.asyncio
    async def test_parameter_order_matches_signature(
        self, mock_pool: AsyncMock
    ) -> None:
        """Positional args bound as ``$1=workflow_id, $2=old, $3=new``."""

        mock_pool._conn.fetchrow.return_value = None
        repo = PrSupersedeLogRepo(mock_pool)

        await repo.record(
            "automation-bb-payment-callbacks-pr-200",
            old_pr_id=199,
            new_pr_id=200,
        )

        # asyncpg call: fetchrow(sql, *args)
        call_args = mock_pool._conn.fetchrow.await_args.args
        assert call_args[1] == "automation-bb-payment-callbacks-pr-200"
        assert call_args[2] == 199
        assert call_args[3] == 200

    @pytest.mark.asyncio
    async def test_does_not_inject_superseded_at(
        self, mock_pool: AsyncMock
    ) -> None:
        """``superseded_at`` is owned by the DB ``DEFAULT now()``.

        Workflow code is replay-safe; injecting ``workflow.now()`` for
        ``superseded_at`` is unnecessary because the column has a
        ``DEFAULT now()`` server-side and the value is informational
        (not part of any business rule the workflow re-evaluates).
        Make sure the repo does not accidentally start passing a 4th
        positional argument.
        """

        mock_pool._conn.fetchrow.return_value = None
        repo = PrSupersedeLogRepo(mock_pool)

        await repo.record(
            "automation-bb-svc-pr-1", old_pr_id=10, new_pr_id=11
        )

        call_args = mock_pool._conn.fetchrow.await_args.args
        # sql + 3 positional bind params, exactly.
        assert len(call_args) == 4


# =============================================================================
# record() - connection lifecycle
# =============================================================================


class TestRecordConnectionLifecycle:
    """Verify the repo uses ``pool.acquire()`` as an async context manager."""

    @pytest.mark.asyncio
    async def test_acquires_and_releases_connection(
        self, mock_pool: AsyncMock
    ) -> None:
        mock_pool._conn.fetchrow.return_value = None
        repo = PrSupersedeLogRepo(mock_pool)

        await repo.record(
            "automation-bb-svc-pr-1", old_pr_id=10, new_pr_id=11
        )

        # Exactly one acquire per record() call; entered + exited.
        assert mock_pool.acquire.call_count == 1
        ctx = mock_pool.acquire.return_value
        ctx.__aenter__.assert_awaited_once()
        ctx.__aexit__.assert_awaited_once()
