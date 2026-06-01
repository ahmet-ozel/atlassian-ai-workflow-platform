"""Quick debug script to trace AgentRunnerWorkflow under time-skipping."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_THIS = Path(__file__).resolve()
_PLATFORM = _THIS.parent
sys.path.insert(0, str(_PLATFORM / "workers" / "agent-runner-worker" / "src"))
sys.path.insert(0, str(_PLATFORM / "libs" / "temporal-shared" / "src"))
sys.path.insert(0, str(_PLATFORM / "libs" / "mcp_client" / "src"))

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from agent_runner.workflows.agent_runner_workflow import (
    AgentRunnerWorkflow,
    CommentAddedSignal,
    MAX_ITER,
)
from temporal_shared.messages import (
    AgentRunnerWorkflowInput,
    LlmAnalysisResult,
)


@activity.defn(name="audit_emit")
async def _audit_emit(payload: dict) -> None:
    print(f"  [activity] audit_emit: {payload}")
    return None


@activity.defn(name="jira_add_comment")
async def _jira_add_comment(issue_key: str, body: str, dept_id: str) -> None:
    print(f"  [activity] jira_add_comment({issue_key}, {body[:30]}..., {dept_id})")
    return None


async def main() -> None:
    analysis = LlmAnalysisResult(
        workflow_type="noop_test",
        confidence="high",
        title="Cap test",
        rationale="iter-cap integration test",
    )
    inp = AgentRunnerWorkflowInput(
        parent_workflow_id="automation-jira-PAY-5001",
        issue_key="PAY-5001",
        department_id="payments",
        workflow_type="noop_test",
        analysis=analysis,
        iteration=1,
        max_iter=MAX_ITER,
    )

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="debug-queue",
            workflows=[AgentRunnerWorkflow],
            activities=[_audit_emit, _jira_add_comment],
        ):
            print("Starting workflow with start_signal...")
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id="agent-runner-debug-1",
                task_queue="debug-queue",
                start_signal="comment_added",
                start_signal_args=[
                    CommentAddedSignal(
                        comment_text="lütfen yine de devam et",
                        actor_account_id="user-1",
                    )
                ],
            )
            print("Awaiting result...")
            result = await handle.result()
            print(f"Result type: {type(result).__name__}")
            print(f"Result repr: {result!r}")


asyncio.run(main())
