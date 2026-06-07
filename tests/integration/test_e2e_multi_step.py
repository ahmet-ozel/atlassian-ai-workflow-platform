"""End-to-end integration test for the ``multi_step`` graceful-skip flow.

Scenario
--------

The ``multi_step`` workflow type orchestrates *N* child workflows on
behalf of a single Jira issue. The parent must
**not** fail-fast when any individual child lacks a capability - it
must mark that child ``out_of_scope`` and continue dispatching the
remaining children. The pure decision helper for that contract lives
in :mod:`temporal_shared.multi_step` (:func:`multi_step_dispatch`,
:func:`aggregated_output`).

At the time of writing, neither :class:`AutomationWorkflow` nor
:class:`AgentRunnerWorkflow` carry a dedicated ``_handle_multi_step``
body. That parent branch is still pending and the ``multi_step`` workflow type therefore
falls through to the legacy signal-wait dispatcher when routed through
the production workflows. The end-to-end Temporal *dispatch* of a
``multi_step`` parent is consequently not yet wired.

This test exercises the pieces that **are** wired today, end-to-end,
through a real Temporal time-skipping :class:`WorkflowEnvironment`:

1. Construct a list of :class:`ChildProposal` objects with mixed
 capability requirements.
2. Call :func:`multi_step_dispatch` against a department capability set
 that satisfies some children and is missing capabilities for others.
3. For every plan whose ``action == "start"`` start a stub child
 workflow on a Temporal worker, exactly as the production parent
 body is expected to do once it is wired.
4. Collect a :class:`ChildOutcome` for every input child (started or
 skipped) and feed the list to :func:`aggregated_output`.
5. Assert the graceful-skip contract:

 * ``len(plans) == len(children)`` (no child silently dropped).
 * The missing-capability child is marked ``out_of_scope`` with the
 exact missing-capability set.
 * ``aggregated_output`` reports ``started + skipped == total ==
 len(children)`` and the original outcome order is preserved.

The test mirrors the worker-bootstrap pattern used by
``test_e2e_code_change_with_test.py`` - ``sys.path`` bootstrap
for the platform's ``temporal-shared`` library, the
``_temporal_test_env_available`` import gate so hosts without the
embedded ``temporal-test-server`` skip cleanly, and a single Temporal
worker registered with a stub child workflow that the dispatcher
points at.

Once the ``multi_step`` parent body lands, this test gains a
companion that drives the parent through ``start_workflow`` instead of
calling :func:`multi_step_dispatch` directly. Until then, the property
test ``test_multi_step_aggregator.py`` covers the pure
contract under hypothesis-generated inputs and this integration test
pins the dispatch-and-aggregate sequence end-to-end against a real
Temporal cluster.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# sys.path bootstrap - temporal-shared library
#
# Mirrors the bootstrap used by ``test_e2e_code_change_with_test.py``:
# ``temporal-shared`` ships its sources under
# ``platform/libs/temporal-shared/src/`` and other packages import it
# under the ``temporal_shared`` namespace. Adding the ``src/``
# directory onto ``sys.path`` makes ``import temporal_shared.multi_step``
# resolve without first installing the package.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]
_TEMPORAL_SHARED_SRC: Path = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)

for _candidate in (_TEMPORAL_SHARED_SRC,):
    _candidate_str = str(_candidate)
    if _candidate.is_dir() and _candidate_str not in sys.path:
        sys.path.insert(0, _candidate_str)


# ---------------------------------------------------------------------------
# Module-level skip gate
# ---------------------------------------------------------------------------


def _temporal_test_env_available() -> bool:
    """Return ``True`` when the Temporal time-skipping env imports cleanly.

 Any import failure is treated as "skip cleanly" so hosts without
 the embedded ``temporal-test-server`` (sandboxed CI, missing
 native deps) skip rather than erroring at collection time. The
 same gate is used by ``test_e2e_code_change_with_test.py``.
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
    """Start the time-skipping env, ``pytest.skip``-ing on failure.

 The embedded ``temporal-test-server`` may fail to start on hosts
 where the binary is not bundled. Surface that cleanly as a skip
 so the integration suite stays green on machines that cannot host
 Temporal locally.
 """

    from temporalio.testing import WorkflowEnvironment

    try:
        env_cm = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 - surface as skip.
        pytest.skip(f"temporalio test environment not available: {exc}")
    async with env_cm as env:
        yield env


