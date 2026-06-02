"""Utility functions related to environment checking."""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Literal

from ..bitbucket.config import is_cloud_host, normalise_cloud_api_url
from .urls import is_atlassian_cloud_url

logger = logging.getLogger("mcp-atlassian.utils.environment")


# Per-request HTTP header names used to carry Bitbucket credentials
# alongside ``X-Atlassian-Bitbucket-Url``. Rows A, B, C, D, K in the auth
# truth table (see ``bitbucket-cloud-dc-parity`` design, Section
# "Authentication: truth table").
BITBUCKET_URL_HEADER = "X-Atlassian-Bitbucket-Url"
BITBUCKET_DC_PAT_HEADER = "X-Atlassian-Bitbucket-Personal-Token"  # noqa: S105
BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER = "X-Atlassian-Bitbucket-Cloud-Access-Token"  # noqa: S105
BITBUCKET_CLOUD_APP_PASSWORD_HEADER = "X-Atlassian-Bitbucket-App-Password"  # noqa: S105
BITBUCKET_CLOUD_USERNAME_HEADER = "X-Atlassian-Bitbucket-Username"


@dataclass(frozen=True)
class BitbucketHeaderAuth:
    """Resolved Bitbucket authentication derived from per-request headers.

    This is the data shape returned by
    :func:`_detect_bitbucket_auth_from_headers` — a pure, side-effect-free
    translation from the request's ``X-Atlassian-Bitbucket-*`` headers into
    the outbound ``Authorization`` value and the ``BitbucketConfig`` auth
    type the dependency layer will use to construct a per-request fetcher.

    When ``unauthorized`` is ``True``, callers (the HTTP-to-FastMCP adapter)
    MUST return HTTP 401 without issuing any outbound Bitbucket request
    (row D in the auth truth table). In that case ``auth_type`` and
    ``authorization`` are ``None``.

    Attributes:
        url: The ``X-Atlassian-Bitbucket-Url`` header value, or ``None``
            when the header was absent.
        is_cloud: ``True`` when :attr:`url` resolves to a CloudHost;
            ``False`` otherwise (including when ``url`` is ``None``).
        auth_type: The resolved :class:`BitbucketConfig` ``auth_type`` for
            the request: ``"pat"`` (DC PAT — row A), ``"cloud_bearer"`` (Cloud
            OAuth 2.0 — row B), ``"basic"`` (Cloud Basic with app password —
            row C), or ``None`` when no usable header set was present.
        authorization: The ready-to-use ``Authorization`` header value for
            the outbound Bitbucket request (for example ``"Bearer xyz"`` or
            ``"Basic <base64>"``), or ``None`` when no auth is resolved.
        username: The Cloud username extracted from
            :data:`BITBUCKET_CLOUD_USERNAME_HEADER` (row C only); ``None``
            otherwise.
        personal_token: The DC PAT extracted from the
            :data:`BITBUCKET_DC_PAT_HEADER` (row A only); ``None``
            otherwise.
        cloud_access_token: The Cloud bearer token extracted from
            :data:`BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER` (row B only);
            ``None`` otherwise.
        app_password: The Cloud App Password extracted from
            :data:`BITBUCKET_CLOUD_APP_PASSWORD_HEADER` (row C only);
            ``None`` otherwise.
        unauthorized: ``True`` when :attr:`url` resolves to CloudHost but
            no Cloud credential header is present (row D). Callers MUST
            translate this into an HTTP 401 response without any outbound
            Bitbucket call.
    """

    url: str | None
    is_cloud: bool
    auth_type: Literal["pat", "cloud_bearer", "basic"] | None
    authorization: str | None
    username: str | None = None
    personal_token: str | None = None
    cloud_access_token: str | None = None
    app_password: str | None = None
    unauthorized: bool = False


