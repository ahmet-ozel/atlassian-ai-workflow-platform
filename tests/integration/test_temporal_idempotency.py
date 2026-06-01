"""Integration test: AutomationWorkflow native Temporal idempotency.

**Validates: Requirements 1.6, 2.2, 2.6, 5.5, 10.4, 11.5**

Scenario
--------

The webhook handler de-duplicates incoming events twice over:

1. SHA-256 replay guard at the HTTP boundary
   (``decision/replay.check_and_insert``).
2. Temporal native idempotency: a second
   ``client.start_workflow(..., id="automation-jira-PAY-4211")`` while
   the first is still running surfaces a
   ``temporalio.exceptions.WorkflowAlreadyStartedError`` from the SDK,
   which the webhook handler maps to HTTP 200 ``{"status": "duplicate"}``
   (see ``services/automation-service/src/webhooks/jira.py``).

This test pins the second invariant directly against the Temporal time-
skipping ``WorkflowEnvironment``: we start an :class:`AutomationWorkflow`
that parks in the ``needs_info`` wait (low-confidence LLM analysis,
empty signal queue), then immediately start a *second* workflow with
the **same workflow ID** and assert the SDK raises
``WorkflowAlreadyStartedError``. This is the exact exception the
webhook handler catches to emit its duplicate response.

The handler-side mapping (exception → 200 duplicate JSON) already has
its own unit-test coverage in
``services/automation-service/tests/unit/test_temporal_client.py``;
this integration test pins the SDK contract that makes that mapping
correct.

The ``platform-mimari-workflows`` spec extends this file with three
additional integration cases covering the new idempotency contract
(R1.6, R2.2, R2.6) on top of the foundation-level invariant:

* ``test_start_workflow_idempotent_returns_was_existing_true_on_duplicate``
  — the public :func:`temporal_shared.start_helper.start_workflow_idempotent`
  helper returns ``was_existing=True`` on a duplicate ``workflow_id``
  rather than re-raising.
* ``test_signal_after_start_delivered_to_existing_workflow`` — when a
  duplicate ``signalWithStart``-style call lands on a running workflow
  the signal payload reaches the **existing** workflow's signal handler
  (no new execution is spawned) per R2.2.
* ``test_n_repeated_starts_yield_single_execution`` — N concurrent
  start calls with the same ``workflow_id`` collapse to exactly one
  Temporal execution and ``N-1`` duplicate-detected results per R2.6.

If the Temporal time-skipping environment is unavailable in the
runtime (no test server binary, no network namespace, missing native
deps), every test in this file ``pytest.skip``s cleanly so the
integration suite stays self-contained on machines that cannot run
Temporal locally.
"""

from __future__ import annotations

import asyncio
import contextlib
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
# Environment availability gate
# ---------------------------------------------------------------------------


def _temporal_test_env_available() -> bool:
    """Return ``True`` when the time-skipping ``WorkflowEnvironment`` imports.

    We only import the symbol here — actually starting the env requires
    spinning up the embedded ``temporal-test-server`` binary, which can
    fail at runtime even when the import succeeds. Each test wraps its
    ``WorkflowEnvironment.start_time_skipping()`` call in a
    ``try/except`` that re-raises as ``pytest.skip`` so a missing
    binary surfaces the same way an entirely missing module would.
    """

    try:  # noqa: SIM105 — explicit branch keeps the intent legible.
        from temporalio.testing import WorkflowEnvironment  # noqa: F401
    except Exception:  # noqa: BLE001 — any import failure → skip.
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _temporal_test_env_available(),
    reason="temporalio test environment not available",
)


@contextlib.asynccontextmanager
async def _start_time_skipping_or_skip() -> Any:
    """Start the Temporal time-skipping env, ``pytest.skip``ing on failure.

    The embedded ``temporal-test-server`` may fail to start on machines
    where the binary is not bundled (some headless CI runners, sandboxed
    environments, missing OS dependencies). When that happens we want
    the test to skip rather than error so the integration suite stays
    green on machines that can't host Temporal.
    """

    from temporalio.testing import WorkflowEnvironment

    try:
        env_cm = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 — surface as skip.
        pytest.skip(f"temporalio test environment not available: {exc}")
    async with env_cm as env:
        yield env


