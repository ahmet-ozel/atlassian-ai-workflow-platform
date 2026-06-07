"""Per-model tuning-capability lookup.

A small, dependency-free registry that answers two questions about a
model identifier:

* does the model accept a ``reasoning_effort`` knob?
* does the model accept an output ``verbosity`` knob?

The answers drive both the admin UI (it only renders a field the
chosen model actually honours) and the runtime (it only forwards a
tuning parameter the upstream will accept). The matching is prefix /
substring based so dated snapshots like ``gpt-5.1-2025-11-01`` or
``o3-mini-2025-01-31`` resolve to the same capability profile as their
base model.

Reasoning-capable families:
    * OpenAI o-series: ``o1``, ``o3``, ``o4`` (and ``-mini`` variants).
    * OpenAI gpt-5 family: ``gpt-5``, ``gpt-5.1``, ``gpt-5.5`` …
    * Anthropic extended-thinking models: ``claude-opus-4`` /
      ``claude-sonnet-4`` and the ``-thinking`` snapshots.

Verbosity-capable families:
    * OpenAI gpt-5 family only (the ``text.verbosity`` knob shipped
      with gpt-5).
"""

from __future__ import annotations

__all__ = [
    "supports_reasoning_effort",
    "supports_verbosity",
    "model_capabilities",
]


def _normalise(model: str) -> str:
    return (model or "").strip().lower()


def _is_openai_reasoning(model: str) -> bool:
    """True for OpenAI o-series + gpt-5 family identifiers."""
    # o-series: o1, o3, o4 (+ -mini / -pro / dated snapshots).
    for prefix in ("o1", "o3", "o4"):
        if model == prefix or model.startswith(prefix + "-"):
            return True
    # gpt-5 family: gpt-5, gpt-5.1, gpt-5.5, gpt-5-mini, gpt-5o …
    return model.startswith("gpt-5")


def _is_anthropic_reasoning(model: str) -> bool:
    """True for Claude models exposing extended-thinking effort."""
    if "thinking" in model:
        return True
    # Claude 4 generation (opus/sonnet 4.x) accepts thinking budgets.
    return model.startswith("claude-opus-4") or model.startswith(
        "claude-sonnet-4"
    )


def supports_reasoning_effort(model: str) -> bool:
    """Return ``True`` when *model* accepts a ``reasoning_effort`` knob."""
    norm = _normalise(model)
    if not norm:
        return False
    return _is_openai_reasoning(norm) or _is_anthropic_reasoning(norm)


def supports_verbosity(model: str) -> bool:
    """Return ``True`` when *model* accepts an output ``verbosity`` knob.

    Only the OpenAI gpt-5 family ships the ``text.verbosity`` control.
    """
    norm = _normalise(model)
    if not norm:
        return False
    return norm.startswith("gpt-5")


def model_capabilities(model: str) -> dict[str, bool]:
    """Return the capability flags for *model* as a plain dict.

    Shape: ``{"reasoning_effort": bool, "verbosity": bool}`` - consumed
    by the read-side DTO so the UI can render the right inputs without
    re-deriving the rules client-side for an already-saved provider.
    """
    return {
        "reasoning_effort": supports_reasoning_effort(model),
        "verbosity": supports_verbosity(model),
    }
