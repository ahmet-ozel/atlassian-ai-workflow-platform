"""End-to-end integration test for the ``confluence_doc_update`` flow.

**Validates: Requirements 6.4, 11.1, 11.3**

This test exercises the parent → child workflow boundary using
``WorkflowEnvironment.start_time_skipping()`` and Temporal's deterministic
test server. The flow under test:

    AutomationWorkflow (jira_get_issue → llm_analyze_task[confluence_doc_update])
        → execute_child_workflow("AgentRunnerWorkflow", ...)
        → AgentRunnerWorkflow (confluence branch)
            → confluence_search
            → confluence_get_page
            → llm_generate_doc
            → confluence_update_page
        → completion comment + Done transition

Test scope decision (option (a) — minimal AgentRunnerWorkflow stub)
-------------------------------------------------------------------

The production ``AgentRunnerWorkflow`` class
(``platform/workers/agent-runner-worker/src/workflows/agent_runner_workflow.py``)
is currently an empty stub with no ``@workflow.defn`` decorator. The
orchestrating ``AutomationWorkflow`` dispatches the child by name
(``execute_child_workflow("AgentRunnerWorkflow", ...)``), so we can
supply a *test-local* implementation that registers under the same
name.

The test-local stub lives in
``tests/integration/_e2e_workflow_stubs.py`` (a side-effect-free module
so the Temporal sandbox can validate it). It implements only the
``confluence_doc_update`` branch needed for this scenario. When the
production workflow body lands in spec task 10.1, the test will
transparently start exercising the production class instead — both
register under ``name="AgentRunnerWorkflow"`` and the activity-call
contract is identical.

Activities are mocked: all I/O (Atlassian MCP, LLM provider, work_item
DB) happens inside ``@activity.defn`` callables registered with the
Worker. No external services are required.

Test isolation
--------------

Both worker packages publish themselves under the ``src.`` namespace, so
naive ``sys.path`` manipulation across tests would leak the wrong
``src.*`` subtree into other test files. The
``isolate_worker(...)`` context manager from ``_worker_path`` snapshots
``sys.path``/``sys.modules`` on entry and restores them on exit, keeping
this test hermetic regardless of run order.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.integration._worker_path import isolate_worker

# The test-local AgentRunnerWorkflow stub MUST be importable at module
# collection time. ``_e2e_workflow_stubs`` performs no ``src.*`` imports
# and is sandbox-safe.
from tests.integration._e2e_workflow_stubs import (
    ConfluenceDocUpdateAgentRunnerStub,
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
async def test_confluence_doc_update_e2e_flow() -> None:
    """End-to-end: webhook payload → AutomationWorkflow → AgentRunnerWorkflow.

    Drives the parent + child workflow against the time-skipping test
    server with mocked activities. Asserts the full activity sequence
    runs in the expected order and that the workflow completes with
    ``status="completed"`` and ``workflow_type="confluence_doc_update"``.
    """

    log = _ActivityCallLog()

    # ``isolate_worker`` puts agent-runner-worker on sys.path, evicts
    # any cached ``src.*`` modules from a prior test, and restores the
    # original snapshot on exit so subsequent tests are unaffected.
    with isolate_worker("agent-runner"):
        # Imports inside the block resolve to the agent-runner-worker
        # tree. ``TaskAnalysis`` and ``OutputAction`` are the dataclasses
        # AutomationWorkflow expects from ``llm_analyze_task`` — see
        # automation_workflow._resolve_analysis. Bind them onto the
        # module globals so ``temporalio.activity.defn``'s
        # ``get_type_hints`` call can resolve the annotations.
        from src.prompts.parser import OutputAction, TaskAnalysis
        from src.workflows.automation_workflow import (
            AutomationInput,
            AutomationWorkflow,
        )

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
                "summary": "Update API guidelines documentation",
                "description": (
                    "The API guidelines page in the DOCS space needs an "
                    "updated section on the new auth flow."
                ),
                "issue_type": "Task",
                "status": "To Do",
                "assignee_account_id": None,
                "project_key": issue_key.split("-", 1)[0],
                "labels": [],
                "priority": None,
            }

        # The workflow accesses ``analysis.workflow_type`` etc. as
        # attributes, so the mock returns an actual ``TaskAnalysis``
        # dataclass — not a dict — so the data converter delivers
        # attribute access on the workflow side.
        @activity.defn(name="llm_analyze_task")
        async def llm_analyze_task(issue: Any, ctx: Any) -> TaskAnalysis:
            log.calls.append(("llm_analyze_task", ()))
            return TaskAnalysis(
                workflow_type="confluence_doc_update",
                target_repo=None,
                target_branch=None,
                output_actions=(
                    OutputAction(
                        type="confluence_page",
                        payload={"space": "DOCS", "title": "API guidelines"},
                    ),
                ),
                confidence="high",
                needs_info_question=None,
            )

        # ----- Child (AgentRunnerWorkflow stub) activity mocks ---------

        @activity.defn(name="confluence_search")
        async def confluence_search(
            space_key: str, query: str, dept_id: str
        ) -> list[dict[str, Any]]:
            log.calls.append(("confluence_search", (space_key, query, dept_id)))
            return [{"id": "page-42", "title": "API guidelines"}]

        @activity.defn(name="confluence_get_page")
        async def confluence_get_page(
            page_id: str, dept_id: str
        ) -> dict[str, Any]:
            log.calls.append(("confluence_get_page", (page_id, dept_id)))
            return {
                "id": page_id,
                "title": "API guidelines",
                "body": "<p>Old content</p>",
                "version": 7,
            }

        @activity.defn(name="llm_generate_doc")
        async def llm_generate_doc(
            plan: dict[str, Any], research: Any
        ) -> dict[str, Any]:
            log.calls.append(("llm_generate_doc", ()))
            return {
                "title": plan["title"],
                "body": "<p>Updated content per latest auth flow.</p>",
                "summary": "Refreshed auth section",
            }

        @activity.defn(name="confluence_update_page")
        async def confluence_update_page(
            page_id: str, title: str, body: str, dept_id: str
        ) -> None:
            log.calls.append(
                ("confluence_update_page", (page_id, title, body, dept_id))
            )

        # ----- Drive the workflow --------------------------------------

        async with await WorkflowEnvironment.start_time_skipping() as env:
            task_queue = f"agent-runner-confluence-{uuid.uuid4().hex[:8]}"

            async with Worker(
                env.client,
                task_queue=task_queue,
                workflows=[AutomationWorkflow, ConfluenceDocUpdateAgentRunnerStub],
                activities=[
                    jira_add_comment,
                    jira_transition_issue,
                    update_work_item_status,
                    jira_get_issue,
                    llm_analyze_task,
                    confluence_search,
                    confluence_get_page,
                    llm_generate_doc,
                    confluence_update_page,
                ],
            ):
                handle = await env.client.start_workflow(
                    AutomationWorkflow.run,
                    AutomationInput(
                        issue_key="DOC-101",
                        department_id="engineering",
                        available_capabilities=("jira", "confluence"),
                        available_spaces=("DOCS",),
                        iteration=1,
                    ),
                    id="automation-jira-DOC-101",
                    task_queue=task_queue,
                )

                result = await handle.result()

    # ----- Assertions (outside the isolate_worker block, no src.* needed) -

    # Workflow terminated successfully.
    assert result.status == "completed", (
        f"expected completed status, got {result.status!r} "
        f"(reason: {result.failure_reason!r}, summary: {result.summary!r})"
    )
    assert result.workflow_type == "confluence_doc_update"
    assert result.failure_reason is None
    # Child workflow ID follows the agent_workflow_id(parent, 1) format.
    assert result.child_workflow_id == "agent-automation-jira-DOC-101-iter-1"

    names = log.names()

    # Parent: ack comment first, then state transitions and analysis.
    assert names[0] == "jira_add_comment", names
    assert (
        "update_work_item_status",
        ("automation-jira-DOC-101", "running"),
    ) in log.calls
    assert "jira_get_issue" in names
    assert "llm_analyze_task" in names

    # Child confluence branch ran in the expected order.
    confluence_seq = [
        n
        for n in names
        if n
        in {
            "confluence_search",
            "confluence_get_page",
            "llm_generate_doc",
            "confluence_update_page",
        }
    ]
    assert confluence_seq == [
        "confluence_search",
        "confluence_get_page",
        "llm_generate_doc",
        "confluence_update_page",
    ], f"unexpected confluence call order: {confluence_seq}"

    # Parent: completion comment + Done transition + work_item completed.
    assert (
        "jira_transition_issue",
        ("DOC-101", "Done", "engineering"),
    ) in log.calls
    assert (
        "update_work_item_status",
        ("automation-jira-DOC-101", "completed"),
    ) in log.calls

    # The completion comment carries the workflow_type marker.
    completion_comments = [
        body
        for name, args in log.calls
        if name == "jira_add_comment" and args[0] == "DOC-101"
        for body in (args[1],)
    ]
    assert any(
        "Tamamlandı" in c and "confluence_doc_update" in c
        for c in completion_comments
    ), f"no completion comment found in: {completion_comments!r}"
