"""Integration test: AgentRunnerWorkflow cancel  compensation chain.


Scenario
--------

This test pins the workflow-layer contract for cancel + compensation
defined in :class:`AgentRunnerWorkflow`:

1. A ``cancel_requested`` signal triggers the
 ``compensation_chain_run`` activity exactly once and emits the
 ``workflow_cancelled_by_end_user`` audit row.
2. A second ``cancel_requested`` signal that arrives after the chain
 has been latched is a no-op - no extra ``compensation_chain_run``
 activity invocation, no extra audit row.
3. An admin-role cancel (``actor_role="dept_admin"``) emits
 ``workflow_cancelled_by_admin`` instead of the end-user variant.
4. A ``MAX_ITER`` natural termination must NOT call the compensation
 chain - the workflow returns ``status="out_of_scope"`` cleanly.

The test runs the *real* :class:`AgentRunnerWorkflow` against the
Temporal time-skipping ``WorkflowEnvironment``. Activities the
workflow body calls (``compensation_chain_run``, ``audit_emit``,
``jira_add_comment``, ``update_work_item_status``) are stubbed by
small ``@activity.defn`` wrappers that record every invocation in a
shared :class:`ActivityCallLog`. The assertions inspect the recorded
calls to verify the contract.

Skip behaviour
--------------

If the embedded ``temporal-test-server`` binary is unavailable on the
host (no native deps, sandboxed CI runner, missing import), every
test in this file ``pytest.skip``s cleanly so the integration suite
remains green on machines that cannot host Temporal locally.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap - pull the agent-runner-worker tree, temporal-shared,
# and mcp_client onto the path so ``import agent_runner.*`` resolves.
# Mirrors the bootstrap used by the unit tests under
# ``workers/agent-runner-worker/tests/unit/test_agent_runner_cancel.py``.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]
_AGENT_RUNNER_SRC: Path = (
    _PLATFORM_ROOT / "workers" / "agent-runner-worker" / "src"
)
_TEMPORAL_SHARED_SRC: Path = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)
_MCP_CLIENT_SRC: Path = _PLATFORM_ROOT / "libs" / "mcp_client" / "src"

for _candidate in (_AGENT_RUNNER_SRC, _TEMPORAL_SHARED_SRC, _MCP_CLIENT_SRC):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)


# ---------------------------------------------------------------------------
# Environment availability gate
# ---------------------------------------------------------------------------


def _temporal_test_env_available() -> bool:
    """Return ``True`` when the Temporal time-skipping env imports.

 Module-level skip mirrors the gate used by
 ``test_temporal_idempotency.py`` so a host without the embedded
 ``temporal-test-server`` binary skips this file cleanly instead of
 erroring on import.
 """

    try:
        from temporalio.testing import WorkflowEnvironment  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure  skip.
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _temporal_test_env_available(),
    reason="temporalio test environment not available",
)


@contextlib.asynccontextmanager
async def _start_time_skipping_or_skip() -> Any:
    """Start the Temporal time-skipping env, ``pytest.skip``ing on failure.

 The embedded ``temporal-test-server`` may fail to start on hosts
 where the binary is not bundled. When that happens the test is
 skipped rather than errored so the integration suite stays green
 on machines that can't host Temporal.
 """

    from temporalio.testing import WorkflowEnvironment

    try:
        env_cm = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - surface as skip.
        pytest.skip(f"temporalio test environment not available: {exc}")
    async with env_cm as env:
        yield env


# ---------------------------------------------------------------------------
# Activity call log
# ---------------------------------------------------------------------------


@dataclass
class ActivityCallLog:
    """Append-only log of activity invocations recorded by the stubs."""

    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(
        default_factory=list
    )

    def record(
        self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        self.calls.append((name, args, kwargs))

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def count(self, name: str) -> int:
        return sum(1 for n, _, _ in self.calls if n == name)

    def args_for(self, name: str) -> list[tuple[Any, ...]]:
        return [args for n, args, _ in self.calls if n == name]


# ---------------------------------------------------------------------------
# Activity stub factory
# ---------------------------------------------------------------------------


def _make_activities(log: ActivityCallLog) -> list[Any]:
    """Build the bag of stub activities the workflow body calls.

 The workflow body invokes activities by *name* via
 ``workflow.execute_activity("name", ...)``; the stubs are
 registered with matching ``@activity.defn(name=...)`` so the
 Temporal worker resolves them at dispatch time. Every invocation
 appends to ``log`` so the test assertions can verify call order /
 count without re-mocking inside each test.
 """

    from temporalio import activity

    @activity.defn(name="compensation_chain_run")
    async def _compensation_chain_run(payload: dict[str, Any]) -> dict[str, Any]:
        log.record("compensation_chain_run", (payload,), {})
        return {"ok": True}

    @activity.defn(name="audit_emit")
    async def _audit_emit(payload: dict[str, Any]) -> None:
        log.record("audit_emit", (payload,), {})
        return None

    @activity.defn(name="jira_add_comment")
    async def _jira_add_comment(*args: Any, **kwargs: Any) -> None:
        log.record("jira_add_comment", args, kwargs)
        return None

    @activity.defn(name="update_work_item_status")
    async def _update_work_item_status(*args: Any, **kwargs: Any) -> None:
        log.record("update_work_item_status", args, kwargs)
        return None

    return [
        _compensation_chain_run,
        _audit_emit,
        _jira_add_comment,
        _update_work_item_status,
    ]


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _make_input(
    *,
    workflow_type: str = "noop_test",
    iteration: int = 1,
    max_iter: int = 5,
) -> Any:
    """Build a minimal ``AgentRunnerWorkflowInput`` for the workflow.

 ``noop_test`` is used as the workflow type because it falls
 through to the legacy signal-wait loop in
 :meth:`AgentRunnerWorkflow._dispatch_workflow_type`. That loop is
 the natural surface for cancel-signal handling - the body parks
 in ``workflow.wait_condition(...)`` until either the cap flips or
 a signal lands, and the cancel branch then routes into
 ``_handle_cancel`` which dispatches the compensation chain.
 """

    from temporal_shared.messages import (
        AgentRunnerWorkflowInput,
        LlmAnalysisResult,
    )

    analysis = LlmAnalysisResult(
        workflow_type=workflow_type,
        confidence="high",
        target_repo="payment-callbacks",
        target_branch="ai/PAY-4250",
        title="Cancel + compensation integration test",
        rationale="integration-fixture",
        token_usage=42,
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id="automation-jira-PAY-4250",
        issue_key="PAY-4250",
        department_id="payments",
        workflow_type=workflow_type,
        analysis=analysis,
        target_repo="payment-callbacks",
        target_branch="ai/PAY-4250",
        iteration=iteration,
        max_iter=max_iter,
        default_language="tr",
    )


# ---------------------------------------------------------------------------
# Note on signal delivery
# ---------------------------------------------------------------------------
#
# Under :class:`temporalio.testing.WorkflowEnvironment.start_time_skipping`
# the test server fast-forwards virtual time whenever the workflow has
# nothing to do. The ``noop_test`` workflow type falls through to the
# legacy signal-wait fallback in
# :meth:`AgentRunnerWorkflow._dispatch_workflow_type`, which parks on
# ``workflow.wait_condition(..., timeout=SIGNAL_WAIT_TIMEOUT=7d)``.
# Sending a signal *after* the body has parked therefore races the
# 7-day timeout - the server will sometimes fast-forward to the
# timeout before the signal lands, completing the workflow with
# ``status="out_of_scope"`` instead of ``"cancelled"``.
#
# To eliminate that race we use the ``start_signal`` parameter of
# :meth:`temporalio.client.Client.start_workflow` (signal-with-start).
# The signal is delivered *before* the workflow body's first task -
# the cancel handler runs as part of the initial workflow tick and
# the body observes ``_cancel_requested=True`` on its very first
# ``wait_condition`` evaluation, raising ``_CancelledViaSignal`` and
# routing into ``_handle_cancel`` deterministically.


# ---------------------------------------------------------------------------
# 1. Cancel triggers compensation chain + end-user audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cancel_signal_runs_compensation_chain_once_with_end_user_audit() -> None:
    """A single end-user ``cancel_requested`` signal:

 * triggers ``compensation_chain_run`` exactly once,
 * emits ``workflow_cancelled_by_end_user`` exactly once,
 * terminates the workflow with ``status="cancelled"``.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        CANCEL_BY_END_USER_AUDIT_ACTION,
        AgentRunnerWorkflow,
        CancelRequestedSignal,
    )

    log = ActivityCallLog()
    activities = _make_activities(log)

    workflow_id = "automation-jira-PAY-4250-cancel"
    task_queue = "agent-runner-cancel-compensation"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _make_input()
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
                start_signal="cancel_requested",
                start_signal_args=[
                    CancelRequestedSignal(
                        actor_id="alice",
                        actor_role="end_user",
                        reason="user_cancel",
                    )
                ],
            )

            result: Any = await handle.result()

    # ----- Assertions -------------------------------------------------

    # Compensation chain dispatched exactly once.
    assert log.count("compensation_chain_run") == 1, (
        f"expected exactly one compensation_chain_run call, got "
        f"{log.count('compensation_chain_run')} - call log: {log.names()!r}"
    )

    # End-user audit emitted exactly once.
    cancel_audits = [
        args[0]
        for args in log.args_for("audit_emit")
        if isinstance(args[0], dict)
        and args[0].get("action") == CANCEL_BY_END_USER_AUDIT_ACTION
    ]
    assert len(cancel_audits) == 1, (
        f"expected exactly one workflow_cancelled_by_end_user audit, got "
        f"{len(cancel_audits)} - audit calls: "
        f"{[a[0] for a in log.args_for('audit_emit')]!r}"
    )
    audit_payload = cancel_audits[0]
    assert audit_payload["workflow_id"] == workflow_id
    assert audit_payload["dept_id"] == "payments"
    assert audit_payload["issue_key"] == "PAY-4250"

    # Compensation context carries the actor identity .
    chain_payload = log.args_for("compensation_chain_run")[0][0]
    assert chain_payload["actor_id"] == "alice"
    assert chain_payload["actor_role"] == "end_user"
    assert chain_payload["reason"] == "user_cancel"
    assert chain_payload["dept_id"] == "payments"
    assert chain_payload["issue_key"] == "PAY-4250"
    assert chain_payload["workflow_id"] == workflow_id

    # Terminal status. The workflow returns either an
    # AgentRunnerWorkflowOutput dataclass or a dict (depending on the
    # SDK's data converter); both shapes carry ``status`` somehow.
    status = _extract_status(result)
    assert status == "cancelled", f"expected status=cancelled, got {status!r}"


