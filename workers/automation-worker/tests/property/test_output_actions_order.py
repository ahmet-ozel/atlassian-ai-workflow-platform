"""Invariant test: Output actions sequential execution order.

**: Output actions sequential execution order
-----------------------------------------------------
*For any* list of output actions (up to 20), the Output_Action_Executor
SHALL execute them in strict index order (0, 1, 2,...) and SHALL NOT
skip or reorder any action regardless of previous action outcomes.

Strategy
--------
Generate random lists of OutputAction objects with shuffled indices.
Use a fake MCP caller that records the order of calls. Verify that
actions are always executed in ascending index order regardless of
input order.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
_PLATFORM_ROOT: Path = _WORKER_ROOT.parents[1]
_DB_SHARED_SRC: Path = _PLATFORM_ROOT / "libs" / "db-shared" / "src"

for _candidate in (_SRC_DIR, _DB_SHARED_SRC):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)

from automation_worker.activities.output_actions import (  # noqa: E402
    ExecutionBatchInput,
    OutputAction,
    execute_output_actions,
    set_mcp_caller,
)
from db_shared.enums import ActionType  # noqa: E402


# ---------------------------------------------------------------------------
# Fake MCP caller that records execution order
# ---------------------------------------------------------------------------


@dataclass
class _OrderRecordingMCPCaller:
    """Records the index of each action in the order they are executed."""

    executed_indices: list[int] = field(default_factory=list)

    async def call_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        dept_id: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        # ``jira_transition`` first lists the issue's transitions to
        # resolve a numeric transition id; that lookup carries no
        # ``action_index`` and must not be counted as an executed action.
        # Return a transition list so the resolver matches and proceeds
        # to the real ``jira_transition_issue`` call (which carries the
        # action_index forwarded from the action params).
        if tool_name == "jira_get_transitions":
            return [{"id": "41", "name": "Done"}]
        # Record the action index from params
        if "action_index" in params:
            self.executed_indices.append(params["action_index"])
        return {"ok": True}


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Strategy for generating a valid ActionType
action_type_st = st.sampled_from(list(ActionType))


def output_actions_st(max_size: int = 20) -> st.SearchStrategy[list[OutputAction]]:
    """Generate a list of OutputAction objects with unique shuffled indices.

 Each action carries its own index in the params dict so the fake
 MCP caller can record execution order.
 """

    @st.composite
    def _build(draw: st.DrawFn) -> list[OutputAction]:
        n = draw(st.integers(min_value=1, max_value=max_size))
        # Generate unique indices (not necessarily contiguous)
        indices = draw(
            st.lists(
                st.integers(min_value=0, max_value=99),
                min_size=n,
                max_size=n,
                unique=True,
            )
        )
        # Shuffle the indices to simulate random input order
        shuffled_indices = draw(st.permutations(indices))

        actions: list[OutputAction] = []
        for idx in shuffled_indices:
            action_type = draw(action_type_st)
            params: dict[str, Any] = {
                "action_index": idx,
                "body": f"action-{idx}",
            }
            # A jira_transition action needs a target status so the
            # handler can resolve it to a transition id; the recording
            # caller returns a "Done" transition for the lookup.
            if action_type == ActionType.JIRA_TRANSITION:
                params["target_status"] = "done"
            actions.append(
                OutputAction(
                    type=action_type,
                    params=params,
                    index=idx,
                )
            )
        return actions

    return _build()


# ---------------------------------------------------------------------------
# Invariant test
# ---------------------------------------------------------------------------


class TestOutputActionsSequentialOrder:
    """: Output actions sequential execution order.

 **"""

    @given(actions=output_actions_st())
    @settings(max_examples=100, deadline=None)
    def test_actions_always_executed_in_ascending_index_order(
        self, actions: list[OutputAction]
    ) -> None:
        """For any shuffled list of actions, execution order is always
 ascending by index, regardless of input order.

 **"""
        caller = _OrderRecordingMCPCaller()
        set_mcp_caller(caller)

        inp = ExecutionBatchInput(
            actions=actions,
            issue_key="TEST-1",
            dept_id="test-dept",
            workflow_id="wf-prop-test",
        )

        result = asyncio.run(execute_output_actions(inp))

        # The executed indices must be in strictly ascending order
        assert caller.executed_indices == sorted(caller.executed_indices), (
            f"Actions were not executed in ascending index order. "
            f"Executed order: {caller.executed_indices}, "
            f"Expected order: {sorted(caller.executed_indices)}"
        )

        # All actions should have been executed (up to max 20)
        expected_count = min(len(actions), 20)
        assert len(caller.executed_indices) == expected_count, (
            f"Expected {expected_count} actions to be executed, "
            f"but got {len(caller.executed_indices)}"
        )

        # Result indices should also be in ascending order
        result_indices = [r.index for r in result.results]
        assert result_indices == sorted(result_indices), (
            f"Result indices not in ascending order: {result_indices}"
        )

    @given(actions=output_actions_st())
    @settings(max_examples=100, deadline=None)
    def test_no_actions_skipped_regardless_of_input_order(
        self, actions: list[OutputAction]
    ) -> None:
        """For any input order, all actions are executed - none are
 skipped or dropped.

 **"""
        caller = _OrderRecordingMCPCaller()
        set_mcp_caller(caller)

        inp = ExecutionBatchInput(
            actions=actions,
            issue_key="TEST-2",
            dept_id="test-dept",
            workflow_id="wf-prop-test-2",
        )

        result = asyncio.run(execute_output_actions(inp))

        # Collect expected indices (sorted, truncated to max 20)
        input_indices = sorted(a.index for a in actions)[:20]

        # All expected indices should appear in executed order
        assert caller.executed_indices == input_indices, (
            f"Not all actions were executed. "
            f"Expected indices: {input_indices}, "
            f"Executed indices: {caller.executed_indices}"
        )

        # Every result should have status "success" (fake caller never fails)
        for r in result.results:
            assert r.status == "success"
