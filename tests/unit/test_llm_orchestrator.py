"""Unit tests for ``libs/llm-orchestrator`` providers and factory.

These tests are intentionally example-based (no Hypothesis). They exercise
the four behaviours the provider package needs to guarantee:

1. ``LLMProviderFactory.from_env`` defaults to ``OpenAIProvider`` when
   ``LLM_PROVIDER`` is unset and dispatches correctly when it is set
   explicitly.
2. ``SyntheticLLMProvider.complete`` is deterministic for isolated tests.
3. The three real providers (``OpenAIProvider``, ``AnthropicProvider``,
   ``VLLMProvider``) construct successfully via the factory and via
   direct instantiation — they are no longer stubs that raise
   ``NotImplementedError`` on ``__init__``. See the docstrings on
   ``test_from_env_dispatches_real_providers_to_not_implemented`` and
   ``test_real_providers_raise_not_implemented_on_direct_instantiation``
   for the original stub-era contract and why the inverted semantics are
   recorded under the historical test names.
4. An unknown ``LLM_PROVIDER`` value raises ``ValueError`` rather than
   silently falling back, so misconfiguration fails loudly.

"""

from __future__ import annotations

import pytest

from llm_orchestrator import LLMProviderFactory, SyntheticLLMProvider
from llm_orchestrator.provider import (
    AnthropicProvider,
    OpenAIProvider,
    VLLMProvider,
)


# ---------------------------------------------------------------------------
# Factory dispatch
# ---------------------------------------------------------------------------


def test_from_env_defaults_to_openai_when_unset() -> None:
    """Empty env produces an ``OpenAIProvider`` (default provider)."""

    provider = LLMProviderFactory.from_env(env={})

    assert isinstance(provider, OpenAIProvider)


def test_from_env_with_llm_provider_synthetic_is_rejected() -> None:
    """Synthetic providers are not selectable through production config."""

    with pytest.raises(ValueError):
        LLMProviderFactory.from_env(env={"LLM_PROVIDER": "synthetic"})


def test_from_env_provider_is_case_insensitive_and_strips_whitespace() -> None:
    """The factory normalises ``LLM_PROVIDER`` (lowercase + strip)."""

    provider = LLMProviderFactory.from_env(env={"LLM_PROVIDER": "  OpenAI  "})

    assert isinstance(provider, OpenAIProvider)


# ---------------------------------------------------------------------------
# SyntheticLLMProvider behaviour
# ---------------------------------------------------------------------------


def test_synthetic_complete_returns_deterministic_placeholder() -> None:
    """``complete("hello")`` returns a stable synthetic response."""

    result = SyntheticLLMProvider().complete("hello")

    assert result.startswith("[synthetic] ")
    assert result == "[synthetic] hello"


def test_synthetic_complete_is_deterministic_across_calls() -> None:
    """Calling ``complete`` twice with the same prompt yields the same output."""

    provider = SyntheticLLMProvider()

    first = provider.complete("hello")
    second = provider.complete("hello")

    assert first == second


def test_synthetic_complete_rejects_non_string_prompt() -> None:
    """Non-string prompts raise ``TypeError`` (defensive guard)."""

    with pytest.raises(TypeError):
        SyntheticLLMProvider().complete(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Real provider stubs — must raise NotImplementedError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env_value", "expected_cls"),
    [
        ("openai", OpenAIProvider),
        ("anthropic", AnthropicProvider),
        ("vllm", VLLMProvider),
    ],
)
def test_from_env_dispatches_real_providers_to_not_implemented(
    env_value: str, expected_cls: type
) -> None:
    """The factory dispatches to the real provider class and constructs it.

    .. note::
       Historically the three real providers raised
       ``NotImplementedError`` on instantiation and this test asserted
       that the factory surfaced that error. The providers have since
       been promoted to real implementations (``@dataclass``-based,
       ``httpx``-backed), so their ``__init__`` no longer raises.

       The function name is preserved verbatim for compatibility with
       existing test references. The semantic is inverted to match the
       current production contract:
       ``from_env(LLM_PROVIDER=<real>)`` returns a constructed instance
       of the expected provider class.

    The contract under test today: the factory dispatches by
    ``LLM_PROVIDER`` value, instantiates the registered class, and
    returns the instance to the caller.
    """

    provider = LLMProviderFactory.from_env(env={"LLM_PROVIDER": env_value})

    assert isinstance(provider, expected_cls)
    assert provider.name == env_value

    # Sanity: the class itself is still importable and matches the
    # registry entry the factory would have used.
    assert expected_cls.__name__ in {
        "OpenAIProvider",
        "AnthropicProvider",
        "VLLMProvider",
    }


@pytest.mark.parametrize(
    "provider_cls",
    [OpenAIProvider, AnthropicProvider, VLLMProvider],
)
def test_real_providers_raise_not_implemented_on_direct_instantiation(
    provider_cls: type,
) -> None:
    """Direct ``OpenAIProvider()`` / etc. construct successfully.

    .. note::
       Historically ``OpenAIProvider()`` /
       ``AnthropicProvider()`` / ``VLLMProvider()`` raised
       ``NotImplementedError`` to flag "stub, wire me up later". They
       are now real implementations and construct without error.

       The function name is preserved verbatim for compatibility with
       existing test references. The semantic is inverted to match the current
       production contract: zero-argument construction succeeds and
       yields an instance with the documented ``name`` attribute.
    """

    instance = provider_cls()

    assert isinstance(instance, provider_cls)
    # Each real provider exposes a stable ``name`` attribute used by the
    # orchestrator's logging and downtime tracking.
    assert isinstance(instance.name, str)
    assert instance.name in {"openai", "anthropic", "vllm"}


# ---------------------------------------------------------------------------
# Unknown provider — must fail loudly
# ---------------------------------------------------------------------------


def test_from_env_unknown_provider_raises_value_error() -> None:
    """An unrecognised ``LLM_PROVIDER`` value raises ``ValueError``."""

    with pytest.raises(ValueError):
        LLMProviderFactory.from_env(env={"LLM_PROVIDER": "definitely-not-a-provider"})


def test_from_env_unknown_provider_error_mentions_the_offending_value() -> None:
    """The error message includes the bad value to aid debugging."""

    with pytest.raises(ValueError, match="not-a-real-llm"):
        LLMProviderFactory.from_env(env={"LLM_PROVIDER": "not-a-real-llm"})
