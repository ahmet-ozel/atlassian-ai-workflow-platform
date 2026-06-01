"""Unit tests for pii_shared.filter.

These are *example-based* sanity checks. The exhaustive
property-based test lives at
`platform/tests/property/test_pii_filter.py` (Property 2 — task 3.3).
"""

from __future__ import annotations

from pii_shared import PII_PATTERNS, PiiMatch, mask
from pii_shared.filter import _luhn_valid


# -- TC kimlik no -----------------------------------------------------------


def test_mask_tc_kimlik_basic() -> None:
    masked, matches = mask("TC: 12345678901 lutfen kontrol ediniz.")
    assert "12345678901" not in masked
    assert "***TC_REDACTED***" in masked
    assert any(m.kind == "tc_kimlik" for m in matches)


def test_mask_tc_kimlik_word_boundary() -> None:
    # 12 digits should NOT be redacted as TC (\b\d{11}\b enforces exact 11).
    masked, matches = mask("kod 123456789012 onaylandi")
    assert "123456789012" in masked
    assert all(m.kind != "tc_kimlik" for m in matches)


# -- TR phone ---------------------------------------------------------------


def test_mask_phone_tr_variants() -> None:
    for raw in ("5321234567", "532 123 45 67", "532-123-45-67"):
        text = f"telefonum {raw} arayiniz"
        masked, matches = mask(text)
        assert raw not in masked, f"phone variant leaked: {raw!r}"
        assert any(m.kind == "phone_tr" for m in matches)


def test_mask_phone_tr_non_5_prefix_not_matched() -> None:
    # +90 fixed-line numbers don't start with 5 — must not match.
    masked, _ = mask("ofis 2121234567 numaras\u0131")
    assert "2121234567" in masked


# -- email ------------------------------------------------------------------


def test_mask_email_rfc5322_basic() -> None:
    masked, matches = mask("ali.veli+test@example.co.uk diyor ki")
    assert "ali.veli+test@example.co.uk" not in masked
    assert "***EMAIL_REDACTED***" in masked
    assert any(m.kind == "email" for m in matches)


# -- credit card + Luhn -----------------------------------------------------


def test_luhn_valid_known_test_number() -> None:
    # Visa test number, Luhn-valid.
    assert _luhn_valid("4111111111111111") is True
    # Off-by-one — invalidates the checksum.
    assert _luhn_valid("4111111111111112") is False


def test_mask_credit_card_luhn_valid_redacted() -> None:
    masked, matches = mask("kart 4111 1111 1111 1111 ile odendi")
    assert "4111 1111 1111 1111" not in masked
    assert "***CC_REDACTED***" in masked
    assert any(m.kind == "credit_card" for m in matches)


def test_mask_credit_card_luhn_invalid_left_alone() -> None:
    # 16 digits but failing Luhn — must NOT be redacted, must NOT be reported.
    masked, matches = mask("ref no 1234567890123456 lutfen")
    assert "1234567890123456" in masked
    assert all(m.kind != "credit_card" for m in matches)


# -- determinism ------------------------------------------------------------


def test_mask_is_deterministic() -> None:
    text = (
        "TC 12345678901 tel 5321234567 mail a@b.co kart 4111111111111111"
    )
    a_masked, a_matches = mask(text)
    b_masked, b_matches = mask(text)
    assert a_masked == b_masked
    assert a_matches == b_matches


def test_pii_match_is_frozen_dataclass() -> None:
    m = PiiMatch(kind="email", start=0, end=5)
    try:
        m.start = 1  # type: ignore[misc]
    except (AttributeError, Exception):
        return
    raise AssertionError("PiiMatch must be frozen")


def test_pii_patterns_kinds_are_unique_and_ordered() -> None:
    kinds = [k for k, _, _ in PII_PATTERNS]
    assert kinds == ["tc_kimlik", "phone_tr", "email", "credit_card"]
    assert len(set(kinds)) == len(kinds)
