"""Property test P14 — CQL space-filter awareness.

Validates Requirement 35.3 / 43.3 and design Property 14:
:meth:`mcp_atlassian.confluence.cql_advanced.CQLAdvancedMixin.rewrite_cql_for_space_filter`
must intersect a caller-supplied CQL query with the operator-configured
``CONFLUENCE_SPACES_FILTER`` allow-list so that the outbound search is
always bounded by the allow-list. Three sub-properties are exercised here:

* **Property A — disjoint referenced spaces raise ``filtered_out`` with ZERO HTTP.**
  *For any* non-empty ``requested`` space set that is disjoint from a
  non-empty ``allowed`` allow-list, rewriting a CQL query that references
  those ``requested`` keys raises ``ValueError("filtered_out: ...")`` and
  no outbound ``cql_search`` call is issued afterwards (the server-layer
  contract is: if rewrite raises, short-circuit before HTTP).

* **Property B — subset referenced spaces pass through and issue exactly ONE HTTP call.**
  *For any* non-empty ``requested`` space set that is a subset of a
  non-empty ``allowed`` allow-list, the rewritten CQL references only
  space keys drawn from ``allowed`` and still references every key in
  ``requested``. A subsequent ``cql_search`` call on the rewritten CQL
  issues exactly one HTTP request (the expected search).

* **Property C — CQL without any space clause has an allow-list prepended.**
  *For any* CQL string that contains no ``space = KEY`` or
  ``space in (...)`` clause, the rewrite prepends ``space in (<allowed>)``
  and the referenced spaces in the rewritten CQL equal the full
  (case-normalized) ``allowed`` set.

The test exercises the mixin directly rather than the full server tool so
the "zero outbound HTTP when rewrite rejects" invariant is observable on
a plain ``requests.Session``-shaped mock — no fetcher bootstrap or env
configuration required.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from mcp_atlassian.confluence.cql_advanced import CQLAdvancedMixin

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------
#
# Space keys for this property are drawn from the narrow A–Z/2–5 shape
# called out in the P14 task brief. The cross-suite ``space_keys`` strategy
# in ``conftest`` is 2–10 which would generate noisier examples than this
# property needs — keeping the alphabet small here accelerates shrinking.

space_key_strategy: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(min_codepoint=ord("A"), max_codepoint=ord("Z")),
    min_size=2,
    max_size=5,
)


@st.composite
def _disjoint_allowed_requested(draw: st.DrawFn) -> tuple[set[str], set[str]]:
    """Draw an ``(allowed, requested)`` pair where both sets are non-empty and disjoint.

    We draw a single pool of unique keys and partition it in two rather
    than drawing the two sets independently with ``assume(...)``; this
    avoids the filter-retry overhead on small 26-letter alphabets and
    guarantees a valid example on every draw.
    """
    pool = draw(st.sets(space_key_strategy, min_size=2, max_size=8))
    # ``st.sets(..., min_size=2)`` already guarantees ``len(pool) >= 2``,
    # but assert for the benefit of shrinker-induced edge cases.
    assume(len(pool) >= 2)
    pool_list: list[str] = sorted(pool)
    split_point = draw(st.integers(min_value=1, max_value=len(pool_list) - 1))
    allowed = set(pool_list[:split_point])
    requested = set(pool_list[split_point:])
    return allowed, requested


@st.composite
def _subset_allowed_requested(draw: st.DrawFn) -> tuple[set[str], set[str]]:
    """Draw an ``(allowed, requested)`` pair where ``requested`` is a non-empty subset of ``allowed``."""
    allowed = draw(st.sets(space_key_strategy, min_size=1, max_size=5))
    # ``st.sampled_from`` over a sorted list keeps the draw deterministic
    # given a fixed ``allowed``; size is bounded by ``len(allowed)``.
    requested = draw(
        st.sets(
            st.sampled_from(sorted(allowed)),
            min_size=1,
            max_size=len(allowed),
        )
    )
    return allowed, requested


# CQL fragments that contain no ``space`` clause. These exercise the
# "prepend an allow-list" branch of :meth:`rewrite_cql_for_space_filter`.
# An empty string is included so the prefix-only emission branch (no
# trailing ``AND (...)``) is covered.
_CQL_WITHOUT_SPACE_CLAUSE: tuple[str, ...] = (
    "",
    "type = page",
    "type = blogpost",
    'title ~ "release notes"',
    "lastmodified >= now('-7d')",
    "creator = currentUser()",
    'label = "needs-review"',
    'text ~ "quarterly"',
)


cql_without_space_strategy: st.SearchStrategy[str] = st.sampled_from(
    _CQL_WITHOUT_SPACE_CLAUSE
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Standalone regexes mirroring the mixin's private shapes. Kept local so
# the test does not depend on private module attributes and so the
# assertions remain valid even if the mixin's regex internals shift.
_SPACE_EQ_RE = re.compile(r"space\s*=\s*\"?([^\"\s,\)]+)\"?", re.IGNORECASE)
_SPACE_IN_RE = re.compile(r"space\s+in\s*\(([^)]*)\)", re.IGNORECASE)


def _extract_space_refs(cql: str) -> set[str]:
    """Return the case-normalized set of space keys referenced in ``cql``."""
    refs: set[str] = set()
    for match in _SPACE_EQ_RE.finditer(cql):
        refs.add(match.group(1).strip().upper())
    for match in _SPACE_IN_RE.finditer(cql):
        inner = match.group(1)
        for token in inner.split(","):
            key = token.strip().strip('"').strip()
            if key:
                refs.add(key.upper())
    return refs


def _build_cql_with_spaces(requested: set[str], *, use_in_form: bool) -> str:
    """Build a CQL string that references every key in ``requested``.

    ``use_in_form=True`` emits ``space in (A, B, C)``; otherwise a chain
    of ``space = K`` predicates joined by ``OR`` is emitted. Both shapes
    are legal CQL and both are detected by the mixin's regex.
    """
    if not requested:
        raise ValueError("requested must be non-empty")
    keys = sorted(requested)
    if use_in_form or len(keys) > 1 and use_in_form:
        return f"space in ({', '.join(keys)})"
    if len(keys) == 1:
        return f"space = {keys[0]}"
    # Chain of equality predicates.
    return " OR ".join(f"space = {k}" for k in keys)


def _bind_mixin_to_session(session_mock) -> SimpleNamespace:
    """Return a minimal ``self`` shim for the mixin methods.

    ``CQLAdvancedMixin.rewrite_cql_for_space_filter`` does not touch
    ``self``, but ``CQLAdvancedMixin.cql_search`` does call
    ``self.confluence.get(...)``; wiring that call through the fixture's
    ``get`` mock lets the call-counting helpers observe the real HTTP
    surface without bootstrapping a full ``ConfluenceFetcher``.
    """
    return SimpleNamespace(confluence=SimpleNamespace(get=session_mock.get))


# ---------------------------------------------------------------------------
# Property A — disjoint referenced spaces raise filtered_out + ZERO HTTP
# ---------------------------------------------------------------------------


@given(
    pair=_disjoint_allowed_requested(),
    use_in_form=st.booleans(),
)
def test_disjoint_referenced_spaces_raise_filtered_out_without_http(
    mock_requests_session, pair: tuple[set[str], set[str]], use_in_form: bool
) -> None:
    """P14.A: requested ∩ allowed == ∅ ⇒ filtered_out + zero outbound HTTP."""
    allowed, requested = pair
    # Sanity-check the strategy postcondition locally so a failed example
    # carries the intent (rather than a confusing downstream assertion).
    assert requested, "strategy must produce a non-empty requested set"
    assert allowed.isdisjoint(requested), (
        "strategy must produce disjoint allowed/requested sets"
    )

    # Reset between Hypothesis examples so the "zero HTTP" assertion is
    # meaningful for *this* example rather than cumulative across the run.
    mock_requests_session.reset_mock()
    fake_self = _bind_mixin_to_session(mock_requests_session)

    cql = _build_cql_with_spaces(requested, use_in_form=use_in_form)

    with pytest.raises(ValueError) as excinfo:
        CQLAdvancedMixin.rewrite_cql_for_space_filter(fake_self, cql, allowed)

    assert str(excinfo.value).startswith("filtered_out:"), (
        f"expected 'filtered_out:' prefix, got {excinfo.value!r}"
    )

    # The server layer short-circuits on the ValueError and never issues
    # the outbound search. No HTTP call should be recorded on the mocked
    # session — this is the invariant that guarantees disjoint references
    # never reach Confluence (Req 35.3 / 43.3).
    mock_requests_session.assert_no_http_called()


# ---------------------------------------------------------------------------
# Property B — subset referenced spaces pass through + exactly ONE HTTP
# ---------------------------------------------------------------------------


@given(
    pair=_subset_allowed_requested(),
    use_in_form=st.booleans(),
)
def test_subset_referenced_spaces_pass_through_with_one_http(
    mock_requests_session, pair: tuple[set[str], set[str]], use_in_form: bool
) -> None:
    """P14.B: requested ⊆ allowed ⇒ rewrite preserves requested within allowed
    and downstream cql_search issues exactly one HTTP call."""
    allowed, requested = pair
    assert requested, "strategy must produce a non-empty requested set"
    assert requested.issubset(allowed), (
        "strategy must produce requested ⊆ allowed"
    )

    mock_requests_session.reset_mock()
    fake_self = _bind_mixin_to_session(mock_requests_session)

    cql = _build_cql_with_spaces(requested, use_in_form=use_in_form)

    effective_cql = CQLAdvancedMixin.rewrite_cql_for_space_filter(
        fake_self, cql, allowed
    )

    # The rewrite must not issue any outbound HTTP by itself.
    mock_requests_session.assert_no_http_called()

    refs = _extract_space_refs(effective_cql)
    # Every referenced space key must come from the allow-list...
    assert refs.issubset({s.upper() for s in allowed}), (
        f"effective CQL references {refs!r} outside allow-list "
        f"{sorted(allowed)!r}: {effective_cql!r}"
    )
    # ...and every requested space key must still be referenced so the
    # caller's intent is preserved.
    assert {s.upper() for s in requested}.issubset(refs), (
        f"effective CQL {effective_cql!r} dropped requested keys; "
        f"refs={refs!r}, requested={sorted(requested)!r}"
    )

    # Now exercise the downstream search and assert exactly one HTTP call.
    CQLAdvancedMixin.cql_search(fake_self, effective_cql)

    mock_requests_session.assert_http_call_count(1)
    mock_requests_session.assert_http_methods_called({"get"})


# ---------------------------------------------------------------------------
# Property C — CQL without a space clause has allow-list prepended
# ---------------------------------------------------------------------------


@given(
    allowed=st.sets(space_key_strategy, min_size=1, max_size=5),
    cql=cql_without_space_strategy,
)
def test_missing_space_clause_injects_allow_list(
    mock_requests_session, allowed: set[str], cql: str
) -> None:
    """P14.C: CQL with no space clause has ``space in (<allowed>)`` prepended."""
    # Pre-condition: the strategy pool intentionally contains no space
    # clauses; assert it as a safety net so any accidental edit to
    # ``_CQL_WITHOUT_SPACE_CLAUSE`` is caught early.
    assert _extract_space_refs(cql) == set(), (
        f"fixture CQL {cql!r} unexpectedly contains space references"
    )

    mock_requests_session.reset_mock()
    fake_self = _bind_mixin_to_session(mock_requests_session)

    effective_cql = CQLAdvancedMixin.rewrite_cql_for_space_filter(
        fake_self, cql, allowed
    )

    # The rewrite must not issue any outbound HTTP by itself.
    mock_requests_session.assert_no_http_called()

    # The rewritten CQL must carry a ``space in (...)`` clause referencing
    # exactly the (case-normalized) allow-list.
    assert re.search(r"space\s+in\s*\(", effective_cql, re.IGNORECASE), (
        f"expected injected 'space in (...)' clause in {effective_cql!r}"
    )
    refs = _extract_space_refs(effective_cql)
    assert refs == {s.upper() for s in allowed}, (
        f"effective CQL references {refs!r}; expected allow-list "
        f"{sorted(s.upper() for s in allowed)!r}: {effective_cql!r}"
    )

    # When the original CQL had a non-empty body, the rewrite must wrap it
    # with a logical AND so the caller's predicates are preserved.
    if cql.strip():
        assert cql in effective_cql, (
            f"rewrite dropped caller predicates; cql={cql!r}, "
            f"effective={effective_cql!r}"
        )
        assert " AND " in effective_cql.upper(), (
            f"rewrite failed to AND-join prefix with caller predicates: "
            f"{effective_cql!r}"
        )
