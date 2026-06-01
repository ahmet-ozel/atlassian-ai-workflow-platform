"""llm-orchestrator: provider-agnostic LLM factory for the multi-service scaffold.

Re-exports the public API so callers can simply do::

    from llm_orchestrator import LLMProviderFactory
    from llm_orchestrator import LlmOrchestrator, RateLimitError, ProviderUnavailable
    from llm_orchestrator import FallbackLLMProviderFactory, LLMProviderConfig

The provider factory covers the production LLM contract (Spec 1).
The :class:`LlmOrchestrator` plus its retry / fallback policy
implements task 4.3 of ``platform-mimari-ops``.
The :class:`FallbackLLMProviderFactory` implements task 21.1 of
``platform-completion`` (LLM Fallback Auto-Switch).
"""

from .fallback import (
    FallbackEvent,
    FallbackLLMProviderFactory,
    LLMProviderConfig,
    LLMServerError,
    LLMTimeoutError,
    LLMUnavailableError,
)
from .orchestrator import (
    LlmOrchestrator,
    LlmProviderStream,
    ProviderChunk,
    ProviderUnavailable,
    RateLimitError,
)
from .provider import (
    AnthropicProvider,
    LLMProviderFactory,
    OpenAIProvider,
    SyntheticLLMProvider,
    VLLMProvider,
)

__all__ = [
    "AnthropicProvider",
    "FallbackEvent",
    "FallbackLLMProviderFactory",
    "LLMProviderConfig",
    "LLMProviderFactory",
    "LLMServerError",
    "LLMTimeoutError",
    "LLMUnavailableError",
    "LlmOrchestrator",
    "LlmProviderStream",
    "OpenAIProvider",
    "SyntheticLLMProvider",
    "ProviderChunk",
    "ProviderUnavailable",
    "RateLimitError",
    "VLLMProvider",
]