# ---------------------------------------------------------------------------
# Existing test: SDK-level WorkflowAlreadyStartedError contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_second_start_with_same_id_raises_workflow_already_started_error() -> None:
    """**Validates: Requirements 1.6, 2.2, 5.5, 10.4, 11.5**

    Starting two workflows with the same workflow ID against the
    Temporal time-skipping environment must raise
    :class:`temporalio.exceptions.WorkflowAlreadyStartedError` on the
    second start. This is the SDK-level invariant the webhook handler
    relies on to return 200 ``duplicate`` for replayed events.
    """

    # Local imports keep test-collection light and avoid pulling the
    # Temporal sandbox into ``sys.modules`` for unrelated tests.
    from temporalio import activity
    from temporalio.exceptions import WorkflowAlreadyStartedError
    from temporalio.worker import Worker

    from src.workflows.automation_workflow import (
        AutomationInput,
        AutomationWorkflow,
    )

    log = CallLog()

    # Low-confidence LLM result so the workflow parks in the
    # ``needs_info`` wait condition and stays running long enough for
    # the second start to collide on the same workflow ID.
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

    workflow_id = "automation-jira-PAY-4211"
    task_queue = "agent-runner-idempotency"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AutomationWorkflow, StubAgentRunnerWorkflow],
            activities=activities,
        ):
            inp = AutomationInput(
                issue_key="PAY-4211",
                department_id="payments",
                available_capabilities=("jira", "bitbucket", "execution"),
                available_repos=("payment-service",),
                iteration=1,
            )

            # First start: succeeds and parks the workflow in the
            # needs_info wait. We deliberately do NOT await the
            # workflow's terminal result here — leaving it running is
            # exactly the precondition the webhook handler's duplicate
            # branch covers.
            handle = await env.client.start_workflow(
                AutomationWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
            )

            # Wait until the first workflow has actually entered the
            # needs_info wait state (i.e. the server has registered a
            # running execution under ``workflow_id``). Without this
            # guard the second start can race the first if the
            # initial activities haven't been scheduled yet.
            for _ in range(50):
                pending = await handle.query("get_pending_question")
                if pending:
                    break
                await env.sleep(0.1)
            else:  # pragma: no cover - watchdog
                pytest.fail(
                    "first workflow never reached the needs_info wait state"
                )

            # Second start: same workflow ID, must raise.
            with pytest.raises(WorkflowAlreadyStartedError):
                await env.client.start_workflow(
                    AutomationWorkflow.__name__,
                    inp,
                    id=workflow_id,
                    task_queue=task_queue,
                )

            # Tear down the still-running first workflow before exiting
            # the worker context. ``terminate`` is forceful (does not
            # run completion handlers) which is exactly what we want
            # for the test cleanup; ``cancel`` would block on the
            # workflow draining the cancel scope and time out under
            # the time-skipping environment.
            await handle.terminate(reason="test cleanup")


