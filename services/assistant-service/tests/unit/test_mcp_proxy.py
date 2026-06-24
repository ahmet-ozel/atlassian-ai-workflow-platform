"""Tests for assistant-service MCP credential-ref proxy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import sys

_REPO_ROOT = Path(__file__).resolve().parents[4]
for _src in (
    _REPO_ROOT / "libs" / "vault_client" / "src",
    _REPO_ROOT / "libs" / "http-shared" / "src",
):
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.mcp_tool_dispatch import (  # noqa: E402
    McpToolDispatch,
    _ensure_read_only_mail_tool,
    _is_read_only_mail_tool,
    _jsonrpc_payload,
    _mcp_headers,
    _safe_error_text,
    _service_for_tool,
    _with_bitbucket_workspace,
    bind_credential_refs,
    reset_credential_refs,
)
from src.session_credentials import SessionCredentialDeps, router as session_router  # noqa: E402


class _Vault:
    def __init__(self) -> None:
        self.writes: list[tuple[Any, dict[str, str]]] = []

    def read(self, path: Any) -> dict[str, str]:
        return {"url": "https://example.atlassian.net", "username": "u@example.com", "personal_token": "tok"}

    def write(self, path: Any, data: dict[str, str]) -> None:
        self.writes.append((path, dict(data)))

    def delete(self, path: Any) -> None:
        return None


def _app() -> FastAPI:
    from src.mcp_proxy import router

    app = FastAPI()
    app.include_router(router)
    app.state.session_creds = SessionCredentialDeps(vault=_Vault())
    return app


def _session_app(vault: _Vault) -> FastAPI:
    app = FastAPI()
    app.include_router(session_router)
    app.state.session_creds = SessionCredentialDeps(vault=vault)
    return app


def test_jira_cloud_session_token_maps_to_api_token_header() -> None:
    headers = _mcp_headers(
        "jira",
        {
            "url": "https://acme.atlassian.net",
            "username": "bot@example.com",
            "personal_token": "ATATT3x-token",
        },
    )

    assert headers["X-Atlassian-Jira-Api-Token"] == "ATATT3x-token"
    assert "X-Atlassian-Jira-Personal-Token" not in headers


def test_bitbucket_cloud_tokens_map_to_supported_headers() -> None:
    bearer = _mcp_headers(
        "bitbucket",
        {
            "url": "https://api.bitbucket.org",
            "username": "bot-user",
            "personal_token": "ATCTT3x-workspace-token",
        },
    )
    basic = _mcp_headers(
        "bitbucket",
        {
            "url": "https://bitbucket.org",
            "username": "bot-user",
            "personal_token": "ATATT3x-app-password",
        },
    )

    assert bearer["X-Atlassian-Bitbucket-Cloud-Access-Token"].startswith("ATCTT")
    assert basic["X-Atlassian-Bitbucket-App-Password"].startswith("ATATT")


def test_bitbucket_list_repos_gets_workspace_from_credential_url() -> None:
    args = _with_bitbucket_workspace(
        "bitbucket_list_repos",
        {"limit": 3},
        {"url": "https://bitbucket.org/acme-workspace"},
    )

    assert args == {"limit": 3, "project_key": "acme-workspace"}


def test_list_tools_skips_missing_inferred_session_credentials() -> None:
    class _PartialVault:
        def read(self, path: Any) -> dict[str, str]:
            raw = str(getattr(path, "raw", path))
            if raw.endswith("/jira"):
                return {
                    "url": "https://example.atlassian.net",
                    "username": "u@example.com",
                    "personal_token": "tok",
                }
            raise KeyError(raw)

    dispatch = McpToolDispatch(
        mcp_base_url="http://mcp.test",
        session_deps=SessionCredentialDeps(vault=_PartialVault()),
    )
    token = bind_credential_refs(
        {
            "jira": "vault:atlassian/_user_session/s/jira",
            "confluence": "vault:atlassian/_user_session/s/confluence",
        }
    )
    try:
        credentials = dispatch._read_bound_credentials()
    finally:
        reset_credential_refs(token)

    assert list(credentials) == ["jira"]


def test_mail_tool_prefixes_route_to_mail_mcp_services() -> None:
    dispatch = McpToolDispatch(
        mcp_base_url="http://atlassian.test",
        gmail_mcp_base_url="http://gmail.test",
        outlook_mcp_base_url="http://outlook.test",
        session_deps=SessionCredentialDeps(vault=_Vault()),
    )

    assert _service_for_tool("gmail_list_messages") == "gmail"
    assert _service_for_tool("outlook_search_messages") == "outlook"
    assert dispatch._base_url_for_service("gmail") == "http://gmail.test"
    assert dispatch._base_url_for_service("outlook") == "http://outlook.test"
    assert dispatch._base_url_for_service("jira") == "http://atlassian.test"


def test_mail_dispatch_blocks_write_tools() -> None:
    assert _is_read_only_mail_tool(
        {"name": "gmail_search_messages", "annotations": {"readOnlyHint": True}}
    )
    assert not _is_read_only_mail_tool(
        {"name": "gmail_send_email", "annotations": {"readOnlyHint": True}}
    )
    assert not _is_read_only_mail_tool(
        {"name": "outlook_get_message", "annotations": {"readOnlyHint": False}}
    )

    with pytest.raises(RuntimeError, match="write tool blocked"):
        _ensure_read_only_mail_tool("outlook_delete_message")


def test_session_credentials_accept_mail_oauth_payload() -> None:
    vault = _Vault()
    resp = TestClient(_session_app(vault)).post(
        "/session/credentials",
        json={
            "session_id": "s1",
            "service": "gmail",
            "email": "alice@example.com",
            "refresh_token": "refresh-1",
            "client_id": "client-1",
            "client_secret": "secret-1",
            "scopes": "https://www.googleapis.com/auth/gmail.readonly",
        },
    )

    assert resp.status_code == 201
    assert resp.json()["vault_path"] == "vault:atlassian/_user_session/s1/gmail"
    assert len(vault.writes) == 1
    _, payload = vault.writes[0]
    assert payload == {
        "provider": "gmail",
        "refresh_token": "refresh-1",
        "email": "alice@example.com",
        "username": "alice@example.com",
        "client_id": "client-1",
        "client_secret": "secret-1",
        "scopes": "https://www.googleapis.com/auth/gmail.readonly",
    }


def test_session_credentials_reject_incomplete_mail_oauth_payload() -> None:
    resp = TestClient(_session_app(_Vault())).post(
        "/session/credentials",
        json={
            "session_id": "s1",
            "service": "gmail",
            "email": "alice@example.com",
            "refresh_token": "refresh-1",
        },
    )

    assert resp.status_code == 400
    assert "client_id/client_secret" in resp.json()["detail"]


def test_session_credentials_accept_mail_access_token_only() -> None:
    vault = _Vault()
    resp = TestClient(_session_app(vault)).post(
        "/session/credentials",
        json={
            "session_id": "s1",
            "service": "outlook",
            "email": "alice@example.com",
            "access_token": "access-1",
        },
    )

    assert resp.status_code == 201
    _, payload = vault.writes[0]
    assert payload == {
        "provider": "outlook",
        "access_token": "access-1",
        "email": "alice@example.com",
        "username": "alice@example.com",
    }


def test_jsonrpc_payload_parser_accepts_sse_data() -> None:
    response = httpx.Response(
        200,
        text='event: message\ndata: {"jsonrpc":"2.0","result":{"ok":true}}\n\n',
    )

    assert _jsonrpc_payload(response) == {"jsonrpc": "2.0", "result": {"ok": True}}


def test_safe_error_text_redacts_secret_material() -> None:
    text = _safe_error_text("upstream failed access_token=supersecretvalue12345")

    assert "supersecretvalue12345" not in text
    assert "[REDACTED_SECRET]" in text


@pytest.mark.asyncio
async def test_mail_list_tools_filters_non_read_only_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": "tools",
                    "result": {
                        "tools": [
                            {"name": "gmail_list_messages", "annotations": {"readOnlyHint": True}},
                            {"name": "gmail_send_email", "annotations": {"readOnlyHint": True}},
                            {"name": "gmail_modify_message", "annotations": {"readOnlyHint": False}},
                        ]
                    },
                },
            )

    import src.mcp_tool_dispatch as dispatch_module

    monkeypatch.setattr(dispatch_module.httpx, "AsyncClient", _FakeAsyncClient)
    dispatch = McpToolDispatch(
        mcp_base_url="http://atlassian.test",
        gmail_mcp_base_url="http://gmail.test",
        session_deps=SessionCredentialDeps(vault=_Vault()),
    )

    tools = await dispatch._list_routed_tools("gmail")

    assert tools == [{"name": "gmail_list_messages", "annotations": {"readOnlyHint": True}}]


@pytest.mark.asyncio
async def test_mail_invoke_forwards_credential_ref_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
            captured["headers"] = kwargs.get("headers")
            return httpx.Response(200, json={"jsonrpc": "2.0", "result": {"ok": True}})

    import src.mcp_tool_dispatch as dispatch_module

    monkeypatch.setattr(dispatch_module.httpx, "AsyncClient", _FakeAsyncClient)
    dispatch = McpToolDispatch(
        mcp_base_url="http://atlassian.test",
        gmail_mcp_base_url="http://gmail.test",
        session_deps=SessionCredentialDeps(vault=_Vault()),
    )
    token = bind_credential_refs({"gmail": "vault:atlassian/_user_session/s1/gmail"})
    try:
        result = await dispatch.invoke(
            {"tool_name": "gmail_list_messages", "arguments": {"limit": 1}}
        )
    finally:
        reset_credential_refs(token)

    assert result == {"jsonrpc": "2.0", "result": {"ok": True}}
    assert captured["headers"]["X-Credential-Ref-Gmail"] == (
        "vault:atlassian/_user_session/s1/gmail"
    )
    assert captured["headers"]["X-Credential-Ref-Mail"] == (
        "vault:atlassian/_user_session/s1/gmail"
    )


def test_proxy_lists_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeDispatch:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def list_tools(self) -> list[dict[str, Any]]:
            return [{"name": "jira_search_issues"}]

    import src.mcp_proxy as proxy

    monkeypatch.setattr(proxy, "McpToolDispatch", _FakeDispatch)
    resp = TestClient(_app()).get("/api/mcp/tools")

    assert resp.status_code == 200
    assert resp.json() == {"tools": [{"name": "jira_search_issues"}]}


def test_proxy_calls_tool_with_credential_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    refs_seen: list[dict[str, str]] = []

    class _FakeDispatch:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def invoke(self, tool_call: Any) -> dict[str, Any]:
            import src.mcp_tool_dispatch as dispatch_module

            calls.append(tool_call)
            refs_seen.append(dict(dispatch_module._credential_refs.get()))
            return {"jsonrpc": "2.0", "result": {"ok": True}}

    import src.mcp_proxy as proxy

    monkeypatch.setattr(proxy, "McpToolDispatch", _FakeDispatch)
    resp = TestClient(_app()).post(
        "/api/mcp/tools/call",
        json={"tool_name": "jira_search_issues", "arguments": {"jql": "project=KAN"}},
        headers={"X-Credential-Ref-Jira": "vault:atlassian/_user_session/s/jira"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert calls == [{"tool_name": "jira_search_issues", "arguments": {"jql": "project=KAN"}}]
    assert refs_seen == [{"jira": "vault:atlassian/_user_session/s/jira"}]


def test_proxy_calls_mail_tool_with_credential_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    refs_seen: list[dict[str, str]] = []

    class _FakeDispatch:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def invoke(self, tool_call: Any) -> dict[str, Any]:
            import src.mcp_tool_dispatch as dispatch_module

            refs_seen.append(dict(dispatch_module._credential_refs.get()))
            return {"jsonrpc": "2.0", "result": {"ok": True}}

    import src.mcp_proxy as proxy

    monkeypatch.setattr(proxy, "McpToolDispatch", _FakeDispatch)
    resp = TestClient(_app()).post(
        "/api/mcp/tools/call",
        json={"tool_name": "gmail_list_messages", "arguments": {"limit": 1}},
        headers={"X-Credential-Ref-Gmail": "vault:atlassian/_user_session/s/gmail"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert refs_seen == [{"gmail": "vault:atlassian/_user_session/s/gmail"}]
