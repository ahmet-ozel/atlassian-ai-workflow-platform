"""End-to-end integration test for the ``code_change_with_test`` flow.

**Validates: Requirements 11.1, 11.3, 11.5, 6.3, 6.7, 6.10**

Property under test (`.kiro/specs/p0-critical-path/tasks.md` task 15.1)::

    webhook payload
        → AutomationWorkflow
            → (LLM analysis returns code_change_with_test)
            → AgentRunnerWorkflow child
                → branch + commit + draft PR
        → completion comment + Done transition + work_items.status='completed'

Reality check
-------------

``src.workflows.agent_runner_workflow.AgentRunnerWorkflow`` is currently a
stub (no ``@workflow.defn`` body — see the dedicated AgentRunner task in
``.kiro/specs/p0-critical-path/tasks.md``). This test therefore registers
a *test double* AgentRunnerWorkflow that returns a canned success result
matching the shape ``AutomationWorkflow`` consumes (a ``summary`` field
plus a ``draft`` flag mirroring MIMARI §1 Kural 10 — PRs are always
opened as drafts).

The double captures:

* the ``_AgentRunnerInputShape`` it received (so the test asserts the
  parent populated branch / repo / output_actions correctly), and
* the ``draft=True`` invariant on the bitbucket_pr output_action, which
  is enforced statically by the ``parse_task_analysis`` parser
  (`platform/workers/agent-runner-worker/src/prompts/parser.py`,
  function ``_coerce_draft_true``).

Test environment
----------------

The ``temporalio.testing.WorkflowEnvironment.start_time_skipping()``
test server is used so the suite is hermetic — no external Temporal
cluster, no Docker. Activities are mocked in-process with the names
``AutomationWorkflow`` invokes (``jira_add_comment``, ``jira_get_issue``,
``llm_analyze_task``, ``jira_transition_issue``, ``update_work_item_status``).

The ``update_work_item_status`` mock keeps an in-memory state machine
that funnels through :func:`validate_work_item_transition`, ensuring the
pending → running → completed path passes the same validator the real
activity uses (Property 9). MinIO upload assertions are **out of scope
for this test**: artifact upload happens inside the AgentRunnerWorkflow
child, which is mocked here. A separate task covers the AgentRunner
body and its real S3/MinIO interactions; running that test against real
MinIO from Compose is gated behind the ``--run-docker`` pytest CLI
flag the workspace already exposes.

OpenCode and Atlassian MCP are mocked at the activity boundary (rather
than at the HTTP / fixture level) because ``AutomationWorkflow`` itself
never speaks HTTP — it dispatches the `AgentRunnerWorkflow` child which
owns those clients. Mocking at the activity boundary keeps the test
focused on workflow orchestration and avoids re-testing transport
plumbing that is covered by the per-activity unit tests.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Workspace anchors and sys.path bootstrapping
# ---------------------------------------------------------------------------
#
# Mirrors the path bootstrap in
# ``tests/property/test_workflow_determinism_replay.py``: the worker ships
# its source under ``platform/workers/agent-runner-worker/src/`` and its
# modules import each other under the ``src.`` namespace. Adding the
# *worker root* (not its ``src/`` child) onto ``sys.path`` makes
# ``from src.workflows.automation_workflow import ...`` resolve to that
# worker's source tree.

_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]
_AGENT_RUNNER_WORKER: Path = _PLATFORM_ROOT / "workers" / "agent-runner-worker"

for _candidate in (_AGENT_RUNNER_WORKER,):
    _candidate_str = str(_candidate)
    if _candidate.is_dir() and _candidate_str not in sys.path:
        sys.path.insert(0, _candidate_str)


# ---------------------------------------------------------------------------
# Test fixtures: mocked activities + double AgentRunnerWorkflow
# ---------------------------------------------------------------------------


@dataclass
class _RecordedState:
    """Mutable bag the activity mocks write into so the test can assert.

    Using a dataclass (not a dict) gives us attribute-level autocomplete
    in IDEs and makes the assertion section read-as-prose.
    """

    # Jira side effects
    comments: list[str] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)

    # work_items state-machine tape
    work_item_status_history: list[tuple[str, str]] = field(
        default_factory=list
    )


# ---------------------------------------------------------------------------
# AgentRunnerWorkflow double — imported from a sandbox-safe sibling module
# ---------------------------------------------------------------------------
#
# ``temporalio`` re-imports any module containing a ``@workflow.defn``
# class under its sandbox to validate the workflow body. That sandbox
# bans calls like ``Path(__file__).resolve()`` — which this test module
# performs at import time for the sys.path bootstrap. Keeping the
# workflow double in a separate, minimal module
# (``_e2e_doubles.py``) lets the test do its own setup at import time
# without falling foul of sandbox restrictions.
from tests.integration._e2e_doubles import (  # noqa: E402
    AgentRunnerWorkflowDouble,
    LAST_RUN as _LAST_RUN,
    e2e_record_child_input,
)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_code_change_with_test_e2e_flow_completes() -> None:
    """**Validates: Requirements 11.1, 11.3, 11.5, 6.3, 6.7, 6.10**

    Drive the AutomationWorkflow with a webhook-style ``AutomationInput``
    that the (mocked) LLM analyses as ``code_change_with_test``. The
    AgentRunnerWorkflow child is mocked at the workflow level so the
    test focuses on the orchestration contract:

    1. work_items.status reaches ``"completed"``.
    2. The bitbucket_pr output_action carries ``draft=True`` (PR draft
       invariant — MIMARI §1 Kural 10) by the time it reaches the child.
    3. A completion comment is posted to Jira and the issue transitions
       to ``Done``.
    """

    # Local imports keep import-time side effects (Temporal sandbox
    # initialisation) out of pytest's collection phase.
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from src.activities.work_item import (
        validate_work_item_transition,
    )
    from src.prompts.parser import parse_task_analysis
    from src.workflows.automation_workflow import (
        AutomationInput,
        AutomationResult,
        AutomationWorkflow,
    )

    state = _RecordedState()

    # --- Activity mocks -----------------------------------------------------

    @activity.defn(name="jira_add_comment")
    async def jira_add_comment(
        issue_key: str, body: str, dept_id: str
    ) -> None:
        state.comments.append(body)

    @activity.defn(name="jira_get_issue")
    async def jira_get_issue(
        issue_key: str, dept_id: str
    ) -> dict[str, Any]:
        return {
            "key": issue_key,
            "summary": "Add /healthz endpoint with smoke test",
            "description": (
                "The payment-service needs a `/healthz` endpoint and a "
                "smoke test to verify it responds 200 on boot."
            ),
            "issue_type": "Story",
            "status": "To Do",
            "assignee_account_id": None,
            "project_key": issue_key.split("-", 1)[0],
            "labels": [],
            "priority": None,
        }

    @activity.defn(name="llm_analyze_task")
    async def llm_analyze_task(issue: Any, ctx: Any) -> Any:
        # Build a TaskAnalysis through the canonical parser so every
        # validation invariant the real LLM output respects (including
        # ``draft=True`` coercion on bitbucket_pr actions) is honoured
        # here too. ``parse_task_analysis`` lives outside the workflow
        # sandbox; activities are free to import non-deterministic code.
        return parse_task_analysis(
            {
                "workflow_type": "code_change_with_test",
                "target_repo": "payment-service",
                "target_branch": "develop",
                "confidence": "high",
                "needs_info_question": None,
                "output_actions": [
                    {
                        "type": "bitbucket_pr",
                        "payload": {
                            # NB: ``draft`` is intentionally False here
                            # to verify the parser coerces it to True
                            # (MIMARI §1 Kural 10). The assertion
                            # downstream reads it from the child input.
                            "draft": False,
                            "title": "Add /healthz endpoint",
                            "branch": "ai/PAY-4211/iter-1",
                        },
                    },
                    {
                        "type": "jira_comment",
                        "payload": {"body": "PR opened in draft."},
                    },
                ],
            }
        )

    @activity.defn(name="jira_transition_issue")
    async def jira_transition_issue(
        issue_key: str, target_status: str, dept_id: str
    ) -> None:
        state.transitions.append(target_status)

    # In-memory state machine for work_items. Funnels through the same
    # pure validator the real activity uses (Property 9) so the test
    # exercises the canonical edge set, not a parallel definition.
    @activity.defn(name="update_work_item_status")
    async def update_work_item_status(
        workflow_id: str, new_status: str
    ) -> None:
        prev = (
            state.work_item_status_history[-1][1]
            if state.work_item_status_history
            else "pending"
        )
        validate_work_item_transition(prev, new_status)
        state.work_item_status_history.append((prev, new_status))

    # --- AgentRunnerWorkflow double ----------------------------------------
    #
    # ``_AgentRunnerWorkflowDouble`` is defined at module scope (Temporal
    # rejects ``@workflow.run`` on local classes) and writes onto the
    # ``_LAST_RUN`` sink. Reset that sink at entry to keep the test
    # hermetic if the module is ever loaded twice (e.g. by pytest's
    # ``--lf`` rerun).
    _LAST_RUN["input"] = None
    _LAST_RUN["invocations"] = 0

    # --- Run the workflow against the time-skipping test server ------------

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="agent-runner-e2e",
            workflows=[AutomationWorkflow, AgentRunnerWorkflowDouble],
            activities=[
                jira_add_comment,
                jira_get_issue,
                llm_analyze_task,
                jira_transition_issue,
                update_work_item_status,
                e2e_record_child_input,
            ],
        ):
            handle = await env.client.start_workflow(
                AutomationWorkflow.run,
                AutomationInput(
                    issue_key="PAY-4211",
                    department_id="payments",
                    available_capabilities=("jira", "bitbucket", "execution"),
                    available_repos=("payment-service",),
                    iteration=1,
                ),
                id="automation-jira-PAY-4211",
                task_queue="agent-runner-e2e",
            )
            result: AutomationResult = await handle.result()

    # --- Assertions --------------------------------------------------------

    # 1. Terminal status is "completed" (Requirement 11.5 — work_items
    #    state machine reaches the success terminal state).
    assert result.status == "completed", (
        f"AutomationWorkflow did not reach completed: {result!r}"
    )
    assert result.workflow_type == "code_change_with_test"
    assert result.failure_reason is None
    assert result.child_workflow_id == "agent-automation-jira-PAY-4211-iter-1"

    # 2. The work_items state machine traversed pending → running → completed.
    statuses = [edge[1] for edge in state.work_item_status_history]
    assert statuses == ["running", "completed"], (
        f"unexpected work_items.status path: {statuses!r}"
    )

    # 3. The AgentRunnerWorkflow child was invoked exactly once with the
    #    expected shape (target_repo, target_branch, output_actions
    #    populated and the bitbucket_pr action's draft flag coerced to
    #    True by parse_task_analysis — Requirement 6.10 / MIMARI §1
    #    Kural 10).
    assert _LAST_RUN["invocations"] == 1, (
        f"AgentRunnerWorkflow invoked {_LAST_RUN['invocations']} times; "
        "expected exactly once"
    )
    child_input = _LAST_RUN["input"]
    assert child_input is not None
    assert _attr(child_input, "issue_key") == "PAY-4211"
    assert _attr(child_input, "department_id") == "payments"
    assert _attr(child_input, "workflow_type") == "code_change_with_test"
    assert _attr(child_input, "target_repo") == "payment-service"
    assert _attr(child_input, "target_branch") == "develop"

    output_actions = _attr(child_input, "output_actions")
    pr_actions = [a for a in output_actions if _attr(a, "type") == "bitbucket_pr"]
    assert len(pr_actions) == 1, (
        f"expected one bitbucket_pr action, got {len(pr_actions)}: "
        f"{output_actions!r}"
    )
    pr_payload = _attr(pr_actions[0], "payload")
    # PR ``draft`` MUST be True regardless of what the LLM returned
    # (parser coerces the value — MIMARI §1 Kural 10, Property 11.3).
    assert pr_payload.get("draft") is True, (
        f"bitbucket_pr.draft must be True, got payload={pr_payload!r}"
    )

    # 4. Completion comment posted (✅ prefix per
    #    AutomationWorkflow._format_completion_comment) and the issue
    #    transitioned to Done.
    assert any(c.startswith("✅") for c in state.comments), (
        f"completion comment missing in {state.comments!r}"
    )
    assert "Done" in state.transitions, (
        f"Done transition missing in {state.transitions!r}"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from ``obj`` whether it is a dataclass or a dict.

    Temporal serialises payloads on the workflow boundary, so the child
    input the double receives may arrive either as the original
    ``_AgentRunnerInputShape`` instance (when running in-process under
    the test server) or as a plain dict reconstituted from JSON. The
    test asserts equally well against both shapes via this duck-typing
    accessor.
    """

    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