# ---------------------------------------------------------------------------
# workflows-spec extension: helper-level idempotency contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_start_workflow_idempotent_returns_was_existing_true_on_duplicate() -> None:
    """**Validates: Requirements 1.6, 2.2**

    The workflows-spec public helper
    :func:`temporal_shared.start_helper.start_workflow_idempotent`
    swallows the SDK's :class:`WorkflowAlreadyStartedError` and returns
    ``StartResult(execution_id=workflow_id, was_existing=True)`` so
    HTTP callers (webhooks, admin endpoints) can route replayed events
    to a single response shape (HTTP 202, ``was_existing=True``)
    without a per-call ``try/except``.

    The first call must return ``was_existing=False`` (fresh start);
    the second call against the **same** ``workflow_id`` while the
    first execution is still running must return ``was_existing=True``
    with the caller-supplied id surfaced in ``execution_id`` —
    matching the contract documented at
    ``platform/libs/temporal-shared/src/temporal_shared/start_helper.py``.
    """

    from temporalio import activity
    from temporalio.worker import Worker

    from src.workflows.automation_workflow import (
        AutomationInput,
        AutomationWorkflow,
    )
    from temporal_shared.start_helper import start_workflow_idempotent

    log = CallLog()

    # Low-confidence LLM result so the first workflow parks on the
    # needs_info wait — the same long-running shape used by the
    # SDK-level test above. Two consecutive calls to the idempotent
    # helper land while the workflow is still running.
    @activity.defn(name="llm_analyze_task")
    async def _llm_analyze_task_low_confidence(
        _issue: Any, _ctx: Any
    ) -> Any:
        log.record("llm_analyze_task")
        return make_task_analysis(
            workflow_type="code_change_with_test",
            confidence="low",
            needs_info_question="Hangi repo branch'i?",
        )

    activities = [
        *make_default_activities(log=log),
        _llm_analyze_task_low_confidence,
    ]
    StubAgentRunnerWorkflow = make_stub_agent_runner_workflow()

    workflow_id = "automation-jira-PAY-4212"
    task_queue = "agent-runner-idempotent-helper"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AutomationWorkflow, StubAgentRunnerWorkflow],
            activities=activities,
        ):
            inp = AutomationInput(
                issue_key="PAY-4212",
                department_id="payments",
                available_capabilities=("jira", "bitbucket", "execution"),
                available_repos=("payment-service",),
                iteration=1,
            )

            # First call: fresh start.
            first = await start_workflow_idempotent(
                env.client,
                AutomationWorkflow.__name__,
                workflow_id,
                [inp],
                task_queue=task_queue,
            )

            assert first.execution_id == workflow_id, (
                f"first start should echo the supplied workflow_id; "
                f"got {first.execution_id!r}"
            )
            assert first.was_existing is False, (
                f"first start should report was_existing=False; "
                f"got {first.was_existing!r}"
            )

            # Wait until the workflow is parked on the wait condition
            # so the second call lands on a running execution rather
            # than racing the start.
            handle = env.client.get_workflow_handle(workflow_id)
            for _ in range(50):
                pending = await handle.query("get_pending_question")
                if pending:
                    break
                await env.sleep(0.1)
            else:  # pragma: no cover - watchdog
                pytest.fail(
                    "first workflow never reached needs_info wait state"
                )

            # Second call: must collapse to the existing execution.
            second = await start_workflow_idempotent(
                env.client,
                AutomationWorkflow.__name__,
                workflow_id,
                [inp],
                task_queue=task_queue,
            )

            assert second.execution_id == workflow_id, (
                f"duplicate start should echo the original workflow_id; "
                f"got {second.execution_id!r}"
            )
            assert second.was_existing is True, (
                f"duplicate start should report was_existing=True; "
                f"got {second.was_existing!r}"
            )

            # Exactly one LLM call: the duplicate did not spawn a new
            # execution. ``llm_analyze_task`` is the first activity
            # the workflow runs after start, so its call count is the
            # cleanest proxy for "how many executions actually ran".
            assert log.count("llm_analyze_task") == 1, (
                f"expected exactly 1 LLM call across both starts, "
                f"got {log.count('llm_analyze_task')}: {log.names_called()}"
            )

            await handle.terminate(reason="test cleanup")


