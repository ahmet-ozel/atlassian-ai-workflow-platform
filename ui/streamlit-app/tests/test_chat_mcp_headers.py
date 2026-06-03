from __future__ import annotations

from types import SimpleNamespace

import chat_mcp
from chat_mcp import _mcp_headers, _post_llm_with_retry


def test_bitbucket_cloud_token_uses_basic_headers_even_for_atctt_prefix() -> None:
    # On a Cloud URL the credential is always sent as Basic auth, regardless of
    # token prefix. A workspace access token (ATCTT...) must NOT be promoted to a
    # Personal-Token/Bearer header: the MCP truth-table discards that header on a
    # Cloud URL and returns 401.
    credential = SimpleNamespace(
        service="bitbucket",
        url="https://bitbucket.org",
        email="user@example.com",
        api_token="ATCTT_workspace_access_token",
        deployment="cloud",
    )

    headers = _mcp_headers(lambda service: credential if service == "bitbucket" else None)

    assert headers["X-Atlassian-Bitbucket-Username"] == credential.email
    assert headers["X-Atlassian-Bitbucket-App-Password"] == credential.api_token
    assert headers["X-Atlassian-Bitbucket-Api-Token"] == credential.api_token
    assert "X-Atlassian-Bitbucket-Personal-Token" not in headers


def test_bitbucket_server_token_uses_personal_token_header() -> None:
    credential = SimpleNamespace(
        service="bitbucket",
        url="https://bitbucket.example.com",
        email="user@example.com",
        api_token="server_personal_access_token",
        deployment="server",
    )

    headers = _mcp_headers(lambda service: credential if service == "bitbucket" else None)

    assert headers["X-Atlassian-Bitbucket-Personal-Token"] == credential.api_token
    assert "X-Atlassian-Bitbucket-Username" not in headers
    assert "X-Atlassian-Bitbucket-App-Password" not in headers
    assert "X-Atlassian-Bitbucket-Api-Token" not in headers


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


def test_llm_post_retries_transient_transport_error(monkeypatch) -> None:
    attempts = {"count": 0}

    class Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, timeout: float) -> None:
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, url, headers, json):
            del url, headers, json
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise chat_mcp.httpx.ConnectError("temporary eof")
            return Response()

    monkeypatch.setattr(chat_mcp.httpx, "Client", Client)
    monkeypatch.setattr(chat_mcp.time, "sleep", lambda seconds: None)

    response = _post_llm_with_retry(
        "https://example.invalid",
        headers={"Authorization": "Bearer test"},
        payload={"model": "test"},
    )

    assert response.status_code == 200
    assert attempts["count"] == 2
