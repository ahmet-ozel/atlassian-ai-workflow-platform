"""Unit tests for ``ClientSourceLoggingMiddleware``.

Validates: Requirements 9.1, 9.2, 9.6 (platform-gap-fill spec)

* 9.1 — every request log carries a ``client_source`` field.
* 9.2 — missing/empty ``X-Client-Source`` header → ``client_source = "unknown"``.
* 9.6 — Prometheus exposes
  ``mcp_requests_total{client_source, tool, status}`` with the value
  observed on the request.

These tests drive the middleware directly through the ASGI protocol so
they don't depend on a full FastMCP server lifecycle.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from prometheus_client import generate_latest

from mcp_atlassian.servers.main import (
    ClientSourceLoggingMiddleware,
    _METRICS_REGISTRY,
    _extract_tool_from_jsonrpc_body,
    mcp_requests_total,
)


# ---------------------------------------------------------------------------
# ASGI helpers
# ---------------------------------------------------------------------------


def _build_scope(
    *,
    method: str = "POST",
    path: str = "/mcp",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": headers or [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
    }


class _MockApp:
    """Minimal ASGI 3 app that returns a configurable HTTP status."""

    def __init__(self, status: int = 200, body: bytes = b'{"ok":true}') -> None:
        self.status = status
        self.body = body
        self.received_body: bytes = b""
        self.calls: int = 0

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        self.calls += 1
        # Drain the body via the (potentially wrapped) receive callable
        # so we can assert the middleware replays the buffered payload.
        chunks: list[bytes] = []
        more = True
        while more:
            msg = await receive()
            if msg["type"] != "http.request":
                break
            chunks.append(msg.get("body", b"") or b"")
            more = msg.get("more_body", False)
        self.received_body = b"".join(chunks)
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": self.body})


def _make_receive(body: bytes) -> Any:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _collect_send() -> tuple[list[dict[str, Any]], Any]:
    sent: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    return sent, send


def _counter_value(*, client_source: str, tool: str, status: str) -> float:
    """Read the current value of the labelled counter."""
    return mcp_requests_total.labels(
        client_source=client_source, tool=tool, status=status
    )._value.get()


# ---------------------------------------------------------------------------
# Tool extraction from JSON-RPC bodies
# ---------------------------------------------------------------------------


class TestExtractToolFromJsonrpc:
    def test_tools_call_returns_tool_name(self) -> None:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "jira_get_issue", "arguments": {}},
            }
        ).encode("utf-8")
        assert _extract_tool_from_jsonrpc_body(body) == "jira_get_issue"

    def test_other_method_returns_method_name(self) -> None:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        ).encode("utf-8")
        assert _extract_tool_from_jsonrpc_body(body) == "tools/list"

    def test_empty_body_returns_none(self) -> None:
        assert _extract_tool_from_jsonrpc_body(b"") is None

    def test_malformed_json_returns_none(self) -> None:
        assert _extract_tool_from_jsonrpc_body(b"not json") is None

    def test_non_object_payload_returns_none(self) -> None:
        assert _extract_tool_from_jsonrpc_body(b"[1, 2, 3]") is None

    def test_tools_call_without_name_returns_none(self) -> None:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}
        ).encode("utf-8")
        assert _extract_tool_from_jsonrpc_body(body) is None


# ---------------------------------------------------------------------------
# Middleware behaviour — Requirements 9.1, 9.2, 9.6
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestClientSourceLoggingMiddleware:
    async def test_header_present_logs_client_source(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """R9.1 — log records carry the supplied ``client_source``."""
        app = _MockApp()
        mw = ClientSourceLoggingMiddleware(app)
        scope = _build_scope(headers=[(b"x-client-source", b"automation-worker")])
        sent, send = _collect_send()

        with caplog.at_level(logging.INFO, logger="mcp-atlassian.client_source"):
            await mw(scope, _make_receive(b'{}'), send)

        assert scope["state"]["client_source"] == "automation-worker"
        records = [r for r in caplog.records if r.name == "mcp-atlassian.client_source"]
        assert records, "expected a structured client_source log record"
        assert records[0].client_source == "automation-worker"

    async def test_header_missing_defaults_to_unknown(self) -> None:
        """R9.2 — absent header → ``client_source = "unknown"``."""
        app = _MockApp()
        mw = ClientSourceLoggingMiddleware(app)
        scope = _build_scope(headers=[])
        sent, send = _collect_send()

        await mw(scope, _make_receive(b""), send)

        assert scope["state"]["client_source"] == "unknown"
        assert app.calls == 0
        assert sent[0]["status"] == 400

    async def test_header_empty_value_defaults_to_unknown(self) -> None:
        """R9.2 — whitespace-only header → ``unknown``."""
        app = _MockApp()
        mw = ClientSourceLoggingMiddleware(app)
        scope = _build_scope(headers=[(b"x-client-source", b"   ")])
        sent, send = _collect_send()

        await mw(scope, _make_receive(b""), send)

        assert scope["state"]["client_source"] == "unknown"
        assert app.calls == 0
        assert sent[0]["status"] == 400

    async def test_request_body_replayed_to_downstream_app(self) -> None:
        """The middleware must not consume the request body — the
        wrapped app needs to read the same payload."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "confluence_search", "arguments": {"q": "foo"}},
            }
        ).encode("utf-8")
        app = _MockApp()
        mw = ClientSourceLoggingMiddleware(app)
        scope = _build_scope(headers=[(b"x-client-source", b"streamlit-ui")])
        sent, send = _collect_send()

        await mw(scope, _make_receive(body), send)

        assert app.received_body == body
        assert app.calls == 1

    async def test_metric_increments_with_tool_label_for_tools_call(self) -> None:
        """R9.6 — successful tools/call → ``status="success"`` counter."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "jira_get_issue", "arguments": {}},
            }
        ).encode("utf-8")
        before = _counter_value(
            client_source="streamlit-ui", tool="jira_get_issue", status="success"
        )

        mw = ClientSourceLoggingMiddleware(_MockApp(status=200))
        scope = _build_scope(headers=[(b"x-client-source", b"streamlit-ui")])
        _, send = _collect_send()
        await mw(scope, _make_receive(body), send)

        after = _counter_value(
            client_source="streamlit-ui", tool="jira_get_issue", status="success"
        )
        assert after - before == pytest.approx(1.0)

    async def test_metric_records_error_status_on_5xx(self) -> None:
        """R9.6 — non-2xx response → ``status="error"`` counter."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "bitbucket_get_pull_request", "arguments": {}},
            }
        ).encode("utf-8")
        before = _counter_value(
            client_source="automation-worker",
            tool="bitbucket_get_pull_request",
            status="error",
        )

        mw = ClientSourceLoggingMiddleware(_MockApp(status=503))
        scope = _build_scope(headers=[(b"x-client-source", b"automation-worker")])
        _, send = _collect_send()
        await mw(scope, _make_receive(body), send)

        after = _counter_value(
            client_source="automation-worker",
            tool="bitbucket_get_pull_request",
            status="error",
        )
        assert after - before == pytest.approx(1.0)

    async def test_metric_uses_unknown_label_when_header_missing(self) -> None:
        """R9.6 — counter label defaults to ``unknown`` when no header."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "jira_search", "arguments": {}},
            }
        ).encode("utf-8")
        before = _counter_value(
            client_source="unknown", tool="jira_search", status="error"
        )

        mw = ClientSourceLoggingMiddleware(_MockApp(status=200))
        scope = _build_scope(headers=[])
        _, send = _collect_send()
        await mw(scope, _make_receive(body), send)

        after = _counter_value(
            client_source="unknown", tool="jira_search", status="error"
        )
        assert after - before == pytest.approx(1.0)

    async def test_metric_uses_method_label_for_non_tool_calls(self) -> None:
        """R9.6 — JSON-RPC ``initialize`` etc. surface as the method
        name in the ``tool`` label rather than dropping the metric."""
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}
        ).encode("utf-8")
        before = _counter_value(
            client_source="agent-runner-worker",
            tool="initialize",
            status="success",
        )

        mw = ClientSourceLoggingMiddleware(_MockApp(status=200))
        scope = _build_scope(headers=[(b"x-client-source", b"agent-runner-worker")])
        _, send = _collect_send()
        await mw(scope, _make_receive(body), send)

        after = _counter_value(
            client_source="agent-runner-worker",
            tool="initialize",
            status="success",
        )
        assert after - before == pytest.approx(1.0)

    async def test_non_http_scope_passes_through(self) -> None:
        """Lifespan / websocket scopes must be forwarded unchanged."""
        app = _MockApp()
        mw = ClientSourceLoggingMiddleware(app)
        scope: dict[str, Any] = {"type": "lifespan"}

        async def receive() -> dict[str, Any]:
            return {"type": "lifespan.startup"}

        async def send(_: dict[str, Any]) -> None:
            pass

        # Should not raise — the middleware simply delegates.
        await mw(scope, receive, send)
        assert app.calls == 1

    async def test_metric_exposed_in_prometheus_exposition(self) -> None:
        """R9.6 — the counter is part of the MCP-server registry and
        is therefore picked up by ``GET /metrics``."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "jira_add_comment", "arguments": {}},
            }
        ).encode("utf-8")

        mw = ClientSourceLoggingMiddleware(_MockApp(status=200))
        scope = _build_scope(headers=[(b"x-client-source", b"streamlit-ui")])
        _, send = _collect_send()
        await mw(scope, _make_receive(body), send)

        exposition = generate_latest(_METRICS_REGISTRY).decode("utf-8")
        assert "mcp_requests_total" in exposition
        # Ensure the three required label keys appear in the exposed series.
        assert 'client_source="streamlit-ui"' in exposition
        assert 'tool="jira_add_comment"' in exposition
        assert 'status="success"' in exposition
