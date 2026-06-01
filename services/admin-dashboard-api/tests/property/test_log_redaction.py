"""Property 13 — log redaction matches every documented LLM key pattern.

# Feature: llm-provider-management, Property 13: Log redaction matches every documented LLM key pattern across CRUD, test, and exception paths

Validates Requirements 13.1, 13.2, 13.3, 13.4, 13.5 of the
``llm-provider-management`` spec: the six regex patterns added to
:data:`http_shared.redaction.REDACTION_PATTERNS` cover every LLM
credential shape this feature supports (Anthropic ``sk-ant-``,
OpenAI ``sk-proj/live/test-``, OpenAI generic ``sk-``, Google
Gemini ``AIza...``).
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings, strategies as st


_API_ROOT = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _API_ROOT.parents[1]
for _path in (_API_ROOT, _PLATFORM_ROOT / "libs" / "http-shared" / "src"):
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from http_shared.redaction import REDACTION_PLACEHOLDER, redact_text  # noqa: E402


#: Suffix charset for ``sk-`` / ``AIza`` keys — strict ASCII alphanumeric
#: plus ``_-``. The redaction regexes are ASCII-only (``[A-Za-z0-9_\-]``)
#: so the strategy stays inside that alphabet; Unicode "Nd"/"Ll" code
#: points produce characters the regex does not match, which is fine in
#: production but useless for *this* property (Property 13 only pins the
#: documented patterns, not Unicode-extension behaviour).
#: 30+ chars covers every documented prefix's minimum length
#: (sk- patterns need ≥ 20, AIza needs ≥ 30 — 30 satisfies both).
#: Restricted to alphanumeric so the bare ``sk-[A-Za-z0-9]{20,}``
#: regex (which does NOT allow ``_-`` in the body) still matches.
_KEY_CHARSET = st.text(
    alphabet=st.sampled_from(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    ),
    min_size=30,
    max_size=64,
)

_KEY_PREFIXES = (
    "sk-ant-",
    "sk-proj-",
    "sk-live-",
    "sk-test-",
    "sk-",
    "AIza",
)


@given(prefix=st.sampled_from(_KEY_PREFIXES), suffix=_KEY_CHARSET)
@settings(max_examples=200, deadline=None)
def test_known_prefix_keys_are_redacted(prefix: str, suffix: str) -> None:
    """A generated key with any documented prefix is redacted in full."""

    key = prefix + suffix
    text = f"some log line carrying {key} for context"
    redacted = redact_text(text)
    # The key itself must NOT survive verbatim.
    assert key not in redacted
    # The placeholder must have replaced the key.
    assert REDACTION_PLACEHOLDER in redacted


@given(
    prefix=st.sampled_from(_KEY_PREFIXES),
    suffix=_KEY_CHARSET,
    noise=st.text(max_size=50),
)
@settings(max_examples=200, deadline=None)
def test_redaction_is_idempotent(prefix: str, suffix: str, noise: str) -> None:
    """Re-running ``redact_text`` is a no-op once the secret is masked."""

    text = f"{noise} {prefix}{suffix} {noise}"
    once = redact_text(text)
    twice = redact_text(once)
    assert once == twice


def test_safe_text_is_unchanged() -> None:
    """Text that does not match any pattern survives verbatim."""

    sample = "this is plain text with no secrets to redact"
    assert redact_text(sample) == sample
