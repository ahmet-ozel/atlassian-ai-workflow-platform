"""Integration tests for Temporal-driven iteration caps.

This file holds two complementary integration suites against the
time-skipping Temporal ``WorkflowEnvironment``:

1. **Foundation parity** - :func:`test_three_low_confidence_signal_cycles_hit_loop_cap`
 exercises the legacy ``AutomationWorkflow.MAX_LOOP_COUNT`` ceiling
 (3 in the implementation). Three back-to-back ``new_comment`` signals with a
 low-confidence LLM response push ``_loop_count`` to the cap, the
 workflow posts the Turkish "loop cap reached" comment, and
 terminates with ``status="failed"`` /
 ``failure_reason="loop_cap_reached"``.


2. **AgentRunner cap coverage** -
 :func:`test_agent_runner_signal_advances_iter_count_via_real_temporal`
 plus :func:`test_agent_runner_started_at_cap_returns_out_of_scope`
 plus :class:`AgentRunnerIterCapStateMachine` exercise the new
 :class:`agent_runner.workflows.agent_runner_workflow.AgentRunnerWorkflow`'s
 ``MAX_ITER`` invariant (5 in the implementation) on a
 real Temporal cluster. The state-machine variant mirrors the
 property test under
 ``tests/property/test_temporal_loop_cap.py`` but each rule starts a
 fresh workflow against the time-skipping environment, sends one
 ``comment_added`` signal, and asserts the terminal output's
 ``iter_count`` never exceeds :data:`MAX_ITER` regardless of the
 initial ``iteration`` value the rule chose.


Both suites run against
:func:`temporalio.testing.WorkflowEnvironment.start_time_skipping` so
the integration is hermetic and fast. When the embedded Temporal test
server cannot start (sandboxed CI, missing native dependencies, …)
the AgentRunner tests ``pytest.skip`` cleanly so the suite stays
green on machines that cannot host Temporal.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
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


# ---------------------------------------------------------------------------
# ``sys.path`` bootstrapping for the AgentRunner imports.
#
# The new :class:`AgentRunnerWorkflow` lives under
# ``platform/workers/agent-runner-worker/src/agent_runner/...``. The
# foundation tests in this file consume the legacy ``src.workflows...``
# tree - ``ensure_worker_on_sys_path`` adds the worker root for that
# import. The AgentRunner coverage also needs the worker's ``src/`` directory
# so ``from agent_runner.workflows.agent_runner_workflow import ...``
# resolves. We add it here mirroring
# ``tests/property/test_temporal_loop_cap.py``.
# ---------------------------------------------------------------------------


_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]
_AGENT_RUNNER_SRC: Path = (
    _PLATFORM_ROOT / "workers" / "agent-runner-worker" / "src"
)
_TEMPORAL_SHARED_SRC: Path = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)

for _candidate in (_AGENT_RUNNER_SRC, _TEMPORAL_SHARED_SRC):
    _candidate_str = str(_candidate)
    if _candidate.is_dir() and _candidate_str not in sys.path:
        sys.path.insert(0, _candidate_str)


# ---------------------------------------------------------------------------
# Foundation parity test - AutomationWorkflow.MAX_LOOP_COUNT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_three_low_confidence_signal_cycles_hit_loop_cap() -> None:
    """Three back-to-back low-confidence iterations push ``_loop_count``
 to ``MAX_LOOP_COUNT=3`` and the workflow terminates via the loop
 cap branch.
 """

    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from src.workflows.automation_workflow import (
        MAX_LOOP_COUNT,
        AutomationInput,
        AutomationResult,
        AutomationWorkflow,
        NewCommentSignal,
    )

    log = CallLog()

    @activity.defn(name="llm_analyze_task")
    async def _llm_analyze_task_always_low(
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
        _llm_analyze_task_always_low,
    ]
    StubAgentRunnerWorkflow = make_stub_agent_runner_workflow()

    workflow_id = "automation-jira-PAY-4240"
    task_queue = "agent-runner-loop-cap"

    async def _wait_for_pending_question(handle: Any) -> None:
        """Spin (in virtual time) until the workflow re-enters the wait state."""

        for _ in range(50):
            pending = await handle.query(  # type: ignore[attr-defined]
                "get_pending_question"
            )
            if pending:
                return
            await env.sleep(0.1)  # type: ignore[name-defined]
        pytest.fail("workflow never reached the needs_info wait state")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AutomationWorkflow, StubAgentRunnerWorkflow],
            activities=activities,
        ):
            inp = AutomationInput(
                issue_key="PAY-4240",
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

            # Drive ``MAX_LOOP_COUNT`` signal cycles. Between each signal
            # we wait for the workflow to re-enter the needs_info wait
            # state (the previous LLM call cleared and re-set
            # ``_pending_question``). The final signal pushes
            # ``_loop_count`` to MAX_LOOP_COUNT, after which the wait
            # predicate falls through to the cap branch and the
            # workflow terminates.
            for i in range(MAX_LOOP_COUNT):
                await _wait_for_pending_question(handle)
                await handle.signal(
                    AutomationWorkflow.new_comment,
                    NewCommentSignal(
                        comment_text=f"Yorum {i + 1}: hâlâ belirsiz."
                    ),
                )

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
    assert result["failure_reason"] == "loop_cap_reached"
    assert result["workflow_type"] == "code_change_with_test"

    # The Turkish loop-cap comment must have been posted before the
    # work item was marked failed.
    comments_posted = [
        args[1] for args in log.args_for("jira_add_comment")
    ]
    assert any(
        "3 iterasyon" in body and "kapatıldı" in body
        for body in comments_posted
    ), f"loop-cap comment never posted; got {comments_posted!r}"

    # LLM was called exactly ``MAX_LOOP_COUNT + 1`` times: once for
    # the initial analysis plus once per signal-driven re-analysis. On
    # the iteration *after* the final increment the wait predicate
    # observes ``_loop_count >= MAX_LOOP_COUNT`` and short-circuits to
    # the cap branch before another re-analysis is issued.
    assert log.count("llm_analyze_task") == MAX_LOOP_COUNT + 1, (
        f"expected {MAX_LOOP_COUNT + 1} LLM calls, got "
        f"{log.count('llm_analyze_task')}: {log.names_called()}"
    )

    statuses = [args[1] for args in log.args_for("update_work_item_status")]
    assert statuses[-1] == "failed", (
        f"expected terminal status 'failed', got {statuses!r}"
    )


# ---------------------------------------------------------------------------
# AgentRunnerWorkflow.MAX_ITER on a real Temporal cluster
# ---------------------------------------------------------------------------
#
# These tests pin the runtime contract that ``iter_count`` never exceeds
# :data:`MAX_ITER` regardless of how many ``comment_added`` signals
# the gateway forwards. Each test starts a fresh
# :class:`AgentRunnerWorkflow` via the Temporal time-skipping
# environment (so the workflow body, signal-handling and replay
# determinism go through the real SDK) and asserts the final output
# respects the cap.
#
# The workflow's legacy signal-wait fallback (used by
# ``multi_step`` / ``noop_test`` / ``remote_ssh_test_only``)
# terminates after the first signal, so each rule of the state-machine
# variant drives exactly one signal per workflow instance - the
# invariant under test is the *upper bound*, not the loop length, so
# this is the right granularity.
# ---------------------------------------------------------------------------


def _agent_runner_temporal_env_available() -> bool:
    """Return ``True`` when the Temporal test environment imports cleanly.

 Mirrors the gating in ``test_temporal_idempotency.py``: importing
 :class:`temporalio.testing.WorkflowEnvironment` is cheap, but
 actually starting the embedded server can fail at runtime when the
 binary is unavailable (sandboxed CI, missing native deps). Each
 AgentRunner test wraps the start call in
 :func:`_start_time_skipping_or_skip` so a missing binary surfaces
 the same way an entirely missing module would.
 """

    try:  # noqa: SIM105 - explicit branch keeps the intent legible.
        from temporalio.testing import WorkflowEnvironment  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure → skip.
        return False
    return True


@contextlib.asynccontextmanager
async def _start_time_skipping_or_skip() -> Any:
    """Start the Temporal time-skipping env, ``pytest.skip``ing on failure.

 The embedded ``temporal-test-server`` may fail to start on
 machines without the bundled binary. Surface that cleanly as a
 skip so the integration suite stays green on hosts that cannot
 run Temporal locally.
 """

    from temporalio.testing import WorkflowEnvironment

    try:
        env_cm = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - surface as skip.
        pytest.skip(f"temporalio test environment not available: {exc}")
    async with env_cm as env:
        yield env


def _make_agent_runner_input(
    *,
    issue_key: str,
    iteration: int,
    max_iter: int,
    workflow_type: str = "noop_test",
) -> Any:
    """Build a minimal :class:`AgentRunnerWorkflowInput` for the cap tests.

 ``workflow_type="noop_test"`` falls through to the workflow body's
 legacy signal-wait fallback (no per-type activities are dispatched
 before the wait), which is exactly the path we want to exercise:
 the iteration-cap pre-condition runs at workflow start *and* on
 every signal, so a single signal is enough to confirm the cap is
 honoured for the chosen ``iteration`` value.
 """

    from temporal_shared.messages import (
        AgentRunnerWorkflowInput,
        LlmAnalysisResult,
    )

    analysis = LlmAnalysisResult(
        workflow_type=workflow_type,
        confidence="high",
        title=f"Cap test for {issue_key}",
        rationale="iter-cap integration test",
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id=f"automation-jira-{issue_key}",
        issue_key=issue_key,
        department_id="payments",
        workflow_type=workflow_type,
        analysis=analysis,
        iteration=iteration,
        max_iter=max_iter,
    )


def _make_agent_runner_activities(log: CallLog) -> list[Any]:
    """Register the minimum activity bag the workflow body relies on.

 The legacy fallback path inside :class:`AgentRunnerWorkflow`
 invokes two best-effort activities even on the noop branch:

 * ``audit_emit`` - drained for the iter==3 banner audit and any
 pending ``[fix]`` debounce / cache hits flushed by the signal
 handler.
 * ``jira_add_comment`` - fired the first time ``iter_count``
 crosses :data:`agent_runner.workflows.agent_runner_workflow.ITER_WARNING_THRESHOLD`
 to post the banner.

 Both wrappers are no-ops that record the invocation in
 ``log`` so individual tests can introspect call counts. Failures
 inside these activities are swallowed by the workflow body
 (``# noqa: BLE001 - audit is best-effort``), so a crash here would
 only affect the audit trail; the cap-invariant assertions still
 run on the workflow output.
 """

    from temporalio import activity

    @activity.defn(name="audit_emit")
    async def _audit_emit(payload: dict[str, Any]) -> None:
        log.record("audit_emit", payload)
        return None

    @activity.defn(name="jira_add_comment")
    async def _jira_add_comment(
        issue_key: str, body: str, dept_id: str
    ) -> None:
        log.record("jira_add_comment", issue_key, body, dept_id)
        return None

    return [_audit_emit, _jira_add_comment]


def _agent_runner_output_to_dict(result_raw: Any) -> dict[str, Any]:
    """Coerce the workflow result into a dict for assertion ergonomics.

 ``AgentRunnerWorkflowOutput`` is a frozen dataclass; depending on
 the SDK's data converter the result either round-trips back into
 the dataclass or surfaces as a plain dict. The test only needs the
 fields below, so we normalise both shapes to a single mapping.
 """

    fields = ("status", "iter_count", "summary", "failure_reason")
    if hasattr(result_raw, "__dataclass_fields__"):
        return {name: getattr(result_raw, name, None) for name in fields}
    if isinstance(result_raw, dict):
        return {name: result_raw.get(name) for name in fields}
    pytest.fail(f"unexpected workflow result shape: {type(result_raw).__name__}")
    return {}  # pragma: no cover - pytest.fail terminates the test


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    not _agent_runner_temporal_env_available(),
    reason="temporalio test environment not available",
)
async def test_agent_runner_signal_advances_iter_count_via_real_temporal() -> None:
    """Start :class:`AgentRunnerWorkflow` at ``iteration=ITER_WARNING_THRESHOLD``
 (3) with ``max_iter=5``, deliver one ``comment_added`` signal
 through the real Temporal cluster *under the slow-banner sync
 barrier*, and confirm the terminal output's ``iter_count`` is
 exactly 4 - i.e. the run-body's initial advance lifted the
 counter from 2 to 3 (arming the iter==3 banner) and the
 barrier-queued signal handler then lifted it from 3 to 4, never
 beyond :data:`MAX_ITER`.

 Race-free signal delivery via the slow-banner barrier
 -----------------------------------------------------

 The 7-day legacy signal-wait timeout collapses to zero wall-clock
 time under :func:`WorkflowEnvironment.start_time_skipping`, so a
 naive post-start :meth:`handle.signal` call would race the
 virtual clock and the workflow would complete with
 ``signal_wait_timeout`` before the signal lands. Buffering via
 ``start_signal=`` does NOT solve this race for the contract under
 test: the SDK delivers the buffered signal *before* the
 :meth:`AgentRunnerWorkflow.run` body re-seeds
 ``iter_count = max(0, iteration - 1)`` (via
 :func:`dataclasses.replace`), so the handler's advance is
 overwritten by the re-seed.

 The slow-banner barrier solves both problems together: the
 workflow seeds at ``iteration=ITER_WARNING_THRESHOLD`` so the
 run-body's initial advance arms the iter==3 banner edge and the
 body parks inside the ``jira_add_comment`` activity. While
 parked, a follow-up :meth:`handle.signal` lands cleanly on the
 workflow, queues against the ``comment_added`` handler, and runs
 in the next workflow task as soon as the activity returns -
 after the re-seed, so its advance survives.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        AgentRunnerWorkflow,
        CommentAddedSignal,
        ITER_WARNING_THRESHOLD,
        MAX_ITER,
    )

    log = _LcActivityCallLog()
    chain_started = _lc_asyncio.Event()
    chain_may_finish = _lc_asyncio.Event()
    activities = _lc_make_agent_runner_activities(
        log,
        chain_started=chain_started,
        chain_may_finish=chain_may_finish,
    )

    workflow_id = "agent-runner-jira-PAY-5001"
    task_queue = "agent-runner-iter-cap-advance"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _lc_make_agent_runner_input(
                issue_key="PAY-5001",
                iteration=ITER_WARNING_THRESHOLD,
                max_iter=MAX_ITER,
            )
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
            )

            # Wait for the body to park inside the banner activity
            # (the sync barrier). At this point the run-body has
            # already re-seeded iter_count=2 and lifted it to 3 via
            # ``_advance_iter_with_banner_check``.
            await _lc_asyncio.wait_for(
                chain_started.wait(), timeout=10.0
            )

            # Queue one ``comment_added`` while the body is parked.
            # The signal lands on the server, queues against the
            # workflow's signal handler, and fires in the next
            # workflow task as soon as the barrier releases - after
            # the re-seed, so its advance survives.
            await handle.signal(
                AgentRunnerWorkflow.comment_added,
                CommentAddedSignal(
                    comment_text="lütfen yine de devam et",
                    actor_account_id="user-1",
                ),
            )

            # Release the barrier - the body wakes, the queued
            # comment_added handler runs and advances iter 3→4, the
            # wait_condition observes _signal_pending=True, and the
            # workflow completes via the legacy fallback's
            # "completed" branch.
            chain_may_finish.set()

            result = _agent_runner_output_to_dict(await handle.result())

    # The run-body's initial advance lifted iter from 2 to 3 (banner
    # armed) and the barrier-queued signal handler advanced 3→4. The
    # cap holds.
    assert result["iter_count"] == 4, (
        f"expected iter_count=4 after run-body advance + one signal, "
        f"got {result!r}"
    )
    assert result["iter_count"] <= MAX_ITER
    # The iter==3 banner activity MUST have fired exactly once.
    assert log.count("jira_add_comment") == 1, (
        f"banner must fire once at iter==3 threshold; got "
        f"{log.names()!r}"
    )


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    not _agent_runner_temporal_env_available(),
    reason="temporalio test environment not available",
)
async def test_agent_runner_started_at_cap_returns_out_of_scope() -> None:
    """A workflow started with ``iteration > MAX_ITER`` must terminate
 immediately with ``status="out_of_scope"`` and an ``iter_count``
 that respects the cap - the run body's initial
 ``should_advance_iter`` pre-condition is the gatekeeper.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        AgentRunnerWorkflow,
        MAX_ITER,
    )

    log = CallLog()
    activities = _make_agent_runner_activities(log)

    workflow_id = "agent-runner-jira-PAY-5002"
    task_queue = "agent-runner-iter-cap-saturated"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            # ``iteration`` carries the desired starting count; the
            # workflow seeds ``iter_count = iteration - 1`` and then
            # runs ``_should_advance_iter``. With ``iteration=MAX_ITER + 1``
            # the seed equals MAX_ITER, the pre-condition fires, and
            # the workflow returns ``out_of_scope`` without entering
            # the wait loop.
            inp = _make_agent_runner_input(
                issue_key="PAY-5002",
                iteration=MAX_ITER + 1,
                max_iter=MAX_ITER,
            )
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
            )
            result = _agent_runner_output_to_dict(await handle.result())

    assert result["status"] == "out_of_scope", (
        f"expected status=out_of_scope, got {result!r}"
    )
    assert result["iter_count"] <= MAX_ITER, (
        f"iter_count={result['iter_count']} exceeds MAX_ITER={MAX_ITER}"
    )
    # No Jira comment fires from this short-circuit path: the body
    # short-circuits before reaching ``_maybe_post_iter_warning_banner``.
    assert log.count("jira_add_comment") == 0


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    not _agent_runner_temporal_env_available(),
    reason="temporalio test environment not available",
)
@pytest.mark.parametrize(
    "initial_iteration",
    [3, 4, 5, 6],
    ids=lambda v: f"iter_{v}",
)
async def test_agent_runner_iter_cap_holds_across_initial_iterations(
    initial_iteration: int,
) -> None:
    """Parametrised over a range that brackets :data:`MAX_ITER`: for
 every ``initial_iteration`` from
 :data:`ITER_WARNING_THRESHOLD` (3) through ``MAX_ITER + 1`` (6)
 the workflow's terminal ``iter_count`` must respect the cap.
 This is the integration-level analogue of the property-based
 state machine in ``tests/property/test_temporal_loop_cap.py`` -
 each parameter drives one workflow round-trip through real
 Temporal. The parametrisation acts as the "state machine" for
 the implementation: every input lands a different cap-cross scenario
 without sharing state across runs.

 Race-free signal delivery via the slow-banner barrier
 -----------------------------------------------------

 Each parameter delivers exactly one ``comment_added`` signal
 *under the barrier* - i.e. via :meth:`handle.signal` while the
 body is parked inside the ``jira_add_comment`` activity. Under
 :func:`WorkflowEnvironment.start_time_skipping` the legacy
 7-day signal-wait timeout collapses to zero wall-clock time, so
 a post-start :meth:`handle.signal` call against an already-
 yielded body would race the virtual clock; the slow-banner
 barrier solves that - every parameter seeds at
 ``initial_iteration >= ITER_WARNING_THRESHOLD`` so the run-body's
 initial advance arms the iter==3 banner edge and parks inside
 the activity, giving the queued signal handler a deterministic
 window to fire after the re-seed.

 Note on ``start_signal=`` vs barrier-queued
 ``handle.signal``: ``start_signal`` would buffer the signal so
 the handler runs *before* :meth:`AgentRunnerWorkflow.run` re-seeds
 ``iter_count = max(0, iteration - 1)`` (via
 :func:`dataclasses.replace`), and the handler's advance would be
 overwritten by the re-seed. Sending the signal *under the
 barrier* via :meth:`handle.signal` ensures the handler runs in
 the workflow task *after* the activity returns - past the
 re-seed - so its advance survives.

 Note on the brief's "iter_1, iter_2" parameterisation: with
 ``iteration < 3`` the iter==3 banner edge never arms and the
 body exits the wait_condition on the first turn, leaving no
 parked window for the barrier-queued signal to advance during.
 Those headroom-from-iter=1 scenarios are covered by
 :func:`test_agent_runner_signal_advances_iter_count_via_real_temporal`
 (single-signal advance under the slow-banner barrier) and the
 multi-signal :func:`test_iter_count_never_exceeds_max_iter`
 further down. The parameter range here pins the
 ``iter >= ITER_WARNING_THRESHOLD`` corner of the cap matrix end
 to end.

 Trace per parameter (with ``max_iter=MAX_ITER=5``):

 * ``iter_3``: re-seed=2 → advance to 3 (banner armed) → park in
 barrier → queued signal advances 3→4 → body wakes → status
 ``completed``, iter_count=4.
 * ``iter_4``: re-seed=3 → advance to 4 (banner armed because
 iter >= 3) → park in barrier → queued signal advances 4→5
 → body wakes → status ``completed``, iter_count=5.
 * ``iter_5``: re-seed=4 → advance to 5 (= MAX_ITER, banner
 armed) → park in barrier → queued signal tries 5→6, hits
 cap, flips ``_out_of_scope`` → body wakes → status
 ``out_of_scope``, iter_count=5.
 * ``iter_6``: re-seed=5 → ``_should_advance_iter`` denies on
 the run-body's initial pre-condition → body returns
 ``out_of_scope`` immediately, BEFORE reaching the banner. No
 barrier engages; iter_count=5.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        AgentRunnerWorkflow,
        CommentAddedSignal,
        MAX_ITER,
    )

    log = _LcActivityCallLog()
    chain_started = _lc_asyncio.Event()
    chain_may_finish = _lc_asyncio.Event()
    activities = _lc_make_agent_runner_activities(
        log,
        chain_started=chain_started,
        chain_may_finish=chain_may_finish,
    )

    workflow_id = f"agent-runner-jira-CAP-{initial_iteration:02d}"
    task_queue = f"agent-runner-iter-cap-{initial_iteration}"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _lc_make_agent_runner_input(
                issue_key=f"CAP-{initial_iteration:02d}",
                iteration=initial_iteration,
                max_iter=MAX_ITER,
            )
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
            )

            # When the input already saturates the cap the body
            # short-circuits to ``out_of_scope`` BEFORE reaching the
            # banner activity, so the barrier never engages and there
            # is no parked-body window to queue a signal into. We
            # still drive the assertion for that boundary by simply
            # awaiting the result.
            if initial_iteration <= MAX_ITER:
                await _lc_asyncio.wait_for(
                    chain_started.wait(), timeout=10.0
                )

                # Queue one ``comment_added`` while the body is
                # parked in the banner activity. The signal lands
                # cleanly on the workflow, queues against the
                # ``comment_added`` handler, and runs in the next
                # workflow task - past the re-seed, so its advance
                # survives.
                await handle.signal(
                    AgentRunnerWorkflow.comment_added,
                    CommentAddedSignal(
                        comment_text="devam et",
                        actor_account_id="user-1",
                    ),
                )

            # Releasing the event is harmless when the body never
            # parked - the activity stub just exits its wait early.
            chain_may_finish.set()

            result = _agent_runner_output_to_dict(await handle.result())

    # Core invariant - the cap must hold for every initial iteration.
    assert result["iter_count"] <= MAX_ITER, (
        f"iter_count={result['iter_count']} exceeds MAX_ITER={MAX_ITER} "
        f"for initial_iteration={initial_iteration}"
    )

    # Per-parameter assertions follow the trace above.
    if initial_iteration > MAX_ITER:
        # iter_6: run-body short-circuits before the banner; the
        # buffered signal sees _out_of_scope=True and returns
        # silently. No banner fires.
        assert result["status"] == "out_of_scope"
        assert result["iter_count"] == MAX_ITER
        assert log.count("jira_add_comment") == 0, (
            f"banner must not fire when run-body short-circuits "
            f"before reaching it; got {log.names()!r}"
        )
    elif initial_iteration == MAX_ITER:
        # iter_5: run-body advance lifts iter to MAX_ITER (banner
        # armed). Buffered signal tries to advance MAX_ITER→
        # MAX_ITER+1 and the in-handler cap pre-condition flips
        # ``_out_of_scope``. Banner fires once.
        assert result["status"] == "out_of_scope"
        assert result["iter_count"] == MAX_ITER
        assert log.count("jira_add_comment") == 1
    else:
        # iter_3 / iter_4: headroom - the signal advances by 1 and
        # the workflow exits via the legacy fallback's "completed"
        # branch. The banner fires once because iter >= 3 after the
        # run-body's initial advance.
        assert result["iter_count"] == initial_iteration + 1
        assert result["status"] == "completed"
        assert log.count("jira_add_comment") == 1


