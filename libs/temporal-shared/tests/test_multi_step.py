"""Unit tests for ``temporal_shared.multi_step``.

Validates the pure :func:`multi_step_dispatch` decision function and the
:func:`aggregated_output` aggregator against
§"Workflow Type Routing" (multi_step graceful skip), the invariant.

The dedicated property-test suite for this module lives in
this file covers concrete examples and the validation error paths so a
``pytest libs/temporal-shared`` run remains hermetic.

"""

from __future__ import annotations

import pytest

from temporal_shared.messages import ChildWorkflowSpec
from temporal_shared.multi_step import (
    REASON_DISPATCHED,
    REASON_NESTED_MULTI_STEP,
    REASON_OUT_OF_SCOPE,
    REASON_UNKNOWN_WORKFLOW_TYPE,
    AggregatedOutput,
    ChildOutcome,
    ChildPlan,
    ChildProposal,
    InvariantViolation,
    aggregated_output,
    multi_step_dispatch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(workflow_id: str = "agent-1") -> ChildWorkflowSpec:
    """Construct a minimal :class:`ChildWorkflowSpec` for tests."""
    return ChildWorkflowSpec(
        workflow_name="AgentRunnerWorkflow",
        workflow_id=workflow_id,
        task_queue="agent-runner-tq",
    )


# ---------------------------------------------------------------------------
# multi_step_dispatch - happy paths
# ---------------------------------------------------------------------------


class TestMultiStepDispatchHappyPath:
    """Concrete examples for the core start/skip discriminator."""

    def test_empty_children_returns_empty_list(self) -> None:
        assert multi_step_dispatch([], frozenset({"jira"})) == []

    def test_all_children_have_caps_returns_all_start(self) -> None:
        """

        When every child's required capabilities are satisfied, every
        plan is ``"start"`` with reason ``"dispatched"``.
        """
        children = [
            ChildProposal("pr_review", _spec("c-1")),
            ChildProposal("noop_test", _spec("c-2")),
        ]
        plans = multi_step_dispatch(
            children, frozenset({"jira", "bitbucket"})
        )
        assert len(plans) == len(children)
        assert all(p.action == "start" for p in plans)
        assert all(p.reason == REASON_DISPATCHED for p in plans)
        assert all(p.missing_capabilities == frozenset() for p in plans)

    def test_missing_capability_yields_skip_with_missing_set(self) -> None:
        """

        ``code_change_with_test`` requires ``execution`` - a dept with
        only ``jira`` + ``bitbucket`` skips it as ``out_of_scope``
        with the exact missing set.
        """
        children = [
            ChildProposal("code_change_with_test", _spec("c-1")),
        ]
        plans = multi_step_dispatch(
            children, frozenset({"jira", "bitbucket"})
        )
        assert plans[0].action == "skip"
        assert plans[0].reason == REASON_OUT_OF_SCOPE
        assert plans[0].missing_capabilities == frozenset({"execution"})

    def test_partial_skip_preserves_total_length(self) -> None:
        """

        Mixed children - some with all caps, some without - produce a
        plan list of exactly ``len(children)``: graceful skip means no
        child is ever dropped.
        """
        children = [
            ChildProposal("pr_review", _spec("c-1")),
            ChildProposal("code_change_with_test", _spec("c-2")),
            ChildProposal("noop_test", _spec("c-3")),
            ChildProposal(
                "research_with_web", _spec("c-4")
            ),  # needs web_search
        ]
        plans = multi_step_dispatch(
            children, frozenset({"jira", "bitbucket"})
        )
        assert len(plans) == len(children)

        # pr_review and noop_test → start (have jira+bitbucket / jira)
        assert plans[0].action == "start"
        assert plans[2].action == "start"

        # code_change_with_test → skip (missing execution)
        assert plans[1].action == "skip"
        assert plans[1].reason == REASON_OUT_OF_SCOPE
        assert plans[1].missing_capabilities == frozenset({"execution"})

        # research_with_web → skip (missing web_search)
        assert plans[3].action == "skip"
        assert plans[3].reason == REASON_OUT_OF_SCOPE
        assert plans[3].missing_capabilities == frozenset({"web_search"})

    def test_set_argument_accepted_alongside_frozenset(self) -> None:
        """

        ``dept_capabilities`` accepts both :class:`set` and
        :class:`frozenset` for ergonomics - the function normalises
        internally.
        """
        children = [ChildProposal("pr_review", _spec("c-1"))]
        plans_frozen = multi_step_dispatch(
            children, frozenset({"jira", "bitbucket"})
        )
        plans_mut = multi_step_dispatch(
            children, {"jira", "bitbucket"}
        )
        assert plans_frozen == plans_mut

    def test_iterable_argument_accepted_alongside_sequence(self) -> None:
        """

        The ``children`` parameter accepts a generator / iterator, not
        only sequences.
        """
        children_gen = (
            ChildProposal("pr_review", _spec(f"c-{i}")) for i in range(3)
        )
        plans = multi_step_dispatch(children_gen, frozenset({"jira", "bitbucket"}))
        assert len(plans) == 3
        assert all(p.action == "start" for p in plans)

    def test_dispatch_is_deterministic_for_same_input(self) -> None:
        """

        Two calls with the same inputs return equal plan lists - the
        function is pure.
        """
        children = [
            ChildProposal("pr_review", _spec("c-1")),
            ChildProposal("code_change_with_test", _spec("c-2")),
        ]
        caps = frozenset({"jira", "bitbucket"})
        assert multi_step_dispatch(children, caps) == multi_step_dispatch(
            children, caps
        )


# ---------------------------------------------------------------------------
# multi_step_dispatch - edge cases (unknown workflow type, nested multi_step)
# ---------------------------------------------------------------------------


class TestMultiStepDispatchEdgeCases:
    """Coverage for the guard branches that keep the dispatcher total."""

    def test_unknown_workflow_type_yields_skip(self) -> None:
        """

        The LLM may occasionally emit a workflow type outside the
        closed vocabulary - the dispatcher records it and continues
        rather than raising :class:`KeyError`.
        """
        children = [ChildProposal("code_change_invented", _spec("c-1"))]
        plans = multi_step_dispatch(children, frozenset({"jira"}))
        assert plans[0].action == "skip"
        assert plans[0].reason == REASON_UNKNOWN_WORKFLOW_TYPE
        assert plans[0].missing_capabilities == frozenset()

    def test_nested_multi_step_yields_skip_with_dedicated_reason(self) -> None:
        """

        ``multi_step`` cannot nest inside ``multi_step``; the
        dispatcher catches it with a dedicated reason rather than
        emitting an unknown-workflow-type skip.
        """
        children = [ChildProposal("multi_step", _spec("c-1"))]
        plans = multi_step_dispatch(children, frozenset({"jira"}))
        assert plans[0].action == "skip"
        assert plans[0].reason == REASON_NESTED_MULTI_STEP
        assert plans[0].missing_capabilities == frozenset()

    def test_dept_with_no_capabilities_skips_everything(self) -> None:
        children = [
            ChildProposal("pr_review", _spec("c-1")),
            ChildProposal("noop_test", _spec("c-2")),
        ]
        plans = multi_step_dispatch(children, frozenset())
        assert len(plans) == len(children)
        assert all(p.action == "skip" for p in plans)
        assert all(p.reason == REASON_OUT_OF_SCOPE for p in plans)

    def test_skip_carries_original_child_spec_unchanged(self) -> None:
        """

        Both ``"start"`` and ``"skip"`` plans must echo the proposal's
        :class:`ChildWorkflowSpec` byte-for-byte so the parent has the
        full audit context.
        """
        original_spec = _spec("agent-original")
        children = [ChildProposal("code_change_with_test", original_spec)]
        plans = multi_step_dispatch(children, frozenset({"jira"}))
        assert plans[0].child_spec is original_spec


# ---------------------------------------------------------------------------
# aggregated_output - happy paths
# ---------------------------------------------------------------------------


class TestAggregatedOutputHappyPath:
    """Concrete examples for the started/skipped counter."""

    def test_empty_outcomes_yields_zero_aggregate(self) -> None:
        agg = aggregated_output([])
        assert agg.started == 0
        assert agg.skipped == 0
        assert agg.total == 0
        assert agg.child_outcomes == ()

    def test_invariant_started_plus_skipped_equals_total(self) -> None:
        """

        For every input the aggregate satisfies
        ``started + skipped == len(child_outcomes)``.
        """
        outcomes = [
            ChildOutcome(
                "started",
                _spec("c-1"),
                REASON_DISPATCHED,
                status="completed",
            ),
            ChildOutcome(
                "skipped",
                _spec("c-2"),
                REASON_OUT_OF_SCOPE,
                missing_capabilities=frozenset({"execution"}),
            ),
            ChildOutcome(
                "started",
                _spec("c-3"),
                REASON_DISPATCHED,
                status="failed",
                failure_reason="child_failed",
            ),
        ]
        agg = aggregated_output(outcomes)
        assert agg.started == 2
        assert agg.skipped == 1
        assert agg.total == 3
        assert agg.started + agg.skipped == len(outcomes)
        assert agg.child_outcomes == tuple(outcomes)

    def test_preserves_outcome_order(self) -> None:
        outcomes = [
            ChildOutcome("skipped", _spec("a"), REASON_OUT_OF_SCOPE),
            ChildOutcome("started", _spec("b"), REASON_DISPATCHED),
            ChildOutcome("skipped", _spec("c"), REASON_NESTED_MULTI_STEP),
        ]
        agg = aggregated_output(outcomes)
        assert [o.child_spec.workflow_id for o in agg.child_outcomes] == [
            "a",
            "b",
            "c",
        ]


# ---------------------------------------------------------------------------
# aggregated_output - invariant enforcement
# ---------------------------------------------------------------------------


class TestAggregatedOutputInvariantEnforcement:
    """Coverage for :class:`InvariantViolation` paths."""

    def test_unknown_action_raises_invariant_violation(self) -> None:
        """

        An outcome whose ``action`` is outside ``{"started",
        "skipped"}`` would silently lose a child from the summary;
        the aggregator surfaces it as :class:`InvariantViolation`.
        """
        # Bypass the Literal type check by constructing via ``object``.
        bad_outcome = ChildOutcome.__new__(ChildOutcome)
        # frozen dataclass - use object.__setattr__ for fields
        object.__setattr__(bad_outcome, "action", "exploded")
        object.__setattr__(bad_outcome, "child_spec", _spec("c-1"))
        object.__setattr__(bad_outcome, "reason", "unexpected")
        object.__setattr__(bad_outcome, "missing_capabilities", frozenset())
        object.__setattr__(bad_outcome, "status", None)
        object.__setattr__(bad_outcome, "failure_reason", None)

        with pytest.raises(InvariantViolation):
            aggregated_output([bad_outcome])

    def test_constructor_rejects_mismatched_total(self) -> None:
        """

        Constructing :class:`AggregatedOutput` with mismatched
        counters raises immediately - even without going through the
        aggregator function.
        """
        with pytest.raises(InvariantViolation):
            AggregatedOutput(
                started=2,
                skipped=1,
                total=5,  # 2 + 1 != 5
                child_outcomes=(),
            )

    def test_constructor_rejects_mismatched_child_outcomes_length(
        self,
    ) -> None:
        with pytest.raises(InvariantViolation):
            AggregatedOutput(
                started=1,
                skipped=0,
                total=1,
                child_outcomes=(),  # length 0 != total 1
            )

    def test_constructor_rejects_negative_counts(self) -> None:
        with pytest.raises(InvariantViolation):
            AggregatedOutput(
                started=-1,
                skipped=2,
                total=1,
                child_outcomes=(
                    ChildOutcome("skipped", _spec("a"), REASON_OUT_OF_SCOPE),
                ),
            )


# ---------------------------------------------------------------------------
# Plan / outcome shape sanity
# ---------------------------------------------------------------------------


class TestChildPlanShape:
    """Shape contract sanity - frozen, hashable, and discriminator typed."""

    def test_child_plan_is_frozen(self) -> None:
        plan = ChildPlan(
            action="start",
            child_spec=_spec("c-1"),
            reason=REASON_DISPATCHED,
        )
        with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
            plan.action = "skip"  # type: ignore[misc]

    def test_child_outcome_is_frozen(self) -> None:
        outcome = ChildOutcome(
            action="started",
            child_spec=_spec("c-1"),
            reason=REASON_DISPATCHED,
        )
        with pytest.raises((AttributeError, Exception)):
            outcome.status = "tampered"  # type: ignore[misc]

    def test_aggregated_output_is_frozen(self) -> None:
        agg = aggregated_output([])
        with pytest.raises((AttributeError, Exception)):
            agg.total = 99  # type: ignore[misc]
