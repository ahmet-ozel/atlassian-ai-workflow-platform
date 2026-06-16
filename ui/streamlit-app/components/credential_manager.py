"""Streamlit per-session credential lifecycle manager.

This component is a strict in-memory credential store: every value
the user types lives **only** inside ``st.session_state`` for the
lifetime of the active browser tab. Nothing is ever persisted to
disk, to a database, to Vault, or to a log line. That hard rule is
the difference between this component and the legacy
``components.credential_form`` (which writes a Vault reference and
optionally PIN-encrypts a Z7 persistent path).

Lifecycle contract
------------------
* The store lives at
  ``st.session_state["_credential_manager_state"]``. Tokens never
  leave this dict; not via logging (we mask email + emit no token
  bytes), not via cookies (we never write one), not via the disk
  (no ``open()`` calls anywhere in this module).
* Every interaction (store / get / validate / render)
  touches ``last_activity``. When 60 minutes pass with no touch,
  the next call to :meth:`CredentialManager.is_expired` returns
  ``True`` and :meth:`CredentialManager.clear_all` wipes the dict.
* The warning text rendered by
  :func:`render_credential_warning` is the verbatim Turkish copy
  shown to users: *"Bu bilgiler yalnızca bu tarayıcı
  sekmesinde, bu oturum süresince saklanır. Sekme kapatıldığında
  veya 60 dakika işlem yapılmadığında otomatik silinir."*
* :meth:`CredentialManager.validate` issues a single
  authenticated request through an injectable validator. The default
  validator probes the selected Atlassian surface directly, so a green
  state means that service accepted the token rather than only MCP
  health responding. Failures keep the credential stored but mark
  ``is_valid=False`` so the UI can surface the problem inline.
* :meth:`CredentialManager.get_auth_header` returns the
  ``Authorization: Basic ...`` value callers should attach to MCP
  requests. The plain token never appears in the returned mapping
  beyond the base64-encoded auth value, and the manager exposes no
  helper that echoes raw tokens back into the page.
* :func:`render_logout_button` clears the entire
  manager state and returns ``True`` when the user pressed it; the
  caller redirects (typically ``st.switch_page("pages/0_credentials.py")``).

The component is split into a pure :class:`CredentialManager` class
(unit-testable without Streamlit) and a thin set of ``render_*``
helpers that drive ``st.session_state``. Tests can drive the class
through a fake ``state`` dict and a deterministic ``now`` clock -
matching the seam pattern used by ``components.dept_switcher``.
"""

from __future__ import annotations

import base64
from http.cookies import SimpleCookie
import json
import logging
import os
import secrets
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Callable, Final, MutableMapping
from urllib.parse import quote, unquote, urlparse

import streamlit as st