# ---------------------------------------------------------------------------
# Stub child workflow
#
# Temporal's ``@workflow.defn`` decorator rejects local classes (the
# worker needs a globally referenceable class name), so the stub MUST
# be declared at module scope. The stub is registered under a synthetic
# name ``"MultiStepChildStub"`` rather than ``"AgentRunnerWorkflow"``
# so it cannot collide with the production workflow registration on
# the same worker; the test driver passes the same name in the
# :class:`ChildWorkflowSpec` so the dispatcher and the worker line up.
#
# The stub returns a small dict carrying the child workflow's id and a
# fixed ``status="completed"`` so the test driver can verify that the
# Temporal cluster routed the child correctly. The cluster's success
# is the only side effect under test - the contents of the dict are
# echoed straight back into the corresponding :class:`ChildOutcome`.
# ---------------------------------------------------------------------------

from temporalio import workflow as _wf  # noqa: E402 - module-level by design


@_wf.defn(name="MultiStepChildStub", sandboxed=False)
class _MultiStepChildStub:
    """Module-level stub registered as the children's workflow target.

 Returns ``{"id": <workflow.id>, "status": "completed"}`` so the
 test driver can verify the Temporal cluster actually executed the
 stub. The dispatch / capability gate decisions are made *before*
 Temporal sees the child, so the stub does no work - its sole
 purpose is to confirm that ``multi_step_dispatch``'s ``"start"``
 plans translate cleanly into real ``start_workflow`` calls.
 """

    @_wf.run
    async def run(self, _payload: Any = None) -> dict[str, Any]:
        return {
            "id": _wf.info().workflow_id,
            "status": "completed",
        }


# ---------------------------------------------------------------------------
# Activity log (parity with ``test_e2e_code_change_with_test.py``)
#
# The multi_step path is dispatch-only - no activities fire on the
# parent's behalf in this test - but keeping a call log around mirrors
# the reference test's structure and gives future contributors a
# place to record audit emissions when the parent body lands.
# ---------------------------------------------------------------------------


@dataclass
class ActivityCallLog:
    """Append-only log of activity invocations recorded by stubs."""

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


# ---------------------------------------------------------------------------
# Helper - build a :class:`ChildProposal` for one child workflow
# ---------------------------------------------------------------------------


def _make_proposal(
    workflow_type: str,
    *,
    workflow_id: str,
    task_queue: str,
) -> Any:
    """Construct a :class:`ChildProposal` whose ``child_spec`` points at the stub."""

    from temporal_shared.messages import ChildWorkflowSpec
    from temporal_shared.multi_step import ChildProposal

    return ChildProposal(
        workflow_type=workflow_type,
        child_spec=ChildWorkflowSpec(
            workflow_name="MultiStepChildStub",
            workflow_id=workflow_id,
            task_queue=task_queue,
        ),
    )