def _detect_bitbucket_auth_from_headers(
    headers: dict[str, str] | None,
) -> BitbucketHeaderAuth:
    """Resolve Bitbucket per-request auth from ``X-Atlassian-Bitbucket-*`` headers.

    Implements rows A, B, C, D, and K of the authentication truth table
    defined in the ``bitbucket-cloud-dc-parity`` design:

    - **Row A** — DCHost URL + DC PAT header → ``auth_type="pat"``, outbound
      ``Authorization: Bearer <PAT>``.
    - **Row B** — CloudHost URL + Cloud Access Token header →
      ``auth_type="cloud_bearer"``, outbound ``Authorization: Bearer <token>``.
      Takes priority over any Basic headers when both are present.
    - **Row C** — CloudHost URL + Cloud Username and App Password headers →
      ``auth_type="basic"``, outbound ``Authorization: Basic <base64(u:p)>``.
    - **Row D** — CloudHost URL with no Cloud credential header →
      :attr:`BitbucketHeaderAuth.unauthorized` is ``True``. The caller
      (HTTP-to-FastMCP adapter) MUST return HTTP 401 without any outbound
      Bitbucket request.
    - **Row K** — A Cloud bearer / Basic header is never combined with a
      DCHost URL: Cloud credential headers are discarded on DC URLs, and a
      DC PAT header is discarded on Cloud URLs. The resolver treats per-
      request headers atomically so a Cloud bearer without a CloudHost URL
      header is never emitted on the wire.

    When ``X-Atlassian-Bitbucket-Url`` is absent, only the DC PAT header
    can resolve to a usable auth (Requirement 17.1) — the DC PAT is
    applied against the globally configured URL. Cloud credential headers
    without a URL header are discarded because row K forbids mixing Cloud
    bearer with a DC base URL and we cannot classify the URL without the
    header.

    Args:
        headers: The per-request HTTP header map, as returned by the ASGI
            middleware (normalised to the canonical capitalisation used in
            :data:`BITBUCKET_URL_HEADER`). ``None`` is treated as an empty
            map.

    Returns:
        A :class:`BitbucketHeaderAuth` describing the resolved auth or the
        unauthorised state for row D. When no relevant header is present,
        every field is ``None`` / ``False``.
    """
    headers = headers or {}

    url_header_val = headers.get(BITBUCKET_URL_HEADER)
    dc_pat_header_val = headers.get(BITBUCKET_DC_PAT_HEADER)
    cloud_token_header_val = headers.get(BITBUCKET_CLOUD_ACCESS_TOKEN_HEADER)
    cloud_username_header_val = headers.get(BITBUCKET_CLOUD_USERNAME_HEADER)
    cloud_app_password_header_val = headers.get(BITBUCKET_CLOUD_APP_PASSWORD_HEADER)

    # Classify the URL if present. Absence means we cannot apply Cloud
    # semantics — in that case only row A (DC PAT against the global URL)
    # can still resolve.
    cloud = bool(url_header_val) and is_cloud_host(url_header_val)

    if cloud:
        cloud_url = normalise_cloud_api_url(url_header_val)
        # Row K (first half): discard DC PAT when URL resolves to CloudHost.
        # We do not want to mix a DC PAT with a Cloud URL on the same
        # request (Requirement 17.5).
        if dc_pat_header_val:
            logger.debug(
                "Discarding %s header because %s resolves to a Cloud host",
                BITBUCKET_DC_PAT_HEADER,
                BITBUCKET_URL_HEADER,
            )

        # Row B — Cloud OAuth2 bearer token takes priority over Basic.
        if cloud_token_header_val:
            return BitbucketHeaderAuth(
                url=cloud_url,
                is_cloud=True,
                auth_type="cloud_bearer",
                authorization=f"Bearer {cloud_token_header_val}",
                cloud_access_token=cloud_token_header_val,
            )

        # Row C — Cloud Basic with App Password. Both the username and the
        # app-password header must be present; a lone half is not usable.
        if cloud_username_header_val and cloud_app_password_header_val:
            encoded = base64.b64encode(
                f"{cloud_username_header_val}:{cloud_app_password_header_val}".encode()
            ).decode("ascii")
            return BitbucketHeaderAuth(
                url=cloud_url,
                is_cloud=True,
                auth_type="basic",
                authorization=f"Basic {encoded}",
                username=cloud_username_header_val,
                app_password=cloud_app_password_header_val,
            )

        # Row D — CloudHost URL but no usable Cloud credential header.
        # The caller MUST return HTTP 401 without any outbound Bitbucket
        # request (Requirement 17.4).
        return BitbucketHeaderAuth(
            url=cloud_url,
            is_cloud=True,
            auth_type=None,
            authorization=None,
            unauthorized=True,
        )

    # Non-Cloud URL (either DCHost, or no URL header at all).
    #
    # Row K (second half): discard Cloud credential headers when the URL
    # resolves to a DC host (or is absent). Never mix a Cloud bearer with
    # a DC base URL on the same request (Requirement 17.5).
    if cloud_token_header_val or cloud_username_header_val or cloud_app_password_header_val:
        logger.debug(
            "Discarding Cloud credential headers because %s does not resolve "
            "to a Cloud host",
            BITBUCKET_URL_HEADER,
        )

    # Row A — DC PAT header. When the URL header is absent we still accept
    # the DC PAT and defer it to the globally configured DC URL
    # (Requirement 17.1).
    if dc_pat_header_val:
        return BitbucketHeaderAuth(
            url=url_header_val,
            is_cloud=False,
            auth_type="pat",
            authorization=f"Bearer {dc_pat_header_val}",
            personal_token=dc_pat_header_val,
        )

    # No usable header set.
    return BitbucketHeaderAuth(
        url=url_header_val,
        is_cloud=cloud,
        auth_type=None,
        authorization=None,
    )


