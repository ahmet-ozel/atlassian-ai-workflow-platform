"""External provider HTTP probe with credential injection and caching.

This module implements :func:`probe_external`, the helper that turns a
``kind="external"`` manifest entry into an :class:`ExternalProbeResult`
by issuing a single HTTP request against the provider's probe endpoint.

Key behaviours
--------------
* **Credential header injection** (behavior 10.4): API keys are
  resolved from environment variables (``OPENAI_API_KEY``,
  ``ANTHROPIC_API_KEY``, ``FIRECRAWL_CLOUD_API_KEY``) or, when absent, from
  Vault paths (``secret/data/external/<provider>/api_key``). If no
  credential is found for a provider that requires one, the result
  status is ``"unauthorized"``.
* **30-second in-memory cache** (behavior 10.3): Probe results are
  cached per entry name with a 30 s TTL so rapid UI refreshes do not
  hammer external APIs. The cache is process-local and resets on
  restart.
* **Status mapping**: HTTP 200 → ``"ok"``, 401/403 → ``"unauthorized"``,
  429 → ``"rate_limited"``, timeout / connection error → ``"unreachable"``.
* **Audit + streak alerting** (behavior 10.7, audit and alarm wiring): Every
  failed probe emits an ``external_provider_probe_failed`` audit entry.
  When a provider accumulates 3 consecutive failures, a single
  ``external_provider_streak_alert`` audit entry is emitted (mirroring
  the ``health_streak_alert`` pattern from the lifecycle service). The
  streak counter resets on a successful probe.

Design references
-----------------
* design notes §rule 10 - External Provider Downtime Widget.
* implementation notes external probe wiring - External probe helper.
* implementation notes audit and alarm wiring - Audit + alarm.
* behaviors 10.3, 10.4, 10.7.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Literal, Mapping, Protocol
from uuid import uuid4

import httpx

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

ExternalProbeStatus = Literal[
    "ok",
    "unreachable",
    "unauthorized",
    "rate_limited",
]


@dataclass(frozen=True)
class ExternalProbeResult:
    """Single point-in-time probe observation for an external provider.

    Attributes
    ----------
    name:
        The manifest entry name (e.g. ``"openai"``, ``"vllm"``).
    status:
        Roll-up verdict: ``"ok"`` when the probe endpoint returned the
        expected status code, ``"unreachable"`` on connection/timeout
        failure, ``"unauthorized"`` when credentials are missing or the
        provider returned 401/403, ``"rate_limited"`` on HTTP 429.
    base_url:
        The resolved base URL that was probed.
    latency_ms:
        Round-trip time of the probe request in milliseconds, or
        ``None`` when the request could not be completed (timeout,
        connection refused).
    error:
        Short diagnostic string on failure, or ``None`` on success.
    last_probed_at:
        Unix timestamp (seconds since epoch) of the probe completion.
    """

    name: str
    status: ExternalProbeStatus
    base_url: str
    latency_ms: float | None
    error: str | None
    last_probed_at: float


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Per-request timeout for external probe calls.
_PROBE_TIMEOUT_SECONDS: Final[float] = 5.0

#: Cache TTL in seconds. Probe results are reused within this window.
_CACHE_TTL_SECONDS: Final[float] = 30.0

#: Mapping from provider name to the environment variable that holds
#: the API key. Only providers that require authentication are listed.
_CREDENTIAL_ENV_MAP: Final[dict[str, str]] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "firecrawl-cloud": "FIRECRAWL_CLOUD_API_KEY",
}

#: Vault KV-v2 path template for external provider API keys.
#: Used as a fallback when the environment variable is not set.
_CREDENTIAL_VAULT_PATH_TEMPLATE: Final[str] = (
    "secret/data/external/{provider}/api_key"
)

#: Mapping from provider name to the HTTP header used for auth.
_CREDENTIAL_HEADER_MAP: Final[dict[str, str]] = {
    "openai": "Authorization",
    "anthropic": "x-api-key",
    "firecrawl-cloud": "Authorization",
}

#: Providers whose Authorization header uses Bearer token format.
_BEARER_TOKEN_PROVIDERS: Final[set[str]] = {"openai", "firecrawl-cloud"}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@dataclass
class _CacheEntry:
    """In-memory cache slot for a single external probe result."""

    result: ExternalProbeResult
    expires_at: float


#: Process-local probe result cache. Keyed by entry name.
_cache: dict[str, _CacheEntry] = {}


def _get_cached(name: str) -> ExternalProbeResult | None:
    """Return cached result if still valid, else ``None``."""
    entry = _cache.get(name)
    if entry is None:
        return None
    if time.monotonic() > entry.expires_at:
        del _cache[name]
        return None
    return entry.result


def _set_cached(name: str, result: ExternalProbeResult) -> None:
    """Store a probe result in the cache with TTL."""
    _cache[name] = _CacheEntry(
        result=result,
        expires_at=time.monotonic() + _CACHE_TTL_SECONDS,
    )


def clear_cache() -> None:
    """Clear the entire probe cache. Useful for testing and manual re-probe."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------


