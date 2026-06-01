"""Runner resolver activity — least-busy algorithm for multi-SSH runner pool.

Provides :func:`resolve_runner`, a Temporal activity that selects the
least-busy SSH runner assigned to a given department. The selection
algorithm:

1. Fetches all active runners assigned to the department from
   ``infrastructure.dept_ssh_assignments`` joined with
   ``infrastructure.ssh_runners``.
2. For each runner, counts the number of currently running workflows
   (via ``temporal.workflow_executions`` tracking table).
3. Selects the runner with the minimum active workflow count.
4. When counts are equal, uses the ``priority`` column as tiebreaker
   (lower priority value = higher precedence).
5. If no active runner is found, raises :class:`RunnerResolutionError`
   and writes an audit event ``no_runner_assigned_to_dept``.
6. On successful selection, writes an audit event ``ssh_runner_selected``
   with ``dept_id``, ``runner_id``, and ``selection_reason``.

Spec: platform-quick-fixes — Task 7.3
Requirements: 4.5, 4.6, 4.7, 4.16
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from temporalio import activity

__all__ = [
    "RunnerResolution",
    "RunnerResolutionError",
    "resolve_runner",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunnerResolution:
    """Result of the runner resolver activity.

    Contains the connection details for the selected SSH runner.

    Attributes
    ----------
    runner_id : str
        Unique identifier of the selected runner.
    host : str
        SSH host address.
    port : int
        SSH port number.
    username : str
        SSH username for authentication.
    base_path : str
        Remote workspace root for task folders.
    vault_path : str
        Vault path for the runner's SSH key material.
    """

    runner_id: str
    host: str
    port: int
    username: str
    base_path: str
    vault_path: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for Temporal payload."""
        return {
            "runner_id": self.runner_id,
            "host": self.host,
            "port": self.port,
            "username": self.username,
            "base_path": self.base_path,
            "vault_path": self.vault_path,
        }


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class RunnerResolutionError(RuntimeError):
    """Raised when no active runner can be resolved for a department.

    Attributes
    ----------
    dept_id : str
        The department that has no available runner.
    audit_event : str
        The audit event name that was (or should be) written.
    """

    def __init__(
        self,
        message: str,
        *,
        audit_event: str = "no_runner_assigned_to_dept",
    ) -> None:
        self.audit_event = audit_event
        super().__init__(message)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _postgres_dsn() -> str:
    """Read PostgreSQL DSN from environment."""
    return os.environ.get(
        "POSTGRES_DSN",
        "postgresql://ai:ai_dev_only@postgres:5432/ai",
    )


def _admin_dashboard_api_url() -> str:
    """Read Admin Dashboard API URL from environment."""
    return os.environ.get(
        "ADMIN_DASHBOARD_API_URL",
        "http://admin-dashboard-api:8080",
    )


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


