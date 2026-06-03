"""Unit tests for cookie_manager module.

Tests sign/verify with known values, TTL expiry behavior,
and invalid signature handling.

"""

from __future__ import annotations

import base64
import hashlib
import hmac
import sys
from unittest.mock import MagicMock

import pytest

# Mock streamlit before importing cookie_manager since it's not available
# in the test environment.
sys.modules["streamlit"] = MagicMock()

from components.cookie_manager import (  # noqa: E402
    COOKIE_NAME,
    COOKIE_TTL_DAYS,
    sign_cookie,
    verify_cookie,
)


class TestSignCookie:
    """Tests for sign_cookie function."""

    def test_sign_produces_dot_separated_format(self):
        """Signed cookie has format: <base64url(value)>.<base64url(hmac)>."""
        result = sign_cookie("engineering", "my-secret")
        parts = result.split(".")
        assert len(parts) == 2
        # Both parts should be valid base64url
        base64.urlsafe_b64decode(parts[0])
        base64.urlsafe_b64decode(parts[1])

    def test_sign_with_known_values(self):
        """Verify sign_cookie produces expected output for known input."""
        value = "engineering"
        secret = "test-secret-key"

        result = sign_cookie(value, secret)

        # Manually compute expected output
        value_bytes = value.encode("utf-8")
        expected_sig = hmac.new(
            key=secret.encode("utf-8"),
            msg=value_bytes,
            digestmod=hashlib.sha256,
        ).digest()

        expected_value_b64 = base64.urlsafe_b64encode(value_bytes).decode("ascii")
        expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode("ascii")
        expected = f"{expected_value_b64}.{expected_sig_b64}"

        assert result == expected

    def test_sign_different_secrets_produce_different_signatures(self):
        """Different secrets must produce different signed cookies."""
        value = "marketing"
        result1 = sign_cookie(value, "secret-one")
        result2 = sign_cookie(value, "secret-two")

        # Value part should be the same, signature part should differ
        val1, sig1 = result1.split(".")
        val2, sig2 = result2.split(".")
        assert val1 == val2
        assert sig1 != sig2

    def test_sign_different_values_produce_different_outputs(self):
        """Different values must produce different signed cookies."""
        secret = "shared-secret"
        result1 = sign_cookie("engineering", secret)
        result2 = sign_cookie("marketing", secret)
        assert result1 != result2

    def test_sign_unicode_value(self):
        """sign_cookie handles unicode department names."""
        result = sign_cookie("mühendislik", "secret")
        assert "." in result
        # Should be verifiable
        assert verify_cookie(result, "secret") == "mühendislik"


class TestVerifyCookie:
    """Tests for verify_cookie function."""

    def test_verify_valid_cookie(self):
        """verify_cookie returns original value for valid signed cookie."""
        secret = "my-secret"
        department = "engineering"
        signed = sign_cookie(department, secret)

        result = verify_cookie(signed, secret)
        assert result == department

    def test_verify_returns_none_for_wrong_secret(self):
        """verify_cookie returns None when secret doesn't match — Req 10.5."""
        signed = sign_cookie("engineering", "correct-secret")
        result = verify_cookie(signed, "wrong-secret")
        assert result is None

    def test_verify_returns_none_for_tampered_value(self):
        """verify_cookie returns None when value portion is tampered — Req 10.5."""
        signed = sign_cookie("engineering", "secret")
        value_b64, sig_b64 = signed.split(".")

        # Tamper with the value portion
        tampered_value = base64.urlsafe_b64encode(b"hacked").decode("ascii")
        tampered_cookie = f"{tampered_value}.{sig_b64}"

        result = verify_cookie(tampered_cookie, "secret")
        assert result is None

    def test_verify_returns_none_for_tampered_signature(self):
        """verify_cookie returns None when signature is tampered — Req 10.5."""
        signed = sign_cookie("engineering", "secret")
        value_b64, sig_b64 = signed.split(".")

        # Tamper with the signature (flip a character)
        tampered_sig = base64.urlsafe_b64encode(b"fake-signature").decode("ascii")
        tampered_cookie = f"{value_b64}.{tampered_sig}"

        result = verify_cookie(tampered_cookie, "secret")
        assert result is None

    def test_verify_returns_none_for_empty_string(self):
        """verify_cookie returns None for empty input."""
        assert verify_cookie("", "secret") is None

    def test_verify_returns_none_for_no_dot(self):
        """verify_cookie returns None when no dot separator present."""
        assert verify_cookie("nodothere", "secret") is None

    def test_verify_returns_none_for_invalid_base64(self):
        """verify_cookie returns None for malformed base64 content."""
        assert verify_cookie("not!valid!b64.also!invalid", "secret") is None

    def test_verify_returns_none_for_none_input(self):
        """verify_cookie handles None-like falsy input gracefully."""
        assert verify_cookie("", "secret") is None

    def test_roundtrip_various_departments(self):
        """sign then verify returns original value for various inputs."""
        secret = "roundtrip-secret"
        departments = ["engineering", "marketing", "sales", "hr", "devops"]

        for dept in departments:
            signed = sign_cookie(dept, secret)
            assert verify_cookie(signed, secret) == dept


