"""Unit tests for Settings.validate_provider_credentials.

Tests the fail-fast credential validation logic:
  - openai provider requires OPENAI_API_KEY
  - anthropic provider requires ANTHROPIC_API_KEY
  - vllm provider requires a valid VLLM_BASE_URL and VLLM_API_KEY
  - unknown providers are rejected
  - ConfigurationError is raised for missing/invalid credentials
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the service root is on sys.path so ``from src.config import ...`` works.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

import pytest

from src.config import ConfigurationError, Settings, _is_valid_url


# ---------------------------------------------------------------------------
# _is_valid_url helper tests
# ---------------------------------------------------------------------------


class TestIsValidUrl:
    """Tests for the _is_valid_url helper function."""

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8000/v1",
            "https://api.example.com",
            "http://host.docker.internal:8000/v1",
            "https://vllm.internal.corp:443/api",
        ],
    )
    def test_valid_urls(self, url: str) -> None:
        assert _is_valid_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "not-a-url",
            "localhost:8000",
            "/v1/completions",
            "://missing-scheme",
            "ftp://",  # scheme but no netloc
        ],
    )
    def test_invalid_urls(self, url: str) -> None:
        assert _is_valid_url(url) is False


# ---------------------------------------------------------------------------
# validate_provider_credentials tests
# ---------------------------------------------------------------------------


class TestValidateProviderCredentials:
    """Tests for Settings.validate_provider_credentials()."""

    def _make_settings(self, **overrides) -> Settings:
        """Create a Settings instance with env vars overridden."""
        defaults = {
            "llm_provider": "openai",
            "openai_api_key": "sk-test-key-123",
            "anthropic_api_key": "",
            "vllm_base_url": "http://host.docker.internal:8000/v1",
            "vllm_api_key": "not-needed",
        }
        defaults.update(overrides)
        return Settings(**defaults)

    # --- unknown provider: fail closed ---

    def test_unknown_provider_raises(self) -> None:
        """Unsupported provider values are rejected at boot validation."""
        settings = self._make_settings(llm_provider="synthetic")
        with pytest.raises(ConfigurationError, match="LLM_PROVIDER must be one of"):
            settings.validate_provider_credentials()

    # --- openai provider ---

    def test_openai_provider_missing_key_raises(self) -> None:
        """openai provider with empty OPENAI_API_KEY raises ConfigurationError."""
        settings = self._make_settings(llm_provider="openai", openai_api_key="")
        with pytest.raises(ConfigurationError, match="OPENAI_API_KEY required"):
            settings.validate_provider_credentials()

    def test_openai_provider_with_valid_key_passes(self) -> None:
        """openai provider with a non-empty key passes validation."""
        settings = self._make_settings(
            llm_provider="openai", openai_api_key="sk-test-key-123"
        )
        settings.validate_provider_credentials()

    # --- anthropic provider ---

    def test_anthropic_provider_missing_key_raises(self) -> None:
        """anthropic provider with empty ANTHROPIC_API_KEY raises ConfigurationError."""
        settings = self._make_settings(llm_provider="anthropic", anthropic_api_key="")
        with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY required"):
            settings.validate_provider_credentials()

    def test_anthropic_provider_with_valid_key_passes(self) -> None:
        """anthropic provider with a non-empty key passes validation."""
        settings = self._make_settings(
            llm_provider="anthropic", anthropic_api_key="sk-ant-test-key"
        )
        settings.validate_provider_credentials()

    # --- vllm provider ---

    def test_vllm_provider_empty_url_raises(self) -> None:
        """vllm provider with empty VLLM_BASE_URL raises ConfigurationError."""
        settings = self._make_settings(llm_provider="vllm", vllm_base_url="")
        with pytest.raises(ConfigurationError, match="VLLM_BASE_URL must be a valid URL"):
            settings.validate_provider_credentials()

    def test_vllm_provider_invalid_url_raises(self) -> None:
        """vllm provider with invalid URL format raises ConfigurationError."""
        settings = self._make_settings(llm_provider="vllm", vllm_base_url="not-a-url")
        with pytest.raises(ConfigurationError, match="VLLM_BASE_URL must be a valid URL"):
            settings.validate_provider_credentials()

    def test_vllm_provider_path_only_url_raises(self) -> None:
        """vllm provider with path-only URL (no scheme/netloc) raises."""
        settings = self._make_settings(llm_provider="vllm", vllm_base_url="/v1/completions")
        with pytest.raises(ConfigurationError, match="VLLM_BASE_URL must be a valid URL"):
            settings.validate_provider_credentials()

    def test_vllm_provider_missing_key_raises(self) -> None:
        """vllm provider with empty VLLM_API_KEY raises ConfigurationError."""
        settings = self._make_settings(llm_provider="vllm", vllm_api_key="")
        with pytest.raises(ConfigurationError, match="VLLM_API_KEY required"):
            settings.validate_provider_credentials()

    def test_vllm_provider_with_valid_url_passes(self) -> None:
        """vllm provider with a valid URL passes validation."""
        settings = self._make_settings(
            llm_provider="vllm",
            vllm_base_url="http://localhost:8000/v1",
            vllm_api_key="not-needed",
        )
        settings.validate_provider_credentials()

    def test_vllm_provider_https_url_passes(self) -> None:
        """vllm provider with HTTPS URL passes validation."""
        settings = self._make_settings(
            llm_provider="vllm",
            vllm_base_url="https://vllm.internal:443/api",
            vllm_api_key="vllm-key",
        )
        settings.validate_provider_credentials()
