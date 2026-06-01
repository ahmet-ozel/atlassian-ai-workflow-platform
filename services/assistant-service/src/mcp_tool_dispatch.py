"""MCP tool dispatch for assistant-service chat."""

from __future__ import annotations

import contextvars
import json
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

_credential_refs: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "assistant_credential_refs", default={}
)

_MCP_ACCEPT = "application/json, text/event-stream"


def bind_credential_refs(refs: dict[str, str]):
    """Bind per-request credential refs for the streaming tool loop."""
    return _credential_refs.set(dict(refs))


def reset_credential_refs(token: contextvars.Token[dict[str, str]]) -> None:
    _credential_refs.reset(token)


class McpToolDispatch:
    """Invoke stateless Atlassian MCP tools with session credentials."""

    def __init__(self, *, mcp_base_url: str, session_deps: Any) -> None:
        self._mcp_base_url = mcp_base_url
        self._session_deps = session_deps

    async def list_tools(self) -> list[dict[str, Any]]:
        headers = {
            "X-Client-Source": "assistant-service",
            "Accept": _MCP_ACCEPT,
        }
        for service, credential in self._read_bound_credentials().items():
            headers.update(_mcp_headers(service, credential))
        async with httpx.AsyncClient(base_url=self._mcp_base_url, timeout=15.0) as client:
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": "tools", "method": "tools/list"},
                headers=headers,
            )
        if response.status_code >= 400:
            return []
        payload = _jsonrpc_payload(response)
        result = payload.get("result") if isinstance(payload, dict) else None
        tools = result.get("tools") if isinstance(result, dict) else None
        return tools if isinstance(tools, list) else []

    async def invoke(self, tool_call: Any) -> Any:
        name = _tool_name(tool_call)
        args = _tool_args(tool_call)
        service = _service_for_tool(name)
        credential = self._read_credential(service)
        if service == "bitbucket":
            args = _with_bitbucket_workspace(name, args, credential)
        headers = _mcp_headers(service, credential)
        async with httpx.AsyncClient(base_url=self._mcp_base_url, timeout=30.0) as client:
            response = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": name,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": args},
                },
                headers=headers,
            )
        if response.status_code >= 400:
            return {"ok": False, "status": response.status_code, "body": response.text[:500]}
        return _jsonrpc_payload(response)

    def _read_credential(self, service: str) -> Mapping[str, str]:
        ref = _credential_refs.get().get(service) or _credential_refs.get().get("jira")
        if not ref:
            raise RuntimeError(f"{service} credential is not bound for this chat session")
        return self._read_ref(ref)

    def _read_bound_credentials(self) -> dict[str, Mapping[str, str]]:
        credentials: dict[str, Mapping[str, str]] = {}
        for service, ref in _credential_refs.get().items():
            if service not in {"jira", "bitbucket", "confluence"} or not ref:
                continue
            try:
                credentials[service] = self._read_ref(ref)
            except Exception:
                continue
        return credentials

    def _read_ref(self, ref: str) -> Mapping[str, str]:
        if self._session_deps is None or getattr(self._session_deps, "vault", None) is None:
            raise RuntimeError("session credential vault is not wired")
        from vault_client import VaultPath

        return self._session_deps.vault.read(VaultPath.parse(ref))


def _tool_name(tool_call: Any) -> str:
    if isinstance(tool_call, Mapping):
        return str(tool_call.get("tool_name") or tool_call.get("name") or "")
    return str(getattr(tool_call, "tool_name", "") or getattr(tool_call, "name", ""))


def _tool_args(tool_call: Any) -> dict[str, Any]:
    if isinstance(tool_call, Mapping):
        raw = tool_call.get("arguments") or tool_call.get("args") or {}
    else:
        raw = getattr(tool_call, "arguments", None) or getattr(tool_call, "args", {})
    return dict(raw) if isinstance(raw, Mapping) else {}


def _service_for_tool(name: str) -> str:
    if name.startswith("bitbucket_"):
        return "bitbucket"
    if name.startswith("confluence_"):
        return "confluence"
    return "jira"


def _with_bitbucket_workspace(
    name: str,
    args: dict[str, Any],
    credential: Mapping[str, str],
) -> dict[str, Any]:
    if name != "bitbucket_list_repos" or args.get("project_key"):
        return args
    workspace = _bitbucket_workspace_from_url(credential.get("url", ""))
    if not workspace:
        return args
    enriched = dict(args)
    enriched["project_key"] = workspace
    return enriched


def _bitbucket_workspace_from_url(url: str) -> str:
    parsed = urlparse(url)
    if "bitbucket.org" not in parsed.netloc.lower():
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0] if parts else ""


def _mcp_headers(service: str, credential: Mapping[str, str]) -> dict[str, str]:
    prefix = f"X-Atlassian-{service.capitalize()}"
    url = credential.get("url", "")
    username = credential.get("username") or credential.get("email", "")
    token = credential.get("personal_token") or credential.get("token")
    api_token = credential.get("api_token") or ""
    app_password = credential.get("app_password") or ""
    cloud_access_token = credential.get("cloud_access_token") or ""
    headers = {
        "X-Client-Source": "assistant-service",
        "Accept": _MCP_ACCEPT,
        f"{prefix}-Url": url,
        f"{prefix}-Username": username,
    }
    if service == "bitbucket":
        _put_bitbucket_token_headers(
            headers,
            prefix=prefix,
            url=url,
            username=username,
            token=token or "",
            app_password=app_password,
            cloud_access_token=cloud_access_token,
        )
    elif api_token:
        headers[f"{prefix}-Api-Token"] = api_token
    elif token and _looks_like_cloud_atlassian(url, username):
        headers[f"{prefix}-Api-Token"] = token
    elif token:
        headers[f"{prefix}-Personal-Token"] = token
    return {k: v for k, v in headers.items() if v}


def _jsonrpc_payload(response: httpx.Response) -> dict[str, Any]:
    """Decode JSON-RPC payloads returned as JSON or SSE message data."""

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
    return {}


def _looks_like_cloud_atlassian(url: str, username: str) -> bool:
    hostish = url.lower()
    return ".atlassian.net" in hostish or "@" in username


def _looks_like_bitbucket_cloud(url: str) -> bool:
    hostish = url.lower()
    return "bitbucket.org" in hostish or "api.bitbucket.org" in hostish


def _put_bitbucket_token_headers(
    headers: dict[str, str],
    *,
    prefix: str,
    url: str,
    username: str,
    token: str,
    app_password: str,
    cloud_access_token: str,
) -> None:
    if cloud_access_token:
        headers[f"{prefix}-Cloud-Access-Token"] = cloud_access_token
        return
    if app_password:
        headers[f"{prefix}-App-Password"] = app_password
        return
    if not token:
        return
    if _looks_like_bitbucket_cloud(url):
        if token.startswith("ATCTT") or not username:
            headers[f"{prefix}-Cloud-Access-Token"] = token
        else:
            headers[f"{prefix}-App-Password"] = token
        return
    headers[f"{prefix}-Personal-Token"] = token
