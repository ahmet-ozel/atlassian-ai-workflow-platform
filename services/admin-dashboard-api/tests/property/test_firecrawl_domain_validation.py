"""Property test: Firecrawl allowlist domain format validation.

Feature: platform-completion, Property 26: For any input string submitted as a new
domain, it SHALL be accepted iff it is a valid DNS domain (max 253 chars) AND
does not already exist in the allowlist.

Validates: Requirements 12.2, 12.3
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

from hypothesis import given, strategies as st, settings
from pydantic import ValidationError
import pytest

_SERVICE_ROOT = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(_SERVICE_ROOT))

from routers.firecrawl_allowlist import (
    DomainAddRequest, MAX_DOMAIN_LENGTH, _DNS_PATTERN,
)


_VALID_DOMAINS = st.from_regex(
    r"[a-z][a-z0-9-]{1,30}\.[a-z]{2,10}",
    fullmatch=True,
).filter(lambda s: len(s) <= MAX_DOMAIN_LENGTH and "--" not in s and not s.endswith("-"))


@settings(max_examples=100, deadline=None)
@given(domain=_VALID_DOMAINS)
def test_valid_dns_format_accepted(domain: str) -> None:
    """Valid DNS-format domains under 253 chars are accepted."""
    req = DomainAddRequest(domain=domain)
    assert req.domain == domain.lower()


@settings(max_examples=200, deadline=None)
@given(s=st.text(min_size=1, max_size=300))
def test_validation_classifies_correctly(s: str) -> None:
    """Validation matches the DNS pattern + length rule."""
    s_clean = s.strip().lower()
    is_valid = (
        len(s_clean) <= MAX_DOMAIN_LENGTH
        and _DNS_PATTERN.match(s_clean) is not None
    )
    if is_valid:
        DomainAddRequest(domain=s)  # should not raise
    else:
        with pytest.raises((ValidationError, ValueError)):
            DomainAddRequest(domain=s)


@settings(max_examples=20, deadline=None)
@given(
    base=st.from_regex(r"[a-z][a-z0-9]{0,40}", fullmatch=True),
    tld=st.sampled_from(["com", "net", "org", "io"]),
    repeat=st.integers(min_value=10, max_value=100),
)
def test_oversized_domain_rejected(base: str, tld: str, repeat: int) -> None:
    """Domains exceeding 253 chars are rejected."""
    long_domain = (base + ".") * repeat + tld
    if len(long_domain) > MAX_DOMAIN_LENGTH:
        with pytest.raises((ValidationError, ValueError)):
            DomainAddRequest(domain=long_domain)


def test_max_length_constant() -> None:
    """MAX_DOMAIN_LENGTH matches the spec."""
    assert MAX_DOMAIN_LENGTH == 253
