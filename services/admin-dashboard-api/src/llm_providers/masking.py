"""``API_Key_Mask`` - pure last-4-char masking helper (Requirements 4.2, 4.3).

Used by every read endpoint in the LLM provider management surface to
project credentials before they leave the process. The function is
intentionally pure (no globals, no I/O) so it can be exercised by
property-based tests against arbitrary Hypothesis ``text()`` strategies
without infrastructure.
"""

from __future__ import annotations


__all__ = ["mask"]


def mask(value: str | None) -> str:
    """Return the masked form of an LLM credential string.

    Returns ``"…" + value[-4:]`` when ``len(value) >= 4`` so the
    operator can recognise which credential they configured without
    learning the full secret. Anything shorter (``None`` / empty /
    1-3 chars) collapses to the bare ``"…"`` sentinel; this keeps
    the contract round-trip-safe for both real Anthropic / OpenAI /
    Gemini API keys (always ≥ 4 chars) and the empty/optional
    ``org_id`` slot which we still surface through the same path.

    Requirements 4.2, 4.3:

    * Never returns the unmasked credential.
    * Always returns a string suitable for direct JSON serialisation.
    * Result length is at most ``len(value) + 1`` (the leading "…"
      placeholder).
    """

    if value is None:
        return "…"
    if len(value) >= 4:
        return "…" + value[-4:]
    return "…"
