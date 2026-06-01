"""Unit tests for the Bitbucket per-request auth header parser.

Validates the auth truth table rows implemented in
``mcp_atlassian.utils.environment._detect_bitbucket_auth_from_headers`` —
the per-request header rows A, B, C, D, and K from the
``bitbucket-cloud-dc-parity`` design.

Each test asserts the resolved :class:`BitbucketHeaderAuth` fields —
primarily ``auth_type`` and the outbound ``authorization`` header value —
that the dependency layer will forward to the underlying
``atlassian.Bitbucket`` session.

Requirements covered: 3.6, 3.7, 3.8, 3.9, 17.1, 17.2, 17.3, 17.4, 17.5.
"""

from __future__ import annotations

import base64

import pytest

from mcp_atlassian.utils.environment import (
    BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER,
    BITBUCKET_CLOUD_APP_PASSWORD_HEADER,
    BITBUCKET_CLOUD_USERNAME_HEADER,
    BITBUCKET_DC_PAT_HEADER,
    BITBUCKET_URL_HEADER,
    BitbucketHeaderAuth,
    _detect_bitbucket_auth_from_headers,
)


DC_URL = "https://bitbucket.example.corp"
CLOUD_URL = "https://api.bitbucket.org"
CLOUD_BITBUCKET_ORG_URL = "https://bitbucket.org/my-workspace"


def _basic(u: str, p: str) -> str:
    """Compute the Basic-auth ``Authorization`` header for ``u:p``."""
    return "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode("ascii")


# ---------------------------------------------------------------------------
# Empty / None input
# ---------------------------------------------------------------------------


class TestNoHeaders:
    """No per-request headers means no resolved auth and no URL override."""

    def test_none_headers_returns_empty_auth(self) -> None:
        """``None`` is accepted and treated like an empty dict."""
        result = _detect_bitbucket_auth_from_headers(None)

        assert isinstance(result, BitbucketHeaderAuth)
        assert result.url is None
        assert result.is_cloud is False
        assert result.auth_type is None
        assert result.authorization is None
        assert result.unauthorized is False

    def test_empty_headers_returns_empty_auth(self) -> None:
        """An empty dict resolves to no auth, no URL override."""
        result = _detect_bitbucket_auth_from_headers({})

        assert result.url is None
        assert result.is_cloud is False
        assert result.auth_type is None
        assert result.authorization is None
        assert result.unauthorized is False


# ---------------------------------------------------------------------------
# Row A — DCHost URL + DC PAT header
# ---------------------------------------------------------------------------


class TestRowA:
    """Row A: DC URL + DC PAT → ``auth_type="pat"``, ``Bearer <pat>``."""

    def test_dc_url_plus_dc_pat_resolves_to_pat(self) -> None:
        """The DC PAT is forwarded on the `Authorization` header."""
        headers = {
            BITBUCKET_URL_HEADER: DC_URL,
            BITBUCKET_DC_PAT_HEADER: "dc-pat-abc123",
        }

        result = _detect_bitbucket_auth_from_headers(headers)

        assert result.url == DC_URL
        assert result.is_cloud is False
        assert result.auth_type == "pat"
        assert result.authorization == "Bearer dc-pat-abc123"
        assert result.personal_token == "dc-pat-abc123"
        assert result.unauthorized is False

    def test_dc_pat_without_url_header_still_resolves(self) -> None:
        """Row A (Req 17.1): DC PAT without a URL header still resolves.

        The global DC URL is used by the dependency layer when the per-
        request URL header is absent.
        """
        headers = {BITBUCKET_DC_PAT_HEADER: "dc-pat-xyz"}

        result = _detect_bitbucket_auth_from_headers(headers)

        assert result.url is None
        assert result.is_cloud is False
        assert result.auth_type == "pat"
        assert result.authorization == "Bearer dc-pat-xyz"
        assert result.personal_token == "dc-pat-xyz"


# ---------------------------------------------------------------------------
# Row B — CloudHost URL + Cloud Access Token header
# ---------------------------------------------------------------------------


