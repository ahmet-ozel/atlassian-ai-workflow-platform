"""Configuration module for Bitbucket Data Center API interactions."""

import logging
import os
import urllib.parse
from dataclasses import dataclass
from typing import Literal

from ..utils.env import get_custom_headers, is_env_ssl_verify

logger = logging.getLogger("mcp-atlassian.bitbucket.config")


_BITBUCKET_CLOUD_API_URL = "https://api.bitbucket.org"


def is_cloud_host(url: str) -> bool:
    """Return True when *url* hosts Atlassian Cloud Bitbucket.

    A URL is classified as Cloud when its hostname (parsed by
    :func:`urllib.parse.urlparse`, lowercased) matches any of:

    - ``api.bitbucket.org``
    - ``bitbucket.org``
    - any subdomain of ``bitbucket.org`` (i.e. ends with ``.bitbucket.org``)

    All other hostnames, including IP literals (e.g. ``192.0.2.10``) and
    ``localhost``, classify as Data Center (``False``).

    Args:
        url: The base URL to classify. May be empty or malformed; in those
            cases the function returns ``False`` rather than raising.

    Returns:
        ``True`` when *url* points at Atlassian Cloud Bitbucket, else ``False``.
    """
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except (ValueError, AttributeError, TypeError):
        return False
    host = host.lower()
    return (
        host == "api.bitbucket.org"
        or host == "bitbucket.org"
        or host.endswith(".bitbucket.org")
    )


def normalise_cloud_api_url(url: str) -> str:
    """Return the canonical Bitbucket Cloud REST API base URL.

    Operators often paste the browser URL for a workspace or repository
    (for example ``https://bitbucket.org/team/repo``). Cloud tool calls use
    ``/2.0/...`` REST paths, so keeping the browser URL as the base produces
    invalid URLs such as ``https://bitbucket.org/team/repo/2.0/...``.
    """
    return _BITBUCKET_CLOUD_API_URL if is_cloud_host(url) else url


_normalise_cloud_api_url = normalise_cloud_api_url


