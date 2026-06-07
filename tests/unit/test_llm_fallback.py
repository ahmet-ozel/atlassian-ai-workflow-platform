"""Unit tests for LLM Provider Factory fallback mechanism.

Tests the FallbackLLMProviderFactory class from
platform/libs/llm-orchestrator/src/llm_orchestrator/fallback.py

"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from llm_orchestrator.fallback import (
    FallbackEvent,
    FallbackLLMProviderFactory,
    LLMProviderConfig,
    LLMServerError,
    LLMUnavailableError,
)


# ---------------------------------------------------------------------------
# Fixtures & Helpers
# ---------------------------------------------------------------------------


class FakeLLMProvider:
    """Fake LLM provider for testing."""

    def __init__(self, responses: list[str | Exception] | None = None):
        self._responses = list(responses or ["response"])
        self._call_count = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def complete(self, prompt: str, **kwargs: Any) -> str:
        self.calls.append((prompt, kwargs))
        idx = min(self._call_count, len(self._responses) - 1)
        self._call_count += 1
        response = self._responses[idx]
        if isinstance(response, Exception):
            raise response
        return response


def _make_config(
    name: str = "primary", timeout: float = 30.0
) -> LLMProviderConfig:
    return LLMProviderConfig(
        name=name,
        base_url="http://localhost:8000/v1",
        api_key="test-key",
        timeout_seconds=timeout,
    )


def _make_factory(
    primary_provider: FakeLLMProvider,
    fallback_provider: FakeLLMProvider,
    notification_callback: Any = None,
    primary_timeout: float = 30.0,
    fallback_timeout: float = 30.0,
) -> FallbackLLMProviderFactory:
    """Create a factory with fake providers injected."""
    primary_config = _make_config("primary-vllm", primary_timeout)
    fallback_config = _make_config("fallback-openai", fallback_timeout)

    providers = {
        id(primary_config): primary_provider,
        id(fallback_config): fallback_provider,
    }

    # Use a provider_factory that returns our fakes based on config identity
    def provider_factory(config: LLMProviderConfig) -> Any:
        return providers[id(config)]

    return FallbackLLMProviderFactory(
        primary_config=primary_config,
        fallback_config=fallback_config,
        notification_callback=notification_callback,
        provider_factory=provider_factory,
        sleep_func=_instant_sleep,
    )


async def _instant_sleep(seconds: float) -> None:
    """No-op sleep for fast tests."""
    pass


# ---------------------------------------------------------------------------
# Tests: Primary Success
# ---------------------------------------------------------------------------


class TestPrimarySuccess:
    """Tests for successful primary provider responses."""

    @pytest.mark.asyncio
    async def test_primary_returns_response(self):
        """Primary provider responds successfully."""
        primary = FakeLLMProvider(["hello world"])
        fallback = FakeLLMProvider(["fallback response"])
        factory = _make_factory(primary, fallback)

        result = await factory.complete("test prompt", dept_id="dept-1")

        assert result == "hello world"
        assert not factory.using_fallback
        assert len(primary.calls) == 1
        assert len(fallback.calls) == 0

    @pytest.mark.asyncio
    async def test_primary_preserves_kwargs(self):
        """Primary provider receives all kwargs."""
        primary = FakeLLMProvider(["ok"])
        fallback = FakeLLMProvider(["fb"])
        factory = _make_factory(primary, fallback)

        await factory.complete(
            "prompt", dept_id="dept-1", temperature=0.5, max_tokens=100
        )

        assert primary.calls[0][1] == {"temperature": 0.5, "max_tokens": 100}


# ---------------------------------------------------------------------------
# Tests: Primary Timeout  Fallback
# ---------------------------------------------------------------------------


class TestPrimaryTimeout:
    """Tests for primary timeout triggering fallback switch."""

    @pytest.mark.asyncio
    async def test_timeout_switches_to_fallback(self):
        """Primary timeout switches to fallback."""

        async def slow_complete(prompt: str, **kwargs: Any) -> str:
            await asyncio.sleep(100)  # Will be cancelled by wait_for
            return "never"

        primary = FakeLLMProvider()
        primary.complete = slow_complete  # type: ignore[assignment]
        fallback = FakeLLMProvider(["fallback response"])

        factory = _make_factory(
            primary, fallback, primary_timeout=0.01  # Very short timeout
        )

        result = await factory.complete("test", dept_id="dept-1")

        assert result == "fallback response"
        assert factory.using_fallback
        assert len(fallback.calls) == 1

    @pytest.mark.asyncio
    async def test_timeout_preserves_original_request(self):
        """Original request data is preserved on fallback."""

        async def slow_complete(prompt: str, **kwargs: Any) -> str:
            await asyncio.sleep(100)
            return "never"

        primary = FakeLLMProvider()
        primary.complete = slow_complete  # type: ignore[assignment]
        fallback = FakeLLMProvider(["ok"])

        factory = _make_factory(primary, fallback, primary_timeout=0.01)

        await factory.complete(
            "original prompt", dept_id="dept-1", model="gpt-4"
        )

        assert fallback.calls[0][0] == "original prompt"
        assert fallback.calls[0][1] == {"model": "gpt-4"}

    @pytest.mark.asyncio
    async def test_timeout_logs_fallback_event(self):
        """Fallback event is logged on timeout switch."""

        async def slow_complete(prompt: str, **kwargs: Any) -> str:
            await asyncio.sleep(100)
            return "never"

        primary = FakeLLMProvider()
        primary.complete = slow_complete  # type: ignore[assignment]
        fallback = FakeLLMProvider(["ok"])

        factory = _make_factory(primary, fallback, primary_timeout=0.01)
        await factory.complete("test", dept_id="dept-1")

        events = factory.fallback_events
        assert len(events) == 1
        assert events[0].failed_provider == "primary-vllm"
        assert "Timeout" in events[0].error_reason
        assert events[0].switched_to == "fallback-openai"
        assert events[0].timestamp  # ISO format string


# ---------------------------------------------------------------------------
# Tests: Primary 5xx  Retry  Fallback
# ---------------------------------------------------------------------------


class TestPrimary5xxRetry:
    """Tests for primary 5xx retry logic and fallback switch."""

    @pytest.mark.asyncio
    async def test_5xx_retries_3_times_then_fallback(self):
        """3 retries with 2s interval, then fallback."""
        primary = FakeLLMProvider([
            LLMServerError("error", 500),
            LLMServerError("error", 502),
            LLMServerError("error", 503),
        ])
        fallback = FakeLLMProvider(["fallback ok"])

        factory = _make_factory(primary, fallback)
        result = await factory.complete("test", dept_id="dept-1")

        assert result == "fallback ok"
        assert factory.using_fallback
        # 1 initial + 2 retries = 3 total attempts on primary
        assert len(primary.calls) == 3
        assert len(fallback.calls) == 1

    @pytest.mark.asyncio
    async def test_5xx_succeeds_on_retry(self):
        """Primary recovers on second retry - no fallback switch."""
        primary = FakeLLMProvider([
            LLMServerError("error", 500),
            "recovered",
        ])
        fallback = FakeLLMProvider(["fallback"])

        factory = _make_factory(primary, fallback)
        result = await factory.complete("test", dept_id="dept-1")

        assert result == "recovered"
        assert not factory.using_fallback
        assert len(primary.calls) == 2
        assert len(fallback.calls) == 0

    @pytest.mark.asyncio
    async def test_5xx_logs_event_after_exhausted_retries(self):
        """Log event after all retries exhausted."""
        primary = FakeLLMProvider([
            LLMServerError("err", 500),
            LLMServerError("err", 500),
            LLMServerError("err", 500),
        ])
        fallback = FakeLLMProvider(["ok"])

        factory = _make_factory(primary, fallback)
        await factory.complete("test", dept_id="dept-1")

        events = factory.fallback_events
        assert len(events) == 1
        assert "500" in events[0].error_reason
        assert "3 attempts" in events[0].error_reason


# ---------------------------------------------------------------------------
# Tests: Fallback Also Fails
# ---------------------------------------------------------------------------


class TestFallbackAlsoFails:
    """Tests for both providers failing  LLMUnavailableError."""

    @pytest.mark.asyncio
    async def test_fallback_timeout_raises_unavailable(self):
        """Fallback timeout raises llm_unavailable."""

        async def slow_primary(prompt: str, **kwargs: Any) -> str:
            await asyncio.sleep(100)
            return "never"

        async def slow_fallback(prompt: str, **kwargs: Any) -> str:
            await asyncio.sleep(100)
            return "never"

        primary = FakeLLMProvider()
        primary.complete = slow_primary  # type: ignore[assignment]
        fallback = FakeLLMProvider()
        fallback.complete = slow_fallback  # type: ignore[assignment]

        factory = _make_factory(
            primary, fallback, primary_timeout=0.01, fallback_timeout=0.01
        )

        with pytest.raises(LLMUnavailableError) as exc_info:
            await factory.complete("test", dept_id="dept-1")

        assert "unavailable" in str(exc_info.value).lower()
        assert exc_info.value.context["dept_id"] == "dept-1"

    @pytest.mark.asyncio
    async def test_fallback_5xx_raises_unavailable(self):
        """Fallback 5xx raises llm_unavailable."""
        primary = FakeLLMProvider([
            LLMServerError("err", 500),
            LLMServerError("err", 500),
            LLMServerError("err", 500),
        ])
        fallback = FakeLLMProvider([LLMServerError("fb err", 502)])

        factory = _make_factory(primary, fallback)

        with pytest.raises(LLMUnavailableError) as exc_info:
            await factory.complete("test", dept_id="dept-1")

        assert "502" in str(exc_info.value)
        assert exc_info.value.context["fallback_error"] == "HTTP 502"


# ---------------------------------------------------------------------------
# Tests: Notification Callback
# ---------------------------------------------------------------------------


class TestNotificationCallback:
    """Tests for Admin Dashboard notification on fallback switch."""

    @pytest.mark.asyncio
    async def test_notification_called_on_switch(self):
        """Notification is sent on fallback switch."""
        callback = AsyncMock()
        primary = FakeLLMProvider([
            LLMServerError("err", 500),
            LLMServerError("err", 500),
            LLMServerError("err", 500),
        ])
        fallback = FakeLLMProvider(["ok"])

        factory = _make_factory(primary, fallback, notification_callback=callback)
        await factory.complete("test", dept_id="dept-1")

        callback.assert_called_once()
        event = callback.call_args[0][0]
        assert isinstance(event, FallbackEvent)
        assert event.failed_provider == "primary-vllm"
        assert event.switched_to == "fallback-openai"

    @pytest.mark.asyncio
    async def test_notification_failure_does_not_block(self):
        """Notification callback failure should not prevent fallback."""

        async def failing_callback(event: FallbackEvent) -> None:
            raise RuntimeError("notification service down")

        primary = FakeLLMProvider([
            LLMServerError("err", 500),
            LLMServerError("err", 500),
            LLMServerError("err", 500),
        ])
        fallback = FakeLLMProvider(["ok"])

        factory = _make_factory(
            primary, fallback, notification_callback=failing_callback
        )
        result = await factory.complete("test", dept_id="dept-1")

        assert result == "ok"
        assert factory.using_fallback


# ---------------------------------------------------------------------------
# Tests: Health Probe
# ---------------------------------------------------------------------------


class TestHealthProbe:
    """Tests for primary health probe and restoration."""

    @pytest.mark.asyncio
    async def test_health_probe_restores_primary(self):
        """Successful health probe routes back to primary."""
        primary = FakeLLMProvider([
            LLMServerError("err", 500),
            LLMServerError("err", 500),
            LLMServerError("err", 500),
            "healthy",  # health probe response
            "primary again",  # subsequent request
        ])
        fallback = FakeLLMProvider(["fallback ok"])

        factory = _make_factory(primary, fallback)

        # First call triggers fallback
        result1 = await factory.complete("test1", dept_id="dept-1")
        assert result1 == "fallback ok"
        assert factory.using_fallback

        # Force health probe
        success = await factory.force_health_probe()
        assert success
        assert not factory.using_fallback

        # Next call goes to primary
        result2 = await factory.complete("test2", dept_id="dept-1")
        assert result2 == "primary again"

    @pytest.mark.asyncio
    async def test_health_probe_fails_stays_on_fallback(self):
        """Failed health probe  stay on fallback."""
        primary = FakeLLMProvider([
            LLMServerError("err", 500),
            LLMServerError("err", 500),
            LLMServerError("err", 500),
            LLMServerError("still down", 500),  # health probe fails
        ])
        fallback = FakeLLMProvider(["fallback ok", "still fallback"])

        factory = _make_factory(primary, fallback)

        await factory.complete("test1", dept_id="dept-1")
        assert factory.using_fallback

        success = await factory.force_health_probe()
        assert not success
        assert factory.using_fallback

    @pytest.mark.asyncio
    async def test_health_probe_timeout_stays_on_fallback(self):
        """Health probe timeout  stay on fallback."""

        async def slow_complete(prompt: str, **kwargs: Any) -> str:
            await asyncio.sleep(100)
            return "never"

        primary = FakeLLMProvider([
            LLMServerError("err", 500),
            LLMServerError("err", 500),
            LLMServerError("err", 500),
        ])
        fallback = FakeLLMProvider(["fallback ok"])

        factory = _make_factory(
            primary, fallback, primary_timeout=0.01
        )

        await factory.complete("test", dept_id="dept-1")
        assert factory.using_fallback

        # Replace primary's complete with a slow one for health probe
        primary.complete = slow_complete  # type: ignore[assignment]

        success = await factory.force_health_probe()
        assert not success
        assert factory.using_fallback


# ---------------------------------------------------------------------------
# Tests: State Management
# ---------------------------------------------------------------------------


class TestStateManagement:
    """Tests for factory state tracking."""

    @pytest.mark.asyncio
    async def test_active_provider_name(self):
        """active_provider_name reflects current routing."""
        primary = FakeLLMProvider([
            LLMServerError("err", 500),
            LLMServerError("err", 500),
            LLMServerError("err", 500),
        ])
        fallback = FakeLLMProvider(["ok"])

        factory = _make_factory(primary, fallback)

        assert factory.active_provider_name == "primary-vllm"

        await factory.complete("test", dept_id="dept-1")

        assert factory.active_provider_name == "fallback-openai"

    @pytest.mark.asyncio
    async def test_reset_restores_primary(self):
        """reset() switches back to primary."""
        primary = FakeLLMProvider([
            LLMServerError("err", 500),
            LLMServerError("err", 500),
            LLMServerError("err", 500),
            "back",
        ])
        fallback = FakeLLMProvider(["ok"])

        factory = _make_factory(primary, fallback)
        await factory.complete("test", dept_id="dept-1")
        assert factory.using_fallback

        factory.reset()
        assert not factory.using_fallback
        assert factory.active_provider_name == "primary-vllm"

    @pytest.mark.asyncio
    async def test_subsequent_calls_use_fallback(self):
        """After switch, subsequent calls go to fallback without retrying primary."""
        primary = FakeLLMProvider([
            LLMServerError("err", 500),
            LLMServerError("err", 500),
            LLMServerError("err", 500),
        ])
        fallback = FakeLLMProvider(["fb1", "fb2", "fb3"])

        factory = _make_factory(primary, fallback)

        await factory.complete("test1", dept_id="dept-1")
        result2 = await factory.complete("test2", dept_id="dept-1")
        result3 = await factory.complete("test3", dept_id="dept-1")

        assert result2 == "fb2"
        assert result3 == "fb3"
        # Primary was only called during the initial failure sequence
        assert len(primary.calls) == 3


# ---------------------------------------------------------------------------
# Tests: Data Classes
# ---------------------------------------------------------------------------


class TestDataClasses:
    """Tests for LLMProviderConfig and FallbackEvent."""

    def test_provider_config_defaults(self):
        """LLMProviderConfig has correct defaults."""
        config = LLMProviderConfig(
            name="test", base_url="http://localhost", api_key="key"
        )
        assert config.timeout_seconds == 30.0

    def test_provider_config_frozen(self):
        """LLMProviderConfig is immutable."""
        config = LLMProviderConfig(
            name="test", base_url="http://localhost", api_key="key"
        )
        with pytest.raises(Exception):
            config.name = "changed"  # type: ignore[misc]

    def test_fallback_event_frozen(self):
        """FallbackEvent is immutable."""
        event = FallbackEvent(
            timestamp="2024-01-01T00:00:00Z",
            failed_provider="vllm",
            error_reason="timeout",
            switched_to="openai",
        )
        assert event.timestamp == "2024-01-01T00:00:00Z"
        with pytest.raises(Exception):
            event.failed_provider = "changed"  # type: ignore[misc]
