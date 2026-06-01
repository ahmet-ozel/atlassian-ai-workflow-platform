"""Unit tests for the production HTTP-backed :class:`HttpMCPCaller`.

Validates: Requirements 9.3, 9.4 (platform-gap-fill spec)

Properties verified:

* Every outgoing MCP request carries ``X-Client-Source: automation-worker``
  (R9.3).
* The caller routes the request through the JSON-RPC ``tools/call``
  envelope and returns the parsed ``result`` body to the activity layer.
* JSON-RPC ``error`` envelopes and HTTP non-2xx responses are surfaced
  as :class:`MCPHttpError` instead of silently succeeding.
* The right Atlassian service credential is selected for each tool name
  (jira / bitbucket / confluence) so :func:`with_atlassian_creds` injects
  the correct headers.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap — mirror ``test_output_actions.py``.
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
_PLATFORM_ROOT: Path = _WORKER_ROOT.parents[1]
_HTTP_SHARED_SRC: Path = _PLATFORM_ROOT / "libs" / "http-shared" / "src"

for _candidate in (_SRC_DIR, _HTTP_SHARED_SRC):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)

from automation_worker.activities.mcp_caller import (  # noqa: E402
    CLIENT_SOURCE,
    HttpMCPCaller,
    MCPHttpError,
    infer_service_from_tool,
    resolve_mcp_tool_name,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeCredential:
    url: str = "https://example.atlassian.net"
    username: str = "bot@example.com"
    personal_token: str = "token"


class _FakeCredentialResolver:
    """Returns a constant credential — sufficient for asserting that
    :func:`with_atlassian_creds` runs successfully and the JSON-RPC
    request reaches the transport."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def get(
        self, dept_id: str, service: str, *, scope: str = "org"
    ) -> _FakeCredential:
        self.calls.append((dept_id, service, scope))
        return _FakeCredential()


def _make_caller(
    *, transport: httpx.MockTransport, base_url: str = "http://atlassian-mcp:8090"
) -> tuple[HttpMCPCaller, _FakeCredentialResolver]:
    """Build a caller that uses ``MockTransport`` while still going
    through ``http_shared.make_mcp_client`` so the
    ``X-Client-Source`` header is set by the same code path that
    runs in production."""
    from http_shared import make_mcp_client

    def _factory(*, base_url: str, timeout: float) -> httpx.AsyncClient:
        return make_mcp_client(
            client_source=CLIENT_SOURCE,
            base_url=base_url,
            timeout=timeout,
            headers={"Accept": "application/json, text/event-stream"},
            transport=transport,
        )

    resolver = _FakeCredentialResolver()
    caller = HttpMCPCaller(
        credential_resolver=resolver,
        base_url=base_url,
        client_factory=_factory,
    )
    return caller, resolver


# ---------------------------------------------------------------------------
# Tool → service mapping (R9.3 — credential injection routing)
# ---------------------------------------------------------------------------


class TestInferServiceFromTool:
    @pytest.mark.parametrize(
        "tool_name, expected_service",
        [
            ("jira_add_comment", "jira"),
            ("jira_add_attachment", "jira"),
            ("jira_transition_issue", "jira"),
            ("bitbucket_create_pr", "bitbucket"),
            ("bitbucket_create_pull_request", "bitbucket"),
            ("bitbucket_put_file_content", "bitbucket"),
            ("confluence_create_page", "confluence"),
            ("confluence_update_page", "confluence"),
        ],
    )
    def test_known_tools_map_to_expected_service(
        self, tool_name: str, expected_service: str
    ) -> None:
        assert infer_service_from_tool(tool_name) == expected_service

    def test_unknown_tool_defaults_to_jira(self) -> None:
        assert infer_service_from_tool("totally_made_up_tool") == "jira"

    def test_pr_alias_maps_to_mounted_fastmcp_tool_name(self) -> None:
        assert (
            resolve_mcp_tool_name("bitbucket_create_pr")
            == "bitbucket_create_pull_request"
        )


# ---------------------------------------------------------------------------
# Header propagation — Requirement 9.3
# ---------------------------------------------------------------------------


