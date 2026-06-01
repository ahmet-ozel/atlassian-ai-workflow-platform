"""Turkish natural language PR comment intent classifier (Feature 15).

Pure regex-based function that classifies PR comment text into one of
three intents: ``"fix"``, ``"explain"``, or ``"review"``. Supports both
Turkish and English keywords.

Intent mapping:
- (düzelt|fix|tamir et) → "fix"
- (açıkla|explain|anlat) → "explain"
- (incele|review|gözden geçir) → "review"

If no intent is matched, returns ``None``.

Audit event: ``pr_comment_intent_normalized`` (emitted by the caller
after classification).
"""

from __future__ import annotations

import re
from typing import Literal

__all__ = ["classify_pr_comment_intent", "PRCommentIntent"]

#: The three recognized PR comment intents.
PRCommentIntent = Literal["fix", "explain", "review"]

# ---------------------------------------------------------------------------
# Intent patterns (Turkish + English)
# ---------------------------------------------------------------------------

#: Pattern for "fix" intent — matches Turkish "düzelt", "tamir et" and
#: English "fix". Case-insensitive, word-boundary aware.
_FIX_PATTERN = re.compile(
    r"\b(düzelt|fix|tamir\s*et)\b",
    re.IGNORECASE | re.UNICODE,
)

#: Pattern for "explain" intent — matches Turkish "açıkla", "anlat" and
#: English "explain". Case-insensitive, word-boundary aware.
_EXPLAIN_PATTERN = re.compile(
    r"\b(açıkla|explain|anlat)\b",
    re.IGNORECASE | re.UNICODE,
)

#: Pattern for "review" intent — matches Turkish "incele", "gözden geçir"
#: and English "review". Case-insensitive, word-boundary aware.
_REVIEW_PATTERN = re.compile(
    r"\b(incele|review|gözden\s*geçir)\b",
    re.IGNORECASE | re.UNICODE,
)

#: Ordered list of (pattern, intent) pairs. First match wins.
_INTENT_PATTERNS: list[tuple[re.Pattern[str], PRCommentIntent]] = [
    (_FIX_PATTERN, "fix"),
    (_EXPLAIN_PATTERN, "explain"),
    (_REVIEW_PATTERN, "review"),
]


def classify_pr_comment_intent(comment_text: str) -> PRCommentIntent | None:
    """Classify a PR comment into an intent using regex matching.

    Args:
        comment_text: The raw PR comment text to classify.

    Returns:
        One of ``"fix"``, ``"explain"``, ``"review"`` if a known intent
        pattern is found, or ``None`` if no intent could be determined.

    Examples:
        >>> classify_pr_comment_intent("Bu kodu düzelt")
        'fix'
        >>> classify_pr_comment_intent("Please fix this bug")
        'fix'
        >>> classify_pr_comment_intent("Bunu açıkla lütfen")
        'explain'
        >>> classify_pr_comment_intent("Can you explain this?")
        'explain'
        >>> classify_pr_comment_intent("Şu kısmı incele")
        'review'
        >>> classify_pr_comment_intent("Gözden geçir bu PR'ı")
        'review'
        >>> classify_pr_comment_intent("Merhaba dünya")
        None
    """
    if not comment_text or not isinstance(comment_text, str):
        return None

    for pattern, intent in _INTENT_PATTERNS:
        if pattern.search(comment_text):
            return intent

    return None
