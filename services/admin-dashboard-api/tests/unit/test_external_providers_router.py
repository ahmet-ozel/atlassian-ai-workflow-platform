"""Unit tests for external provider visibility rules."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.routers.external_providers import _external_entry_is_enabled  # noqa: E402


def test_non_optional_external_provider_is_visible() -> None:
    entry = {"name": "openai", "kind": "external"}

    assert _external_entry_is_enabled(entry) is True


def test_optional_provider_default_url_does_not_enable_it() -> None:
    entry = {
        "name": "anthropic",
        "kind": "external",
        "optional": True,
        "enabled_env": "ANTHROPIC_ENABLED",
        "base_url_default": "https://api.anthropic.com/v1",
    }

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ANTHROPIC_ENABLED", None)
        assert _external_entry_is_enabled(entry) is False


def test_optional_provider_can_be_enabled_by_flag() -> None:
    entry = {
        "name": "vllm",
        "kind": "external",
        "optional": True,
        "enabled_env": "VLLM_ENABLED",
        "base_url_default": "http://host.docker.internal:8000/v1",
    }

    with patch.dict(os.environ, {"VLLM_ENABLED": "true"}, clear=False):
        assert _external_entry_is_enabled(entry) is True


def test_optional_provider_can_be_enabled_by_base_url_env() -> None:
    entry = {
        "name": "firecrawl-cloud",
        "kind": "external",
        "optional": True,
        "base_url_env": "FIRECRAWL_CLOUD_BASE_URL",
    }

    with patch.dict(
        os.environ,
        {"FIRECRAWL_CLOUD_BASE_URL": "https://api.firecrawl.dev"},
        clear=False,
    ):
        assert _external_entry_is_enabled(entry) is True
