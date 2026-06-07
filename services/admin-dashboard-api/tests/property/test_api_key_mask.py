"""- API key masking is "…" + last-4.
* :func:`llm_providers.masking.mask` returns a string starting with
  ``"…"`` for every non-empty input.
* The result length is at most ``len(input) + 1`` so the mask never
  expands the input by more than the leading placeholder character.
* When the input is ≥ 4 chars the last four characters survive
  verbatim - operators can recognise the credential they configured
  without learning the full secret."""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings, strategies as st


_API_ROOT = Path(__file__).resolve().parents[2]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from src.llm_providers.masking import mask  # noqa: E402


@given(value=st.text())
@settings(max_examples=200, deadline=None)
def test_mask_always_starts_with_ellipsis(value: str) -> None:
    """The mask always begins with the ``"…"`` placeholder."""

    assert mask(value).startswith("…")


@given(value=st.text())
@settings(max_examples=200, deadline=None)
def test_mask_never_exceeds_input_length_plus_one(value: str) -> None:
    """``len(mask(v)) ≤ len(v) + 1`` for every input.

    The leading ``"…"`` adds at most one character; the last-4 slice
    can never make the output longer than the input itself.
    """

    assert len(mask(value)) <= len(value) + 1


@given(value=st.text(min_size=4))
@settings(max_examples=200, deadline=None)
def test_mask_preserves_last_four_for_long_inputs(value: str) -> None:
    """Inputs ≥ 4 chars keep their final 4 characters verbatim."""

    masked = mask(value)
    assert masked.endswith(value[-4:])


@given(value=st.text(max_size=3))
@settings(max_examples=200, deadline=None)
def test_mask_collapses_short_inputs_to_bare_placeholder(value: str) -> None:
    """Inputs shorter than 4 chars collapse to the bare ``"…"`` sentinel."""

    assert mask(value) == "…"


def test_mask_handles_none() -> None:
    """``None`` is treated as the bare placeholder."""

    assert mask(None) == "…"
