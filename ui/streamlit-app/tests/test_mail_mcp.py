from __future__ import annotations

import json

import pytest

import mail_mcp
from mail_mcp import (
    MailMcpWriteBlockedError,
    list_mail_tools,
    mail_mcp_call,
    mail_mcp_call_any,
)


def test_list_mail_tools_filters_write_tools(monkeypatch) -> None:
    monkeypatch.setattr(
        mail_mcp,
        "_mail_mcp_jsonrpc",
        lambda provider, method, params=None: {
            "tools": [
                {"name": "gmail_search_messages"},
                {"name": "gmail_send_email"},
                {"name": "outlook_get_message"},
                {"name": "outlook_delete_message"},
            ]
        },
    )

    tools = list_mail_tools("gmail")

    assert [tool["name"] for tool in tools] == [
        "gmail_search_messages",
        "outlook_get_message",
    ]


def test_mail_mcp_call_blocks_write_tool_before_http(monkeypatch) -> None:
    called = {"value": False}

    def fail_if_called(provider, method, params=None):
        del provider, method, params
        called["value"] = True
        return {}

    monkeypatch.setattr(mail_mcp, "_mail_mcp_jsonrpc", fail_if_called)

    with pytest.raises(MailMcpWriteBlockedError):
        mail_mcp_call("gmail", "gmail_send_email", {"to": "a@example.com"})

    assert called["value"] is False


def test_mail_mcp_call_any_returns_first_success(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_call(provider, tool_name, args=None):
        del args
        calls.append((provider, tool_name))
        if provider == "gmail":
            raise mail_mcp.MailMcpError("down")
        return {"ok": True}

    monkeypatch.setattr(mail_mcp, "mail_mcp_call", fake_call)

    provider, tool_name, result = mail_mcp_call_any(
        [
            ("gmail", "gmail_search_messages", {"q": "x"}),
            ("outlook", "outlook_search_messages", {"q": "x"}),
        ]
    )

    assert provider == "outlook"
    assert tool_name == "outlook_search_messages"
    assert result == {"ok": True}
    assert calls == [
        ("gmail", "gmail_search_messages"),
        ("outlook", "outlook_search_messages"),
    ]


def test_parse_sse_jsonrpc_response() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": "tools",
        "result": {"tools": [{"name": "gmail_list_messages"}]},
    }

    assert mail_mcp._parse_mcp_response(f"data: {json.dumps(payload)}\n\n") == {
        "tools": [{"name": "gmail_list_messages"}]
    }