def _check_service_auth(
    service_name: str,
    service_url: str,
    client_id_envs: tuple[str, str],
    client_secret_envs: tuple[str, str],
    access_token_envs: tuple[str, str],
    username_env: str,
    api_env: str,
    pat_env: str,
) -> bool:
    """Detect whether a single Atlassian service is authenticated.

    Args:
        service_name: Human-readable service name (e.g. ``"Confluence"``).
        service_url: URL of the service instance.
        client_id_envs: ``(shared_env, service_env)`` pair for OAuth client ID.
        client_secret_envs: ``(shared_env, service_env)`` pair for OAuth client secret.
        access_token_envs: ``(shared_env, service_env)`` pair for OAuth access token.
        username_env: Env var name for the Basic Auth username.
        api_env: Env var name for the Basic Auth API token / password.
        pat_env: Env var name for the Personal Access Token (Server/DC only).

    Returns:
        ``True`` when a valid auth configuration is detected, ``False`` otherwise.
    """
    is_cloud = is_atlassian_cloud_url(service_url)

    client_id = os.getenv(client_id_envs[0]) or os.getenv(client_id_envs[1])
    client_secret = os.getenv(client_secret_envs[0]) or os.getenv(client_secret_envs[1])
    access_token = os.getenv(access_token_envs[0]) or os.getenv(access_token_envs[1])
    cloud_id = os.getenv("ATLASSIAN_OAUTH_CLOUD_ID")

    # Cloud OAuth check (needs cloud_id)
    if all([client_id, client_secret, cloud_id]):
        logger.info("Using %s OAuth 2.0 (3LO) authentication (Cloud)", service_name)
        return True

    # DC OAuth check (no cloud_id, but has client credentials + non-cloud URL)
    if not is_cloud and client_id and client_secret:
        logger.info("Using %s OAuth 2.0 authentication (Data Center)", service_name)
        return True

    # Cloud BYO access token
    if all([access_token, cloud_id]):
        logger.info(
            "Using %s OAuth 2.0 (3LO) authentication (Cloud) "
            "with provided access token",
            service_name,
        )
        return True

    # DC BYO access token (no cloud_id, non-cloud URL)
    if not is_cloud and access_token:
        logger.info(
            "Using %s OAuth 2.0 authentication (Data Center) "
            "with provided access token",
            service_name,
        )
        return True

    if is_cloud:  # Cloud non-OAuth
        if os.getenv(username_env) and os.getenv(api_env):
            logger.info("Using %s Cloud Basic Authentication (API Token)", service_name)
            return True
    else:  # Server/Data Center non-OAuth
        if os.getenv(pat_env) or (os.getenv(username_env) and os.getenv(api_env)):
            logger.info(
                "Using %s Server/Data Center authentication (PAT or Basic Auth)",
                service_name,
            )
            return True

    return False