# ---------------------------------------------------------------------------
# 2. Second cancel signal is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_second_cancel_signal_after_chain_is_idempotent_no_op() -> None:
    """Sending two ``cancel_requested`` signals must produce exactly one
 ``compensation_chain_run`` invocation and exactly one cancel audit
 row. The second cancel is observed by the latched workflow state
 (``_cancel_requested=True``, ``_compensation_running=True``) and
 short-circuits in the signal handler.

 Implementation: the first cancel is delivered via signal-with-start
 (so it is processed during the workflow's first tick - eliminating
 the race against the legacy signal-wait fallback's 7-day timeout).
 The compensation activity is wired with a small delay so we can
 send the second cancel while the chain is in flight, exercising
 the ``_compensation_running`` idempotency latch.
 """

    import asyncio

    from temporalio import activity
    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        CANCEL_BY_ADMIN_AUDIT_ACTION,
        CANCEL_BY_END_USER_AUDIT_ACTION,
        AgentRunnerWorkflow,
        CancelRequestedSignal,
    )

    log = ActivityCallLog()

    # Custom compensation activity that signals back to the test
    # before returning, so the test can deliver the second cancel
    # while the chain is in flight.
    chain_started = asyncio.Event()
    chain_may_finish = asyncio.Event()

    @activity.defn(name="compensation_chain_run")
    async def _slow_compensation(payload: dict[str, Any]) -> dict[str, Any]:
        log.record("compensation_chain_run", (payload,), {})
        chain_started.set()
        # Heartbeat while waiting so Temporal does not time out the
        # activity. The wait is bounded so a misbehaving test does
        # not hang indefinitely.
        try:
            await asyncio.wait_for(chain_may_finish.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            pass
        return {"ok": True}

    @activity.defn(name="audit_emit")
    async def _audit_emit(payload: dict[str, Any]) -> None:
        log.record("audit_emit", (payload,), {})
        return None

    @activity.defn(name="jira_add_comment")
    async def _jira_add_comment(*args: Any, **kwargs: Any) -> None:
        log.record("jira_add_comment", args, kwargs)
        return None

    @activity.defn(name="update_work_item_status")
    async def _update_work_item_status(*args: Any, **kwargs: Any) -> None:
        log.record("update_work_item_status", args, kwargs)
        return None

    activities = [
        _slow_compensation,
        _audit_emit,
        _jira_add_comment,
        _update_work_item_status,
    ]

    workflow_id = "automation-jira-PAY-4250-double-cancel"
    task_queue = "agent-runner-cancel-idempotent"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _make_input()
            # Deliver the first cancel via signal-with-start so it is
            # processed in the workflow's first tick, deterministically
            # routing the body into ``_handle_cancel``.
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
                start_signal="cancel_requested",
                start_signal_args=[
                    CancelRequestedSignal(
                        actor_id="alice",
                        actor_role="end_user",
                        reason="first",
                    )
                ],
            )

            # Wait for the compensation chain activity to start. While
            # the activity is parked on ``chain_may_finish``, the
            # workflow's signal handler is still installed and able to
            # accept (and discard, via the latch) a second cancel.
            await asyncio.wait_for(chain_started.wait(), timeout=10.0)

            # Second cancel - must be observed as no-op by the
            # signal handler's idempotency guard
            # (``_cancel_requested`` already True,
            # ``_compensation_running`` already True).
            await handle.signal(
                AgentRunnerWorkflow.cancel_requested,
                CancelRequestedSignal(
                    actor_id="bob",
                    actor_role="admin",
                    reason="duplicate",
                ),
            )

            # Release the compensation activity so the workflow can
            # complete.
            chain_may_finish.set()

            result: Any = await handle.result()

    # ----- Assertions -------------------------------------------------

    # Compensation chain dispatched exactly once despite two cancels.
    assert log.count("compensation_chain_run") == 1, (
        f"expected exactly one compensation_chain_run call after a "
        f"double cancel, got {log.count('compensation_chain_run')} - "
        f"call log: {log.names()!r}"
    )

    # Exactly one cancel audit emitted (the first cancel's role wins).
    cancel_audits = [
        args[0]
        for args in log.args_for("audit_emit")
        if isinstance(args[0], dict)
        and args[0].get("action")
        in {
            CANCEL_BY_END_USER_AUDIT_ACTION,
            CANCEL_BY_ADMIN_AUDIT_ACTION,
        }
    ]
    assert len(cancel_audits) == 1, (
        f"expected exactly one cancel audit, got {len(cancel_audits)} - "
        f"audit calls: {[a[0] for a in log.args_for('audit_emit')]!r}"
    )
    # The first cancel was end-user - its role wins the latch.
    assert cancel_audits[0]["action"] == CANCEL_BY_END_USER_AUDIT_ACTION

    # First-cancel actor wins the latch - the chain payload reflects
    # ``alice / end_user / first``, not ``bob / admin / duplicate``.
    chain_payload = log.args_for("compensation_chain_run")[0][0]
    assert chain_payload["actor_id"] == "alice"
    assert chain_payload["actor_role"] == "end_user"
    assert chain_payload["reason"] == "first"

    status = _extract_status(result)
    assert status == "cancelled", f"expected status=cancelled, got {status!r}"


