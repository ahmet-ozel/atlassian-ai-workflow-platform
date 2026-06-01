"""Unit tests for http_shared.auth_inject module.

Tests the with_atlassian_creds async context manager, CredentialResolutionError,
scope validation, header injection, and header restoration.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import httpx
import pytest

from http_shared.auth_inject import (
    CredentialResolutionError,
    ServiceLiteral,
    _HEADER_PREFIX,
    with_atlassian_creds,
)


# ---------------------------------------------------------------------------
# Fake credential resolver for testing
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FakeCredential:
    url: str
    username: str
    personal_token: str
    api_token: str = ""
    app_password: str = ""
    cloud_access_token: str = ""


class FakeCredentialResolver:
    """A minimal duck-typed credential resolver for testing."""

    def __init__(self, credentials: dict[tuple[str, str], FakeCredential]) -> None:
        self._credentials = credentials

    async def get(self, dept_id: str, service: str, *, scope: str = "org") -> FakeCredential:
        key = (dept_id, service)
        if key not in self._credentials:
            raise KeyError(f"No credential for {key}")
        return self._credentials[key]


# ---------------------------------------------------------------------------
# Tests: CredentialResolutionError
# ---------------------------------------------------------------------------


class TestCredentialResolutionError:
    def test_inherits_runtime_error(self) -> None:
        err = CredentialResolutionError("payment", "jira")
        assert isinstance(err, RuntimeError)

    def test_attributes(self) -> None:
        err = CredentialResolutionError("hr", "confluence", "vault 404")
        assert err.dept_id == "hr"
        assert err.service == "confluence"

    def test_str_contains_dept_and_service(self) -> None:
        err = CredentialResolutionError("legal", "bitbucket", "timeout")
        msg = str(err)
        assert "legal" in msg
        assert "bitbucket" in msg
        assert "timeout" in msg

    def test_default_cause(self) -> None:
        err = CredentialResolutionError("payment", "jira")
        assert "incomplete credential" in str(err)


# ---------------------------------------------------------------------------
# Tests: scope validation
# ---------------------------------------------------------------------------


class TestScopeValidation:
    @pytest.mark.asyncio
    async def test_user_scope_accepted(self) -> None:
        """uyumluluk R2.1: scope='user' is now accepted (Q7 per-user path)."""
        cred = FakeCredential(
            url="https://jira.example.com",
            username="alice@example.com",
            personal_token="user-token",
        )
        resolver = FakeCredentialResolver({("payment", "jira"): cred})
        client = httpx.AsyncClient()

        async with with_atlassian_creds(
            client,
            dept_id="payment",
            service="jira",
            credential_resolver=resolver,
            scope="user",
        ) as c:
            assert c is client
            # Headers were injected from the user-scope credential.
            assert c.headers["X-Atlassian-Jira-Url"] == cred.url
            assert c.headers["X-Atlassian-Jira-Username"] == cred.username
            assert c.headers["X-Atlassian-Jira-Personal-Token"] == cred.personal_token

        await client.aclose()

    @pytest.mark.asyncio
    async def test_org_scope_accepted(self) -> None:
        """uyumluluk R2.1: scope='org' is the new canonical worker bot scope."""
        cred = FakeCredential(
            url="https://jira.example.com",
            username="bot@example.com",
            personal_token="token123",
        )
        resolver = FakeCredentialResolver({("payment", "jira"): cred})
        client = httpx.AsyncClient()

        async with with_atlassian_creds(
            client,
            dept_id="payment",
            service="jira",
            credential_resolver=resolver,
            scope="org",
        ) as c:
            assert c is client

        await client.aclose()

    @pytest.mark.asyncio
    async def test_default_scope_is_org(self) -> None:
        """uyumluluk R2.1: default scope is 'org' (backward compatible with old 'bot')."""
        cred = FakeCredential(
            url="https://jira.example.com",
            username="bot@example.com",
            personal_token="token123",
        )
        resolver = FakeCredentialResolver({("payment", "jira"): cred})
        captured_scope: dict[str, str] = {}

        original_get = resolver.get

        async def spy_get(dept_id: str, service: str, *, scope: str = "org") -> FakeCredential:
            captured_scope["value"] = scope
            return await original_get(dept_id, service, scope=scope)

        resolver.get = spy_get  # type: ignore[assignment]
        client = httpx.AsyncClient()

        async with with_atlassian_creds(
            client,
            dept_id="payment",
            service="jira",
            credential_resolver=resolver,
        ):
            pass

        assert captured_scope["value"] == "org"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_bot_scope_emits_deprecation_warning_and_routes_to_org(self) -> None:
        """uyumluluk R2.1: scope='bot' is a deprecated alias for 'org'.

        It must emit DeprecationWarning and forward to the resolver as
        scope='org' so existing call sites keep working without changes.
        """
        cred = FakeCredential(
            url="https://jira.example.com",
            username="bot@example.com",
            personal_token="token123",
        )
        resolver = FakeCredentialResolver({("payment", "jira"): cred})
        captured_scope: dict[str, str] = {}

        original_get = resolver.get

        async def spy_get(dept_id: str, service: str, *, scope: str = "org") -> FakeCredential:
            captured_scope["value"] = scope
            return await original_get(dept_id, service, scope=scope)

        resolver.get = spy_get  # type: ignore[assignment]
        client = httpx.AsyncClient()

        with pytest.warns(DeprecationWarning, match="scope='bot' is deprecated"):
            async with with_atlassian_creds(
                client,
                dept_id="payment",
                service="jira",
                credential_resolver=resolver,
                scope="bot",  # type: ignore[arg-type]
            ) as c:
                assert c is client

        # The resolver must be called with the rerouted 'org' scope.
        assert captured_scope["value"] == "org"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_unknown_scope_raises_value_error(self) -> None:
        """Unknown scopes (after bot-alias resolution) raise ValueError."""
        client = httpx.AsyncClient()
        resolver = FakeCredentialResolver({})

        with pytest.raises(ValueError, match="scope must be one of"):
            async with with_atlassian_creds(
                client,
                dept_id="payment",
                service="jira",
                credential_resolver=resolver,
                scope="superuser",  # type: ignore[arg-type]
            ):
                pass  # pragma: no cover

        await client.aclose()


# ---------------------------------------------------------------------------
# Tests: credential injection
# ---------------------------------------------------------------------------


class TestCredentialInjection:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "service,expected_prefix",
        [
            ("jira", "X-Atlassian-Jira"),
            ("bitbucket", "X-Atlassian-Bitbucket"),
            ("confluence", "X-Atlassian-Confluence"),
        ],
    )
    async def test_injects_correct_headers(
        self, service: ServiceLiteral, expected_prefix: str
    ) -> None:
        cred = FakeCredential(
            url=f"https://{service}.example.com",
            username=f"bot-{service}@example.com",
            personal_token=f"pat-{service}-secret",
        )
        resolver = FakeCredentialResolver({("dept1", service): cred})
        client = httpx.AsyncClient()

        async with with_atlassian_creds(
            client,
            dept_id="dept1",
            service=service,
            credential_resolver=resolver,
        ) as c:
            assert c.headers[f"{expected_prefix}-Url"] == cred.url
            assert c.headers[f"{expected_prefix}-Username"] == cred.username
            assert c.headers[f"{expected_prefix}-Personal-Token"] == cred.personal_token

        await client.aclose()

    @pytest.mark.asyncio
    async def test_jira_cloud_personal_token_uses_api_token_header(self) -> None:
        cred = FakeCredential(
            url="https://acme.atlassian.net",
            username="bot@example.com",
            personal_token="atlassian-cloud-token",
        )
        resolver = FakeCredentialResolver({("dept1", "jira"): cred})
        client = httpx.AsyncClient()

        async with with_atlassian_creds(
            client,
            dept_id="dept1",
            service="jira",
            credential_resolver=resolver,
        ) as c:
            assert c.headers["X-Atlassian-Jira-Api-Token"] == cred.personal_token
            assert "X-Atlassian-Jira-Personal-Token" not in c.headers

        await client.aclose()

    @pytest.mark.asyncio
    async def test_confluence_cloud_personal_token_uses_api_token_header(self) -> None:
        cred = FakeCredential(
            url="https://acme.atlassian.net/wiki",
            username="bot@example.com",
            personal_token="atlassian-cloud-token",
        )
        resolver = FakeCredentialResolver({("dept1", "confluence"): cred})
        client = httpx.AsyncClient()

        async with with_atlassian_creds(
            client,
            dept_id="dept1",
            service="confluence",
            credential_resolver=resolver,
        ) as c:
            assert c.headers["X-Atlassian-Confluence-Api-Token"] == cred.personal_token
            assert "X-Atlassian-Confluence-Personal-Token" not in c.headers

        await client.aclose()

    @pytest.mark.asyncio
    async def test_bitbucket_cloud_personal_token_uses_app_password_header(self) -> None:
        cred = FakeCredential(
            url="https://bitbucket.org",
            username="bot@example.com",
            personal_token="bitbucket-app-password",
        )
        resolver = FakeCredentialResolver({("dept1", "bitbucket"): cred})
        client = httpx.AsyncClient()

        async with with_atlassian_creds(
            client,
            dept_id="dept1",
            service="bitbucket",
            credential_resolver=resolver,
        ) as c:
            assert c.headers["X-Atlassian-Bitbucket-App-Password"] == cred.personal_token
            assert "X-Atlassian-Bitbucket-Personal-Token" not in c.headers

        await client.aclose()

    @pytest.mark.asyncio
    async def test_incomplete_credential_raises_error(self) -> None:
        # Empty url
        cred = FakeCredential(url="", username="user", personal_token="token")
        resolver = FakeCredentialResolver({("payment", "jira"): cred})
        client = httpx.AsyncClient()

        with pytest.raises(CredentialResolutionError) as exc_info:
            async with with_atlassian_creds(
                client,
                dept_id="payment",
                service="jira",
                credential_resolver=resolver,
            ):
                pass  # pragma: no cover

        assert exc_info.value.dept_id == "payment"
        assert exc_info.value.service == "jira"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_empty_username_raises_error(self) -> None:
        cred = FakeCredential(url="https://jira.example.com", username="", personal_token="token")
        resolver = FakeCredentialResolver({("hr", "jira"): cred})
        client = httpx.AsyncClient()

        with pytest.raises(CredentialResolutionError):
            async with with_atlassian_creds(
                client,
                dept_id="hr",
                service="jira",
                credential_resolver=resolver,
            ):
                pass  # pragma: no cover

        await client.aclose()

    @pytest.mark.asyncio
    async def test_empty_token_raises_error(self) -> None:
        cred = FakeCredential(url="https://jira.example.com", username="user", personal_token="")
        resolver = FakeCredentialResolver({("hr", "jira"): cred})
        client = httpx.AsyncClient()

        with pytest.raises(CredentialResolutionError):
            async with with_atlassian_creds(
                client,
                dept_id="hr",
                service="jira",
                credential_resolver=resolver,
            ):
                pass  # pragma: no cover

        await client.aclose()


# ---------------------------------------------------------------------------
# Tests: header preservation and restoration
# ---------------------------------------------------------------------------


class TestHeaderPreservation:
    @pytest.mark.asyncio
    async def test_preserves_x_client_source(self) -> None:
        cred = FakeCredential(
            url="https://jira.example.com",
            username="bot@example.com",
            personal_token="token123",
        )
        resolver = FakeCredentialResolver({("payment", "jira"): cred})
        client = httpx.AsyncClient(headers={"X-Client-Source": "agent-runner-worker"})

        async with with_atlassian_creds(
            client,
            dept_id="payment",
            service="jira",
            credential_resolver=resolver,
        ) as c:
            # X-Client-Source must still be present during the block
            assert c.headers["X-Client-Source"] == "agent-runner-worker"
            # Credential headers are also present
            assert c.headers["X-Atlassian-Jira-Url"] == "https://jira.example.com"

        # After exit, X-Client-Source is still intact
        assert client.headers["X-Client-Source"] == "agent-runner-worker"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_restores_headers_on_exit(self) -> None:
        cred = FakeCredential(
            url="https://jira.example.com",
            username="bot@example.com",
            personal_token="token123",
        )
        resolver = FakeCredentialResolver({("payment", "jira"): cred})
        client = httpx.AsyncClient()

        # Before: no credential headers
        assert "X-Atlassian-Jira-Url" not in client.headers

        async with with_atlassian_creds(
            client,
            dept_id="payment",
            service="jira",
            credential_resolver=resolver,
        ) as c:
            assert c.headers["X-Atlassian-Jira-Url"] == "https://jira.example.com"

        # After: credential headers removed
        assert "X-Atlassian-Jira-Url" not in client.headers
        assert "X-Atlassian-Jira-Username" not in client.headers
        assert "X-Atlassian-Jira-Personal-Token" not in client.headers
        await client.aclose()

    @pytest.mark.asyncio
    async def test_restores_previous_credential_headers(self) -> None:
        """If credential headers existed before, they are restored to original values."""
        cred = FakeCredential(
            url="https://jira-new.example.com",
            username="new-bot@example.com",
            personal_token="new-token",
        )
        resolver = FakeCredentialResolver({("payment", "jira"): cred})
        client = httpx.AsyncClient(
            headers={
                "X-Atlassian-Jira-Url": "https://jira-old.example.com",
                "X-Atlassian-Jira-Username": "old-bot@example.com",
                "X-Atlassian-Jira-Personal-Token": "old-token",
            }
        )

        async with with_atlassian_creds(
            client,
            dept_id="payment",
            service="jira",
            credential_resolver=resolver,
        ) as c:
            assert c.headers["X-Atlassian-Jira-Url"] == "https://jira-new.example.com"

        # After: original values restored
        assert client.headers["X-Atlassian-Jira-Url"] == "https://jira-old.example.com"
        assert client.headers["X-Atlassian-Jira-Username"] == "old-bot@example.com"
        assert client.headers["X-Atlassian-Jira-Personal-Token"] == "old-token"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_restores_on_exception(self) -> None:
        """Headers are restored even if an exception occurs in the with-block."""
        cred = FakeCredential(
            url="https://jira.example.com",
            username="bot@example.com",
            personal_token="token123",
        )
        resolver = FakeCredentialResolver({("payment", "jira"): cred})
        client = httpx.AsyncClient(headers={"X-Client-Source": "test"})

        with pytest.raises(RuntimeError, match="boom"):
            async with with_atlassian_creds(
                client,
                dept_id="payment",
                service="jira",
                credential_resolver=resolver,
            ):
                raise RuntimeError("boom")

        # Headers restored despite exception
        assert "X-Atlassian-Jira-Url" not in client.headers
        assert client.headers["X-Client-Source"] == "test"
        await client.aclose()

    @pytest.mark.asyncio
    async def test_preserves_unrelated_headers(self) -> None:
        """Other headers (not credential-related) remain untouched."""
        cred = FakeCredential(
            url="https://confluence.example.com",
            username="bot@example.com",
            personal_token="token123",
        )
        resolver = FakeCredentialResolver({("payment", "confluence"): cred})
        client = httpx.AsyncClient(
            headers={
                "X-Client-Source": "agent-runner-worker",
                "Authorization": "Bearer xyz",
                "X-Custom-Header": "custom-value",
            }
        )

        async with with_atlassian_creds(
            client,
            dept_id="payment",
            service="confluence",
            credential_resolver=resolver,
        ) as c:
            assert c.headers["Authorization"] == "Bearer xyz"
            assert c.headers["X-Custom-Header"] == "custom-value"
            assert c.headers["X-Client-Source"] == "agent-runner-worker"

        # All unrelated headers still intact after exit
        assert client.headers["Authorization"] == "Bearer xyz"
        assert client.headers["X-Custom-Header"] == "custom-value"
        assert client.headers["X-Client-Source"] == "agent-runner-worker"
        await client.aclose()