class VaultCredentialReader:
    """Protocol-compatible Vault reader for external provider API keys.

    Injected at startup so the probe helper can resolve credentials
    from Vault when environment variables are absent. When ``None`` is
    passed to :func:`probe_external`, only env-var resolution is used.
    """

    def __init__(self, *, vault_client: Any) -> None:
        self._vault = vault_client

    async def read_api_key(self, provider: str) -> str | None:
        """Read the API key for ``provider`` from Vault KV-v2.

        Returns ``None`` if the path does not exist or the read fails.
        """
        if self._vault is None:
            return None
        try:
            path = _CREDENTIAL_VAULT_PATH_TEMPLATE.format(provider=provider)
            # Use the vault client's internal read method pattern
            response = await self._vault._client.get(
                f"{self._vault._addr}/v1/{path}",
                headers={"X-Vault-Token": self._vault._token},
            )
            if response.status_code != 200:
                return None
            payload = response.json()
            data = payload.get("data", {}).get("data", {})
            return data.get("value") or data.get("api_key")
        except Exception:
            return None


def _resolve_credential_from_env(
    provider: str,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve API key from environment variable for the given provider."""
    env_var = _CREDENTIAL_ENV_MAP.get(provider)
    if env_var is None:
        return None
    source = os.environ if env is None else env
    value = source.get(env_var, "").strip()
    return value if value else None


def _build_auth_headers(provider: str, api_key: str) -> dict[str, str]:
    """Build the authentication header dict for the given provider."""
    header_name = _CREDENTIAL_HEADER_MAP.get(provider, "Authorization")
    if provider in _BEARER_TOKEN_PROVIDERS:
        return {header_name: f"Bearer {api_key}"}
    # Anthropic uses a plain key value in x-api-key header
    return {header_name: api_key}


# ---------------------------------------------------------------------------
# Probe logic
# ---------------------------------------------------------------------------


def _resolve_base_url(
    entry: dict[str, Any],
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the base URL for an external manifest entry.

    Priority: environment variable (``base_url_env``) > default
    (``base_url_default``). Returns ``None`` if neither is available.
    """
    source = os.environ if env is None else env
    base_url_env = entry.get("base_url_env")
    if base_url_env:
        url = source.get(base_url_env, "").strip()
        if url:
            return url.rstrip("/")

    base_url_default = entry.get("base_url_default")
    if base_url_default:
        return base_url_default.rstrip("/")

    return None


def _map_status(
    status_code: int,
    expected_status: int,
) -> ExternalProbeStatus:
    """Map an HTTP response status code to an ExternalProbeStatus."""
    if status_code == expected_status:
        return "ok"
    if status_code in (401, 403):
        return "unauthorized"
    if status_code == 429:
        return "rate_limited"
    # Any other non-expected status is treated as unreachable
    return "unreachable"


def _sync_probe_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
) -> httpx.Response:
    """Run a one-shot provider probe with the sync transport as a fallback."""
    with httpx.Client(trust_env=True) as client:
        return client.request(
            method=method,
            url=url,
            headers=headers,
            timeout=httpx.Timeout(_PROBE_TIMEOUT_SECONDS),
        )


