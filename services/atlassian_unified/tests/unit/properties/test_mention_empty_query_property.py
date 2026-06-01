"""Property 10 — empty-query short-circuit for Jira mention suggestions.

Validates Requirement 23.2:

    For any string ``q`` that is empty or composed entirely of whitespace
    (including tabs, newlines, mixed-width spaces), invoking
    ``jira_get_mention_suggestions(query=q)`` SHALL return an empty list
    AND SHALL cause zero outbound HTTP requests against the Jira mention
    suggestions endpoint.

The contract is enforced inside
:meth:`mcp_atlassian.jira.mentions.MentionsMixin.get_mention_suggestions`
so we exercise the mixin directly against a mocked ``.jira`` attribute
(an :class:`atlassian.Jira`-shaped object) and assert:

* Property A — whitespace-only queries: result is ``[]`` **and** zero
  HTTP methods on the mock were invoked. The Hypothesis strategy draws
  from ASCII whitespace, tab, CR, LF, vertical tab, form feed, and
  U+00A0 (non-breaking space), with sizes including 0 so the empty
  string is exercised on every run.
* Property B — sanity: two non-whitespace sample queries each cause
  exactly one call to the picker endpoint, confirming the short-circuit
  is gated on the query content and not always suppressing HTTP.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcp_atlassian.jira.mentions import MentionsMixin

# ---------------------------------------------------------------------------
# Whitespace-query strategy
# ---------------------------------------------------------------------------

# Whitespace alphabet covering the cases called out in the design:
#   * ASCII space, tab, newline, carriage return
#   * Vertical tab (\x0b) and form feed (\x0c)
#   * U+00A0 non-breaking space (mixed-width space)
# ``min_size=0`` ensures the empty string is sampled.
WHITESPACE_ALPHABET = " \t\n\r\x0b\x0c\u00a0"

whitespace_queries: st.SearchStrategy[str] = st.text(
    alphabet=WHITESPACE_ALPHABET,
    min_size=0,
    max_size=20,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mentions_mixin(session_mock: Any) -> MentionsMixin:
    """Instantiate ``MentionsMixin`` without invoking ``JiraClient.__init__``.

    ``JiraClient`` performs real auth + atlassian-python-api client
    construction in ``__init__``; property tests only need the mixin's
    method under test. ``object.__new__`` sidesteps the base-class
    initializer and the mocked ``.jira`` attribute supplies the only
    collaborator the method touches.
    """
    mixin = object.__new__(MentionsMixin)
    # The mixin calls ``self.jira.get(...)`` — the mocked session plays
    # that role. Attribute assignment is enough; no other attributes of
    # ``JiraClient`` are referenced from ``get_mention_suggestions``.
    mixin.jira = session_mock  # type: ignore[attr-defined]
    return mixin


# ---------------------------------------------------------------------------
# Property A — whitespace-only queries short-circuit before any HTTP
# ---------------------------------------------------------------------------


@given(query=whitespace_queries)
def test_empty_or_whitespace_query_returns_empty_and_skips_http(
    query: str,
    mock_requests_session: Any,
) -> None:
    """Whitespace-only queries return ``[]`` with zero HTTP calls."""
    # Reset between Hypothesis examples so earlier iterations' call
    # counts don't leak into later ones.
    mock_requests_session.reset_mock()

    mixin = _make_mentions_mixin(mock_requests_session)

    result = mixin.get_mention_suggestions(query)

    assert result == [], (
        f"Expected [] for whitespace-only query {query!r}, got {result!r}"
    )
    mock_requests_session.assert_no_http_called()


# ---------------------------------------------------------------------------
# Property B — sanity: non-whitespace queries DO reach the picker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["alice", "bob.smith"])
def test_non_whitespace_query_triggers_http_call(
    query: str,
    mock_requests_session: Any,
) -> None:
    """A non-whitespace query reaches ``GET /rest/api/2/user/picker`` once."""
    # Return a well-formed picker envelope so the method returns the
    # ``users`` list rather than falling through the unexpected-shape
    # branch. The assertion here is about the HTTP call, but a realistic
    # response keeps the sanity check faithful to production behavior.
    mock_requests_session.get.return_value = {
        "users": [{"name": query, "displayName": query.title()}],
        "total": 1,
        "header": "Showing 1 of 1 matching users",
    }

    mixin = _make_mentions_mixin(mock_requests_session)

    result = mixin.get_mention_suggestions(query)

    # The picker returned one user, so result mirrors the envelope.
    assert isinstance(result, list)
    assert result and result[0]["name"] == query

    # Exactly one HTTP call — the picker GET — was issued.
    mock_requests_session.assert_http_call_count(1)
    mock_requests_session.assert_http_methods_called(["get"])
    mock_requests_session.get.assert_called_once()
    call_args, call_kwargs = mock_requests_session.get.call_args
    assert call_args[0] == "rest/api/2/user/picker"
    assert call_kwargs["params"]["query"] == query
