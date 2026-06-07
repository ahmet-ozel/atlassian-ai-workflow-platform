"""Unit tests for ``src.routers.mcp_traffic`` .
The router exposes:
* ``GET /api/v1/mcp/traffic`` - counters fetched from the MCP server's
  ``/metrics`` endpoint, optionally filtered by ``client_source`` /
  ``tool`` / ``status``.
These tests inject:
* A :class:`_FakeMcpMetricsClient` that records the requested call and
  scripts the response (success / :class:`McpMetricsError`).
* An override on :func:`require_admin` so the OIDC layer can be bypassed."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# sys.path bootstrap (mirror the pattern other tests use).
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for _lib in ("audit_logger", "auth-shared", "http-shared"):
    _src = _WORKSPACE_ROOT / "libs" / _lib / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))


from src.auth.dependencies import AuthClaims, require_admin  # noqa: E402
from src.clients.mcp_metrics_client import (  # noqa: E402
    McpMetricsError,
    McpRequestCounter,
)
from src.routers.mcp_traffic import router as mcp_traffic_router  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeMcpMetricsClient:
    """In-memory :class:`McpMetricsClient` stub.

    Tests configure :attr:`counters` (the scripted response) and
    optionally :attr:`raise_on_call` (an :class:`McpMetricsError` to
    surface from :meth:`fetch_request_counters`).
    """

    def __init__(self) -> None:
        self.counters: list[McpRequestCounter] = []
        self.raise_on_call: McpMetricsError | None = None
        self.call_count: int = 0

    async def fetch_request_counters(self) -> list[McpRequestCounter]:
        self.call_count += 1
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return list(self.counters)


def _build_app(
    *,
    client: _FakeMcpMetricsClient | None,
    actor_sub: str = "admin-1",
) -> FastAPI:
    app = FastAPI()
    app.include_router(mcp_traffic_router)
    app.state.mcp_metrics_client = client
    app.dependency_overrides[require_admin] = lambda: AuthClaims(
        sub=actor_sub, groups=("admin",)
    )
    return app


def _sample_counters() -> list[McpRequestCounter]:
    return [
        McpRequestCounter(
            client_source="automation-worker",
            tool="jira_get_issue",
            status="success",
            count=12,
        ),
        McpRequestCounter(
            client_source="automation-worker",
            tool="jira_get_issue",
            status="error",
            count=1,
        ),
        McpRequestCounter(
            client_source="streamlit-ui",
            tool="confluence_search",
            status="success",
            count=5,
        ),
        McpRequestCounter(
            client_source="unknown",
            tool="initialize",
            status="success",
            count=3,
        ),
    ]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_traffic_returns_rows_and_totals() -> None:
    """Counters → JSON envelope with rows + totals + filters."""

    fake = _FakeMcpMetricsClient()
    fake.counters = _sample_counters()
    client = TestClient(_build_app(client=fake))

    response = client.get("/api/v1/mcp/traffic")

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["source"] == "atlassian-mcp"
    assert body["filters"] == {
        "client_source": None,
        "tool": None,
        "status": None,
    }
    assert "fetched_at" in body and body["fetched_at"]

    # Rows are returned in the order the client emitted them.
    rows = body["rows"]
    assert len(rows) == 4
    assert rows[0] == {
        "client_source": "automation-worker",
        "tool": "jira_get_issue",
        "status": "success",
        "count": 12,
    }

    totals = body["totals"]
    assert totals["total"] == 12 + 1 + 5 + 3
    # Sorted desc by count.
    assert list(totals["by_client_source"].keys())[0] == "automation-worker"
    assert totals["by_client_source"]["automation-worker"] == 13
    assert totals["by_client_source"]["streamlit-ui"] == 5
    assert totals["by_client_source"]["unknown"] == 3

    assert totals["by_tool"]["jira_get_issue"] == 13
    assert totals["by_tool"]["confluence_search"] == 5
    assert totals["by_tool"]["initialize"] == 3

    assert totals["by_status"]["success"] == 20
    assert totals["by_status"]["error"] == 1


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


def test_traffic_filters_by_client_source() -> None:
    fake = _FakeMcpMetricsClient()
    fake.counters = _sample_counters()
    client = TestClient(_build_app(client=fake))

    response = client.get(
        "/api/v1/mcp/traffic", params={"client_source": "automation-worker"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["filters"]["client_source"] == "automation-worker"
    assert {r["client_source"] for r in body["rows"]} == {"automation-worker"}
    assert body["totals"]["total"] == 13


def test_traffic_filters_by_tool() -> None:
    fake = _FakeMcpMetricsClient()
    fake.counters = _sample_counters()
    client = TestClient(_build_app(client=fake))

    response = client.get(
        "/api/v1/mcp/traffic", params={"tool": "jira_get_issue"}
    )

    assert response.status_code == 200
    body = response.json()
    assert {r["tool"] for r in body["rows"]} == {"jira_get_issue"}
    assert body["totals"]["total"] == 13


def test_traffic_filters_by_status() -> None:
    fake = _FakeMcpMetricsClient()
    fake.counters = _sample_counters()
    client = TestClient(_build_app(client=fake))

    response = client.get("/api/v1/mcp/traffic", params={"status": "error"})

    assert response.status_code == 200
    body = response.json()
    assert {r["status"] for r in body["rows"]} == {"error"}
    assert body["totals"]["total"] == 1


def test_traffic_combines_filters() -> None:
    fake = _FakeMcpMetricsClient()
    fake.counters = _sample_counters()
    client = TestClient(_build_app(client=fake))

    response = client.get(
        "/api/v1/mcp/traffic",
        params={
            "client_source": "automation-worker",
            "tool": "jira_get_issue",
            "status": "success",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["count"] == 12
    assert body["totals"]["total"] == 12


def test_traffic_empty_result_when_no_match() -> None:
    fake = _FakeMcpMetricsClient()
    fake.counters = _sample_counters()
    client = TestClient(_build_app(client=fake))

    response = client.get(
        "/api/v1/mcp/traffic", params={"client_source": "no-such-caller"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["rows"] == []
    assert body["totals"]["total"] == 0
    assert body["totals"]["by_client_source"] == {}


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


def test_traffic_returns_503_when_client_unwired() -> None:
    """``app.state.mcp_metrics_client is None`` → 503 with reason."""

    client = TestClient(_build_app(client=None))

    response = client.get("/api/v1/mcp/traffic")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["status"] == "not_ready"
    assert detail["reason"] == "mcp_metrics_unavailable"


def test_traffic_returns_502_on_metrics_error() -> None:
    """Client raises ``McpMetricsError`` → 502 ``mcp_metrics_fetch_failed``."""

    fake = _FakeMcpMetricsClient()
    fake.raise_on_call = McpMetricsError("upstream timeout")
    client = TestClient(_build_app(client=fake))

    response = client.get("/api/v1/mcp/traffic")

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["error"] == "mcp_metrics_fetch_failed"
    assert "upstream timeout" in detail["message"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_traffic_requires_admin() -> None:
    """No admin override → 401 from require_admin (no bearer token)."""

    fake = _FakeMcpMetricsClient()
    fake.counters = _sample_counters()

    # Build app without dependency override so the real require_admin
    # runs. No Authorization header → 401.
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(mcp_traffic_router)
    app.state.mcp_metrics_client = fake

    client = TestClient(app)
    response = client.get("/api/v1/mcp/traffic")
    assert response.status_code == 401
    # Underlying client is never called when auth fails.
    assert fake.call_count == 0
