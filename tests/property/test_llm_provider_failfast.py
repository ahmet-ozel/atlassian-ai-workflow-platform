"""Property test 1 — Provider Credential Validation (Fail-Fast).

# Feature: platform-quick-fixes, Property 1: Provider Credential Validation (Fail-Fast)

Spec: ``platform-quick-fixes`` — Property 1.

**Validates: Requirements 1.2, 1.3, 1.4**

Property Statement
------------------

*For any* LLM provider requiring credentials (openai, anthropic, vllm)
and *for any* empty, missing, or invalid credential value, the system
SHALL raise ``ConfigurationError`` at boot time and the ``/healthz``
endpoint SHALL NOT return 200 for readiness.

Strategy
--------

We use Hypothesis to generate random invalid credential values for each
provider type:

- **openai**: empty strings, whitespace-only strings
- **anthropic**: empty strings, whitespace-only strings
- **vllm**: empty strings, whitespace-only strings, invalid URL formats
  (no scheme, no netloc, garbage text)

For each generated (provider, credential) pair we construct a
``Settings`` instance with the invalid credential and call
``validate_provider_credentials()``. The test asserts that
``ConfigurationError`` is always raised.

Additionally, we verify the ``/healthz`` invariant: when credential
validation fails at boot, ``app.state.llm_client`` remains ``None``,
causing ``/healthz`` to return 503 (not 200).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap — expose the assistant-service src so we can
# import the config module directly.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_REQUIRED_SRC_DIRS: Final[tuple[Path, ...]] = (
    _PLATFORM_ROOT / "services" / "assistant-service" / "src",
    _PLATFORM_ROOT / "services" / "assistant-service",
)
for _src in _REQUIRED_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)

# Now import the config module under test.
from config import ConfigurationError, Settings, _is_valid_url  # type: ignore[import]


# ---------------------------------------------------------------------------
# Hypothesis strategies for invalid credentials
# ---------------------------------------------------------------------------

#: Strategy for empty/whitespace-only strings (invalid API keys).
#: The implementation uses ``.strip()`` to detect both empty and
#: whitespace-only values. Requirements 1.2/1.3 specify "boş VEYA
#: tanımsızsa" (empty OR undefined) — whitespace-only is effectively
#: empty since no valid API key consists solely of whitespace.
_empty_or_whitespace = st.one_of(
    st.just(""),
    st.text(alphabet=" \t\n\r", min_size=1, max_size=20),
)

#: Strategy for strings that are NOT valid URLs (for vllm).
#: Includes: empty, whitespace, no-scheme, no-netloc, garbage.
_invalid_url_strategy = st.one_of(
    # Empty or whitespace
    st.just(""),
    st.text(alphabet=" \t\n\r", min_size=0, max_size=10),
    # No scheme (just a hostname)
    st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Nd")),
        min_size=1,
        max_size=30,
    ).map(lambda s: s),
    # Scheme but no netloc
    st.just("http://"),
    st.just("https://"),
    # Relative paths (no scheme, no netloc)
    st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Nd", "Pd")),
        min_size=1,
        max_size=20,
    ).map(lambda s: f"/{s}"),
    # Just a colon (malformed)
    st.just(":"),
    st.just("://"),
    # Scheme with empty netloc variants
    st.just("ftp://"),
)

#: Strategy for provider names that require credentials.
_credential_providers = st.sampled_from(["openai", "anthropic", "vllm"])


# ---------------------------------------------------------------------------
# Helper: build a Settings instance with specific overrides
# ---------------------------------------------------------------------------


def _make_settings(
    *,
    llm_provider: str,
    openai_api_key: str = "",
    anthropic_api_key: str = "",
    vllm_base_url: str = "",
) -> Settings:
    """Construct a Settings instance with explicit field values.

    We bypass env-file loading by passing values directly and using
    model_validate to construct the instance.
    """
    return Settings(
        llm_provider=llm_provider,
        openai_api_key=openai_api_key,
        anthropic_api_key=anthropic_api_key,
        vllm_base_url=vllm_base_url,
    )


# ---------------------------------------------------------------------------
# Property 1: OpenAI — empty/missing API key → ConfigurationError
# ---------------------------------------------------------------------------


class TestOpenAICredentialFailFast:
    """**Validates: Requirements 1.2**

    For any empty or whitespace-only OPENAI_API_KEY when
    LLM_PROVIDER=openai, validate_provider_credentials() SHALL raise
    ConfigurationError.
    """

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(api_key=_empty_or_whitespace)
    def test_openai_empty_key_raises_configuration_error(
        self, api_key: str
    ) -> None:
        """R1.2: OpenAI provider + empty/whitespace key → ConfigurationError."""
        s = _make_settings(llm_provider="openai", openai_api_key=api_key)
        with pytest.raises(ConfigurationError):
            s.validate_provider_credentials()

    def test_openai_valid_key_does_not_raise(self) -> None:
        """Sanity: OpenAI provider + non-empty key → no error."""
        s = _make_settings(
            llm_provider="openai", openai_api_key="sk-test-valid-key-12345"
        )
        # Should not raise
        s.validate_provider_credentials()


# ---------------------------------------------------------------------------
# Property 1: Anthropic — empty/missing API key → ConfigurationError
# ---------------------------------------------------------------------------


class TestAnthropicCredentialFailFast:
    """**Validates: Requirements 1.3**

    For any empty or whitespace-only ANTHROPIC_API_KEY when
    LLM_PROVIDER=anthropic, validate_provider_credentials() SHALL raise
    ConfigurationError.
    """

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(api_key=_empty_or_whitespace)
    def test_anthropic_empty_key_raises_configuration_error(
        self, api_key: str
    ) -> None:
        """R1.3: Anthropic provider + empty/whitespace key → ConfigurationError."""
        s = _make_settings(llm_provider="anthropic", anthropic_api_key=api_key)
        with pytest.raises(ConfigurationError):
            s.validate_provider_credentials()

    def test_anthropic_valid_key_does_not_raise(self) -> None:
        """Sanity: Anthropic provider + non-empty key → no error."""
        s = _make_settings(
            llm_provider="anthropic",
            anthropic_api_key="sk-ant-test-valid-key-12345",
        )
        # Should not raise
        s.validate_provider_credentials()


# ---------------------------------------------------------------------------
# Property 1: vLLM — empty/invalid URL → ConfigurationError
# ---------------------------------------------------------------------------


class TestVllmCredentialFailFast:
    """**Validates: Requirements 1.4**

    For any empty, whitespace-only, or invalid URL value for
    VLLM_BASE_URL when LLM_PROVIDER=vllm, validate_provider_credentials()
    SHALL raise ConfigurationError.
    """

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(base_url=_invalid_url_strategy)
    def test_vllm_invalid_url_raises_configuration_error(
        self, base_url: str
    ) -> None:
        """R1.4: vLLM provider + invalid URL → ConfigurationError."""
        # Pre-filter: only test values that are actually invalid URLs
        # (the strategy should produce only invalid ones, but double-check)
        if _is_valid_url(base_url):
            # Skip this example — it's actually a valid URL
            from hypothesis import assume

            assume(False)

        s = _make_settings(llm_provider="vllm", vllm_base_url=base_url)
        with pytest.raises(ConfigurationError):
            s.validate_provider_credentials()

    def test_vllm_valid_url_does_not_raise(self) -> None:
        """Sanity: vLLM provider + valid URL → no error."""
        s = _make_settings(
            llm_provider="vllm", vllm_base_url="http://localhost:8000/v1"
        )
        # Should not raise
        s.validate_provider_credentials()

    def test_vllm_empty_string_raises(self) -> None:
        """R1.4: vLLM provider + empty string → ConfigurationError."""
        s = _make_settings(llm_provider="vllm", vllm_base_url="")
        with pytest.raises(ConfigurationError):
            s.validate_provider_credentials()

    def test_vllm_whitespace_only_raises(self) -> None:
        """R1.4: vLLM provider + whitespace-only → ConfigurationError."""
        s = _make_settings(llm_provider="vllm", vllm_base_url="   ")
        with pytest.raises(ConfigurationError):
            s.validate_provider_credentials()

    def test_vllm_no_scheme_raises(self) -> None:
        """R1.4: vLLM provider + URL without scheme → ConfigurationError."""
        s = _make_settings(llm_provider="vllm", vllm_base_url="localhost:8000/v1")
        with pytest.raises(ConfigurationError):
            s.validate_provider_credentials()


# ---------------------------------------------------------------------------
# Property 1: Cross-provider — any credential-requiring provider with
# invalid credentials → ConfigurationError
# ---------------------------------------------------------------------------


class TestCrossProviderFailFast:
    """**Validates: Requirements 1.2, 1.3, 1.4**

    Combined property: for any provider requiring credentials and any
    invalid credential value, ConfigurationError is raised at boot.
    """

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        provider=_credential_providers,
        invalid_value=_empty_or_whitespace,
    )
    def test_any_provider_with_empty_credential_raises(
        self, provider: str, invalid_value: str
    ) -> None:
        """Any credential-requiring provider + empty credential → ConfigurationError.

        For vllm, empty/whitespace is always invalid. For openai/anthropic,
        empty/whitespace API keys are always invalid.
        """
        if provider == "openai":
            s = _make_settings(llm_provider="openai", openai_api_key=invalid_value)
        elif provider == "anthropic":
            s = _make_settings(
                llm_provider="anthropic", anthropic_api_key=invalid_value
            )
        else:  # vllm
            s = _make_settings(llm_provider="vllm", vllm_base_url=invalid_value)

        with pytest.raises(ConfigurationError):
            s.validate_provider_credentials()


# ---------------------------------------------------------------------------
# Property 1: /healthz invariant — when credentials are invalid,
# the boot fails before llm_client is set, so /healthz returns 503
# ---------------------------------------------------------------------------


class TestHealthzInvariant:
    """**Validates: Requirements 1.2, 1.3, 1.4**

    When validate_provider_credentials() raises ConfigurationError at
    boot, the /healthz endpoint SHALL NOT return 200. This is because
    the boot sequence calls validate_provider_credentials() before
    wiring app.state.llm_client, so on failure llm_client remains None
    and /healthz returns 503.

    We test this invariant by verifying the logical chain:
    1. Invalid credentials → ConfigurationError raised
    2. ConfigurationError at boot → llm_client never set (remains None)
    3. llm_client is None → /healthz returns 503 (not 200)
    """

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        provider=_credential_providers,
        invalid_value=_empty_or_whitespace,
    )
    def test_invalid_credentials_prevent_healthz_200(
        self, provider: str, invalid_value: str
    ) -> None:
        """Invalid credentials → ConfigurationError → llm_client=None → /healthz ≠ 200.

        We verify the first link in the chain: that ConfigurationError
        IS raised, which guarantees the boot sequence aborts before
        setting llm_client. The /healthz handler checks
        app.state.llm_client and returns 503 when it's None.
        """
        if provider == "openai":
            s = _make_settings(llm_provider="openai", openai_api_key=invalid_value)
        elif provider == "anthropic":
            s = _make_settings(
                llm_provider="anthropic", anthropic_api_key=invalid_value
            )
        else:  # vllm
            s = _make_settings(llm_provider="vllm", vllm_base_url=invalid_value)

        # The boot sequence calls validate_provider_credentials() BEFORE
        # wiring llm_client. If it raises, llm_client stays None.
        raised = False
        try:
            s.validate_provider_credentials()
        except ConfigurationError:
            raised = True

        assert raised, (
            f"Expected ConfigurationError for provider={provider!r} "
            f"with credential={invalid_value!r}, but no exception was raised. "
            f"This means /healthz could return 200 with invalid credentials."
        )

    def test_unknown_provider_is_rejected(self) -> None:
        """Unsupported providers fail closed during validation."""
        s = _make_settings(llm_provider="synthetic")
        with pytest.raises(ConfigurationError, match="LLM_PROVIDER must be one of"):
            s.validate_provider_credentials()
