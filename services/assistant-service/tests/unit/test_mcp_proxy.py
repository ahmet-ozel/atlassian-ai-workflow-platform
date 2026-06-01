"""Tests for assistant-service MCP credential-ref proxy."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
    _mcp_headers,
    _with_bitbucket_workspace,
    bind_credential_refs,
    reset_credential_refs,
)
from src.session_credentials import SessionCredentialDeps  # noqa: E402


class _Vault:
    def read(self, path: Any) -> dict[str, str]:
        return {"url": "https://example.atlassian.net", "username": "u@example.com", "personal_token": "tok"}


def _app() -> FastAPI:
    from src.mcp_proxy import router

    app = FastAPI()
    app.include_router(router)
    app.state.session_creds = SessionCredentialDeps(vault=_Vault())
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
            raw = str(path)
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

    class _FakeDispatch:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def invoke(self, tool_call: Any) -> dict[str, Any]:
            calls.append(tool_call)
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
