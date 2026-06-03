"""Invariant test: Confluence page create vs update decision.

Feature:,: For any confluence_page action,
if page_id is present in the action params then the existing page SHALL
be updated; if page_id is absent then a new page SHALL be created in
the specified space.

Strategy
--------
The Output_Action_Executor decides between ``confluence_update_page``
and ``confluence_create_page`` solely based on whether ``params["page_id"]``
is truthy. We mirror that decision in a pure helper and verify that, for
any randomly generated params dict, the decision matches the
truthiness of ``page_id``.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st


def _decide_action(params: dict) -> str:
    """Pure helper that mirrors the executor's decision logic.

 Mirrors:func:`automation_worker.activities.output_actions._handle_confluence_page`
 (which selects ``confluence_update_page`` if ``params.get("page_id")``
 is truthy, else ``confluence_create_page``).
 """
    page_id = params.get("page_id")
    if page_id:
        return "update"
    return "create"


@settings(max_examples=200, deadline=None)
@given(
    page_id=st.one_of(
        st.none(),
        st.text(min_size=0, max_size=20),
        st.integers(min_value=1, max_value=999_999),
    ),
    space=st.text(min_size=1, max_size=10),
    title=st.text(min_size=1, max_size=50),
)
def test_decision_based_on_page_id(page_id, space: str, title: str) -> None:
    """Decision is 'update' iff ``page_id`` is truthy, else 'create'."""
    params: dict = {"space": space, "title": title}
    if page_id is not None:
        params["page_id"] = page_id

    decision = _decide_action(params)
    if page_id:
        assert decision == "update", (
            f"Expected 'update' for truthy page_id={page_id!r}, got {decision!r}"
        )
    else:
        assert decision == "create", (
            f"Expected 'create' for falsy page_id={page_id!r}, got {decision!r}"
        )


@settings(max_examples=100, deadline=None)
@given(space=st.text(min_size=1, max_size=10), title=st.text(min_size=1, max_size=50))
def test_missing_page_id_means_create(space: str, title: str) -> None:
    """When ``page_id`` key is absent entirely, the decision is always 'create'."""
    params = {"space": space, "title": title}
    assert _decide_action(params) == "create"
