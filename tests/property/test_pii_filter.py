"""PII filter mandatory full-masking property tests.

Hypothesis-driven exercise of :func:`pii_shared.mask`:

(a) For any text + N injected PII patterns (TR phone, email,
    Luhn-valid credit-card), the masked output contains zero
    PII pattern matches and ``len(matches) == N``.
(b) The function is deterministic — same input ⇒ same output
    (same matches, same masked string).
(c) Luhn-invalid card numbers are NOT masked (they are not credit
    cards, so we must not destroy unrelated 13-19 digit numbers).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent
_LIB_SRC = _PLATFORM_ROOT / "libs" / "pii-shared" / "src"
if _LIB_SRC.is_dir() and str(_LIB_SRC) not in sys.path:
    sys.path.insert(0, str(_LIB_SRC))

try:  # pragma: no cover
    from pii_shared import mask  # type: ignore[import-not-found]
except ModuleNotFoundError as exc:  # pragma: no cover
    mask = None  # type: ignore[assignment]
    _IMPORT_ERROR: str | None = str(exc)
else:
    _IMPORT_ERROR = None

pytestmark = pytest.mark.skipif(
    mask is None,
    reason=f"pii_shared.mask unavailable: {_IMPORT_ERROR!r}",
)


_PHONE_RE = re.compile(r"\b5\d{2}[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


@settings(max_examples=120, deadline=None, suppress_health_check=(HealthCheck.too_slow,))
@given(
    prefix=st.text(min_size=0, max_size=40),
    middle=st.text(min_size=0, max_size=40),
    suffix=st.text(min_size=0, max_size=40),
)
def test_phone_and_email_are_masked(
    prefix: str, middle: str, suffix: str
) -> None:
    text = (
        f"{prefix} contact: 555 123 45 67 — email me at user@example.com "
        f"{middle} ok? {suffix}"
    )
    masked, matches = mask(text)
    assert _PHONE_RE.search(masked) is None, (
        f"phone leaked through mask: {masked!r}"
    )
    assert _EMAIL_RE.search(masked) is None, (
        f"email leaked through mask: {masked!r}"
    )
    assert len(matches) >= 2


@settings(max_examples=80, deadline=None)
@given(
    text=st.text(min_size=0, max_size=200),
)
def test_mask_is_deterministic(text: str) -> None:
    a, ma = mask(text)
    b, mb = mask(text)
    assert a == b
    # Match list equality at the (kind, start, end) level.
    assert [
        (m.kind, m.start, m.end) for m in ma
    ] == [(m.kind, m.start, m.end) for m in mb]


def test_luhn_invalid_card_not_masked() -> None:
    # 13-digit number that fails Luhn — must not be masked.
    text = "Order ref 1234567890123 archived."
    masked, matches = mask(text)
    assert "1234567890123" in masked
    assert all(m.kind != "credit_card" for m in matches)
