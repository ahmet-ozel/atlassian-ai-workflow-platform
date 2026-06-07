"""``multi_step`` graceful skip aggregator.



Invariant statement
------------------------------------------------------------

The ``multi_step`` workflow type orchestrates *N* child workflows on
behalf of a single Jira issue. The pure helpers under test
(:func:`temporal_shared.multi_step.multi_step_dispatch` and:func:`temporal_shared.multi_step.aggregated_output`) implement the
*graceful skip* contract used by workflow type routing:

* Each child whose required capabilities are satisfied gets a
 ``"start"``:class:`ChildPlan` with reason ``"dispatched"``.
* Each child whose ``workflow_type`` is unknown, or which would nest a
 ``multi_step`` inside a ``multi_step``, or whose dept is missing one
 or more capabilities, gets a ``"skip"`` plan with the appropriate
 audit reason and (for the missing-capability case) the exact
 missing-capability set.
* No child is ever silently dropped: ``len(plans) == len(children)``
 for every input - the *total-length* invariant.
* The aggregator over runtime outcomes carries the parallel invariant
 ``started + skipped == total == len(child_outcomes)``.
* Both helpers are pure (no I/O, no ``datetime`` / ``random`` /
 ``uuid``) and therefore deterministic - repeating a call with the
 same inputs returns equal output.

For any hypothesis-generated tuple
``(children, dept_capabilities)`` the dispatcher SHALL satisfy:

**Total length / no child dropped (graceful skip).**
 ``len(multi_step_dispatch(children, caps)) == len(children)``.
 Every input child appears in the plan list exactly once and in
 the original position - graceful skip means a missing capability
 turns a child into a skip plan but never removes it from the
 summary.

**Order preserved.**
 For every index ``i``, ``plans[i].child_spec is children[i].child_spec``.
 The dispatcher echoes the proposal's:class:`ChildWorkflowSpec`
 unchanged so the parent has the full audit context for both the
 dispatch path and the skip path.

**Discriminator is total.**
 Every ``ChildPlan.action`` belongs to ``{"start", "skip"}`` and
 every ``ChildPlan.reason`` belongs to the four-element vocabulary
 ``{REASON_DISPATCHED, REASON_OUT_OF_SCOPE,
 REASON_UNKNOWN_WORKFLOW_TYPE, REASON_NESTED_MULTI_STEP}``.

**Skip reason classification.**
 For every plan with ``action == "skip"`` the reason is one of:

 * ``"nested_multi_step_forbidden"`` iff
 ``child.workflow_type == "multi_step"``;
 * ``"unknown_workflow_type"`` iff ``workflow_type`` is not a key
 of:data:`WORKFLOW_TYPE_CAPABILITIES` (and is not the
 ``"multi_step"`` meta-type);
 * ``"out_of_scope"`` iff ``workflow_type`` *is* a known key but
 ``required_capabilities(workflow_type) - dept_capabilities``
 is non-empty.

**Missing-capability fidelity.**
 ``plan.missing_capabilities ==
 required_capabilities(workflow_type) - dept_capabilities`` for
 every ``out_of_scope`` skip plan, and ``frozenset`` for every
 other plan (start, unknown, nested).

**Determinism / idempotence of dispatch.**
 ``multi_step_dispatch(children, caps) ==
 multi_step_dispatch(children, caps)`` for every legal input -
 the helper is pure. Equivalently: applying ``dispatch`` twice to
 the same arguments yields equal plan lists; the *aggregator step*
 is therefore also idempotent in the engineering sense (running
 the pipeline twice with the same set yields the same result).

For any hypothesis-generated outcome list ``outcomes`` the aggregator
SHALL satisfy:

**Empty input → empty aggregate.**
 ``aggregated_output([])`` returns
 ``AggregatedOutput(started=0, skipped=0, total=0,
 child_outcomes=)``.

**Counter invariant.**
 ``started + skipped == total == len(child_outcomes)`` for every
 legal input (no outcome is dropped, none is double-counted).

**Order preserved.**
 ``agg.child_outcomes == tuple(outcomes)`` - the aggregator never
 reorders outcomes.

**Aggregator idempotence.**
 ``aggregated_output(outcomes) ==
 aggregated_output(outcomes)`` for every legal input. Re-running
 the aggregator over the same outcome list produces an equal
 aggregate - the helper is pure and the result depends only on
 its argument.

These properties together pin the contract that
``AgentRunnerWorkflow.multi_step`` and the parent:class:`AutomationWorkflow.multi_step` branch consume.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from temporal_shared.capabilities import (
    WORKFLOW_TYPE_CAPABILITIES,
    required_capabilities,
)
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
    aggregated_output,
    multi_step_dispatch,
)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: The closed simple-vocabulary capability set understood by
#::func:`required_capabilities` (split caps collapsed to service names).
SIMPLE_CAPABILITIES: frozenset[str] = frozenset(
    {"jira", "bitbucket", "confluence", "execution", "web_search"}
)

#: Every legal workflow-type key - including ``multi_step`` itself, so
#: the nested-multi_step skip branch is exercised by the dispatcher.
KNOWN_WORKFLOW_TYPES: tuple[str, ...] = tuple(WORKFLOW_TYPE_CAPABILITIES.keys())


# Strategy for ``ChildWorkflowSpec`` - keep the surface tiny because the
# spec itself is opaque to the dispatcher (it is echoed unchanged).
spec_strategy = st.builds(
    ChildWorkflowSpec,
    workflow_name=st.sampled_from(
        ("AgentRunnerWorkflow", "ExecutionRunWorkflow")
    ),
    workflow_id=st.text(
        alphabet=st.characters(
            min_codepoint=ord("a"),
            max_codepoint=ord("z"),
            whitelist_categories=("Ll", "Nd"),
            whitelist_characters="-_",
        ),
        min_size=1,
        max_size=24,
    ).filter(lambda s: bool(s.strip())),
    task_queue=st.sampled_from(
        ("agent-runner-tq", "execution-runner-tq", "automation-tq")
    ),
)


# Strategy for an arbitrary "workflow_type" string the LLM might emit.
# Mixes the known closed set with a small sample of plausible-but-bogus
# strings so the unknown-workflow-type branch is exercised.
workflow_type_strategy = st.one_of(
    st.sampled_from(KNOWN_WORKFLOW_TYPES),
    st.sampled_from(
        (
            "code_change_invented",
            "pr_close",
            "definitely_not_a_workflow",
            "",
            "MULTI_STEP",  # case-sensitive - must miss the table
        )
    ),
)


@st.composite
def _child_proposal(draw: st.DrawFn) -> ChildProposal:
    return ChildProposal(
        workflow_type=draw(workflow_type_strategy),
        child_spec=draw(spec_strategy),
    )


children_strategy = st.lists(_child_proposal(), min_size=0, max_size=8)

#: Dept capabilities are drawn from the simple vocabulary as a
#: frozenset (the dispatcher accepts both ``frozenset`` and ``set``;
#: we cover both shapes inline below for the determinism property).
dept_caps_strategy = st.sets(
    st.sampled_from(tuple(SIMPLE_CAPABILITIES)),
    min_size=0,
    max_size=len(SIMPLE_CAPABILITIES),
).map(frozenset)


# ---------------------------------------------------------------------------
# Outcome strategy - drives the aggregator properties
# ---------------------------------------------------------------------------


@st.composite
def _child_outcome(draw: st.DrawFn) -> ChildOutcome:
    """Generate a syntactically-legal:class:`ChildOutcome`.

 ``action`` is constrained to ``{"started", "skipped"}`` (the
 aggregator raises:class:`InvariantViolation` outside that set -
 the unit test layer covers that path). Reasons and capability
 sets are sampled from the four-element audit vocabulary so the
 invariants under test reflect the production callers' behaviour.
 """

    action = draw(st.sampled_from(("started", "skipped")))
    spec = draw(spec_strategy)
    if action == "started":
        return ChildOutcome(
            action="started",
            child_spec=spec,
            reason=REASON_DISPATCHED,
            missing_capabilities=frozenset(),
            status=draw(st.sampled_from(("completed", "failed", None))),
            failure_reason=draw(
                st.one_of(st.none(), st.sampled_from(("child_failed",)))
            ),
        )

    reason = draw(
        st.sampled_from(
            (
                REASON_OUT_OF_SCOPE,
                REASON_UNKNOWN_WORKFLOW_TYPE,
                REASON_NESTED_MULTI_STEP,
            )
        )
    )
    if reason == REASON_OUT_OF_SCOPE:
        missing = draw(
            st.sets(
                st.sampled_from(tuple(SIMPLE_CAPABILITIES)),
                min_size=1,
                max_size=len(SIMPLE_CAPABILITIES),
            ).map(frozenset)
        )
    else:
        missing = frozenset()

    return ChildOutcome(
        action="skipped",
        child_spec=spec,
        reason=reason,
        missing_capabilities=missing,
        status=None,
        failure_reason=None,
    )


outcomes_strategy = st.lists(_child_outcome(), min_size=0, max_size=8)


# ---------------------------------------------------------------------------
# invariant - multi_step_dispatch
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(children=children_strategy, caps=dept_caps_strategy)
def test_dispatch_total_length_invariant(
    children: list[ChildProposal], caps: frozenset[str]
) -> None:
    """No child is ever dropped during graceful skip dispatch.

 Graceful skip means no child is ever dropped:
 ``len(plans) == len(children)`` for every input.
 """

    plans = multi_step_dispatch(children, caps)
    assert len(plans) == len(children)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(children=children_strategy, caps=dept_caps_strategy)
def test_dispatch_preserves_order_and_child_spec_identity(
    children: list[ChildProposal], caps: frozenset[str]
) -> None:
    """Dispatch preserves order and child spec identity.

 Plan at index ``i`` references the same:class:`ChildWorkflowSpec`
 (by identity) as the input proposal at the same index; no reordering,
 no copying.
 """

    plans = multi_step_dispatch(children, caps)
    for i, plan in enumerate(plans):
        assert plan.child_spec is children[i].child_spec


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(children=children_strategy, caps=dept_caps_strategy)
def test_dispatch_action_and_reason_are_in_closed_vocabulary(
    children: list[ChildProposal], caps: frozenset[str]
) -> None:
    """Dispatch actions and reasons stay in the closed vocabulary.

 Every ``ChildPlan.action`` is in ``{"start", "skip"}`` and
 every ``reason`` is in the four-token closed audit vocabulary.
 """

    plans = multi_step_dispatch(children, caps)
    legal_actions = {"start", "skip"}
    legal_reasons = {
        REASON_DISPATCHED,
        REASON_OUT_OF_SCOPE,
        REASON_UNKNOWN_WORKFLOW_TYPE,
        REASON_NESTED_MULTI_STEP,
    }
    for plan in plans:
        assert isinstance(plan, ChildPlan)
        assert plan.action in legal_actions
        assert plan.reason in legal_reasons


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(children=children_strategy, caps=dept_caps_strategy)
def test_dispatch_skip_reason_classification(
    children: list[ChildProposal], caps: frozenset[str]
) -> None:
    """Skip reasons match the workflow routing table.

 Every skip plan's ``reason`` matches the rule table:

 * ``"multi_step"`` workflow type → ``nested_multi_step_forbidden``
 * unknown workflow type → ``unknown_workflow_type``
 * known type with missing caps → ``out_of_scope``

 And every ``"start"`` plan carries reason ``dispatched`` and an
 empty ``missing_capabilities`` set.
 """

    plans = multi_step_dispatch(children, caps)
    for child, plan in zip(children, plans, strict=True):
        wf_type = child.workflow_type
        if plan.action == "start":
            assert plan.reason == REASON_DISPATCHED
            assert plan.missing_capabilities == frozenset()
            # Sanity: the child's workflow type is a known key whose
            # required caps are a subset of dept caps.
            assert wf_type in WORKFLOW_TYPE_CAPABILITIES
            assert required_capabilities(wf_type) <= caps
            continue

        # action == "skip" - discriminate the reason.
        if wf_type == "multi_step":
            assert plan.reason == REASON_NESTED_MULTI_STEP
            assert plan.missing_capabilities == frozenset()
        elif wf_type not in WORKFLOW_TYPE_CAPABILITIES:
            assert plan.reason == REASON_UNKNOWN_WORKFLOW_TYPE
            assert plan.missing_capabilities == frozenset()
        else:
            assert plan.reason == REASON_OUT_OF_SCOPE
            expected_missing = required_capabilities(wf_type) - caps
            # Exact missing-capability fidelity for out_of_scope
            # skips.
            assert plan.missing_capabilities == expected_missing
            assert plan.missing_capabilities  # non-empty by construction


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(children=children_strategy, caps=dept_caps_strategy)
def test_dispatch_is_deterministic_and_idempotent(
    children: list[ChildProposal], caps: frozenset[str]
) -> None:
    """Dispatch is deterministic and idempotent.

 Running the dispatch twice with the same arguments returns
 equal plan lists. The aggregator-step contract is therefore
 *idempotent* in the engineering sense (the second pass through
 the pipeline does not change the result).

 Also asserts that the helper accepts both ``frozenset`` and
 ``set`` for ``dept_capabilities`` and that the two shapes produce
 the same plans (the helper normalises internally).
 """

    plans_a = multi_step_dispatch(children, caps)
    plans_b = multi_step_dispatch(children, caps)
    assert plans_a == plans_b

    # ``dept_capabilities`` accepts both frozenset and mutable set -
    # the dispatcher must normalise so the choice does not bleed into
    # the result.
    plans_set = multi_step_dispatch(children, set(caps))
    assert plans_a == plans_set


# ---------------------------------------------------------------------------
# invariant - aggregated_output
# ---------------------------------------------------------------------------


def test_aggregated_output_empty_input() -> None:
    """Empty aggregate input produces the zero aggregate.

 ``aggregated_output([])`` is the zero aggregate.

 Pinned as an example-based test (no Hypothesis input) because the
 invariant collapses to a single concrete value.
 """

    agg = aggregated_output([])
    assert agg == AggregatedOutput(
        started=0, skipped=0, total=0, child_outcomes=()
    )


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(outcomes=outcomes_strategy)
def test_aggregated_output_counter_invariant(
    outcomes: list[ChildOutcome],
) -> None:
    """Aggregated counters match the outcome list length.

 ``started + skipped == total == len(child_outcomes)``.

 Counts every ``"started"`` and ``"skipped"`` outcome exactly
 once and never drops a child from the summary.
 """

    agg = aggregated_output(outcomes)

    expected_started = sum(1 for o in outcomes if o.action == "started")
    expected_skipped = sum(1 for o in outcomes if o.action == "skipped")

    assert agg.started == expected_started
    assert agg.skipped == expected_skipped
    assert agg.total == len(outcomes)
    assert agg.started + agg.skipped == agg.total
    assert agg.started + agg.skipped == len(outcomes)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(outcomes=outcomes_strategy)
def test_aggregated_output_preserves_order(
    outcomes: list[ChildOutcome],
) -> None:
    """Aggregator preserves outcome order.

 ``agg.child_outcomes == tuple(outcomes)``. The aggregator
 is a counter, not a sorter - every outcome's relative position
 in the input list is preserved in the output tuple.
 """

    agg = aggregated_output(outcomes)
    assert agg.child_outcomes == tuple(outcomes)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(outcomes=outcomes_strategy)
def test_aggregated_output_is_idempotent(
    outcomes: list[ChildOutcome],
) -> None:
    """Aggregator is deterministic and idempotent.

 ``aggregated_output(outcomes) == aggregated_output(outcomes)``
 for every legal input. Re-running the aggregator over the same
 outcome list produces an equal aggregate; the helper is pure and
 the result depends only on its argument.

 Also exercises the ``Iterable`` overload by passing a generator
 on the second call - both shapes must produce the same aggregate.
 """

    agg_a = aggregated_output(outcomes)
    agg_b = aggregated_output(outcomes)
    assert agg_a == agg_b

    agg_gen = aggregated_output(o for o in outcomes)
    assert agg_a == agg_gen
