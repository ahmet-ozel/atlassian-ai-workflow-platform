"""Unit tests for :mod:`src.chat.write_action`.

These tests cover the deterministic decision table of
:func:`src.chat.write_action.is_write_intent` and the structural
invariants of the :data:`WRITE_ACTION_TOOLS` frozen set.

The exhaustive property-based test of the same predicate lives in
``platform/tests/property/test_write_action_intercept.py``. The unit
tests here focus on tabular spot checks and immutability so a regression
in the catalogue or the predicate is caught quickly during local iteration.
"""

from __future__ import annotations

import pytest

from src.chat.write_action import (
    WRITE_ACTION_TOOLS,
    ToolCall,
    is_write_intent,
)


# ---------------------------------------------------------------------------
# WRITE_ACTION_TOOLS catalogue
# ---------------------------------------------------------------------------


class TestWriteActionToolsCatalogue:
    """Structural invariants of the static tool catalogue."""

    def test_is_a_frozen_set(self) -> None:
        """The catalogue must be immutable at runtime."""
        assert isinstance(WRITE_ACTION_TOOLS, frozenset)

    def test_exact_membership_matches_design(self) -> None:
        """The seven entries match the write-action catalogue."""
        assert WRITE_ACTION_TOOLS == frozenset(
            {
                "bitbucket_create_pull_request_cloud",
                "bitbucket_create_pull_request_dc",
                "bitbucket_commit",
                "confluence_create_page",
                "confluence_update_page",
                "jira_create_issue",
                "jira_transition_issue",
            }
        )

    def test_cardinality_is_seven(self) -> None:
        """Guard against accidental additions or removals."""
        assert len(WRITE_ACTION_TOOLS) == 7

    def test_excludes_banned_tools(self) -> None:
        """``bitbucket_merge_pr`` and ``confluence_delete_page`` are blocked
        upstream by the foundation banned-tool list and must not
        appear here --- including them would be redundant *and* would
        suggest the chat handler is the final gate, which it is not."""
        assert "bitbucket_merge_pr" not in WRITE_ACTION_TOOLS
        assert "confluence_delete_page" not in WRITE_ACTION_TOOLS


# ---------------------------------------------------------------------------
# is_write_intent decision table
# ---------------------------------------------------------------------------


class TestIsWriteIntent:
    """Tabular checks for the three rows of the decision table."""

    def test_explicit_intent_returns_true_for_any_tool_name(self) -> None:
        """Row 1: ``llm_intent_field == 'write_action_requested'``  True
        regardless of the tool name (including read-only tools)."""
        call = ToolCall(tool_name="jira_search")  # not in WRITE_ACTION_TOOLS

        assert is_write_intent(call, llm_intent_field="write_action_requested") is True

    def test_explicit_intent_overrides_safe_tool(self) -> None:
        """Even an empty tool name combined with the explicit intent
        triggers an intercept --- the LLM's structured signal is
        authoritative."""
        call = ToolCall(tool_name="")

        assert is_write_intent(call, llm_intent_field="write_action_requested") is True

    @pytest.mark.parametrize(
        "tool_name",
        sorted(
            {
                "bitbucket_create_pull_request_cloud",
                "bitbucket_create_pull_request_dc",
                "bitbucket_commit",
                "confluence_create_page",
                "confluence_update_page",
                "jira_create_issue",
                "jira_transition_issue",
            }
        ),
    )
    def test_implicit_intent_via_tool_name(self, tool_name: str) -> None:
        """Row 2: every tool name in :data:`WRITE_ACTION_TOOLS` triggers an
        intercept even when the explicit intent is missing or unrelated."""
        call = ToolCall(tool_name=tool_name)

        assert is_write_intent(call, llm_intent_field=None) is True
        assert is_write_intent(call, llm_intent_field="read_action") is True

    @pytest.mark.parametrize(
        "tool_name",
        ["jira_search", "confluence_search", "bitbucket_get_pull_request", ""],
    )
    @pytest.mark.parametrize("intent", [None, "read_action", "unknown_intent"])
    def test_safe_call_returns_false(
        self, tool_name: str, intent: str | None
    ) -> None:
        """Row 3: a tool name *outside* the catalogue paired with any
        non-write intent (including ``None``) must pass through."""
        call = ToolCall(tool_name=tool_name)

        assert is_write_intent(call, llm_intent_field=intent) is False

    def test_predicate_is_pure(self) -> None:
        """Calling the predicate twice on the same inputs must yield the
        same result and must not mutate the inputs."""
        call = ToolCall(tool_name="jira_create_issue")

        first = is_write_intent(call, llm_intent_field=None)
        second = is_write_intent(call, llm_intent_field=None)

        assert first is True
        assert second is True
        # ToolCall is frozen so this is doubly guaranteed, but assert the
        # tool_name field still reads identically as a smoke check.
        assert call.tool_name == "jira_create_issue"

    def test_accepts_protocol_compatible_objects(self) -> None:
        """The predicate is typed against :class:`ToolCallLike`; any
        object with a string ``tool_name`` attribute works."""

        class _Stub:
            tool_name = "jira_create_issue"

        assert is_write_intent(_Stub(), llm_intent_field=None) is True


# ---------------------------------------------------------------------------
# ToolCall placeholder dataclass
# ---------------------------------------------------------------------------


class TestToolCallDataclass:
    """The minimal local placeholder used until ``libs/messages`` lands."""

    def test_is_frozen(self) -> None:
        call = ToolCall(tool_name="jira_create_issue")
        with pytest.raises(Exception):  # FrozenInstanceError, but keep generic
            call.tool_name = "other"  # type: ignore[misc]

    def test_equality_is_structural(self) -> None:
        assert ToolCall(tool_name="x") == ToolCall(tool_name="x")
        assert ToolCall(tool_name="x") != ToolCall(tool_name="y")
