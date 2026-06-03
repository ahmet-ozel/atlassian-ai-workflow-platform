"""Deterministic PII regex masker.

Implements the `mask(text)` pure function consumed by
`assistant-service` chat handler before any user-provided text is
forwarded to an LLM, audit log or tool dispatcher.

Design contract:

* No I/O, no logging, no global mutable state.
* `mask(text)` is referentially transparent — same input always
  yields the same `(masked, matches)` tuple.
* `PII_PATTERNS` ordering is stable; iteration matches the canonical
  ordering documented in the README.
* Credit-card candidates that fail the Luhn checksum are *not*
  redacted and are *not* reported in `matches`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal

__all__ = [
    "PII_PATTERNS",
    "PiiKind",
    "PiiMatch",
    "mask",
]


PiiKind = Literal["tc_kimlik", "phone_tr", "email", "credit_card"]


@dataclass(frozen=True, slots=True)
class PiiMatch:
    """A single PII occurrence detected in the *original* input text.

    `start` and `end` are byte/codepoint offsets into the **original**
    input string passed to `mask(...)`, not into the masked output.
    This matches the PiiMatch dataclass contract.

    Attributes:
        kind:  Which pattern matched (one of `PiiKind`).
        start: Inclusive offset in the original input.
        end:   Exclusive offset in the original input.
    """

    kind: PiiKind
    start: int
    end: int


# (kind, compiled regex, replacement string)
#
# The tuple is ordered intentionally:
#   1. tc_kimlik (11 digit run) is checked first so that a longer
#      credit-card-like digit run does not accidentally swallow a
#      shorter TC-id substring before TC redaction is applied.
#   2. phone_tr is more specific than the bare digit run and uses the
#      Turkish mobile prefix `5XX` to avoid masking arbitrary numbers.
#   3. email is well-bounded by the `@` literal.
#   4. credit_card is last and is Luhn-validated in `mask()` itself —
#      invalid candidates are left untouched.
PII_PATTERNS: Final[tuple[tuple[PiiKind, re.Pattern[str], str], ...]] = (
    (
        "tc_kimlik",
        re.compile(r"\b\d{11}\b"),
        "***TC_REDACTED***",
    ),
    (
        "phone_tr",
        re.compile(r"\b5\d{2}[ -]?\d{3}[ -]?\d{2}[ -]?\d{2}\b"),
        "***PHONE_REDACTED***",
    ),
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "***EMAIL_REDACTED***",
    ),
    (
        "credit_card",
        re.compile(r"\b(?:\d[ -]?){13,19}\b"),
        "***CC_REDACTED***",
    ),
)


def _luhn_valid(candidate: str) -> bool:
    """Return True iff the digits in `candidate` pass the Luhn checksum.

    Non-digit characters (spaces, dashes) in the candidate are ignored,
    matching the credit-card regex which permits a single space or dash
    between digits.

    Args:
        candidate: The raw substring matched by the credit-card regex.

    Returns:
        True when 13 ≤ digit-count ≤ 19 and the digits satisfy Luhn,
        False otherwise.
    """
    digits = [int(ch) for ch in candidate if ch.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    # Luhn: from rightmost digit, double every second digit, sum digits.
    parity = len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def mask(text: str) -> tuple[str, list[PiiMatch]]:
    """Mask PII patterns in `text`.

    The function:

    1. Iterates over `PII_PATTERNS` in declaration order.
    2. For each pattern, scans the **current** masked buffer and
       collects `PiiMatch` records using the offsets *in the original
       input string* (so downstream consumers can correlate matches
       back to the user-supplied text).
    3. For credit-card candidates, only Luhn-valid runs are reported
       and substituted; invalid runs are left as-is.
    4. Substitutes every reported occurrence with the pattern's
       replacement token.

    The function is deterministic: invoking `mask(text)` twice on the
    same input yields identical output (string and matches list).

    Args:
        text: The original user-provided text, possibly containing PII.

    Returns:
        A tuple `(masked, matches)` where `masked` is the redacted
        string and `matches` is the list of detected `PiiMatch`
        records, in detection order (by pattern, then by position).
    """
    matches: list[PiiMatch] = []
    masked = text

    for kind, pattern, replacement in PII_PATTERNS:
        # Match against the ORIGINAL `text` so the offsets in
        # `PiiMatch` refer to the user's input, not the partially
        # masked buffer (which has different lengths after substitution).
        if kind == "credit_card":
            valid_spans: list[tuple[int, int]] = []
            for m in pattern.finditer(text):
                if not _luhn_valid(m.group()):
                    continue
                valid_spans.append((m.start(), m.end()))
                matches.append(PiiMatch(kind, m.start(), m.end()))
            if valid_spans:
                masked = _replace_spans(masked, text, valid_spans, replacement)
        else:
            spans: list[tuple[int, int]] = []
            for m in pattern.finditer(text):
                spans.append((m.start(), m.end()))
                matches.append(PiiMatch(kind, m.start(), m.end()))
            if spans:
                masked = _replace_spans(masked, text, spans, replacement)

    return masked, matches


def _replace_spans(
    masked: str,
    original: str,
    spans: list[tuple[int, int]],
    replacement: str,
) -> str:
    """Apply `replacement` to every `(start, end)` span of `original` in `masked`.

    Spans are computed against `original`. For each span, this looks up
    the original substring `original[start:end]` and replaces *all*
    occurrences of that substring inside the current `masked` buffer.

    Using substring replacement (rather than offset rewriting) keeps the
    function safe under earlier passes that have already shifted offsets
    due to substitutions of differing length.
    """
    # Deduplicate substrings so the same literal isn't replaced twice.
    seen: set[str] = set()
    for start, end in spans:
        chunk = original[start:end]
        if chunk in seen:
            continue
        seen.add(chunk)
        masked = masked.replace(chunk, replacement)
    return masked