async def _write_audit_event(
    *,
    action: str,
    dept_id: str,
    metadata: dict[str, Any],
) -> None:
    """Best-effort audit event write to the admin-dashboard API.

    Failure to write is logged but does not propagate — the runner
    resolution result is more important than the audit side-effect.
    """
    url = f"{_admin_dashboard_api_url()}/api/v1/audit/events"
    body = {
        "action": action,
        "actor_role": "system",
        "actor_id": "execution-runner-worker",
        "dept_id": dept_id,
        "resource": f"runner_resolver:{dept_id}",
        "result": "ok" if action == "ssh_runner_selected" else "error",
        "metadata": metadata,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=body)
    except Exception as exc:  # noqa: BLE001
        activity.logger.warning(
            "audit write failed (action=%s dept_id=%s): %s",
            action,
            dept_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Database query
# ---------------------------------------------------------------------------

#: SQL query for the least-busy runner selection algorithm.
#:
#: Joins ssh_runners with dept_ssh_assignments to find runners assigned
#: to the given department that are in 'active' status. Left-joins with
#: temporal.workflow_executions to count running workflows per runner.
#: Orders by active_count ASC (least busy first), then priority ASC
#: (lower value = higher precedence as tiebreaker).
_LEAST_BUSY_QUERY = """\
SELECT r.runner_id, r.host, r.port, r.username, r.base_path, r.vault_path,
       COUNT(w.id) FILTER (WHERE w.status = 'running') AS active_count
FROM infrastructure.ssh_runners r
JOIN infrastructure.dept_ssh_assignments a ON a.runner_id = r.runner_id
LEFT JOIN temporal.workflow_executions w ON w.runner_id = r.runner_id
WHERE a.dept_id = $1 AND r.status = 'active'
GROUP BY r.runner_id, r.host, r.port, r.username, r.base_path, r.vault_path
ORDER BY active_count ASC, a.priority ASC
"""

#: Fallback query when temporal.workflow_executions table does not exist.
#: Selects runners by priority only (no workflow count available).
_FALLBACK_QUERY = """\
SELECT r.runner_id, r.host, r.port, r.username, r.base_path, r.vault_path, 0 AS active_count
FROM infrastructure.ssh_runners r
JOIN infrastructure.dept_ssh_assignments a ON a.runner_id = r.runner_id
WHERE a.dept_id = $1 AND r.status = 'active'
ORDER BY a.priority ASC
"""


# ---------------------------------------------------------------------------
# Temporal activity
# ---------------------------------------------------------------------------


@activity.defn(name="resolve_runner")
async def resolve_runner(dept_id: str) -> dict[str, Any]:
    """Select the least-busy SSH runner assigned to a department.

    Implements the least-busy algorithm:
    1. Fetch active runners assigned to the department.
    2. Count active workflows per runner (if tracking table exists).
    3. Select the runner with minimum active workflows; tiebreak by priority.
    4. If no active runner exists, raise RunnerResolutionError.

    Parameters
    ----------
    dept_id : str
        The department identifier to resolve a runner for.

    Returns
    -------
    dict[str, Any]
        Serialized :class:`RunnerResolution` (runner_id, host, port,
        username, vault_path).

    Raises
    ------
    RunnerResolutionError
        When no active runner is assigned to the department.
    """
    import asyncpg  # type: ignore[import-not-found]

    activity.logger.info(
        "resolve_runner: selecting least-busy runner for dept_id=%s", dept_id
    )

    pool = await asyncpg.create_pool(
        dsn=_postgres_dsn(), min_size=1, max_size=2
    )

    try:
        runners = await _fetch_runners(pool, dept_id)
    finally:
        await pool.close()

    if not runners:
        # No active runner available — write audit and raise
        activity.logger.warning(
            "resolve_runner: no active runner assigned to dept %s", dept_id
        )
        await _write_audit_event(
            action="no_runner_assigned_to_dept",
            dept_id=dept_id,
            metadata={"reason": "no_active_runner_assigned"},
        )
        raise RunnerResolutionError(
            f"No active runner assigned to dept {dept_id}",
            audit_event="no_runner_assigned_to_dept",
        )

    # Select the first runner (already ordered by least-busy + priority)
    selected = runners[0]
    total_candidates = len(runners)

    resolution = RunnerResolution(
        runner_id=selected["runner_id"],
        host=selected["host"],
        port=selected["port"],
        username=selected["username"],
        base_path=selected.get("base_path") or "/var/ai-runner",
        vault_path=selected["vault_path"],
    )

    # Determine selection reason
    selection_reason = "least_busy" if total_candidates > 1 else "only_one"

    activity.logger.info(
        "resolve_runner: selected runner_id=%s for dept_id=%s "
        "(reason=%s, candidates=%d, active_count=%d)",
        resolution.runner_id,
        dept_id,
        selection_reason,
        total_candidates,
        selected["active_count"],
    )

    # Write audit event for runner selection (R4.16)
    await _write_audit_event(
        action="ssh_runner_selected",
        dept_id=dept_id,
        metadata={
            "dept_id": dept_id,
            "runner_id": resolution.runner_id,
            "selection_reason": selection_reason,
        },
    )

    return resolution.to_dict()


async def _fetch_runners(pool, dept_id: str) -> list[dict[str, Any]]:
    """Fetch runners using the least-busy query with fallback.

    Tries the full query with workflow_executions join first. If the
    temporal.workflow_executions table doesn't exist yet, falls back
    to a priority-only query.

    Parameters
    ----------
    pool : asyncpg.Pool
        Active database connection pool.
    dept_id : str
        Department identifier.

    Returns
    -------
    list[dict[str, Any]]
        List of runner records ordered by least-busy + priority.
    """
    try:
        rows = await pool.fetch(_LEAST_BUSY_QUERY, dept_id)
        return [dict(row) for row in rows]
    except Exception as exc:
        # If temporal.workflow_executions doesn't exist, fall back
        if "workflow_executions" in str(exc).lower() or "does not exist" in str(exc).lower():
            activity.logger.info(
                "resolve_runner: temporal.workflow_executions table not available, "
                "falling back to priority-only selection: %s",
                exc,
            )
            rows = await pool.fetch(_FALLBACK_QUERY, dept_id)
            return [dict(row) for row in rows]
        raise
