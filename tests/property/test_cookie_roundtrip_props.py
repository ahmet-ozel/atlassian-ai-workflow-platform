"""invariant for Cookie Sign/Verify Round-Trip.



invariant: Cookie Sign/Verify Round-Trip

For any non-empty department string and any valid secret key,
``verify_cookie(sign_cookie(department, secret), secret)`` SHALL return
the original department string. Conversely, for any signed cookie value
where the signature portion has been modified (tampered),
``verify_cookie`` SHALL return ``None``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings, assume
from hypothesis import strategies as st

# Ensure the cookie_manager module is importable from the Streamlit app source tree.
_STREAMLIT_APP = (
    Path(__file__).resolve().parents[2]
    / "ui"
    / "streamlit-app"
)
if str(_STREAMLIT_APP) not in sys.path:
    sys.path.insert(0, str(_STREAMLIT_APP))

from components.cookie_manager import sign_cookie, verify_cookie


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Non-empty department strings — printable text that could be a department name.
_DEPARTMENT = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "Z"),
        blacklist_characters="\x00",
    ),
    min_size=1,
    max_size=100,
)

# Secret keys — non-empty strings used for HMAC signing.
_SECRET = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "S"),
        blacklist_characters="\x00",
    ),
    min_size=1,
    max_size=64,
)


# ---------------------------------------------------------------------------
# invariant
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(
    department=_DEPARTMENT,
    secret=_SECRET,
)
def test_cookie_sign_verify_roundtrip(department: str, secret: str) -> None:
    """Feature:, invariant: Cookie Sign/Verify Round-Trip



 For any non-empty department string and any valid secret key,
 verify_cookie(sign_cookie(department, secret), secret) returns
 the original department string.
 """
    signed = sign_cookie(department, secret)
    result = verify_cookie(signed, secret)

    assert result == department, (
        f"Round-trip failed: sign_cookie({department!r}, {secret!r}) = {signed!r}, "
        f"but verify_cookie returned {result!r} instead of {department!r}"
    )


@settings(max_examples=100)
@given(
    department=_DEPARTMENT,
    secret=_SECRET,
    tamper_byte=st.integers(min_value=0, max_value=255),
    tamper_pos=st.integers(min_value=0),
)
def test_tampered_signature_returns_none(
    department: str,
    secret: str,
    tamper_byte: int,
    tamper_pos: int,
) -> None:
    """Feature:, invariant: Cookie Sign/Verify Round-Trip (tamper)



 For any signed cookie value where the signature portion has been
 modified (tampered), verify_cookie SHALL return None.
 """
    signed = sign_cookie(department, secret)

    # Split into value and signature parts
    parts = signed.split(".", 1)
    assert len(parts) == 2, "sign_cookie must produce 'value.signature' format"

    value_b64, sig_b64 = parts

    # Tamper with the signature portion
    assume(len(sig_b64) > 0)
    tamper_pos = tamper_pos % len(sig_b64)

    sig_chars = list(sig_b64)
    original_char = sig_chars[tamper_pos]

    # Ensure we actually change the character
    new_char = chr(tamper_byte % 128)  # Keep in ASCII range
    assume(new_char != original_char)
    # Ensure the new char is valid base64url (letters, digits, -, _, =)
    assume(new_char.isalnum() or new_char in "-_=")

    sig_chars[tamper_pos] = new_char
    tampered_sig = "".join(sig_chars)
    tampered_cookie = f"{value_b64}.{tampered_sig}"

    # Tampered cookie must not verify
    result = verify_cookie(tampered_cookie, secret)
    assert result is None, (
        f"Tampered cookie should return None but got {result!r}. "
        f"Original signed: {signed!r}, tampered: {tampered_cookie!r}"
    )


@settings(max_examples=100)
@given(
    department=_DEPARTMENT,
    secret=_SECRET,
    wrong_secret=_SECRET,
)
def test_wrong_secret_returns_none(
    department: str,
    secret: str,
    wrong_secret: str,
) -> None:
    """Feature:, invariant: Cookie Sign/Verify Round-Trip (wrong key)



 A cookie signed with one secret cannot be verified with a different secret.
 """
    assume(secret != wrong_secret)

    signed = sign_cookie(department, secret)
    result = verify_cookie(signed, wrong_secret)

    assert result is None, (
        f"Cookie signed with {secret!r} should not verify with {wrong_secret!r}, "
        f"but got {result!r}"
    )
