"""pii-shared: deterministic PII regex masker.

Re-exports the public API of the package so callers can simply do::

    from pii_shared import mask, PiiMatch, PII_PATTERNS

Provides shared PII masking helpers.
"""

from .filter import (
    PII_PATTERNS,
    PiiKind,
    PiiMatch,
    mask,
)

__all__ = [
    "PII_PATTERNS",
    "PiiKind",
    "PiiMatch",
    "mask",
]