def get_available_services(
    headers: dict[str, str] | None = None,
) -> dict[str, bool | None]:
    """Determine which services are available based on environment variables and optional headers."""
    headers = headers or {}

    confluence_url = os.getenv("CONFLUENCE_URL")
    confluence_is_setup = False
    if confluence_url:
        confluence_is_setup = _check_service_auth(
            service_name="Confluence",
            service_url=confluence_url,
            client_id_envs=("ATLASSIAN_OAUTH_CLIENT_ID", "CONFLUENCE_OAUTH_CLIENT_ID"),
            client_secret_envs=(
                "ATLASSIAN_OAUTH_CLIENT_SECRET",
                "CONFLUENCE_OAUTH_CLIENT_SECRET",
            ),
            access_token_envs=(
                "ATLASSIAN_OAUTH_ACCESS_TOKEN",
                "CONFLUENCE_OAUTH_ACCESS_TOKEN",
            ),
            username_env="CONFLUENCE_USERNAME",
            api_env="CONFLUENCE_API_TOKEN",
            pat_env="CONFLUENCE_PERSONAL_TOKEN",
        )

    if not confluence_is_setup and os.getenv("ATLASSIAN_OAUTH_ENABLE", "").lower() in (
        "true",
        "1",
        "yes",
    ):
        confluence_is_setup = True
        logger.info(
            "Using Confluence minimal OAuth configuration "
            "- expecting user-provided tokens via headers"
        )

    if not confluence_is_setup:
        confluence_token = headers.get("X-Atlassian-Confluence-Personal-Token")
        confluence_url_header = headers.get("X-Atlassian-Confluence-Url")
        confluence_username = headers.get("X-Atlassian-Confluence-Username")
        confluence_api_token = headers.get("X-Atlassian-Confluence-Api-Token")

        if confluence_url_header and (
            confluence_token or (confluence_username and confluence_api_token)
        ):
            confluence_is_setup = True
            logger.info("Using Confluence authentication from request headers")

    jira_url = os.getenv("JIRA_URL")
    jira_is_setup = False
    if jira_url:
        jira_is_setup = _check_service_auth(
            service_name="Jira",
            service_url=jira_url,
            client_id_envs=("ATLASSIAN_OAUTH_CLIENT_ID", "JIRA_OAUTH_CLIENT_ID"),
            client_secret_envs=(
                "ATLASSIAN_OAUTH_CLIENT_SECRET",
                "JIRA_OAUTH_CLIENT_SECRET",
            ),
            access_token_envs=(
                "ATLASSIAN_OAUTH_ACCESS_TOKEN",
                "JIRA_OAUTH_ACCESS_TOKEN",
            ),
            username_env="JIRA_USERNAME",
            api_env="JIRA_API_TOKEN",
            pat_env="JIRA_PERSONAL_TOKEN",
        )

    if not jira_is_setup and os.getenv("ATLASSIAN_OAUTH_ENABLE", "").lower() in (
        "true",
        "1",
        "yes",
    ):
        jira_is_setup = True
        logger.info(
            "Using Jira minimal OAuth configuration "
            "- expecting user-provided tokens via headers"
        )

    if not jira_is_setup:
        jira_token = headers.get("X-Atlassian-Jira-Personal-Token")
        jira_url_header = headers.get("X-Atlassian-Jira-Url")
        jira_username = headers.get("X-Atlassian-Jira-Username")
        jira_api_token = headers.get("X-Atlassian-Jira-Api-Token")

        if jira_url_header and (jira_token or (jira_username and jira_api_token)):
            jira_is_setup = True
            logger.info("Using Jira authentication from request headers")

    if not confluence_is_setup:
        logger.info(
            "Confluence is not configured or required environment variables are missing."
        )
    if not jira_is_setup:
        logger.info(
            "Jira is not configured or required environment variables are missing."
        )

    # Check Bitbucket (DC env vars, then Cloud env vars, then per-request headers)
    bitbucket_url = os.getenv("BITBUCKET_URL")
    bitbucket_is_setup = False
    if bitbucket_url:
        personal_token = os.getenv("BITBUCKET_PERSONAL_TOKEN")
        username = os.getenv("BITBUCKET_USERNAME")
        password = os.getenv("BITBUCKET_PASSWORD")
        api_token = os.getenv("BITBUCKET_API_TOKEN")
        app_password = os.getenv("BITBUCKET_APP_PASSWORD")
        cloud_access_token = os.getenv("BITBUCKET_CLOUD_ACCESS_TOKEN")
        if is_cloud_host(bitbucket_url):
            # Cloud: OAuth 2.0 bearer, or Username + API token / App Password.
            if cloud_access_token or (username and (api_token or app_password)):
                bitbucket_is_setup = True
                logger.info("Using Bitbucket Cloud authentication")
        else:
            # DC: PAT, or Username + Password.
            if personal_token or (username and password):
                bitbucket_is_setup = True
                logger.info("Using Bitbucket Server/Data Center authentication")

    if not bitbucket_is_setup:
        header_auth = _detect_bitbucket_auth_from_headers(headers)
        # A URL header plus a resolved auth_type (rows A, B, C) means the
        # per-request headers can carry a full Bitbucket auth. Row D
        # (unauthorized) is reported as "setup" too so the HTTP adapter
        # can translate it into the 401 response without pretending the
        # service is unavailable.
        if header_auth.url and (header_auth.auth_type or header_auth.unauthorized):
            bitbucket_is_setup = True
            if header_auth.auth_type == "cloud_bearer":
                logger.info(
                    "Using Bitbucket Cloud authentication from header bearer token"
                )
            elif header_auth.auth_type == "basic" and header_auth.is_cloud:
                logger.info(
                    "Using Bitbucket Cloud authentication from header app password"
                )
            elif header_auth.auth_type == "pat":
                logger.info(
                    "Using Bitbucket authentication from header personal token"
                )
            elif header_auth.unauthorized:
                logger.info(
                    "Bitbucket Cloud URL header present without Cloud "
                    "credential header — request will be rejected with 401"
                )

    if not bitbucket_is_setup and bitbucket_url:
        logger.info(
            "Bitbucket is not configured or required environment variables are missing."
        )

    return {"confluence": confluence_is_setup, "jira": jira_is_setup, "bitbucket": bitbucket_is_setup}
