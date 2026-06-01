"""Property test P13 — CQL ``order_by`` validation rejects unknown fields.

Validates Requirement 35.2 / design Property 13: ``CQLAdvancedMixin.cql_search``
must raise ``invalid_order_by`` *before* issuing any outbound HTTP request
when the supplied ``order_by`` is outside
:data:`mcp_atlassian.confluence.cql_advanced.SORTABLE_FIELDS`, and must
issue exactly one outbound HTTP call for every member of ``SORTABLE_FIELDS``.

Test shape:

* **Property A (Hypothesis)** — for any ``order_by`` string outside the
  allowlist, ``cql_search`` raises ``ValueError`` prefixed with
  ``invalid_order_by:`` and the mocked HTTP session records zero calls.
  Covers arbitrary rejected inputs (the negative space).
* **Property B (parametrized)** — for every member of ``SORTABLE_FIELDS``,
  ``cql_search`` passes validation and issues exactly one HTTP call.
  Covers the positive space exhaustively; a fixed ``@pytest.mark.parametrize``
  rather than Hypothesis keeps the enumeration explicit and fast.

The test exercises the mixin directly rather than the server tool so the
"zero outbound HTTP on validation failure" invariant is observable on a
plain ``requests.Session``-shaped mock — no fetcher bootstrap required.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcp_atlassian.confluence.cql_advanced import (
    SORTABLE_FIELDS,
    CQLAdvancedMixin,
)


# ---------------------------------------------------------------------------
# Hypothesis strategy — arbitrary non-sortable order_by strings
# ---------------------------------------------------------------------------
#
# The task brief specifies ``text(min_size=1, max_size=30)`` filtered against
# ``SORTABLE_FIELDS``. This covers the full negative space: any non-empty
# Unicode string up to 30 chars that is not one of the six allowed fields.
invalid_order_by_strategy: st.SearchStrategy[str] = (
    st.text(min_size=1, max_size=30).filter(lambda s: s not in SORTABLE_FIELDS)
)


def _bind_mixin_to_session(session_mock) -> SimpleNamespace:
    """Return a minimal ``self`` shim for ``CQLAdvancedMixin.cql_search``.

    The mixin calls ``self.confluence.get(...)`` for its single outbound
    request. Wiring that call through ``session_mock.get`` lets the
    fixture's call-counting helpers observe the real HTTP surface without
    needing a full ``ConfluenceFetcher`` (which would require env config
    and network probes to instantiate).
    """
    return SimpleNamespace(confluence=SimpleNamespace(get=session_mock.get))


# ---------------------------------------------------------------------------
# Property A — invalid order_by raises and issues ZERO HTTP calls
# ---------------------------------------------------------------------------


@given(order_by=invalid_order_by_strategy)
def test_invalid_order_by_raises_before_any_http(
    mock_requests_session, order_by: str
) -> None:
    """P13.A: any non-allowlisted ``order_by`` is rejected pre-HTTP."""
    # Reset between Hypothesis examples so the "zero calls" assertion is
    # meaningful for *this* example rather than cumulative across the run.
    mock_requests_session.reset_mock()

    fake_self = _bind_mixin_to_session(mock_requests_session)

    with pytest.raises(ValueError) as excinfo:
        CQLAdvancedMixin.cql_search(
            fake_self,
            "type = page",
            order_by=order_by,
        )

    assert str(excinfo.value).startswith("invalid_order_by:"), (
        f"expected 'invalid_order_by:' prefix, got {excinfo.value!r}"
    )
    mock_requests_session.assert_no_http_called()


# ---------------------------------------------------------------------------
# Property B — every SORTABLE_FIELDS member performs exactly one HTTP call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("order_by", sorted(SORTABLE_FIELDS))
def test_sortable_order_by_issues_exactly_one_http(
    mock_requests_session, order_by: str
) -> None:
    """P13.B: every allowlisted ``order_by`` reaches the upstream once."""
    fake_self = _bind_mixin_to_session(mock_requests_session)

    CQLAdvancedMixin.cql_search(
        fake_self,
        "type = page",
        order_by=order_by,
    )

    mock_requests_session.assert_http_call_count(1)
    mock_requests_session.assert_http_methods_called({"get"})
