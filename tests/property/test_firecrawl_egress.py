"""Firecrawl egress allowlist behavior.



Firecrawl egress allowlist



Universal property
------------------

For every ``(target_host, allowlist)`` pair the firecrawl wrapper inspects:.. code-block:: text

 ∀ host, allowlist:
 host ∈ allowlist ⇒ verdict = "allowed" ∧ audit_action ≠ "egress_denied"
 host ∉ allowlist ⇒ verdict = "denied" ∧ audit_action = "egress_denied"
 ∧ HTTP 403 returned
 ∧ structured log carries "egress_denied"
 ∧ ``firecrawl_egress_denied_total`` counter advanced

The Hypothesis strategies build random hosts and random allowlists and
deliberately steer half the examples into the *allowed* branch and the
other half into the *denied* branch. Both branches assert the full triple
(verdict + audit token + observable record) so a regression in either the
matching predicate or the side-effect emission shows up here.

Why this file talks to multiple layers
--------------------------------------

The the operational rule names two enforcement surfaces - the pure-Python allowlist
helper *and* the FastAPI 403 / log / metric triple. We exercise both: the
fast pure-function class drives Hypothesis at high iteration counts to
shake out boundary conditions, and a smaller TestClient class samples the
HTTP path to confirm the side-effects are wired up.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Iterator

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap for the firecrawl service src tree
# ---------------------------------------------------------------------------
#
# The ``firecrawl-egress`` package is not pip-installed inside the property
# test environment. We expose its ``src/`` directory on ``sys.path`` the same
# way ``test_webhook_predicates.py`` and ``test_replay_dedup.py`` expose the
# automation-service src tree, so ``import firecrawl.egress`` resolves
# directly to ``platform/services/firecrawl/src/firecrawl/egress.py``.

_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]
_FIRECRAWL_SRC: Path = (
    _PLATFORM_ROOT / "services" / "firecrawl" / "src"
)
if _FIRECRAWL_SRC.is_dir() and str(_FIRECRAWL_SRC) not in sys.path:
    sys.path.insert(0, str(_FIRECRAWL_SRC))


from firecrawl.app import create_app  # noqa: E402
from firecrawl.config import Settings  # noqa: E402
from firecrawl.egress import (  # noqa: E402
    EGRESS_ALLOWED_AUDIT_ACTION,
    EGRESS_DENIED_AUDIT_ACTION,
    decide_egress,
    is_host_allowed,
    parse_allowlist,
)
from firecrawl.metrics import metrics  # noqa: E402


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: A DNS label: lowercase ASCII letters / digits / single hyphen, 1-20 chars.
#: Hyphens are allowed but never leading or trailing.
_dns_label = st.from_regex(r"\A[a-z0-9](?:[a-z0-9-]{0,18}[a-z0-9])?\Z", fullmatch=True)


@st.composite
def _hostnames(draw: st.DrawFn) -> str:
    """Build a 1-4-label DNS hostname (no scheme, no port)."""

    n = draw(st.integers(min_value=1, max_value=4))
    labels = [draw(_dns_label) for _ in range(n)]
    return ".".join(labels)


@st.composite
def _allowlists(draw: st.DrawFn) -> tuple[str, ...]:
    """Build a non-empty allowlist of 1-5 distinct host suffixes."""

    n = draw(st.integers(min_value=1, max_value=5))
    hosts: list[str] = []
    seen: set[str] = set()
    for _ in range(n):
        h = draw(_hostnames())
        if h not in seen:
            hosts.append(h)
            seen.add(h)
    return tuple(hosts)


@st.composite
def _allowed_pairs(draw: st.DrawFn) -> tuple[str, tuple[str, ...]]:
    """Pick an allowlist and a target host that the matcher MUST allow.

 Half the time the host is taken verbatim from the allowlist (exact
 match) and half the time we prepend a random subdomain so we exercise
 the label-boundary branch of:func:`is_host_allowed` too.
 """

    allowlist = draw(_allowlists())
    base = draw(st.sampled_from(allowlist))
    if draw(st.booleans()):
        return base, allowlist
    sub = draw(_dns_label)
    return f"{sub}.{base}", allowlist


@st.composite
def _denied_pairs(draw: st.DrawFn) -> tuple[str, tuple[str, ...]]:
    """Pick an allowlist and a target host that the matcher MUST deny.

 The host is sampled until it matches none of the allowlist suffixes
 under the label-boundary rule. Hypothesis ``assume`` filters out the
 rare draws where the random host happens to land inside the allowlist
 (e.g. ``a`` drawn under an allowlist of ``a``).
 """

    allowlist = draw(_allowlists())
    host = draw(_hostnames())
    assume(not is_host_allowed(host, allowlist))
    # Defensive guard: also assume the host doesn't accidentally match a
    # confusable parent - the assumption above already covers it but we
    # keep the explicit check for readability.
    for entry in allowlist:
        assume(host != entry)
        assume(not host.endswith("." + entry))
    return host, allowlist


# ---------------------------------------------------------------------------
# pure-function allowlist matcher behavior
# ---------------------------------------------------------------------------


class TestPureAllowlistMatcher:
    """``decide_egress`` returns the right verdict for every (host, allowlist).


 """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow, HealthCheck.filter_too_much),
    )
    @given(pair=_allowed_pairs())
    def test_host_in_allowlist_is_allowed(
        self, pair: tuple[str, tuple[str, ...]]
    ) -> None:
        """Matched hosts are allowed by the egress decision.

 For any ``(host, allowlist)`` pair where ``host`` matches one of
 the allowlist suffixes under the label-boundary rule, the
 decision is ``"allowed"`` and the audit token is **not**
 ``egress_denied``.
 """

        host, allowlist = pair
        url = f"https://{host}/path"
        decision = decide_egress(url, allowlist)
        assert decision.verdict == "allowed"
        assert decision.host == host.lower()
        assert decision.audit_action == EGRESS_ALLOWED_AUDIT_ACTION
        assert decision.audit_action != EGRESS_DENIED_AUDIT_ACTION
        assert decision.reason == "allowlisted"

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow, HealthCheck.filter_too_much),
    )
    @given(pair=_denied_pairs())
    def test_host_not_in_allowlist_emits_egress_denied(
        self, pair: tuple[str, tuple[str, ...]]
    ) -> None:
        """Unmatched hosts are denied with the egress audit token.

 For any ``(host, allowlist)`` pair where ``host`` is outside the
 allowlist, the decision is ``"denied"`` and the audit token is
 the canonical ``egress_denied`` string. This is the
 observable-record half of the property: callers downstream
 (FastAPI handler, audit writer) can pivot off this exact
 constant.
 """

        host, allowlist = pair
        url = f"https://{host}/path"
        decision = decide_egress(url, allowlist)
        assert decision.verdict == "denied"
        assert decision.host == host.lower()
        assert decision.audit_action == EGRESS_DENIED_AUDIT_ACTION
        assert decision.reason == "not_in_allowlist"

    @settings(max_examples=100, deadline=2000)
    @given(host=_hostnames())
    def test_empty_allowlist_denies_every_host(self, host: str) -> None:
        """An empty allowlist denies every external host.

 The closed-by-default posture: an empty
 ``FIRECRAWL_EGRESS_ALLOWLIST`` denies every external host, with
 the canonical ``egress_denied`` audit action.
 """

        decision = decide_egress(f"https://{host}/", ())
        assert decision.verdict == "denied"
        assert decision.audit_action == EGRESS_DENIED_AUDIT_ACTION
        assert decision.reason == "empty_allowlist"

    @settings(max_examples=200, deadline=2000)
    @given(pair=_allowed_pairs())
    def test_allow_decision_round_trips_through_parse_allowlist(
        self, pair: tuple[str, tuple[str, ...]]
    ) -> None:
        """Parsing the allowlist preserves allow decisions.

 Going through the env-string parser (the real production path)
 does not change the verdict: a host that matches the typed
 tuple also matches the parsed comma-separated string built from
 the same tuple. This guards against drift between the parser
 and the matcher.
 """

        host, allowlist = pair
        raw = ",".join(allowlist)
        parsed = parse_allowlist(raw)
        # The parser may dedupe / reorder; the resulting matcher must
        # still allow the same host.
        decision = decide_egress(f"https://{host}/", parsed)
        assert decision.verdict == "allowed"
        assert decision.audit_action != EGRESS_DENIED_AUDIT_ACTION


# ---------------------------------------------------------------------------
# observable HTTP / log / metric record behavior
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_metrics() -> Iterator[None]:
    """Each property example starts with fresh counters.

 The fixture is module-local rather than session-scoped so the metric
 assertions inside the invariant are deterministic regardless of
 which Hypothesis example runs first.
 """

    metrics.reset()
    yield
    metrics.reset()


class TestObservableEgressDeniedRecord:
    """Allowlist-failing requests produce an observable ``egress_denied`` record.



 These tests exercise the FastAPI surface so the property captures
 the *observable* part of: HTTP 403 response, ``egress_denied``
 in the structured log, and a bumped Prometheus counter. We use a
 single TestClient per example (cheap - no network) and rely on
 Hypothesis to drive the matrix of allowlists and target hosts.
 """

    @settings(
        max_examples=50,
        deadline=4000,
        suppress_health_check=(
            HealthCheck.too_slow,
            HealthCheck.filter_too_much,
            HealthCheck.function_scoped_fixture,
        ),
    )
    @given(pair=_denied_pairs())
    def test_denied_host_returns_403_and_logs_egress_denied(
        self,
        caplog: pytest.LogCaptureFixture,
        pair: tuple[str, tuple[str, ...]],
    ) -> None:
        """Denied hosts return 403 and emit observable records.

 The observable contract: a request to a non-allowlisted host
 SHALL return HTTP 403 with the ``egress_denied`` error code and
 emit a structured log record carrying the same token. The
 counter ``firecrawl_egress_denied_total`` advances by exactly
 one per request.
 """

        from fastapi.testclient import TestClient

        host, allowlist = pair
        # Defensive: skip the rare case where the random host string
        # happens to be a structurally-invalid URL component once the
        # allowlist parser folds it back to lowercase.
        assume(host == host.lower())

        metrics.reset()
        caplog.clear()
        caplog.set_level(logging.WARNING, logger="firecrawl.egress")

        settings_obj = Settings(
            FIRECRAWL_EGRESS_ALLOWLIST=",".join(allowlist),
            FIRECRAWL_UPSTREAM_BASE_URL="",
        )
        client = TestClient(create_app(settings=settings_obj))

        resp = client.post("/scrape", json={"url": f"https://{host}/path"})

        # 1. HTTP 403 with the canonical error code.
        assert resp.status_code == 403
        body = resp.json()
        assert body["error"] == EGRESS_DENIED_AUDIT_ACTION
        assert body["error_code"] == EGRESS_DENIED_AUDIT_ACTION
        assert body["host"] == host

        # 2. Structured log carries the ``egress_denied`` token.
        denial_records = [
            r
            for r in caplog.records
            if "egress_denied" in r.getMessage()
            or getattr(r, "audit_action", None) == EGRESS_DENIED_AUDIT_ACTION
        ]
        assert denial_records, (
            "expected at least one log record with audit_action=egress_denied; "
            f"got: {[r.getMessage() for r in caplog.records]}"
        )

        # 3. Metric counter advanced by exactly one and the allowed
        # counter did not move.
        assert metrics.denied == 1
        assert metrics.allowed == 0

    @settings(
        max_examples=50,
        deadline=4000,
        suppress_health_check=(
            HealthCheck.too_slow,
            HealthCheck.filter_too_much,
            HealthCheck.function_scoped_fixture,
        ),
    )
    @given(pair=_allowed_pairs())
    def test_allowed_host_does_not_emit_egress_denied(
        self,
        caplog: pytest.LogCaptureFixture,
        pair: tuple[str, tuple[str, ...]],
    ) -> None:
        """Allowed hosts do not emit denied-egress records.

 The complementary property: a request to an allow-listed host
 SHALL **not** produce an ``egress_denied`` log record and the
 denial counter SHALL **not** advance. We do not assert on the
 forwarded HTTP status (the built-in fetcher would attempt a
 real network call) - instead we install a metrics-and-log
 check that is invariant to the upstream branch.
 """

        from unittest.mock import patch

        from fastapi.testclient import TestClient

        host, allowlist = pair
        metrics.reset()
        caplog.clear()
        caplog.set_level(logging.WARNING, logger="firecrawl.egress")

        settings_obj = Settings(
            FIRECRAWL_EGRESS_ALLOWLIST=",".join(allowlist),
            FIRECRAWL_UPSTREAM_BASE_URL="",
        )
        client = TestClient(create_app(settings=settings_obj))

        # Stub the forwarder so we never actually hit the network. The
        # allowlist check happens *before* the forwarder is invoked, so
        # the property's observable side (allowed counter, no denial
        # log) is fully exercised even with the stub returning a fake
        # 200 envelope.
        from fastapi.responses import JSONResponse

        async def _fake_forward(*args, **kwargs):
            return JSONResponse(
                status_code=200,
                content={"url": kwargs.get("target_url", ""), "stubbed": True},
            )

        with patch("firecrawl.app._forward_or_fetch", new=_fake_forward):
            resp = client.post("/scrape", json={"url": f"https://{host}/path"})

        # 1. The request was not denied at the egress layer.
        assert resp.status_code != 403, (
            f"expected an allowed host to bypass the 403 branch; "
            f"got body: {resp.text}"
        )

        # 2. No structured log emitted ``egress_denied`` for this host.
        denial_records = [
            r
            for r in caplog.records
            if getattr(r, "audit_action", None) == EGRESS_DENIED_AUDIT_ACTION
        ]
        assert denial_records == [], (
            f"unexpected egress_denied log records for allow-listed host {host}: "
            f"{[r.getMessage() for r in denial_records]}"
        )

        # 3. The denial counter did not move; the allowed counter did.
        assert metrics.denied == 0
        assert metrics.allowed == 1
