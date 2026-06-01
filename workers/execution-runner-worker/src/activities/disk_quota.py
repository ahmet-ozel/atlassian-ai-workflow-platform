"""Disk Quota Enforcement activity module for the execution-runner-worker.

Provides :func:`check_disk_quota`, a Temporal activity that checks disk
usage on the SSH runner for a department's workspace base path and enforces
quota limits.

Workflow:
1. If ``ssh_workspace_quota_mb`` is None/null, skip quota check entirely
2. SSH into the runner and compute disk usage via ``du -sm``
3. If usage exceeds quota: reject workspace creation with "disk_quota_exceeded"
4. If usage reaches 80% threshold: send warning to Admin Dashboard (deduplicated)
5. If above 80%: list workspaces older than 72 hours as cleanup candidates

Warning Deduplication:
    No duplicate warning is sent for the same department within 60 minutes.
    Deduplication is tracked via the ``disk_quota_warnings`` table.

Requirements: 16.1, 16.2, 16.3, 16.4, 16.5
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from temporalio import activity

__all__ = [
    "DiskQuotaInput",
    "DiskQuotaResult",
    "DiskQuotaError",
    "check_disk_quota",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Timeout for the SSH disk usage check command (Requirement 16.1: 30s).
DISK_CHECK_TIMEOUT_SECONDS: float = 30.0

#: Warning threshold as a fraction of quota (Requirement 16.3: 80%).
WARNING_THRESHOLD: float = 0.80

#: Deduplication window for warnings (Requirement 16.3: 60 minutes).
WARNING_DEDUP_MINUTES: int = 60

#: Age threshold for cleanup candidates (Requirement 16.4: 72 hours).
CLEANUP_AGE_HOURS: int = 72


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class DiskQuotaError(RuntimeError):
    """Raised when disk quota check encounters an unrecoverable error.

    Attributes
    ----------
    dept_id : str
        The department whose quota was being checked.
    cause : str
        Human-readable description of what went wrong.
    """

    def __init__(self, dept_id: str, cause: str) -> None:
        self.dept_id = dept_id
        self.cause = cause
        super().__init__(
            f"disk quota check failed for dept={dept_id}: {cause}"
        )


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiskQuotaInput:
    """Input for the check_disk_quota activity.

    Attributes
    ----------
    dept_id : str
        Department identifier for quota lookup and warning dedup.
    workspace_base : str
        Base path on the SSH runner where department workspaces reside.
    quota_mb : float | None
        Department disk quota in MB from ``ssh_workspace_quota_mb``.
        If None, quota check is skipped entirely (Requirement 16.5).
    """

    dept_id: str
    workspace_base: str
    quota_mb: float | None


@dataclass(frozen=True)
class DiskQuotaResult:
    """Result of the check_disk_quota activity.

    Attributes
    ----------
    allowed : bool
        Whether workspace creation is allowed.
    usage_mb : float
        Current disk usage in MB for the department workspace base path.
    quota_mb : float | None
        The configured quota in MB, or None if no quota is configured.
    error : str | None
        Error message if quota is exceeded or check failed; None otherwise.
    warning_sent : bool
        Whether a warning was sent to Admin Dashboard (80% threshold).
    cleanup_candidates : list[str]
        List of workspace directory names older than 72 hours when above 80%.
    """

    allowed: bool
    usage_mb: float
    quota_mb: float | None
    error: str | None = None
    warning_sent: bool = False
    cleanup_candidates: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _admin_dashboard_api_url() -> str:
    """Read Admin Dashboard API URL from environment."""
    return os.environ.get(
        "ADMIN_DASHBOARD_API_URL", "http://admin-dashboard-api:8000"
    ).rstrip("/")


def _db_connection_string() -> str:
    """Read PostgreSQL connection string from environment."""
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@postgres:5432/automation",
    )


# ---------------------------------------------------------------------------
# SSH execution helper
# ---------------------------------------------------------------------------


async def _execute_ssh_command(
    command: str,
    timeout_seconds: float,
    dept_id: str,
) -> dict[str, Any]:
    """Execute a command on the SSH runner.

    Uses the existing SSH infrastructure (vault credential fetch + paramiko).

    Parameters
    ----------
    command:
        Shell command to execute on the SSH runner.
    timeout_seconds:
        Maximum execution time in seconds.
    dept_id:
        Department ID for error context.

    Returns
    -------
    dict
        Result with stdout, stderr, exit_code keys.

    Raises
    ------
    DiskQuotaError
        If SSH execution fails.
    """
    from src.activities.vault import vault_fetch_ssh_credentials
    from src.activities.ssh import _ssh_execute_command, SSHActivityError

    try:
        cred = await vault_fetch_ssh_credentials(f"disk_quota_{dept_id}")
    except Exception as exc:
        raise DiskQuotaError(
            dept_id=dept_id,
            cause=f"SSH credential fetch failed: {exc}",
        ) from exc

    try:
        result = await asyncio.to_thread(
            _ssh_execute_command,
            cred.host,
            cred.port,
            cred.user,
            cred.private_key,
            command,
            "",  # no workspace path needed
            int(timeout_seconds),
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
        }
    except SSHActivityError as exc:
        raise DiskQuotaError(
            dept_id=dept_id,
            cause=f"SSH execution failed: {exc.cause}",
        ) from exc


# ---------------------------------------------------------------------------
# Disk usage measurement
# ---------------------------------------------------------------------------


async def _get_disk_usage_mb(
    workspace_base: str,
    dept_id: str,
) -> float:
    """Get disk usage in MB for the workspace base path via SSH.

    Uses ``du -sm`` to compute total disk usage in megabytes.
    Requirement 16.1: 30s timeout.

    Parameters
    ----------
    workspace_base:
        Base path on the SSH runner.
    dept_id:
        Department ID for error context.

    Returns
    -------
    float
        Disk usage in megabytes.

    Raises
    ------
    DiskQuotaError
        If the command fails or output cannot be parsed.
    """
    # du -sm gives total size in MB; use 2>/dev/null to suppress permission errors
    # on individual files, and default to 0 if path doesn't exist yet
    command = (
        f"if [ -d {workspace_base} ]; then "
        f"du -sm {workspace_base} 2>/dev/null | awk '{{print $1}}'; "
        f"else echo 0; fi"
    )

    result = await _execute_ssh_command(
        command=command,
        timeout_seconds=DISK_CHECK_TIMEOUT_SECONDS,
        dept_id=dept_id,
    )

    if result["exit_code"] != 0:
        raise DiskQuotaError(
            dept_id=dept_id,
            cause=(
                f"du command failed (exit_code={result['exit_code']}): "
                f"{result['stderr'][:200]}"
            ),
        )

    stdout = result["stdout"].strip()
    try:
        usage_mb = float(stdout)
    except (ValueError, TypeError) as exc:
        raise DiskQuotaError(
            dept_id=dept_id,
            cause=f"unable to parse disk usage from output: {stdout!r}",
        ) from exc

    return usage_mb


# ---------------------------------------------------------------------------
# Cleanup candidates (workspaces older than 72 hours)
# ---------------------------------------------------------------------------


async def _get_cleanup_candidates(
    workspace_base: str,
    dept_id: str,
) -> list[str]:
    """List workspace directories older than 72 hours.

    Requirement 16.4: When above 80%, list workspaces older than 72 hours
    for cleanup suggestions.

    Parameters
    ----------
    workspace_base:
        Base path on the SSH runner.
    dept_id:
        Department ID for error context.

    Returns
    -------
    list[str]
        List of workspace directory names older than 72 hours.
    """
    # find directories directly under workspace_base that are older than 72 hours
    command = (
        f"find {workspace_base} -maxdepth 1 -mindepth 1 -type d "
        f"-mmin +{CLEANUP_AGE_HOURS * 60} "
        f"-printf '%f\\n' 2>/dev/null || true"
    )

    try:
        result = await _execute_ssh_command(
            command=command,
            timeout_seconds=DISK_CHECK_TIMEOUT_SECONDS,
            dept_id=dept_id,
        )
    except DiskQuotaError:
        # Best-effort: if we can't list candidates, return empty
        activity.logger.warning(
            "Failed to list cleanup candidates for dept=%s", dept_id
        )
        return []

    if result["exit_code"] != 0:
        return []

    stdout = result["stdout"].strip()
    if not stdout:
        return []

    candidates = [
        line.strip()
        for line in stdout.splitlines()
        if line.strip()
    ]
    return candidates


# ---------------------------------------------------------------------------
# Warning deduplication
# ---------------------------------------------------------------------------


async def _should_send_warning(dept_id: str) -> bool:
    """Check if a warning should be sent (deduplication within 60 minutes).

    Queries the ``disk_quota_warnings`` table to see if a warning was
    already sent for this department within the last 60 minutes.

    Parameters
    ----------
    dept_id:
        Department identifier.

    Returns
    -------
    bool
        True if a warning should be sent (no recent warning exists).
    """
    try:
        # Use Admin Dashboard API to check dedup status
        url = f"{_admin_dashboard_api_url()}/api/v1/disk-quota/warnings/check"
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                url,
                params={
                    "dept_id": dept_id,
                    "dedup_minutes": WARNING_DEDUP_MINUTES,
                },
            )
            if response.status_code == 200:
                data = response.json()
                return data.get("should_warn", True)
            # If API is unavailable, default to sending warning
            return True
    except Exception:
        # If we can't check dedup, default to sending warning
        # (better to warn twice than miss a warning)
        activity.logger.warning(
            "Failed to check warning dedup for dept=%s, defaulting to send",
            dept_id,
        )
        return True


async def _send_warning_to_dashboard(
    dept_id: str,
    usage_mb: float,
    quota_mb: float,
    cleanup_candidates: list[str],
) -> bool:
    """Send a disk quota warning to the Admin Dashboard.

    Also records the warning in the deduplication table.

    Parameters
    ----------
    dept_id:
        Department identifier.
    usage_mb:
        Current disk usage in MB.
    quota_mb:
        Configured quota in MB.
    cleanup_candidates:
        List of workspace names eligible for cleanup.

    Returns
    -------
    bool
        True if warning was sent successfully.
    """
    try:
        url = f"{_admin_dashboard_api_url()}/api/v1/disk-quota/warnings"
        payload = {
            "dept_id": dept_id,
            "usage_mb": usage_mb,
            "quota_mb": quota_mb,
            "usage_percent": round((usage_mb / quota_mb) * 100, 1),
            "cleanup_candidates": cleanup_candidates,
            "warned_at": datetime.now(timezone.utc).isoformat(),
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            if 200 <= response.status_code < 300:
                activity.logger.info(
                    "Disk quota warning sent for dept=%s "
                    "(usage=%.1f MB / %.1f MB = %.1f%%)",
                    dept_id,
                    usage_mb,
                    quota_mb,
                    (usage_mb / quota_mb) * 100,
                )
                return True
            else:
                activity.logger.warning(
                    "Failed to send disk quota warning for dept=%s: "
                    "HTTP %d",
                    dept_id,
                    response.status_code,
                )
                return False
    except Exception as exc:
        activity.logger.warning(
            "Failed to send disk quota warning for dept=%s: %s",
            dept_id,
            str(exc),
        )
        return False


# ---------------------------------------------------------------------------
# Temporal activity
# ---------------------------------------------------------------------------


@activity.defn(name="check_disk_quota")
async def check_disk_quota(input: DiskQuotaInput) -> DiskQuotaResult:
    """Check disk usage for a department workspace and enforce quota limits.

    Implements the full disk quota enforcement flow:
    1. If quota_mb is None: skip check, allow workspace creation
    2. SSH to runner and compute disk usage via ``du -sm`` (30s timeout)
    3. If usage > quota: reject with "disk_quota_exceeded"
    4. If usage >= 80% of quota: send deduplicated warning to Admin Dashboard
    5. If above 80%: list workspaces older than 72 hours as cleanup candidates

    Parameters
    ----------
    input:
        Disk quota check parameters including dept_id, workspace_base,
        and quota_mb.

    Returns
    -------
    DiskQuotaResult
        Result indicating whether workspace creation is allowed,
        current usage, and any warnings or cleanup suggestions.
    """
    activity.logger.info(
        "Checking disk quota for dept=%s workspace_base=%s quota_mb=%s",
        input.dept_id,
        input.workspace_base,
        input.quota_mb,
    )

    # Requirement 16.5: If ssh_workspace_quota_mb is null, skip check entirely
    if input.quota_mb is None:
        activity.logger.info(
            "Disk quota check skipped for dept=%s (quota_mb is null)",
            input.dept_id,
        )
        return DiskQuotaResult(
            allowed=True,
            usage_mb=0.0,
            quota_mb=None,
            error=None,
            warning_sent=False,
            cleanup_candidates=[],
        )

    # Step 1: Get current disk usage (Requirement 16.1: 30s timeout)
    try:
        usage_mb = await _get_disk_usage_mb(
            workspace_base=input.workspace_base,
            dept_id=input.dept_id,
        )
    except DiskQuotaError as exc:
        activity.logger.error(
            "Disk usage check failed for dept=%s: %s",
            input.dept_id,
            exc.cause,
        )
        # On failure to check, allow creation but report the error
        return DiskQuotaResult(
            allowed=True,
            usage_mb=0.0,
            quota_mb=input.quota_mb,
            error=f"disk_check_failed: {exc.cause}",
            warning_sent=False,
            cleanup_candidates=[],
        )

    activity.logger.info(
        "Disk usage for dept=%s: %.1f MB / %.1f MB (%.1f%%)",
        input.dept_id,
        usage_mb,
        input.quota_mb,
        (usage_mb / input.quota_mb) * 100 if input.quota_mb > 0 else 0,
    )

    # Step 2: Check if quota is exceeded (Requirement 16.2)
    if usage_mb > input.quota_mb:
        activity.logger.warning(
            "Disk quota exceeded for dept=%s: %.1f MB > %.1f MB",
            input.dept_id,
            usage_mb,
            input.quota_mb,
        )
        return DiskQuotaResult(
            allowed=False,
            usage_mb=usage_mb,
            quota_mb=input.quota_mb,
            error="disk_quota_exceeded",
            warning_sent=False,
            cleanup_candidates=[],
        )

    # Step 3: Check 80% warning threshold (Requirement 16.3)
    warning_sent = False
    cleanup_candidates: list[str] = []
    threshold_mb = input.quota_mb * WARNING_THRESHOLD

    if usage_mb >= threshold_mb:
        activity.logger.info(
            "Disk usage at/above 80%% threshold for dept=%s "
            "(%.1f MB >= %.1f MB threshold)",
            input.dept_id,
            usage_mb,
            threshold_mb,
        )

        # Step 4: List cleanup candidates (Requirement 16.4)
        cleanup_candidates = await _get_cleanup_candidates(
            workspace_base=input.workspace_base,
            dept_id=input.dept_id,
        )

        # Step 5: Send deduplicated warning (Requirement 16.3)
        should_warn = await _should_send_warning(input.dept_id)
        if should_warn:
            warning_sent = await _send_warning_to_dashboard(
                dept_id=input.dept_id,
                usage_mb=usage_mb,
                quota_mb=input.quota_mb,
                cleanup_candidates=cleanup_candidates,
            )
        else:
            activity.logger.info(
                "Disk quota warning suppressed for dept=%s "
                "(within %d-minute dedup window)",
                input.dept_id,
                WARNING_DEDUP_MINUTES,
            )

    return DiskQuotaResult(
        allowed=True,
        usage_mb=usage_mb,
        quota_mb=input.quota_mb,
        error=None,
        warning_sent=warning_sent,
        cleanup_candidates=cleanup_candidates,
    )
