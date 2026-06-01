"""Base client module for Bitbucket Data Center API interactions."""

import logging
import os
from typing import Any, Callable

from atlassian import Bitbucket
from requests.exceptions import ConnectionError as RequestsConnectionError

from ..exceptions import MCPAtlassianAuthenticationError
from ..utils.dc_guards import DCVersionProbe
from ..utils.logging import get_masked_session_headers, log_config_param, mask_sensitive
from ..utils.ssl import configure_ssl_verification
from .config import BitbucketConfig

logger = logging.getLogger("mcp-atlassian.bitbucket")


class BitbucketClient(DCVersionProbe):
    """Base client for Bitbucket Server/Data Center API interactions."""

    config: BitbucketConfig

    def __init__(self, config: BitbucketConfig | None = None) -> None:
        """Initialize the Bitbucket client with configuration options.

        Args:
            config: Optional configuration object (will use env vars if not provided)

        Raises:
            ValueError: If configuration is invalid or required credentials are missing
            MCPAtlassianAuthenticationError: If authentication fails
        """
        self.config = config or BitbucketConfig.from_env()

        # Lazily probed DC version cache. The DCVersionProbe mixin declares a
        # class-level ``_dc_version = None`` attribute; we set an instance
        # attribute here so each client has its own cache slot.
        self._dc_version: str | None = None

        # Initialize the Bitbucket client based on auth type. The ``cloud``
        # flag passed to ``atlassian.Bitbucket`` is derived from
        # ``self.config.is_cloud`` (URL-based classification) for PAT and
        # Basic auth; the ``cloud_bearer`` variant is Cloud-only by
        # construction and always sets ``cloud=True`` (Requirements 4.1,
        # 4.2, 4.3). No unconditional ``cloud=False`` path remains.
        if self.config.auth_type == "pat":
            logger.debug(
                f"Initializing Bitbucket client with Token (PAT) auth. "
                f"URL: {self.config.url}, "
                f"Token (masked): {mask_sensitive(str(self.config.personal_token))}, "
                f"cloud={self.config.is_cloud}"
            )
            self.bitbucket = Bitbucket(
                url=self.config.url,
                token=self.config.personal_token,
                cloud=self.config.is_cloud,
                verify_ssl=self.config.ssl_verify,
                timeout=self.config.timeout,
            )
        elif self.config.auth_type == "cloud_bearer":
            logger.debug(
                f"Initializing Bitbucket client with Cloud OAuth 2.0 bearer auth. "
                f"URL: {self.config.url}, "
                f"Token (masked): {mask_sensitive(str(self.config.cloud_access_token))}"
            )
            self.bitbucket = Bitbucket(
                url=self.config.url,
                token=self.config.cloud_access_token,
                cloud=True,
                verify_ssl=self.config.ssl_verify,
                timeout=self.config.timeout,
            )
        else:  # basic auth (DC password or Cloud app password)
            logger.debug(
                f"Initializing Bitbucket client with Basic auth. "
                f"URL: {self.config.url}, Username: {self.config.username}, "
                f"cloud={self.config.is_cloud}"
            )
            self.bitbucket = Bitbucket(
                url=self.config.url,
                username=self.config.username,
                password=self.config.password or self.config.app_password,
                cloud=self.config.is_cloud,
                verify_ssl=self.config.ssl_verify,
                timeout=self.config.timeout,
            )

        # Disable trust_env for PAT to prevent .netrc from overriding
        if self.config.auth_type == "pat":
            self.bitbucket._session.trust_env = False

        # Explicitly wire Cloud authentication onto the session to guarantee
        # the Authorization header shape documented in Requirements 3.1 and
        # 3.2, independent of any behavioral change in atlassian-python-api's
        # constructor-time auth setup. For DC auth types (PAT, and Basic with
        # DC password) the library's defaults remain in effect unchanged —
        # the PAT branch already sets ``Authorization: Bearer <token>`` via
        # ``_create_token_session`` and the DC Basic branch already sets
        # ``_session.auth = (username, password)`` via
        # ``_create_basic_session``, which is exactly what Requirement 3.5
        # preserves.
        if self.config.auth_type == "cloud_bearer":
            # Requirement 3.2: Cloud OAuth 2.0 bearer token is sent on every
            # outbound HTTP request and SHALL NOT be combined with Basic
            # credentials. Clear any leftover ``_session.auth`` tuple so
            # requests does not emit a competing ``Authorization: Basic``
            # header alongside the bearer.
            token = (self.config.cloud_access_token or "").strip()
            self.bitbucket._session.headers["Authorization"] = f"Bearer {token}"
            self.bitbucket._session.auth = None
        elif self.config.auth_type == "basic" and self.config.is_cloud:
            # Requirement 3.1: Cloud Basic auth uses ``username:app_password``.
            # ``atlassian.Bitbucket`` has already set ``_session.auth`` from
            # the ``password=`` constructor argument (which we pass as
            # ``self.config.password or self.config.app_password``); we
            # re-assign it explicitly here so the source of credentials is
            # unambiguous in code and so a future refactor cannot silently
            # drop Cloud Basic wiring.
            self.bitbucket._session.auth = (
                self.config.username or "",
                self.config.app_password or self.config.password or "",
            )

        # Configure SSL verification
        configure_ssl_verification(
            service_name="Bitbucket",
            url=self.config.url,
            session=self.bitbucket._session,
            ssl_verify=self.config.ssl_verify,
            client_cert=self.config.client_cert,
            client_key=self.config.client_key,
            client_key_password=self.config.client_key_password,
        )

        # Proxy configuration
        proxies = {}
        if self.config.http_proxy:
            proxies["http"] = self.config.http_proxy
        if self.config.https_proxy:
            proxies["https"] = self.config.https_proxy
        if self.config.socks_proxy:
            proxies["socks"] = self.config.socks_proxy
        if proxies:
            self.bitbucket._session.proxies.update(proxies)
            for k, v in proxies.items():
                log_config_param(
                    logger, "Bitbucket", f"{k.upper()}_PROXY", v, sensitive=True
                )
        if self.config.no_proxy and isinstance(self.config.no_proxy, str):
            os.environ["NO_PROXY"] = self.config.no_proxy
            log_config_param(logger, "Bitbucket", "NO_PROXY", self.config.no_proxy)

        # Apply custom headers if configured
        if self.config.custom_headers:
            self._apply_custom_headers()

        # Test authentication during initialization (in debug mode only)
        if logger.isEnabledFor(logging.DEBUG):
            try:
                self._validate_authentication()
            except MCPAtlassianAuthenticationError:
                logger.warning(
                    "Authentication validation failed during client initialization - "
                    "continuing anyway"
                )

    @property
    def is_cloud(self) -> bool:
        """Effective operating mode for this client instance.

        Returns ``True`` when the underlying :class:`BitbucketConfig` classifies
        the configured URL as an Atlassian Cloud Bitbucket host (``bitbucket.org``
        or any ``*.bitbucket.org`` subdomain), and ``False`` for Bitbucket Data
        Center / Server hosts (Requirement 4.4).

        This property is read-only and reflects the effective mode for the
        current request. Any per-request header override (for example
        ``X-Atlassian-Bitbucket-Url``) is applied by the dependency layer
        before ``__init__`` runs, so the URL on ``self.config`` is already the
        resolved per-request URL by the time this property is evaluated.
        """
        return self.config.is_cloud

    def _validate_authentication(self) -> None:
        """Validate authentication by making a simple API call."""
        try:
            logger.debug(
                "Testing Bitbucket authentication by retrieving project list..."
            )
            # Use a simple API call to validate auth
            result = self.bitbucket.project_list()
            if result is not None:
                logger.info("Bitbucket authentication successful.")
            else:
                logger.warning(
                    "Bitbucket authentication test returned empty result - "
                    "this may indicate an issue"
                )
        except RequestsConnectionError as e:
            error_msg = (
                f"Could not connect to Bitbucket at {self.config.url}. "
                "Check that BITBUCKET_URL is correct and the instance is reachable."
            )
            logger.error(error_msg)
            raise MCPAtlassianAuthenticationError(error_msg) from e
        except Exception as e:
            error_msg = f"Bitbucket authentication validation failed: {e}"
            logger.error(error_msg)
            raise MCPAtlassianAuthenticationError(error_msg) from e

    def _apply_custom_headers(self) -> None:
        """Apply custom headers to the Bitbucket session."""
        if not self.config.custom_headers:
            return

        logger.debug(
            f"Applying {len(self.config.custom_headers)} custom headers to Bitbucket session"
        )
        for header_name, header_value in self.config.custom_headers.items():
            self.bitbucket._session.headers[header_name] = header_value
            logger.debug(f"Applied custom header: {header_name}")

    def get_dc_version(self) -> str | None:
        """Lazily probe and cache the Bitbucket Data Center version.

        On first call this issues ``GET /rest/api/latest/application-properties``
        against the configured Bitbucket instance and extracts the ``version``
        field from the JSON response (a dict shaped like
        ``{"version": "8.19.0", "buildNumber": "8019000", ...}``). The
        resulting string is cached on ``self._dc_version`` so subsequent
        invocations and every downstream ``check_dc_version`` call reuse the
        same probe without additional HTTP traffic.

        Any failure during the probe (404 on legacy versions, network error,
        unexpected payload shape) leaves ``self._dc_version`` as ``None`` so
        :func:`mcp_atlassian.utils.dc_guards.check_dc_version` can fall
        through to ``dc_version_unknown`` once a tool attempts the actual
        call and sees a 404 / 501.

        The probe is intentionally not called from ``__init__`` so that
        importing / constructing the client stays a pure-Python operation
        and does not issue HTTP during test-collection or config-dump runs.

        Returns:
            The detected version string (for example ``"8.19.0"``) or
            ``None`` when the probe has not yet succeeded or failed.
        """
        if self._dc_version is not None:
            return self._dc_version

        try:
            response = self.bitbucket.get("/rest/api/latest/application-properties")
        except Exception as e:  # noqa: BLE001 — probe must never raise
            logger.debug(
                f"Bitbucket DC version probe failed against "
                f"/rest/api/latest/application-properties: {e}"
            )
            return None

        if not isinstance(response, dict):
            logger.debug(
                f"Bitbucket DC version probe returned unexpected payload shape: "
                f"{type(response).__name__}"
            )
            return None

        version = response.get("version")
        if not isinstance(version, str) or not version.strip():
            logger.debug(
                "Bitbucket DC version probe response did not contain a "
                "usable 'version' field; caching None."
            )
            return None

        self._dc_version = version
        logger.debug(f"Detected Bitbucket DC version: {version}")
        return self._dc_version

    def _get_paged_results(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        limit: int = 25,
        normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all pages of results from a Bitbucket paginated API endpoint.

        Dispatches to the appropriate mode-specific helper based on
        ``self.is_cloud``. The DC branch consumes the DC_Pagination_Shape
        envelope (``{values, isLastPage, nextPageStart, size, limit, start}``),
        and the Cloud branch consumes the Cloud_Pagination_Shape envelope
        (``{values, next, page, pagelen, size}``). Callers always receive a
        flat list of value dicts; the Cloud branch never exposes the ``next``
        URL to the caller (Requirement 7.4).

        Args:
            url: The API endpoint URL (relative to base URL)
            params: Optional query parameters. On Cloud, only the first
                request carries these; subsequent requests follow the
                ``next`` URL which carries its own ``pagelen`` / ``page``.
            limit: Upper bound on the cumulative number of values returned.
                When positive, iteration stops as soon as the accumulator
                reaches ``limit`` values in either mode (Requirement 7.5).
                On DC, ``limit`` is also used as the per-page size.
            normalizer: Optional callable applied to each Cloud value before
                accumulating. Ignored in DC mode, where payloads are already
                in the DC shape that downstream code consumes.

        Returns:
            List of all result values across all pages, normalized to the
            DC-shaped dict on Cloud when ``normalizer`` is supplied.
        """
        if self.is_cloud:
            return self._get_paged_results_cloud(
                url, params=params, limit=limit, normalizer=normalizer
            )
        return self._get_paged_results_dc(url, params=params, limit=limit)

    def _get_paged_results_dc(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Fetch all pages of results from a Bitbucket DC paginated API endpoint.

        Consumes the DC_Pagination_Shape envelope, stopping when
        ``isLastPage`` is ``True`` or ``nextPageStart`` is ``None``
        (Requirement 7.2), or when the accumulated value count reaches
        ``limit`` (Requirement 7.5).

        Args:
            url: The API endpoint URL (relative to base URL)
            params: Optional query parameters
            limit: Number of results per page (default 25)

        Returns:
            List of all result values across all pages
        """
        all_values: list[dict[str, Any]] = []
        start = 0
        params = params or {}

        while True:
            params["start"] = start
            params["limit"] = limit
            response = self.bitbucket.get(url, params=params)

            if not isinstance(response, dict):
                break

            values = response.get("values", [])
            all_values.extend(values)

            if response.get("isLastPage", True):
                break

            start = response.get("nextPageStart", start + limit)

        return all_values

    def _get_paged_results_cloud(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        limit: int = 25,
        normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch all pages of results from a Bitbucket Cloud 2.0 paginated endpoint.

        Consumes the Cloud_Pagination_Shape envelope
        (``{values, next, pagelen, page, size}``), stopping when ``next`` is
        absent or ``None`` (Requirement 7.3), or when the accumulated value
        count reaches ``limit`` (Requirement 7.5). The ``next`` URL returned
        by Cloud carries its own ``pagelen`` / ``page`` query parameters, so
        only the first request is issued with the caller-supplied ``params``;
        subsequent requests follow ``next`` unchanged. The returned list
        SHALL NOT expose the Cloud ``next`` URL (Requirement 7.4).

        When a ``normalizer`` callable is provided, each raw Cloud value is
        passed through it before being appended to the accumulator, so that
        downstream mixin code continues to see the DC-shaped payload it
        already consumes.

        Args:
            url: The Cloud API endpoint URL (relative or absolute). The first
                request uses this URL together with ``params``; subsequent
                requests follow the envelope's ``next`` URL.
            params: Optional query parameters sent with the FIRST request
                only. ``pagelen`` defaults to ``limit`` when positive.
            limit: Upper bound on cumulative values returned. Zero or
                negative values disable the limit (all pages consumed).
            normalizer: Optional per-value transformation applied before
                accumulation. ``None`` means identity (pass through as-is).

        Returns:
            Flat list of (optionally normalized) value dicts across all
            pages. The Cloud ``next`` URL is not present in the output.
        """
        all_values: list[dict[str, Any]] = []
        first_params: dict[str, Any] | None = dict(params or {})
        if limit and limit > 0:
            first_params.setdefault("pagelen", limit)
        next_url: str | None = url
        is_first = True

        while next_url:
            if is_first:
                response = self.bitbucket.get(next_url, params=first_params)
                is_first = False
            else:
                # Cloud's ``next`` is a fully-qualified URL that already
                # carries its own ``pagelen`` / ``page`` parameters, so we
                # pass ``params=None`` to avoid doubling them up.
                response = self.bitbucket.get(next_url, params=None)

            if not isinstance(response, dict):
                break

            values = response.get("values", []) or []
            for value in values:
                normalized = normalizer(value) if normalizer else value
                all_values.append(normalized)
                if limit and limit > 0 and len(all_values) >= limit:
                    return all_values[:limit]

            next_url = response.get("next")

        return all_values
