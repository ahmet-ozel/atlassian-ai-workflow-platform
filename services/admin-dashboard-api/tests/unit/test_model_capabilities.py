"""Unit tests for the per-model tuning-capability lookup.

Verifies which model identifiers expose ``reasoning_effort`` and/or
``verbosity`` so the provider form and the connection probe only emit
parameters the upstream will accept.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_API_ROOT = Path(__file__).resolve().parents[2]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from src.llm_providers.model_capabilities import (  # noqa: E402
    model_capabilities,
    supports_reasoning_effort,
    supports_verbosity,
)


@pytest.mark.parametrize(
    "model",
    [
        "gpt-5",
        "gpt-5.5",
        "gpt-5.1-2025-11-01",
        "gpt-5-mini",
        "o1",
        "o3-mini",
        "o4-mini-2025-04-16",
        "claude-opus-4-20250514",
        "claude-sonnet-4-5-thinking",
    ],
)
def test_reasoning_capable_models(model: str) -> None:
    assert supports_reasoning_effort(model) is True


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o-mini",
        "gpt-4o",
        "gpt-4-turbo",
        "claude-3-5-sonnet-20241022",
        "qwen2.5-coder",
        "",
    ],
)
def test_non_reasoning_models(model: str) -> None:
    assert supports_reasoning_effort(model) is False


@pytest.mark.parametrize("model", ["gpt-5", "gpt-5.5", "gpt-5-mini"])
def test_verbosity_capable_models(model: str) -> None:
    assert supports_verbosity(model) is True


@pytest.mark.parametrize(
    "model",
    ["gpt-4o-mini", "o3-mini", "claude-opus-4-20250514", "qwen2.5-coder", ""],
)
def test_verbosity_incapable_models(model: str) -> None:
    # Only the gpt-5 family ships text.verbosity — o-series and Claude
    # are reasoning-capable but do NOT accept verbosity.
    assert supports_verbosity(model) is False


def test_model_capabilities_shape() -> None:
    caps = model_capabilities("gpt-5.5")
    assert caps == {"reasoning_effort": True, "verbosity": True}

    caps = model_capabilities("o3-mini")
    assert caps == {"reasoning_effort": True, "verbosity": False}

    caps = model_capabilities("gpt-4o-mini")
    assert caps == {"reasoning_effort": False, "verbosity": False}
