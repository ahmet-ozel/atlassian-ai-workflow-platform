"""Unit tests for the disk_quota activity module.

Tests cover:
- Quota skip when quota_mb is None
- Quota exceeded rejection
- 80% warning threshold detection
- Warning deduplication
- Cleanup candidates listing
- SSH command failure handling
- DiskQuotaInput/DiskQuotaResult dataclass behavior
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.activities.disk_quota import (
    CLEANUP_AGE_HOURS,
    DISK_CHECK_TIMEOUT_SECONDS,
    WARNING_DEDUP_MINUTES,
    WARNING_THRESHOLD,
    DiskQuotaError,
    DiskQuotaInput,
    DiskQuotaResult,
    check_disk_quota,
    _get_disk_usage_mb,
    _get_cleanup_candidates,
    _should_send_warning,
)


# ---------------------------------------------------------------------------
# DiskQuotaInput tests
# ---------------------------------------------------------------------------


class TestDiskQuotaInput:
    """Tests for DiskQuotaInput dataclass."""

    def test_basic_construction(self) -> None:
        inp = DiskQuotaInput(
            dept_id="payment",
            workspace_base="/var/ai-runner/workspaces/payment",
            quota_mb=10240.0,
        )
        assert inp.dept_id == "payment"
        assert inp.workspace_base == "/var/ai-runner/workspaces/payment"
        assert inp.quota_mb == 10240.0

    def test_none_quota(self) -> None:
        inp = DiskQuotaInput(
            dept_id="hr",
            workspace_base="/var/ai-runner/workspaces/hr",
            quota_mb=None,
        )
        assert inp.quota_mb is None

    def test_frozen(self) -> None:
        inp = DiskQuotaInput(
            dept_id="payment",
            workspace_base="/var/ai-runner/workspaces/payment",
            quota_mb=10240.0,
        )
        with pytest.raises(AttributeError):
            inp.dept_id = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DiskQuotaResult tests
# ---------------------------------------------------------------------------


class TestDiskQuotaResult:
    """Tests for DiskQuotaResult dataclass."""

    def test_allowed_result(self) -> None:
        result = DiskQuotaResult(
            allowed=True,
            usage_mb=5000.0,
            quota_mb=10240.0,
        )
        assert result.allowed is True
        assert result.usage_mb == 5000.0
        assert result.quota_mb == 10240.0
        assert result.error is None
        assert result.warning_sent is False
        assert result.cleanup_candidates == []

    def test_rejected_result(self) -> None:
        result = DiskQuotaResult(
            allowed=False,
            usage_mb=11000.0,
            quota_mb=10240.0,
            error="disk_quota_exceeded",
        )
        assert result.allowed is False
        assert result.error == "disk_quota_exceeded"

    def test_with_cleanup_candidates(self) -> None:
        result = DiskQuotaResult(
            allowed=True,
            usage_mb=8500.0,
            quota_mb=10240.0,
            warning_sent=True,
            cleanup_candidates=["workspace-old-1", "workspace-old-2"],
        )
        assert result.warning_sent is True
        assert len(result.cleanup_candidates) == 2


# ---------------------------------------------------------------------------
# DiskQuotaError tests
# ---------------------------------------------------------------------------


class TestDiskQuotaError:
    """Tests for DiskQuotaError."""

    def test_error_attributes(self) -> None:
        err = DiskQuotaError(dept_id="payment", cause="SSH timeout")
        assert err.dept_id == "payment"
        assert err.cause == "SSH timeout"
        assert "payment" in str(err)
        assert "SSH timeout" in str(err)


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestConstants:
    """Tests for module constants."""

    def test_disk_check_timeout(self) -> None:
        assert DISK_CHECK_TIMEOUT_SECONDS == 30.0

    def test_warning_threshold(self) -> None:
        assert WARNING_THRESHOLD == 0.80

    def test_warning_dedup_minutes(self) -> None:
        assert WARNING_DEDUP_MINUTES == 60

    def test_cleanup_age_hours(self) -> None:
        assert CLEANUP_AGE_HOURS == 72


# ---------------------------------------------------------------------------
# check_disk_quota activity tests
# ---------------------------------------------------------------------------


class TestCheckDiskQuota:
    """Tests for the check_disk_quota Temporal activity."""

    @pytest.mark.asyncio
    async def test_skip_when_quota_is_none(self) -> None:
        """Skip check when quota_mb is None."""
        with patch("src.activities.disk_quota.activity"):
            inp = DiskQuotaInput(
                dept_id="payment",
                workspace_base="/var/ai-runner/workspaces/payment",
                quota_mb=None,
            )
            result = await check_disk_quota(inp)

            assert result.allowed is True
            assert result.usage_mb == 0.0
            assert result.quota_mb is None
            assert result.error is None
            assert result.warning_sent is False
            assert result.cleanup_candidates == []

    @pytest.mark.asyncio
    async def test_quota_exceeded_rejects(self) -> None:
        """Reject when usage exceeds quota."""
        with (
            patch("src.activities.disk_quota.activity"),
            patch(
                "src.activities.disk_quota._get_disk_usage_mb",
                new_callable=AsyncMock,
                return_value=11000.0,
            ),
        ):
            inp = DiskQuotaInput(
                dept_id="payment",
                workspace_base="/var/ai-runner/workspaces/payment",
                quota_mb=10240.0,
            )
            result = await check_disk_quota(inp)

            assert result.allowed is False
            assert result.usage_mb == 11000.0
            assert result.quota_mb == 10240.0
            assert result.error == "disk_quota_exceeded"

    @pytest.mark.asyncio
    async def test_below_threshold_allows(self) -> None:
        """Usage below 80% threshold: allow without warning."""
        with (
            patch("src.activities.disk_quota.activity"),
            patch(
                "src.activities.disk_quota._get_disk_usage_mb",
                new_callable=AsyncMock,
                return_value=5000.0,
            ),
        ):
            inp = DiskQuotaInput(
                dept_id="payment",
                workspace_base="/var/ai-runner/workspaces/payment",
                quota_mb=10240.0,
            )
            result = await check_disk_quota(inp)

            assert result.allowed is True
            assert result.usage_mb == 5000.0
            assert result.error is None
            assert result.warning_sent is False
            assert result.cleanup_candidates == []

    @pytest.mark.asyncio
    async def test_at_80_percent_sends_warning(self) -> None:
        """Send warning at 80% threshold."""
        # 80% of 10240 = 8192
        with (
            patch("src.activities.disk_quota.activity"),
            patch(
                "src.activities.disk_quota._get_disk_usage_mb",
                new_callable=AsyncMock,
                return_value=8200.0,
            ),
            patch(
                "src.activities.disk_quota._get_cleanup_candidates",
                new_callable=AsyncMock,
                return_value=["old-workspace-1"],
            ),
            patch(
                "src.activities.disk_quota._should_send_warning",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "src.activities.disk_quota._send_warning_to_dashboard",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_send_warning,
        ):
            inp = DiskQuotaInput(
                dept_id="payment",
                workspace_base="/var/ai-runner/workspaces/payment",
                quota_mb=10240.0,
            )
            result = await check_disk_quota(inp)

            assert result.allowed is True
            assert result.warning_sent is True
            assert result.cleanup_candidates == ["old-workspace-1"]
            mock_send_warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_warning_deduplicated(self) -> None:
        """No duplicate warning within 60 minutes."""
        # 80% of 10240 = 8192
        with (
            patch("src.activities.disk_quota.activity"),
            patch(
                "src.activities.disk_quota._get_disk_usage_mb",
                new_callable=AsyncMock,
                return_value=8500.0,
            ),
            patch(
                "src.activities.disk_quota._get_cleanup_candidates",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "src.activities.disk_quota._should_send_warning",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch(
                "src.activities.disk_quota._send_warning_to_dashboard",
                new_callable=AsyncMock,
            ) as mock_send_warning,
        ):
            inp = DiskQuotaInput(
                dept_id="payment",
                workspace_base="/var/ai-runner/workspaces/payment",
                quota_mb=10240.0,
            )
            result = await check_disk_quota(inp)

            assert result.allowed is True
            assert result.warning_sent is False
            mock_send_warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_ssh_failure_allows_with_error(self) -> None:
        """On SSH failure, allow creation but report error."""
        with (
            patch("src.activities.disk_quota.activity"),
            patch(
                "src.activities.disk_quota._get_disk_usage_mb",
                new_callable=AsyncMock,
                side_effect=DiskQuotaError(
                    dept_id="payment", cause="SSH connection refused"
                ),
            ),
        ):
            inp = DiskQuotaInput(
                dept_id="payment",
                workspace_base="/var/ai-runner/workspaces/payment",
                quota_mb=10240.0,
            )
            result = await check_disk_quota(inp)

            assert result.allowed is True
            assert result.usage_mb == 0.0
            assert "disk_check_failed" in result.error

    @pytest.mark.asyncio
    async def test_exactly_at_quota_rejects(self) -> None:
        """Usage exactly at quota boundary should still be allowed (not >)."""
        with (
            patch("src.activities.disk_quota.activity"),
            patch(
                "src.activities.disk_quota._get_disk_usage_mb",
                new_callable=AsyncMock,
                return_value=10240.0,
            ),
            patch(
                "src.activities.disk_quota._get_cleanup_candidates",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "src.activities.disk_quota._should_send_warning",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch(
                "src.activities.disk_quota._send_warning_to_dashboard",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            inp = DiskQuotaInput(
                dept_id="payment",
                workspace_base="/var/ai-runner/workspaces/payment",
                quota_mb=10240.0,
            )
            result = await check_disk_quota(inp)

            # Exactly at quota: usage (10240) is NOT > quota (10240)
            # But it IS >= 80% threshold (8192), so warning is sent
            assert result.allowed is True
            assert result.warning_sent is True


# ---------------------------------------------------------------------------
# _get_disk_usage_mb tests
# ---------------------------------------------------------------------------


class TestGetDiskUsageMb:
    """Tests for the SSH disk usage measurement helper."""

    @pytest.mark.asyncio
    async def test_parses_du_output(self) -> None:
        """Correctly parses du -sm output."""
        with patch(
            "src.activities.disk_quota._execute_ssh_command",
            new_callable=AsyncMock,
            return_value={"stdout": "5120\n", "stderr": "", "exit_code": 0},
        ):
            result = await _get_disk_usage_mb(
                "/var/ai-runner/workspaces/payment", "payment"
            )
            assert result == 5120.0

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises(self) -> None:
        """Non-zero exit code raises DiskQuotaError."""
        with patch(
            "src.activities.disk_quota._execute_ssh_command",
            new_callable=AsyncMock,
            return_value={
                "stdout": "",
                "stderr": "Permission denied",
                "exit_code": 1,
            },
        ):
            with pytest.raises(DiskQuotaError) as exc_info:
                await _get_disk_usage_mb(
                    "/var/ai-runner/workspaces/payment", "payment"
                )
            assert "du command failed" in exc_info.value.cause

    @pytest.mark.asyncio
    async def test_unparseable_output_raises(self) -> None:
        """Non-numeric output raises DiskQuotaError."""
        with patch(
            "src.activities.disk_quota._execute_ssh_command",
            new_callable=AsyncMock,
            return_value={
                "stdout": "not_a_number\n",
                "stderr": "",
                "exit_code": 0,
            },
        ):
            with pytest.raises(DiskQuotaError) as exc_info:
                await _get_disk_usage_mb(
                    "/var/ai-runner/workspaces/payment", "payment"
                )
            assert "unable to parse" in exc_info.value.cause

    @pytest.mark.asyncio
    async def test_zero_for_nonexistent_path(self) -> None:
        """Returns 0 when path doesn't exist (handled by shell script)."""
        with patch(
            "src.activities.disk_quota._execute_ssh_command",
            new_callable=AsyncMock,
            return_value={"stdout": "0\n", "stderr": "", "exit_code": 0},
        ):
            result = await _get_disk_usage_mb(
                "/var/ai-runner/workspaces/new_dept", "new_dept"
            )
            assert result == 0.0


