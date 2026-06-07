"""Unit tests for ``AgentRunnerWorkflow`` ``multi_step`` (Epic) flow.

Covers the Epic fan-out handler ``_handle_multi_step``:

    1. Happy path - an Epic with two children fans out to one child
       ``AutomationWorkflow`` per subtask (sequentially), posts a
       progress comment per completed subtask and a final completion
       comment, and threads the parent's capability envelope into each
       child input.
    2. Empty Epic - no children yields a guidance comment, an
       ``epic_no_subtasks`` failure reason, and no child dispatch.
    3. Subtask failure - a child whose gateway result reports
       ``decision="denied"`` stops the fan-out with an
       ``epic_subtask_failed`` reason and skips the remaining subtasks.

The tests drive ``_handle_multi_step`` directly without spinning up a
Temporal worker, mirroring ``test_agent_runner_research.py``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from temporalio import workflow as _temporal_workflow


# ---------------------------------------------------------------------------
# sys.path bootstrap - mirrors ``test_agent_runner_research.py``.
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
_PLATFORM_ROOT: Path = _WORKER_ROOT.parents[1]
_TEMPORAL_SHARED_SRC: Path = _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
_MCP_CLIENT_SRC: Path = _PLATFORM_ROOT / "libs" / "mcp_client" / "src"

for _candidate in (_SRC_DIR, _TEMPORAL_SHARED_SRC, _MCP_CLIENT_SRC):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)

from agent_runner.workflows.agent_runner_workflow import (  # noqa: E402
    AgentRunnerWorkflow,
    _EpicSubtaskFailed,
)
from temporal_shared.messages import (  # noqa: E402
    AgentRunnerWorkflowInput,
    LlmAnalysisResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_input() -> AgentRunnerWorkflowInput:
    analysis = LlmAnalysisResult(
        workflow_type="multi_step",
        confidence="high",
        title="Epic fan-out",
        rationale="Epic with subtasks.",
        token_usage=42,
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id="automation-jira-EPIC-1",
        issue_key="EPIC-1",
        department_id="payment",
        workflow_type="multi_step",
        analysis=analysis,
        iteration=1,
        max_iter=5,
        default_language="tr",
        available_capabilities=("jira", "bitbucket"),
        available_repos=("smoke-test",),
        available_spaces=("DOCS",),
    )


@pytest.fixture
def make_wf():
    from dataclasses import replace

    def _build() -> AgentRunnerWorkflow:
        wf = AgentRunnerWorkflow()
        wf._iteration_state = replace(wf._iteration_state, iter_count=1)
        return wf

    return _build


def _drive(coro_factory) -> None:
    asyncio.run(coro_factory())


def _activity_dispatcher(children: Any) -> AsyncMock:
    """Resolve ``execute_activity``; ``jira_list_epic_children`` → children."""

    async def _fake(*args, **kwargs):
        name = args[0] if args else kwargs.get("activity")
        if name == "jira_list_epic_children":
            return children
        return None

    return AsyncMock(side_effect=_fake)


def _patch_runtime(activity_mock: AsyncMock, child_mock: AsyncMock):
    info_stub = type(
        "WfInfo", (), {"workflow_id": "automation-jira-EPIC-1", "run_id": "abcd1234ef"}
    )()
    return [
        patch.object(_temporal_workflow, "execute_activity", activity_mock),
        patch.object(_temporal_workflow, "info", lambda: info_stub),
        patch.object(
            _temporal_workflow, "execute_child_workflow", child_mock
        ),
    ]


def _comment_bodies(activity_mock: AsyncMock) -> list[str]:
    bodies: list[str] = []
    for call in activity_mock.call_args_list:
        if call.args and call.args[0] == "jira_add_comment":
            args = (
                list(call.kwargs["args"])
                if "args" in call.kwargs
                else list(call.args[1] if len(call.args) >= 2 else [])
            )
            if len(args) >= 2:
                bodies.append(str(args[1]))
    return bodies


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


class TestMultiStepHappyPath:
    def test_fans_out_to_each_child_and_completes(self, make_wf) -> None:
        wf = make_wf()
        inp = _make_input()
        children = [
            {"key": "SUB-1", "summary": "first"},
            {"key": "SUB-2", "summary": "second"},
        ]
        activity_mock = _activity_dispatcher(children)
        child_mock = AsyncMock(return_value={"decision": "dispatched"})

        async def _run() -> None:
            ctxs = _patch_runtime(activity_mock, child_mock)
            for c in ctxs:
                c.__enter__()
            try:
                await wf._handle_multi_step(inp)
            finally:
                for c in reversed(ctxs):
                    c.__exit__(None, None, None)

        _drive(_run)

        # One child workflow started per subtask.
        assert child_mock.await_count == 2
        # Each child input inherits the parent capability envelope.
        first_child_input = child_mock.await_args_list[0].kwargs["args"][0]
        assert tuple(first_child_input.available_capabilities) == (
            "jira",
            "bitbucket",
        )
        assert first_child_input.issue_key == "SUB-1"
        # Progress + completion comments posted to the Epic.
        bodies = _comment_bodies(activity_mock)
        assert any("1/2" in b for b in bodies)
        assert any("2/2" in b for b in bodies)
        assert any("Epic tamamland" in b for b in bodies)
        assert wf._failure_reason is None


# ---------------------------------------------------------------------------
# 2. Empty Epic
# ---------------------------------------------------------------------------


class TestMultiStepEmptyEpic:
    def test_no_children_posts_guidance_and_does_not_dispatch(
        self, make_wf
    ) -> None:
        wf = make_wf()
        inp = _make_input()
        activity_mock = _activity_dispatcher([])
        child_mock = AsyncMock()

        async def _run() -> None:
            ctxs = _patch_runtime(activity_mock, child_mock)
            for c in ctxs:
                c.__enter__()
            try:
                await wf._handle_multi_step(inp)
            finally:
                for c in reversed(ctxs):
                    c.__exit__(None, None, None)

        _drive(_run)

        assert child_mock.await_count == 0
        assert wf._failure_reason == "epic_no_subtasks"
        bodies = _comment_bodies(activity_mock)
        assert any("subtask bulunamad" in b for b in bodies)


# ---------------------------------------------------------------------------
# 3. Subtask failure stops the fan-out
# ---------------------------------------------------------------------------


class TestMultiStepSubtaskFailure:
    def test_denied_child_stops_and_skips_remaining(self, make_wf) -> None:
        wf = make_wf()
        inp = _make_input()
        children = [
            {"key": "SUB-1", "summary": "first"},
            {"key": "SUB-2", "summary": "second"},
        ]
        activity_mock = _activity_dispatcher(children)
        # First child is denied by the gateway → Epic must stop.
        child_mock = AsyncMock(return_value={"decision": "denied"})

        async def _run() -> None:
            ctxs = _patch_runtime(activity_mock, child_mock)
            for c in ctxs:
                c.__enter__()
            try:
                await wf._handle_multi_step(inp)
            finally:
                for c in reversed(ctxs):
                    c.__exit__(None, None, None)

        with pytest.raises(_EpicSubtaskFailed):
            _drive(_run)

        # Only the first child was attempted; the second is skipped.
        assert child_mock.await_count == 1
        assert wf._failure_reason == "epic_subtask_failed"
        bodies = _comment_bodies(activity_mock)
        assert any("durduruldu" in b for b in bodies)
