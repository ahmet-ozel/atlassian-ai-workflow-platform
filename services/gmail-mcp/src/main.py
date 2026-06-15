"""Read-only Gmail MCP placeholder service.

This service owns Gmail OAuth configuration via its environment. The first
platform integration exposes a healthy JSON-RPC MCP surface with read-only
tool descriptors; real Google API calls can replace the placeholder handlers
without changing Streamlit or dashboard wiring.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


PROVIDER = os.environ.get("MAIL_MCP_PROVIDER", "gmail").strip().lower() or "gmail"
READ_ONLY = os.environ.get("MAIL_MCP_READ_ONLY", "true").strip().lower() != "false"


def _tool(name: str, description: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "additionalProperties": True,
        },
        "annotations": {"readOnlyHint": True},
    }


TOOLS = [
    _tool(
        "gmail_list_messages",
        "List recent Gmail messages.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 25}},
    ),
    _tool(
        "gmail_list_unread_messages",
        "List unread Gmail messages.",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 25}},
    ),
    _tool(
        "gmail_search_messages",
        "Search Gmail messages by query, sender, or subject.",
        {
            "query": {"type": "string"},
            "from": {"type": "string"},
            "subject": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 25},
        },
    ),
    _tool(
        "gmail_get_message",
        "Get one Gmail message by id.",
        {
            "message_id": {"type": "string"},
            "id": {"type": "string"},
            "include_body": {"type": "boolean"},
        },
    ),
]


app = FastAPI(title="gmail-mcp", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "provider": PROVIDER, "read_only": READ_ONLY}


@app.post("/mcp")
async def mcp(request: Request) -> JSONResponse:
    payload = await request.json()
    method = payload.get("method")
    request_id = payload.get("id")

    if method == "tools/list":
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": TOOLS},
            }
        )

    if method == "tools/call":
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        tool_name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if tool_name not in {tool["name"] for tool in TOOLS}:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                },
                status_code=200,
            )
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Gmail MCP read-only placeholder is running. "
                                "OAuth/API integration is not configured in this stub."
                            ),
                        }
                    ],
                    "structuredContent": {
                        "provider": PROVIDER,
                        "tool": tool_name,
                        "arguments": arguments,
                        "items": [],
                        "read_only": READ_ONLY,
                        "oauth_owner": "gmail-mcp",
                    },
                },
            }
        )

    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        },
        status_code=200,
    )
