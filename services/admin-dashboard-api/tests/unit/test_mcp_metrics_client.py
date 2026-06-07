"""Unit tests for ``src.clients.mcp_metrics_client`` .
Covers parsing the MCP server's Prometheus exposition format and the
HTTP fetch flow against an :class:`httpx.MockTransport` so the suite
stays hermetic."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest


# ---------------------------------------------------------------------------
# sys.path bootstrap (mirror the pattern other tests in this package use).
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


from src.clients.mcp_metrics_client import (  # noqa: E402
    McpMetricsClient,
    McpMetricsError,
    McpRequestCounter,
    REQUEST_COUNTER_NAME,
    parse_request_counters,
)


# ---------------------------------------------------------------------------
# Fixtures: realistic Prometheus exposition body
# ---------------------------------------------------------------------------


_VALID_EXPOSITION = """\
# HELP mcp_requests_total Total MCP HTTP requests served, labelled by calling client, MCP tool name (or HTTP path when no tool is invoked) and outcome.
# TYPE mcp_requests_total counter
mcp_requests_total{client_source="automation-worker",tool="jira_get_issue",status="success"} 12.0
mcp_requests_total{client_source="automation-worker",tool="jira_get_issue",status="error"} 1.0
mcp_requests_total{client_source="streamlit-ui",tool="confluence_search",status="success"} 5.0
mcp_requests_total{client_source="unknown",tool="initialize",status="success"} 3.0
# HELP some_other_counter unrelated counter
# TYPE some_other_counter counter
some_other_counter{label="x"} 7.0
"""


_EMPTY_EXPOSITION = """\
# HELP mcp_requests_total Total MCP HTTP requests served.
# TYPE mcp_requests_total counter
"""


_MISSING_LABELS_EXPOSITION = """\
# HELP mcp_requests_total Total MCP HTTP requests served.
# TYPE mcp_requests_total counter
mcp_requests_total{client_source="x"} 1.0
mcp_requests_total{client_source="x",tool="y",status="success"} 2.0
"""


# ---------------------------------------------------------------------------
# parse_request_counters
# ---------------------------------------------------------------------------


def test_parse_request_counters_extracts_all_label_combinations() -> None:
    """Every well-formed sample becomes one ``McpRequestCounter`` row."""

    rows = parse_request_counters(_VALID_EXPOSITION)

    assert len(rows) == 4
    keys = {(r.client_source, r.tool, r.status, int(r.count)) for r in rows}
    assert keys == {
        ("automation-worker", "jira_get_issue", "success", 12),
        ("automation-worker", "jira_get_issue", "error", 1),
        ("streamlit-ui", "confluence_search", "success", 5),
        ("unknown", "initialize", "success", 3),
    }


def test_parse_request_counters_skips_unrelated_metrics() -> None:
    """Only ``mcp_requests_total`` samples are emitted."""

    rows = parse_request_counters(_VALID_EXPOSITION)

    # ``some_other_counter`` from the fixture must be absent.
    for row in rows:
        assert row.tool != "x"


def test_parse_request_counters_handles_empty_exposition() -> None:
    """No samples  empty list, no error."""

    rows = parse_request_counters(_EMPTY_EXPOSITION)
    assert rows == []


def test_parse_request_counters_skips_samples_with_missing_labels() -> None:
    """Samples missing any required label are ignored."""

    rows = parse_request_counters(_MISSING_LABELS_EXPOSITION)

    assert len(rows) == 1
    only = rows[0]
    assert only.client_source == "x"
    assert only.tool == "y"
    assert only.status == "success"
    assert int(only.count) == 2


def test_parse_request_counters_raises_on_unparseable_body() -> None:
    """Garbage input  ``McpMetricsError``."""

    with pytest.raises(McpMetricsError):
        parse_request_counters("not even close to valid \x00\x01\x02")


def test_request_counter_to_response_returns_int_count() -> None:
    """Floats are coerced to ints for the JSON response."""

    counter = McpRequestCounter(
        client_source="cs", tool="t", status="success", count=42.0
    )
    payload = counter.to_response()
    assert payload == {
        "client_source": "cs",
        "tool": "t",
        "status": "success",
        "count": 42,
    }
    assert isinstance(payload["count"], int)


# ---------------------------------------------------------------------------
# McpMetricsClient.fetch_request_counters
# ---------------------------------------------------------------------------


def _build_mock_client(
    responder, base_url: str = "http://atlassian-mcp:8090"
) -> tuple[McpMetricsClient, httpx.AsyncClient]:
    transport = httpx.MockTransport(responder)
    http_client = httpx.AsyncClient(transport=transport)
    return McpMetricsClient(base_url=base_url, http_client=http_client), http_client


@pytest.mark.asyncio
async def test_fetch_request_counters_returns_parsed_rows() -> None:
    """Happy path: 200 + valid body  list of counters."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/metrics"
        assert request.method == "GET"
        return httpx.Response(200, text=_VALID_EXPOSITION)

    client, http_client = _build_mock_client(respond)
    try:
        rows = await client.fetch_request_counters()
    finally:
        await http_client.aclose()

    assert len(rows) == 4
    sources = {r.client_source for r in rows}
    assert "automation-worker" in sources
    assert "streamlit-ui" in sources


@pytest.mark.asyncio
async def test_fetch_request_counters_strips_trailing_slash_in_base_url() -> None:
    """``base_url`` with trailing slash still resolves to ``/metrics``."""

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://atlassian-mcp:8090/metrics"
        return httpx.Response(200, text=_EMPTY_EXPOSITION)

    transport = httpx.MockTransport(respond)
    http_client = httpx.AsyncClient(transport=transport)
    client = McpMetricsClient(
        base_url="http://atlassian-mcp:8090/", http_client=http_client
    )
    try:
        rows = await client.fetch_request_counters()
    finally:
        await http_client.aclose()
    assert rows == []


@pytest.mark.asyncio
async def test_fetch_request_counters_raises_on_non_2xx() -> None:
    """5xx  ``McpMetricsError``."""

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    client, http_client = _build_mock_client(respond)
    try:
        with pytest.raises(McpMetricsError) as excinfo:
            await client.fetch_request_counters()
    finally:
        await http_client.aclose()

    assert "503" in str(excinfo.value)


@pytest.mark.asyncio
async def test_fetch_request_counters_raises_on_transport_error() -> None:
    """``httpx.HTTPError``  ``McpMetricsError`` with cause."""

    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("could not connect")

    client, http_client = _build_mock_client(respond)
    try:
        with pytest.raises(McpMetricsError) as excinfo:
            await client.fetch_request_counters()
    finally:
        await http_client.aclose()

    assert isinstance(excinfo.value.cause, httpx.HTTPError)


def test_constructor_rejects_empty_base_url() -> None:
    """``base_url=""``  ``ValueError`` at construction time."""

    with pytest.raises(ValueError):
        McpMetricsClient(base_url="", http_client=httpx.AsyncClient())


def test_request_counter_name_constant_matches_mcp_server() -> None:
    """Sanity check: counter name must match what the MCP server registers."""

    assert REQUEST_COUNTER_NAME == "mcp_requests_total"
