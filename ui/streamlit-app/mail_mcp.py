"""Minimal read-only MCP client for Streamlit Mail Chat.

The Atlassian chat helpers carry provider-specific auth headers, retry logic,
and LLM summarisation. Mail Chat only needs a small JSON-RPC transport layer
in this phase, so this module keeps the surface deliberately narrow.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal, Sequence

import httpx

from config import Settings


MailProvider = Literal["gmail", "outlook"]
MailToolCandidate = tuple[MailProvider, str, dict[str, Any]]

_MAIL_MCP_TIMEOUT_SECONDS = 30.0
_READ_ONLY_BLOCKED_TOKENS = frozenset(
    {
        "archive",
        "compose",
        "create",
        "delete",
        "draft",
        "flag",
        "forward",
        "label",
        "mark",
        "modify",
        "move",
        "reply",
        "send",
        "star",
        "trash",
        "unarchive",
        "unflag",
        "unlabel",
        "unstar",
        "update",
    }
)


class MailMcpError(RuntimeError):
    """Raised when a mail MCP request fails."""


class MailMcpWriteBlockedError(PermissionError):
    """Raised when a caller tries to invoke a non-read-only mail tool."""


def _provider_base_url(provider: MailProvider) -> str:
    settings = Settings()
    if provider == "gmail":
        return settings.gmail_mcp_base_url.rstrip("/")
    if provider == "outlook":
        return settings.outlook_mcp_base_url.rstrip("/")
    raise ValueError(f"Unknown mail MCP provider: {provider!r}")


def _headers() -> dict[str, str]:
    return {
        "Accept": "application/json, text/event-stream",
        "X-Client-Source": "streamlit-app:mail-chat",
    }


def _parse_mcp_response(text: str) -> Any:
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        payload = json.loads(raw)
        if isinstance(payload, dict) and payload.get("error"):
            raise MailMcpError(json.dumps(payload["error"], ensure_ascii=False))
        return payload.get("result", payload) if isinstance(payload, dict) else payload

    payload = json.loads(text)
    if isinstance(payload, dict) and payload.get("error"):
        raise MailMcpError(json.dumps(payload["error"], ensure_ascii=False))
    return payload.get("result", payload) if isinstance(payload, dict) else payload


def _mail_mcp_jsonrpc(
    provider: MailProvider,
    method: str,
    params: dict[str, Any] | None = None,
) -> Any:
    base_url = _provider_base_url(provider)
    if not base_url:
        raise MailMcpError(f"{provider} MCP base URL is not configured")

    payload = {
        "jsonrpc": "2.0",
        "id": method,
        "method": method,
        "params": params or {},
    }
    timeout = httpx.Timeout(_MAIL_MCP_TIMEOUT_SECONDS, connect=5.0)
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{base_url}/mcp",
                json=payload,
                headers=_headers(),
            )
    except httpx.RequestError as exc:
        raise MailMcpError(f"{provider} MCP request failed: {exc}") from exc

    if response.status_code >= 400:
        raise MailMcpError(
            f"{provider} MCP HTTP {response.status_code}: {response.text[:500]}"
        )
    try:
        return _parse_mcp_response(response.text)
    except json.JSONDecodeError as exc:
        raise MailMcpError(f"{provider} MCP returned invalid JSON") from exc


def _tool_name(tool: Any) -> str:
    if isinstance(tool, str):
        return tool
    if isinstance(tool, dict):
        name = tool.get("name")
        return name if isinstance(name, str) else ""
    name = getattr(tool, "name", "")
    return name if isinstance(name, str) else ""


def _tool_annotations(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict):
        annotations = tool.get("annotations")
        return annotations if isinstance(annotations, dict) else {}
    annotations = getattr(tool, "annotations", None)
    return annotations if isinstance(annotations, dict) else {}


def _name_tokens(tool_name: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9]+", tool_name.lower()) if token}


def _is_read_only_tool(tool: Any) -> bool:
    annotations = _tool_annotations(tool)
    if annotations.get("readOnlyHint") is False:
        return False
    if annotations.get("read_only") is False:
        return False
    return not (_name_tokens(_tool_name(tool)) & _READ_ONLY_BLOCKED_TOKENS)


def _ensure_read_only_tool(tool_name: str) -> None:
    if not _is_read_only_tool(tool_name):
        raise MailMcpWriteBlockedError(
            f"Mail MCP write tool blocked in read-only mode: {tool_name}"
        )


def _tools_from_result(result: Any) -> list[Any]:
    if isinstance(result, dict):
        tools = result.get("tools") or result.get("items") or result.get("result")
        if isinstance(tools, list):
            return tools
    if isinstance(result, list):
        return result
    return []


def list_mail_tools(provider: MailProvider) -> list[Any]:
    """Return the provider's available read-only mail MCP tools."""

    result = _mail_mcp_jsonrpc(provider, "tools/list")
    return [tool for tool in _tools_from_result(result) if _is_read_only_tool(tool)]


def mail_mcp_call(
    provider: MailProvider,
    tool_name: str,
    args: dict[str, Any] | None = None,
) -> Any:
    """Call a read-only mail MCP tool."""

    _ensure_read_only_tool(tool_name)
    return _mail_mcp_jsonrpc(
        provider,
        "tools/call",
        {"name": tool_name, "arguments": args or {}},
    )


def mail_mcp_call_any(
    provider_candidates: Sequence[MailToolCandidate],
) -> tuple[MailProvider, str, Any]:
    """Try mail MCP tool candidates in order and return the first success."""

    errors: list[str] = []
    for provider, tool_name, args in provider_candidates:
        try:
            return provider, tool_name, mail_mcp_call(provider, tool_name, args)
        except MailMcpWriteBlockedError:
            raise
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{provider}:{tool_name}: {exc}")
    raise MailMcpError(" | ".join(errors[-3:]) or "No mail MCP candidates provided")
