"""Unit tests for src.seed_default_runner — boot-time SSH_HOST seed logic.

Spec: platform-quick-fixes — Task 7.2
Requirements: 4.3, 4.17, 4.19
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from src.seed_default_runner import _resolve_ssh_host, seed_default_runner


# ---------------------------------------------------------------------------
# _resolve_ssh_host tests
# ---------------------------------------------------------------------------


class TestResolveSshHost:
    """Tests for the SSH_HOST resolution helper."""

    def test_returns_ssh_host_when_set(self):
        with patch.dict("os.environ", {"SSH_HOST": "10.0.0.1"}, clear=False):
            assert _resolve_ssh_host() == "10.0.0.1"

    def test_returns_none_when_not_set(self):
        with patch.dict(
            "os.environ", {"SSH_HOST": "", "SSH_HOST_1": ""}, clear=False
        ):
            assert _resolve_ssh_host() is None

    def test_falls_back_to_ssh_host_1(self):
        with patch.dict(
            "os.environ", {"SSH_HOST": "", "SSH_HOST_1": "legacy-host"}, clear=False
        ):
            assert _resolve_ssh_host() == "legacy-host"

    def test_ssh_host_takes_precedence_over_ssh_host_1(self):
        with patch.dict(
            "os.environ",
            {"SSH_HOST": "primary", "SSH_HOST_1": "legacy"},
            clear=False,
        ):
            assert _resolve_ssh_host() == "primary"

    def test_strips_whitespace(self):
        with patch.dict("os.environ", {"SSH_HOST": "  host.example.com  "}, clear=False):
            assert _resolve_ssh_host() == "host.example.com"

    def test_whitespace_only_treated_as_empty(self):
        with patch.dict(
            "os.environ", {"SSH_HOST": "   ", "SSH_HOST_1": ""}, clear=False
        ):
            assert _resolve_ssh_host() is None


# ---------------------------------------------------------------------------
# seed_default_runner tests
# ---------------------------------------------------------------------------


class TestSeedDefaultRunner:
    """Tests for the async seed_default_runner function."""

    @pytest.mark.asyncio
    async def test_seeds_runner_when_ssh_host_set(self):
        """When SSH_HOST is set, should INSERT into ssh_runners and dept_ssh_assignments."""
        pool = AsyncMock()
        pool.execute = AsyncMock(return_value="INSERT 0 1")

        with patch.dict("os.environ", {"SSH_HOST": "10.0.0.5"}, clear=False):
            await seed_default_runner(pool)

        # Should have called execute twice: once for runner insert, once for dept assignment
        assert pool.execute.call_count == 2

        # First call: insert runner
        first_call_sql = pool.execute.call_args_list[0][0][0]
        assert "infrastructure.ssh_runners" in first_call_sql
        assert "ON CONFLICT (runner_id) DO NOTHING" in first_call_sql
        # The host parameter
        assert pool.execute.call_args_list[0][0][1] == "10.0.0.5"

        # Second call: assign departments
        second_call_sql = pool.execute.call_args_list[1][0][0]
        assert "infrastructure.dept_ssh_assignments" in second_call_sql
        assert "automation.departments" in second_call_sql
        assert "ON CONFLICT DO NOTHING" in second_call_sql

    @pytest.mark.asyncio
    async def test_no_seed_when_ssh_host_not_set_and_pool_empty(self, caplog):
        """When SSH_HOST is not set and runner pool is empty, should log warning."""
        pool = AsyncMock()
        pool.fetchval = AsyncMock(return_value=0)

        with patch.dict(
            "os.environ", {"SSH_HOST": "", "SSH_HOST_1": ""}, clear=False
        ):
            with caplog.at_level(logging.WARNING):
                await seed_default_runner(pool)

        # Should NOT have called execute (no seeding)
        pool.execute.assert_not_called()
        # Should have checked the count
        pool.fetchval.assert_called_once()
        # Should have logged the runner_pool_empty warning
        assert "runner_pool_empty" in caplog.text

    @pytest.mark.asyncio
    async def test_no_warning_when_ssh_host_not_set_but_pool_has_runners(self, caplog):
        """When SSH_HOST is not set but runners exist, no warning."""
        pool = AsyncMock()
        pool.fetchval = AsyncMock(return_value=3)

        with patch.dict(
            "os.environ", {"SSH_HOST": "", "SSH_HOST_1": ""}, clear=False
        ):
            with caplog.at_level(logging.WARNING):
                await seed_default_runner(pool)

        pool.execute.assert_not_called()
        assert "runner_pool_empty" not in caplog.text

    @pytest.mark.asyncio
    async def test_handles_table_not_exists_gracefully(self, caplog):
        """When the table doesn't exist yet, should not crash."""
        pool = AsyncMock()
        pool.fetchval = AsyncMock(
            side_effect=Exception("relation does not exist")
        )

        with patch.dict(
            "os.environ", {"SSH_HOST": "", "SSH_HOST_1": ""}, clear=False
        ):
            with caplog.at_level(logging.DEBUG):
                await seed_default_runner(pool)

        # Should not crash — graceful handling
        pool.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_seed_failure_gracefully(self, caplog):
        """When the INSERT fails, should log error but not crash."""
        pool = AsyncMock()
        pool.execute = AsyncMock(side_effect=Exception("connection refused"))

        with patch.dict("os.environ", {"SSH_HOST": "10.0.0.1"}, clear=False):
            with caplog.at_level(logging.ERROR):
                await seed_default_runner(pool)

        assert "Failed to seed default runner" in caplog.text

    @pytest.mark.asyncio
    async def test_idempotent_on_conflict(self):
        """ON CONFLICT DO NOTHING means re-running is safe."""
        pool = AsyncMock()
        # Simulate "INSERT 0 0" (no rows inserted due to conflict)
        pool.execute = AsyncMock(return_value="INSERT 0 0")

        with patch.dict("os.environ", {"SSH_HOST": "10.0.0.1"}, clear=False):
            # Should not raise
            await seed_default_runner(pool)

        assert pool.execute.call_count == 2