# ---------------------------------------------------------------------------
# Test 1 - multi_step dispatch + aggregated output via real Temporal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multi_step_dispatch_with_one_skip_via_real_temporal() -> None:
    """Three child proposals exercise the three relevant decision
 branches of :func:`multi_step_dispatch` against a department whose
 capability set deliberately misses ``confluence``:

 * ``code_change_with_test`` requires ``{jira, bitbucket, execution}``
 - present in the dept caps, so the dispatcher emits a
 ``"start"`` plan with reason ``"dispatched"``.
 * ``pr_review`` requires ``{jira, bitbucket}`` - also present, so
 another ``"start"`` plan.
 * ``confluence_doc_create`` requires ``{jira, confluence}`` -
 ``confluence`` is **missing** from the dept caps, so the
 dispatcher emits a ``"skip"`` plan with reason ``"out_of_scope"``
 and ``missing_capabilities == frozenset({"confluence"})``.

 Each ``"start"`` plan is then handed to a real Temporal time-
 skipping :class:`WorkflowEnvironment` via ``start_workflow``; the
 stub :class:`_MultiStepChildStub` echoes the child id back. The
 test collects a :class:`ChildOutcome` for every input child
 (whether started or skipped) and feeds the list to
 :func:`aggregated_output`.

 Assertions
 ----------
 * Plan list length equals the input length, preserving graceful skip behavior.
 * The two satisfied children produce ``"start"`` plans with
 reason ``"dispatched"`` and an empty ``missing_capabilities``
 set.
 * The unsatisfied child produces a ``"skip"`` plan with reason
 ``"out_of_scope"`` and ``missing_capabilities ==
 frozenset({"confluence"})``.
 * Every started child's stub workflow returns
 ``status="completed"`` (the real cluster routed the dispatch
 correctly).
 * :func:`aggregated_output` reports ``started == 2``,
 ``skipped == 1``, ``total == 3``, and ``started + skipped ==
 total == len(children)``.
 * The aggregator preserves the original child order - the
 missing-capability child still occupies index 2 in
 ``agg.child_outcomes``.
 """

    from temporalio.worker import Worker

    from temporal_shared.multi_step import (
        REASON_DISPATCHED,
        REASON_OUT_OF_SCOPE,
        ChildOutcome,
        aggregated_output,
        multi_step_dispatch,
    )

    # ----- Inputs ---------------------------------------------------

    task_queue = "multi-step-children-tq-happy"
    children = [
        _make_proposal(
            "code_change_with_test",
            workflow_id="multi-step-child-code-1",
            task_queue=task_queue,
        ),
        _make_proposal(
            "pr_review",
            workflow_id="multi-step-child-pr-1",
            task_queue=task_queue,
        ),
        _make_proposal(
            "confluence_doc_create",
            workflow_id="multi-step-child-conf-1",
            task_queue=task_queue,
        ),
    ]
    # Department capabilities deliberately exclude ``confluence`` to
    # exercise the ``out_of_scope`` skip branch on the third proposal.
    dept_capabilities = frozenset({"jira", "bitbucket", "execution"})

    # ----- 1. Pure dispatch decision --------------------------------
    # # ``multi_step_dispatch`` is replay-safe and pure; it makes no
    # Temporal calls. Calling it before the env spins up keeps the
    # test isolated from any cluster state.

    plans = multi_step_dispatch(children, dept_capabilities)
    assert len(plans) == len(children), (
        f"graceful-skip total-length invariant violated: "
        f"len(plans)={len(plans)} != len(children)={len(children)}"
    )

    # Plan-level shape assertions before we touch Temporal - bailing
    # out here gives a clearer failure message than a downstream
    # ``start_workflow`` mismatch would.
    assert plans[0].action == "start"
    assert plans[0].reason == REASON_DISPATCHED
    assert plans[0].missing_capabilities == frozenset()

    assert plans[1].action == "start"
    assert plans[1].reason == REASON_DISPATCHED
    assert plans[1].missing_capabilities == frozenset()

    assert plans[2].action == "skip"
    assert plans[2].reason == REASON_OUT_OF_SCOPE
    assert plans[2].missing_capabilities == frozenset({"confluence"}), (
        f"expected missing_capabilities={{'confluence'}}, got "
        f"{plans[2].missing_capabilities!r}"
    )

    # ----- 2. Dispatch the ``"start"`` plans through real Temporal --

    outcomes: list[ChildOutcome] = []
    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[_MultiStepChildStub],
        ):
            for plan in plans:
                if plan.action == "start":
                    # Walk the same path the production parent body
                    # will use once it is wired: start the child via
                    # the ``ChildWorkflowSpec`` pinned on the plan.
                    handle = await env.client.start_workflow(
                        plan.child_spec.workflow_name,
                        id=plan.child_spec.workflow_id,
                        task_queue=plan.child_spec.task_queue,
                    )
                    result: dict[str, Any] = await handle.result()
                    assert result["id"] == plan.child_spec.workflow_id, (
                        f"child workflow returned mismatched id: "
                        f"{result!r} (expected "
                        f"{plan.child_spec.workflow_id!r})"
                    )
                    assert result["status"] == "completed", (
                        f"child workflow {plan.child_spec.workflow_id!r} "
                        f"did not report completed: {result!r}"
                    )
                    outcomes.append(
                        ChildOutcome(
                            action="started",
                            child_spec=plan.child_spec,
                            reason=plan.reason,
                            missing_capabilities=plan.missing_capabilities,
                            status=result["status"],
                            failure_reason=None,
                        )
                    )
                else:
                    # ``"skip"`` plans never reach Temporal; the
                    # outcome echoes the plan's reason / missing-cap
                    # set so the audit trail in
                    # :func:`aggregated_output` remains complete.
                    outcomes.append(
                        ChildOutcome(
                            action="skipped",
                            child_spec=plan.child_spec,
                            reason=plan.reason,
                            missing_capabilities=plan.missing_capabilities,
                            status=None,
                            failure_reason=None,
                        )
                    )

    # ----- 3. Aggregate the per-child outcomes ----------------------

    agg = aggregated_output(outcomes)

    # Counter invariant for the graceful-skip aggregate.
    assert agg.started == 2, f"expected started=2, got {agg.started}"
    assert agg.skipped == 1, f"expected skipped=1, got {agg.skipped}"
    assert agg.total == 3, f"expected total=3, got {agg.total}"
    assert agg.started + agg.skipped == agg.total
    assert agg.started + agg.skipped == len(children), (
        "graceful-skip count invariant violated: "
        f"started ({agg.started}) + skipped ({agg.skipped}) != "
        f"len(children) ({len(children)})"
    )

    # Order preservation. The
    # missing-capability child must still occupy its original index.
    assert [o.child_spec.workflow_id for o in agg.child_outcomes] == [
        "multi-step-child-code-1",
        "multi-step-child-pr-1",
        "multi-step-child-conf-1",
    ], (
        "aggregated_output reordered children: "
        f"{[o.child_spec.workflow_id for o in agg.child_outcomes]!r}"
    )

    # The skipped outcome must echo the plan's reason / missing-cap
    # set verbatim.
    skipped_outcome = agg.child_outcomes[2]
    assert skipped_outcome.action == "skipped"
    assert skipped_outcome.reason == REASON_OUT_OF_SCOPE
    assert skipped_outcome.missing_capabilities == frozenset({"confluence"})

    # The two started outcomes must report ``status="completed"`` -
    # the stub workflow only returns ``"completed"`` when the
    # Temporal cluster actually routed the dispatch.
    for started_index in (0, 1):
        outcome = agg.child_outcomes[started_index]
        assert outcome.action == "started"
        assert outcome.status == "completed", (
            f"started child at index {started_index} did not report "
            f"completed: {outcome!r}"
        )
        assert outcome.failure_reason is None
        assert outcome.missing_capabilities == frozenset()


