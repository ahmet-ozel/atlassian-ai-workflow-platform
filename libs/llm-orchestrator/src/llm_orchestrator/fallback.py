"""LLM Provider Factory with automatic fallback switching.

Provides a resilient
LLM completion interface that automatically switches between primary
and fallback providers based on timeout and HTTP 5xx error conditions.

Fallback Logic:
    - Primary timeout (30s): immediately switch to fallback provider.
    - Primary HTTP 5xx: retry 3 times with 2s interval, then switch.
    - Fallback also fails (30s timeout or 5xx): raise LLMUnavailableError.
    - On switch: log timestamp, failed provider name, error reason;
      invoke notification_callback for Admin Dashboard.
    - Health probe: 5 minutes after fallback switch, send single health
      check to primary; if successful, route subsequent requests back.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Protocol, runtime_checkable

__all__ = [
    "FallbackEvent",
    "FallbackLLMProviderFactory",
    "LLMProviderConfig",
    "LLMUnavailableError",
]

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default timeout for a single LLM request (seconds).
_DEFAULT_TIMEOUT_SECONDS: float = 30.0

#: Number of retry attempts on HTTP 5xx before switching to fallback.
_MAX_5XX_RETRIES: int = 3

#: Interval between retries on HTTP 5xx (seconds).
_RETRY_INTERVAL_SECONDS: float = 2.0

#: Time to wait before probing primary health after fallback switch (seconds).
_HEALTH_PROBE_DELAY_SECONDS: float = 300.0  # 5 minutes


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LLMUnavailableError(Exception):
    """Raised when both primary and fallback providers are unavailable.

    The workflow should stop with "llm_unavailable" status when this
    exception is raised.
    """

    def __init__(self, message: str, context: dict[str, Any] | None = None):
        super().__init__(message)
        self.context = context or {}


class LLMTimeoutError(Exception):
    """Raised when an LLM provider does not respond within the timeout."""


class LLMServerError(Exception):
    """Raised when an LLM provider returns HTTP 5xx."""

    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Responses API helper
# ---------------------------------------------------------------------------


def _extract_responses_text(data: Any) -> str:
    """Extract assistant text from an OpenAI Responses API payload.

    Prefers the convenience ``output_text`` field; otherwise walks the
    structured ``output[].content[].text`` array of assistant messages.
    """
    if not isinstance(data, dict):
        return ""

    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    if isinstance(output_text, list):
        joined = "".join(part for part in output_text if isinstance(part, str))
        if joined:
            return joined

    chunks: list[str] = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("output_text", "text"):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "".join(chunks)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMProvider(Protocol):
    """Minimal async provider contract for the fallback factory."""

    async def complete(self, prompt: str, **kwargs: Any) -> str:
        """Send a completion request and return the response text."""
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMProviderConfig:
    """Configuration for an LLM provider endpoint.

    Attributes:
        name: Human-readable provider name (e.g. "vllm", "openai").
        base_url: Base URL for the provider API.
        api_key: API key or token for authentication.
        timeout_seconds: Request timeout in seconds (default 30.0).
    """

    name: str
    base_url: str
    api_key: str
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class FallbackEvent:
    """Record of a fallback switch event.

    Attributes:
        timestamp: ISO 8601 timestamp of the switch event.
        failed_provider: Name of the provider that failed.
        error_reason: Description of why the provider failed.
        switched_to: Name of the provider switched to.
    """

    timestamp: str
    failed_provider: str
    error_reason: str
    switched_to: str


# ---------------------------------------------------------------------------
# Fallback LLM Provider Factory
# ---------------------------------------------------------------------------


@dataclass
class FallbackLLMProviderFactory:
    """Manages primary/fallback LLM provider switching.

    - Primary timeout (30s): switch to fallback provider, preserve
      original request data.
    - Primary HTTP 5xx: retry 3 times with 2s interval, then switch
      to fallback.
    - Fallback also fails (30s timeout or 5xx): stop workflow with
      "llm_unavailable" status.
    - On switch: log timestamp, failed provider name, error reason;
      show notification in Admin Dashboard via notification_callback.
    - Health probe: 5 minutes after fallback switch, send single
      health check to primary; if successful, route subsequent
      requests back to primary.

    Args:
        primary_config: Configuration for the primary LLM provider.
        fallback_config: Configuration for the fallback LLM provider.
        notification_callback: Optional async callable invoked on
            fallback switch events. Receives a FallbackEvent.
        provider_factory: Optional callable that creates an LLMProvider
            from an LLMProviderConfig. Used for dependency injection
            in tests.
        sleep_func: Optional async sleep function for testing.
    """

    primary_config: LLMProviderConfig
    fallback_config: LLMProviderConfig
    notification_callback: (
        Callable[[FallbackEvent], Coroutine[Any, Any, None]] | None
    ) = None
    provider_factory: (
        Callable[[LLMProviderConfig], LLMProvider] | None
    ) = None
    sleep_func: Callable[[float], Coroutine[Any, Any, None]] = field(
        default=asyncio.sleep  # type: ignore[assignment]
    )

    # Internal state
    _using_fallback: bool = field(default=False, init=False, repr=False)
    _fallback_switch_time: float = field(default=0.0, init=False, repr=False)
    _fallback_events: list[FallbackEvent] = field(
        default_factory=list, init=False, repr=False
    )
    _primary_provider: LLMProvider | None = field(
        default=None, init=False, repr=False
    )
    _fallback_provider: LLMProvider | None = field(
        default=None, init=False, repr=False
    )
    _health_probe_task: asyncio.Task[None] | None = field(
        default=None, init=False, repr=False
    )

    def __post_init__(self) -> None:
        """Initialize provider instances."""
        if self.provider_factory:
            self._primary_provider = self.provider_factory(self.primary_config)
            self._fallback_provider = self.provider_factory(self.fallback_config)
        else:
            self._primary_provider = _HttpLLMProvider(self.primary_config)
            self._fallback_provider = _HttpLLMProvider(self.fallback_config)

    @property
    def using_fallback(self) -> bool:
        """Whether the factory is currently routing to the fallback provider."""
        return self._using_fallback

    @property
    def fallback_events(self) -> list[FallbackEvent]:
        """List of all fallback switch events that have occurred."""
        return list(self._fallback_events)

    @property
    def active_provider_name(self) -> str:
        """Name of the currently active provider."""
        if self._using_fallback:
            return self.fallback_config.name
        return self.primary_config.name

    async def complete(self, prompt: str, dept_id: str, **kwargs: Any) -> str:
        """Send a completion request with automatic fallback handling.

        Tries the active provider (primary or fallback). On failure,
        applies retry/fallback logic for timeout and HTTP 5xx failures.

        Args:
            prompt: The prompt text to send to the LLM.
            dept_id: Department identifier for context tracking.
            **kwargs: Additional keyword arguments passed to the provider.

        Returns:
            The completion response text.

        Raises:
            LLMUnavailableError: When both providers are unavailable.
        """
        if not self._using_fallback:
            return await self._try_primary(prompt, dept_id, **kwargs)
        else:
            return await self._try_fallback(prompt, dept_id, **kwargs)

    async def _try_primary(
        self, prompt: str, dept_id: str, **kwargs: Any
    ) -> str:
        """Attempt completion with the primary provider.

        On timeout: immediately switch to fallback.
        On 5xx: retry up to 3 times with 2s interval, then switch.
        """
        assert self._primary_provider is not None

        try:
            result = await asyncio.wait_for(
                self._primary_provider.complete(prompt, **kwargs),
                timeout=self.primary_config.timeout_seconds,
            )
            return result
        except asyncio.TimeoutError:
            # Primary timeout  switch to fallback.
            error_reason = (
                f"Timeout after {self.primary_config.timeout_seconds}s"
            )
            await self._switch_to_fallback(error_reason)
            return await self._try_fallback(prompt, dept_id, **kwargs)
        except LLMServerError as exc:
            # Primary 5xx  retry 3 times, then fallback.
            return await self._retry_primary_then_fallback(
                prompt, dept_id, exc, **kwargs
            )

    async def _retry_primary_then_fallback(
        self,
        prompt: str,
        dept_id: str,
        initial_error: LLMServerError,
        **kwargs: Any,
    ) -> str:
        """Retry primary on 5xx up to 3 times, then switch to fallback.

        The initial error counts as the first attempt.
        """
        assert self._primary_provider is not None

        last_error: LLMServerError = initial_error

        # initial_error is attempt 1; retry attempts 2 and 3
        for attempt in range(2, _MAX_5XX_RETRIES + 1):
            await self.sleep_func(_RETRY_INTERVAL_SECONDS)
            try:
                result = await asyncio.wait_for(
                    self._primary_provider.complete(prompt, **kwargs),
                    timeout=self.primary_config.timeout_seconds,
                )
                return result
            except asyncio.TimeoutError:
                error_reason = (
                    f"Timeout after {self.primary_config.timeout_seconds}s "
                    f"on retry attempt {attempt}"
                )
                await self._switch_to_fallback(error_reason)
                return await self._try_fallback(prompt, dept_id, **kwargs)
            except LLMServerError as exc:
                last_error = exc
                _LOG.warning(
                    "Primary provider 5xx retry %d/%d failed",
                    attempt,
                    _MAX_5XX_RETRIES,
                    extra={
                        "provider": self.primary_config.name,
                        "status_code": exc.status_code,
                        "dept_id": dept_id,
                    },
                )

        # All retries exhausted - switch to fallback
        error_reason = (
            f"HTTP {last_error.status_code} after "
            f"{_MAX_5XX_RETRIES} attempts"
        )
        await self._switch_to_fallback(error_reason)
        return await self._try_fallback(prompt, dept_id, **kwargs)

    async def _try_fallback(
        self, prompt: str, dept_id: str, **kwargs: Any
    ) -> str:
        """Attempt completion with the fallback provider.

        If fallback also fails (timeout or 5xx), raise
        LLMUnavailableError to stop the workflow.
        """
        assert self._fallback_provider is not None

        try:
            result = await asyncio.wait_for(
                self._fallback_provider.complete(prompt, **kwargs),
                timeout=self.fallback_config.timeout_seconds,
            )
            return result
        except asyncio.TimeoutError:
            raise LLMUnavailableError(
                "Both primary and fallback providers unavailable: "
                f"fallback '{self.fallback_config.name}' timed out "
                f"after {self.fallback_config.timeout_seconds}s",
                context={
                    "dept_id": dept_id,
                    "primary_provider": self.primary_config.name,
                    "fallback_provider": self.fallback_config.name,
                    "fallback_error": "timeout",
                },
            )
        except LLMServerError as exc:
            raise LLMUnavailableError(
                "Both primary and fallback providers unavailable: "
                f"fallback '{self.fallback_config.name}' returned "
                f"HTTP {exc.status_code}",
                context={
                    "dept_id": dept_id,
                    "primary_provider": self.primary_config.name,
                    "fallback_provider": self.fallback_config.name,
                    "fallback_error": f"HTTP {exc.status_code}",
                },
            )

    async def _switch_to_fallback(self, error_reason: str) -> None:
        """Switch routing to the fallback provider and record the event.

        Log timestamp, failed provider, and error reason. Notify Admin
        Dashboard via notification_callback.
        """
        self._using_fallback = True
        self._fallback_switch_time = time.monotonic()

        event = FallbackEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            failed_provider=self.primary_config.name,
            error_reason=error_reason,
            switched_to=self.fallback_config.name,
        )
        self._fallback_events.append(event)

        _LOG.warning(
            "LLM provider fallback switch",
            extra={
                "timestamp": event.timestamp,
                "failed_provider": event.failed_provider,
                "error_reason": event.error_reason,
                "switched_to": event.switched_to,
            },
        )

        # Notify Admin Dashboard
        if self.notification_callback is not None:
            try:
                await self.notification_callback(event)
            except Exception:  # noqa: BLE001
                _LOG.exception(
                    "Failed to send fallback notification to Admin Dashboard"
                )

        # Schedule health probe for primary restoration
        self._schedule_health_probe()

    def _schedule_health_probe(self) -> None:
        """Schedule a health probe to check if primary can be restored.

        Five minutes after fallback switch, send a single health check
        to primary; if successful, route back.
        """
        # Cancel any existing probe task
        if self._health_probe_task is not None and not self._health_probe_task.done():
            self._health_probe_task.cancel()

        try:
            loop = asyncio.get_running_loop()
            self._health_probe_task = loop.create_task(
                self._delayed_health_probe()
            )
        except RuntimeError:
            # No running event loop - skip scheduling (test environment)
            _LOG.debug("No running event loop; skipping health probe schedule")

    async def _delayed_health_probe(self) -> None:
        """Wait 5 minutes then probe primary provider health."""
        try:
            await self.sleep_func(_HEALTH_PROBE_DELAY_SECONDS)
            success = await self._health_probe_primary()
            if success:
                self._using_fallback = False
                _LOG.info(
                    "Primary provider restored after health probe",
                    extra={"provider": self.primary_config.name},
                )
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            _LOG.exception("Health probe failed unexpectedly")

    async def _health_probe_primary(self) -> bool:
        """Send a single health check request to the primary provider.

        If successful, subsequent requests route back to primary.

        Returns:
            True if primary responded successfully, False otherwise.
        """
        assert self._primary_provider is not None

        try:
            await asyncio.wait_for(
                self._primary_provider.complete("health_check", **{}),
                timeout=self.primary_config.timeout_seconds,
            )
            return True
        except (asyncio.TimeoutError, LLMServerError, Exception):
            _LOG.warning(
                "Primary health probe failed; staying on fallback",
                extra={"provider": self.primary_config.name},
            )
            return False

    async def force_health_probe(self) -> bool:
        """Manually trigger a health probe (useful for testing).

        Returns:
            True if primary is healthy and routing was restored.
        """
        success = await self._health_probe_primary()
        if success:
            self._using_fallback = False
        return success

    def reset(self) -> None:
        """Reset the factory to use the primary provider.

        Cancels any pending health probe tasks.
        """
        self._using_fallback = False
        self._fallback_switch_time = 0.0
        if self._health_probe_task is not None and not self._health_probe_task.done():
            self._health_probe_task.cancel()
            self._health_probe_task = None


# ---------------------------------------------------------------------------
# Default HTTP-based LLM Provider
# ---------------------------------------------------------------------------


class _HttpLLMProvider:
    """Default HTTP-based LLM provider implementation.

    Uses httpx to make requests to the configured endpoint. Raises
    LLMServerError on 5xx responses. Timeout handling is done by the
    caller via asyncio.wait_for.
    """

    def __init__(self, config: LLMProviderConfig) -> None:
        self._config = config

    def _is_openai(self) -> bool:
        """Return True when this config targets the OpenAI cloud API.

        OpenAI must be driven through the Responses API; vLLM and other
        OpenAI-compatible self-hosted endpoints keep the Chat Completions
        surface. We detect OpenAI by provider name or the canonical host.
        """
        name = (self._config.name or "").strip().lower()
        base = (self._config.base_url or "").lower()
        return name == "openai" or "api.openai.com" in base

    async def complete(self, prompt: str, **kwargs: Any) -> str:
        """Send completion request via HTTP POST.

        OpenAI is routed to ``/v1/responses`` (Responses API); every other
        OpenAI-compatible endpoint (vLLM, etc.) uses ``/chat/completions``.
        """
        import httpx

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._config.api_key}",
        }

        base = self._config.base_url.rstrip("/")
        if self._is_openai():
            url = f"{base}/responses" if base else "https://api.openai.com/v1/responses"
            payload: dict[str, Any] = {
                "model": kwargs.get("model", "default"),
                "input": prompt,
                "max_output_tokens": kwargs.get("max_tokens", 2048),
                "temperature": kwargs.get("temperature", 0.2),
            }
        else:
            url = f"{base}/chat/completions"
            payload = {
                "model": kwargs.get("model", "default"),
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": kwargs.get("max_tokens", 2048),
                "temperature": kwargs.get("temperature", 0.2),
            }

        async with httpx.AsyncClient(
            timeout=self._config.timeout_seconds
        ) as client:
            response = await client.post(
                url,
                json=payload,
                headers=headers,
            )

        if response.status_code >= 500:
            raise LLMServerError(
                f"{self._config.name} server error: {response.status_code}",
                status_code=response.status_code,
            )

        response.raise_for_status()
        data = response.json()
        if self._is_openai():
            return _extract_responses_text(data)
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
        return ""