# ---------------------------------------------------------------------------
# 3. Admin cancel emits the admin-role audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("admin_role", ["admin", "dept_admin"])
async def test_admin_cancel_emits_admin_audit_action(admin_role: str) -> None:
    """A cancel signal carrying ``actor_role ∈ {admin, dept_admin}``
 emits ``workflow_cancelled_by_admin`` instead of
 ``workflow_cancelled_by_end_user``.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        CANCEL_BY_ADMIN_AUDIT_ACTION,
        CANCEL_BY_END_USER_AUDIT_ACTION,
        AgentRunnerWorkflow,
        CancelRequestedSignal,
    )

    log = ActivityCallLog()
    activities = _make_activities(log)

    workflow_id = f"automation-jira-PAY-4250-admin-{admin_role}"
    task_queue = f"agent-runner-cancel-admin-{admin_role}"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _make_input()
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
                start_signal="cancel_requested",
                start_signal_args=[
                    CancelRequestedSignal(
                        actor_id="carol",
                        actor_role=admin_role,
                        reason="admin_cancel",
                    )
                ],
            )

            result: Any = await handle.result()

    # ----- Assertions -------------------------------------------------

    # Compensation chain dispatched once.
    assert log.count("compensation_chain_run") == 1

    # Admin audit emitted exactly once. End-user audit is NOT emitted.
    admin_audits = [
        args[0]
        for args in log.args_for("audit_emit")
        if isinstance(args[0], dict)
        and args[0].get("action") == CANCEL_BY_ADMIN_AUDIT_ACTION
    ]
    end_user_audits = [
        args[0]
        for args in log.args_for("audit_emit")
        if isinstance(args[0], dict)
        and args[0].get("action") == CANCEL_BY_END_USER_AUDIT_ACTION
    ]
    assert len(admin_audits) == 1, (
        f"expected exactly one admin audit for role={admin_role!r}, got "
        f"{len(admin_audits)} - audit calls: "
        f"{[a[0] for a in log.args_for('audit_emit')]!r}"
    )
    assert end_user_audits == [], (
        f"end-user audit must NOT be emitted for admin-role cancel; got "
        f"{end_user_audits!r}"
    )

    # Chain payload carries the admin role for downstream consumers.
    chain_payload = log.args_for("compensation_chain_run")[0][0]
    assert chain_payload["actor_role"] == admin_role
    assert chain_payload["actor_id"] == "carol"

    status = _extract_status(result)
    assert status == "cancelled"


# ---------------------------------------------------------------------------
# 4. MAX_ITER natural termination must NOT run compensation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_max_iter_natural_termination_does_not_run_compensation() -> None:
    """When the workflow input already exceeds :data:`MAX_ITER` the
 initial ``_should_advance_iter`` check refuses to start the run
 and the body returns ``status="out_of_scope"`` without ever
 invoking ``compensation_chain_run``. This pins the workflow contract
 that natural termination (iter cap, ``out_of_scope``) is
 distinct from cancel - only cancel runs the compensation chain.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        CANCEL_BY_ADMIN_AUDIT_ACTION,
        CANCEL_BY_END_USER_AUDIT_ACTION,
        MAX_ITER,
        AgentRunnerWorkflow,
    )

    log = ActivityCallLog()
    activities = _make_activities(log)

    workflow_id = "automation-jira-PAY-4250-max-iter"
    task_queue = "agent-runner-cancel-max-iter"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            # Drive the workflow with iteration > MAX_ITER so the
            # initial advance-precondition refuses and the body
            # short-circuits to ``out_of_scope`` without entering the
            # signal-wait loop.
            inp = _make_input(iteration=MAX_ITER + 1, max_iter=MAX_ITER)
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
            )

            result: Any = await handle.result()

    # ----- Assertions -------------------------------------------------

    # Compensation chain MUST NOT have been called.
    assert log.count("compensation_chain_run") == 0, (
        f"compensation_chain_run must not run on natural termination "
        f"(MAX_ITER); call log: {log.names()!r}"
    )

    # Cancel audits MUST NOT have been emitted.
    cancel_audits = [
        args[0]
        for args in log.args_for("audit_emit")
        if isinstance(args[0], dict)
        and args[0].get("action")
        in {
            CANCEL_BY_END_USER_AUDIT_ACTION,
            CANCEL_BY_ADMIN_AUDIT_ACTION,
        }
    ]
    assert cancel_audits == [], (
        f"cancel audit must not be emitted on natural termination; got "
        f"{cancel_audits!r}"
    )

    # Workflow terminated with ``out_of_scope`` (not cancelled, not
    # failed).
    status = _extract_status(result)
    assert status == "out_of_scope", (
        f"expected status=out_of_scope, got {status!r}"
    )


# ---------------------------------------------------------------------------
# Result extraction helper
# ---------------------------------------------------------------------------


def _extract_status(result: Any) -> str | None:
    """Return ``result.status`` regardless of dataclass / dict shape.

 Temporal's data converter sometimes round-trips frozen dataclasses
 back into the original class and sometimes into plain dicts
 (depending on SDK version and worker configuration). The helper
 accepts both shapes so the assertions stay robust.
 """

    if hasattr(result, "status"):
        return getattr(result, "status")
    if isinstance(result, dict):
        return result.get("status")
    return None
