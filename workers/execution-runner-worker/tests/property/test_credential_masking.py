"""Credential masking in logs.

For any credential value retrieved from Vault, it never appears as plain text
in any log output produced by the Credential_Injector — only masked as "***".

This property test uses Hypothesis to generate random credential strings and
verifies that after adding a credential to the :class:`CredentialMaskingFilter`
and filtering a log record containing that credential, the credential does NOT
appear in plain text in the output.

The function :func:`mask_credential_value` is also tested to ensure it never
returns the original credential value for any non-trivial input.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings, assume
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Make ``src`` importable without first installing the worker package.
# ---------------------------------------------------------------------------

_WORKER_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_DIR = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from activities.credential_injector import (  # noqa: E402
    CREDENTIAL_MASK,
    CredentialMaskingFilter,
    mask_credential_value,
)


# ---------------------------------------------------------------------------
# Credential masking in logs
# ---------------------------------------------------------------------------


class TestCredentialMaskingProperty:
    """Generated-input tests for credential masking in log output.

    The Credential_Injector must not show credential values as plain text in
    log output; credential values should appear masked as "***".
    """

    @given(credential=st.text(min_size=1, max_size=100))
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_credential_never_appears_in_filtered_log_output(
        self, credential: str
    ) -> None:
        """For any credential string added to the masking filter, the
        credential does not appear in plain text in the filtered log record.
        """
        # Set up the masking filter with the credential
        masking_filter = CredentialMaskingFilter()
        masking_filter.add_sensitive(credential)

        # Create a log record that contains the credential
        record = logging.LogRecord(
            name="test.credential_injector",
            level=logging.INFO,
            pathname="credential_injector.py",
            lineno=1,
            msg=f"Connecting with credential: {credential}",
            args=None,
            exc_info=None,
        )

        # Apply the filter
        result = masking_filter.filter(record)

        # The filter should always return True (allow the record through)
        assert result is True

        # The credential must NOT appear in the filtered message
        filtered_message = record.getMessage()
        assert credential not in filtered_message, (
            f"Credential '{credential}' leaked into log output: "
            f"'{filtered_message}'"
        )

    @given(credential=st.text(min_size=1, max_size=100))
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_credential_replaced_with_mask_in_log_output(
        self, credential: str
    ) -> None:
        """For any credential string in a log message, after filtering,
        the mask placeholder is present where the credential was.
        """
        masking_filter = CredentialMaskingFilter()
        masking_filter.add_sensitive(credential)

        # Build a message that definitely contains the credential
        original_msg = f"secret={credential}"
        record = logging.LogRecord(
            name="test.credential_injector",
            level=logging.INFO,
            pathname="credential_injector.py",
            lineno=1,
            msg=original_msg,
            args=None,
            exc_info=None,
        )

        masking_filter.filter(record)
        filtered_message = record.getMessage()

        # The mask must be present in the output
        assert CREDENTIAL_MASK in filtered_message, (
            f"Expected mask '{CREDENTIAL_MASK}' in filtered output "
            f"but got: '{filtered_message}'"
        )

    @given(credential=st.text(min_size=1, max_size=100))
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_credential_masked_even_with_multiple_occurrences(
        self, credential: str
    ) -> None:
        """If a credential appears multiple times, all occurrences are masked."""
        masking_filter = CredentialMaskingFilter()
        masking_filter.add_sensitive(credential)

        # Message with credential appearing multiple times
        record = logging.LogRecord(
            name="test.credential_injector",
            level=logging.INFO,
            pathname="credential_injector.py",
            lineno=1,
            msg=f"user={credential} pass={credential} token={credential}",
            args=None,
            exc_info=None,
        )

        masking_filter.filter(record)
        filtered_message = record.getMessage()

        assert credential not in filtered_message, (
            f"Credential '{credential}' still present after masking "
            f"multiple occurrences: '{filtered_message}'"
        )

    @given(credential=st.text(min_size=3, max_size=100))
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_mask_credential_value_never_returns_original(
        self, credential: str
    ) -> None:
        """The mask_credential_value function never returns the
        original credential value for strings of length >= 3.
        """
        masked = mask_credential_value(credential)

        assert masked != credential, (
            f"mask_credential_value returned the original value: "
            f"'{credential}'"
        )

    @given(credential=st.text(min_size=1, max_size=100))
    @settings(
        max_examples=200,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    def test_mask_credential_value_contains_mask_placeholder(
        self, credential: str
    ) -> None:
        """The mask_credential_value function always includes the
        CREDENTIAL_MASK placeholder in its output.
        """
        masked = mask_credential_value(credential)

        assert CREDENTIAL_MASK in masked, (
            f"mask_credential_value output '{masked}' does not contain "
            f"the mask placeholder '{CREDENTIAL_MASK}'"
        )
