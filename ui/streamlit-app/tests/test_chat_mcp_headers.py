from __future__ import annotations

from types import SimpleNamespace

from chat_mcp import _mcp_headers


def test_bitbucket_workspace_token_uses_personal_token_header() -> None:
    credential = SimpleNamespace(
        service="bitbucket",
        url="https://bitbucket.org",
        email="user@example.com",
        api_token="ATCTT_workspace_access_token",
        deployment="cloud",
    )

    headers = _mcp_headers(lambda service: credential if service == "bitbucket" else None)

    assert headers["X-Atlassian-Bitbucket-Personal-Token"] == credential.api_token
    assert "X-Atlassian-Bitbucket-Username" not in headers
    assert "X-Atlassian-Bitbucket-App-Password" not in headers


def test_bitbucket_account_token_uses_basic_headers() -> None:
    credential = SimpleNamespace(
        service="bitbucket",
        url="https://bitbucket.org",
        email="user@example.com",
        api_token="ATATT_account_api_token",
        deployment="cloud",
    )

    headers = _mcp_headers(lambda service: credential if service == "bitbucket" else None)

    assert headers["X-Atlassian-Bitbucket-Username"] == credential.email
    assert headers["X-Atlassian-Bitbucket-App-Password"] == credential.api_token
    assert headers["X-Atlassian-Bitbucket-Api-Token"] == credential.api_token
    assert "X-Atlassian-Bitbucket-Personal-Token" not in headers
