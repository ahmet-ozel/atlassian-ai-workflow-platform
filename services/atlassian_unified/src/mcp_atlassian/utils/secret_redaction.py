"""Secret redaction utilities for MCP Atlassian.

This module provides the default set of secret-like key names used to
redact sensitive values from tool responses before they are returned to
MCP clients, along with a recursive walker that applies the redaction
over nested data structures (Requirement 44.1/44.2/44.5).

Keys are matched **case-insensitively on the final key name only**.
Substring matches are explicitly not performed — for example,
``description`` and ``tokenize`` are left untouched, while ``Secret``
and ``CLIENT_SECRET`` are redacted when their canonical lowercase form
appears in the configured key set.
"""

from __future__ import annotations

from typing import Any

# Default set of secret-like key names that should be redacted from tool
# responses. The membership here matches the keys enumerated in the design
# document (utils/secret_redaction.py section) and Requirement 44.2.
DEFAULT_SECRET_KEYS: frozenset[str] = frozenset(
    {
        "secret",
        "token",
        "password",
        "apiKey",
        "api_key",
        "privateKey",
        "private_key",
        "clientSecret",
        "client_secret",
    }
)

# Literal replacement string used for every redacted value.
REDACTED_PLACEHOLDER: str = "[REDACTED]"


def redact_secrets(
    obj: Any,
    *,
    keys: frozenset[str] = DEFAULT_SECRET_KEYS,
) -> Any:
    """Recursively redact secret-like values in ``obj``.

    The walker descends through ``dict``, ``list`` and ``tuple`` nodes and
    returns a new structure with any value whose key matches ``keys``
    replaced by :data:`REDACTED_PLACEHOLDER`. Matching is performed
    case-insensitively on the final key name only — substring matches are
    deliberately not performed, so keys such as ``description`` or
    ``tokenize`` are left untouched.

    Non-container values (and keys that are not strings) are returned as
    is. The input ``obj`` itself is never mutated.

    Args:
        obj: The value to walk. May be a primitive, ``dict``, ``list``,
            ``tuple``, or any other object.
        keys: Secret-like key names to redact. Defaults to
            :data:`DEFAULT_SECRET_KEYS`.

    Returns:
        A new structure of the same container shape as ``obj`` with
        matching leaf values replaced by ``"[REDACTED]"``.
    """
    # Precompute the lowercased comparison set once per top-level call so
    # deep structures don't pay the cost on every recursion.
    lowered_keys = {k.lower() for k in keys}
    return _redact(obj, lowered_keys)


def _redact(obj: Any, lowered_keys: set[str]) -> Any:
    """Internal recursion helper that reuses a prepared key set."""
    if isinstance(obj, dict):
        result: dict[Any, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in lowered_keys:
                result[key] = REDACTED_PLACEHOLDER
            else:
                result[key] = _redact(value, lowered_keys)
        return result
    if isinstance(obj, list):
        return [_redact(item, lowered_keys) for item in obj]
    if isinstance(obj, tuple):
        return tuple(_redact(item, lowered_keys) for item in obj)
    return obj