__all__ = [
    "CREDENTIAL_WARNING_TEXT",
    "CredentialManager",
    "StoredCredential",
    "render_credential_manager",
    "render_credential_warning",
    "render_logout_button",
    "restore_cached_credentials",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Verbatim warning copy shown to users. The string
#: is a module-level constant so tests can
#: assert exact equality against it.
CREDENTIAL_WARNING_TEXT: Final[str] = (
    "Bu bilgiler yalnızca bu tarayıcı sekmesinde, bu oturum süresince "
    "saklanır. Sekme kapatıldığında veya 60 dakika işlem yapılmadığında "
    "otomatik silinir."
)

#: The single ``st.session_state`` key under which every credential
#: lives. Bundling everything into one namespaced dict keeps the
#: full-clear logic O(1) - :meth:`CredentialManager.clear_all` simply
#: drops this key - and prevents accidental key collisions with
#: other components.
_STATE_KEY: Final[str] = "_credential_manager_state"

_RESTORE_COOKIE_NAME: Final[str] = "streamlit_credential_session"
_RESTORE_COOKIE_TTL_DAYS: Final[int] = 1
_RESTORE_CACHE: Final[dict[str, dict[str, Any]]] = {}


@st.cache_resource(show_spinner=False)
def _restore_cache() -> dict[str, dict[str, Any]]:
    return _RESTORE_CACHE

#: Inactivity threshold in seconds: 60 minutes.
_SESSION_TIMEOUT_SECONDS: Final[int] = 60 * 60

#: Atlassian services this manager understands. The Atlassian Cloud
#: Basic-auth scheme (``email:api_token``) is identical across all
#: three so a single ``StoredCredential`` shape covers the lot.
_SUPPORTED_SERVICES: Final[tuple[str, ...]] = ("jira", "confluence", "bitbucket")

_DEFAULT_SERVICE_URLS: Final[dict[str, str]] = {
    "jira": "https://your-company.atlassian.net",
    "confluence": "https://your-company.atlassian.net/wiki",
    "bitbucket": "https://bitbucket.org",
}

_DEFAULT_DC_SERVICE_URLS: Final[dict[str, str]] = {
    "jira": "https://jira.your-company.com",
    "confluence": "https://confluence.your-company.com",
    "bitbucket": "https://bitbucket.your-company.com",
}

_SERVICE_URL_ENV_KEYS: Final[dict[str, str]] = {
    "jira": "JIRA_URL",
    "confluence": "CONFLUENCE_URL",
    "bitbucket": "BITBUCKET_URL",
}

_DEPLOYMENTS: Final[dict[str, str]] = {
    "Cloud": "cloud",
    "Server/Data Center": "server",
}

_DEPLOYMENT_LABELS: Final[dict[str, str]] = {
    "cloud": "Cloud",
    "server": "Local/DC",
}

#: Header name expected by the Streamlit page slot that owns the
#: credential entry UI. Used by :func:`render_logout_button` to
#: drive the post-logout redirect.
_CREDENTIAL_PAGE_PATH: Final[str] = "pages/0_credentials.py"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StoredCredential:
    """One Atlassian credential held in session_state.

    Attributes:
        service: Atlassian surface this credential targets - one of
            :data:`_SUPPORTED_SERVICES`.
        email: Atlassian account email. Never logged in full; the
            manager masks it to the first three characters when it
            has to surface a hint.
        api_token: Plain Atlassian API token. **Never** written to a
            log line, never serialized to disk, never echoed into a
            response body. Lives strictly inside session_state until
            the session expires or the user logs out.
        stored_at: Epoch seconds when the credential was first
            stored. Drives the "issued N minutes ago" hint in the
            UI; reset on every successful re-store.
        last_validated_at: Epoch seconds of the last successful
            :meth:`CredentialManager.validate` call, or ``None`` if
            the credential has never been validated. ``None`` is
            also the default for credentials whose latest validation
            attempt failed (``is_valid`` then carries ``False``).
        is_valid: Tri-state validity flag.
            ``None`` - not yet validated;
            ``True`` - last validate() returned ok;
            ``False`` - last validate() returned an auth/network
            failure. The token is **kept** in either case so the
            user can retry without retyping; the UI uses the flag
            to decide whether to surface a red error banner.
    """

    service: str
    email: str
    api_token: str
    stored_at: float
    url: str = ""
    workspace: str = ""
    deployment: str = "cloud"
    last_validated_at: float | None = None
    is_valid: bool | None = None

    def masked_email(self) -> str:
        """Return a log-safe rendering of ``email``.

        The first three characters are kept, the rest is collapsed
        to ``***`` plus the domain suffix. Never used for auth - the
        original ``email`` field is what builds the Authorization
        header.
        """
        if "@" not in self.email:
            return self.email[:3] + "***"
        local, _, domain = self.email.partition("@")
        return f"{local[:3]}***@{domain}"


# ---------------------------------------------------------------------------
# Validator type
# ---------------------------------------------------------------------------


#: Callable signature for the credential validator.
#:
#: A validator receives the service name plus the plain credential
#: pair and returns ``(ok, error_message)``. Returning ``ok=True``
#: with a ``None`` message is the success path; ``ok=False`` with a
#: short human-readable string is the failure path. The default
#: validator (:func:`_default_validator`) talks to the requested
#: Atlassian service directly.
CredentialValidator = Callable[[str, str, str], tuple[bool, str | None]]


def _is_bitbucket_cloud_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host in {"bitbucket.org", "www.bitbucket.org", "api.bitbucket.org"}


def _bitbucket_cloud_api_base(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower() == "api.bitbucket.org":
        return f"{parsed.scheme or 'https'}://api.bitbucket.org"
    return "https://api.bitbucket.org"


def _runtime_deployment() -> str:
    value = os.environ.get("ATLASSIAN_DEPLOYMENT", "cloud").strip().lower()
    if value in {"server", "dc", "local", "local-dc", "datacenter", "data-center"}:
        return "server"
    return "cloud"


def _default_urls_for_deployment(deployment: str) -> dict[str, str]:
    fallback = (
        _DEFAULT_SERVICE_URLS if deployment == "cloud" else _DEFAULT_DC_SERVICE_URLS
    )
    urls = dict(fallback)
    for service, env_key in _SERVICE_URL_ENV_KEYS.items():
        value = os.environ.get(env_key, "").strip().rstrip("/")
        if value:
            urls[service] = value
    return urls


def _basic_auth_headers(email: str, api_token: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    raw = f"{email}:{api_token}".encode("utf-8")
    headers["Authorization"] = f"Basic {base64.b64encode(raw).decode('ascii')}"
    return headers


def _bearer_auth_headers(api_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {api_token.strip()}",
    }


def _bitbucket_cloud_headers(email: str, api_token: str) -> dict[str, str]:
    # Bitbucket Cloud authenticates with account email + Atlassian API token
    # via Basic auth. Server/DC uses Personal Access Token instead.
    return _basic_auth_headers(email, api_token)


def _confluence_cloud_current_user_endpoint(url: str) -> str:
    base = url.rstrip("/")
    path = urlparse(base).path.rstrip("/")
    if path == "/wiki" or path.startswith("/wiki/"):
        return f"{base}/rest/api/user/current"
    return f"{base}/wiki/rest/api/user/current"


def _validate_atlassian_credential(
    httpx_module: Any,
    service: str,
    email: str,
    api_token: str,
    credential: "StoredCredential | None",
) -> tuple[bool, str | None]:
    deployment = str(getattr(credential, "deployment", "cloud") or "cloud").lower()
    defaults = _default_urls_for_deployment(deployment)
    url = str(getattr(credential, "url", "") or defaults[service]).strip()
    label = service.title()

    if deployment == "server":
        if service == "jira":
            endpoint = f"{url.rstrip('/')}/rest/api/2/myself"
        else:
            endpoint = f"{url.rstrip('/')}/rest/api/user/current"
        headers = _bearer_auth_headers(api_token)
    else:
        if not email.strip():
            return False, f"{label} Cloud icin e-posta zorunlu."
        if service == "jira":
            endpoint = f"{url.rstrip('/')}/rest/api/3/myself"
        else:
            endpoint = _confluence_cloud_current_user_endpoint(url)
        headers = _basic_auth_headers(email, api_token)

    request_error = getattr(httpx_module, "RequestError", Exception)
    try:
        with httpx_module.Client(timeout=8.0) as client:
            resp = client.get(endpoint, headers=headers)
    except request_error as exc:
        return False, f"{label} API'ye ulasilamadi: {exc}"

    if 200 <= resp.status_code < 300:
        return True, None
    if resp.status_code in (401, 403):
        return (
            False,
            f"{label} credential reddedildi (HTTP {resp.status_code}). "
            "Token gecersiz, suresi dolmus veya gerekli izin/scope yok.",
        )
    if resp.status_code == 404:
        return (
            False,
            f"{label} URL bulunamadi veya bu kullanicinin yetkisi yok (HTTP 404).",
        )
    return False, f"{label} dogrulama hatasi (HTTP {resp.status_code})."


def _validate_bitbucket_credential(
    httpx_module: Any,
    email: str,
    api_token: str,
    credential: "StoredCredential | None",
) -> tuple[bool, str | None]:
    deployment = str(getattr(credential, "deployment", "cloud") or "cloud").lower()
    defaults = _default_urls_for_deployment(deployment)
    url = str(getattr(credential, "url", "") or defaults["bitbucket"]).strip()
    workspace = str(getattr(credential, "workspace", "") or "").strip().strip("/")

    if deployment == "server" or not _is_bitbucket_cloud_url(url):
        auth_only_probe = False
        endpoint = f"{url.rstrip('/')}/rest/api/1.0/projects?limit=1"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {api_token.strip()}",
        }
    else:
        api_base = _bitbucket_cloud_api_base(url)
        if workspace:
            auth_only_probe = False
            endpoint = f"{api_base}/2.0/repositories/{quote(workspace)}?pagelen=1"
        else:
            auth_only_probe = True
            endpoint = f"{api_base}/2.0/user"
        headers = _bitbucket_cloud_headers(email, api_token)

    request_error = getattr(httpx_module, "RequestError", Exception)
    try:
        with httpx_module.Client(timeout=8.0) as client:
            resp = client.get(endpoint, headers=headers)
    except request_error as exc:
        return False, f"Bitbucket API'ye ulasilamadi: {exc}"

    if 200 <= resp.status_code < 300:
        return True, None
    if resp.status_code in (401, 403):
        if auth_only_probe:
            return (
                False,
                "Bitbucket Cloud auth reddedildi "
                f"(/2.0/user HTTP {resp.status_code}). Curl ile ayni endpoint "
                "calisiyorsa Streamlit'e girilen username/token veya .env degeri "
                "farklidir.",
            )
        return (
            False,
            "Bitbucket credential reddedildi "
            f"(HTTP {resp.status_code}). Token gecersiz, suresi dolmus "
            "veya gerekli Bitbucket scope yok.",
        )
    if resp.status_code == 404 and workspace:
        return (
            False,
            f"Bitbucket workspace bulunamadi veya yetki yok (HTTP 404): {workspace}",
        )
    return False, f"Bitbucket dogrulama hatasi (HTTP {resp.status_code})."


def _default_validator(
    service: str,
    email: str,
    api_token: str,
    *,
    credential: "StoredCredential | None" = None,
) -> tuple[bool, str | None]:
    """Default validator that probes the requested Atlassian API directly."""

    try:  # pragma: no cover - exercised in integration only.
        import httpx  # type: ignore[import-not-found]
    except ImportError:
        return False, "httpx kütüphanesi yok; credential doğrulanamıyor."

    if service == "bitbucket":
        return _validate_bitbucket_credential(
            httpx_module=httpx,
            email=email,
            api_token=api_token,
            credential=credential,
        )

    if service in {"jira", "confluence"}:
        return _validate_atlassian_credential(
            httpx_module=httpx,
            service=service,
            email=email,
            api_token=api_token,
            credential=credential,
        )

    return False, f"Bilinmeyen servis: {service}"


# ---------------------------------------------------------------------------
# CredentialManager - pure state-machine slice
# ---------------------------------------------------------------------------


def _restore_cookie_key() -> str | None:
    raw_value: str | None = None
    try:
        query_value = st.query_params.get("credential_session")
        if isinstance(query_value, list):
            query_value = query_value[0] if query_value else None
        if isinstance(query_value, str) and query_value:
            raw_value = query_value
    except Exception:  # noqa: BLE001 - continue with cookie fallbacks.
        raw_value = None
    try:
        context = getattr(st, "context", None)
        cookies = getattr(context, "cookies", None)
        if not raw_value and cookies is not None:
            raw_value = cookies.get(_RESTORE_COOKIE_NAME)
        if not raw_value:
            headers = getattr(context, "headers", None)
            cookie_header = headers.get("Cookie") if headers is not None else None
            if not cookie_header and headers is not None:
                cookie_header = headers.get("cookie")
            if cookie_header:
                parsed = SimpleCookie()
                parsed.load(cookie_header)
                morsel = parsed.get(_RESTORE_COOKIE_NAME)
                raw_value = morsel.value if morsel is not None else None
    except Exception:  # noqa: BLE001 - fallback to component reader below.
        raw_value = None
    if not raw_value:
        reader = st.session_state.get("_cookie_reader")
        if reader is None:
            return None
        try:
            raw_value = reader(_RESTORE_COOKIE_NAME)
        except Exception:  # noqa: BLE001 - cookie bridge failures are non-fatal.
            return None
    if not raw_value:
        return None
    raw_value = unquote(str(raw_value).strip())
    try:
        from components.cookie_manager import _get_secret, verify_cookie

        return verify_cookie(raw_value, _get_secret())
    except Exception:  # noqa: BLE001
        return None


def _write_restore_cookie(key: str) -> None:
    writer = st.session_state.get("_cookie_writer")
    try:
        from components.cookie_manager import _get_secret, sign_cookie

        signed_value = sign_cookie(key, _get_secret())
    except Exception:  # noqa: BLE001 - credentials still remain in session_state.
        return
    if writer is not None:
        try:
            writer(
                _RESTORE_COOKIE_NAME,
                signed_value,
                ttl_days=_RESTORE_COOKIE_TTL_DAYS,
            )
        except Exception:  # noqa: BLE001 - fallback JS cookie write is best-effort.
            pass
    try:
        import streamlit.components.v1 as components

        ttl_seconds = _RESTORE_COOKIE_TTL_DAYS * 86400
        components.html(
            """
            <script>
            (function () {
              const name = %s;
              const value = encodeURIComponent(%s);
              document.cookie = name + "=" + value
                + "; Max-Age=%d; Path=/; SameSite=Lax";
            })();
            </script>
            """
            % (
                json.dumps(_RESTORE_COOKIE_NAME),
                json.dumps(signed_value),
                ttl_seconds,
            ),
            height=0,
        )
    except Exception:  # noqa: BLE001 - direct page reload can still use session_state.
        return


def _clear_restore_cookie() -> None:
    reader = st.session_state.get("_cookie_reader")
    delete = getattr(reader, "delete", None)
    if callable(delete):
        try:
            delete(_RESTORE_COOKIE_NAME)
        except Exception:  # noqa: BLE001
            pass
    try:
        import streamlit.components.v1 as components

        components.html(
            """
            <script>
            document.cookie = %s + "=; Max-Age=0; Path=/; SameSite=Lax";
            </script>
            """
            % json.dumps(_RESTORE_COOKIE_NAME),
            height=0,
        )
    except Exception:  # noqa: BLE001
        return


def _copy_restore_credentials(bucket: dict[str, Any]) -> dict[str, StoredCredential]:
    credentials = bucket.get("credentials")
    if not isinstance(credentials, dict):
        return {}
    return {
        service: replace(credential)
        for service, credential in credentials.items()
        if isinstance(service, str) and isinstance(credential, StoredCredential)
    }


def _sync_restore_cache(bucket: dict[str, Any]) -> None:
    key = bucket.get("restore_key")
    if not isinstance(key, str) or not key:
        return
    credentials = _copy_restore_credentials(bucket)
    if not credentials:
        _restore_cache().pop(key, None)
        return
    _restore_cache()[key] = {
        "credentials": credentials,
        "last_activity": float(bucket.get("last_activity", time.monotonic())),
        "session_started_at": float(bucket.get("session_started_at", time.monotonic())),
    }


def _cache_credentials(bucket: dict[str, Any]) -> None:
    key = bucket.get("restore_key")
    if not isinstance(key, str) or not key:
        key = secrets.token_urlsafe(32)
        bucket["restore_key"] = key
    _sync_restore_cache(bucket)
    _write_restore_cookie(key)
    _LOG.info(
        "credential_restore_cache_written",
        extra={
            "restore_key_prefix": key[:8],
            "services": sorted(_copy_restore_credentials(bucket)),
        },
    )


def _restore_credentials_from_cache(manager: "CredentialManager") -> bool:
    existing = manager.state.get(_STATE_KEY)
    if isinstance(existing, dict) and existing.get("credentials"):
        return False

    key = _restore_cookie_key()
    if not key:
        _LOG.info("credential_restore_cookie_missing")
        return False
    cached = _restore_cache().get(key)
    if not isinstance(cached, dict):
        _LOG.info("credential_restore_cache_miss", extra={"restore_key_prefix": key[:8]})
        return False

    now = manager.now()
    last_activity = cached.get("last_activity")
    if not isinstance(last_activity, (int, float)):
        _restore_cache().pop(key, None)
        return False
    if (now - float(last_activity)) >= _SESSION_TIMEOUT_SECONDS:
        _restore_cache().pop(key, None)
        _clear_restore_cookie()
        _LOG.info("credential_restore_cache_expired", extra={"restore_key_prefix": key[:8]})
        return False

    credentials = _copy_restore_credentials(cached)
    if not credentials:
        _restore_cache().pop(key, None)
        _LOG.info("credential_restore_cache_empty", extra={"restore_key_prefix": key[:8]})
        return False

    manager.state[_STATE_KEY] = {
        "credentials": credentials,
        "last_activity": now,
        "session_started_at": float(cached.get("session_started_at", now)),
        "restore_key": key,
    }
    _sync_restore_cache(manager.state[_STATE_KEY])
    _LOG.info(
        "credential_restore_cache_restored",
        extra={"restore_key_prefix": key[:8], "services": sorted(credentials)},
    )
    return True


def restore_cached_credentials(state: MutableMapping[str, Any] | None = None) -> bool:
    """Restore direct Chat reloads using an opaque signed cookie key."""

    manager = CredentialManager(
        state=state if state is not None else st.session_state,
        now=time.monotonic,
        validator=_default_validator,
    )
    return _restore_credentials_from_cache(manager)


@dataclass
class CredentialManager:
    """In-memory credential lifecycle manager.

    The class is intentionally split off from the ``render_*``
    helpers so unit tests can drive it without standing up
    Streamlit. The only collaborators are:

    * a ``state`` mapping (defaults to ``st.session_state``) -
      where the credential dict lives;
    * a ``now`` callable (defaults to ``time.monotonic``) - used to
      compute the inactivity window deterministically in tests;
    * an optional ``validator`` (defaults to
      :func:`_default_validator`) that performs the MCP test
      request for :meth:`validate`.
    """

    state: MutableMapping[str, Any] = field(default_factory=dict)
    now: Callable[[], float] = field(default=time.monotonic)
    validator: CredentialValidator = field(default=_default_validator)

    # ---- internal -------------------------------------------------------

    def _ensure_state(self) -> dict[str, Any]:
        """Return the namespaced state dict, creating it if missing."""
        bucket = self.state.get(_STATE_KEY)
        if not isinstance(bucket, dict):
            bucket = {
                "credentials": {},
                "last_activity": self.now(),
                "session_started_at": self.now(),
            }
            self.state[_STATE_KEY] = bucket
        # Required keys may have been mutated by external code; defensively
        # fill them so a single broken interaction can't permanently brick
        # the manager.
        bucket.setdefault("credentials", {})
        bucket.setdefault("last_activity", self.now())
        bucket.setdefault("session_started_at", self.now())
        return bucket  # type: ignore[return-value]

    def _touch(self) -> None:
        """Refresh ``last_activity`` to the current clock value."""
        bucket = self._ensure_state()
        bucket["last_activity"] = self.now()
        _sync_restore_cache(bucket)

    # ---- timeout / clear -----------------------------------------------

    def is_expired(self) -> bool:
        """Return ``True`` when the inactivity threshold has been crossed.

        The check is read-only - it does **not** touch
        ``last_activity``. Callers that want to act on expiry should
        explicitly invoke :meth:`clear_all` after an ``is_expired()``
        positive. When no credentials have ever
        been stored the function returns ``False`` so a freshly-
        opened page does not immediately render an "expired" banner.
        """
        bucket = self.state.get(_STATE_KEY)
        if not isinstance(bucket, dict):
            return False
        last_activity = bucket.get("last_activity")
        if not isinstance(last_activity, (int, float)):
            return False
        return (self.now() - float(last_activity)) >= _SESSION_TIMEOUT_SECONDS

    def enforce_timeout(self) -> bool:
        """Clear state if expired; return ``True`` when a clear happened.

        Pages should call this at the top of their render loop so a
        long-idle tab cannot leak credentials into the next request.
        Returning a boolean lets the caller render an "session
        expired, please re-enter credentials" banner without having
        to re-derive the condition.
        """
        if self.is_expired():
            self.clear_all()
            return True
        return False

    def clear_all(self) -> None:
        """Drop every credential and reset the state bucket.

        The implementation removes the namespaced state dict
        outright rather than mutating it in place - the goal is to
        guarantee that no stale token byte stays referenced through
        a forgotten dict key. A fresh, empty bucket is reinstalled
        on the next interaction by :meth:`_ensure_state`.
        """
        if _STATE_KEY in self.state:
            # Iterate explicitly so any future per-credential cleanup hooks
            # have a stable place to land.
            bucket = self.state.get(_STATE_KEY)
            if isinstance(bucket, dict):
                restore_key = bucket.get("restore_key")
                if isinstance(restore_key, str):
                    _restore_cache().pop(restore_key, None)
                bucket.get("credentials", {}).clear()
            del self.state[_STATE_KEY]

    def delete(self, service: str) -> bool:
        """Delete one stored credential without touching the others."""
        if service not in _SUPPORTED_SERVICES:
            raise ValueError(f"Unknown service {service!r}; expected one of {_SUPPORTED_SERVICES}.")
        if self.enforce_timeout():
            return False
        bucket = self._ensure_state()
        credentials = bucket.get("credentials", {})
        if service not in credentials:
            return False
        del credentials[service]
        bucket["last_activity"] = self.now()
        if credentials:
            _sync_restore_cache(bucket)
        else:
            restore_key = bucket.get("restore_key")
            if isinstance(restore_key, str):
                _restore_cache().pop(restore_key, None)
            _clear_restore_cookie()
        _LOG.info("credential_deleted", extra={"service": service})
        return True

    # ---- store / get ----------------------------------------------------

    def store(
        self,
        service: str,
        email: str,
        api_token: str,
        *,
        url: str | None = None,
        workspace: str = "",
        deployment: str = "cloud",
    ) -> StoredCredential:
        """Persist a credential into session_state.

        Args:
            service: One of :data:`_SUPPORTED_SERVICES`.
            email: Atlassian account email; must contain ``@``.
            api_token: Atlassian API token; must be non-empty.

        Returns:
            The :class:`StoredCredential` now living in the state
            bucket. Validation status is left at ``None`` - call
            :meth:`validate` separately so the (potentially slow)
            HTTP request is not hidden inside the storage path.

        Raises:
            ValueError: when ``service`` is unknown, ``email`` is
                malformed, or ``api_token`` is empty. The error is
                surfaced inline in the UI; nothing is stored.
        """
        if service not in _SUPPORTED_SERVICES:
            raise ValueError(f"Unknown service {service!r}; expected one of {_SUPPORTED_SERVICES}.")
        clean_deployment = deployment.strip().lower()
        if clean_deployment not in {"cloud", "server"}:
            raise ValueError("Deployment cloud veya server olmalidir.")
        default_urls = (
            _DEFAULT_SERVICE_URLS if clean_deployment == "cloud" else _DEFAULT_DC_SERVICE_URLS
        )
        clean_url = (url or default_urls[service]).strip().rstrip("/")
        clean_email = email.strip()
        clean_workspace = workspace.strip().strip("/")
        if not (clean_url.startswith("https://") or clean_url.startswith("http://")):
            raise ValueError("Gecerli bir servis URL'i giriniz.")
        if clean_deployment == "cloud" and "@" not in clean_email:
            raise ValueError("Geçerli bir e-posta giriniz (örn: ad.soyad@firma.com).")
        if not api_token.strip():
            raise ValueError("API token veya Personal Access Token bos olamaz.")

        bucket = self._ensure_state()
        cred = StoredCredential(
            service=service,
            url=clean_url,
            email=clean_email,
            api_token=api_token.strip(),
            stored_at=self.now(),
            workspace=clean_workspace,
            deployment=clean_deployment,
            last_validated_at=None,
            is_valid=None,
        )
        bucket["credentials"][service] = cred
        bucket["last_activity"] = self.now()
        # Never log the token. Only the masked email + service
        # land in the structured log.
        _LOG.info(
            "credential_stored",
            extra={"service": service, "email_masked": cred.masked_email()},
        )
        return cred

    def get(self, service: str) -> StoredCredential | None:
        """Return the stored credential for ``service`` or ``None``.

        Calling this method counts as an interaction, so the
        inactivity timer is refreshed. When the session has already
        expired (the timer crossed 60 minutes before this call) the
        state is cleared **and** the function returns ``None`` - a
        caller may immediately re-prompt without an extra check.
        """
        if self.enforce_timeout():
            return None
        bucket = self._ensure_state()
        cred = bucket["credentials"].get(service)
        if cred is None:
            return None
        self._touch()
        return cred  # type: ignore[return-value]

    def get_active_services(self) -> list[str]:
        """Return the services that currently hold a credential.

        The list is preserved in :data:`_SUPPORTED_SERVICES` order so
        the UI can render a stable "configured services" indicator.
        Triggers the timeout check; an expired session yields ``[]``.
        """
        if self.enforce_timeout():
            return []
        bucket = self._ensure_state()
        active = bucket["credentials"].keys()
        return [s for s in _SUPPORTED_SERVICES if s in active]

    # ---- validation -----------------------------------------------------

    def validate(self, service: str) -> tuple[bool, str | None]:
        """Run the validator for ``service`` and update ``is_valid``.

        Returns ``(ok, error_message)``. Successful runs leave
        ``error_message`` at ``None`` and bump
        ``last_validated_at``; failures keep the credential stored
        so the user can retry without retyping. The HTTP request
        itself is delegated to :attr:`validator` - the default
        impl talks to MCP ``/healthz``.
        """
        cred = self.get(service)
        if cred is None:
            return False, "Credential bulunamadı."

        if self.validator is _default_validator:
            ok, err = _default_validator(
                service,
                cred.email,
                cred.api_token,
                credential=cred,
            )
        else:
            ok, err = self.validator(service, cred.email, cred.api_token)
        cred.is_valid = bool(ok)
        if ok:
            cred.last_validated_at = self.now()
        self._touch()
        return ok, err

    # ---- header builder -------------------------------------------------

    def get_auth_header(self, service: str) -> str | None:
        """Return the ``Authorization: Basic ...`` value for ``service``.

        The function constructs the standard Atlassian Cloud
        ``Basic <base64(email:token)>`` header on the fly so the
        plain token never lives outside :class:`StoredCredential`.
        Returns ``None`` when no credential is stored or the session
        has expired.
        """
        cred = self.get(service)
        if cred is None:
            return None
        token = base64.b64encode(
            f"{cred.email}:{cred.api_token}".encode("utf-8")
        ).decode("ascii")
        return f"Basic {token}"

    # ---- diagnostic snapshot (no secrets) -------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Return a token-free view of the state for UI rendering.

        The snapshot is what the dashboard / status panel renders
        next to the form; it omits ``api_token`` entirely and only
        keeps the masked email so a screenshot of the panel is safe
        to share. The structure is intentionally a plain dict so
        Streamlit's ``st.json`` widget can render it as-is during
        debugging.
        """
        bucket = self.state.get(_STATE_KEY)
        if not isinstance(bucket, dict):
            return {"credentials": {}, "active": False}
        out: dict[str, Any] = {
            "active": True,
            "session_started_at": bucket.get("session_started_at"),
            "last_activity": bucket.get("last_activity"),
            "expires_in_seconds": max(
                0,
                int(
                    _SESSION_TIMEOUT_SECONDS
                    - (self.now() - float(bucket.get("last_activity", self.now())))
                ),
            ),
            "credentials": {},
        }
        for svc, cred in bucket.get("credentials", {}).items():
            if not isinstance(cred, StoredCredential):
                continue
            entry = asdict(cred)
            # Drop the raw token before serialising; mask the email.
            entry.pop("api_token", None)
            entry["email"] = cred.masked_email()
            out["credentials"][svc] = entry
        return out


# ---------------------------------------------------------------------------
# Streamlit render helpers
# ---------------------------------------------------------------------------


def _get_manager(*, validator: CredentialValidator | None = None) -> CredentialManager:
    """Return a :class:`CredentialManager` bound to ``st.session_state``.

    The wall-clock seam uses :func:`time.monotonic` so the inactivity
    window keeps ticking even if the OS clock is adjusted while the
    page is open. The validator can be overridden so a Streamlit page
    fixture can inject a stub during AppTest runs.
    """
    return CredentialManager(
        state=st.session_state,
        now=time.monotonic,
        validator=validator or _default_validator,
    )


def render_credential_warning() -> None:
    """Render the verbatim credential warning text.

    Kept as a standalone function so pages that bundle the
    credential entry form alongside other UI (chat, task creator)
    can render the warning without invoking the full manager
    panel.
    """
    st.warning(CREDENTIAL_WARNING_TEXT)


def render_logout_button(*, key: str = "credential_manager_logout") -> bool:
    """Render the "Oturumu Kapat" button.

    Returns ``True`` after a successful logout so the caller can
    redirect - typically via ``st.switch_page("pages/0_credentials.py")``.
    The button clears every credential entry first; the redirect
    only fires when the clear succeeds, so a transient
    Streamlit reroute can't leave the manager in a half-cleared
    state.
    """
    manager = _get_manager()
    if st.button(" Oturumu Kapat", key=key, type="secondary"):
        manager.clear_all()
        _clear_restore_cookie()
        st.success("Oturum kapatıldı; tüm credential'lar bellekten silindi.")
        # Try the modern ``switch_page`` API first - it lands the user
        # on the canonical credential page. Streamlit ≥1.30
        # ships it; older runtimes fall back to ``st.rerun`` which at
        # least re-renders the page in its post-logout state.
        switch_page = getattr(st, "switch_page", None)
        if callable(switch_page):
            try:
                switch_page(_CREDENTIAL_PAGE_PATH)
            except Exception:  # noqa: BLE001 - rerun is the safe fallback
                rerun = getattr(st, "rerun", None) or getattr(st, "experimental_rerun", None)
                if callable(rerun):
                    rerun()
        else:  # pragma: no cover - legacy Streamlit fallback
            rerun = getattr(st, "rerun", None) or getattr(st, "experimental_rerun", None)
            if callable(rerun):
                rerun()
        return True
    return False


def _credential_status_label(entry: Mapping[str, Any]) -> str:
    is_valid = entry.get("is_valid")
    if is_valid is True:
        return "Dogrulandi"
    if is_valid is False:
        return "Reddedildi"
    return "Dogrulanmadi"


def _render_saved_credentials(
    manager: CredentialManager,
    snapshot: Mapping[str, Any],
) -> None:
    credentials = snapshot.get("credentials")
    st.subheader("Kayitli credentials")
    st.caption(
        "Token degerleri gosterilmez. Guncellemek icin ilgili servis formunu "
        "yeniden doldurup Bagla ve dogrula'ya basin."
    )
    if not isinstance(credentials, Mapping) or not credentials:
        st.info("Henuz kayitli Jira, Confluence veya Bitbucket credential yok.")
        return

    for service in _SUPPORTED_SERVICES:
        entry = credentials.get(service)
        if not isinstance(entry, Mapping):
            continue
        cols = st.columns([1.1, 2.0, 1.5, 0.6])
        cols[0].markdown(f"**{service.title()}**")
        cols[0].caption(_credential_status_label(entry))
        url = entry.get("url") or "-"
        email = entry.get("email") or "-"
        cols[1].markdown(f"`{url}`")
        cols[1].caption(f"Kullanici: {email}")
        deployment = entry.get("deployment") or "cloud"
        workspace = entry.get("workspace") or "-"
        if service == "bitbucket":
            cols[2].markdown(f"Deployment: `{deployment}`")
            cols[2].caption(f"Workspace/project: {workspace}")
        else:
            cols[2].markdown(f"Deployment: `{deployment}`")
            cols[2].caption("Workspace gerekmez")
        if cols[3].button("Sil", key=f"_cred_mgr_delete_{service}"):
            manager.delete(service)
            st.success(f"{service.title()} credential silindi.")
            rerun = getattr(st, "rerun", None) or getattr(st, "experimental_rerun", None)
            if callable(rerun):
                rerun()
        st.divider()


def _render_service_form(manager: CredentialManager, service: str) -> None:
    """Render one ``st.form`` per Atlassian service."""

    cred = manager.get(service)
    with st.form(f"_cred_mgr_{service}", clear_on_submit=True):
        service_label = service.title()
        st.markdown(f"##### {service_label}")
        if cred is not None:
            status = (
                " Doğrulandı"
                if cred.is_valid is True
                else (" Doğrulanmadı" if cred.is_valid is None else " Reddedildi")
            )
            st.caption(f"Mevcut: `{cred.masked_email()}` - {status}")
        deployment = _runtime_deployment()
        st.caption(f"Deployment: `{_DEPLOYMENT_LABELS[deployment]}`")
        is_cloud = deployment == "cloud"
        default_urls = _default_urls_for_deployment(deployment)
        email = ""
        if is_cloud:
            if service == "bitbucket":
                email = st.text_input(
                    "Bitbucket e-posta",
                    placeholder="your.email@company.com",
                    help=(
                        "Cloud icin Atlassian hesap e-postasi gerekir. "
                        "Jira, Confluence ve Bitbucket Cloud ayni hesap "
                        "e-postasi + API token ile dogrulanir."
                    ),
                    key=f"_cred_mgr_email_{service}",
                )
            else:
                email = st.text_input(
                    f"{service_label} e-posta",
                    placeholder="ad.soyad@firma.com",
                    help="Cloud icin Atlassian hesap e-postasi gerekir.",
                    key=f"_cred_mgr_email_{service}",
                )
        url = st.text_input(
            f"{service_label} URL",
            value=default_urls[service],
            help=(
                f"Ornek: {default_urls[service]}"
                if is_cloud
                else f"Server/DC URL ornegi: {default_urls[service]}"
            ),
            key=f"_cred_mgr_url_{service}_{deployment}",
        )
        workspace = ""
        if service == "bitbucket":
            workspace = st.text_input(
                (
                    "Bitbucket workspace (Cloud, opsiyonel)"
                    if is_cloud
                    else "Bitbucket project key (Server/DC opsiyonel)"
                ),
                placeholder="example_workspace" if is_cloud else "PROJ",
                help=(
                    "Opsiyonel. Repo URL'sindeki bitbucket.org/{workspace}/{repo} "
                    "bolumunden alinabilir; bos birakilirsa chat sorusunda "
                    "workspace/repo belirtin."
                    if is_cloud
                    else "Server/DC icin varsayilan project key opsiyoneldir."
                ),
                key=f"_cred_mgr_workspace_{service}",
            )
        if service == "bitbucket" and is_cloud:
            token_label = "Bitbucket API token"
            token_help = (
                "Atlassian API token girin. Jira, Confluence ve Bitbucket Cloud "
                "icin ayni ATATT token kullanilabilir; workspace access token "
                "veya Server/DC PAT kullanmayin."
            )
        elif is_cloud:
            token_label = f"{service_label} API token"
            token_help = (
                "Atlassian API token girin. Jira ve Confluence icin ayni token "
                "kullanilabilir."
            )
        else:
            token_label = f"{service_label} Personal Access Token"
            token_help = "Server/Data Center profilinden uretilen PAT girin."
        token = st.text_input(
            token_label,
            type="password",
            help=token_help,
            key=f"_cred_mgr_token_{service}",
        )
        submitted = st.form_submit_button(
            "Bağla ve doğrula", type="primary"
        )

    if not submitted:
        return

    try:
        manager.store(
            service,
            email=email,
            api_token=token,
            url=url,
            workspace=workspace,
            deployment=deployment,
        )
    except ValueError as exc:
        st.error(str(exc))
        return

    ok, err = manager.validate(service)
    _cache_credentials(manager._ensure_state())
    if ok:
        st.success(f"{service.title()} credential doğrulandı.")
    else:
        st.error(
            f"{service.title()} credential doğrulanamadı: "
            f"{err or 'bilinmeyen hata'}"
        )


def render_credential_manager(
    *,
    validator: CredentialValidator | None = None,
) -> None:
    """Render the full credential manager panel.

    The panel layout is:

    1. Warning banner.
    2. Per-service entry form (Jira / Confluence / Bitbucket).
    3. Status snapshot - masked email + validation state +
       remaining session window.
    4. Logout button.

    A page that just wants to render one piece (e.g. only the
    warning, or only the logout button) can call the standalone
    helpers; this function is the one-stop shop the credentials
    page (``pages/0_credentials.py``) wires up.
    """
    manager = _get_manager(validator=validator)
    _restore_credentials_from_cache(manager)

    # Enforce timeout on every render - a tab idle for over an hour
    # surfaces an "session expired" banner instead of leaking the old
    # credentials into a fresh request.
    if manager.enforce_timeout():
        st.warning(
            "Oturum süresi (60 dakika) doldu; tüm credential'lar silindi. "
            "Lütfen yeniden giriş yapın.",
            icon="⌛",
        )

    render_credential_warning()

    tabs = st.tabs([s.title() for s in _SUPPORTED_SERVICES])
    for tab, service in zip(tabs, _SUPPORTED_SERVICES):
        with tab:
            _render_service_form(manager, service)

    snapshot = manager.snapshot()
    _render_saved_credentials(manager, snapshot)
    st.divider()
    if snapshot.get("active"):
        with st.expander("Oturum durumu", expanded=False):
            st.json(snapshot)
    render_logout_button()
