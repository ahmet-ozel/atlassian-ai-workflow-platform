"""HMAC-SHA256 webhook signature verification.

Validates incoming webhook payloads from Atlassian (Jira / Bitbucket)
using the ``X-Hub-Signature`` header format: ``sha256=<hex-digest>``.

All comparisons use :func:`hmac.compare_digest` for constant-time
evaluation, preventing timing side-channel attacks.

Requirements: 2.1, 2.2, 3.1, 3.2
"""

from __future__ import annotations

import hashlib
import hmac

_ALGORITHM_PREFIX = "sha256="


def compute(payload: bytes, secret: bytes) -> str:
    """Compute the HMAC-SHA256 signature for *payload* using *secret*.

    Returns the full header value in Atlassian format: ``sha256=<hex>``.
    Useful for generating test fixtures and outgoing webhook signatures.
    """
    digest = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return f"{_ALGORITHM_PREFIX}{digest}"


def verify(payload: bytes, signature_header: str, secret: bytes) -> bool:
    """Verify an Atlassian webhook HMAC-SHA256 signature.

    Parameters
    ----------
    payload:
        The raw request body bytes exactly as received (no re-encoding).
    signature_header:
        The value of the ``X-Hub-Signature`` header, expected in the
        format ``sha256=<hex-digest>``.
    secret:
        The department-specific webhook secret (bytes).

    Returns
    -------
    bool
        ``True`` if the signature is valid; ``False`` otherwise.
        Returns ``False`` for malformed headers (missing prefix, empty
        string, wrong algorithm prefix).
    """
    if not signature_header:
        return False

    if not signature_header.startswith(_ALGORITHM_PREFIX):
        return False

    received_hex = signature_header[len(_ALGORITHM_PREFIX):]
    if not received_hex:
        return False

    expected_hex = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_hex, received_hex)
