"""Base client module for Confluence API interactions."""

import logging
import os
import re

from atlassian import Confluence
from requests import Session
from requests.exceptions import ConnectionError as RequestsConnectionError

from ..exceptions import MCPAtlassianAuthenticationError
from ..utils.dc_guards import DCVersionProbe
from ..utils.logging import get_masked_session_headers, log_config_param, mask_sensitive
from ..utils.oauth import configure_oauth_session
from ..utils.ssl import configure_ssl_verification
from .config import ConfluenceConfig

# Configure logging
logger = logging.getLogger("mcp-atlassian")

# Regex used to extract the leading ``<version>...</version>`` element value
# from an `/rest/applinks/.../manifest` XML payload without pulling in an XML
# parser. DC's manifest document is tightly scoped (Atlassian-authored, served
# by the user's own trusted Confluence instance) and the probe treats the
# value as opaque text: :func:`parse_dc_version` normalizes any stray
# whitespace or pre-release suffixes downstream.
_MANIFEST_VERSION_RE = re.compile(r"<version>\s*([^<\s][^<]*?)\s*</version>")


class ConfluenceClient(DCVersionProbe):
    """Base client for Confluence API interactions."""

    def __init__(self, config: ConfluenceConfig | None = None) -> None:
        """Initialize the Confluence client with given or environment config.

        Args:
            config: Configuration for Confluence client. If None, will load from
                environment.

        Raises:
            ValueError: If configuration is invalid or environment variables are missing
            MCPAtlassianAuthenticationError: If OAuth authentication fails
        """
        self.config = config or ConfluenceConfig.from_env()

        # Lazily probed DC version cache. The DCVersionProbe mixin declares a
        # class-level ``_dc_version = None`` attribute; we set an instance
        # attribute here so each client has its own cache slot and so a
        # successful probe on one client does not leak across instances.
        self._dc_version: str | None = None
        self._dc_version_probed: bool = False

        # Initialize the Confluence client based on auth type
        if self.config.auth_type == "oauth":
            if not self.config.oauth_config:
                error_msg = "OAuth authentication requires oauth_config"
                raise ValueError(error_msg)

            # Determine Cloud vs Data Center OAuth
            is_dc_oauth = (
                getattr(self.config.oauth_config, "is_data_center", False) is True
            )

            if not is_dc_oauth and not self.config.oauth_config.cloud_id:
                error_msg = "Cloud OAuth authentication requires a valid cloud_id"
                raise ValueError(error_msg)

            # Create a session for OAuth
            session = Session()

            # Configure the session with OAuth authentication
            if not configure_oauth_session(session, self.config.oauth_config):
                error_msg = "Failed to configure OAuth session"
                raise MCPAtlassianAuthenticationError(error_msg)

            if is_dc_oauth:
                # Data Center: use the instance URL directly
                api_url = self.config.url
                is_cloud = False
            else:
                # Cloud: use the Atlassian Cloud API URL
                api_url = f"https://api.atlassian.com/ex/confluence/{self.config.oauth_config.cloud_id}"
                is_cloud = True

            # Initialize Confluence with the session
            self.confluence = Confluence(
                url=api_url,
                session=session,
                cloud=is_cloud,
                verify_ssl=self.config.ssl_verify,
                timeout=self.config.timeout,
            )
        elif self.config.auth_type == "pat":
            logger.debug(
                f"Initializing Confluence client with Token (PAT) auth. "
                f"URL: {self.config.url}, "
                f"Token (masked): {mask_sensitive(str(self.config.personal_token))}"
            )
            self.confluence = Confluence(
                url=self.config.url,
                token=self.config.personal_token,
                cloud=self.config.is_cloud,
                verify_ssl=self.config.ssl_verify,
                timeout=self.config.timeout,
            )
        else:  # basic auth
            logger.debug(
                f"Initializing Confluence client with Basic auth. "
                f"URL: {self.config.url}, Username: {self.config.username}, "
                f"API Token present: {bool(self.config.api_token)}, "
                f"Is Cloud: {self.config.is_cloud}"
            )
            self.confluence = Confluence(
                url=self.config.url,
                username=self.config.username,
                password=self.config.api_token,  # API token is used as password
                cloud=self.config.is_cloud,
                verify_ssl=self.config.ssl_verify,
                timeout=self.config.timeout,
            )
            logger.debug(
                f"Confluence client initialized. "
                f"Session headers (Authorization masked): "
                f"{get_masked_session_headers(dict(self.confluence._session.headers))}"
            )

        # Disable trust_env for PAT and OAuth to prevent .netrc from overriding
        # explicit credentials (#860). Basic auth can safely use .netrc.
        if self.config.auth_type in ("pat", "oauth"):
            self.confluence._session.trust_env = False

        # Configure SSL verification using the shared utility
        configure_ssl_verification(
            service_name="Confluence",
            url=self.config.url,
            session=self.confluence._session,
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
            self.confluence._session.proxies.update(proxies)
            for k, v in proxies.items():
                log_config_param(
                    logger, "Confluence", f"{k.upper()}_PROXY", v, sensitive=True
                )
        if self.config.no_proxy and isinstance(self.config.no_proxy, str):
            os.environ["NO_PROXY"] = self.config.no_proxy
            log_config_param(logger, "Confluence", "NO_PROXY", self.config.no_proxy)

        # Apply custom headers if configured
        if self.config.custom_headers:
            self._apply_custom_headers()

        # Import here to avoid circular imports
        from ..preprocessing.confluence import ConfluencePreprocessor

        self.preprocessor = ConfluencePreprocessor(base_url=self.config.url)

        # Test authentication during initialization (in debug mode only)
        if logger.isEnabledFor(logging.DEBUG):
            try:
                self._validate_authentication()
            except MCPAtlassianAuthenticationError:
                logger.warning(
                    "Authentication validation failed during client initialization - "
                    "continuing anyway"
                )

    def _validate_authentication(self) -> None:
        """Validate authentication by making a simple API call."""
        try:
            logger.debug(
                "Testing Confluence authentication by making a simple API call..."
            )
            # Make a simple API call to test authentication
            spaces = self.confluence.get_all_spaces(start=0, limit=1)
            if spaces is not None:
                logger.info(
                    f"Confluence authentication successful. "
                    f"API call returned {len(spaces.get('results', []))} spaces."
                )
            else:
                logger.warning(
                    "Confluence authentication test returned None - "
                    "this may indicate an issue"
                )
        except RequestsConnectionError as e:
            error_msg = (
                f"Could not connect to Confluence at {self.config.url}. "
                "Check that CONFLUENCE_URL is correct and the instance is reachable."
            )
            logger.error(error_msg)
            raise MCPAtlassianAuthenticationError(error_msg) from e
        except Exception as e:
            error_msg = f"Confluence authentication validation failed: {e}"
            logger.error(error_msg)
            logger.debug(
                f"Authentication headers during failure: "
                f"{get_masked_session_headers(dict(self.confluence._session.headers))}"
            )
            raise MCPAtlassianAuthenticationError(error_msg) from e

    def _apply_custom_headers(self) -> None:
        """Apply custom headers to the Confluence session."""
        if not self.config.custom_headers:
            return

        logger.debug(
            f"Applying {len(self.config.custom_headers)} custom headers to Confluence session"
        )
        for header_name, header_value in self.config.custom_headers.items():
            self.confluence._session.headers[header_name] = header_value
            logger.debug(f"Applied custom header: {header_name}")

    def _process_html_content(
        self, html_content: str, space_key: str
    ) -> tuple[str, str]:
        """Process HTML content into both HTML and markdown formats.

        Args:
            html_content: Raw HTML content from Confluence
            space_key: The key of the space containing the content

        Returns:
            Tuple of (processed_html, processed_markdown)
        """
        return self.preprocessor.process_html_content(
            html_content, space_key, self.confluence
        )

    def get_dc_version(self) -> str | None:
        """Return the cached Confluence DC version, probing lazily on first call.

        The probe is issued at most once per ``ConfluenceClient`` instance: the
        first caller issues the HTTP request, subsequent callers reuse the
        cached value on ``self._dc_version``. Both success (a version string)
        and failure (``None``) are cached so a transient network error or a
        404 on the manifest endpoint does not trigger a new probe on every
        tool invocation.

        Probe strategy (matches the design's ``DCVersionProbe`` contract):

        1. **Primary** ``GET /rest/applinks/latest/manifest``. Confluence DC
           publishes an Atlassian Application Links manifest whose XML body
           contains a ``<version>...</version>`` element (for example
           ``<version>8.5.0</version>``). The XML shape is stable across
           supported DC versions.
        2. **Fallback** ``GET /rest/applinks/1.0/manifest``. Older DC
           instances expose the manifest under the ``1.0`` path. We try this
           only if the ``latest`` probe fails (non-2xx, connection error, or
           XML without a parseable ``<version>`` element).
        3. **Failure** Any non-2xx response, connection error, or missing
           ``<version>`` element caches ``None``. :func:`check_dc_version`
           treats ``None`` as indeterminate so the DC-gated tool falls
           through to the upstream call, which can then be mapped to
           ``dc_version_unknown`` on the 404/501 path per Requirement 45.3.

        The Confluence ``/rest/api/user/current`` endpoint is not a version
        source (its response does not carry a server version field), so we
        do not treat it as a fallback for *version detection*. It is still
        available elsewhere as a cheap auth-validation probe.

        Returns:
            The DC version string (for example ``"8.5.0"`` or
            ``"5.4-SNAPSHOT"``) on successful probe, or ``None`` when the
            manifest endpoint is unreachable, returns a non-2xx status, or
            omits a ``<version>`` element. The value is cached for the
            lifetime of this client instance.
        """
        if self._dc_version_probed:
            return self._dc_version

        # Mark as probed up-front so a raising probe call does not cause an
        # unbounded retry loop on every subsequent tool invocation. The
        # cached value stays ``None`` in that case.
        self._dc_version_probed = True

        base_url = (self.config.url or "").rstrip("/")
        session = self.confluence._session
        for path in ("/rest/applinks/latest/manifest", "/rest/applinks/1.0/manifest"):
            url = f"{base_url}{path}"
            try:
                response = session.get(url, timeout=self.config.timeout)
            except Exception as exc:  # noqa: BLE001 — any HTTP/transport error
                logger.debug(
                    "Confluence DC version probe via %s failed: %s", path, exc
                )
                continue

            if response.status_code >= 400:
                logger.debug(
                    "Confluence DC version probe via %s returned HTTP %s",
                    path,
                    response.status_code,
                )
                continue

            try:
                body = response.text or ""
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "Confluence DC version probe via %s could not read body: %s",
                    path,
                    exc,
                )
                continue

            match = _MANIFEST_VERSION_RE.search(body)
            if match is None:
                logger.debug(
                    "Confluence DC version probe via %s returned no <version> element",
                    path,
                )
                continue

            version = match.group(1).strip()
            if not version:
                continue

            self._dc_version = version
            logger.debug("Detected Confluence DC version %s via %s", version, path)
            return version

        # Both probes failed; keep ``_dc_version`` at ``None`` so
        # ``check_dc_version`` falls through to ``dc_version_unknown``.
        self._dc_version = None
        return None