# ---------------------------------------------------------------------------
# workflows-spec extension: signal-after-start lands on the existing run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_signal_after_start_delivered_to_existing_workflow() -> None:
    """**Validates: Requirements 2.2**

    Per R2.2 the webhook handler treats the second start as a
    *signal-with-start*: the running workflow's signal handler must
    receive the new payload, no fresh execution is spawned, and the
    workflow advances on the same event history.

    We exercise that contract by:

    1. Starting :class:`AutomationWorkflow` with low-confidence LLM
       analysis so it parks on the ``needs_info`` wait condition.
    2. Sending a ``new_comment`` signal with a recognisable payload —
       the same payload the webhook handler synthesises after running
       :func:`start_workflow_idempotent` and finding
       ``was_existing=True``.
    3. Asserting the second LLM analysis (driven by the signal) sees
       the comment text in its ``description`` argument and that
       exactly one execution was started under the workflow id.
    """

    from temporalio import activity
    from temporalio.worker import Worker

    from src.workflows.automation_workflow import (
        AutomationInput,
        AutomationResult,
        AutomationWorkflow,
        NewCommentSignal,
    )

    log = CallLog()
    state: dict[str, Any] = {"calls": 0, "descriptions": []}

    @activity.defn(name="llm_analyze_task")
    async def _llm_analyze_task(_issue: Any, _ctx: Any) -> Any:
        state["calls"] = int(state["calls"]) + 1  # type: ignore[arg-type]
        description = ""
        if hasattr(_issue, "description"):
            description = getattr(_issue, "description", "") or ""
        elif isinstance(_issue, dict):
            description = str(_issue.get("description", ""))
        descriptions = state["descriptions"]
        assert isinstance(descriptions, list)
        descriptions.append(description)
        log.record("llm_analyze_task", description)

        if state["calls"] == 1:
            return make_task_analysis(
                workflow_type="code_change_with_test",
                confidence="low",
                needs_info_question="Hangi branch?",
            )
        # Second analysis (post-signal): high confidence so the
        # workflow proceeds past the wait into the (stubbed) child.
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

    workflow_id = "automation-jira-PAY-4213"
    task_queue = "agent-runner-signal-after-start"
    signal_payload = "Lütfen develop branch'inde uygula."

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AutomationWorkflow, StubAgentRunnerWorkflow],
            activities=activities,
        ):
            inp = AutomationInput(
                issue_key="PAY-4213",
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

            # Wait for the workflow to park on the needs_info condition.
            for _ in range(50):
                pending = await handle.query("get_pending_question")
                if pending:
                    break
                await env.sleep(0.1)
            else:  # pragma: no cover - watchdog
                pytest.fail(
                    "first workflow never reached needs_info wait state"
                )

            # The webhook handler signals the existing workflow on
            # the duplicate-event branch. We use the same handle the
            # ``start_workflow_idempotent`` helper would resolve via
            # ``client.get_workflow_handle(workflow_id)``.
            existing_handle = env.client.get_workflow_handle(workflow_id)
            await existing_handle.signal(
                AutomationWorkflow.new_comment,
                NewCommentSignal(comment_text=signal_payload),
            )

            # Drive the workflow to completion through the stubbed
            # child so the assertions below have a terminal status to
            # check.
            result_raw: Any = await handle.result()

    # ----- Assertions --------------------------------------------------

    # Coerce the result envelope (dict or dataclass) to a single shape.
    if isinstance(result_raw, AutomationResult):
        status = result_raw.status
    else:
        assert isinstance(result_raw, dict), (
            f"unexpected result shape: {type(result_raw).__name__}"
        )
        status = result_raw.get("status")

    assert status == "completed", (
        f"signal-driven completion expected status=completed, got {status!r}"
    )

    # The signal must have driven exactly one additional LLM call —
    # i.e. one execution served both the start and the signal-with-start.
    assert state["calls"] == 2, (
        f"expected 2 LLM calls (initial + post-signal), "
        f"got {state['calls']}: {log.names_called()}"
    )

    # The second LLM call must have observed the signal payload in
    # the issue description (the workflow appends new comments before
    # re-running analysis).
    descriptions = state["descriptions"]
    assert isinstance(descriptions, list) and len(descriptions) == 2
    second_description = descriptions[1]
    assert isinstance(second_description, str)
    assert signal_payload in second_description, (
        "signal payload should be visible to the second LLM call; "
        f"got {second_description!r}"
    )


# ---------------------------------------------------------------------------
# workflows-spec extension: N concurrent starts → one execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_n_repeated_starts_yield_single_execution() -> None:
    """**Validates: Requirements 2.6**

    R2.6 — the same webhook event payload delivered N times in a row
    must produce exactly one Temporal execution. We exercise the
    contract through :func:`start_workflow_idempotent` (the helper the
    webhook handler is built on) by issuing five concurrent calls
    against the same ``workflow_id`` while the workflow is parked on
    the needs_info wait.

    Expected outcome:

    * Exactly **one** call returns ``was_existing=False`` (the fresh
      start).
    * The remaining ``N-1`` calls return ``was_existing=True`` —
      Temporal collapsed them onto the running execution rather than
      spawning duplicates.
    * The activity call log shows the LLM was invoked exactly once,
      proving only one execution actually ran.

    The test uses ``asyncio.gather`` so all five calls race against
    each other; under the time-skipping env the server still
    serialises them via the workflow id slot and only the winner
    creates the execution.
    """

    from temporalio import activity
    from temporalio.worker import Worker

    from src.workflows.automation_workflow import (
        AutomationInput,
        AutomationWorkflow,
    )
    from temporal_shared.start_helper import (
        StartResult,
        start_workflow_idempotent,
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
            needs_info_question="Hangi branch?",
        )

    activities = [
        *make_default_activities(log=log),
        _llm_analyze_task_low_confidence,
    ]
    StubAgentRunnerWorkflow = make_stub_agent_runner_workflow()

    workflow_id = "automation-jira-PAY-4214"
    task_queue = "agent-runner-n-repeats"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AutomationWorkflow, StubAgentRunnerWorkflow],
            activities=activities,
        ):
            inp = AutomationInput(
                issue_key="PAY-4214",
                department_id="payments",
                available_capabilities=("jira", "bitbucket", "execution"),
                available_repos=("payment-service",),
                iteration=1,
            )

            # Fire 5 concurrent starts against the same workflow id.
            # Temporal's server-side workflow-id mutex collapses them
            # onto a single execution; the helper translates the
            # losing N-1 attempts into ``was_existing=True``.
            n = 5
            results: list[StartResult] = await asyncio.gather(
                *[
                    start_workflow_idempotent(
                        env.client,
                        AutomationWorkflow.__name__,
                        workflow_id,
                        [inp],
                        task_queue=task_queue,
                    )
                    for _ in range(n)
                ]
            )

            handle = env.client.get_workflow_handle(workflow_id)
            # Let the singleton execution reach the needs_info wait so
            # the activity-call assertion below is stable. Without
            # this the LLM activity may not have been scheduled yet
            # by the time we count.
            for _ in range(50):
                pending = await handle.query("get_pending_question")
                if pending:
                    break
                await env.sleep(0.1)
            else:  # pragma: no cover - watchdog
                pytest.fail(
                    "winning workflow never reached needs_info wait state"
                )

            await handle.terminate(reason="test cleanup")

    # ----- Assertions --------------------------------------------------

    fresh = [r for r in results if not r.was_existing]
    duplicates = [r for r in results if r.was_existing]

    assert len(fresh) == 1, (
        f"expected exactly 1 fresh start across {n} concurrent calls, "
        f"got {len(fresh)} (results={results!r})"
    )
    assert len(duplicates) == n - 1, (
        f"expected {n - 1} duplicate detections across {n} concurrent "
        f"calls, got {len(duplicates)} (results={results!r})"
    )

    # Every result must echo the supplied workflow_id.
    assert all(r.execution_id == workflow_id for r in results), (
        f"every result should carry the supplied workflow_id; "
        f"got {[r.execution_id for r in results]!r}"
    )

    # Exactly one execution actually ran — the LLM activity is the
    # first side effect inside the workflow body.
    assert log.count("llm_analyze_task") == 1, (
        f"expected exactly 1 LLM call (single execution), got "
        f"{log.count('llm_analyze_task')}: {log.names_called()}"
    )