async def probe_external(
    entry: dict[str, Any],
    *,
    http_client: httpx.AsyncClient | None = None,
    vault_reader: VaultCredentialReader | None = None,
    bypass_cache: bool = False,
    env: Mapping[str, str] | None = None,
) -> ExternalProbeResult:
    """Probe a single external provider and return the result.

    Parameters
    ----------
    entry:
        A dict representing a ``kind="external"`` manifest entry.
        Expected keys: ``name``, ``base_url_env``, ``base_url_default``,
        ``probe_path``, ``probe_method``, ``probe_expected_status``.
    http_client:
        Optional pre-configured :class:`httpx.AsyncClient`. When
        ``None``, a temporary client is created for the request.
    vault_reader:
        Optional :class:`VaultCredentialReader` for Vault-based
        credential resolution. When ``None``, only environment
        variables are checked.
    bypass_cache:
        When ``True``, skip the cache lookup and force a fresh probe.
        The result is still written to the cache.

    Returns
    -------
    ExternalProbeResult
        The probe observation including status, latency, and any error.
    """
    name: str = entry.get("name", "unknown")

    # --- Cache check ---
    if not bypass_cache:
        cached = _get_cached(name)
        if cached is not None:
            return cached

    # --- Resolve base URL ---
    base_url = _resolve_base_url(entry, env=env)
    if base_url is None:
        result = ExternalProbeResult(
            name=name,
            status="unreachable",
            base_url="",
            latency_ms=None,
            error="No base URL configured (env var unset and no default)",
            last_probed_at=time.time(),
        )
        _set_cached(name, result)
        return result

    # --- Resolve credentials ---
    api_key = _resolve_credential_from_env(name, env=env)
    if api_key is None and vault_reader is not None:
        api_key = await vault_reader.read_api_key(name)

    # If provider requires auth but no key found → unauthorized
    if name in _CREDENTIAL_ENV_MAP and api_key is None:
        result = ExternalProbeResult(
            name=name,
            status="unauthorized",
            base_url=base_url,
            latency_ms=None,
            error=f"No API key found (checked env ${_CREDENTIAL_ENV_MAP[name]} and Vault)",
            last_probed_at=time.time(),
        )
        _set_cached(name, result)
        return result

    # --- Build request ---
    probe_path = entry.get("probe_path", "/")
    probe_method = entry.get("probe_method", "GET").upper()
    probe_expected_status = entry.get("probe_expected_status", 200)
    url = f"{base_url}{probe_path}"

    headers: dict[str, str] = {}
    if api_key is not None:
        headers.update(_build_auth_headers(name, api_key))

    # --- Execute probe ---
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(trust_env=True)
    try:
        start_time = time.monotonic()
        try:
            response = await client.request(
                method=probe_method,
                url=url,
                headers=headers,
                timeout=httpx.Timeout(_PROBE_TIMEOUT_SECONDS),
            )
            elapsed_ms = (time.monotonic() - start_time) * 1000.0

            status = _map_status(response.status_code, probe_expected_status)
            error_msg: str | None = None
            if status != "ok":
                error_msg = (
                    f"HTTP {response.status_code} from {url} "
                    f"(expected {probe_expected_status})"
                )

            result = ExternalProbeResult(
                name=name,
                status=status,
                base_url=base_url,
                latency_ms=round(elapsed_ms, 2),
                error=error_msg,
                last_probed_at=time.time(),
            )

        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            if not owns_client:
                error_prefix = (
                    f"Timeout after {_PROBE_TIMEOUT_SECONDS}s"
                    if isinstance(exc, httpx.TimeoutException)
                    else "Connection error"
                )
                result = ExternalProbeResult(
                    name=name,
                    status="unreachable",
                    base_url=base_url,
                    latency_ms=None,
                    error=f"{error_prefix}: {type(exc).__name__}",
                    last_probed_at=time.time(),
                )
            else:
                try:
                    response = await asyncio.to_thread(
                        _sync_probe_request,
                        method=probe_method,
                        url=url,
                        headers=headers,
                    )
                    elapsed_ms = (time.monotonic() - start_time) * 1000.0

                    status = _map_status(
                        response.status_code,
                        probe_expected_status,
                    )
                    error_msg = None
                    if status != "ok":
                        error_msg = (
                            f"HTTP {response.status_code} from {url} "
                            f"(expected {probe_expected_status})"
                        )

                    result = ExternalProbeResult(
                        name=name,
                        status=status,
                        base_url=base_url,
                        latency_ms=round(elapsed_ms, 2),
                        error=error_msg,
                        last_probed_at=time.time(),
                    )
                except httpx.TimeoutException as fallback_exc:
                    result = ExternalProbeResult(
                        name=name,
                        status="unreachable",
                        base_url=base_url,
                        latency_ms=None,
                        error=(
                            f"Timeout after {_PROBE_TIMEOUT_SECONDS}s: "
                            f"{type(fallback_exc).__name__}"
                        ),
                        last_probed_at=time.time(),
                    )
                except httpx.HTTPError as fallback_exc:
                    result = ExternalProbeResult(
                        name=name,
                        status="unreachable",
                        base_url=base_url,
                        latency_ms=None,
                        error=f"Connection error: {type(fallback_exc).__name__}",
                        last_probed_at=time.time(),
                    )

    finally:
        if owns_client:
            await client.aclose()

    _set_cached(name, result)
    return result


