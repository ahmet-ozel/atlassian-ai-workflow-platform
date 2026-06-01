"""Cookie manager for Streamlit department selection persistence.

Provides HMAC-SHA256 signed cookies so that department selection
survives browser sessions without risk of tampering.

Validates Requirements:
    * 10.1 — Cookie dependency for department persistence.
    * 10.2 — Signed cookie write on department selection.
    * 10.4 — 30-day TTL for department cookie.
    * 10.5 — Invalid signature → cookie deleted, user redirected.

The module exposes four public functions:

- ``sign_cookie(value, secret)`` — HMAC-SHA256 signs a value.
- ``verify_cookie(signed_value, secret)`` — verifies and returns
  the original value, or ``None`` on failure.
- ``read_department_cookie()`` — reads and verifies the department
  cookie from the Streamlit cookie store.
- ``write_department_cookie(department)`` — writes a signed
  department cookie with 30-day TTL.

The cookie format is: ``<base64url(value)>.<base64url(signature)>``
where signature = HMAC-SHA256(value_bytes, secret).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from typing import Final

import streamlit as st

__all__ = [
    "sign_cookie",
    "verify_cookie",
    "read_department_cookie",
    "write_department_cookie",
    "COOKIE_NAME",
    "COOKIE_TTL_DAYS",
]

#: Cookie name used for department selection persistence.
COOKIE_NAME: Final[str] = "dept_selection"

#: Cookie time-to-live in days (Requirement 10.4).
COOKIE_TTL_DAYS: Final[int] = 30

#: Environment variable for the cookie signing secret.
_COOKIE_SECRET_ENV: Final[str] = "COOKIE_SECRET"

#: Fallback secret for development only — production MUST set COOKIE_SECRET.
_DEV_FALLBACK_SECRET: Final[str] = "streamlit-dev-cookie-secret-not-for-prod"


def _get_secret() -> str:
    """Retrieve the cookie signing secret from environment.

    Falls back to a dev-only default when COOKIE_SECRET is not set.
    Production deployments MUST set this environment variable.
    """
    return os.environ.get(_COOKIE_SECRET_ENV, _DEV_FALLBACK_SECRET)


def sign_cookie(value: str, secret: str) -> str:
    """Sign a cookie value using HMAC-SHA256.

    Args:
        value: The plaintext value to sign (e.g. department name).
        secret: The HMAC secret key.

    Returns:
        A signed string in the format ``<base64url(value)>.<base64url(hmac)>``.
    """
    value_bytes = value.encode("utf-8")
    signature = hmac.new(
        key=secret.encode("utf-8"),
        msg=value_bytes,
        digestmod=hashlib.sha256,
    ).digest()

    value_b64 = base64.urlsafe_b64encode(value_bytes).decode("ascii")
    sig_b64 = base64.urlsafe_b64encode(signature).decode("ascii")

    return f"{value_b64}.{sig_b64}"


def verify_cookie(signed_value: str, secret: str) -> str | None:
    """Verify a signed cookie and return the original value.

    Args:
        signed_value: The signed cookie string (format: ``value_b64.sig_b64``).
        secret: The HMAC secret key used during signing.

    Returns:
        The original plaintext value if the signature is valid,
        or ``None`` if verification fails (tampered, malformed, etc.).
    """
    if not signed_value or "." not in signed_value:
        return None

    parts = signed_value.split(".", 1)
    if len(parts) != 2:
        return None

    value_b64, sig_b64 = parts

    try:
        value_bytes = base64.urlsafe_b64decode(value_b64)
        provided_sig = base64.urlsafe_b64decode(sig_b64)
    except (ValueError, Exception):
        return None

    expected_sig = hmac.new(
        key=secret.encode("utf-8"),
        msg=value_bytes,
        digestmod=hashlib.sha256,
    ).digest()

    if not hmac.compare_digest(provided_sig, expected_sig):
        return None

    try:
        return value_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_department_cookie() -> str | None:
    """Read and verify the department cookie from the Streamlit cookie store.

    Uses the cookie reader installed on ``st.session_state["_cookie_reader"]``
    by the app boot sequence. If no reader is available or the cookie
    is missing/invalid, returns ``None``.

    Returns:
        The verified department string, or ``None`` if the cookie is
        absent, expired, or has an invalid signature.
    """
    reader = st.session_state.get("_cookie_reader")
    if reader is None:
        return None

    try:
        raw_value = reader(COOKIE_NAME)
    except Exception:  # noqa: BLE001
        return None

    if not raw_value:
        return None

    secret = _get_secret()
    return verify_cookie(raw_value, secret)


def write_department_cookie(department: str) -> None:
    """Write the department selection as a signed cookie.

    Signs the department value with HMAC-SHA256 and writes it to the
    browser via the cookie writer installed on session state.

    Args:
        department: The department identifier to persist.
    """
    writer = st.session_state.get("_cookie_writer")
    if writer is None:
        return

    secret = _get_secret()
    signed_value = sign_cookie(department, secret)

    try:
        writer(COOKIE_NAME, signed_value, ttl_days=COOKIE_TTL_DAYS)
    except Exception:  # noqa: BLE001 — non-fatal; cookie write is best-effort
        pass
