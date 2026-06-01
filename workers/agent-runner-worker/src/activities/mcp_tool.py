"""Small MCP client helpers for agent-runner activities."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from http_shared import make_mcp_client, with_atlassian_creds

from . import get_credential_resolver

MCP_ACCEPT = "application/json, text/event-stream"
MCP_PATH = "/mcp"
DEFAULT_MCP_BASE_URL = "http://atlassian-mcp:8090"


class MCPToolError(RuntimeError):
    """Raised when an MCP HTTP or JSON-RPC call fails."""

    def __init__(self, tool_name: str, status_code: int, detail: str) -> None:
        self.tool_name = tool_name
        self.status_code = status_code
        self.detail = detail
        super().__init__(
            f"MCP tool {tool_name!r} failed (status={status_code}): {detail}"
        )


def mcp_base_url() -> str:
    return os.environ.get("MCP_BASE_URL", DEFAULT_MCP_BASE_URL)


def build_jsonrpc_request(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }


def decode_jsonrpc_payload(response: httpx.Response, tool_name: str) -> dict[str, Any]:
    """Decode JSON-RPC payloads returned as JSON or SSE message frames."""

    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        return payload

    for line in response.text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise MCPToolError(
        tool_name,
        response.status_code,
        f"non-JSON/SSE response: {response.text[:500]}",
    )


def parse_jsonrpc_result(payload: dict[str, Any], tool_name: str) -> dict[str, Any]:
    if "error" in payload:
        error = payload.get("error") or {}
        message = error.get("message", "unknown error") if isinstance(error, dict) else str(error)
        raise MCPToolError(tool_name, -1, str(message))

    result = payload.get("result")
    if not isinstance(result, dict):
        if result is None:
            raise MCPToolError(tool_name, -1, "empty result envelope")
        return {"result": result}

    if result.get("isError") is True:
        raise MCPToolError(tool_name, -1, result_text(result)[:500])
    return result


def result_text(result: dict[str, Any]) -> str:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        nested = structured.get("result")
        if isinstance(nested, str) and nested:
            return nested
        if nested is not None:
            return json.dumps(nested, ensure_ascii=False)

    for item in result.get("content", []) or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            return item["text"]

    raw = result.get("result")
    if isinstance(raw, str):
        return raw
    if raw is not None:
        return json.dumps(raw, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


def result_data(result: dict[str, Any]) -> Any:
    text = result_text(result)
    try:
        return json.loads(text)
    except ValueError:
        return result


async def call_mcp_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    dept_id: str,
    service: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Call one Atlassian MCP tool with org credentials for a department."""

    resolver = get_credential_resolver()
    client = make_mcp_client(
        client_source="agent-runner-worker",
        timeout=timeout,
        base_url=mcp_base_url(),
        headers={"Accept": MCP_ACCEPT},
    )

    async with client:
        async with with_atlassian_creds(
            client,
            dept_id=dept_id,
            service=service,  # type: ignore[arg-type]
            credential_resolver=resolver,
        ) as authed:
            try:
                response = await authed.post(
                    MCP_PATH,
                    json=build_jsonrpc_request(tool_name, arguments),
                )
            except httpx.HTTPError as exc:
                raise MCPToolError(tool_name, -1, f"transport error: {exc}") from exc

            if response.status_code >= 400:
                raise MCPToolError(tool_name, response.status_code, response.text[:500])

            payload = decode_jsonrpc_payload(response, tool_name)

    return parse_jsonrpc_result(payload, tool_name)
