from __future__ import annotations

import pytest

from mail_auth import (
    assert_streamlit_oauth_not_enabled,
    mail_auth_status,
    mail_auth_statuses,
)


def test_mail_auth_statuses_keep_oauth_in_mcp_services() -> None:
    statuses = mail_auth_statuses()

    assert [status.provider for status in statuses] == ["gmail", "outlook"]
    assert all(status.owner == "mcp-service" for status in statuses)
    assert all(status.streamlit_handles_tokens is False for status in statuses)


def test_gmail_oauth_env_contract() -> None:
    status = mail_auth_status("gmail")

    assert status.required_env_keys == (
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REDIRECT_URI",
    )


def test_outlook_oauth_env_contract() -> None:
    status = mail_auth_status("outlook")

    assert status.required_env_keys == (
        "MICROSOFT_TENANT_ID",
        "MICROSOFT_CLIENT_ID",
        "MICROSOFT_CLIENT_SECRET",
        "MICROSOFT_REDIRECT_URI",
    )


def test_streamlit_oauth_flow_is_explicitly_not_enabled() -> None:
    with pytest.raises(NotImplementedError, match="not implemented"):
        assert_streamlit_oauth_not_enabled("gmail")
