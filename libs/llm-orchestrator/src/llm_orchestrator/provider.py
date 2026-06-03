"""LLM Provider implementations: vLLM, OpenAI, and Anthropic.

Provides concrete
provider classes that can be used by the :class:`LlmOrchestrator` for
the tool-call loop with retry + fallback.

Provider Selection:
    - ``vllm`` — Self-hosted vLLM (OpenAI-compatible API).
    - ``openai`` — OpenAI cloud API.
    - ``anthropic`` — Anthropic Claude API.

All real providers implement the same interface and can be used as
primary or fallback in the orchestrator's retry chain.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, Sequence, runtime_checkable

__all__ = [
    "AnthropicProvider",
    "LLMProvider",
    "LLMProviderFactory",
    "OpenAIProvider",
    "SyntheticLLMProvider",
    "VLLMProvider",
]

_LOG = logging.getLogger(__name__)


def _extract_responses_text(data: Any) -> str:
    """Extract assistant text from an OpenAI Responses API payload.

    The Responses API returns the generated text in two equivalent
    shapes. The SDK-style convenience field ``output_text`` is preferred
    when present; otherwise we walk the structured ``output`` array and
    concatenate every ``output_text`` content part of the assistant
    ``message`` items (Responses API ``output[].content[].text``).
    """
    if not isinstance(data, dict):
        return ""

    # 1. Convenience aggregate field (string or list of strings).
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    if isinstance(output_text, list):
        joined = "".join(part for part in output_text if isinstance(part, str))
        if joined:
            return joined

    # 2. Structured ``output`` array walk.
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
    """Minimal provider contract used by the factory."""

    name: str

    def complete(self, prompt: str) -> str:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Synthetic Provider
# ---------------------------------------------------------------------------


class SyntheticLLMProvider:
    """Deterministic, dependency-free provider reserved for isolated tests.

    ``complete`` echoes a short, stable prefix so that tests can assert on
    the exact string without any network or model state.
    """

    name: str = "synthetic"

    def __init__(self, *, model_name: str | None = None, **kwargs: Any) -> None:
        self.model_name = model_name or "synthetic-model"
        self._last_healthy = time.monotonic()

    def complete(self, prompt: str) -> str:
        """Return a deterministic placeholder response."""
        if not isinstance(prompt, str):
            raise TypeError("prompt must be a str")
        flat = " ".join(prompt.split())
        prefix = flat[:32]
        return f"[synthetic] {prefix}"

    def downtime(self) -> int:
        """Synthetic provider is always healthy."""
        return 0


# ---------------------------------------------------------------------------
# vLLM Provider (OpenAI-compatible)
# ---------------------------------------------------------------------------


@dataclass
class VLLMProvider:
    """vLLM provider — self-hosted, OpenAI-compatible API.

    Uses the OpenAI client library pointed at the vLLM endpoint.
    Tracks downtime for fallback decisions.
    """

    name: str = "vllm"
    base_url: str = ""
    model_name: str = "qwen2.5-coder"
    api_key: str = "not-needed"
    timeout: float = 60.0
    max_tokens: int = 2048
    temperature: float = 0.2
    _last_healthy: float = field(default_factory=time.monotonic, init=False)
    _last_error_time: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if not self.base_url:
            self.base_url = os.environ.get(
                "VLLM_BASE_URL", "http://host.docker.internal:8000/v1"
            )
        if not self.api_key:
            self.api_key = os.environ.get("VLLM_API_KEY", "not-needed")

    def complete(self, prompt: str) -> str:
        """Synchronous completion via vLLM (OpenAI-compatible endpoint).

        Uses httpx for the HTTP call to avoid importing openai SDK.
        """
        import httpx

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )

            if response.status_code == 429:
                self._last_error_time = time.monotonic()
                from llm_orchestrator.orchestrator import RateLimitError
                raise RateLimitError(
                    f"vLLM rate limit: {response.status_code}"
                )

            if response.status_code >= 500:
                self._last_error_time = time.monotonic()
                from llm_orchestrator.orchestrator import ProviderUnavailable
                raise ProviderUnavailable(
                    f"vLLM unavailable: {response.status_code}"
                )

            response.raise_for_status()
            self._last_healthy = time.monotonic()
            data = response.json()
            return data["choices"][0]["message"]["content"]

        except httpx.RequestError as exc:
            self._last_error_time = time.monotonic()
            from llm_orchestrator.orchestrator import ProviderUnavailable
            raise ProviderUnavailable(
                f"vLLM connection error: {exc}"
            ) from exc

    def downtime(self) -> int:
        """Return seconds since last healthy response.

        Used by the orchestrator to decide fallback (>=60s → switch).
        """
        if self._last_error_time <= 0:
            return 0
        if self._last_healthy >= self._last_error_time:
            return 0
        return int(time.monotonic() - self._last_healthy)


# ---------------------------------------------------------------------------
# OpenAI Provider
# ---------------------------------------------------------------------------


@dataclass
class OpenAIProvider:
    """OpenAI cloud provider.

    Uses the OpenAI **Responses API** (``POST /v1/responses``). Serves as
    the default fallback when vLLM is unavailable. The legacy Chat
    Completions surface is intentionally NOT used — every OpenAI call in
    this codebase goes through the Responses API.
    """

    name: str = "openai"
    api_key: str = ""
    model_name: str = "gpt-4o"
    timeout: float = 60.0
    max_tokens: int = 2048
    temperature: float = 0.2
    _last_healthy: float = field(default_factory=time.monotonic, init=False)
    _last_error_time: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if not self.api_key:
            self.api_key = os.environ.get("OPENAI_API_KEY", "")

    def complete(self, prompt: str) -> str:
        """Synchronous completion via the OpenAI Responses API."""
        import httpx

        if not self.api_key:
            from llm_orchestrator.orchestrator import ProviderUnavailable
            raise ProviderUnavailable("OpenAI API key not configured")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model_name,
            "input": prompt,
            "max_output_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    "https://api.openai.com/v1/responses",
                    json=payload,
                    headers=headers,
                )

            if response.status_code == 429:
                self._last_error_time = time.monotonic()
                from llm_orchestrator.orchestrator import RateLimitError
                raise RateLimitError(
                    f"OpenAI rate limit: {response.status_code}"
                )

            if response.status_code >= 500:
                self._last_error_time = time.monotonic()
                from llm_orchestrator.orchestrator import ProviderUnavailable
                raise ProviderUnavailable(
                    f"OpenAI unavailable: {response.status_code}"
                )

            response.raise_for_status()
            self._last_healthy = time.monotonic()
            data = response.json()
            return _extract_responses_text(data)

        except httpx.RequestError as exc:
            self._last_error_time = time.monotonic()
            from llm_orchestrator.orchestrator import ProviderUnavailable
            raise ProviderUnavailable(
                f"OpenAI connection error: {exc}"
            ) from exc

    def downtime(self) -> int:
        """Return seconds since last healthy response."""
        if self._last_error_time <= 0:
            return 0
        if self._last_healthy >= self._last_error_time:
            return 0
        return int(time.monotonic() - self._last_healthy)


# ---------------------------------------------------------------------------
# Anthropic Provider
# ---------------------------------------------------------------------------


@dataclass
class AnthropicProvider:
    """Anthropic Claude provider.

    Uses the Anthropic messages API.
    """

    name: str = "anthropic"
    api_key: str = ""
    model_name: str = "claude-sonnet-4-20250514"
    timeout: float = 60.0
    max_tokens: int = 2048
    temperature: float = 0.2
    _last_healthy: float = field(default_factory=time.monotonic, init=False)
    _last_error_time: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if not self.api_key:
            self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    def complete(self, prompt: str) -> str:
        """Synchronous completion via Anthropic API."""
        import httpx

        if not self.api_key:
            from llm_orchestrator.orchestrator import ProviderUnavailable
            raise ProviderUnavailable("Anthropic API key not configured")

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": self.model_name,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
        }

        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.post(
                    "https://api.anthropic.com/v1/messages",
                    json=payload,
                    headers=headers,
                )

            if response.status_code == 429:
                self._last_error_time = time.monotonic()
                from llm_orchestrator.orchestrator import RateLimitError
                raise RateLimitError(
                    f"Anthropic rate limit: {response.status_code}"
                )

            if response.status_code >= 500:
                self._last_error_time = time.monotonic()
                from llm_orchestrator.orchestrator import ProviderUnavailable
                raise ProviderUnavailable(
                    f"Anthropic unavailable: {response.status_code}"
                )

            response.raise_for_status()
            self._last_healthy = time.monotonic()
            data = response.json()
            # Anthropic response format: content[0].text
            content = data.get("content", [])
            if content and isinstance(content, list):
                return content[0].get("text", "")
            return ""

        except httpx.RequestError as exc:
            self._last_error_time = time.monotonic()
            from llm_orchestrator.orchestrator import ProviderUnavailable
            raise ProviderUnavailable(
                f"Anthropic connection error: {exc}"
            ) from exc

    def downtime(self) -> int:
        """Return seconds since last healthy response."""
        if self._last_error_time <= 0:
            return 0
        if self._last_healthy >= self._last_error_time:
            return 0
        return int(time.monotonic() - self._last_healthy)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# Mapping from LLM_PROVIDER env value to the provider class.
_PROVIDER_REGISTRY: dict[str, type] = {
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "vllm": VLLMProvider,
}


class LLMProviderFactory:
    """Factory that builds provider instances from environment configuration.

    The factory dispatches on ``LLM_PROVIDER`` env variable:
    * ``vllm`` — self-hosted vLLM (OpenAI-compatible).
    * ``openai`` — OpenAI cloud.
    * ``anthropic`` — Anthropic Claude.

    For fallback chain construction, use :meth:`from_env_with_fallback`
    which returns a (primary, fallback) tuple suitable for
    :class:`LlmOrchestrator`.
    """

    DEFAULT_PROVIDER: str = "openai"

    @staticmethod
    def known_providers() -> frozenset[str]:
        """Return the set of provider keys understood by the factory."""
        return frozenset(_PROVIDER_REGISTRY)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> LLMProvider:
        """Build a provider instance from ``LLM_PROVIDER`` (default ``openai``).

        ``env`` is optional and primarily used by tests; when omitted the
        process environment (``os.environ``) is consulted.
        """
        source = env if env is not None else os.environ
        raw = source.get("LLM_PROVIDER", cls.DEFAULT_PROVIDER) or cls.DEFAULT_PROVIDER
        key = raw.strip().lower()
        if key not in _PROVIDER_REGISTRY:
            raise ValueError(
                f"Unknown LLM_PROVIDER {raw!r}; expected one of "
                f"{sorted(_PROVIDER_REGISTRY)}"
            )
        provider_cls = _PROVIDER_REGISTRY[key]
        model_name = source.get("LLM_MODEL_NAME")

        kwargs: dict[str, Any] = {}
        if model_name:
            kwargs["model_name"] = model_name

        if provider_cls is VLLMProvider:
            base_url = source.get("VLLM_BASE_URL", "")
            if base_url:
                kwargs["base_url"] = base_url
            api_key = source.get("VLLM_API_KEY", "")
            if api_key:
                kwargs["api_key"] = api_key
        elif provider_cls is OpenAIProvider:
            api_key = source.get("OPENAI_API_KEY", "")
            if api_key:
                kwargs["api_key"] = api_key
        elif provider_cls is AnthropicProvider:
            api_key = source.get("ANTHROPIC_API_KEY", "")
            if api_key:
                kwargs["api_key"] = api_key

        return provider_cls(**kwargs)

    @classmethod
    def from_env_with_fallback(
        cls, env: dict[str, str] | None = None
    ) -> tuple[LLMProvider, LLMProvider | None]:
        """Build primary + fallback provider pair from environment.

        The fallback is determined by:
        - If primary is ``vllm`` → fallback is ``openai`` (if OPENAI_API_KEY set).
        - If primary is ``openai`` → fallback is ``anthropic`` (if ANTHROPIC_API_KEY set).
        - If primary is ``anthropic`` → fallback is ``openai`` (if OPENAI_API_KEY set).
        Returns:
            Tuple of (primary_provider, fallback_provider_or_None).
        """
        source = env if env is not None else os.environ
        primary = cls.from_env(source)

        # Determine fallback
        primary_key = source.get("LLM_PROVIDER", cls.DEFAULT_PROVIDER).strip().lower()

        fallback: LLMProvider | None = None

        if primary_key == "vllm":
            # vLLM → OpenAI fallback
            openai_key = source.get("OPENAI_API_KEY", "")
            if openai_key:
                fallback_model = source.get("LLM_MODEL_NAME", "gpt-4o")
                fallback = OpenAIProvider(
                    api_key=openai_key,
                    model_name=fallback_model,
                )
        elif primary_key == "openai":
            # OpenAI → Anthropic fallback
            anthropic_key = source.get("ANTHROPIC_API_KEY", "")
            if anthropic_key:
                fallback = AnthropicProvider(api_key=anthropic_key)
        elif primary_key == "anthropic":
            # Anthropic → OpenAI fallback
            openai_key = source.get("OPENAI_API_KEY", "")
            if openai_key:
                fallback = OpenAIProvider(api_key=openai_key)

        return primary, fallback

    @classmethod
    def from_dept_config(
        cls,
        dept_llm_config: dict[str, Any] | None,
        env: dict[str, str] | None = None,
    ) -> tuple[LLMProvider, LLMProvider | None]:
        """Build provider pair from department-level LLM override config.

        If ``dept_llm_config`` is None or empty, falls back to
        :meth:`from_env_with_fallback`.

        The dept config shape (from departments.json ``llm_overrides``):
        ```json
        {
            "primary": {"provider": "vllm", "base_url_ref": "vault:..."},
            "fallback": {"provider": "openai", "api_key_ref": "vault:..."}
        }
        ```

        Note: Vault references must be resolved BEFORE calling this method.
        Pass resolved values in the ``env`` dict.
        """
        if not dept_llm_config:
            return cls.from_env_with_fallback(env)

        source = env if env is not None else os.environ

        # Build primary from dept config
        primary_cfg = dept_llm_config.get("primary", {})
        if primary_cfg:
            provider_key = primary_cfg.get("provider", "").strip().lower()
            if provider_key in _PROVIDER_REGISTRY:
                override_env = dict(source)
                override_env["LLM_PROVIDER"] = provider_key
                if "base_url" in primary_cfg:
                    override_env["VLLM_BASE_URL"] = primary_cfg["base_url"]
                if "api_key" in primary_cfg:
                    if provider_key == "vllm":
                        override_env["VLLM_API_KEY"] = primary_cfg["api_key"]
                    elif provider_key == "openai":
                        override_env["OPENAI_API_KEY"] = primary_cfg["api_key"]
                    elif provider_key == "anthropic":
                        override_env["ANTHROPIC_API_KEY"] = primary_cfg["api_key"]
                if "model" in primary_cfg:
                    override_env["LLM_MODEL_NAME"] = primary_cfg["model"]
                primary = cls.from_env(override_env)
            else:
                primary = cls.from_env(source)
        else:
            primary = cls.from_env(source)

        # Build fallback from dept config
        fallback: LLMProvider | None = None
        fallback_cfg = dept_llm_config.get("fallback", {})
        if fallback_cfg:
            fb_provider_key = fallback_cfg.get("provider", "").strip().lower()
            if fb_provider_key in _PROVIDER_REGISTRY:
                fb_kwargs: dict[str, Any] = {}
                if "model" in fallback_cfg:
                    fb_kwargs["model_name"] = fallback_cfg["model"]
                if "api_key" in fallback_cfg:
                    fb_kwargs["api_key"] = fallback_cfg["api_key"]
                if "base_url" in fallback_cfg:
                    fb_kwargs["base_url"] = fallback_cfg["base_url"]
                fb_cls = _PROVIDER_REGISTRY[fb_provider_key]
                fallback = fb_cls(**fb_kwargs)

        return primary, fallback