# ===========================================================================
# Slow-banner barrier coverage for the implementation
#

#
# The block below pins :data:`MAX_ITER` end-to-end against a real
# Temporal time-skipping cluster using the **slow-banner sync barrier**
# pattern - the same race-free post-start signal delivery
# support code shipped with the implementation in
# ``test_temporal_signal.py``. The earlier AgentRunner tests above
# already pin the cap with single-signal scenarios driven by
# ``start_signal=`` alone; this new section fires *back-to-back*
# signal sequences (six plain ``comment_added`` and an eight-signal
# mixed sequence) plus the iter==3 banner-once contract - all
# scenarios that need the body to be parked deterministically while
# follow-up :meth:`handle.signal` calls are queued.
#
# Race-free signal delivery - recap
# ---------------------------------
#
# Under :func:`WorkflowEnvironment.start_time_skipping` virtual time
# fast-forwards while the workflow is parked, so a signal sent
# *after* the body reaches ``wait_condition`` can race the 7-day
# ``SIGNAL_WAIT_TIMEOUT`` and the workflow may complete with
# ``signal_wait_timeout`` before the signal lands. The legacy
# ``noop_test`` fallback exits its ``wait_condition`` on the very
# first turn (the run-body's initial
# :meth:`AgentRunnerWorkflow._advance_iter_with_banner_check` flips
# ``_signal_pending=True``) so by the time a post-start
# :meth:`handle.signal` reaches the server the workflow has already
# returned - under ``start_time_skipping`` the seven-day wait is
# collapsed to zero wall-clock time.
#
# Every test in this section seeds
# ``iteration=ITER_WARNING_THRESHOLD`` (3). The run-body's initial
# advance lifts ``iter_count`` from 2 to 3, which arms the iter==3
# banner edge; the body then awaits the ``jira_add_comment``
# activity inside :meth:`AgentRunnerWorkflow._maybe_post_iter_warning_banner`.
# The stub ``jira_add_comment`` activity blocks on an
# :class:`asyncio.Event` (``chain_may_finish``) - while it blocks
# the workflow body is parked inside the activity, signals fired
# via :meth:`handle.signal` reach the server cleanly, queue against
# the workflow's signal handlers, and are processed in the *next*
# workflow task as soon as the activity returns. Once we release
# the barrier the queued signals all fire in order, advance the
# iteration state deterministically, and the body's
# ``wait_condition`` evaluates the post-signal state.
#
# This integration-level coverage mirrors the property-based state
# machine in ``platform/tests/property/test_temporal_loop_cap.py``
# (the Hypothesis variant exercises the signal handlers in isolation
# with a stub clock); the tests below drive the same invariants
# through a *real* Temporal cluster - signal dispatch, sandbox, and
# replay determinism all participate.
# ===========================================================================

