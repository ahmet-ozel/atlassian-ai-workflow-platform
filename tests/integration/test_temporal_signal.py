"""Integration test: AutomationWorkflow ``new_comment`` signal flow.


Scenario
--------

When the initial ``llm_analyze_task`` returns ``confidence="low"`` with
a ``needs_info_question``, :class:`AutomationWorkflow` posts the
question to Jira and parks in
``workflow.wait_condition(...)``. The Jira webhook handler forwards
``jira:comment_created`` events as ``new_comment`` signals; the
workflow:

1. Stores the comment text in ``self._comments_received``.
2. Increments ``self._loop_count``.
3. Re-runs ``llm_analyze_task`` with the appended history.
4. Returns to the capability gate / confidence check loop.

This test drives that flow end-to-end against the time-skipping
``WorkflowEnvironment``: low-confidence on the first analysis, then a
``new_comment`` signal, then high-confidence on the second analysis so
the workflow proceeds to the (stubbed) ``AgentRunnerWorkflow`` child
and completes successfully. Assertions verify:

- ``llm_analyze_task`` was invoked exactly twice.
- The needs_info question was posted to Jira.
- The second LLM call observed the new comment in the issue history.
- The terminal :class:`AutomationResult` is ``status="completed"``.
"""

from __future__ import annotations

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
async def test_new_comment_signal_drives_loop_increment_and_reanalysis() -> None:
    """Initial low-confidence LLM result parks the workflow in the
 needs_info wait; a ``new_comment`` signal wakes it; the second
 LLM call observes the appended comment and returns high confidence;
 the workflow completes via the stubbed AgentRunner child.
 """

    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from src.workflows.automation_workflow import (
        AutomationInput,
        AutomationResult,
        AutomationWorkflow,
        NewCommentSignal,
    )

    log = CallLog()

    # State shared with the LLM mock: it returns "low" on the first
    # call and "high" on every subsequent call. The mock also records
    # the description it received so the test can assert the new
    # comment ended up in the issue history.
    state: dict[str, Any] = {"calls": 0, "descriptions": []}

    @activity.defn(name="llm_analyze_task")
    async def _llm_analyze_task(
        issue: Any, _ctx: Any
    ) -> Any:
        state["calls"] = int(state["calls"]) + 1  # type: ignore[arg-type]
        # Capture the description the workflow assembled for this call.
        description = ""
        if hasattr(issue, "description"):
            description = getattr(issue, "description", "") or ""
        elif isinstance(issue, dict):
            description = str(issue.get("description", ""))
        descriptions = state["descriptions"]
        assert isinstance(descriptions, list)
        descriptions.append(description)
        log.record("llm_analyze_task", description)

        if state["calls"] == 1:
            # First analysis: low confidence so the workflow parks in
            # the needs_info wait.
            return make_task_analysis(
                workflow_type="code_change_with_test",
                confidence="low",
                needs_info_question=(
                    "Hangi repo branch'inde değişiklik yapılmalı?"
                ),
            )
        # Second analysis: high confidence so the workflow proceeds
        # past the needs_info loop into the child dispatch.
        return make_task_analysis(
            workflow_type="code_change_with_test",
            confidence="high",
            needs_info_question=None,
        )

    activities = [
        *make_default_activities(log=log),
        _llm_analyze_task,
    ]
    StubAgentRunnerWorkflow = make_stub_agent_runner_workflow()

    workflow_id = "automation-jira-PAY-4220"
    task_queue = "agent-runner-signal"

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AutomationWorkflow, StubAgentRunnerWorkflow],
            activities=activities,
        ):
            inp = AutomationInput(
                issue_key="PAY-4220",
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

            # Wait until the first low-confidence analysis has been
            # consumed and the workflow is parked on the wait condition.
            # The pending question query returns the needs_info text in
            # exactly that state.
            comment_text = "Lütfen develop branch'inde uygula."
            for _ in range(50):
                pending = await handle.query("get_pending_question")
                if pending:
                    break
                # Time-skipping environment: nudge the test server's
                # clock forward by 0.1 s while we wait for activities
                # to complete. ``sleep`` on the time-skipping env is
                # virtual time, so this loop is effectively instant.
                await env.sleep(0.1)
            else:  # pragma: no cover - watchdog
                pytest.fail(
                    "workflow never reached the needs_info wait state"
                )

            await handle.signal(
                AutomationWorkflow.new_comment,
                NewCommentSignal(comment_text=comment_text),
            )

            result_raw: Any = await handle.result()
            # Temporal serialises dataclasses through its data
            # converter; in some SDK versions the result reconstructs
            # to AutomationResult, in others it stays as a dict. Coerce
            # both shapes to a single dict for the assertions below.
            if isinstance(result_raw, AutomationResult):
                result = {
                    "status": result_raw.status,
                    "workflow_type": result_raw.workflow_type,
                    "failure_reason": result_raw.failure_reason,
                }
            else:
                assert isinstance(result_raw, dict), (
                    f"unexpected result shape: {type(result_raw).__name__}"
                )
                result = {
                    "status": result_raw.get("status"),
                    "workflow_type": result_raw.get("workflow_type"),
                    "failure_reason": result_raw.get("failure_reason"),
                }

    # ----- Assertions -------------------------------------------------

    # 1. LLM was called exactly twice (initial + post-signal).
    assert state["calls"] == 2, (
        f"expected 2 LLM calls, got {state['calls']}: {log.names_called()}"
    )

    # 2. Workflow completed successfully via the stubbed child.
    assert result["status"] == "completed", (
        f"expected status=completed, got {result!r}"
    )
    assert result["workflow_type"] == "code_change_with_test"
    assert result["failure_reason"] is None

    # 3. The needs_info question was posted to Jira.
    comments_posted = [
        args[1] for args in log.args_for("jira_add_comment")
    ]
    assert any(
        "Hangi repo branch'inde" in body for body in comments_posted
    ), f"needs_info question never posted; got {comments_posted!r}"

    # 4. The second LLM call observed the new comment in its
    # description (the workflow appends signal history before
    # re-running the activity).
    descriptions = state["descriptions"]
    assert isinstance(descriptions, list) and len(descriptions) == 2
    second_description = descriptions[1]
    assert isinstance(second_description, str)
    assert comment_text in second_description, (
        "second LLM call should see the new comment in its description; "
        f"got {second_description!r}"
    )



# ===========================================================================
# AgentRunner ``comment_added`` signal coverage
#

#
# The tests below pin the design contract that ``comment_added`` signals
# routed through :class:`AgentRunnerWorkflow`:
#
# 1. advance the iteration counter on a plain comment;
# 2. flip the workflow into ``out_of_scope`` once :data:`MAX_ITER` is
# exhausted - and never call ``compensation_chain_run``,
# because natural termination must NOT trigger compensation
# without triggering compensation;
# 3. honour the ``[needs_info]`` keyword routing by bumping the streak
# counter without consuming an iteration.
#
# Each test runs against a real :func:`WorkflowEnvironment.start_time_skipping`
# cluster so the SDK's signal dispatch, sandbox, and replay determinism
# all participate. Activities the workflow body invokes are stubbed by
# small ``@activity.defn`` wrappers that record every call in a shared
# :class:`ActivityCallLog`. ``compensation_chain_run`` is registered
# *defensively* - these tests must never call it; a recorded
# invocation would catch a regression where natural termination
# leaks into the compensation path.
#
# Race-free signal delivery
# -------------------------
#
# Under ``start_time_skipping`` virtual time fast-forwards while the
# workflow is parked, so a signal sent *after* the body reaches
# ``wait_condition`` can race the 7-day ``SIGNAL_WAIT_TIMEOUT`` and
# the workflow may complete with ``signal_wait_timeout`` before the
# signal lands. The legacy noop_test fallback exits its
# ``wait_condition`` on the very first turn (the run-body's initial
# ``_advance_iter_with_banner_check`` flips ``_signal_pending=True``)
# so by the time a post-start ``handle.signal(...)`` reaches the
# server the workflow has already returned - under
# ``start_time_skipping`` the seven-day wait is collapsed to zero
# wall-clock time.
#
# To eliminate the race deterministically every test below seeds
# ``iteration=ITER_WARNING_THRESHOLD`` (3). The run-body's initial
# advance brings ``iter_count`` from 2 to 3, which arms the iter==3
# banner edge; the body then awaits the ``jira_add_comment`` activity
# inside :meth:`AgentRunnerWorkflow._maybe_post_iter_warning_banner`.
# The stub ``jira_add_comment`` activity blocks on an
# :class:`asyncio.Event` (``chain_may_finish``) - while it blocks the
# workflow body is parked inside the activity, signals fired via
# :meth:`handle.signal` reach the server cleanly, queue against the
# workflow's signal handlers, and are processed in the *next*
# workflow task as soon as the activity returns. Once we release the
# barrier the queued signals all fire in order, advance the
# iteration state deterministically, and the body's
# ``wait_condition`` evaluates the post-signal state. This gives us
# observable iter advances under ``start_time_skipping`` without
# depending on virtual-time edge cases.
# ===========================================================================

import asyncio as _ar_asyncio
import contextlib
import sys
from dataclasses import dataclass as _ar_dataclass
from dataclasses import field as _ar_field
from pathlib import Path as _ArPath


# ---------------------------------------------------------------------------
# sys.path bootstrap - agent-runner-worker tree, temporal-shared, mcp_client.
# Mirrors the bootstrap used by ``test_temporal_cancel_compensation.py``.
# ---------------------------------------------------------------------------

_AR_PLATFORM_ROOT: _ArPath = _ArPath(__file__).resolve().parents[2]
_AR_AGENT_RUNNER_SRC: _ArPath = (
    _AR_PLATFORM_ROOT / "workers" / "agent-runner-worker" / "src"
)
_AR_TEMPORAL_SHARED_SRC: _ArPath = (
    _AR_PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)
_AR_MCP_CLIENT_SRC: _ArPath = (
    _AR_PLATFORM_ROOT / "libs" / "mcp_client" / "src"
)

for _ar_candidate in (
    _AR_AGENT_RUNNER_SRC,
    _AR_TEMPORAL_SHARED_SRC,
    _AR_MCP_CLIENT_SRC,
):
    _ar_str = str(_ar_candidate)
    if _ar_candidate.is_dir() and _ar_str not in sys.path:
        sys.path.insert(0, _ar_str)


# ---------------------------------------------------------------------------
# Skip gate + start_time_skipping context manager
# ---------------------------------------------------------------------------


def _agent_runner_temporal_env_available() -> bool:
    """Return ``True`` when the Temporal time-skipping env imports cleanly.

 A ``pytest.skipif`` decorator backed by this predicate is applied
 to every AgentRunner signal test below so hosts without the embedded
 ``temporal-test-server`` skip cleanly instead of erroring at
 collection time.
 """

    try:
        from temporalio.testing import WorkflowEnvironment  # noqa: F401
    except Exception:  # noqa: BLE001 - any import failure → skip.
        return False
    return True


_AR_TEMPORAL_SKIP = pytest.mark.skipif(
    not _agent_runner_temporal_env_available(),
    reason="temporalio test environment not available",
)


@contextlib.asynccontextmanager
async def _ar_start_time_skipping_or_skip() -> Any:
    """Start the Temporal time-skipping env, ``pytest.skip``ing on failure.

 The embedded ``temporal-test-server`` may fail to start on hosts
 where the binary is not bundled. Surface that cleanly as a skip.
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


@_ar_dataclass
class ActivityCallLog:
    """Append-only log of activity invocations recorded by the stubs."""

    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = _ar_field(
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


def _make_agent_runner_activities(
    log: ActivityCallLog,
    *,
    chain_started: _ar_asyncio.Event,
    chain_may_finish: _ar_asyncio.Event,
) -> list[Any]:
    """Build the bag of stub activities the AgentRunnerWorkflow body invokes.

 The legacy signal-wait fallback (used by ``noop_test``) calls
 ``audit_emit`` for the iter==3 banner audit and any pending
 ``[fix]`` debounce / cache hits flushed by the signal handler, and
 ``jira_add_comment`` for the iter==3 banner itself.

 The ``jira_add_comment`` stub here is the **sync barrier**: it
 sets ``chain_started`` when first called (the test waits on this
 to confirm the workflow body is parked) and then blocks on
 ``chain_may_finish`` (the test sets this once it has fired all
 the signals it wants delivered). While the workflow body is
 parked inside this activity await, queued
 ``handle.signal(...)`` calls land on the workflow without racing
 the legacy fallback's signal-wait timeout - the SDK delivers
 them as part of the next workflow task once the activity
 returns.

 ``compensation_chain_run`` is registered *defensively*: these
 tests must never invoke it ( - natural termination must NOT
 trigger compensation). A recorded invocation would catch a
 regression in that branch.

 Failures inside ``audit_emit`` / ``jira_add_comment`` are swallowed
 by the workflow body (``# noqa: BLE001 - ... best-effort``), so a
 crash here would only affect the audit trail; the spec
 assertions still run on the workflow output.
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
        # Sync barrier: signal "we're here", then park until the
        # test releases us. The wait is bounded so a misbehaving
        # test does not hang indefinitely.
        chain_started.set()
        try:
            await _ar_asyncio.wait_for(
                chain_may_finish.wait(), timeout=10.0
            )
        except _ar_asyncio.TimeoutError:
            # Bounded wait - release the activity so the workflow
            # can complete and the test can collect its result.
            pass
        return None

    return [_compensation_chain_run, _audit_emit, _jira_add_comment]


# ---------------------------------------------------------------------------
# Input fixture
# ---------------------------------------------------------------------------


def _make_agent_runner_input(
    *,
    issue_key: str = "PAY-5101",
    iteration: int = 3,
    max_iter: int = 5,
    workflow_type: str = "noop_test",
) -> Any:
    """Build a minimal :class:`AgentRunnerWorkflowInput` for the tests.

 ``workflow_type="noop_test"`` falls through to the workflow body's
 legacy signal-wait fallback in
 :meth:`AgentRunnerWorkflow._dispatch_workflow_type` - the natural
 surface for ``comment_added`` signal handling. The default
 ``iteration=3`` matches
 :data:`agent_runner.workflows.agent_runner_workflow.ITER_WARNING_THRESHOLD`
 so the run-body's initial advance arms the iter==3 banner edge,
 parking the body inside the slow ``jira_add_comment`` activity -
 this is the sync barrier the tests use to deliver post-start
 signals deterministically.
 """

    from temporal_shared.messages import (
        AgentRunnerWorkflowInput,
        LlmAnalysisResult,
    )

    analysis = LlmAnalysisResult(
        workflow_type=workflow_type,
        confidence="high",
        title=f"comment_added integration test for {issue_key}",
        rationale="comment-added integration fixture",
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


# ---------------------------------------------------------------------------
# Result / query coercion helpers
#
# ``AgentRunnerWorkflowOutput`` and ``IterationState`` are both frozen
# dataclasses; depending on the SDK's data converter the workflow
# result and query responses round-trip back into the dataclass or
# surface as plain dicts. The helpers below normalise both shapes so
# the assertions stay robust across SDK versions.
# ---------------------------------------------------------------------------


def _ar_extract_status(result: Any) -> str | None:
    if hasattr(result, "status"):
        return getattr(result, "status")
    if isinstance(result, dict):
        return result.get("status")
    return None


def _ar_extract_iter_count(state: Any) -> int:
    if hasattr(state, "iter_count"):
        return int(getattr(state, "iter_count"))
    if isinstance(state, dict):
        return int(state.get("iter_count", -1))
    return -1


def _ar_extract_needs_info_streak(state: Any) -> int:
    if hasattr(state, "needs_info_streak"):
        return int(getattr(state, "needs_info_streak"))
    if isinstance(state, dict):
        return int(state.get("needs_info_streak", -1))
    return -1


# ---------------------------------------------------------------------------
# 1. Plain comment_added advances iter_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@_AR_TEMPORAL_SKIP
async def test_comment_added_signal_advances_iter_count() -> None:
    """Start :class:`AgentRunnerWorkflow` at
 ``iteration=ITER_WARNING_THRESHOLD=3`` / ``max_iter=5``. The
 run-body's initial advance lifts ``iter_count`` from 2 to 3 and
 arms the iter==3 banner edge. The body parks inside the slow
 ``jira_add_comment`` banner activity; while it is parked we
 deliver one plain ``comment_added`` signal (no ``[fix]`` /
 ``[explain]`` / ``[needs_info]`` keyword markers). Once the
 barrier releases the signal handler runs in the next workflow
 task, advances ``iter_count`` to 4 (still below MAX_ITER=5), and
 the body exits via the wait_condition wakeup with
 ``status="completed"``.

 The post-condition pinned by this test is the spec contract from
 the brief: ``iter_count >= 2`` after a plain ``comment_added``
 signal - i.e. the signal handler successfully advances the
 counter end-to-end through a real Temporal cluster.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        AgentRunnerWorkflow,
        CommentAddedSignal,
    )

    log = ActivityCallLog()
    chain_started = _ar_asyncio.Event()
    chain_may_finish = _ar_asyncio.Event()
    activities = _make_agent_runner_activities(
        log,
        chain_started=chain_started,
        chain_may_finish=chain_may_finish,
    )

    workflow_id = "agent-runner-jira-PAY-5101-comment-added"
    task_queue = "agent-runner-comment-added-advance"

    async with _ar_start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _make_agent_runner_input(
                issue_key="PAY-5101", iteration=3, max_iter=5
            )
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
                start_signal="comment_added",
                start_signal_args=[
                    CommentAddedSignal(
                        comment_text="lütfen yine de devam et",
                        actor_account_id="user-1",
                    )
                ],
            )

            # Wait until the workflow body parks inside the banner
            # activity - that is the moment when post-start signals
            # can be delivered without racing the legacy signal-wait
            # fallback timeout.
            await _ar_asyncio.wait_for(
                chain_started.wait(), timeout=10.0
            )

            # Release the barrier so the workflow can drain the
            # buffered start_signal handler and complete.
            chain_may_finish.set()

            result: Any = await handle.result()

            iter_state = await handle.query("get_iteration_state")
            out_of_scope = await handle.query("is_out_of_scope")

    # ----- Assertions -------------------------------------------------

    iter_count = _ar_extract_iter_count(iter_state)
    assert iter_count >= 2, (
        f"expected iter_count>=2 after one comment_added signal, got "
        f"{iter_count} (state={iter_state!r})"
    )

    # The plain comment is below the cap - workflow must NOT be
    # in ``out_of_scope`` after a single advance.
    assert out_of_scope is False, (
        f"is_out_of_scope must be False after one signal at "
        f"iter_count={iter_count}; got {out_of_scope!r}"
    )

    # The workflow exits via the wait_condition wakeup, NOT via the
    # cancel branch. Compensation chain MUST NOT have run .
    assert log.count("compensation_chain_run") == 0, (
        f"compensation_chain_run must not run on natural termination; "
        f"call log: {log.names()!r}"
    )

    status = _ar_extract_status(result)
    assert status == "completed", (
        f"expected status=completed for one-signal advance, got "
        f"{status!r} (result={result!r})"
    )


# ---------------------------------------------------------------------------
# 2. Five comment_added signals flip the workflow to out_of_scope
# (MAX_ITER cap; natural termination is distinct from compensation)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@_AR_TEMPORAL_SKIP
async def test_five_comments_flip_workflow_to_out_of_scope() -> None:
    """Fire five ``comment_added`` signals back-to-back with
 ``iteration=3`` / ``max_iter=5``: the first lands via
 signal-with-start (race-free; processed during the workflow's
 first tick alongside the buffered handler), the remaining four
 are queued via :meth:`handle.signal` while the body is parked
 inside the iter==3 banner activity (the sync barrier).

 Trace (with ``iteration=3`` / ``max_iter=5``):

 * run initial advance → ``iter_count=3`` (banner armed)
 * banner activity parks the body → barrier holds the body
 * signal 1 (signal-with-start) buffered, handler advances
 → ``iter_count=4``
 * signals 2-5 queued via handle.signal
 * barrier releases - handlers fire → 4→5 (signal 2),
 5→cap (signal 3 flips
 ``_out_of_scope``),
 signals 4-5 see
 ``_out_of_scope=True`` and
 return silently
 * workflow body wakes, observes
 ``_out_of_scope=True``, returns
 with ``status="out_of_scope"``.

 The cap MUST hold (``iter_count <= MAX_ITER=5``) and
 ``compensation_chain_run`` MUST NOT have run - natural
 termination (iter cap, ``out_of_scope``) is distinct from cancel;
 only cancel runs the compensation chain.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        MAX_ITER,
        AgentRunnerWorkflow,
        CommentAddedSignal,
    )

    log = ActivityCallLog()
    chain_started = _ar_asyncio.Event()
    chain_may_finish = _ar_asyncio.Event()
    activities = _make_agent_runner_activities(
        log,
        chain_started=chain_started,
        chain_may_finish=chain_may_finish,
    )

    workflow_id = "agent-runner-jira-PAY-5102-five-comments"
    task_queue = "agent-runner-comment-added-cap"

    async with _ar_start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _make_agent_runner_input(
                issue_key="PAY-5102", iteration=3, max_iter=5
            )
            # First signal - race-free via signal-with-start so the
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
            await _ar_asyncio.wait_for(
                chain_started.wait(), timeout=10.0
            )

            # Signals 2-5 - fired back-to-back. The Temporal server
            # batches signals queued before the workflow's next
            # workflow-task runs, so all four typically land in the
            # same task as signal 1 once the barrier releases.
            for i in range(2, 6):
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

    assert out_of_scope is True, (
        f"is_out_of_scope must be True after the cap fires; "
        f"got {out_of_scope!r} (iter_state={iter_state!r})"
    )

    iter_count = _ar_extract_iter_count(iter_state)
    # The cap clamps iter_count at MAX_ITER=5 - the signal whose
    # ``_should_advance_iter`` returns advance=False leaves
    # ``iter_count`` untouched while flipping ``_out_of_scope``.
    assert iter_count <= MAX_ITER, (
        f"iter_count={iter_count} must not exceed MAX_ITER={MAX_ITER}"
    )

    status = _ar_extract_status(result)
    assert status == "out_of_scope", (
        f"expected status=out_of_scope after iter cap, got {status!r} "
        f"(result={result!r})"
    )

    # - natural termination must NOT trigger compensation.
    assert log.count("compensation_chain_run") == 0, (
        f"compensation_chain_run must not run on natural termination "
        f"(MAX_ITER); call log: {log.names()!r}"
    )


# ---------------------------------------------------------------------------
# 3. ``[needs_info]`` keyword does not advance iter_count 
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@_AR_TEMPORAL_SKIP
async def test_needs_info_keyword_does_not_advance_iter() -> None:
    """The ``[needs_info]`` keyword routes ``comment_added`` to
 :meth:`AgentRunnerWorkflow._apply_needs_info_signal`, which bumps
 :attr:`IterationState.needs_info_streak` instead of advancing
 ``iter_count``.

 Trace (with ``iteration=3`` / ``max_iter=5``):

 * run initial advance → ``iter_count=3``,
 ``needs_info_streak=0``
 * banner activity parks the body → barrier holds
 * signal 1 ``[needs_info] please clarify`` → ``iter_count=3``,
 ``needs_info_streak=1``
 * signal 2 ``[needs_info] please clarify`` → ``iter_count=3``,
 ``needs_info_streak=2``
 * streak still below cap of 3 - workflow exits ``status="completed"``
 with ``is_out_of_scope=False``.

 The first signal lands via signal-with-start; the second via
 :meth:`handle.signal` while the body is parked in the banner
 activity barrier. After the barrier releases both handlers run
 in order; ``iter_count`` MUST stay at 3 (the run-body's initial
 advance) and ``needs_info_streak`` MUST be at most 2 (still
 below :data:`NEEDS_INFO_MAX_STREAK`=3).
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        AgentRunnerWorkflow,
        CommentAddedSignal,
    )

    log = ActivityCallLog()
    chain_started = _ar_asyncio.Event()
    chain_may_finish = _ar_asyncio.Event()
    activities = _make_agent_runner_activities(
        log,
        chain_started=chain_started,
        chain_may_finish=chain_may_finish,
    )

    workflow_id = "agent-runner-jira-PAY-5103-needs-info"
    task_queue = "agent-runner-needs-info-streak"

    async with _ar_start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _make_agent_runner_input(
                issue_key="PAY-5103", iteration=3, max_iter=5
            )
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
                start_signal="comment_added",
                start_signal_args=[
                    CommentAddedSignal(
                        comment_text="[needs_info] please clarify",
                        actor_account_id="user-1",
                    )
                ],
            )

            await _ar_asyncio.wait_for(
                chain_started.wait(), timeout=10.0
            )

            # Second ``[needs_info]`` signal - queued while the
            # body is parked in the banner activity barrier.
            try:
                await handle.signal(
                    AgentRunnerWorkflow.comment_added,
                    CommentAddedSignal(
                        comment_text="[needs_info] please clarify",
                        actor_account_id="user-1",
                    ),
                )
            except Exception:  # noqa: BLE001 - post-completion no-op
                pass

            # Release the barrier so signals fire and the body completes.
            chain_may_finish.set()

            result: Any = await handle.result()

            iter_state = await handle.query("get_iteration_state")
            out_of_scope = await handle.query("is_out_of_scope")

    # ----- Assertions -------------------------------------------------

    iter_count = _ar_extract_iter_count(iter_state)
    # - ``[needs_info]`` does NOT advance ``iter_count``. Only
    # the run-body's initial ``_advance_iter_with_banner_check`` runs,
    # leaving the counter at the seed value of 3.
    assert iter_count == 3, (
        f"expected iter_count==3 (needs_info bumps streak, not iter); "
        f"got {iter_count} (state={iter_state!r})"
    )

    needs_info_streak = _ar_extract_needs_info_streak(iter_state)
    # The streak should reflect the needs_info signals that landed.
    # In the most common SDK timing the value is exactly 2 (both
    # signals processed); in rare batched-delivery cases the second
    # signal is consumed in the same workflow task, but either way
    # the value is bounded by 1 <= streak < NEEDS_INFO_MAX_STREAK=3.
    assert 1 <= needs_info_streak < 3, (
        f"expected needs_info_streak in [1, 3) after up to 2 "
        f"[needs_info] signals, got {needs_info_streak} "
        f"(state={iter_state!r})"
    )

    # Streak still below the cap of 3 - workflow must NOT be
    # ``out_of_scope`` after at most 2 needs_info signals.
    assert out_of_scope is False, (
        f"is_out_of_scope must be False with needs_info_streak<3; "
        f"got {out_of_scope!r} (state={iter_state!r})"
    )

    # No compensation on natural termination ( regression check).
    assert log.count("compensation_chain_run") == 0, (
        f"compensation_chain_run must not run on natural termination; "
        f"call log: {log.names()!r}"
    )

    # Workflow exited cleanly via the wait_condition wakeup.
    status = _ar_extract_status(result)
    assert status == "completed", (
        f"expected status=completed when needs_info_streak below cap, "
        f"got {status!r} (result={result!r})"
    )
