"""Department-level LLM provider resolution (Feature 6).

Implements the resolution chain:
    dept-override → global fallback

When a department has ``llm_overrides`` configured in departments.json,
the resolver uses the department-specific provider/credentials. Otherwise
it falls back to the global ``LLM_PROVIDER`` / ``VLLM_BASE_URL`` /
``OPENAI_API_KEY`` environment variables.

Resolution order:
1. Check ``llm_overrides.primary`` for the department.
2. If primary fails or is not configured, check ``llm_overrides.fallback``.
3. If neither is configured, use ``LLMProviderFactory.from_env()`` (global).

Audit event ``llm_provider_resolved`` is emitted with:
- provider: str (e.g. "vllm", "openai", "anthropic")
- source: str ("dept_override_primary" | "dept_override_fallback" | "global")
- dept_id: str
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .provider import LLMProvider, LLMProviderFactory

__all__ = [
    "LLMResolution",
    "resolve_llm_for_department",
]

logger = logging.getLogger(__name__)

#: Path to departments.json — resolved relative to the platform config dir.
_DEPARTMENTS_CONFIG_PATH = (
    Path(__file__).resolve().parents[4] / "config" / "departments.json"
)


@dataclass(frozen=True)
class LLMResolution:
    """Result of LLM provider resolution for a department.

    Attributes
    ----------
    provider : LLMProvider
        The resolved provider instance.
    provider_name : str
        Provider identifier (e.g. "vllm", "openai", "anthropic").
    source : str
        Where the provider was resolved from:
        "dept_override_primary", "dept_override_fallback", or "global".
    dept_id : str
        Department that requested the resolution.
    """

    provider: LLMProvider
    provider_name: str
    source: str
    dept_id: str


def _load_dept_config(dept_id: str) -> dict[str, Any] | None:
    """Load a specific department's config from departments.json."""
    try:
        with open(_DEPARTMENTS_CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
        departments = data.get("departments", [])
        return next((d for d in departments if d.get("id") == dept_id), None)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "dept_resolver: failed to load departments.json: %s", exc
        )
        return None


def _try_dept_override(
    dept_id: str,
    override: dict[str, Any] | None,
    slot_name: str,
) -> LLMResolution | None:
    """Attempt to build a provider from a dept override slot.

    Returns None if the override is not configured or the provider
    cannot be instantiated.
    """
    if not override:
        return None

    provider_name = override.get("provider", "").strip().lower()
    if not provider_name:
        return None

    # Build an env dict that the factory can consume.
    env_override: dict[str, str] = {"LLM_PROVIDER": provider_name}

    # Resolve base_url_ref / api_key_ref — in production these would
    # be fetched from Vault. For now we check if the ref points to an
    # env var or use it as a direct value hint.
    base_url_ref = override.get("base_url_ref", "")
    api_key_ref = override.get("api_key_ref", "")

    if base_url_ref:
        # If it's a vault ref, the actual value should be in env as
        # DEPT_{DEPT_ID}_VLLM_BASE_URL or fallback to global.
        env_key = f"DEPT_{dept_id.upper()}_VLLM_BASE_URL"
        base_url = os.environ.get(env_key, os.environ.get("VLLM_BASE_URL", ""))
        if base_url:
            env_override["VLLM_BASE_URL"] = base_url

    if api_key_ref:
        env_key = f"DEPT_{dept_id.upper()}_OPENAI_API_KEY"
        api_key = os.environ.get(env_key, os.environ.get("OPENAI_API_KEY", ""))
        if api_key:
            env_override["OPENAI_API_KEY"] = api_key

    model_name = override.get("model_name") or os.environ.get("LLM_MODEL_NAME")
    if model_name:
        env_override["LLM_MODEL_NAME"] = model_name

    try:
        provider = LLMProviderFactory.from_env(env_override)
        return LLMResolution(
            provider=provider,
            provider_name=provider_name,
            source=f"dept_override_{slot_name}",
            dept_id=dept_id,
        )
    except (ValueError, NotImplementedError) as exc:
        logger.info(
            "dept_resolver: %s override for dept=%s failed: %s",
            slot_name,
            dept_id,
            exc,
        )
        return None


def resolve_llm_for_department(dept_id: str) -> LLMResolution:
    """Resolve the LLM provider for a department.

    Resolution chain: dept primary → dept fallback → global.

    Parameters
    ----------
    dept_id:
        Department identifier (e.g. "payment", "hr").

    Returns
    -------
    LLMResolution
        The resolved provider with metadata about the resolution source.
    """
    dept_config = _load_dept_config(dept_id)

    if dept_config is not None:
        llm_overrides = dept_config.get("llm_overrides")
        if llm_overrides and isinstance(llm_overrides, dict):
            # Try primary.
            primary = _try_dept_override(
                dept_id, llm_overrides.get("primary"), "primary"
            )
            if primary is not None:
                logger.info(
                    "llm_provider_resolved dept=%s provider=%s source=%s",
                    dept_id,
                    primary.provider_name,
                    primary.source,
                )
                return primary

            # Try fallback.
            fallback = _try_dept_override(
                dept_id, llm_overrides.get("fallback"), "fallback"
            )
            if fallback is not None:
                logger.info(
                    "llm_provider_resolved dept=%s provider=%s source=%s",
                    dept_id,
                    fallback.provider_name,
                    fallback.source,
                )
                return fallback

    # Global fallback.
    provider = LLMProviderFactory.from_env()
    provider_name = getattr(provider, "name", "unknown")
    resolution = LLMResolution(
        provider=provider,
        provider_name=provider_name,
        source="global",
        dept_id=dept_id,
    )
    logger.info(
        "llm_provider_resolved dept=%s provider=%s source=%s",
        dept_id,
        resolution.provider_name,
        resolution.source,
    )
    return resolution
