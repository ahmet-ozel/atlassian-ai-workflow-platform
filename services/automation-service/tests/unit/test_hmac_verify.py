"""Unit tests for decision.hmac_verify module.

Validates HMAC-SHA256 sign/verify round-trip, tamper rejection,
and Atlassian header format parsing.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the automation-service src is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from decision.hmac_verify import compute, verify


class TestCompute:
    """Tests for the compute() helper."""

    def test_returns_sha256_prefixed_string(self) -> None:
        result = compute(b"hello", b"secret")
        assert result.startswith("sha256=")

    def test_deterministic(self) -> None:
        sig1 = compute(b"payload", b"key")
        sig2 = compute(b"payload", b"key")
        assert sig1 == sig2

    def test_different_payloads_produce_different_signatures(self) -> None:
        sig1 = compute(b"payload1", b"key")
        sig2 = compute(b"payload2", b"key")
        assert sig1 != sig2

    def test_different_secrets_produce_different_signatures(self) -> None:
        sig1 = compute(b"payload", b"key1")
        sig2 = compute(b"payload", b"key2")
        assert sig1 != sig2


class TestVerify:
    """Tests for the verify() function."""

    def test_valid_signature_returns_true(self) -> None:
        payload = b'{"event": "jira:issue_created"}'
        secret = b"webhook-secret-123"
        sig = compute(payload, secret)
        assert verify(payload, sig, secret) is True

    def test_tampered_payload_returns_false(self) -> None:
        payload = b'{"event": "jira:issue_created"}'
        secret = b"webhook-secret-123"
        sig = compute(payload, secret)
        tampered = b'{"event": "jira:issue_created", "extra": true}'
        assert verify(tampered, sig, secret) is False

    def test_wrong_secret_returns_false(self) -> None:
        payload = b'{"event": "jira:issue_created"}'
        secret = b"correct-secret"
        sig = compute(payload, secret)
        assert verify(payload, sig, b"wrong-secret") is False

    def test_tampered_signature_returns_false(self) -> None:
        payload = b'{"event": "jira:issue_created"}'
        secret = b"webhook-secret-123"
        sig = compute(payload, secret)
        # Flip a character in the hex digest
        tampered_sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        assert verify(payload, tampered_sig, secret) is False

    def test_empty_signature_header_returns_false(self) -> None:
        assert verify(b"payload", "", b"secret") is False

    def test_missing_sha256_prefix_returns_false(self) -> None:
        payload = b"payload"
        secret = b"secret"
        sig = compute(payload, secret)
        # Strip the prefix
        hex_only = sig[len("sha256="):]
        assert verify(payload, hex_only, secret) is False

    def test_wrong_algorithm_prefix_returns_false(self) -> None:
        payload = b"payload"
        secret = b"secret"
        sig = compute(payload, secret)
        wrong_prefix = "sha1=" + sig[len("sha256="):]
        assert verify(payload, wrong_prefix, secret) is False

    def test_sha256_prefix_only_no_digest_returns_false(self) -> None:
        assert verify(b"payload", "sha256=", b"secret") is False

    def test_empty_payload_valid_signature(self) -> None:
        payload = b""
        secret = b"secret"
        sig = compute(payload, secret)
        assert verify(payload, sig, secret) is True

    def test_large_payload(self) -> None:
        payload = b"x" * 65536
        secret = b"secret"
        sig = compute(payload, secret)
        assert verify(payload, sig, secret) is True