# ---------------------------------------------------------------------------
# _get_cleanup_candidates tests
# ---------------------------------------------------------------------------


class TestGetCleanupCandidates:
    """Tests for the cleanup candidates listing helper."""

    @pytest.mark.asyncio
    async def test_returns_old_workspaces(self) -> None:
        """Lists workspace directories older than 72 hours."""
        with patch(
            "src.activities.disk_quota._execute_ssh_command",
            new_callable=AsyncMock,
            return_value={
                "stdout": "workspace-abc\nworkspace-def\nworkspace-ghi\n",
                "stderr": "",
                "exit_code": 0,
            },
        ):
            result = await _get_cleanup_candidates(
                "/var/ai-runner/workspaces/payment", "payment"
            )
            assert result == ["workspace-abc", "workspace-def", "workspace-ghi"]

    @pytest.mark.asyncio
    async def test_empty_when_no_old_workspaces(self) -> None:
        """Returns empty list when no old workspaces exist."""
        with patch(
            "src.activities.disk_quota._execute_ssh_command",
            new_callable=AsyncMock,
            return_value={"stdout": "", "stderr": "", "exit_code": 0},
        ):
            result = await _get_cleanup_candidates(
                "/var/ai-runner/workspaces/payment", "payment"
            )
            assert result == []

    @pytest.mark.asyncio
    async def test_empty_on_ssh_failure(self) -> None:
        """Returns empty list on SSH failure (best-effort)."""
        with patch(
            "src.activities.disk_quota._execute_ssh_command",
            new_callable=AsyncMock,
            side_effect=DiskQuotaError(
                dept_id="payment", cause="SSH timeout"
            ),
        ), patch("src.activities.disk_quota.activity"):
            result = await _get_cleanup_candidates(
                "/var/ai-runner/workspaces/payment", "payment"
            )
            assert result == []
