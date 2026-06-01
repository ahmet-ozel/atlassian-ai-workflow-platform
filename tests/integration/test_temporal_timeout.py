"""Integration test: AutomationWorkflow ``needs_info`` 7-day timeout.

**Validates: Requirements 5.5, 5.7, 11.5**

Scenario
--------

When the LLM returns ``confidence="low"`` with a non-empty
``needs_info_question`` and no ``new_comment`` signal arrives within
``NEEDS_INFO_TIMEOUT`` (7 days), :class:`AutomationWorkflow` posts the
canonical Turkish timeout comment and terminates with
``status="failed"`` and ``failure_reason="needs_info_timeout"``.

This test pins that branch using
``WorkflowEnvironment.start_time_skipping()``, which advances the test
server's virtual clock without sleeping in real wall-clock time. We
start the workflow, wait until the needs_info question is posted (so
the wait condition has been entered), then advance virtual time by 8
days and assert the workflow completes with the expected terminal
state.

Activities are mocked so the test is hermetic — no real Atlassian /
Postgres / LLM provider is contacted.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from ._temporal_helpers import (
    CallLog,
    ensure_worker_on_sys_path,
    make_default_activities,
    make_stub_agent_runner_workflow,
    make_task_analysis,
)

ensure_worker_on_sys_path()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_needs_info_timeout_after_seven_days_marks_workflow_failed() -> None:
    """**Validates: Requirements 5.5, 5.7, 11.5**

    The workflow parks on a low-confidence analysis; advancing virtual
    time past 7 days fires the ``wait_condition`` timeout, posts the
    Turkish timeout comment, and terminates with
    ``failure_reason="needs_info_timeout"``.
    """

    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from src.workflows.automation_workflow import (
        AutomationInput,
        AutomationResult,
        AutomationWorkflow,
    )

    log = CallLog()

    @activity.defn(name="llm_analyze_task")
    async def _llm_analyze_task_low_confidence(
        _issue: Any, _ctx: Any
    ) -> Any:
        log.record("llm_analyze_task")
        return make_task_analysis(
            workflow_type="code_change_with_test",
            confidence="low",
            needs_info_question=(
                "Hangi repo branch'inde değişiklik yapılmalı?"
            ),
        )

    activities = [
        *make_default_activities(log=log),
        _llm_analyze_task_low_confidence,
    ]
    StubAgentRunnerWorkflow = make_stub_agent_runner_workflow()

    workflow_id = "automation-jira-PAY-4230"
    task_queue = "agent-runner-timeout"

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AutomationWorkflow, StubAgentRunnerWorkflow],
            activities=activities,
        ):
            inp = AutomationInput(
                issue_key="PAY-4230",
                department_id="payments",
                available_capabilities=("jira", "bitbucket", "execution"),
                available_repos=("payment-service",),
                iteration=1,
            )
            handle = await env.client.start_workflow(
                AutomationWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
            )

            # Wait (in virtual time) until the workflow has reached the
            # needs_info wait state. Without this guard the time-skip
            # below could race the LLM activity completion.
            for _ in range(50):
                pending = await handle.query("get_pending_question")
                if pending:
                    break
                await env.sleep(0.1)
            else:  # pragma: no cover - watchdog
                pytest.fail(
                    "workflow never reached the needs_info wait state"
                )

            # Advance virtual time past the 7-day timeout. The
            # time-skipping server fires the wait_condition timer,
            # which surfaces inside the workflow as a TimeoutError that
            # the body catches in ``_resolve_analysis``.
            await env.sleep(timedelta(days=8))

            result_raw: Any = await handle.result()
            if isinstance(result_raw, AutomationResult):
                result = {
                    "status": result_raw.status,
                    "workflow_type": result_raw.workflow_type,
                    "failure_reason": result_raw.failure_reason,
                    "summary": result_raw.summary,
                }
            else:
                assert isinstance(result_raw, dict), (
                    f"unexpected result shape: {type(result_raw).__name__}"
                )
                result = {
                    "status": result_raw.get("status"),
                    "workflow_type": result_raw.get("workflow_type"),
                    "failure_reason": result_raw.get("failure_reason"),
                    "summary": result_raw.get("summary", ""),
                }

    # ----- Assertions -------------------------------------------------

    assert result["status"] == "failed"
    assert result["failure_reason"] == "needs_info_timeout"
    assert result["workflow_type"] == "code_change_with_test"

    # The Turkish timeout comment must have been posted to Jira before
    # the work item was marked failed.
    comments_posted = [
        args[1] for args in log.args_for("jira_add_comment")
    ]
    assert any(
        "7 gün" in body and "yanıt alınmadı" in body
        for body in comments_posted
    ), f"timeout comment never posted; got {comments_posted!r}"

    # The work item state machine transitioned through running → failed.
    statuses = [args[1] for args in log.args_for("update_work_item_status")]
    assert "running" in statuses
    assert statuses[-1] == "failed", (
        f"expected terminal status 'failed', got {statuses!r}"
    )

    # LLM was called exactly once (no signal arrived → no re-analysis).
    assert log.count("llm_analyze_task") == 1
