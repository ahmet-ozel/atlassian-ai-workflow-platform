"""firecrawl — egress-allowlisted web scrape/search wrapper.

The package implements the wrapper service that fronts Firecrawl with a
deterministic per-host egress allowlist (Requirement 10.3, MIMARI §16.14 Y3).
The egress matcher (:mod:`firecrawl.egress`) is the load-bearing piece and is
exercised by both the unit tests under ``tests/unit/`` and the platform
property test ``platform/tests/property/test_firecrawl_egress.py``.
"""

from firecrawl.egress import (
    EgressDecision,
    EgressDenied,
    EgressVerdict,
    parse_allowlist,
    is_host_allowed,
    decide_egress,
)

__all__ = [
    "EgressDecision",
    "EgressDenied",
    "EgressVerdict",
    "parse_allowlist",
    "is_host_allowed",
    "decide_egress",
]