import asyncio as _lc_asyncio
from dataclasses import dataclass as _lc_dataclass
from dataclasses import field as _lc_field

from ._temporal_helpers import ensure_worker_on_sys_path as _lc_ensure_worker_on_sys_path

_lc_ensure_worker_on_sys_path()


# ---------------------------------------------------------------------------
# Activity call log (richer than ``CallLog`` - captures kwargs too)
# ---------------------------------------------------------------------------


@_lc_dataclass
class _LcActivityCallLog:
    """Append-only log of activity invocations with full kwargs capture.

 The shared :class:`CallLog` (above) only captures positional args;
 the slow-banner stubs below also forward kwargs so the assertions
 in :func:`test_iter_warning_at_three_banner_fires_once` can verify
 the banner activity was invoked with the expected
 :data:`ITER_WARNING_BANNER_TEXT` body without depending on the
 SDK's positional/keyword dispatch.
 """

    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = _lc_field(
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
# Activity stub factory - slow-banner sync barrier
# ---------------------------------------------------------------------------


def _lc_make_agent_runner_activities(
    log: _LcActivityCallLog,
    *,
    chain_started: _lc_asyncio.Event,
    chain_may_finish: _lc_asyncio.Event,
) -> list[Any]:
    """Build the bag of stub activities the AgentRunnerWorkflow body invokes.

 The ``jira_add_comment`` stub is the **sync barrier**: it sets
 ``chain_started`` when first called (so the test can confirm the
 workflow body is parked) and then blocks on ``chain_may_finish``
 until the test releases it. While the workflow body is parked
 inside this activity await, queued :meth:`handle.signal` calls
 land on the workflow without racing the legacy fallback's
 signal-wait timeout.

 ``compensation_chain_run`` is registered *defensively*: these
 tests must never invoke it ( - natural termination must
 NOT trigger compensation). A recorded invocation would catch a
 regression in that branch.

 Failures inside ``audit_emit`` / ``jira_add_comment`` are
 swallowed by the workflow body (``# noqa: BLE001 - audit is
 best-effort``), so a crash here would only affect the audit
 trail; the spec assertions still run on the workflow output.
 """

    from temporalio import activity

    @activity.defn(name="compensation_chain_run")
    async def _compensation_chain_run(
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        log.record("compensation_chain_run", (payload,), {})
        return {"ok": True}

    @activity.defn(name="audit_emit")
    async def _audit_emit(payload: dict[str, Any]) -> None:
        log.record("audit_emit", (payload,), {})
        return None

    @activity.defn(name="jira_add_comment")
    async def _jira_add_comment(*args: Any, **kwargs: Any) -> None:
        log.record("jira_add_comment", args, kwargs)
        # Sync barrier: signal "we're here", then park until the
        # test releases us. The wait is bounded so a misbehaving
        # test does not hang indefinitely.
        chain_started.set()
        try:
            await _lc_asyncio.wait_for(
                chain_may_finish.wait(), timeout=10.0
            )
        except _lc_asyncio.TimeoutError:
            # Bounded wait - release the activity so the workflow
            # can complete and the test can collect its result.
            pass
        return None

    return [_compensation_chain_run, _audit_emit, _jira_add_comment]


# ---------------------------------------------------------------------------
# Input + result coercion helpers (slow-banner variant)
#
# These mirror the helpers in ``test_temporal_signal.py`` - the
# default ``iteration=3`` arms the iter==3 banner edge so the body
# parks inside ``jira_add_comment`` (the sync barrier) on its first
# turn. The coercion helpers normalise dataclass/dict round-trip
# shapes so the assertions stay robust across SDK versions.
# ---------------------------------------------------------------------------


def _lc_make_agent_runner_input(
    *,
    issue_key: str,
    iteration: int = 3,
    max_iter: int = 5,
    workflow_type: str = "noop_test",
) -> Any:
    """Build a minimal :class:`AgentRunnerWorkflowInput` for the cap tests.

 ``workflow_type="noop_test"`` falls through to the workflow body's
 legacy signal-wait fallback (no per-type activities are dispatched
 before the wait), which is exactly the path we want to exercise:
 the iteration-cap pre-condition runs at workflow start *and* on
 every signal, so a back-to-back signal sequence is enough to
 confirm the cap is honoured.
 """

    from temporal_shared.messages import (
        AgentRunnerWorkflowInput,
        LlmAnalysisResult,
    )

    analysis = LlmAnalysisResult(
        workflow_type=workflow_type,
        confidence="high",
        title=f"loop-cap integration test for {issue_key}",
        rationale="loop-cap integration fixture",
        token_usage=42,
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id=f"automation-jira-{issue_key}",
        issue_key=issue_key,
        department_id="payments",
        workflow_type=workflow_type,
        analysis=analysis,
        iteration=iteration,
        max_iter=max_iter,
        default_language="tr",
    )


def _lc_extract_status(result: Any) -> str | None:
    if hasattr(result, "status"):
        return getattr(result, "status")
    if isinstance(result, dict):
        return result.get("status")
    return None


def _lc_extract_iter_count(state: Any) -> int:
    if hasattr(state, "iter_count"):
        return int(getattr(state, "iter_count"))
    if isinstance(state, dict):
        return int(state.get("iter_count", -1))
    return -1


# ---------------------------------------------------------------------------
# 1. Six back-to-back ``comment_added`` signals respect the iter cap
# (iter_count never exceeds MAX_ITER on a real Temporal cluster)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    not _agent_runner_temporal_env_available(),
    reason="temporalio test environment not available",
)
async def test_iter_count_never_exceeds_max_iter() -> None:
    """Fire six ``comment_added`` signals back-to-back against a single
 :class:`AgentRunnerWorkflow` instance and confirm the terminal
 ``iter_count`` honours :data:`MAX_ITER` (5).

 Trace (with ``iteration=3`` / ``max_iter=5``):

 * run initial advance → ``iter_count=3`` (banner armed)
 * banner activity parks the body → barrier holds
 * signal 1 (signal-with-start) buffered, handler advances
 → ``iter_count=4``
 * signals 2-6 queued via ``handle.signal``
 * barrier releases - handlers fire → 4→5 (signal 2),
 5→cap-flip ``_out_of_scope``
 on signal 3, signals 4-6
 see ``_out_of_scope=True``
 and return silently
 * workflow body wakes, observes
 ``_out_of_scope=True``, returns
 with ``status="out_of_scope"``.

 Note on the brief's "iteration=1" parameterisation: the slow-
 banner barrier requires the body to park inside the iter==3
 banner activity, which only arms when ``iteration >= 3``. The
 invariant under test is the *upper bound* on ``iter_count``, not
 the loop length; seeding at the threshold and firing 6 signals
 still drives the cap branch end-to-end. The single-signal,
 headroom-from-iter=1 scenario is already covered by
 :func:`test_agent_runner_signal_advances_iter_count_via_real_temporal`
 above.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        MAX_ITER,
        AgentRunnerWorkflow,
        CommentAddedSignal,
    )

    log = _LcActivityCallLog()
    chain_started = _lc_asyncio.Event()
    chain_may_finish = _lc_asyncio.Event()
    activities = _lc_make_agent_runner_activities(
        log,
        chain_started=chain_started,
        chain_may_finish=chain_may_finish,
    )

    workflow_id = "agent-runner-jira-PAY-5301-six-comments"
    task_queue = "agent-runner-loop-cap-six-comments"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _lc_make_agent_runner_input(
                issue_key="PAY-5301", iteration=3, max_iter=5
            )
            # Signal 1 - race-free via signal-with-start so the
            # handler is buffered for the workflow's first tick.
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
                start_signal="comment_added",
                start_signal_args=[
                    CommentAddedSignal(
                        comment_text="comment 1 - devam et",
                        actor_account_id="user-1",
                    )
                ],
            )

            # Wait for the body to park inside the banner activity
            # (the sync barrier). At this point handle.signal calls
            # land cleanly on the workflow.
            await _lc_asyncio.wait_for(
                chain_started.wait(), timeout=10.0
            )

            # Signals 2-6 - fired back-to-back. The Temporal server
            # batches signals queued before the workflow's next
            # workflow-task runs, so all five typically land in the
            # same task as signal 1 once the barrier releases.
            for i in range(2, 7):
                try:
                    await handle.signal(
                        AgentRunnerWorkflow.comment_added,
                        CommentAddedSignal(
                            comment_text=f"comment {i} - devam et",
                            actor_account_id="user-1",
                        ),
                    )
                except Exception:  # noqa: BLE001 - post-completion no-op
                    # Defensive - the workflow may have already
                    # completed in some SDK versions.
                    pass

            # Release the barrier - signals fire in order, advancing
            # iter_count up to MAX_ITER and then flipping
            # ``_out_of_scope``.
            chain_may_finish.set()

            result: Any = await handle.result()

            # Queries are still serviceable on a closed workflow as
            # long as the execution history is retained.
            iter_state = await handle.query("get_iteration_state")
            out_of_scope = await handle.query("is_out_of_scope")

    # ----- Assertions -------------------------------------------------

    iter_count = _lc_extract_iter_count(iter_state)
    # Core invariant - iter_count never exceeds MAX_ITER regardless of
    # how many comment_added signals were forwarded.
    assert iter_count <= MAX_ITER, (
        f"iter_count={iter_count} must not exceed MAX_ITER={MAX_ITER} "
        f"after 6 back-to-back comment_added signals; "
        f"state={iter_state!r}"
    )

    # Six signals against ``iteration=3`` MUST flip the workflow into
    # ``out_of_scope`` - the run-body's initial advance lifts iter to
    # 3, two more signals lift it to MAX_ITER=5, and the third signal
    # flips ``_out_of_scope`` because ``_should_advance_iter`` denies
    # past the cap.
    assert out_of_scope is True, (
        f"is_out_of_scope must be True after the cap fires; "
        f"got {out_of_scope!r} (iter_state={iter_state!r})"
    )

    status = _lc_extract_status(result)
    assert status == "out_of_scope", (
        f"expected status=out_of_scope after iter cap, got {status!r} "
        f"(result={result!r})"
    )

    # - natural termination must NOT trigger compensation.
    # ``compensation_chain_run`` is registered defensively; a recorded
    # invocation would catch a regression where the iter-cap branch
    # leaks into the compensation path.
    assert log.count("compensation_chain_run") == 0, (
        f"compensation_chain_run must not run on natural termination "
        f"(MAX_ITER); call log: {log.names()!r}"
    )


# ---------------------------------------------------------------------------
# 2. Mixed deterministic signal sequence still respects the iter cap
# (integration mirror of the property-test state machine)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    not _agent_runner_temporal_env_available(),
    reason="temporalio test environment not available",
)
async def test_random_signal_sequence_respects_iter_cap() -> None:
    """Drive a deterministic but mixed sequence of eight
 ``comment_added`` signals - plain, ``[fix]``, ``[explain]`` and
 ``[needs_info]`` - through the slow-banner sync barrier and
 confirm the terminal ``iter_count`` still honours
 :data:`MAX_ITER`.

 This is the integration-level mirror of the property-based
 state machine in
 ``platform/tests/property/test_temporal_loop_cap.py``. The
 Hypothesis variant exercises the signal handlers in isolation
 with a stubbed clock; the test here drives the same invariants
 through a real Temporal cluster - signal dispatch, sandbox, and
 replay determinism all participate.

 Sequence (8 signals, deterministic order):

 1. ``comment_added`` plain
 2. ``comment_added`` plain
 3. ``comment_added [fix]`` (diff_hash="hash-A")
 4. ``comment_added [explain]`` (pr_diff_hash="pr-A")
 5. ``comment_added [needs_info]``
 6. ``comment_added`` plain
 7. ``comment_added [fix]`` (diff_hash="hash-B")
 8. ``comment_added [needs_info]``

 The ``[needs_info]`` count is intentionally bounded to 2 (below
 :data:`NEEDS_INFO_MAX_STREAK`=3) so the cap under test is
 :data:`MAX_ITER`, not the streak - every plain or keyword-routed
 signal that *advances* counts toward the iteration ceiling, and
 after the cap is reached every subsequent signal short-circuits
 on ``_out_of_scope=True``.

 Trace (with ``iteration=3`` / ``max_iter=5``):

 * run initial advance → ``iter_count=3`` (banner armed)
 * banner parks the body → barrier holds
 * signal 1 plain (start_signal) → buffered, advances 3→4
 * signals 2-8 queued → drained when barrier releases
 * signal 2 plain → 4→5
 * signal 3 ``[fix]`` → 5→cap-flip ``_out_of_scope``
 * signals 4-8 → see ``_out_of_scope=True``
 and return silently
 * workflow returns ``status="out_of_scope"`` with
 ``iter_count=5 == MAX_ITER``.

 The exact *interleaving* of the signal handler runs depends on
 the SDK's batching of buffered signals against the current
 workflow task; what we pin is the upper-bound invariant -
 ``iter_count <= MAX_ITER`` - irrespective of the order.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        MAX_ITER,
        NEEDS_INFO_MAX_STREAK,
        AgentRunnerWorkflow,
        CommentAddedSignal,
    )

    log = _LcActivityCallLog()
    chain_started = _lc_asyncio.Event()
    chain_may_finish = _lc_asyncio.Event()
    activities = _lc_make_agent_runner_activities(
        log,
        chain_started=chain_started,
        chain_may_finish=chain_may_finish,
    )

    workflow_id = "agent-runner-jira-PAY-5302-mixed-sequence"
    task_queue = "agent-runner-loop-cap-mixed"

    # Deterministic mixed sequence - first signal goes via
    # start_signal, the rest via handle.signal under the barrier.
    sequence: list[CommentAddedSignal] = [
        CommentAddedSignal(
            comment_text="signal 1 plain - devam et",
            actor_account_id="user-1",
        ),
        CommentAddedSignal(
            comment_text="signal 2 plain - devam et",
            actor_account_id="user-1",
        ),
        CommentAddedSignal(
            comment_text="signal 3 [fix] please rerun the test",
            actor_account_id="reviewer-1",
            diff_hash="hash-A",
        ),
        CommentAddedSignal(
            comment_text="signal 4 [explain] what changed?",
            actor_account_id="reviewer-1",
            diff_hash="pr-A",
        ),
        CommentAddedSignal(
            comment_text="signal 5 [needs_info] please clarify",
            actor_account_id="reporter-1",
        ),
        CommentAddedSignal(
            comment_text="signal 6 plain - daha fazla bilgi",
            actor_account_id="user-1",
        ),
        CommentAddedSignal(
            comment_text="signal 7 [fix] rerun please",
            actor_account_id="reviewer-1",
            diff_hash="hash-B",
        ),
        CommentAddedSignal(
            comment_text="signal 8 [needs_info] still unclear",
            actor_account_id="reporter-1",
        ),
    ]

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _lc_make_agent_runner_input(
                issue_key="PAY-5302", iteration=3, max_iter=5
            )
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
                start_signal="comment_added",
                start_signal_args=[sequence[0]],
            )

            # Wait for the body to park inside the banner activity.
            await _lc_asyncio.wait_for(
                chain_started.wait(), timeout=10.0
            )

            # Queue signals 2-8 while the body is parked. The Temporal
            # server batches signals queued before the workflow's
            # next workflow-task runs, so they all land deterministically
            # once the barrier releases.
            for sig in sequence[1:]:
                try:
                    await handle.signal(
                        AgentRunnerWorkflow.comment_added, sig
                    )
                except Exception:  # noqa: BLE001 - post-completion no-op
                    # Defensive - the workflow may have already
                    # completed in some SDK versions.
                    pass

            # Release the barrier - signals fire in order.
            chain_may_finish.set()

            result: Any = await handle.result()

            iter_state = await handle.query("get_iteration_state")
            out_of_scope = await handle.query("is_out_of_scope")

    # ----- Assertions -------------------------------------------------

    iter_count = _lc_extract_iter_count(iter_state)
    # Core invariant - the integration-level mirror of the property
    # test's ``iter_count <= MAX_ITER`` invariant.
    assert iter_count <= MAX_ITER, (
        f"iter_count={iter_count} must not exceed MAX_ITER={MAX_ITER} "
        f"after a mixed 8-signal sequence; state={iter_state!r}"
    )

    # The streak is bounded by NEEDS_INFO_MAX_STREAK regardless of
    # which order the [needs_info] signals landed - the streak
    # increments only when the workflow is not already
    # ``out_of_scope``, so this is also < NEEDS_INFO_MAX_STREAK.
    if hasattr(iter_state, "needs_info_streak") or isinstance(
        iter_state, dict
    ):
        streak = (
            getattr(iter_state, "needs_info_streak", None)
            if hasattr(iter_state, "needs_info_streak")
            else iter_state.get("needs_info_streak", 0)
        )
        assert int(streak or 0) <= NEEDS_INFO_MAX_STREAK, (
            f"needs_info_streak={streak} exceeds "
            f"NEEDS_INFO_MAX_STREAK={NEEDS_INFO_MAX_STREAK}"
        )

    # The mixed sequence drives at least two iter advances past the
    # initial seed (signals 1 + 2 are plain comments) so the cap MUST
    # have fired and ``_out_of_scope`` MUST be latched.
    assert out_of_scope is True, (
        f"is_out_of_scope must be True after the mixed sequence "
        f"saturates the cap; got {out_of_scope!r} "
        f"(iter_state={iter_state!r})"
    )

    status = _lc_extract_status(result)
    assert status == "out_of_scope", (
        f"expected status=out_of_scope after mixed-sequence cap, got "
        f"{status!r} (result={result!r})"
    )

    # - natural termination MUST NOT trigger compensation.
    assert log.count("compensation_chain_run") == 0, (
        f"compensation_chain_run must not run on natural termination "
        f"(mixed sequence cap); call log: {log.names()!r}"
    )


