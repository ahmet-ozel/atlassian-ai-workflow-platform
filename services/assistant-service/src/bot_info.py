"""Bot info endpoint for the Task Creator assignee card (R7.6).

Exposes ``GET /api/dept/{id}/bot-info`` which returns the department's
display name and a list of registered bots with their service type,
username, account_id, and probe status.

Data sources:
  - ``automation.departments`` — department display_name.
  - ``automation.department_bots`` — per-service bot registrations
    (service, username, account_id).
  - ``shared.capability_probes`` — latest probe result per
    (dept_id, service) pair (probe_status, probed_at).

The endpoint is consumed by the Streamlit Task Creator page to render
the "Bot Assignee Info Card" (Requirement 7.1–7.5).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

__all__ = ["BotInfoDeps", "router"]

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database protocol — duck-typed for testability
# ---------------------------------------------------------------------------


class DbPool(Protocol):
    """Minimal asyncpg-pool-like interface needed by the bot-info handler."""

    async def fetchrow(self, query: str, *args: Any) -> Any: ...

    async def fetch(self, query: str, *args: Any) -> list[Any]: ...


# ---------------------------------------------------------------------------
# Dependency container
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BotInfoDeps:
    """Collaborators injected via ``app.state.bot_info_deps``."""

    db: DbPool


def _deps(request: Request) -> BotInfoDeps:
    deps = getattr(request.app.state, "bot_info_deps", None)
    if deps is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="bot-info endpoint not wired (database pool unavailable)",
        )
    return deps


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(tags=["bot-info"])


@router.get("/api/dept/{dept_id}/bot-info")
async def get_bot_info(dept_id: str, request: Request) -> JSONResponse:
    """Return bot assignee information for a department.

    Response shape::

        {
            "display_name": "Payment Team",
            "bots": [
                {
                    "service": "jira",
                    "username": "payment-ai-bot",
                    "account_id": "5fc9e78d...",
                    "probe_status": "ok",
                    "probed_at": "2024-01-15T10:30:00Z"
                },
                ...
            ]
        }

    Returns 404 when the department does not exist.
    """

    deps = _deps(request)

    # 1. Fetch department display_name
    dept_row = await deps.db.fetchrow(
        """
        SELECT display_name
        FROM automation.departments
        WHERE id = $1
        """,
        dept_id,
    )

    if dept_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"department '{dept_id}' not found",
        )

    display_name: str = dept_row["display_name"]

    # 2. Fetch bots with probe status via LEFT JOIN
    bot_rows = await deps.db.fetch(
        """
        SELECT
            b.service,
            b.username,
            b.account_id,
            COALESCE(p.status, 'not_probed') AS probe_status,
            p.probed_at
        FROM automation.department_bots b
        LEFT JOIN shared.capability_probes p
            ON p.dept_id = b.department_id AND p.service = b.service
        WHERE b.department_id = $1
        ORDER BY b.service
        """,
        dept_id,
    )

    bots = []
    for row in bot_rows:
        probed_at_val = row["probed_at"]
        bots.append(
            {
                "service": row["service"],
                "username": row["username"],
                "account_id": row["account_id"],
                "probe_status": row["probe_status"],
                "probed_at": (
                    probed_at_val.isoformat() if isinstance(probed_at_val, datetime) else probed_at_val
                ),
            }
        )

    return JSONResponse(
        status_code=200,
        content={
            "display_name": display_name,
            "bots": bots,
        },
    )
