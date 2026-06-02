"""Unit tests for external provider visibility rules."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.external_probe import clear_cache, probe_external  # noqa: E402
from src.routers.external_providers import (  # noqa: E402
    _external_entry_is_enabled,
    _get_http_client,
)


def test_non_optional_external_provider_is_visible() -> None:
    entry = {"name": "legacy-provider", "kind": "external"}

    assert _external_entry_is_enabled(entry) is True


def test_optional_provider_without_explicit_config_is_hidden() -> None:
    entry = {
        "name": "openai",
        "kind": "external",
        "optional": True,
        "enabled_env": "OPENAI_ENABLED",
        "credential_env": "OPENAI_API_KEY",
        "base_url_default": "https://api.openai.com/v1",
    }

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENAI_ENABLED", None)
        os.environ.pop("OPENAI_API_KEY", None)
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


def test_optional_provider_can_be_enabled_by_credential_env() -> None:
    entry = {
        "name": "openai",
        "kind": "external",
        "optional": True,
        "enabled_env": "OPENAI_ENABLED",
        "credential_env": "OPENAI_API_KEY",
        "base_url_default": "https://api.openai.com/v1",
    }

    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}, clear=False):
        assert _external_entry_is_enabled(entry) is True


def test_optional_provider_default_base_url_env_does_not_enable_it() -> None:
    entry = {
        "name": "vllm",
        "kind": "external",
        "optional": True,
        "enabled_env": "VLLM_ENABLED",
        "base_url_env": "VLLM_BASE_URL",
        "base_url_default": "http://host.docker.internal:8000/v1",
    }

    with patch.dict(
        os.environ,
        {"VLLM_BASE_URL": "http://host.docker.internal:8000/v1"},
        clear=False,
    ):
        os.environ.pop("VLLM_ENABLED", None)
        assert _external_entry_is_enabled(entry) is False


def test_optional_provider_can_be_enabled_by_base_url_env() -> None:
    entry = {
        "name": "firecrawl-cloud",
        "kind": "external",
        "optional": True,
        "base_url_env": "FIRECRAWL_CLOUD_BASE_URL",
        "base_url_default": "https://api.default.invalid",
    }

    with patch.dict(
        os.environ,
        {"FIRECRAWL_CLOUD_BASE_URL": "https://api.firecrawl.dev"},
        clear=False,
    ):
        assert _external_entry_is_enabled(entry) is True


def test_external_provider_probe_does_not_reuse_internal_http_client() -> None:
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http_client=object())))

    assert _get_http_client(request) is None


@pytest.mark.asyncio
async def test_external_probe_retries_with_sync_transport_on_owned_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clear_cache()
    entry = {
        "name": "openai",
        "kind": "external",
        "base_url_default": "https://api.openai.com/v1",
        "probe_path": "/models",
        "probe_method": "GET",
        "probe_expected_status": 200,
    }

    class FailingAsyncClient:
        async def request(self, **_: object) -> httpx.Response:
            raise httpx.ConnectTimeout("async timeout")

        async def aclose(self) -> None:
            return None

    class OkSyncClient:
        def __init__(self, **_: object) -> None:
            pass

        def __enter__(self) -> "OkSyncClient":
            return self

        def __exit__(self, *_: object) -> bool:
            return False

        def request(self, **_: object) -> httpx.Response:
            return httpx.Response(200)

    monkeypatch.setattr(
        "src.lifecycle.external_probe.httpx.AsyncClient",
        lambda **_: FailingAsyncClient(),
    )
    monkeypatch.setattr(
        "src.lifecycle.external_probe.httpx.Client",
        lambda **_: OkSyncClient(),
    )

    result = await probe_external(
        entry,
        bypass_cache=True,
        env={"OPENAI_API_KEY": "sk-test"},
    )

    assert result.status == "ok"
    clear_cache()