class TestClientSourceHeader:
    @pytest.mark.anyio
    async def test_outgoing_request_carries_x_client_source_header(self) -> None:
        """R9.3 — every MCP call from automation-worker carries
        ``X-Client-Source: automation-worker``."""

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [{"type": "text", "text": "{}"}]},
                },
            )

        transport = httpx.MockTransport(handler)
        caller, _ = _make_caller(transport=transport)

        result = await caller.call_tool(
            "jira_add_comment",
            {"issue_key": "PAY-1", "body": "hi"},
            dept_id="payments",
        )

        assert len(captured) == 1
        request = captured[0]
        assert request.headers.get("X-Client-Source") == CLIENT_SOURCE
        assert request.headers.get("Accept") == "application/json, text/event-stream"
        assert CLIENT_SOURCE == "automation-worker"
        # JSON-RPC envelope + tool name are forwarded.
        body = json.loads(request.content.decode("utf-8"))
        assert body["method"] == "tools/call"
        assert body["params"]["name"] == "jira_add_comment"
        assert isinstance(result, dict)

    @pytest.mark.anyio
    async def test_bitbucket_pr_alias_is_resolved_in_jsonrpc_body(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
            )

        transport = httpx.MockTransport(handler)
        caller, _ = _make_caller(transport=transport)

        await caller.call_tool(
            "bitbucket_create_pr",
            {"project_key": "PAY", "repo_slug": "api"},
            dept_id="payments",
        )

        body = json.loads(captured[0].content.decode("utf-8"))
        assert body["params"]["name"] == "bitbucket_create_pull_request"

    @pytest.mark.anyio
    async def test_streamable_http_sse_response_is_decoded(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=(
                    'event: message\n'
                    'data: {"jsonrpc":"2.0","id":1,'
                    '"result":{"ok":true,"content":[]}}\n\n'
                ),
            )

        transport = httpx.MockTransport(handler)
        caller, _ = _make_caller(transport=transport)

        result = await caller.call_tool(
            "jira_add_comment",
            {"issue_key": "PAY-1", "body": "hi"},
            dept_id="payments",
        )

        assert result["ok"] is True


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    @pytest.mark.anyio
    async def test_jsonrpc_error_envelope_raises(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32000, "message": "boom"},
                },
            )

        transport = httpx.MockTransport(handler)
        caller, _ = _make_caller(transport=transport)

        with pytest.raises(MCPHttpError) as excinfo:
            await caller.call_tool(
                "jira_add_comment", {}, dept_id="payments"
            )
        assert "boom" in str(excinfo.value)

    @pytest.mark.anyio
    async def test_http_5xx_raises(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream down")

        transport = httpx.MockTransport(handler)
        caller, _ = _make_caller(transport=transport)

        with pytest.raises(MCPHttpError) as excinfo:
            await caller.call_tool(
                "jira_add_comment", {}, dept_id="payments"
            )
        assert excinfo.value.status_code == 503

    @pytest.mark.anyio
    async def test_empty_result_envelope_raises(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1})

        transport = httpx.MockTransport(handler)
        caller, _ = _make_caller(transport=transport)

        with pytest.raises(MCPHttpError):
            await caller.call_tool(
                "jira_add_comment", {}, dept_id="payments"
            )

    @pytest.mark.anyio
    async def test_mcp_is_error_result_raises(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": "bad args"}],
                    },
                },
            )

        transport = httpx.MockTransport(handler)
        caller, _ = _make_caller(transport=transport)

        with pytest.raises(MCPHttpError) as excinfo:
            await caller.call_tool(
                "jira_add_comment", {"dept_id": "payments"}, dept_id="payments"
            )
        assert "bad args" in str(excinfo.value)

    @pytest.mark.anyio
    async def test_embedded_success_false_result_raises(self) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": '{"success": false, "error": "nope"}',
                            }
                        ],
                    },
                },
            )

        transport = httpx.MockTransport(handler)
        caller, _ = _make_caller(transport=transport)

        with pytest.raises(MCPHttpError) as excinfo:
            await caller.call_tool(
                "bitbucket_put_file_content", {}, dept_id="payments"
            )
        assert "nope" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Credential routing
# ---------------------------------------------------------------------------


class TestCredentialRouting:
    @pytest.mark.anyio
    @pytest.mark.parametrize(
        "tool_name, expected_service",
        [
            ("jira_add_comment", "jira"),
            ("bitbucket_create_pr", "bitbucket"),
            ("bitbucket_put_file_content", "bitbucket"),
            ("confluence_create_page", "confluence"),
        ],
    )
    async def test_resolver_called_for_correct_service(
        self, tool_name: str, expected_service: str
    ) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": {}},
            )

        transport = httpx.MockTransport(handler)
        caller, resolver = _make_caller(transport=transport)

        await caller.call_tool(tool_name, {}, dept_id="payments")

        assert len(resolver.calls) == 1
        dept_id, service, scope = resolver.calls[0]
        assert dept_id == "payments"
        assert service == expected_service
        assert scope == "org"


# ---------------------------------------------------------------------------
# anyio backend selector — keep test runtime constrained to asyncio.
# ---------------------------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
