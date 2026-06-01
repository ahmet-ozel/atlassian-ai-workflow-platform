"""Platform-completion integration helpers for ``AutomationWorkflow``.

This module ships the four helper coroutines required by
:doc:`platform-completion task 26.1 <.kiro/specs/platform-completion/tasks.md>`.
The existing :class:`automation_worker.workflows.automation_workflow.AutomationWorkflow`
is large and battle-tested — instead of rewriting its body, the
gateway workflow can call these helpers at the documented hook
points to wire in the new components shipped by the
platform-completion spec:

* multi-step orchestrator delegation (R5.1–5.10)
* pre-workflow repo-field resolution (R9.1–9.5)
* pre-commit approval gate (R11.1–11.8)
* post-execution output-action batch (R3.1–3.11)

Determinism contract
--------------------

Every helper is **pure orchestration** — its only side effects are
``workflow.execute_activity`` and
``workflow.execute_child_workflow`` calls.  The helpers explicitly
do **not**:

* import or call activity callables directly;
* read ``os.environ``, the wall clock, or the random module;
* perform any direct I/O (HTTP, Postgres, files).

This keeps them safe to call from inside the Temporal workflow
sandbox — the static AST scanner used by the determinism property
test treats ``workflow.execute_*`` exactly like the existing
``AutomationWorkflow`` body.

Activity / workflow imports live inside
``workflow.unsafe.imports_passed_through()`` so the sandbox accepts
the network-side dataclasses without complaint.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final

from temporalio import workflow
from temporalio.common import RetryPolicy


# ---------------------------------------------------------------------------
# Sandbox-safe imports
# ---------------------------------------------------------------------------
#
# The dataclasses below are pulled into the workflow process as plain
# data carriers. Their modules import :mod:`temporalio.activity` (for
# the ``@activity.defn`` decorator on the activity callables) which
# would otherwise trip the sandbox; the ``imports_passed_through``
# block is the Temporal-blessed escape hatch for that case.

with workflow.unsafe.imports_passed_through():
    from automation_worker.activities.output_actions import (
        ExecutionBatchInput,
        ExecutionBatchResult,
        OutputAction,
    )
    from automation_worker.activities.repo_resolver import (
        RepoResolveInput,
        RepoResolveResult,
    )
    from automation_worker.workflows.approval_gate import (
        ApprovalGateInput,
        ApprovalGateResult,
    )
    from automation_worker.workflows.multi_step_workflow import (
        MultiStepInput,
        MultiStepResult,
        StepDefinition,
    )
    from db_shared.enums import ActionType
    from temporal_shared.workflow_registry import task_queue_for


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Activity name string for the output-action executor (R3.1–3.11).
_ACT_EXECUTE_OUTPUT_ACTIONS: Final[str] = "execute_output_actions"

#: Activity name string for the repo-field resolver (R9.1–9.5).
_ACT_RESOLVE_REPO_FIELD: Final[str] = "resolve_repo_field"

#: Default timeout for the output-action batch — 20 actions × 30 s
#: per-action ceiling enforced inside the activity, plus headroom.
_OUTPUT_ACTIONS_TIMEOUT: Final[timedelta] = timedelta(minutes=15)

#: Default timeout for the repo-field resolver. Allows a single LLM
#: round-trip plus the user-prompt comment when confidence is low.
_REPO_RESOLVE_TIMEOUT: Final[timedelta] = timedelta(minutes=2)

#: Default timeout for the approval-gate child workflow's ``run``
#: method — the gate itself blocks for up to 4 hours
#: (:data:`approval_gate.APPROVAL_TIMEOUT`); we double it as a hard
#: ceiling so a misbehaving signal handler cannot wedge the parent.
_APPROVAL_GATE_RUN_TIMEOUT: Final[timedelta] = timedelta(hours=8)

#: Default timeout for the multi-step orchestrator child. The child
#: enforces per-step timeouts internally; this is just the outer
#: wall-clock guard.
_MULTI_STEP_RUN_TIMEOUT: Final[timedelta] = timedelta(hours=24)

#: Retry policy for short, idempotent activity calls. Mirrors the
#: parent workflow's defaults so operators see consistent retry
#: behaviour across both gateways.
_DEFAULT_RETRY: Final[RetryPolicy] = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)


__all__ = (
    "execute_output_actions_post",
    "maybe_run_approval_gate",
    "maybe_run_multi_step",
    "resolve_repo_pre_workflow",
)


# ---------------------------------------------------------------------------
# Helper 1 — multi-step delegation (R5.1–5.10)
# ---------------------------------------------------------------------------


async def maybe_run_multi_step(
    workflow_type: str,
    plan: list[dict[str, Any]] | None,
    issue_key: str,
    dept_id: str,
    workflow_id: str,
    output_actions: list[dict[str, Any]] | None = None,
) -> bool:
    """Delegate to :class:`MultiStepWorkflow` when the LLM picks ``multi_step``.

    Validates Requirements **5.1** (2–20 step plan), **5.2** (each
    step runs as an independent child workflow), **5.7**, **5.8**.

    The helper inspects ``workflow_type`` and, only when it equals
    ``"multi_step"``, builds a :class:`MultiStepInput` from ``plan``
    (a list of dicts shaped like
    ``{"name", "workflow_type", "input_data", "timeout_seconds",
    "max_retries"}``) and dispatches the child workflow via
    ``workflow.execute_child_workflow``. Any other ``workflow_type``
    is left to the existing dispatch logic and the helper returns
    ``False``.

    The child runs to completion before this helper returns so the
    parent workflow can observe its outcome and short-circuit the
    rest of the gateway pipeline (the parent should treat a True
    return value as "the work is done — do not dispatch further
    children").

    Returns
    -------
    bool
        ``True`` when the multi-step child workflow was dispatched
        (and awaited); ``False`` when ``workflow_type`` is anything
        other than ``"multi_step"``.

    Notes
    -----
    Determinism: only ``workflow.execute_child_workflow`` is used for
    the side effect.  Empty / None ``plan`` values trigger an early
    ``False`` return without raising — the validator inside
    :class:`MultiStepWorkflow` would reject the input anyway, so
    bouncing here keeps the parent's failure handling close to its
    own decision points.
    """

    if workflow_type != "multi_step":
        return False
    if not plan:
        # Without a plan there's nothing to dispatch. The parent
        # gateway should fall back to its normal failure path
        # (likely an LLM analysis re-prompt) when this happens.
        workflow.logger.warning(
            "maybe_run_multi_step: workflow_type='multi_step' but no "
            "plan provided for %s — skipping",
            issue_key,
        )
        return False

    steps: list[StepDefinition] = []
    for raw in plan:
        if not isinstance(raw, dict):
            continue
        steps.append(
            StepDefinition(
                name=str(raw.get("name", "")),
                workflow_type=str(raw.get("workflow_type", "")),
                input_data=dict(raw.get("input_data") or {}),
                timeout_seconds=int(raw.get("timeout_seconds", 300)),
                max_retries=int(raw.get("max_retries", 3)),
            )
        )

    child_input = MultiStepInput(
        issue_key=issue_key,
        dept_id=dept_id,
        steps=steps,
        workflow_id=workflow_id,
        output_actions=list(output_actions or []),
    )

    # The child runs on the same task queue as the parent gateway
    # — task_queue_for("MultiStepWorkflow") would also work, but
    # MultiStepWorkflow is hosted inside automation-worker so we
    # keep the child colocated.
    try:
        task_queue = task_queue_for("MultiStepWorkflow")
    except KeyError:
        # Registry has not been updated for the new workflow yet —
        # fall back to the parent's queue so the dispatch still
        # works against the same worker pool.
        task_queue = workflow.info().task_queue

    child_workflow_id = (
        f"MultiStepWorkflow-{workflow_id}-{issue_key}"
    )

    await workflow.execute_child_workflow(
        "MultiStepWorkflow",
        args=[child_input],
        id=child_workflow_id,
        task_queue=task_queue,
        execution_timeout=_MULTI_STEP_RUN_TIMEOUT,
    )
    return True


# ---------------------------------------------------------------------------
# Helper 2 — approval gate (R11.1–11.8)
# ---------------------------------------------------------------------------


async def maybe_run_approval_gate(
    commit_files: list[str],
    dept_config: dict[str, Any],
    issue_key: str,
    workflow_id: str,
) -> bool:
    """Block on :class:`ApprovalGateWorkflow` when commit paths are protected.

    Validates Requirements **11.1** (regex-based path matching),
    **11.2** (block + Jira comment), **11.3**/**11.4** (signal-driven
    approve/reject), **11.6** (authorized approvers only).

    Reads two keys from ``dept_config``:

    * ``approval_required_paths`` — a list of regex patterns. When
      empty/missing the helper returns ``True`` immediately (the
      commit is implicitly approved — Requirement 11.7).
    * ``approvers`` — a list of authorized Jira account IDs.

    The pure path-matching logic and signal authorization live
    inside :class:`ApprovalGateWorkflow`; this helper's job is just
    to dispatch the child and surface the boolean outcome to the
    parent gateway. ``True`` means "commit allowed", ``False``
    means "commit blocked" (rejected or timed out).

    Returns
    -------
    bool
        ``True`` when the approval gate signalled approval *or*
        when it was skipped because no protected paths were
        configured; ``False`` when the gate rejected or timed
        out.

    Notes
    -----
    Determinism: the helper only awaits a child workflow.  All
    branching is on dataclass fields, not external state.
    """

    approval_paths = dept_config.get("approval_required_paths") or []
    if not approval_paths:
        # Nothing to gate — the parent gateway proceeds.
        return True

    approvers = list(dept_config.get("approvers") or [])
    dept_id = str(dept_config.get("department_id", ""))

    child_input = ApprovalGateInput(
        issue_key=issue_key,
        dept_id=dept_id,
        workflow_id=workflow_id,
        commit_files=list(commit_files),
        approval_required_paths=list(approval_paths),
        approvers=approvers,
    )

    try:
        task_queue = task_queue_for("ApprovalGateWorkflow")
    except KeyError:
        task_queue = workflow.info().task_queue

    child_workflow_id = (
        f"ApprovalGateWorkflow-{workflow_id}-{issue_key}"
    )

    result: ApprovalGateResult = await workflow.execute_child_workflow(
        "ApprovalGateWorkflow",
        args=[child_input],
        id=child_workflow_id,
        task_queue=task_queue,
        execution_timeout=_APPROVAL_GATE_RUN_TIMEOUT,
        result_type=ApprovalGateResult,
    )

    # Only an explicit approval lets the parent proceed; rejection
    # and timeout both block the commit.
    return bool(result.approved and not result.timed_out)


# ---------------------------------------------------------------------------
# Helper 3 — output-action batch (R3.1–3.11)
# ---------------------------------------------------------------------------


async def execute_output_actions_post(
    actions: list[dict[str, Any]] | None,
    issue_key: str,
    dept_id: str,
    workflow_id: str,
) -> dict[str, Any]:
    """Run the LLM-proposed output_actions list via the executor activity.

    Validates Requirements **3.1** (sequential execution), **3.7**
    (continue on failure), **3.9** (per-action audit record), **3.11**
    (failure summary).

    Each action dict is expected to carry at least ``type`` and
    ``params`` keys; ``index`` is filled in by the helper using the
    list position so callers can build the actions in plain
    declaration order.  Action types are coerced into the
    :class:`db_shared.enums.ActionType` enum — unknown values are
    skipped with a warning so a single bad action does not block
    the rest of the batch.

    Returns
    -------
    dict
        ``{"all_succeeded": bool, "results": [...]}`` — the
        :class:`ExecutionBatchResult` projected into a plain dict
        so the caller can round-trip it through Temporal's data
        converter without coupling to the activity's dataclass.
    """

    if not actions:
        return {
            "all_succeeded": True,
            "results": [],
            "failed_actions": [],
        }

    coerced: list[OutputAction] = []
    for index, raw in enumerate(actions):
        if not isinstance(raw, dict):
            continue
        action_type_value = raw.get("type")
        try:
            action_type = (
                action_type_value
                if isinstance(action_type_value, ActionType)
                else ActionType(str(action_type_value))
            )
        except (TypeError, ValueError):
            workflow.logger.warning(
                "execute_output_actions_post: skipping action with "
                "unknown type=%r at index %d",
                action_type_value,
                index,
            )
            continue
        coerced.append(
            OutputAction(
                type=action_type,
                params=dict(raw.get("params") or {}),
                index=index,
            )
        )

    batch = ExecutionBatchInput(
        actions=coerced,
        issue_key=issue_key,
        dept_id=dept_id,
        workflow_id=workflow_id,
    )

    result: ExecutionBatchResult = await workflow.execute_activity(
        _ACT_EXECUTE_OUTPUT_ACTIONS,
        args=[batch],
        result_type=ExecutionBatchResult,
        start_to_close_timeout=_OUTPUT_ACTIONS_TIMEOUT,
        retry_policy=_DEFAULT_RETRY,
    )

    return {
        "all_succeeded": bool(result.all_succeeded),
        "results": [
            {
                "action_type": getattr(r.action_type, "value", str(r.action_type)),
                "index": r.index,
                "status": r.status,
                "error": r.error,
            }
            for r in (result.results or [])
        ],
        "failed_actions": [
            {
                "action_type": getattr(r.action_type, "value", str(r.action_type)),
                "index": r.index,
                "status": r.status,
                "error": r.error,
            }
            for r in (result.failed_actions or [])
        ],
    }


# ---------------------------------------------------------------------------
# Helper 4 — pre-workflow repo resolution (R9.1–9.5)
# ---------------------------------------------------------------------------


async def resolve_repo_pre_workflow(
    structured_field: str | None,
    description: str,
    dept_config: dict[str, Any],
    issue_key: str,
    workflow_id: str,
) -> dict[str, Any]:
    """Resolve the target repo before dispatching the child workflow.

    Validates Requirements **9.1** (allowed-list validation), **9.2**
    (structured field priority), **9.3** (LLM fallback parsing),
    **9.4** (rejection on out-of-list values), **9.5** (user-prompt
    comment).

    Reads ``repo_mappings`` and ``department_id`` from
    ``dept_config``; the activity itself enforces the priority
    order documented in :mod:`automation_worker.activities.repo_resolver`.

    Returns
    -------
    dict
        ``{"resolved": bool, "repo_url": str | None, "confidence":
        float, "needs_user_input": bool, "error": str | None}`` —
        the :class:`RepoResolveResult` projected into a plain
        dict so callers can serialize it onto the parent workflow
        output without coupling to the activity's dataclass.
    """

    repo_mappings = list(dept_config.get("repo_mappings") or [])
    dept_id = str(dept_config.get("department_id", ""))

    payload = RepoResolveInput(
        issue_key=issue_key,
        dept_id=dept_id,
        workflow_id=workflow_id,
        structured_field_value=structured_field,
        description=description,
        repo_mappings=repo_mappings,
    )

    result: RepoResolveResult = await workflow.execute_activity(
        _ACT_RESOLVE_REPO_FIELD,
        args=[payload],
        result_type=RepoResolveResult,
        start_to_close_timeout=_REPO_RESOLVE_TIMEOUT,
        retry_policy=_DEFAULT_RETRY,
    )

    return {
        "resolved": bool(result.resolved),
        "repo_url": result.repo_url,
        "confidence": float(result.confidence),
        "needs_user_input": bool(result.needs_user_input),
        "error": result.error,
    }