class TestRowB:
    """Row B: Cloud URL + Cloud Access Token → ``auth_type="cloud_bearer"``."""

    def test_cloud_url_plus_cloud_bearer_resolves_to_cloud_bearer(self) -> None:
        """The Cloud OAuth2 bearer token is forwarded on ``Authorization``."""
        headers = {
            BITBUCKET_URL_HEADER: CLOUD_URL,
            BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER: "cloud-oauth-token-xyz",
        }

        result = _detect_bitbucket_auth_from_headers(headers)

        assert result.url == CLOUD_URL
        assert result.is_cloud is True
        assert result.auth_type == "cloud_bearer"
        assert result.authorization == "Bearer cloud-oauth-token-xyz"
        assert result.cloud_access_token == "cloud-oauth-token-xyz"
        assert result.unauthorized is False

    def test_bitbucket_org_subdomain_url_classifies_as_cloud(self) -> None:
        """``bitbucket.org`` (not just ``api.bitbucket.org``) is a CloudHost."""
        headers = {
            BITBUCKET_URL_HEADER: CLOUD_BITBUCKET_ORG_URL,
            BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER: "tok",
        }

        result = _detect_bitbucket_auth_from_headers(headers)

        assert result.url == CLOUD_URL
        assert result.is_cloud is True
        assert result.auth_type == "cloud_bearer"
        assert result.authorization == "Bearer tok"

    def test_cloud_bearer_takes_priority_over_cloud_basic(self) -> None:
        """Row B wins over Row C when both header sets are present."""
        headers = {
            BITBUCKET_URL_HEADER: CLOUD_URL,
            BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER: "bearer-wins",
            BITBUCKET_CLOUD_USERNAME_HEADER: "alice",
            BITBUCKET_CLOUD_APP_PASSWORD_HEADER: "app-pw",
        }

        result = _detect_bitbucket_auth_from_headers(headers)

        assert result.auth_type == "cloud_bearer"
        assert result.authorization == "Bearer bearer-wins"
        # Basic headers are not consumed when the bearer wins.
        assert result.username is None
        assert result.app_password is None


# ---------------------------------------------------------------------------
# Row C — CloudHost URL + Cloud Username + App Password
# ---------------------------------------------------------------------------


class TestRowC:
    """Row C: Cloud URL + Username + App Password → ``Basic <base64(u:p)>``."""

    def test_cloud_url_plus_username_and_app_password_resolves_to_basic(self) -> None:
        """Basic auth header is `base64(username:app_password)`."""
        headers = {
            BITBUCKET_URL_HEADER: CLOUD_URL,
            BITBUCKET_CLOUD_USERNAME_HEADER: "alice",
            BITBUCKET_CLOUD_APP_PASSWORD_HEADER: "secret-app-pw",
        }

        result = _detect_bitbucket_auth_from_headers(headers)

        assert result.url == CLOUD_URL
        assert result.is_cloud is True
        assert result.auth_type == "basic"
        assert result.authorization == _basic("alice", "secret-app-pw")
        assert result.username == "alice"
        assert result.app_password == "secret-app-pw"
        assert result.unauthorized is False

    @pytest.mark.parametrize(
        "headers_extra",
        [
            # Only the username header — app password missing.
            {BITBUCKET_CLOUD_USERNAME_HEADER: "alice"},
            # Only the app password header — username missing.
            {BITBUCKET_CLOUD_APP_PASSWORD_HEADER: "secret"},
        ],
    )
    def test_lone_half_of_cloud_basic_pair_does_not_resolve(
        self, headers_extra: dict[str, str]
    ) -> None:
        """A single half of the Cloud Basic pair is not usable — falls through to Row D."""
        headers = {BITBUCKET_URL_HEADER: CLOUD_URL, **headers_extra}

        result = _detect_bitbucket_auth_from_headers(headers)

        # Falls through to Row D (Cloud URL with no usable Cloud credential).
        assert result.auth_type is None
        assert result.authorization is None
        assert result.unauthorized is True
        assert result.is_cloud is True


# ---------------------------------------------------------------------------
# Row D — CloudHost URL + no Cloud credential header
# ---------------------------------------------------------------------------


