"""Unit tests for the runner_resolver activity.

Tests the least-busy runner selection algorithm, error handling when
no active runners are available, and audit event writing.

Spec: platform-quick-fixes — Task 7.3
Requirements: 4.5, 4.6, 4.7, 4.16
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.activities.runner_resolver import (
    RunnerResolution,
    RunnerResolutionError,
    _fetch_runners,
    _write_audit_event,
    resolve_runner,
)


class TestRunnerResolution:
    """Tests for the RunnerResolution dataclass."""

    def test_to_dict(self):
        """RunnerResolution.to_dict() returns all fields."""
        resolution = RunnerResolution(
            runner_id="runner-1",
            host="10.0.0.1",
            port=22,
            username="ai-runner",
            base_path="/srv/ai-runner",
            vault_path="vault:ssh/runners/runner-1/active",
        )
        result = resolution.to_dict()
        assert result == {
            "runner_id": "runner-1",
            "host": "10.0.0.1",
            "port": 22,
            "username": "ai-runner",
            "base_path": "/srv/ai-runner",
            "vault_path": "vault:ssh/runners/runner-1/active",
        }

    def test_frozen(self):
        """RunnerResolution is immutable."""
        resolution = RunnerResolution(
            runner_id="r1",
            host="host",
            port=22,
            username="user",
            base_path="/var/ai-runner",
            vault_path="path",
        )
        with pytest.raises(AttributeError):
            resolution.runner_id = "r2"  # type: ignore[misc]


class TestRunnerResolutionError:
    """Tests for the RunnerResolutionError exception."""

    def test_default_audit_event(self):
        """Default audit_event is 'no_runner_assigned_to_dept'."""
        err = RunnerResolutionError("No runner")
        assert err.audit_event == "no_runner_assigned_to_dept"
        assert "No runner" in str(err)

    def test_custom_audit_event(self):
        """Custom audit_event can be specified."""
        err = RunnerResolutionError(
            "Custom error", audit_event="custom_event"
        )
        assert err.audit_event == "custom_event"


class TestResolveRunner:
    """Tests for the resolve_runner activity function."""

    @pytest.mark.asyncio
    async def test_selects_least_busy_runner(self):
        """When multiple runners exist, selects the one with fewest active workflows."""
        mock_pool = AsyncMock()
        mock_pool.fetch = AsyncMock(
            return_value=[
                {
                    "runner_id": "runner-2",
                    "host": "10.0.0.2",
                    "port": 22,
                    "username": "ai-runner",
                    "vault_path": "vault:ssh/runners/runner-2/active",
                    "active_count": 1,
                },
                {
                    "runner_id": "runner-1",
                    "host": "10.0.0.1",
                    "port": 22,
                    "username": "ai-runner",
                    "vault_path": "vault:ssh/runners/runner-1/active",
                    "active_count": 3,
                },
            ]
        )
        mock_pool.close = AsyncMock()

        mock_asyncpg = MagicMock()
        mock_asyncpg.create_pool = AsyncMock(return_value=mock_pool)

        with (
            patch.dict("sys.modules", {"asyncpg": mock_asyncpg}),
            patch("src.activities.runner_resolver._write_audit_event") as mock_audit,
            patch("temporalio.activity.logger") as _mock_logger,
        ):
            result = await resolve_runner("dept-payment")

        assert result["runner_id"] == "runner-2"
        assert result["host"] == "10.0.0.2"
        assert result["port"] == 22
        assert result["username"] == "ai-runner"
        assert result["vault_path"] == "vault:ssh/runners/runner-2/active"

        # Verify audit event was written with correct selection_reason
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args[1]
        assert call_kwargs["action"] == "ssh_runner_selected"
        assert call_kwargs["dept_id"] == "dept-payment"
        assert call_kwargs["metadata"]["runner_id"] == "runner-2"
        assert call_kwargs["metadata"]["selection_reason"] == "least_busy"

    @pytest.mark.asyncio
    async def test_selects_only_runner(self):
        """When only one runner exists, selects it with reason 'only_one'."""
        mock_pool = AsyncMock()
        mock_pool.fetch = AsyncMock(
            return_value=[
                {
                    "runner_id": "default",
                    "host": "10.0.0.1",
                    "port": 22,
                    "username": "ai-runner",
                    "vault_path": "vault:ssh/runners/default/active",
                    "active_count": 0,
                },
            ]
        )
        mock_pool.close = AsyncMock()

        mock_asyncpg = MagicMock()
        mock_asyncpg.create_pool = AsyncMock(return_value=mock_pool)

        with (
            patch.dict("sys.modules", {"asyncpg": mock_asyncpg}),
            patch("src.activities.runner_resolver._write_audit_event") as mock_audit,
            patch("temporalio.activity.logger") as _mock_logger,
        ):
            result = await resolve_runner("dept-hr")

        assert result["runner_id"] == "default"

        # Verify selection_reason is 'only_one'
        call_kwargs = mock_audit.call_args[1]
        assert call_kwargs["metadata"]["selection_reason"] == "only_one"

    @pytest.mark.asyncio
    async def test_raises_when_no_active_runners(self):
        """When no active runners are assigned, raises RunnerResolutionError."""
        mock_pool = AsyncMock()
        mock_pool.fetch = AsyncMock(return_value=[])
        mock_pool.close = AsyncMock()

        mock_asyncpg = MagicMock()
        mock_asyncpg.create_pool = AsyncMock(return_value=mock_pool)

        with (
            patch.dict("sys.modules", {"asyncpg": mock_asyncpg}),
            patch("src.activities.runner_resolver._write_audit_event") as mock_audit,
            patch("temporalio.activity.logger") as _mock_logger,
        ):
            with pytest.raises(RunnerResolutionError) as exc_info:
                await resolve_runner("dept-empty")

        assert "dept-empty" in str(exc_info.value)
        assert exc_info.value.audit_event == "no_runner_assigned_to_dept"

        # Verify audit event was written for the failure
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args[1]
        assert call_kwargs["action"] == "no_runner_assigned_to_dept"
        assert call_kwargs["dept_id"] == "dept-empty"

    @pytest.mark.asyncio
    async def test_priority_tiebreaker(self):
        """When active_count is equal, priority determines selection."""
        # The query already orders by active_count ASC, priority ASC
        # So the first row returned is the winner
        mock_pool = AsyncMock()
        mock_pool.fetch = AsyncMock(
            return_value=[
                {
                    "runner_id": "runner-high-priority",
                    "host": "10.0.0.3",
                    "port": 2222,
                    "username": "deploy",
                    "vault_path": "vault:ssh/runners/runner-high-priority/active",
                    "active_count": 2,
                },
                {
                    "runner_id": "runner-low-priority",
                    "host": "10.0.0.4",
                    "port": 22,
                    "username": "ai-runner",
                    "vault_path": "vault:ssh/runners/runner-low-priority/active",
                    "active_count": 2,
                },
            ]
        )
        mock_pool.close = AsyncMock()

        mock_asyncpg = MagicMock()
        mock_asyncpg.create_pool = AsyncMock(return_value=mock_pool)

        with (
            patch.dict("sys.modules", {"asyncpg": mock_asyncpg}),
            patch("src.activities.runner_resolver._write_audit_event") as mock_audit,
            patch("temporalio.activity.logger") as _mock_logger,
        ):
            result = await resolve_runner("dept-finance")

        # First row wins (ordered by priority in the query)
        assert result["runner_id"] == "runner-high-priority"
        assert result["port"] == 2222
        assert result["username"] == "deploy"


class TestFetchRunners:
    """Tests for the _fetch_runners helper with fallback logic."""

    @pytest.mark.asyncio
    async def test_uses_primary_query(self):
        """When workflow_executions table exists, uses the full query."""
        mock_pool = AsyncMock()
        mock_pool.fetch = AsyncMock(
            return_value=[
                {
                    "runner_id": "r1",
                    "host": "h1",
                    "port": 22,
                    "username": "u1",
                    "vault_path": "v1",
                    "active_count": 0,
                },
            ]
        )

        with patch("temporalio.activity.logger"):
            result = await _fetch_runners(mock_pool, "dept-x")

        assert len(result) == 1
        assert result[0]["runner_id"] == "r1"

    @pytest.mark.asyncio
    async def test_falls_back_when_table_missing(self):
        """When workflow_executions table doesn't exist, falls back to priority-only."""
        mock_pool = AsyncMock()

        # First call raises (table doesn't exist), second call succeeds
        mock_pool.fetch = AsyncMock(
            side_effect=[
                Exception(
                    'relation "temporal.workflow_executions" does not exist'
                ),
                [
                    {
                        "runner_id": "r1",
                        "host": "h1",
                        "port": 22,
                        "username": "u1",
                        "vault_path": "v1",
                        "active_count": 0,
                    },
                ],
            ]
        )

        with patch("temporalio.activity.logger"):
            result = await _fetch_runners(mock_pool, "dept-y")

        assert len(result) == 1
        assert result[0]["runner_id"] == "r1"
        # Should have been called twice (primary + fallback)
        assert mock_pool.fetch.call_count == 2

    @pytest.mark.asyncio
    async def test_raises_on_unrelated_error(self):
        """When an unrelated DB error occurs, it propagates."""
        mock_pool = AsyncMock()
        mock_pool.fetch = AsyncMock(
            side_effect=Exception("connection refused")
        )

        with (
            patch("temporalio.activity.logger"),
            pytest.raises(Exception, match="connection refused"),
        ):
            await _fetch_runners(mock_pool, "dept-z")


class TestWriteAuditEvent:
    """Tests for the _write_audit_event helper."""

    @pytest.mark.asyncio
    async def test_writes_audit_event_successfully(self):
        """Audit event is POSTed to admin-dashboard API."""
        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch("temporalio.activity.logger"),
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock()
            mock_client_cls.return_value = mock_client

            await _write_audit_event(
                action="ssh_runner_selected",
                dept_id="dept-test",
                metadata={"runner_id": "r1", "selection_reason": "only_one"},
            )

            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert "/api/v1/audit/events" in call_args[0][0]
            body = call_args[1]["json"]
            assert body["action"] == "ssh_runner_selected"
            assert body["dept_id"] == "dept-test"
            assert body["metadata"]["runner_id"] == "r1"

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_propagate(self):
        """Audit write failure is logged but does not raise."""
        with (
            patch("httpx.AsyncClient") as mock_client_cls,
            patch("temporalio.activity.logger"),
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(
                side_effect=Exception("network error")
            )
            mock_client_cls.return_value = mock_client

            # Should not raise
            await _write_audit_event(
                action="ssh_runner_selected",
                dept_id="dept-test",
                metadata={"runner_id": "r1"},
            )
