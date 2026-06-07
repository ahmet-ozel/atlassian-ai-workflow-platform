"""Capability gate - Phase 1 and Phase 2 helpers for webhook decision layer.

Phase 1 (webhook handler, pre-workflow):
    ``has_jira_credential(db, dept_id)`` - cheapest possible check: does the
    department have a ``department_bots`` row with ``service='jira'``?  If not,
    there is no point starting a Temporal workflow.

    ``resolve_dept_capabilities(db, dept_id)`` - builds the full capability
    frozenset for a department by combining:
      • services registered in ``department_bots`` (jira, bitbucket, confluence)
      • ``web_search`` flag from ``departments.web_search_enabled``
      • ``execution`` - present only when at least one active SSH runner is
        assigned to the department (via ``infrastructure.dept_ssh_assignments``
        joined with ``infrastructure.ssh_runners`` where status='active')

Phase 2 (inside workflow, post-LLM analysis):
    Re-exports ``missing_capabilities`` and ``required_capabilities`` from
    ``temporal_shared.capabilities`` so that callers in the automation-service
    can import from a single location.

Requirements: 2.11, 4.9, 5.2, 5.3
"""

from __future__ import annotations

import asyncpg

# Phase 2 re-exports from the shared library
from temporal_shared.capabilities import (
    WORKFLOW_TYPE_CAPABILITIES,
    missing_capabilities,
    required_capabilities,
)

__all__ = [
    "has_jira_credential",
    "resolve_dept_capabilities",
    # Phase 2 re-exports
    "missing_capabilities",
    "required_capabilities",
    "WORKFLOW_TYPE_CAPABILITIES",
]


async def has_jira_credential(db: asyncpg.Pool, dept_id: str) -> bool:
    """Phase 1 gate: does the department have a Jira bot credential?

    Performs a single existence check against ``automation.department_bots``
    for the given department with ``service = 'jira'``.  This is the cheapest
    pre-check in the webhook handler - if the department cannot even talk to
    Jira, there is no reason to start a workflow.

    Parameters
    ----------
    db:
        asyncpg connection pool connected to the automation database.
    dept_id:
        The department identifier (``departments.id``).

    Returns
    -------
    bool
        ``True`` if a row with ``service='jira'`` exists for the department;
        ``False`` otherwise.
    """
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT 1
            FROM automation.department_bots
            WHERE department_id = $1
              AND service = 'jira'
            """,
            dept_id,
        )
        return row is not None


async def resolve_dept_capabilities(
    db: asyncpg.Pool,
    dept_id: str,
) -> frozenset[str]:
    """Resolve the full capability set for a department.

    The capability set is the union of:
      1. All ``service`` values from ``automation.department_bots`` for the
         department (e.g. ``{'jira', 'bitbucket', 'confluence'}``).
      2. ``'web_search'`` - included when ``departments.web_search_enabled``
         is ``TRUE`` for the department.
      3. ``'execution'`` - included only when at least one active SSH runner
         is assigned to the department via ``infrastructure.dept_ssh_assignments``
         joined with ``infrastructure.ssh_runners`` (status='active').

    Parameters
    ----------
    db:
        asyncpg connection pool connected to the automation database.
    dept_id:
        The department identifier (``departments.id``).

    Returns
    -------
    frozenset[str]
        Immutable set of capability strings available to the department.
        May be empty if the department has no bots, web_search is disabled,
        and no active runner is assigned.
    """
    async with db.acquire() as conn:
        # Fetch registered bot services for the department
        service_rows = await conn.fetch(
            """
            SELECT service
            FROM automation.department_bots
            WHERE department_id = $1
            """,
            dept_id,
        )

        # Fetch web_search_enabled flag from departments table
        dept_row = await conn.fetchrow(
            """
            SELECT web_search_enabled
            FROM automation.departments
            WHERE id = $1
            """,
            dept_id,
        )

        # Check if at least one active runner is assigned to this department
        runner_count = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM infrastructure.dept_ssh_assignments a
            JOIN infrastructure.ssh_runners r ON r.runner_id = a.runner_id
            WHERE a.dept_id = $1 AND r.status = 'active'
            """,
            dept_id,
        )

    # Build capability set
    capabilities: set[str] = set()

    # Add bot services
    for row in service_rows:
        capabilities.add(row["service"])

    # Add web_search if enabled
    if dept_row is not None and dept_row["web_search_enabled"]:
        capabilities.add("web_search")

    # execution capability: only if at least one active runner is assigned
    if runner_count > 0:
        capabilities.add("execution")

    return frozenset(capabilities)