@dataclass
class BitbucketConfig:
    """Bitbucket Server/Data Center API configuration.

    Handles authentication for Bitbucket Server/Data Center:
    - Personal access token (PAT) - recommended
    - Basic auth (username/password)
    """

    url: str  # Base URL for Bitbucket DC or Cloud (e.g., https://bitbucket.example.com)
    auth_type: Literal["basic", "pat", "cloud_bearer"]  # Authentication type
    username: str | None = None  # Username for basic auth (DC or Cloud)
    password: str | None = None  # Password for basic auth (DC only)
    personal_token: str | None = None  # Personal access token (DC PAT)
    ssl_verify: bool = True  # Whether to verify SSL certificates
    projects_filter: str | None = None  # Comma-separated project keys to filter
    http_proxy: str | None = None  # HTTP proxy URL
    https_proxy: str | None = None  # HTTPS proxy URL
    no_proxy: str | None = None  # Comma-separated list of hosts to bypass proxy
    socks_proxy: str | None = None  # SOCKS proxy URL
    custom_headers: dict[str, str] | None = None  # Custom HTTP headers
    client_cert: str | None = None  # Client certificate file path (.pem)
    client_key: str | None = None  # Client private key file path (.pem)
    client_key_password: str | None = None  # Password for encrypted private key
    timeout: int = 75  # Connection timeout in seconds
    # Cloud-specific configuration (unused in DC mode; populated by from_env in task 2.4)
    workspace: str | None = None  # Default Cloud workspace slug
    app_password: str | None = None  # Cloud App Password (paired with username)
    cloud_access_token: str | None = None  # Cloud OAuth 2.0 bearer token

    @property
    def is_cloud(self) -> bool:
        """Whether the configured Bitbucket instance is Atlassian Cloud.

        The value is derived purely from :attr:`url` using
        :func:`is_cloud_host`. No separate mode flag is maintained.

        Returns:
            ``True`` when :attr:`url` hosts Atlassian Cloud Bitbucket, else
            ``False``.
        """
        return is_cloud_host(self.url)

    @property
    def verify_ssl(self) -> bool:
        """Compatibility property.

        Returns:
            The ssl_verify value
        """
        return self.ssl_verify

    @classmethod
    def from_env(cls) -> "BitbucketConfig":
        """Create configuration from environment variables.

        The parser branches on :func:`is_cloud_host` applied to
        ``BITBUCKET_URL``:

        - Cloud hosts select Cloud-mode credentials
          (``BITBUCKET_CLOUD_ACCESS_TOKEN`` → ``cloud_bearer``, or
          ``BITBUCKET_USERNAME`` + ``BITBUCKET_APP_PASSWORD`` → ``basic``)
          and resolve the default workspace from ``BITBUCKET_WORKSPACE`` or
          the URL path.
        - DC hosts use the original DC credential parsing
          (``BITBUCKET_PERSONAL_TOKEN`` → ``pat``, or
          ``BITBUCKET_USERNAME`` + ``BITBUCKET_PASSWORD`` → ``basic``).
          Cloud-only environment variables are ignored on DC URLs so that
          existing DC deployments behave identically to the pre-feature
          version (Requirement 23.3).

        Returns:
            BitbucketConfig with values from environment variables

        Raises:
            ValueError: If required environment variables are missing or invalid
        """
        url = os.getenv("BITBUCKET_URL")
        if not url:
            error_msg = (
                "Missing required BITBUCKET_URL environment variable. "
                "Set BITBUCKET_URL to your Bitbucket Server/DC base URL, "
                "for example https://bitbucket.your-company.com"
            )
            raise ValueError(error_msg)

        # Branch on Cloud vs DC before any credential parsing so that the
        # DC branch below is byte-for-byte identical to the pre-feature
        # implementation (Requirement 3.5, 23.1, 23.3).
        if is_cloud_host(url):
            auth_type, username, password, personal_token, app_password, \
                cloud_access_token, workspace = cls._parse_cloud_env(url)
            url = _normalise_cloud_api_url(url)
        else:
            # DC mode: existing parsing, unchanged.
            username = os.getenv("BITBUCKET_USERNAME")
            password = os.getenv("BITBUCKET_PASSWORD")
            personal_token = os.getenv("BITBUCKET_PERSONAL_TOKEN")

            if personal_token:
                auth_type = "pat"
            elif username and password:
                auth_type = "basic"
            else:
                error_msg = (
                    "Bitbucket Server/Data Center authentication requires "
                    "BITBUCKET_PERSONAL_TOKEN or BITBUCKET_USERNAME and "
                    "BITBUCKET_PASSWORD. "
                    "Set BITBUCKET_PERSONAL_TOKEN, or set both "
                    "BITBUCKET_USERNAME and BITBUCKET_PASSWORD."
                )
                raise ValueError(error_msg)

            # Cloud-only fields remain unset on DC URLs.
            app_password = None
            cloud_access_token = None
            workspace = None

        # SSL verification
        ssl_verify = is_env_ssl_verify("BITBUCKET_SSL_VERIFY")

        # Projects filter
        projects_filter = os.getenv("BITBUCKET_PROJECTS_FILTER")

        # Proxy settings
        http_proxy = os.getenv("BITBUCKET_HTTP_PROXY", os.getenv("HTTP_PROXY"))
        https_proxy = os.getenv("BITBUCKET_HTTPS_PROXY", os.getenv("HTTPS_PROXY"))
        no_proxy = os.getenv("BITBUCKET_NO_PROXY", os.getenv("NO_PROXY"))
        socks_proxy = os.getenv("BITBUCKET_SOCKS_PROXY", os.getenv("SOCKS_PROXY"))

        # Custom headers
        custom_headers = get_custom_headers("BITBUCKET_CUSTOM_HEADERS")

        # Client certificate settings
        client_cert = os.getenv("BITBUCKET_CLIENT_CERT")
        client_key = os.getenv("BITBUCKET_CLIENT_KEY")
        client_key_password = os.getenv("BITBUCKET_CLIENT_KEY_PASSWORD")

        # Timeout setting
        timeout = 75
        if os.getenv("BITBUCKET_TIMEOUT") and os.getenv("BITBUCKET_TIMEOUT", "").isdigit():
            timeout = int(os.getenv("BITBUCKET_TIMEOUT", "75"))

        return cls(
            url=url,
            auth_type=auth_type,
            username=username,
            password=password,
            personal_token=personal_token,
            ssl_verify=ssl_verify,
            projects_filter=projects_filter,
            http_proxy=http_proxy,
            https_proxy=https_proxy,
            no_proxy=no_proxy,
            socks_proxy=socks_proxy,
            custom_headers=custom_headers,
            client_cert=client_cert,
            client_key=client_key,
            client_key_password=client_key_password,
            timeout=timeout,
            workspace=workspace,
            app_password=app_password,
            cloud_access_token=cloud_access_token,
        )

    @staticmethod
    def _parse_cloud_env(
        url: str,
    ) -> tuple[
        Literal["basic", "cloud_bearer"],
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
        str | None,
    ]:
        """Resolve Cloud-mode credentials and default workspace from env.

        Returns a tuple of
        ``(auth_type, username, password, personal_token, app_password,
        cloud_access_token, workspace)``. ``password`` and ``personal_token``
        are always ``None`` in Cloud mode; they are returned for a uniform
        tuple shape with the DC branch in :meth:`from_env`.

        Raises:
            ValueError: When Cloud credentials are missing or the Basic
                credential pair is incomplete.
        """
        username = os.getenv("BITBUCKET_USERNAME")
        app_password = os.getenv("BITBUCKET_APP_PASSWORD")
        cloud_access_token = os.getenv("BITBUCKET_CLOUD_ACCESS_TOKEN")

        # Validate the Basic credential pair before picking an auth_type so
        # that an incomplete pair is rejected even when a bearer token is
        # also set — the operator has given us contradictory credentials.
        # (Requirement 3.4)
        if app_password and not username:
            raise ValueError(
                "Bitbucket Cloud Basic authentication requires both "
                "BITBUCKET_USERNAME and BITBUCKET_APP_PASSWORD. "
                "BITBUCKET_APP_PASSWORD is set but BITBUCKET_USERNAME is unset."
            )

        auth_type: Literal["basic", "cloud_bearer"]
        if cloud_access_token:
            auth_type = "cloud_bearer"
        elif username and app_password:
            auth_type = "basic"
        else:
            # Requirement 3.3 — no usable Cloud credential pair.
            raise ValueError(
                "Bitbucket Cloud authentication requires "
                "BITBUCKET_CLOUD_ACCESS_TOKEN, or BITBUCKET_USERNAME and "
                "BITBUCKET_APP_PASSWORD. "
                "Set BITBUCKET_CLOUD_ACCESS_TOKEN, or set both "
                "BITBUCKET_USERNAME and BITBUCKET_APP_PASSWORD."
            )

        # Workspace resolution: env var wins; otherwise parse the first path
        # segment from the URL (only for tenant-rooted hosts like
        # bitbucket.org / *.bitbucket.org — api.bitbucket.org URLs carry
        # REST API paths, not workspace slugs).
        workspace = os.getenv("BITBUCKET_WORKSPACE") or None
        if not workspace:
            parsed = urllib.parse.urlparse(url)
            host = (parsed.hostname or "").lower()
            if host != "api.bitbucket.org":
                path_parts = [p for p in parsed.path.split("/") if p]
                if path_parts:
                    workspace = path_parts[0]

        # password / personal_token are never used in Cloud mode.
        return auth_type, username, None, None, app_password, cloud_access_token, workspace

    def is_auth_configured(self) -> bool:
        """Check if the current authentication configuration is complete.

        Returns:
            bool: True if authentication is fully configured, False otherwise.
        """
        if self.auth_type == "pat":
            return bool(self.personal_token)
        elif self.auth_type == "cloud_bearer":
            return bool(self.cloud_access_token)
        elif self.auth_type == "basic":
            # DC uses password; Cloud uses app_password. Either satisfies
            # the Basic pair as long as username is present.
            return bool(self.username and (self.password or self.app_password))
        logger.warning(
            f"Unknown or unsupported auth_type: {self.auth_type} in BitbucketConfig"
        )
        return False
