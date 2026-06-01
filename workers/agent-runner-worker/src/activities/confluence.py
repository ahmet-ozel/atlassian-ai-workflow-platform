"""Confluence MCP activities for AgentRunnerWorkflow."""

from __future__ import annotations

from typing import Any

from temporalio import activity

from . import get_credential_resolver
from .mcp_tool import MCPToolError, call_mcp_tool, result_data, result_text


class ConfluenceActivityError(RuntimeError):
    """Raised when a Confluence MCP call fails."""


async def _default_space(dept_id: str) -> str:
    try:
        creds = await get_credential_resolver().get(
            dept_id,
            "confluence",
            scope="org",
        )
    except Exception:  # noqa: BLE001
        creds = {}
    if isinstance(creds, dict):
        return str(
            creds.get("confluence_space_key")
            or creds.get("space_key")
            or ""
        ).strip()
    return ""


async def _confluence_tool(tool_name: str, args: dict[str, Any], dept_id: str) -> dict[str, Any]:
    try:
        return await call_mcp_tool(
            tool_name,
            args,
            dept_id=dept_id,
            service="confluence",
            timeout=60.0,
        )
    except MCPToolError as exc:
        raise ConfluenceActivityError(
            f"{tool_name} failed: {exc.detail}"
        ) from exc


def _as_dict(result: dict[str, Any]) -> dict[str, Any]:
    data = result_data(result)
    return data if isinstance(data, dict) else {"content": result_text(result)}


def _page_url(data: dict[str, Any]) -> str:
    links = data.get("links") if isinstance(data.get("links"), dict) else {}
    webui = links.get("webui") or links.get("base") or ""
    if isinstance(webui, str) and webui.startswith("http"):
        return webui
    for key in ("url", "web_url", "_links"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
        if isinstance(value, dict):
            nested = value.get("webui") or value.get("base")
            if isinstance(nested, str) and nested.startswith("http"):
                return nested
    return ""


@activity.defn(name="confluence_get_page")
async def confluence_get_page(page_id: str, dept_id: str) -> dict[str, Any]:
    activity.heartbeat(f"reading confluence page {page_id}")
    data = _as_dict(
        await _confluence_tool(
            "confluence_get_page",
            {"page_id": page_id, "include_metadata": True},
            dept_id,
        )
    )
    version = data.get("version") if isinstance(data.get("version"), dict) else {}
    history = data.get("history") if isinstance(data.get("history"), dict) else {}
    last_by = history.get("lastUpdatedBy") if isinstance(history, dict) else {}
    body = data.get("body") or data.get("content") or data.get("markdown") or ""
    return {
        **data,
        "id": str(data.get("id") or data.get("page_id") or page_id),
        "title": str(data.get("title") or ""),
        "body": str(body or ""),
        "url": _page_url(data),
        "last_edit_at": version.get("when") if isinstance(version, dict) else None,
        "last_editor_account_id": (
            last_by.get("accountId") if isinstance(last_by, dict) else None
        ),
    }


@activity.defn(name="confluence_create_page")
async def confluence_create_page(
    space_key: str,
    title: str,
    content: str,
    dept_id: str,
    parent_id: str | None = None,
) -> dict[str, Any]:
    space = (space_key or await _default_space(dept_id)).strip()
    if not space:
        raise ConfluenceActivityError("missing Confluence space_key")
    activity.heartbeat(f"creating confluence page {title}")
    data = _as_dict(
        await _confluence_tool(
            "confluence_create_page",
            {
                "space_key": space,
                "title": title,
                "content": content,
                "parent_id": parent_id,
                "content_format": "markdown",
            },
            dept_id,
        )
    )
    return {
        **data,
        "id": str(data.get("id") or data.get("page_id") or ""),
        "url": _page_url(data),
        "title": str(data.get("title") or title),
    }


@activity.defn(name="confluence_update_page")
async def confluence_update_page(
    page_id: str,
    title: str,
    content: str,
    dept_id: str,
) -> dict[str, Any]:
    activity.heartbeat(f"updating confluence page {page_id}")
    data = _as_dict(
        await _confluence_tool(
            "confluence_update_page",
            {
                "page_id": page_id,
                "title": title,
                "content": content,
                "content_format": "markdown",
                "is_minor_edit": False,
            },
            dept_id,
        )
    )
    return {
        **data,
        "id": str(data.get("id") or data.get("page_id") or page_id),
        "url": _page_url(data),
        "title": str(data.get("title") or title),
    }
