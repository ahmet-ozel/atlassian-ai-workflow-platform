"""Unit tests for the egress allowlist matcher (Requirement 10.3).

These cover the canonical happy paths and the negative cases that the
property test (``platform/tests/property/test_firecrawl_egress.py``,
task 12.9) only samples — concrete regression anchors here pin the
matching contract so a regression in either side fails this file
deterministically.
"""

from __future__ import annotations

import pytest

from firecrawl.egress import (
    EGRESS_ALLOWED_AUDIT_ACTION,
    EGRESS_DENIED_AUDIT_ACTION,
    EgressDecision,
    EgressDenied,
    decide_egress,
    is_host_allowed,
    parse_allowlist,
)


# ---------------------------------------------------------------------------
# parse_allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", ()),
        (None, ()),
        ("   ", ()),
        ("example.com", ("example.com",)),
        ("Example.COM", ("example.com",)),
        (" example.com , wikipedia.org ", ("example.com", "wikipedia.org")),
        ("a,b,a,b,c", ("a", "b", "c")),
        (",,example.com,,", ("example.com",)),
    ],
)
def test_parse_allowlist_normalises(raw: str | None, expected: tuple[str, ...]) -> None:
    assert parse_allowlist(raw) == expected


# ---------------------------------------------------------------------------
# is_host_allowed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host, allowlist, expected",
    [
        # Empty allowlist denies everything (closed by default — Y3).
        ("example.com", (), False),
        ("", ("example.com",), False),
        # Exact match.
        ("example.com", ("example.com",), True),
        # Subdomain match honours label boundary.
        ("api.example.com", ("example.com",), True),
        ("a.b.c.example.com", ("example.com",), True),
        # The classic confusable-parent: must NOT match.
        ("barexample.com", ("example.com",), False),
        ("notexample.com", ("example.com",), False),
        # Case folding.
        ("API.Example.COM", ("example.com",), True),
        # Multiple entries.
        ("docs.python.org", ("wikipedia.org", "python.org"), True),
        ("reddit.com", ("wikipedia.org", "python.org"), False),
    ],
)
def test_is_host_allowed(host: str, allowlist: tuple[str, ...], expected: bool) -> None:
    assert is_host_allowed(host, allowlist) is expected


# ---------------------------------------------------------------------------
# decide_egress — verdict, host, audit_action
# ---------------------------------------------------------------------------


def test_decide_egress_allowed_exact() -> None:
    decision = decide_egress("https://example.com/path", ("example.com",))
    assert decision.verdict == "allowed"
    assert decision.host == "example.com"
    assert decision.reason == "allowlisted"
    assert decision.audit_action == EGRESS_ALLOWED_AUDIT_ACTION


def test_decide_egress_allowed_subdomain() -> None:
    decision = decide_egress("https://api.example.com/x", ("example.com",))
    assert decision.verdict == "allowed"
    assert decision.host == "api.example.com"


def test_decide_egress_denied_not_in_allowlist() -> None:
    decision = decide_egress("https://reddit.com/r/x", ("example.com",))
    assert decision.verdict == "denied"
    assert decision.host == "reddit.com"
    assert decision.reason == "not_in_allowlist"
    assert decision.audit_action == EGRESS_DENIED_AUDIT_ACTION


def test_decide_egress_denied_empty_allowlist() -> None:
    decision = decide_egress("https://example.com", ())
    assert decision.verdict == "denied"
    assert decision.reason == "empty_allowlist"
    assert decision.audit_action == EGRESS_DENIED_AUDIT_ACTION


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        "ftp://example.com/x",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://",  # missing host
    ],
)
def test_decide_egress_denies_invalid_or_missing_scheme(url: str) -> None:
    decision = decide_egress(url, ("example.com",))
    assert decision.verdict == "denied"
    assert decision.reason in {"invalid_url", "missing_host"}
    assert decision.audit_action == EGRESS_DENIED_AUDIT_ACTION


def test_decide_egress_confusable_parent_blocked() -> None:
    # The label-boundary check is the security-critical bit. Without it,
    # an attacker could register `barexample.com` and reach the wrapper.
    decision = decide_egress("https://barexample.com/", ("example.com",))
    assert decision.verdict == "denied"
    assert decision.reason == "not_in_allowlist"


def test_egress_denied_exception_carries_decision() -> None:
    decision = decide_egress("https://reddit.com/", ("example.com",))
    exc = EgressDenied(decision)
    assert exc.decision is decision
    assert "egress_denied" in str(exc)
    assert "reddit.com" in str(exc)