class TestRowD:
    """Row D: Cloud URL with no Cloud credential header → ``unauthorized=True``."""

    def test_cloud_url_without_cloud_creds_is_unauthorized(self) -> None:
        """The caller MUST translate this into HTTP 401 (Req 17.4)."""
        headers = {BITBUCKET_URL_HEADER: CLOUD_URL}

        result = _detect_bitbucket_auth_from_headers(headers)

        assert result.url == CLOUD_URL
        assert result.is_cloud is True
        assert result.auth_type is None
        assert result.authorization is None
        assert result.unauthorized is True


# ---------------------------------------------------------------------------
# Row K — never mix Cloud bearer with DC URL; never mix DC PAT with Cloud URL
# ---------------------------------------------------------------------------


class TestRowK:
    """Row K: atomically discard Cloud creds on DC URLs and vice versa (Req 17.5)."""

    def test_dc_url_plus_cloud_bearer_alone_discards_bearer(self) -> None:
        """A Cloud bearer on a DC URL is discarded; no auth resolved."""
        headers = {
            BITBUCKET_URL_HEADER: DC_URL,
            BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER: "cloud-bearer",
        }

        result = _detect_bitbucket_auth_from_headers(headers)

        assert result.url == DC_URL
        assert result.is_cloud is False
        assert result.auth_type is None
        assert result.authorization is None
        assert result.unauthorized is False
        # Critically: the Cloud bearer never leaks into the resolved auth.
        assert result.cloud_access_token is None

    def test_dc_url_plus_cloud_bearer_and_dc_pat_resolves_as_row_a(self) -> None:
        """The Cloud bearer is discarded and the DC PAT wins (Row A)."""
        headers = {
            BITBUCKET_URL_HEADER: DC_URL,
            BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER: "cloud-bearer",
            BITBUCKET_DC_PAT_HEADER: "dc-pat",
        }

        result = _detect_bitbucket_auth_from_headers(headers)

        assert result.is_cloud is False
        assert result.auth_type == "pat"
        assert result.authorization == "Bearer dc-pat"
        assert result.personal_token == "dc-pat"
        # Cloud bearer was discarded atomically.
        assert result.cloud_access_token is None

    def test_dc_url_plus_cloud_basic_pair_is_discarded(self) -> None:
        """Cloud Basic headers on a DC URL are discarded with no auth resolved."""
        headers = {
            BITBUCKET_URL_HEADER: DC_URL,
            BITBUCKET_CLOUD_USERNAME_HEADER: "alice",
            BITBUCKET_CLOUD_APP_PASSWORD_HEADER: "app-pw",
        }

        result = _detect_bitbucket_auth_from_headers(headers)

        assert result.is_cloud is False
        assert result.auth_type is None
        assert result.authorization is None
        assert result.username is None
        assert result.app_password is None

    def test_cloud_url_plus_dc_pat_alone_is_unauthorized(self) -> None:
        """Row K inverse: DC PAT on a Cloud URL is discarded → Row D."""
        headers = {
            BITBUCKET_URL_HEADER: CLOUD_URL,
            BITBUCKET_DC_PAT_HEADER: "dc-pat-should-be-discarded",
        }

        result = _detect_bitbucket_auth_from_headers(headers)

        assert result.url == CLOUD_URL
        assert result.is_cloud is True
        assert result.auth_type is None
        assert result.authorization is None
        assert result.unauthorized is True
        # The DC PAT never leaks into the Cloud-URL request.
        assert result.personal_token is None

    def test_cloud_url_plus_dc_pat_and_cloud_bearer_resolves_as_row_b(self) -> None:
        """The DC PAT is discarded and the Cloud bearer wins (Row B)."""
        headers = {
            BITBUCKET_URL_HEADER: CLOUD_URL,
            BITBUCKET_DC_PAT_HEADER: "dc-pat-discarded",
            BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER: "cloud-bearer-wins",
        }

        result = _detect_bitbucket_auth_from_headers(headers)

        assert result.is_cloud is True
        assert result.auth_type == "cloud_bearer"
        assert result.authorization == "Bearer cloud-bearer-wins"
        assert result.cloud_access_token == "cloud-bearer-wins"
        # DC PAT was atomically discarded.
        assert result.personal_token is None
