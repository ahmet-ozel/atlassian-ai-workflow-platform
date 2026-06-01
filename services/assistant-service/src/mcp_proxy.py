"""Credential-ref aware MCP proxy for Streamlit direct tool testing."""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .config import Settings
from .mcp_tool_dispatch import (
    McpToolDispatch,
    bind_credential_refs,
    reset_credential_refs,
)

router = APIRouter(prefix="/api/mcp", tags=["mcp-proxy"])


def _refs_from_headers(request: Request) -> dict[str, str]:
    refs = {
        "jira": request.headers.get("X-Credential-Ref-Jira")
        or request.headers.get("X-Credential-Ref")
        or "",
        "bitbucket": request.headers.get("X-Credential-Ref-Bitbucket") or "",
        "confluence": request.headers.get("X-Credential-Ref-Confluence") or "",
    }
    return {key: value for key, value in refs.items() if value}


def _dispatch(request: Request) -> McpToolDispatch:
    deps = getattr(request.app.state, "session_creds", None)
    if deps is None or getattr(deps, "vault", None) is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"reason": "session_credentials_not_wired"},
        )
    return McpToolDispatch(mcp_base_url=Settings().mcp_base_url, session_deps=deps)


@router.get("/tools")
async def list_tools(request: Request) -> JSONResponse:
    token = bind_credential_refs(_refs_from_headers(request))
    try:
        tools = await _dispatch(request).list_tools()
    finally:
        reset_credential_refs(token)
    return JSONResponse({"tools": tools})


@router.post("/tools/call")
async def call_tool(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc
    if not isinstance(body, Mapping):
        raise HTTPException(status_code=400, detail="JSON object body required")

    name = str(body.get("tool_name") or body.get("name") or body.get("tool") or "")
    if not name:
        raise HTTPException(status_code=400, detail="tool_name is required")
    args = body.get("arguments") or body.get("params") or {}
    if not isinstance(args, Mapping):
        raise HTTPException(status_code=400, detail="arguments must be an object")

    token = bind_credential_refs(_refs_from_headers(request))
    try:
        result = await _dispatch(request).invoke(
            {"tool_name": name, "arguments": dict(args)}
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        reset_credential_refs(token)
    return JSONResponse(_unwrap_jsonrpc(result))


def _unwrap_jsonrpc(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        return {"result": result}
    if "error" in result:
        return {"error": result["error"]}
    payload = result.get("result", result)
    if isinstance(payload, Mapping):
        return dict(payload)
    return {"result": payload}