class TestCookieConstants:
    """Tests for cookie configuration constants."""

    def test_cookie_name(self):
        """Cookie name should be 'dept_selection'."""
        assert COOKIE_NAME == "dept_selection"

    def test_cookie_ttl_is_30_days(self):
        """Cookie TTL should be 30 days."""
        assert COOKIE_TTL_DAYS == 30


class TestReadWriteDepartmentCookie:
    """Tests for read/write department cookie with mocked streamlit."""

    def test_write_department_cookie_calls_writer_with_ttl(self):
        """write_department_cookie passes 30-day TTL to cookie writer — Req 10.4."""
        import streamlit as st

        mock_writer = MagicMock()
        st.session_state = {"_cookie_writer": mock_writer}

        from components.cookie_manager import write_department_cookie

        write_department_cookie("engineering")

        mock_writer.assert_called_once()
        call_args = mock_writer.call_args
        # Positional args: cookie_name, signed_value
        assert call_args[0][0] == COOKIE_NAME
        # Keyword arg: ttl_days=30
        assert call_args[1]["ttl_days"] == COOKIE_TTL_DAYS

    def test_write_department_cookie_no_writer_does_nothing(self):
        """write_department_cookie is no-op when no writer in session state."""
        import streamlit as st

        st.session_state = {}

        from components.cookie_manager import write_department_cookie

        # Should not raise
        write_department_cookie("engineering")

    def test_read_department_cookie_returns_verified_value(self):
        """read_department_cookie verifies signature before returning."""
        import os

        import streamlit as st

        secret = "test-cookie-secret"
        os.environ["COOKIE_SECRET"] = secret

        signed = sign_cookie("engineering", secret)
        mock_reader = MagicMock(return_value=signed)
        st.session_state = {"_cookie_reader": mock_reader}

        from components.cookie_manager import read_department_cookie

        result = read_department_cookie()
        assert result == "engineering"

        # Cleanup
        del os.environ["COOKIE_SECRET"]

    def test_read_department_cookie_returns_none_for_invalid_signature(self):
        """read_department_cookie returns None for tampered cookie — Req 10.5."""
        import os

        import streamlit as st

        secret = "test-cookie-secret"
        os.environ["COOKIE_SECRET"] = secret

        # Provide a cookie signed with a different secret
        tampered = sign_cookie("engineering", "wrong-secret")
        mock_reader = MagicMock(return_value=tampered)
        st.session_state = {"_cookie_reader": mock_reader}

        from components.cookie_manager import read_department_cookie

        result = read_department_cookie()
        assert result is None

        # Cleanup
        del os.environ["COOKIE_SECRET"]

    def test_read_department_cookie_returns_none_when_no_reader(self):
        """read_department_cookie returns None when no reader available."""
        import streamlit as st

        st.session_state = {}

        from components.cookie_manager import read_department_cookie

        result = read_department_cookie()
        assert result is None
