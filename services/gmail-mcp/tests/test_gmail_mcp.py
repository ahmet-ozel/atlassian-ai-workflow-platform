from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest


def _load_gmail_module():
    os.environ["MAIL_MCP_READ_ONLY"] = "true"
    os.environ["MAIL_MCP_PROVIDER"] = "gmail"
    path = Path(__file__).resolve().parents[1] / "src" / "main.py"
    spec = importlib.util.spec_from_file_location("gmail_mcp_main_for_tests", path)
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
    gmail = _load_gmail_module()

    assert {tool["name"] for tool in gmail.TOOLS} == {
        "gmail_list_messages",
        "gmail_list_unread_messages",
        "gmail_search_messages",
        "gmail_get_message",
        "gmail_get_latest_message",
        "gmail_list_drafts",
        "gmail_get_latest_draft",
    }
    assert all(tool["annotations"]["readOnlyHint"] is True for tool in gmail.TOOLS)
    assert not any(
        token in tool["name"]
        for tool in gmail.TOOLS
        for token in ("send", "delete", "archive", "move", "mark")
    )


def test_search_query_and_limit_are_normalized() -> None:
    gmail = _load_gmail_module()

    assert gmail._limit(None) == 10
    assert gmail._limit(0) == 1
    assert gmail._limit(999) == gmail.MAX_LIMIT
    assert (
        gmail._search_query(
            {"query": "has:attachment", "from": "a@example.com", "subject": "Invoice"}
        )
        == "has:attachment from:a@example.com subject:Invoice"
    )


def test_normalise_message_extracts_headers_and_plain_body() -> None:
    gmail = _load_gmail_module()
    body = base64.urlsafe_b64encode(b"Hello from Gmail").decode("ascii").rstrip("=")

    result = gmail._normalise_message(
        {
            "id": "m1",
            "snippet": "short",
            "internalDate": "1782144000000",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Subject A"},
                    {"name": "From", "value": "alice@example.com"},
                    {"name": "To", "value": "bob@example.com"},
                    {"name": "Date", "value": "2026-06-17"},
                ],
                "mimeType": "text/plain",
                "body": {"data": body},
            },
        },
        include_body=True,
    )

    assert result == {
        "id": "m1",
        "subject": "Subject A",
        "from": "alice@example.com",
        "to": "bob@example.com",
        "date": "2026-06-17",
        "internal_date": "1782144000000",
        "snippet": "short",
        "body": "Hello from Gmail",
    }


def test_normalise_message_redacts_and_limits_sensitive_body() -> None:
    gmail = _load_gmail_module()
    secret = "access_token=supersecretvalue12345"
    body_text = f"{secret} " + ("x" * (gmail.BODY_CHAR_LIMIT + 50))
    body = base64.urlsafe_b64encode(body_text.encode("utf-8")).decode("ascii").rstrip("=")

    result = gmail._normalise_message(
        {
            "id": "m1",
            "snippet": "Bearer abcdefghijklmnopqrstuvwxyz",
            "payload": {
                "headers": [{"name": "Subject", "value": secret}],
                "mimeType": "text/plain",
                "body": {"data": body},
            },
        },
        include_body=True,
    )

    assert "supersecretvalue12345" not in result["subject"]
    assert "supersecretvalue12345" not in result["body"]
    assert "[REDACTED_SECRET]" in result["body"]
    assert result["body"].endswith("...[truncated]")
    assert len(result["body"]) <= gmail.BODY_CHAR_LIMIT + len(" ...[truncated]")


def test_oauth_refresh_posts_refresh_token_and_caches_token(monkeypatch) -> None:
    gmail = _load_gmail_module()
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

    monkeypatch.setattr(gmail.httpx, "Client", _Client)
    gmail._TOKEN_CACHE.update({"access_token": "", "expires_at": 0.0})

    token = gmail._refresh_access_token("refresh-1", "client-1", "secret-1")

    assert token == "access-1"
    assert calls == [
        {
            "url": gmail.TOKEN_URL,
            "data": {
                "client_id": "client-1",
                "client_secret": "secret-1",
                "refresh_token": "refresh-1",
                "grant_type": "refresh_token",
            },
        }
    ]
    assert gmail._TOKEN_CACHE["access_token"] == "access-1"


