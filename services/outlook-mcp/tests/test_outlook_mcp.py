from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest


def _load_outlook_module():
    os.environ["MAIL_MCP_READ_ONLY"] = "true"
    os.environ["MAIL_MCP_PROVIDER"] = "outlook"
    path = Path(__file__).resolve().parents[1] / "src" / "main.py"
    spec = importlib.util.spec_from_file_location("outlook_mcp_main_for_tests", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Request:
    def __init__(self, payload: dict[str, Any] | BaseException) -> None:
        self.payload = payload

    async def json(self) -> dict[str, Any]:
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


def _response_json(response: Any) -> dict[str, Any]:
    return json.loads(response.body.decode("utf-8"))


def test_tools_are_read_only_and_expected() -> None:
    outlook = _load_outlook_module()

    assert {tool["name"] for tool in outlook.TOOLS} == {
        "outlook_list_messages",
        "outlook_list_unread_messages",
        "outlook_search_messages",
        "outlook_get_message",
        "outlook_get_latest_message",
        "outlook_list_drafts",
        "outlook_get_latest_draft",
    }
    assert all(tool["annotations"]["readOnlyHint"] is True for tool in outlook.TOOLS)
    assert not any(
        token in tool["name"]
        for tool in outlook.TOOLS
        for token in ("send", "delete", "archive", "move", "mark")
    )


def test_search_query_and_limit_are_normalized() -> None:
    outlook = _load_outlook_module()

    assert outlook._limit(None) == 10
    assert outlook._limit(0) == 1
    assert outlook._limit(999) == outlook.MAX_LIMIT
    assert (
        outlook._search_query(
            {"query": "project", "from": "a@example.com", "subject": "Invoice"}
        )
        == "project from:a@example.com subject:Invoice"
    )


def test_normalise_message_extracts_addresses_and_html_body() -> None:
    outlook = _load_outlook_module()

    result = outlook._normalise_message(
        {
            "id": "m1",
            "subject": "Subject A",
            "from": {"emailAddress": {"name": "Alice", "address": "alice@example.com"}},
            "toRecipients": [
                {"emailAddress": {"name": "Bob", "address": "bob@example.com"}}
            ],
            "receivedDateTime": "2026-06-17T10:00:00Z",
            "bodyPreview": "short",
            "body": {"contentType": "html", "content": "<p>Hello <b>Graph</b></p>"},
        },
        include_body=True,
    )

    assert result == {
        "id": "m1",
        "subject": "Subject A",
        "from": "Alice <alice@example.com>",
        "to": "Bob <bob@example.com>",
        "date": "2026-06-17T10:00:00Z",
        "snippet": "short",
        "body": "Hello Graph",
    }


def test_normalise_message_redacts_and_limits_sensitive_body() -> None:
    outlook = _load_outlook_module()
    secret = "refresh_token=supersecretvalue12345"

    result = outlook._normalise_message(
        {
            "id": "m1",
            "subject": secret,
            "from": {"emailAddress": {"address": "alice@example.com"}},
            "toRecipients": [],
            "receivedDateTime": "2026-06-17T10:00:00Z",
            "bodyPreview": "Bearer abcdefghijklmnopqrstuvwxyz",
            "body": {
                "contentType": "text",
                "content": f"{secret} " + ("x" * (outlook.BODY_CHAR_LIMIT + 50)),
            },
        },
        include_body=True,
    )

    assert "supersecretvalue12345" not in result["subject"]
    assert "supersecretvalue12345" not in result["body"]
    assert "[REDACTED_SECRET]" in result["body"]
    assert result["body"].endswith("...[truncated]")
    assert len(result["body"]) <= outlook.BODY_CHAR_LIMIT + len(" ...[truncated]")


def test_oauth_refresh_posts_scope_and_caches_token(monkeypatch) -> None:
    outlook = _load_outlook_module()
    calls: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def post(self, url: str, data: dict[str, Any]) -> httpx.Response:
            calls.append({"url": url, "data": data})
            return httpx.Response(200, json={"access_token": "access-1", "expires_in": 120})

    monkeypatch.setattr(outlook.httpx, "Client", _Client)
    outlook._TOKEN_CACHE.update({"access_token": "", "expires_at": 0.0})

    token = outlook._refresh_access_token("refresh-1", "client-1", "secret-1")

    assert token == "access-1"
    assert calls == [
        {
            "url": outlook.TOKEN_URL,
            "data": {
                "client_id": "client-1",
                "client_secret": "secret-1",
                "refresh_token": "refresh-1",
                "grant_type": "refresh_token",
                "scope": outlook.GRAPH_SCOPES,
            },
        }
    ]
    assert outlook._TOKEN_CACHE["access_token"] == "access-1"


def test_user_vault_credential_refreshes_without_polluting_env_cache(monkeypatch) -> None:
    outlook = _load_outlook_module()
    calls: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def get(self, url: str, headers: dict[str, str]) -> httpx.Response:
            calls.append({"method": "GET", "url": url, "headers": headers})
            return httpx.Response(
                200,
                json={
                    "data": {
                        "data": {
                            "refresh_token": "user-refresh",
                            "client_id": "user-client",
                            "client_secret": "user-secret",
                            "scopes": "offline_access Mail.Read User.Read",
                        }
                    }
                },
            )

        def post(self, url: str, data: dict[str, Any]) -> httpx.Response:
            calls.append({"method": "POST", "url": url, "data": data})
            return httpx.Response(200, json={"access_token": "user-access", "expires_in": 120})

    monkeypatch.setattr(outlook.httpx, "Client", _Client)
    outlook.VAULT_ADDR = "http://vault:8200"
    outlook.VAULT_TOKEN = "vault-token"
    outlook._TOKEN_CACHE.update({"access_token": "", "expires_at": 0.0})
    outlook._USER_TOKEN_CACHE.clear()

    token = outlook._access_token("vault:atlassian/_user_session/s1/outlook")

    assert token == "user-access"
    assert outlook._TOKEN_CACHE["access_token"] == ""
    assert outlook._USER_TOKEN_CACHE[
        "vault:atlassian/_user_session/s1/outlook"
    ]["access_token"] == "user-access"
    assert calls[0]["url"] == "http://vault:8200/v1/secret/data/atlassian/_user_session/s1/outlook"
    assert calls[1]["data"]["refresh_token"] == "user-refresh"
    assert calls[1]["data"]["scope"] == "offline_access Mail.Read User.Read"


def test_user_vault_credential_ref_is_limited_to_outlook_session_path() -> None:
    outlook = _load_outlook_module()

    assert (
        outlook._vault_relative_path("vault:atlassian/_user_session/s1/outlook")
        == "atlassian/_user_session/s1/outlook"
    )
    with pytest.raises(outlook.OutlookMcpError):
        outlook._vault_relative_path("vault:atlassian/_user_session/s1/gmail")
    with pytest.raises(outlook.OutlookMcpError):
        outlook._vault_relative_path("vault:notifications/smtp/credential")


def test_local_dev_backend_reads_same_credential_ref() -> None:
    outlook = _load_outlook_module()
    seen: dict[str, str] = {}

    class _Vault:
        def read(self, path: Any) -> dict[str, str]:
            seen["path"] = path.raw
            return {"refresh_token": "r", "client_id": "c", "client_secret": "s"}

    outlook.VAULT_BACKEND = "local-dev"
    outlook._VAULT_CLIENT = _Vault()

    credential = outlook._read_vault_credential(
        "vault:atlassian/_user_session/s1/outlook"
    )

    assert seen["path"] == "vault:atlassian/_user_session/s1/outlook"
    assert credential["refresh_token"] == "r"


def test_env_user_token_fallback_is_disabled_by_default(monkeypatch) -> None:
    outlook = _load_outlook_module()
    outlook.ALLOW_ENV_USER_TOKEN = False
    monkeypatch.setenv("MICROSOFT_ACCESS_TOKEN", "eyJ.local.dev.token")

    with pytest.raises(outlook.OutlookMcpError, match="credential ref is required"):
        outlook._access_token()


def test_env_user_token_fallback_can_be_enabled_for_local_dev(monkeypatch) -> None:
    outlook = _load_outlook_module()
    outlook.ALLOW_ENV_USER_TOKEN = True
    outlook._TOKEN_CACHE.update({"access_token": "", "expires_at": 0.0})
    monkeypatch.setenv("MICROSOFT_ACCESS_TOKEN", "eyJ.local.dev.token")

    assert outlook._access_token() == "eyJ.local.dev.token"


def test_jsonrpc_tools_list_and_unknown_tool() -> None:
    outlook = _load_outlook_module()

    tools_response = asyncio.run(
        outlook.mcp(_Request({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"}))
    )
    tools_payload = _response_json(tools_response)
    assert tools_payload["result"]["tools"][0]["name"] == "outlook_list_messages"

    error_response = asyncio.run(
        outlook.mcp(
            _Request(
                {
                    "jsonrpc": "2.0",
                    "id": "bad",
                    "method": "tools/call",
                    "params": {"name": "outlook_delete_message", "arguments": {}},
                }
            )
        )
    )
    error_payload = _response_json(error_response)
    assert error_payload["error"]["code"] == -32003
    assert "write tool blocked" in error_payload["error"]["message"]


def test_call_tool_preserves_read_only_result_contract(monkeypatch) -> None:
    outlook = _load_outlook_module()

    monkeypatch.setattr(
        outlook,
        "_list_messages",
        lambda **kwargs: [
            {
                "id": "m1",
                "subject": "Subject A",
                "from": "alice@example.com",
                "to": "",
                "date": "2026-06-17",
                "snippet": "short",
                "body": "",
            }
        ],
    )

    result = outlook._call_tool("outlook_list_messages", {"limit": 1})

    structured = result["structuredContent"]
    assert structured["provider"] == "outlook"
    assert structured["tool"] == "outlook_list_messages"
    assert structured["read_only"] is True
    assert structured["items"][0]["id"] == "m1"


def test_get_latest_message_resolves_id_before_detail(monkeypatch) -> None:
    outlook = _load_outlook_module()

    calls: list[tuple[str, str]] = []

    def fake_list(**kwargs):
        calls.append(("list", f"{kwargs['limit']}:{kwargs.get('inbox')}"))
        return [
            {"id": "m1", "subject": "Old", "date": "2026-06-21T10:00:00Z"},
            {"id": "m2", "subject": "Latest target", "date": "2026-06-23T10:00:00Z"},
            {"id": "m3", "subject": "Middle", "date": "2026-06-22T10:00:00Z"},
        ]

    def fake_get(message_id: str, **kwargs):
        calls.append(("get", message_id))
        return {
            "id": message_id,
            "subject": "Latest target",
            "from": "alice@example.com",
            "to": "bob@example.com",
            "date": "2026-06-23",
            "snippet": "short",
            "body": "Full body",
        }

    monkeypatch.setattr(outlook, "_list_messages", fake_list)
    monkeypatch.setattr(outlook, "_get_message", fake_get)

    result = outlook._call_tool("outlook_get_latest_message", {"offset": 1, "inbox": True})

    assert calls == [("list", "10:True"), ("get", "m2")]
    structured = result["structuredContent"]
    assert structured["items"][0]["body"] == "Full body"
    assert structured["ai_hints"]["requires_message_id_from_user"] is False
    assert "Subject: Latest target" in result["content"][0]["text"]


def test_get_message_without_id_falls_back_to_latest_message(monkeypatch) -> None:
    outlook = _load_outlook_module()

    calls: list[tuple[str, str]] = []

    def fake_list(**kwargs):
        calls.append(("list", f"{kwargs['limit']}:{kwargs.get('inbox')}"))
        return [
            {"id": "m1", "subject": "Old", "date": "2026-06-21T10:00:00Z"},
            {"id": "m2", "subject": "Latest target", "date": "2026-06-23T10:00:00Z"},
        ]

    def fake_get(message_id: str, **kwargs):
        calls.append(("get", message_id))
        return {
            "id": message_id,
            "subject": "Latest target",
            "from": "alice@example.com",
            "to": "bob@example.com",
            "date": "2026-06-23",
            "snippet": "short",
            "body": "Full body",
        }

    monkeypatch.setattr(outlook, "_list_messages", fake_list)
    monkeypatch.setattr(outlook, "_get_message", fake_get)

    result = outlook._call_tool("outlook_get_message", {"include_body": True})

    assert calls == [("list", "10:False"), ("get", "m2")]
    structured = result["structuredContent"]
    assert structured["tool"] == "outlook_get_latest_message"
    assert structured["items"][0]["id"] == "m2"
    assert structured["ai_hints"]["requires_message_id_from_user"] is False


def test_get_latest_draft_resolves_message_before_detail(monkeypatch) -> None:
    outlook = _load_outlook_module()

    calls: list[tuple[str, str]] = []

    def fake_list(**kwargs):
        calls.append(("list", f"{kwargs['limit']}:{kwargs.get('folder')}"))
        return [
            {"id": "m1", "subject": "Old", "date": "2026-06-21T10:00:00Z"},
            {"id": "m2", "subject": "Draft target", "date": "2026-06-23T10:00:00Z"},
        ]

    def fake_get(message_id: str, **kwargs):
        calls.append(("get", message_id))
        return {
            "id": message_id,
            "subject": "Draft target",
            "from": "alice@example.com",
            "to": "bob@example.com",
            "date": "2026-06-23",
            "snippet": "draft",
            "body": "Draft body",
        }

    monkeypatch.setattr(outlook, "_list_messages", fake_list)
    monkeypatch.setattr(outlook, "_get_message", fake_get)

    result = outlook._call_tool("outlook_get_latest_draft", {"offset": 1})

    assert calls == [("list", "10:drafts"), ("get", "m2")]
    assert result["structuredContent"]["items"][0]["is_draft"] is True
