"""Unit test — ``make_mcp_client`` injects ``X-Trace-Id`` from contextvars.

Validates: platform-gap-fill Requirement 8.4 (MCP requests must carry
``X-Trace-Id`` from :func:`observability.get_trace_id`).

The :func:`http_shared.make_mcp_client` factory is the single point that
constructs every outbound MCP / Firecrawl client across the platform.
After platform-gap-fill task 7.2 each client emits the current
contextvars-bound trace_id on every outgoing request via an httpx
event hook, so the MCP server (and every Atlassian API call relayed
through it) can correlate logs back to the originating webhook /
admin action.

The tests below exercise the contract directly against an
:class:`httpx.MockTransport` — no real network involved — so the
property holds independently of Vault, Postgres, or the workspace
``observability`` lib being importable.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from http_shared import make_mcp_client
from observability import set_trace_id


@pytest.mark.anyio
async def test_make_mcp_client_injects_trace_id_from_context() -> None:
    """X-Trace-Id is set from get_trace_id() on every outgoing request."""

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    set_trace_id("018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34")
    transport = httpx.MockTransport(handler)
    client = make_mcp_client(
        client_source="agent-runner-worker",
        transport=transport,
    )
    try:
        await client.post("http://atlassian-mcp:8090/mcp", json={})
    finally:
        await client.aclose()

    assert len(captured) == 1
    assert (
        captured[0].headers["X-Trace-Id"]
        == "018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34"
    )
    # And the X-Client-Source header is still injected — the trace
    # hook composes with (does not replace) the existing factory
    # behaviour.
    assert captured[0].headers["X-Client-Source"] == "agent-runner-worker"


@pytest.mark.anyio
async def test_make_mcp_client_omits_trace_id_when_context_empty() -> None:
    """No X-Trace-Id header is emitted when get_trace_id() returns ''."""

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    set_trace_id("")  # explicitly clear
    transport = httpx.MockTransport(handler)
    client = make_mcp_client(
        client_source="automation-worker",
        transport=transport,
    )
    try:
        await client.post("http://atlassian-mcp:8090/mcp", json={})
    finally:
        await client.aclose()

    assert len(captured) == 1
    # Header must be absent — empty trace_id values are not emitted
    # so log aggregators are not flooded with empty-string fields.
    assert "X-Trace-Id" not in captured[0].headers


@pytest.mark.anyio
async def test_make_mcp_client_preserves_caller_supplied_trace_id() -> None:
    """A request-level X-Trace-Id wins over the contextvars value."""

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"ok": True})

    set_trace_id("018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34")
    transport = httpx.MockTransport(handler)
    client = make_mcp_client(
        client_source="automation-service",
        transport=transport,
    )
    try:
        # The caller pins a specific trace_id on the request itself —
        # the hook must not overwrite it (this path is reserved for
        # explicit retry / replay scenarios).
        await client.post(
            "http://atlassian-mcp:8090/mcp",
            json={},
            headers={"X-Trace-Id": "caller-pinned-trace"},
        )
    finally:
        await client.aclose()

    assert len(captured) == 1
    assert captured[0].headers["X-Trace-Id"] == "caller-pinned-trace"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