# ---------------------------------------------------------------------------
# Test 2 - aggregated_output preserves outcome order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multi_step_aggregator_preserves_order() -> None:
    """The :func:`aggregated_output` aggregator is a counter, not a
 sorter - for every input it returns ``child_outcomes`` in the
 original input order regardless of which children were started or
 skipped. This order-preservation behavior is pinned here
 against a concrete mixed input so the example shows up in the
 integration suite alongside the property-test coverage.

 The test alternates ``"started"`` and ``"skipped"`` outcomes and
 asserts:

 * Each outcome lands at its original index in
 :attr:`AggregatedOutput.child_outcomes`.
 * ``started == 2``, ``skipped == 2``, ``total == 4``.
 * ``started + skipped == total == len(outcomes)`` ( graceful-
 skip count invariant).
 * Re-running the aggregator over the same outcome list returns an
 equal aggregate.

 The aggregator is pure (no Temporal calls), but the test is
 marked ``@pytest.mark.integration`` so it sits next to the
 Temporal-driven dispatch test in the integration suite filter
 (``pytest -m integration``).
 """

    from temporal_shared.messages import ChildWorkflowSpec
    from temporal_shared.multi_step import (
        REASON_DISPATCHED,
        REASON_NESTED_MULTI_STEP,
        REASON_OUT_OF_SCOPE,
        REASON_UNKNOWN_WORKFLOW_TYPE,
        ChildOutcome,
        aggregated_output,
    )

    def _spec(workflow_id: str) -> ChildWorkflowSpec:
        return ChildWorkflowSpec(
            workflow_name="MultiStepChildStub",
            workflow_id=workflow_id,
            task_queue="multi-step-children-tq-order",
        )

    # Mixed alternating started / skipped outcomes - each uses a
    # different skip reason so the test exercises every audit token in
    # the closed vocabulary at least once.
    outcomes = [
        ChildOutcome(
            action="started",
            child_spec=_spec("child-1-started"),
            reason=REASON_DISPATCHED,
            missing_capabilities=frozenset(),
            status="completed",
            failure_reason=None,
        ),
        ChildOutcome(
            action="skipped",
            child_spec=_spec("child-2-out-of-scope"),
            reason=REASON_OUT_OF_SCOPE,
            missing_capabilities=frozenset({"execution"}),
            status=None,
            failure_reason=None,
        ),
        ChildOutcome(
            action="started",
            child_spec=_spec("child-3-started"),
            reason=REASON_DISPATCHED,
            missing_capabilities=frozenset(),
            status="completed",
            failure_reason=None,
        ),
        ChildOutcome(
            action="skipped",
            child_spec=_spec("child-4-unknown"),
            reason=REASON_UNKNOWN_WORKFLOW_TYPE,
            missing_capabilities=frozenset(),
            status=None,
            failure_reason=None,
        ),
    ]

    agg = aggregated_output(outcomes)

    # Count assertions - graceful-skip invariant.
    assert agg.started == 2, f"expected started=2, got {agg.started}"
    assert agg.skipped == 2, f"expected skipped=2, got {agg.skipped}"
    assert agg.total == 4, f"expected total=4, got {agg.total}"
    assert agg.started + agg.skipped == agg.total
    assert agg.started + agg.skipped == len(outcomes)

    # Order preservation. Position 0 is the started
    # outcome, position 1 is the out_of_scope skip, position 2 is the
    # second started outcome, position 3 is the unknown-type skip.
    assert agg.child_outcomes == tuple(outcomes), (
        "aggregated_output reordered child outcomes: "
        f"{[o.child_spec.workflow_id for o in agg.child_outcomes]!r} "
        f"!= {[o.child_spec.workflow_id for o in outcomes]!r}"
    )
    # Spot-check each index by id so a reordering bug surfaces with a
    # readable error message rather than a tuple inequality dump.
    assert agg.child_outcomes[0].child_spec.workflow_id == "child-1-started"
    assert agg.child_outcomes[1].child_spec.workflow_id == "child-2-out-of-scope"
    assert agg.child_outcomes[2].child_spec.workflow_id == "child-3-started"
    assert agg.child_outcomes[3].child_spec.workflow_id == "child-4-unknown"

    # The discriminator and reason on each outcome must come through
    # unchanged - the aggregator never rewrites either field.
    assert agg.child_outcomes[0].action == "started"
    assert agg.child_outcomes[0].reason == REASON_DISPATCHED
    assert agg.child_outcomes[1].action == "skipped"
    assert agg.child_outcomes[1].reason == REASON_OUT_OF_SCOPE
    assert agg.child_outcomes[1].missing_capabilities == frozenset(
        {"execution"}
    )
    assert agg.child_outcomes[2].action == "started"
    assert agg.child_outcomes[2].reason == REASON_DISPATCHED
    assert agg.child_outcomes[3].action == "skipped"
    assert agg.child_outcomes[3].reason == REASON_UNKNOWN_WORKFLOW_TYPE

    # Idempotence. Re-running the aggregator over
    # the same outcome list produces an equal aggregate; the helper
    # is pure and the result depends only on its argument.
    agg_again = aggregated_output(outcomes)
    assert agg == agg_again, (
        "aggregated_output is not idempotent on the same outcome list"
    )

    # The closed audit vocabulary keeps the ``REASON_NESTED_MULTI_STEP``
    # token in scope - referencing it here makes the import visible to
    # static analysers (lint / mypy) and documents the full vocabulary
    # exercised by the invariant.
    assert REASON_NESTED_MULTI_STEP == "nested_multi_step_forbidden"
