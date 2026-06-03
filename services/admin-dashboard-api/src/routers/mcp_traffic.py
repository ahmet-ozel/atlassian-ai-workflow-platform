"""``McpTrafficRouter``.

Surfaces MCP request traffic statistics for the admin dashboard. The
router proxies the MCP server's Prometheus exposition endpoint
(``GET http://atlassian-mcp:8090/metrics``),
parses the ``mcp_requests_total{client_source, tool, status}``
counter, and returns a filterable JSON envelope grouped by
``client_source`` and ``tool``.

Endpoints
---------

* ``GET /api/v1/mcp/traffic`` — counters since the MCP server started.
  Optional query parameters:

  * ``client_source`` — narrow to a single caller (``automation-worker``,
    ``execution-runner-worker``, ``streamlit-ui``, ``unknown``, …).
  * ``tool`` — narrow to a single MCP tool / JSON-RPC method.
  * ``status`` — narrow to ``success`` / ``error``.

  Response body shape:

  .. code-block:: json

      {
        "totals": {
          "by_client_source": {"automation-worker": 1234, ...},
          "by_tool": {"jira_get_issue": 800, ...},
          "by_status": {"success": 1200, "error": 34},
          "total": 1234
        },
        "rows": [
          {
            "client_source": "automation-worker",
            "tool": "jira_get_issue",
            "status": "success",
            "count": 800
          },
          ...
        ],
        "fetched_at": "2025-01-01T12:00:00+00:00",
        "source": "atlassian-mcp"
      }

The router is admin-only (``Depends(require_admin)``). The MCP server
itself does not enforce auth on ``/metrics`` (Prometheus scrape jobs
expect open access) — gating happens here so unauthenticated users
cannot poll the admin dashboard for traffic stats.

A "last 24h" window is the documented UX framing.
The MCP server exposes cumulative counter samples since process
start, not a windowed query. To honour the window framing without a
PromQL aggregator, a future iteration can layer
``increase(mcp_requests_total[24h])`` on top by pointing the client
at a Prometheus aggregator URL — until that lands, the snapshot view
documented above is the surfaced behaviour.

503 / 502 semantics
-------------------

* When ``app.state.mcp_metrics_client`` is ``None`` (lifespan still
  building, MCP wiring not landed) the endpoint returns
  ``HTTP 503`` with ``reason="mcp_metrics_unavailable"``.
* When the underlying client raises :class:`McpMetricsError`
  (transport failure, non-2xx, parse error) the endpoint returns
  ``HTTP 502`` with ``error="mcp_metrics_fetch_failed"``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..auth.dependencies import require_admin
from ..clients.mcp_metrics_client import (
    McpMetricsClient,
    McpMetricsError,
    McpRequestCounter,
)

__all__ = ["router"]


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/v1/mcp", tags=["mcp-traffic"])


def _get_client(request: Request) -> McpMetricsClient:
    """Return the wired :class:`McpMetricsClient` instance.

    Raises:
        HTTPException(503): When the slot is ``None`` (MCP not
            reachable, lifespan still building the client).
    """

    client = getattr(request.app.state, "mcp_metrics_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "mcp_metrics_unavailable",
            },
        )
    return client


def _filter_counters(
    rows: Iterable[McpRequestCounter],
    *,
    client_source: str | None,
    tool: str | None,
    wf_status: str | None,
) -> list[McpRequestCounter]:
    """Apply optional filters to the parsed counter rows.

    Filters are applied case-sensitively against the label values
    emitted by the MCP middleware so a query for ``streamlit-ui``
    matches the canonical label value the middleware writes.
    """

    filtered: list[McpRequestCounter] = []
    for row in rows:
        if client_source is not None and row.client_source != client_source:
            continue
        if tool is not None and row.tool != tool:
            continue
        if wf_status is not None and row.status != wf_status:
            continue
        filtered.append(row)
    return filtered


def _aggregate(rows: Iterable[McpRequestCounter]) -> dict[str, object]:
    """Compute the totals envelope from a list of counter rows.

    Returns a dict shaped as documented in the module docstring.
    """

    by_client: defaultdict[str, int] = defaultdict(int)
    by_tool: defaultdict[str, int] = defaultdict(int)
    by_status: defaultdict[str, int] = defaultdict(int)
    total = 0
    for row in rows:
        amount = int(row.count)
        by_client[row.client_source] += amount
        by_tool[row.tool] += amount
        by_status[row.status] += amount
        total += amount

    # Sort each map by descending count then ascending key for a
    # stable, snapshot-friendly JSON shape.
    def _sorted(items: dict[str, int]) -> dict[str, int]:
        return dict(
            sorted(items.items(), key=lambda kv: (-kv[1], kv[0]))
        )

    return {
        "by_client_source": _sorted(dict(by_client)),
        "by_tool": _sorted(dict(by_tool)),
        "by_status": _sorted(dict(by_status)),
        "total": total,
    }


@router.get(
    "/traffic",
    summary="MCP traffic stats grouped by client_source / tool / status",
    dependencies=[Depends(require_admin)],
)
async def get_mcp_traffic(
    request: Request,
    client_source: str | None = Query(
        default=None,
        description=(
            "Filter by ``X-Client-Source`` label value "
            "(e.g. ``automation-worker``, ``streamlit-ui``)."
        ),
    ),
    tool: str | None = Query(
        default=None,
        description=(
            "Filter by MCP tool name or JSON-RPC method "
            "(e.g. ``jira_get_issue``, ``tools/list``)."
        ),
    ),
    wf_status: str | None = Query(
        default=None,
        alias="status",
        description=(
            "Filter by request outcome label "
            "(``success`` for HTTP 2xx, ``error`` otherwise)."
        ),
    ),
) -> dict[str, object]:
    """Return MCP request counter snapshot grouped by client_source.

    The response is computed in three stages:

    1. Fetch the Prometheus exposition from the MCP server via the
       wired :class:`McpMetricsClient`.
    2. Apply optional ``client_source`` / ``tool`` / ``status``
       filters to narrow the row set.
    3. Aggregate the filtered rows into the totals envelope and
       return both the totals and the row list.
    """

    client = _get_client(request)
    try:
        counters = await client.fetch_request_counters()
    except McpMetricsError as exc:
        logger.warning(
            "mcp_metrics_fetch_failed: %s (cause=%s)",
            exc,
            getattr(exc, "cause", None),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "mcp_metrics_fetch_failed",
                "message": str(exc),
            },
        ) from exc

    filtered = _filter_counters(
        counters,
        client_source=client_source,
        tool=tool,
        wf_status=wf_status,
    )

    return {
        "totals": _aggregate(filtered),
        "rows": [row.to_response() for row in filtered],
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "source": "atlassian-mcp",
        "filters": {
            "client_source": client_source,
            "tool": tool,
            "status": wf_status,
        },
    }
