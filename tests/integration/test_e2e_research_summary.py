"""End-to-end integration test for the ``research_summary_jira`` flow.


This test exercises the parent → child workflow boundary using
``WorkflowEnvironment.start_time_skipping`` and Temporal's deterministic
test server. The flow under test:

 AutomationWorkflow (jira_get_issue → llm_analyze_task[research_summary_jira])
 → execute_child_workflow("AgentRunnerWorkflow", ...)
 → AgentRunnerWorkflow (research branch)
 → llm_research (with optional firecrawl web search)
 → jira_add_comment (research summary on the issue)
 → completion comment + Done transition

Test scope decision (option (a) - minimal AgentRunnerWorkflow stub)
-------------------------------------------------------------------

The production ``AgentRunnerWorkflow`` is currently an empty stub
(see ``src/workflows/agent_runner_workflow.py``). This test registers
a *test-local* class under the same Temporal name
(``"AgentRunnerWorkflow"``); the stub lives in ``_e2e_workflow_stubs.py``
and implements only the ``research_summary_jira`` branch.

When the production workflow body lands (spec the implementation), the test will
transparently switch over - both register under the same name and the
activity-call contract is identical.

Activities are mocked: no LLM provider, no Firecrawl, no Atlassian MCP
or DB are contacted. The ``capabilities`` for the test department
include ``web_search`` so the parent's Phase 2 capability gate passes.

Test isolation
--------------

Both worker packages publish themselves under the ``src.`` namespace, so
naive ``sys.path`` manipulation across tests would leak the wrong
``src.*`` subtree into other test files. The ``isolate_worker(...)``
context manager from ``_worker_path`` snapshots ``sys.path`` /
``sys.modules`` on entry and restores them on exit, keeping this test
hermetic regardless of run order.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.integration._worker_path import isolate_worker
from tests.integration._e2e_workflow_stubs import (
    ResearchSummaryJiraAgentRunnerStub,
)


# ---------------------------------------------------------------------------
# Activity call recorder
# ---------------------------------------------------------------------------


class _ActivityCallLog:
    """Records every activity invocation across the test for assertions."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_research_summary_jira_e2e_flow() -> None:
    """End-to-end: research_summary_jira branch posts an LLM summary on Jira.

 Drives parent + child workflow against the time-skipping test server
 with mocked activities. Verifies the LLM research result is delivered
 as a Jira comment on the source issue and the workflow completes
 cleanly.
 """

    log = _ActivityCallLog()

    summary_text = (
        "Webhook security summary:\n"
        "- HMAC-SHA256 signatures\n"
        "- replay window with monotonic nonce\n"
        "- optional IP allowlist."
    )

    with isolate_worker("agent-runner"):
        from src.prompts.parser import OutputAction, TaskAnalysis
        from src.workflows.automation_workflow import (
            AutomationInput,
            AutomationWorkflow,
        )

        # Bind the dataclasses onto the module globals so
        # ``temporalio.activity.defn``'s ``get_type_hints`` call can
        # resolve the annotations on the inner activity callables.
        globals()["OutputAction"] = OutputAction
        globals()["TaskAnalysis"] = TaskAnalysis

        # ----- Parent (AutomationWorkflow) activity mocks --------------

        @activity.defn(name="jira_add_comment")
        async def jira_add_comment(
            issue_key: str, body: str, dept_id: str
        ) -> None:
            log.calls.append(("jira_add_comment", (issue_key, body, dept_id)))

        @activity.defn(name="jira_transition_issue")
        async def jira_transition_issue(
            issue_key: str, target_status: str, dept_id: str
        ) -> None:
            log.calls.append(
                ("jira_transition_issue", (issue_key, target_status, dept_id))
            )

        @activity.defn(name="update_work_item_status")
        async def update_work_item_status(
            workflow_id: str, new_status: str
        ) -> None:
            log.calls.append(
                ("update_work_item_status", (workflow_id, new_status))
            )

        @activity.defn(name="jira_get_issue")
        async def jira_get_issue(
            issue_key: str, dept_id: str
        ) -> dict[str, Any]:
            log.calls.append(("jira_get_issue", (issue_key, dept_id)))
            return {
                "key": issue_key,
                "summary": "Research the latest webhook security best practices",
                "description": (
                    "We need a 1-page summary of current best practices for "
                    "verifying inbound webhooks (HMAC, replay, IP allowlists)."
                ),
                "issue_type": "Research",
                "status": "To Do",
                "assignee_account_id": None,
                "project_key": issue_key.split("-", 1)[0],
                "labels": ["research"],
                "priority": None,
            }

        @activity.defn(name="llm_analyze_task")
        async def llm_analyze_task(issue: Any, ctx: Any) -> TaskAnalysis:
            log.calls.append(("llm_analyze_task", ()))
            return TaskAnalysis(
                workflow_type="research_summary_jira",
                target_repo=None,
                target_branch=None,
                output_actions=(
                    OutputAction(
                        type="jira_comment",
                        payload={"summary_target": "research"},
                    ),
                ),
                confidence="high",
                needs_info_question=None,
            )

        # ----- Child (AgentRunnerWorkflow stub) activity mocks ---------
        #
        # ``llm_research`` returns a ResearchData-shaped dict - the child
        # stub only needs the ``summary`` field for the Jira comment.

        @activity.defn(name="llm_research")
        async def llm_research(
            query: str, dept_id: str, web_search_enabled: bool
        ) -> dict[str, Any]:
            log.calls.append(
                ("llm_research", (query, dept_id, web_search_enabled))
            )
            return {
                "summary": summary_text,
                "sources": [
                    {
                        "url": "https://example.com/webhook-best-practices",
                        "title": "Best practices",
                    },
                ],
                "raw_content": summary_text,
                "web_search_used": web_search_enabled,
            }

        # ``jira_add_comment`` is shared between parent (ack/completion)
        # and child (research summary). The single mock above records
        # every call; we'll filter by content in assertions.

        # ----- Drive the workflow --------------------------------------

        async with await WorkflowEnvironment.start_time_skipping() as env:
            task_queue = f"agent-runner-research-{uuid.uuid4().hex[:8]}"

            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[AutomationWorkflow, ResearchSummaryJiraAgentRunnerStub],
                activities=[
                    jira_add_comment,
                    jira_transition_issue,
                    update_work_item_status,
                    jira_get_issue,
                    llm_analyze_task,
                    llm_research,
                ],
            ):
                handle = await env.client.start_workflow(
                    AutomationWorkflow.run,
                    AutomationInput(
                        issue_key="RES-77",
                        department_id="security",
                        # research_summary_jira requires {jira, web_search}.
                        available_capabilities=("jira", "web_search"),
                        iteration=1,
                    ),
                    id="automation-jira-RES-77",
                    task_queue=task_queue,
                )

                result = await handle.result()

    # ----- Assertions --------------------------------------------------

    assert result.status == "completed", (
        f"expected completed status, got {result.status!r} "
        f"(reason: {result.failure_reason!r}, summary: {result.summary!r})"
    )
    assert result.workflow_type == "research_summary_jira"
    assert result.failure_reason is None
    assert result.child_workflow_id == "agent-automation-jira-RES-77-iter-1"

    names = log.names()

    # Parent: ack comment first, then state transitions, fetch, analysis.
    assert names[0] == "jira_add_comment", names
    assert (
        "update_work_item_status",
        ("automation-jira-RES-77", "running"),
    ) in log.calls
    assert "jira_get_issue" in names
    assert "llm_analyze_task" in names

    # Child: llm_research ran exactly once with web_search_enabled=True.
    research_calls = [args for n, args in log.calls if n == "llm_research"]
    assert len(research_calls) == 1, f"expected 1 llm_research call: {research_calls}"
    assert research_calls[0][1] == "security"
    assert research_calls[0][2] is True

    # Child: research summary posted to RES-77 (separate from parent's
    # ack/completion comments).
    summary_comments = [
        args
        for n, args in log.calls
        if n == "jira_add_comment"
        and args[0] == "RES-77"
        and summary_text in args[1]
    ]
    assert summary_comments, (
        "expected at least one jira_add_comment on RES-77 carrying the "
        f"research summary; got jira_add_comments: "
        f"{[args for n, args in log.calls if n == 'jira_add_comment']!r}"
    )

    # Parent: completion comment + Done transition + work_item completed.
    assert (
        "jira_transition_issue",
        ("RES-77", "Done", "security"),
    ) in log.calls
    assert (
        "update_work_item_status",
        ("automation-jira-RES-77", "completed"),
    ) in log.calls

    completion_comments = [
        body
        for n, args in log.calls
        if n == "jira_add_comment" and args[0] == "RES-77"
        for body in (args[1],)
    ]
    assert any(
        "Tamamlandı" in c and "research_summary_jira" in c
        for c in completion_comments
    ), f"no completion comment found: {completion_comments!r}"