# ---------------------------------------------------------------------------
# Streak tracking + audit emission (behavior 10.7, audit and alarm wiring)
# ---------------------------------------------------------------------------

#: Consecutive failure threshold that triggers a streak alert.
EXTERNAL_PROVIDER_STREAK_THRESHOLD: Final[int] = 3


@dataclass
class _StreakState:
    """Per-provider consecutive failure counter for streak alerting."""

    consecutive_failures: int = 0
    streak_alert_emitted: bool = False


#: Process-local streak state. Keyed by provider name.
_streak_state: dict[str, _StreakState] = {}


def _get_streak_state(name: str) -> _StreakState:
    """Return (or create) the streak state for a provider."""
    if name not in _streak_state:
        _streak_state[name] = _StreakState()
    return _streak_state[name]


def reset_streak_state() -> None:
    """Clear all streak state. Useful for testing."""
    _streak_state.clear()


class AuditWriterProtocol(Protocol):
    """Minimal protocol for the audit writer dependency.

    Matches :meth:`AuditWriter.write_with_retry` from
    ``audit_writer.py`` so the external probe module does not import
    the full writer (avoiding circular dependencies).
    """

    async def write_with_retry(self, entry: Any) -> Any:
        ...


async def emit_probe_audit(
    result: ExternalProbeResult,
    *,
    audit_writer: AuditWriterProtocol | None = None,
) -> None:
    """Emit audit entries based on probe result and streak state.

    Call this after every :func:`probe_external` invocation. The
    function:

    1. On any non-``"ok"`` status: emits
       ``external_provider_probe_failed`` and increments the streak
       counter.
    2. When the streak counter reaches
       :data:`EXTERNAL_PROVIDER_STREAK_THRESHOLD` (3): emits a single
       ``external_provider_streak_alert`` (not repeated until the
       provider recovers and fails again).
    3. On ``"ok"`` status: resets the streak counter and the alert
       flag.

    Parameters
    ----------
    result:
        The probe result from :func:`probe_external`.
    audit_writer:
        An object implementing :meth:`write_with_retry`. When
        ``None``, audit entries are silently skipped (useful in tests
        or when the audit DB is not wired).
    """
    if audit_writer is None:
        return

    # Lazy import to avoid circular dependency at module level.
    from .audit_writer import AuditEntry

    state = _get_streak_state(result.name)

    if result.status == "ok":
        # Provider recovered - reset streak.
        state.consecutive_failures = 0
        state.streak_alert_emitted = False
        return

    # --- Failed probe ---
    state.consecutive_failures += 1

    # Emit per-failure audit entry.
    await audit_writer.write_with_retry(
        AuditEntry(
            id=uuid4(),
            actor="system",
            actor_type="admin_dashboard_user",
            service_name=result.name,
            action="external_provider_probe_failed",
            timestamp=datetime.now(tz=timezone.utc),
            correlation_id=uuid4(),
            outcome="failed",
            details_json={
                "provider": result.name,
                "status": result.status,
                "base_url": result.base_url,
                "error": result.error,
                "streak": state.consecutive_failures,
            },
        )
    )

    # Emit streak alert on threshold crossing (once per streak).
    if (
        state.consecutive_failures >= EXTERNAL_PROVIDER_STREAK_THRESHOLD
        and not state.streak_alert_emitted
    ):
        await audit_writer.write_with_retry(
            AuditEntry(
                id=uuid4(),
                actor="system",
                actor_type="admin_dashboard_user",
                service_name=result.name,
                action="external_provider_streak_alert",
                timestamp=datetime.now(tz=timezone.utc),
                correlation_id=uuid4(),
                outcome="failed",
                details_json={
                    "provider": result.name,
                    "status": result.status,
                    "base_url": result.base_url,
                    "consecutive_failures": state.consecutive_failures,
                    "threshold": EXTERNAL_PROVIDER_STREAK_THRESHOLD,
                },
            )
        )
        state.streak_alert_emitted = True


__all__ = (
    "EXTERNAL_PROVIDER_STREAK_THRESHOLD",
    "ExternalProbeResult",
    "ExternalProbeStatus",
    "VaultCredentialReader",
    "clear_cache",
    "emit_probe_audit",
    "probe_external",
    "reset_streak_state",
)
