"""Property tests for ``http_shared.make_mcp_client`` header injection
and ``LLMProviderFactory.from_env()`` dispatch correctness.

Validates: Requirements 13.1, 13.3
Property 10: ``make_mcp_client`` always injects ``X-Client-Source``.

The factory in ``libs/http-shared`` is the single point that creates
``httpx.AsyncClient`` instances for outgoing MCP / Firecrawl calls. Two
invariants must hold for every constructed client:

1. ``client.headers["X-Client-Source"]`` equals the ``client_source``
   string passed to the factory, regardless of which Component identity
   is supplied.
2. Any caller-supplied ``X-Client-Source`` header (in any letter
   casing) is overridden by the factory's value, so callers cannot
   accidentally spoof another Component's identity.

**Validates: Requirements 1.1**
Property 2: Provider Factory Dispatch Correctness.

For any valid ``LLM_PROVIDER`` value from the set
``{mock, vllm, openai, anthropic}``, ``LLMProviderFactory.from_env()``
SHALL return an instance of the corresponding provider class, and the
instance SHALL satisfy the ``LLMProvider`` protocol.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from http_shared import KNOWN_CLIENT_SOURCES, make_mcp_client

# httpx encodes header values as ASCII by default and rejects newline
# characters in headers, so the generator is constrained to printable
# ASCII (excluding whitespace control characters). This still exercises
# the full space of legal HTTP token-like values, including symbols and
# digits, while keeping the property well-defined.
_HEADER_VALUE_ALPHABET = st.characters(min_codepoint=0x21, max_codepoint=0x7E)
_CLIENT_SOURCE_TEXT = st.text(alphabet=_HEADER_VALUE_ALPHABET, max_size=64)
_CLIENT_SOURCE_TEXT_NONEMPTY = st.text(
    alphabet=_HEADER_VALUE_ALPHABET, min_size=1, max_size=64
)


def _close(client: httpx.AsyncClient) -> None:
    """Synchronously close an ``httpx.AsyncClient`` created in a test."""

    asyncio.run(client.aclose())


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(client_source=_CLIENT_SOURCE_TEXT)
def test_make_mcp_client_injects_x_client_source(client_source: str) -> None:
    """Property 10a — header value equals the supplied ``client_source``.

    For every ``client_source`` string the factory accepts, the resulting
    client must echo it back via ``client.headers["X-Client-Source"]``.
    """

    client = make_mcp_client(client_source)
    try:
        assert client.headers["X-Client-Source"] == client_source
        # ``httpx.Headers`` normalises key casing on lookup; confirm the
        # canonical header name maps back to the same value via either
        # casing the caller might use.
        assert client.headers["x-client-source"] == client_source
    finally:
        _close(client)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    client_source=_CLIENT_SOURCE_TEXT_NONEMPTY,
    spoofed=_CLIENT_SOURCE_TEXT,
    spoof_key=st.sampled_from(
        [
            "X-Client-Source",
            "x-client-source",
            "X-CLIENT-SOURCE",
            "x-Client-source",
        ]
    ),
)
def test_caller_supplied_x_client_source_is_overridden(
    client_source: str, spoofed: str, spoof_key: str
) -> None:
    """Property 10b — factory header wins over caller-supplied header.

    Even when callers pass ``headers={"X-Client-Source": "..."}`` (or any
    case variant of the same name), the factory's value must win on the
    case-insensitive collision.
    """

    client = make_mcp_client(client_source, headers={spoof_key: spoofed})
    try:
        assert client.headers["X-Client-Source"] == client_source
        # And the spoofed value must not survive under any casing.
        for variant in ("X-Client-Source", "x-client-source", spoof_key):
            assert client.headers[variant] == client_source
    finally:
        _close(client)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    client_source=_CLIENT_SOURCE_TEXT_NONEMPTY,
    other_key=st.text(
        alphabet=st.characters(
            whitelist_categories=("Ll", "Lu", "Nd"),
            min_codepoint=0x30,
            max_codepoint=0x7A,
        ),
        min_size=1,
        max_size=32,
    ).filter(lambda s: s.lower() != "x-client-source"),
    other_value=_CLIENT_SOURCE_TEXT,
)
def test_unrelated_caller_headers_are_preserved(
    client_source: str, other_key: str, other_value: str
) -> None:
    """Property 10c — non-colliding caller headers survive intact.

    The factory must merge unrelated caller-supplied headers without
    touching them; only the ``X-Client-Source`` slot is reserved.
    """

    client = make_mcp_client(
        client_source, headers={other_key: other_value, "X-Client-Source": "spoofed"}
    )
    try:
        assert client.headers["X-Client-Source"] == client_source
        assert client.headers[other_key] == other_value
    finally:
        _close(client)


@pytest.mark.parametrize("known_source", sorted(KNOWN_CLIENT_SOURCES))
def test_make_mcp_client_accepts_every_known_client_source(known_source: str) -> None:
    """Smoke check — each documented Component identity round-trips."""

    client = make_mcp_client(known_source)
    try:
        assert client.headers["X-Client-Source"] == known_source
    finally:
        _close(client)


@pytest.mark.parametrize(
    "spoof_key",
    ["X-Client-Source", "x-client-source", "X-CLIENT-SOURCE", "x-Client-source"],
)
def test_caller_cannot_spoof_known_identity(spoof_key: str) -> None:
    """Concrete example — caller-supplied identity is overridden."""

    client = make_mcp_client(
        "automation-service", headers={spoof_key: "agent-runner-worker"}
    )
    try:
        assert client.headers["X-Client-Source"] == "automation-service"
    finally:
        _close(client)


def test_caller_supplied_headers_can_be_httpx_headers_object() -> None:
    """Concrete example — ``httpx.Headers`` input is normalised correctly."""

    caller_headers: Any = httpx.Headers(
        [("Accept", "application/json"), ("X-Client-Source", "spoofed")]
    )
    client = make_mcp_client("assistant-service", headers=caller_headers)
    try:
        assert client.headers["X-Client-Source"] == "assistant-service"
        assert client.headers["Accept"] == "application/json"
    finally:
        _close(client)


def test_caller_supplied_headers_can_be_iterable_of_pairs() -> None:
    """Concrete example — iterable-of-tuples input is normalised correctly."""

    pairs = [("Accept", "application/json"), ("X-Client-Source", "spoofed")]
    client = make_mcp_client("assistant-service", headers=pairs)
    try:
        assert client.headers["X-Client-Source"] == "assistant-service"
        assert client.headers["Accept"] == "application/json"
    finally:
        _close(client)


# ---------------------------------------------------------------------------
# Feature: platform-quick-fixes, Property 2: Provider Factory Dispatch Correctness
# ---------------------------------------------------------------------------

from llm_orchestrator import LLMProviderFactory
from llm_orchestrator.provider import (
    AnthropicProvider,
    LLMProvider,
    OpenAIProvider,
    VLLMProvider,
)

# Mapping from provider key to expected class — mirrors the internal registry.
_EXPECTED_PROVIDER_CLASS: dict[str, type] = {
    "vllm": VLLMProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
}

# Strategy: draw from the valid provider set.
_VALID_PROVIDERS = st.sampled_from(sorted(_EXPECTED_PROVIDER_CLASS.keys()))


def _make_env_for_provider(provider_key: str) -> dict[str, str]:
    """Build a minimal env dict that satisfies the provider's requirements.

    Each provider needs specific env vars to instantiate without error:
    - vllm: VLLM_BASE_URL (valid URL)
    - openai: OPENAI_API_KEY (non-empty)
    - anthropic: ANTHROPIC_API_KEY (non-empty)
    """
    env: dict[str, str] = {"LLM_PROVIDER": provider_key}
    if provider_key == "vllm":
        env["VLLM_BASE_URL"] = "http://localhost:8000/v1"
        env["VLLM_API_KEY"] = "not-needed"
    elif provider_key == "openai":
        env["OPENAI_API_KEY"] = "sk-test-key-for-property-test"
    elif provider_key == "anthropic":
        env["ANTHROPIC_API_KEY"] = "sk-ant-test-key-for-property-test"
    return env


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(provider_key=_VALID_PROVIDERS)
def test_provider_factory_dispatch_returns_correct_class(provider_key: str) -> None:
    """Property 2a — factory returns the correct provider class for each valid key.

    **Validates: Requirements 1.1**

    For every valid LLM_PROVIDER value, LLMProviderFactory.from_env() must
    return an instance of the corresponding provider class.
    """
    env = _make_env_for_provider(provider_key)
    provider = LLMProviderFactory.from_env(env)

    expected_cls = _EXPECTED_PROVIDER_CLASS[provider_key]
    assert isinstance(provider, expected_cls), (
        f"Expected {expected_cls.__name__} for LLM_PROVIDER={provider_key!r}, "
        f"got {type(provider).__name__}"
    )


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(provider_key=_VALID_PROVIDERS)
def test_provider_factory_dispatch_satisfies_protocol(provider_key: str) -> None:
    """Property 2b — factory-produced instance satisfies the LLMProvider protocol.

    **Validates: Requirements 1.1**

    The LLMProvider protocol requires:
    - A ``name`` attribute (str)
    - A ``complete(prompt: str) -> str`` method

    Every instance returned by the factory must satisfy this runtime-checkable
    protocol regardless of which provider key is selected.
    """
    env = _make_env_for_provider(provider_key)
    provider = LLMProviderFactory.from_env(env)

    # Runtime protocol check (LLMProvider is @runtime_checkable)
    assert isinstance(provider, LLMProvider), (
        f"Provider {type(provider).__name__} does not satisfy LLMProvider protocol"
    )

    # Verify the name attribute matches the provider key
    assert hasattr(provider, "name"), "Provider must have a 'name' attribute"
    assert provider.name == provider_key, (
        f"Expected provider.name == {provider_key!r}, got {provider.name!r}"
    )

    # Verify the complete method exists and is callable
    assert hasattr(provider, "complete"), "Provider must have a 'complete' method"
    assert callable(provider.complete), "Provider.complete must be callable"


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    provider_key=_VALID_PROVIDERS,
    case_variant=st.sampled_from(["lower", "upper", "mixed", "padded"]),
)
def test_provider_factory_dispatch_case_insensitive(
    provider_key: str, case_variant: str
) -> None:
    """Property 2c — factory dispatch is case-insensitive and whitespace-tolerant.

    **Validates: Requirements 1.1**

    The factory normalizes the LLM_PROVIDER value (strip + lower) before
    dispatch, so case variants and leading/trailing whitespace must still
    resolve to the correct provider class.
    """
    # Apply case variant
    if case_variant == "lower":
        raw_key = provider_key.lower()
    elif case_variant == "upper":
        raw_key = provider_key.upper()
    elif case_variant == "mixed":
        raw_key = "".join(
            c.upper() if i % 2 == 0 else c.lower()
            for i, c in enumerate(provider_key)
        )
    else:  # padded
        raw_key = f"  {provider_key}  "

    env = _make_env_for_provider(provider_key)
    env["LLM_PROVIDER"] = raw_key

    provider = LLMProviderFactory.from_env(env)

    expected_cls = _EXPECTED_PROVIDER_CLASS[provider_key]
    assert isinstance(provider, expected_cls), (
        f"Expected {expected_cls.__name__} for LLM_PROVIDER={raw_key!r}, "
        f"got {type(provider).__name__}"
    )


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(provider_key=_VALID_PROVIDERS)
def test_provider_factory_known_providers_includes_all_valid(
    provider_key: str,
) -> None:
    """Property 2d — known_providers() always contains all valid provider keys.

    **Validates: Requirements 1.1**

    The factory's known_providers() set must include every valid provider key
    that from_env() can dispatch to.
    """
    known = LLMProviderFactory.known_providers()
    assert provider_key in known, (
        f"Provider key {provider_key!r} not in known_providers(): {known}"
    )
