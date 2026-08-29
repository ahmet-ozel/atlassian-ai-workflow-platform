from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest


STREAMLIT_ROOT = Path(__file__).resolve().parents[1]
if str(STREAMLIT_ROOT) not in sys.path:
    sys.path.insert(0, str(STREAMLIT_ROOT))

import mail_mcp  # noqa: E402
from mail_mcp import MailMcpError, MailMcpWriteBlockedError  # noqa: E402


def _source(relative: str) -> str:
    return (STREAMLIT_ROOT / relative).read_text(encoding="utf-8-sig")


def test_mail_chat_registered_in_user_navigation() -> None:
    app_source = _source("app.py")

    assert '"Mail Chat", "pages/4_mail_chat.py"' in app_source
    assert "pages/3_explorer.py" not in app_source
    assert "pages/7_mcp_inspector.py" not in app_source


def test_mail_chat_history_is_isolated_from_atlassian_chat() -> None:
    page_source = _source("pages/4_mail_chat.py")

    assert "mail_chat_history" in page_source
    assert "mail_chat_input" in page_source
    assert '"chat_history"' not in page_source
    assert "'chat_history'" not in page_source
    assert '"atlassian_chat_history"' not in page_source
    assert "'atlassian_chat_history'" not in page_source


def test_mail_chat_prefers_assistant_service_mail_mode_with_mcp_fallback() -> None:
    page_source = _source("pages/4_mail_chat.py")

    assert "_assistant_mail_answer" in page_source
    assert "_asks_for_message_id" in page_source
    assert "_direct_mail_mcp_answer" in page_source
    assert "assistant-service asked for message id" in page_source
    assert 'mode="mail"' in page_source
    assert "plan_and_call_mail_mcp" in page_source
    assert "ask_mail_llm" in page_source


def test_mail_chat_rehydrates_mail_credential_refs_from_session_id() -> None:
    page_source = _source("pages/4_mail_chat.py")

    assert "vault:atlassian/_user_session/{session_id}/{provider}" in page_source
    assert "provider in bound" not in page_source
    assert 'st.session_state[f"credential_{provider}"] = SimpleNamespace' in page_source


def test_mail_chat_does_not_persist_mail_secrets_in_streamlit_state() -> None:
    checked_files = [
        "pages/4_mail_chat.py",
        "mail_auth.py",
        "mail_mcp.py",
        "mail_planner.py",
        "mail_llm.py",
    ]
    suspicious_session_secret = re.compile(
        r"st\.session_state[^\n]*(access_token|refresh_token|gmail_token|outlook_token|client_secret)",
        re.IGNORECASE,
    )

    for relative in checked_files:
        body = _source(relative)
        assert not suspicious_session_secret.search(body), (
            f"{relative} appears to store mail OAuth material in Streamlit session_state"
        )

    assert "Authorization" not in mail_mcp._headers()
    assert all(not key.lower().startswith("x-credential-ref") for key in mail_mcp._headers())


@pytest.mark.parametrize(
    "tool",
    [
        "gmail_send_email",
        "gmail_delete_message",
        "outlook_archive_message",
        "outlook_move_message",
        "gmail_mark_as_read",
    ],
)
def test_mail_read_only_guard_blocks_write_tool_names(tool: str) -> None:
    with pytest.raises(MailMcpWriteBlockedError):
        mail_mcp._ensure_read_only_tool(tool)


def test_mail_read_only_guard_honors_mcp_annotations() -> None:
    assert mail_mcp._is_read_only_tool(
        {"name": "gmail_search_messages", "annotations": {"readOnlyHint": True}}
    )
    assert not mail_mcp._is_read_only_tool(
        {"name": "gmail_search_messages", "annotations": {"readOnlyHint": False}}
    )
    assert not mail_mcp._is_read_only_tool(
        {"name": "outlook_get_message", "annotations": {"read_only": False}}
    )


def test_mail_mcp_parser_accepts_plain_json_result() -> None:
    result = mail_mcp._parse_mcp_response(
        '{"jsonrpc":"2.0","id":"tools","result":{"tools":[{"name":"gmail_list_messages"}]}}'
    )

    assert result == {"tools": [{"name": "gmail_list_messages"}]}


def test_mail_mcp_parser_raises_on_jsonrpc_error() -> None:
    with pytest.raises(MailMcpError, match="erisimi reddetti|erisimi"):
        mail_mcp._parse_mcp_response(
            '{"jsonrpc":"2.0","id":"x","error":{"code":401,"message":"invalid_token"}}'
        )


@pytest.mark.parametrize(
    ("raw_error", "expected"),
    [
        ("Gmail OAuth is not configured", "henuz baglanmamis"),
        ("invalid_grant token expired", "suresi dolmus"),
        ("HTTP 429 rate limit", "rate limit"),
        ("HTTP 403 forbidden", "erisimi reddetti"),
        ("request failed timeout", "ulasilamiyor"),
        ("write tool blocked in read-only mode", "read-only"),
    ],
)
def test_mail_mcp_errors_are_user_friendly(raw_error: str, expected: str) -> None:
    assert expected in mail_mcp.user_friendly_mail_error(raw_error)
