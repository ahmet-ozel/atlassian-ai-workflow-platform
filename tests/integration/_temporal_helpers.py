"""Shared helpers for AutomationWorkflow integration tests.

The five integration tests under ``tests/integration/test_temporal_*.py`` and
``test_capability_denied.py`` all spin up a temporal time-skipping
``WorkflowEnvironment``, register the :class:`AutomationWorkflow` (and a
small ``AgentRunnerWorkflow`` stub) plus a configurable bag of mocked
activities, and start a single workflow run to drive a particular
state-machine branch.

To keep each scenario file small and focused on its assertions, the
common support code lives here:

- ``ensure_worker_on_sys_path`` - prepend the ``agent-runner-worker``
 directory to ``sys.path`` so ``from src.workflows...`` imports resolve
 without first installing the worker package.
- ``StubAgentRunnerWorkflow`` - a tiny ``@workflow.defn(name=...)``
 child workflow that just returns the string passed in. It satisfies
 ``AutomationWorkflow.execute_child_workflow("AgentRunnerWorkflow", ...)``
 for tests that drive the workflow through to completion. The
 scenarios that fail before the child dispatch step still register it
 to keep the worker bootstrap uniform.
- ``make_default_activities`` - returns the canonical bag of pure
 no-op acknowledgement activities (jira_add_comment,
 jira_transition_issue, update_work_item_status, jira_get_issue) plus
 a ``CallLog`` capturing every invocation by name. Each scenario
 layers its own ``llm_analyze_task`` mock on top so the LLM behaviour
 is the only differing element across tests.
- ``CallLog`` - append-only list of ``(activity_name, args)`` tuples
 the activity wrappers append to. Tests use this to assert e.g. that
 the timeout-comment was posted exactly once or that the LLM was
 re-run after a signal.
each scenario file's own requirements list).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from temporalio import workflow as _temporal_workflow

# ---------------------------------------------------------------------------
# Path bootstrapping
# ---------------------------------------------------------------------------

# tests/integration/_temporal_helpers.py → platform/
_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]
_AGENT_RUNNER_WORKER: Path = (
    _PLATFORM_ROOT / "workers" / "agent-runner-worker"
)


def ensure_worker_on_sys_path() -> None:
    """Prepend the agent-runner-worker root onto ``sys.path``.

 Mirrors the bootstrap in ``test_workflow_determinism_replay.py``:
 the worker ships its sources under ``workers/agent-runner-worker/src``
 and imports them under the ``src.`` namespace, so adding the worker
 directory (not its ``src/`` child) makes ``from src.workflows...``
 resolve correctly during the integration suite.
 """

    candidate = str(_AGENT_RUNNER_WORKER)
    if _AGENT_RUNNER_WORKER.is_dir() and candidate not in sys.path:
        sys.path.insert(0, candidate)


# ---------------------------------------------------------------------------
# Call log
# ---------------------------------------------------------------------------


@dataclass
class CallLog:
    """Append-only invocation log shared across activity stubs.

 Each ``(name, args)`` tuple is appended in call order. Tests inspect
 ``names_called`` for ordering / count assertions and
 ``args_for(name)`` for payload assertions (e.g. the Jira comment
 body posted on the timeout branch).
 """

    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def record(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def names_called(self) -> list[str]:
        return [name for name, _ in self.calls]

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)

    def args_for(self, name: str) -> list[tuple[Any, ...]]:
        return [args for n, args in self.calls if n == name]


# ---------------------------------------------------------------------------
# Default activity bag + AgentRunnerWorkflow stub
# ---------------------------------------------------------------------------


def make_default_activities(
    *,
    log: CallLog,
    issue_summary: str = "Add /healthz endpoint",
    issue_description: str = "Service needs a basic health probe.",
) -> list[Any]:
    """Return the canonical no-op activity bag used by every scenario.

 Each activity is decorated with ``@activity.defn(name=...)`` so the
 Temporal worker registers it under the same name the workflow body
 passes to ``execute_activity("name", ...)``. Every call appends to
 ``log`` so tests can assert on the call sequence without re-mocking.

 LLM analysis is *not* included here - each scenario binds its own
 ``llm_analyze_task`` mock to drive the state machine into the
 branch under test.
 """

    from temporalio import activity

    @activity.defn(name="jira_add_comment")
    async def _jira_add_comment(
        issue_key: str, body: str, dept_id: str
    ) -> None:
        log.record("jira_add_comment", issue_key, body, dept_id)
        return None

    @activity.defn(name="jira_transition_issue")
    async def _jira_transition_issue(
        issue_key: str, target_status: str, dept_id: str
    ) -> None:
        log.record("jira_transition_issue", issue_key, target_status, dept_id)
        return None

    @activity.defn(name="update_work_item_status")
    async def _update_work_item_status(
        workflow_id: str, new_status: str
    ) -> None:
        log.record("update_work_item_status", workflow_id, new_status)
        return None

    @activity.defn(name="jira_get_issue")
    async def _jira_get_issue(
        issue_key: str, dept_id: str
    ) -> dict[str, Any]:
        log.record("jira_get_issue", issue_key, dept_id)
        return {
            "key": issue_key,
            "summary": issue_summary,
            "description": issue_description,
            "issue_type": "Story",
            "status": "To Do",
            "assignee_account_id": None,
            "project_key": issue_key.split("-", 1)[0],
            "labels": [],
            "priority": None,
        }

    return [
        _jira_add_comment,
        _jira_transition_issue,
        _update_work_item_status,
        _jira_get_issue,
    ]


def make_stub_agent_runner_workflow() -> type:
    """Return the module-level ``AgentRunnerWorkflow`` ``@workflow.defn`` stub.

 The stub returns the constant string ``"stub-ok"`` immediately,
 which is enough to satisfy
 :class:`AutomationWorkflow`'s ``execute_child_workflow`` call for
 happy-path scenarios. Temporal forbids ``@workflow.run`` on a
 *local* class, so the stub is defined at module scope (see
 :class:`_StubAgentRunnerWorkflow` below); this helper just returns
 the same class to every caller and keeps test files free of the
 Temporal decorator import.
 """

    return _StubAgentRunnerWorkflow


def make_task_analysis(
    *,
    workflow_type: str,
    confidence: str = "high",
    needs_info_question: str | None = None,
    target_repo: str | None = "payment-service",
    target_branch: str | None = "develop",
    output_actions: tuple[Any, ...] | None = None,
) -> Any:
    """Build a :class:`TaskAnalysis` dataclass instance for LLM-mock returns.

 The real ``llm_analyze_task`` activity returns a ``TaskAnalysis``
 dataclass; the workflow body relies on attribute access
 (``analysis.workflow_type``, ``a.type for a in analysis.output_actions``,
 ...). Our mocks must return the same type so the data converter
 round-trips identically and the workflow receives objects, not dicts.

 Imports happen lazily so this helper can be called from inside
 activity functions without polluting module-import time.
 """

    from src.prompts.parser import OutputAction, TaskAnalysis

    if output_actions is None:
        output_actions = (
            OutputAction(type="jira_comment", payload={"text": "ok"}),
        )

    return TaskAnalysis(
        workflow_type=workflow_type,
        target_repo=target_repo,
        target_branch=target_branch,
        output_actions=output_actions,
        confidence=confidence,
        needs_info_question=needs_info_question,
    )



# ---------------------------------------------------------------------------
# Module-level AgentRunnerWorkflow stub
# ---------------------------------------------------------------------------
#
# Temporal's ``@workflow.run`` decorator rejects local classes, so the
# stub child workflow must be declared at module scope rather than
# inside ``make_stub_agent_runner_workflow``. The factory above returns
# this class unchanged.


@_temporal_workflow.defn(name="AgentRunnerWorkflow", sandboxed=False)
class _StubAgentRunnerWorkflow:
    """Module-level stub satisfying ``execute_child_workflow("AgentRunnerWorkflow", ...)``.

 Returns a fixed string so happy-path scenarios can drive
 :class:`AutomationWorkflow` through to a ``status="completed"``
 result without needing the real (currently stub) AgentRunner body.
 The fixed return value is short and human-readable so it shows up
 legibly in the completion-comment assertion when scenarios choose
 to inspect it.
 """

    @_temporal_workflow.run
    async def run(self, _input: Any) -> str:
        return "stub-ok"
