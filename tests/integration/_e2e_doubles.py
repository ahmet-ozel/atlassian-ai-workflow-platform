"""Module-scoped Temporal workflow doubles for ``test_e2e_code_change.py``.

This module is intentionally minimal (no filesystem or sys.path
manipulation, no heavy imports) so the Temporal workflow sandbox can
re-import it during workflow validation without tripping over banned
calls (``Path.resolve()``, ``open()``, ``random.*``, ...).

The ``AutomationWorkflow`` orchestrator dispatches its child by the
string name ``"AgentRunnerWorkflow"`` — see
``platform/workers/agent-runner-worker/src/workflows/automation_workflow.py``.
Registering a ``@workflow.defn(name="AgentRunnerWorkflow")`` class on
the same task queue makes the parent's
``workflow.execute_child_workflow("AgentRunnerWorkflow", ...)`` resolve
to this double rather than the production stub (which has no
``@workflow.defn`` body yet).

How the double records its input
--------------------------------

The Temporal workflow sandbox re-imports modules with ``@workflow.defn``
classes into an isolated module namespace, so writing to module-level
variables from inside a workflow body would leave the test harness's
copy untouched. To get observable state out of the workflow, the
double executes a synchronous, no-op recording **activity** —
``e2e_record_child_input`` — which runs *outside* the sandbox and can
mutate plain process-level state (``LAST_RUN``).

The activity is also used to assert that the child was actually
dispatched; if it never runs, ``LAST_RUN["invocations"]`` stays at
zero and the test surfaces a clear error message.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import activity, workflow

#: Process-level sink the recording activity writes onto. The test
#: harness resets these keys at entry to keep state hermetic between
#: runs.
LAST_RUN: dict[str, Any] = {"input": None, "invocations": 0}

#: Canned summary string the double returns; mirrors the success path
#: shape (branch + commit + draft PR) the real AgentRunnerWorkflow body
#: will produce. The parent ``AutomationWorkflow`` flattens this through
#: ``_stringify_child_result`` into the Jira completion comment.
_CANNED_SUMMARY: str = (
    "branch=ai/PAY-4211/iter-1 commit=abc123 "
    "pr=https://bitbucket.example/payment-service/pull/42"
)


@activity.defn(name="e2e_record_child_input")
async def e2e_record_child_input(payload: Any) -> None:
    """Record the AgentRunnerWorkflow's input on the process-level sink.

    Activities run outside the workflow sandbox, so this function can
    safely mutate ``LAST_RUN`` (module-level state in *this* module
    object — the same one the test harness imports). The payload is a
    serialised view of the ``_AgentRunnerInputShape`` the parent
    constructed; Temporal converts the dataclass to a dict on the
    activity boundary by default.
    """

    LAST_RUN["input"] = payload
    LAST_RUN["invocations"] = int(LAST_RUN.get("invocations") or 0) + 1


@workflow.defn(name="AgentRunnerWorkflow")
class AgentRunnerWorkflowDouble:
    """Test double mirroring the success path of AgentRunnerWorkflow.

    Receives the parent's ``_AgentRunnerInputShape`` (or its
    serialised dict equivalent), forwards it to the recording activity
    so the harness can assert against it, and returns a canned summary
    string the parent flattens into the Jira completion comment via
    :py:meth:`AutomationWorkflow._stringify_child_result`.
    """

    @workflow.run
    async def run(self, child_input: Any) -> str:
        # Forward the input out of the sandbox via an activity. The
        # activity name resolves to ``e2e_record_child_input`` registered
        # on the same worker (see the test's ``Worker(activities=...)``
        # list). A short timeout keeps a misregistration loud.
        await workflow.execute_activity(
            "e2e_record_child_input",
            args=[child_input],
            start_to_close_timeout=timedelta(seconds=10),
        )
        return _CANNED_SUMMARY


__all__ = [
    "AgentRunnerWorkflowDouble",
    "LAST_RUN",
    "e2e_record_child_input",
]