def test_user_vault_credential_refreshes_without_polluting_env_cache(monkeypatch) -> None:
    gmail = _load_gmail_module()
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
                        }
                    }
                },
            )

        def post(self, url: str, data: dict[str, Any]) -> httpx.Response:
            calls.append({"method": "POST", "url": url, "data": data})
            return httpx.Response(200, json={"access_token": "user-access", "expires_in": 120})

    monkeypatch.setattr(gmail.httpx, "Client", _Client)
    gmail.VAULT_ADDR = "http://vault:8200"
    gmail.VAULT_TOKEN = "vault-token"
    gmail._TOKEN_CACHE.update({"access_token": "", "expires_at": 0.0})
    gmail._USER_TOKEN_CACHE.clear()

    token = gmail._access_token("vault:atlassian/_user_session/s1/gmail")

    assert token == "user-access"
    assert gmail._TOKEN_CACHE["access_token"] == ""
    assert gmail._USER_TOKEN_CACHE["vault:atlassian/_user_session/s1/gmail"]["access_token"] == "user-access"
    assert calls[0]["url"] == "http://vault:8200/v1/secret/data/atlassian/_user_session/s1/gmail"
    assert calls[1]["data"]["refresh_token"] == "user-refresh"


def test_user_vault_credential_ref_is_limited_to_gmail_session_path() -> None:
    gmail = _load_gmail_module()

    assert (
        gmail._vault_relative_path("vault:atlassian/_user_session/s1/gmail")
        == "atlassian/_user_session/s1/gmail"
    )
    with pytest.raises(gmail.GmailMcpError):
        gmail._vault_relative_path("vault:atlassian/_user_session/s1/outlook")
    with pytest.raises(gmail.GmailMcpError):
        gmail._vault_relative_path("vault:notifications/smtp/credential")


def test_local_dev_backend_reads_same_credential_ref() -> None:
    gmail = _load_gmail_module()
    seen: dict[str, str] = {}

    class _Vault:
        def read(self, path: Any) -> dict[str, str]:
            seen["path"] = path.raw
            return {"refresh_token": "r", "client_id": "c", "client_secret": "s"}

    gmail.VAULT_BACKEND = "local-dev"
    gmail._VAULT_CLIENT = _Vault()

    credential = gmail._read_vault_credential("vault:atlassian/_user_session/s1/gmail")

    assert seen["path"] == "vault:atlassian/_user_session/s1/gmail"
    assert credential["refresh_token"] == "r"


def test_env_user_token_fallback_is_disabled_by_default(monkeypatch) -> None:
    gmail = _load_gmail_module()
    gmail.ALLOW_ENV_USER_TOKEN = False
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "ya29.localdevtoken123456789")

    with pytest.raises(gmail.GmailMcpError, match="credential ref is required"):
        gmail._access_token()


def test_env_user_token_fallback_can_be_enabled_for_local_dev(monkeypatch) -> None:
    gmail = _load_gmail_module()
    gmail.ALLOW_ENV_USER_TOKEN = True
    gmail._TOKEN_CACHE.update({"access_token": "", "expires_at": 0.0})
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "ya29.localdevtoken123456789")

    assert gmail._access_token() == "ya29.localdevtoken123456789"


def test_jsonrpc_tools_list_and_unknown_tool() -> None:
    gmail = _load_gmail_module()

    tools_response = asyncio.run(
        gmail.mcp(_Request({"jsonrpc": "2.0", "id": "tools", "method": "tools/list"}))
    )
    tools_payload = _response_json(tools_response)
    assert tools_payload["result"]["tools"][0]["name"] == "gmail_list_messages"

    error_response = asyncio.run(
        gmail.mcp(
            _Request(
                {
                    "jsonrpc": "2.0",
                    "id": "bad",
                    "method": "tools/call",
                    "params": {"name": "gmail_send_email", "arguments": {}},
                }
            )
        )
    )
    error_payload = _response_json(error_response)
    assert error_payload["error"]["code"] == -32003
    assert "write tool blocked" in error_payload["error"]["message"]