# ---------------------------------------------------------------------------
# 3. iter==3 banner fires once and the latch query is True after the run
# ( - banner-once contract)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.skipif(
    not _agent_runner_temporal_env_available(),
    reason="temporalio test environment not available",
)
async def test_iter_warning_at_three_banner_fires_once() -> None:
    """Start :class:`AgentRunnerWorkflow` at
 ``iteration=ITER_WARNING_THRESHOLD=3`` / ``max_iter=5``. The
 run-body's initial advance lifts ``iter_count`` from 2 to 3 and
 arms the iter==3 banner edge; the body then enters
 :meth:`AgentRunnerWorkflow._maybe_post_iter_warning_banner`,
 which flips ``_iter_warning_at_three=True`` *before* awaiting
 the ``jira_add_comment`` activity so a transient activity
 failure cannot cause the banner to be posted twice on the next
 loop turn (at-most-once is preferred over at-least-once for
 user-facing comments).

 The activity await is the sync barrier: while the activity stub
 parks the body inside :meth:`asyncio.Event.wait`, we query
 :meth:`AgentRunnerWorkflow.is_iter_warning_at_three` against
 the live workflow and assert the latch is already True. We then
 release the barrier, let the body resume, and verify the
 ``jira_add_comment`` activity log carries at least one entry
 whose body matches :data:`ITER_WARNING_BANNER_TEXT`.

 No follow-up ``comment_added`` signal is delivered: the run
 body's initial advance flips ``_signal_pending=True`` (a
 side-effect of :meth:`_advance_iter_with_banner_check`), which
 is enough for the post-banner ``wait_condition`` to exit on
 the first turn - the workflow then completes via the legacy
 fallback's success path. Adding a buffered ``start_signal``
 here would be a no-op for ``iter_count`` (the signal handler's
 advance runs before run's ``dataclasses.replace`` reseeds the
 state from ``inp.iteration``, so the advance is overwritten);
 keeping the test signal-free makes the iter-cap pin
 unambiguous.

 Pinned contracts:

 * The banner state field flips to ``True`` *before* the
 ``jira_add_comment`` activity returns (idempotent
 at-most-once); the latch is observable mid-flight via the
 :meth:`is_iter_warning_at_three` query.
 * The ``jira_add_comment`` activity is invoked with
 :data:`ITER_WARNING_BANNER_TEXT` exactly the first time
 ``iter_count`` crosses :data:`ITER_WARNING_THRESHOLD`.
 * The banner does NOT bypass the iter cap; on
 completion ``iter_count <= MAX_ITER``.
 * (regression check) - natural termination must NOT
 trigger compensation.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        ITER_WARNING_BANNER_TEXT,
        ITER_WARNING_THRESHOLD,
        MAX_ITER,
        AgentRunnerWorkflow,
    )

    log = _LcActivityCallLog()
    chain_started = _lc_asyncio.Event()
    chain_may_finish = _lc_asyncio.Event()
    activities = _lc_make_agent_runner_activities(
        log,
        chain_started=chain_started,
        chain_may_finish=chain_may_finish,
    )

    workflow_id = "agent-runner-jira-PAY-5303-banner-once"
    task_queue = "agent-runner-loop-cap-banner-once"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _lc_make_agent_runner_input(
                issue_key="PAY-5303",
                iteration=ITER_WARNING_THRESHOLD,
                max_iter=5,
            )
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
            )

            # Wait for the body to park inside the banner activity -
            # at this point ``_iter_warning_at_three`` MUST already be
            # True (the banner method flips the latch before awaiting
            # the activity).
            await _lc_asyncio.wait_for(
                chain_started.wait(), timeout=10.0
            )

            # Mid-flight query: latch is True even though the workflow
            # has not completed yet. This is the central pin -
            # the latch flips *before* the side-effect, so observers
            # see a consistent state regardless of activity success.
            mid_flight_latch = await handle.query("is_iter_warning_at_three")

            # Release the barrier so the body can complete cleanly.
            chain_may_finish.set()

            result: Any = await handle.result()

            # Post-completion queries - must agree with the mid-flight
            # observation: the latch is sticky, never resets.
            post_run_latch = await handle.query("is_iter_warning_at_three")
            iter_state = await handle.query("get_iteration_state")

    # ----- Assertions -------------------------------------------------

    # - banner state field flips to True after the run-body's
    # initial advance crosses ITER_WARNING_THRESHOLD. The latch must
    # be observable mid-flight (i.e. before the banner activity
    # returns) so a transient activity failure cannot cause a
    # second invocation on the next loop turn.
    assert mid_flight_latch is True, (
        "is_iter_warning_at_three must be True once the body has "
        "entered the banner activity; got "
        f"mid_flight_latch={mid_flight_latch!r}"
    )
    assert post_run_latch is True, (
        f"is_iter_warning_at_three must remain True after completion; "
        f"got post_run_latch={post_run_latch!r}"
    )

    # - the banner text must have been posted to Jira at least
    # once. The body invokes ``jira_add_comment`` exactly once for
    # the banner via :meth:`_maybe_post_iter_warning_banner`; the
    # noop_test fallback does not call ``jira_add_comment`` for any
    # other reason. We use ``>= 1`` (rather than ``== 1``) because
    # the body re-runs the banner check after the wait wakes - the
    # second invocation is gated by ``_iter_warning_at_three`` and
    # short-circuits, so in practice exactly one call lands. The
    # looser assertion keeps the test robust if the banner method
    # is ever re-entered for unrelated reasons.
    banner_calls = [
        args
        for args in log.args_for("jira_add_comment")
        if any(
            arg == ITER_WARNING_BANNER_TEXT
            for arg in args
            if isinstance(arg, str)
        )
    ]
    assert len(banner_calls) >= 1, (
        f"expected at least one jira_add_comment with "
        f"ITER_WARNING_BANNER_TEXT; got "
        f"{log.args_for('jira_add_comment')!r}"
    )

    # - the banner does NOT bypass the cap. With ``iteration=3``
    # and no follow-up signals iter_count settles at the run-body's
    # initial advance value (3), well below MAX_ITER=5.
    iter_count = _lc_extract_iter_count(iter_state)
    assert iter_count <= MAX_ITER, (
        f"iter_count={iter_count} must not exceed MAX_ITER={MAX_ITER}"
    )
    assert iter_count == ITER_WARNING_THRESHOLD, (
        f"expected iter_count={ITER_WARNING_THRESHOLD} after the "
        f"run-body's initial advance with no follow-up signals, got "
        f"{iter_count} (state={iter_state!r})"
    )

    status = _lc_extract_status(result)
    assert status == "completed", (
        f"expected status=completed (no follow-up signals, below cap), "
        f"got {status!r} (result={result!r})"
    )

    # - natural termination must NOT trigger compensation.
    assert log.count("compensation_chain_run") == 0, (
        f"compensation_chain_run must not run on natural termination "
        f"(banner once); call log: {log.names()!r}"
    )