def test_call_tool_preserves_read_only_result_contract(monkeypatch) -> None:
    gmail = _load_gmail_module()

    monkeypatch.setattr(
        gmail,
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

    result = gmail._call_tool("gmail_list_messages", {"limit": 1})

    structured = result["structuredContent"]
    assert structured["provider"] == "gmail"
    assert structured["tool"] == "gmail_list_messages"
    assert structured["read_only"] is True
    assert structured["items"][0]["id"] == "m1"


def test_get_latest_message_resolves_id_before_detail(monkeypatch) -> None:
    gmail = _load_gmail_module()

    calls: list[tuple[str, str]] = []

    def fake_list(**kwargs):
        calls.append(("list", f"{kwargs['limit']}:{kwargs.get('label_ids')}"))
        return [
            {"id": "m1", "subject": "Old", "internal_date": "1000"},
            {"id": "m2", "subject": "Latest target", "internal_date": "3000"},
            {"id": "m3", "subject": "Middle", "internal_date": "2000"},
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

    monkeypatch.setattr(gmail, "_list_messages", fake_list)
    monkeypatch.setattr(gmail, "_get_message", fake_get)

    result = gmail._call_tool("gmail_get_latest_message", {"offset": 1, "inbox": True})

    assert calls == [("list", "10:['INBOX']"), ("get", "m2")]
    structured = result["structuredContent"]
    assert structured["items"][0]["body"] == "Full body"
    assert structured["ai_hints"]["requires_message_id_from_user"] is False
    assert "Subject: Latest target" in result["content"][0]["text"]


def test_get_message_without_id_falls_back_to_latest_message(monkeypatch) -> None:
    gmail = _load_gmail_module()

    calls: list[tuple[str, str]] = []

    def fake_list(**kwargs):
        calls.append(("list", f"{kwargs['limit']}:{kwargs.get('label_ids')}"))
        return [
            {"id": "m1", "subject": "Old", "internal_date": "1000"},
            {"id": "m2", "subject": "Latest target", "internal_date": "3000"},
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

    monkeypatch.setattr(gmail, "_list_messages", fake_list)
    monkeypatch.setattr(gmail, "_get_message", fake_get)

    result = gmail._call_tool("gmail_get_message", {"include_body": True})

    assert calls == [("list", "10:None"), ("get", "m2")]
    structured = result["structuredContent"]
    assert structured["tool"] == "gmail_get_latest_message"
    assert structured["items"][0]["id"] == "m2"
    assert structured["ai_hints"]["requires_message_id_from_user"] is False


def test_get_latest_draft_resolves_draft_before_detail(monkeypatch) -> None:
    gmail = _load_gmail_module()

    calls: list[tuple[str, str]] = []

    def fake_list(**kwargs):
        calls.append(("list_drafts", str(kwargs["limit"])))
        return [
            {"draft_id": "d1", "id": "m1", "internal_date": "1000"},
            {"draft_id": "d2", "id": "m2", "internal_date": "3000"},
        ]

    def fake_get(draft_id: str, **kwargs):
        calls.append(("get_draft", draft_id))
        return {
            "draft_id": draft_id,
            "id": "m2",
            "subject": "Draft subject",
            "from": "alice@example.com",
            "to": "bob@example.com",
            "date": "2026-06-23",
            "snippet": "draft",
            "body": "Draft body",
            "is_draft": True,
        }

    monkeypatch.setattr(gmail, "_list_drafts", fake_list)
    monkeypatch.setattr(gmail, "_get_draft", fake_get)

    result = gmail._call_tool("gmail_get_latest_draft", {"offset": 1})

    assert calls == [("list_drafts", "10"), ("get_draft", "d2")]
    assert result["structuredContent"]["items"][0]["is_draft"] is True
