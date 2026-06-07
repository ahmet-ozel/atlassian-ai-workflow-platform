"""Multi-step graceful-skip dispatch - pure aggregator for ``multi_step``.

This module is the **single source of truth** for the multi-step
dispatch decision used for graceful skips in ``multi_step`` workflows.

The ``multi_step`` workflow type orchestrates *N* child workflows on
behalf of a single Jira issue.  The parent must
**not** fail-fast when any individual child lacks a capability - it
must mark that child ``out_of_scope`` and continue dispatching the
remaining children.  Splitting that decision out of the workflow body
into a pure function keeps the behaviour replay-safe (no I/O, no
``datetime.now()`` / ``random`` / ``uuid`` calls), trivially unit- and
property-testable, and reusable from the
:class:`AutomationWorkflow.multi_step` branch.

Public API
----------
* :class:`ChildProposal` - frozen dataclass describing one child the
  LLM has asked the platform to dispatch (``workflow_type`` plus the
  fully-formed :class:`ChildWorkflowSpec`).
* :class:`ChildPlan` - frozen dataclass with the discriminator
  ``action: Literal["start", "skip"]``, the same ``child_spec`` from
  the proposal, a snake_case ``reason`` token, and the
  ``missing_capabilities`` set (empty for ``"start"`` plans).
* :class:`ChildOutcome` - frozen dataclass capturing the runtime
  outcome of a single child after the parent has either started it or
  skipped it: discriminator ``action: Literal["started", "skipped"]``,
  the original ``child_spec``, the dispatch ``reason``, the
  ``missing_capabilities`` (empty for started children), and an
  optional ``status`` / ``failure_reason`` lifted from the child's
  output dataclass.
* :class:`AggregatedOutput` - frozen dataclass aggregating the
  ``ChildOutcome`` tuple by ``action``.  Carries ``total = started +
  skipped`` and the asserted invariant
  ``started + skipped == len(child_outcomes)``.
* :func:`multi_step_dispatch` - pure function that maps a sequence of
  :class:`ChildProposal` to a list of :class:`ChildPlan`.  No Temporal
  calls; the workflow body consumes the list and calls
  ``start_child_workflow`` for the ``"start"`` plans.
* :func:`aggregated_output` - pure aggregator over
  :class:`ChildOutcome` values; raises :class:`InvariantViolation`
  when the ``started + skipped == len(children)`` invariant breaks.

Skip semantics
--------------

A child is **skipped** with ``reason="out_of_scope"`` when *any* of its
required capabilities (per
:data:`temporal_shared.capabilities.WORKFLOW_TYPE_CAPABILITIES`,
collapsed to the simple vocabulary via
:func:`temporal_shared.capabilities.required_capabilities`) is absent
from ``dept_capabilities``.  The :class:`ChildPlan.missing_capabilities`
field records exactly which caps were missing so the audit log and the
final Jira comment can name them.

A child whose ``workflow_type`` is **not** a key of
:data:`WORKFLOW_TYPE_CAPABILITIES` (including the meta-type
``"multi_step"`` itself - nesting is forbidden by design) is skipped
with ``reason="unknown_workflow_type"`` and an empty
``missing_capabilities`` set.  Catching the ``KeyError`` here rather
than letting it propagate keeps the dispatcher *total* - the parent
workflow always returns a plan list of the same length as its input
(graceful skip; no child is silently dropped).

Total-length invariant
----------------------

For every input ``children`` sequence the function returns a list of
the **same length**.  No child is ever omitted from the plan; every
child either has an ``action="start"`` plan or an ``action="skip"``
plan.  The :func:`aggregated_output` function reasserts the same
invariant on the runtime outcomes via :class:`InvariantViolation`.

Replay determinism
------------------

Both :func:`multi_step_dispatch` and :func:`aggregated_output` are
pure: only set membership / set difference operations and tuple
construction.  No ``datetime`` / ``random`` / ``uuid`` calls, no
mutable global state, no I/O.  Safe to call directly from inside
Temporal workflow code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterable, Literal, Sequence

from .capabilities import (
    WORKFLOW_TYPE_CAPABILITIES,
    required_capabilities,
)
from .messages import ChildWorkflowSpec

__all__ = [
    "ChildProposal",
    "ChildPlan",
    "ChildOutcome",
    "AggregatedOutput",
    "InvariantViolation",
    "multi_step_dispatch",
    "aggregated_output",
    "REASON_OUT_OF_SCOPE",
    "REASON_UNKNOWN_WORKFLOW_TYPE",
    "REASON_DISPATCHED",
    "REASON_NESTED_MULTI_STEP",
]


# ---------------------------------------------------------------------------
# Audit reason tokens - single source of truth (snake_case)
# ---------------------------------------------------------------------------

#: Plan / outcome reason - child skipped because dept lacks one or more
#: required capabilities.
REASON_OUT_OF_SCOPE: Final[str] = "out_of_scope"

#: Plan / outcome reason - child skipped because its ``workflow_type``
#: is not a key of :data:`WORKFLOW_TYPE_CAPABILITIES`.
REASON_UNKNOWN_WORKFLOW_TYPE: Final[str] = "unknown_workflow_type"

#: Plan / outcome reason - child skipped because nesting ``multi_step``
#: inside a ``multi_step`` is forbidden by design (would produce a
#: dispatch tree that cannot be statically capability-gated).
REASON_NESTED_MULTI_STEP: Final[str] = "nested_multi_step_forbidden"

#: Plan / outcome reason - child dispatched (capability gate passed).
REASON_DISPATCHED: Final[str] = "dispatched"


# ---------------------------------------------------------------------------
# InvariantViolation - raised by :func:`aggregated_output`
# ---------------------------------------------------------------------------


class InvariantViolation(AssertionError):
    """Raised when ``started + skipped != total``.

    Subclassed from :class:`AssertionError` so callers that prefer the
    standard ``assert`` semantics can ``except AssertionError`` while
    callers that want the specific category can target this class
    directly.  The aggregator never produces a partial outcome - either
    every child outcome is accounted for, or this exception fires.
    """


# ---------------------------------------------------------------------------
# ChildProposal - input to :func:`multi_step_dispatch`
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChildProposal:
    """One child the parent is asked to dispatch.

    Built by the LLM-task-analysis parser from the ``children`` array
    of a ``workflow_type="multi_step"`` analysis result.  The
    :class:`AutomationWorkflow` (or the
    :class:`AgentRunnerWorkflow.multi_step` branch)
    constructs a tuple of these and passes them to
    :func:`multi_step_dispatch`.

    Attributes
    ----------
    workflow_type:
        Sub-workflow type - must be a key of
        :data:`temporal_shared.capabilities.WORKFLOW_TYPE_CAPABILITIES`
        **except** ``"multi_step"`` itself (nesting forbidden, see
        :data:`REASON_NESTED_MULTI_STEP`).  Unknown values do **not**
        raise here; instead they produce a ``"skip"``
        :class:`ChildPlan` with reason
        :data:`REASON_UNKNOWN_WORKFLOW_TYPE` so the dispatcher stays
        total.
    child_spec:
        Fully-formed :class:`ChildWorkflowSpec` ready for
        ``start_child_workflow``.  The spec carries the registered
        workflow name, idempotency key, task queue, and tuple-encoded
        input payload - the dispatcher does **not** validate or
        rewrite it.
    """

    workflow_type: str
    child_spec: ChildWorkflowSpec


# ---------------------------------------------------------------------------
# ChildPlan - output of :func:`multi_step_dispatch`
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChildPlan:
    """Decision for one child - either ``"start"`` or ``"skip"``.

    Attributes
    ----------
    action:
        Discriminator.  ``"start"`` → the parent workflow should call
        ``start_child_workflow`` with :attr:`child_spec`.  ``"skip"`` →
        the parent must mark the child ``out_of_scope`` (or the
        equivalent sub-status named by :attr:`reason`) and **not**
        invoke any Temporal API for it.
    child_spec:
        The original :class:`ChildWorkflowSpec` from the proposal.
        Carried unchanged so the parent has all the context it needs
        for both the dispatch path and the audit-trail emitted on
        skip.
    reason:
        Snake_case audit token.  One of
        :data:`REASON_DISPATCHED` (when ``action == "start"``),
        :data:`REASON_OUT_OF_SCOPE`,
        :data:`REASON_UNKNOWN_WORKFLOW_TYPE`, or
        :data:`REASON_NESTED_MULTI_STEP` (the latter three when
        ``action == "skip"``).  Callers compare by identity / equality
        against the module-level constants rather than by hard-coding
        the string value.
    missing_capabilities:
        The set of simple-vocabulary capability names the dept lacks
        for this child.  Non-empty only when ``reason ==
        REASON_OUT_OF_SCOPE``; empty in every other case.
    """

    action: Literal["start", "skip"]
    child_spec: ChildWorkflowSpec
    reason: str
    missing_capabilities: frozenset[str] = frozenset()


# ---------------------------------------------------------------------------
# ChildOutcome - runtime result of one child for :func:`aggregated_output`
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    """Runtime outcome of a single child after parent dispatch.

    Built by the parent workflow body once each :class:`ChildPlan` has
    been acted upon: ``"start"`` plans produce ``"started"`` outcomes
    (carrying the child's terminal status), and ``"skip"`` plans
    produce ``"skipped"`` outcomes (carrying the original skip reason
    and the missing capabilities).

    Attributes
    ----------
    action:
        Discriminator - ``"started"`` or ``"skipped"``.  Mirrors the
        plan's :attr:`ChildPlan.action`, but in past tense, because at
        this point the dispatch has already happened.
    child_spec:
        The original :class:`ChildWorkflowSpec` (unchanged from the
        plan).
    reason:
        Snake_case audit token - same vocabulary as
        :class:`ChildPlan.reason`.  For ``"started"`` outcomes this is
        :data:`REASON_DISPATCHED` regardless of whether the child
        eventually succeeded or failed; the per-child success/failure
        is carried in :attr:`status` / :attr:`failure_reason`.
    missing_capabilities:
        Same semantics as :attr:`ChildPlan.missing_capabilities`.
    status:
        Optional terminal status string lifted from the child's output
        (e.g. ``"completed"``, ``"failed"``, ``"out_of_scope"``).
        ``None`` for skipped children and for started children whose
        result has not yet been observed.
    failure_reason:
        Optional stable failure category from the child output;
        ``None`` for successful or skipped children.
    """

    action: Literal["started", "skipped"]
    child_spec: ChildWorkflowSpec
    reason: str
    missing_capabilities: frozenset[str] = frozenset()
    status: str | None = None
    failure_reason: str | None = None


# ---------------------------------------------------------------------------
# AggregatedOutput - return type of :func:`aggregated_output`
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AggregatedOutput:
    """Aggregate of every :class:`ChildOutcome` for one ``multi_step`` run.

    Attributes
    ----------
    started:
        Count of children with ``action == "started"``.
    skipped:
        Count of children with ``action == "skipped"``.
    total:
        ``started + skipped``; equals ``len(child_outcomes)``.  The
        constructor enforces the invariant
        ``started + skipped == total`` and raises
        :class:`InvariantViolation` on mismatch.
    child_outcomes:
        Tuple of all :class:`ChildOutcome` values in the original
        dispatch order.  Stored as a tuple (not a list) so the
        aggregate is hashable and immutable.
    """

    started: int
    skipped: int
    total: int
    child_outcomes: tuple[ChildOutcome, ...]

    def __post_init__(self) -> None:
        if self.started < 0 or self.skipped < 0 or self.total < 0:
            raise InvariantViolation(
                "AggregatedOutput counts must be non-negative; "
                f"started={self.started}, skipped={self.skipped}, "
                f"total={self.total}"
            )
        if self.started + self.skipped != self.total:
            raise InvariantViolation(
                "AggregatedOutput invariant violated: "
                f"started ({self.started}) + skipped ({self.skipped}) "
                f"!= total ({self.total})"
            )
        if len(self.child_outcomes) != self.total:
            raise InvariantViolation(
                "AggregatedOutput invariant violated: "
                f"len(child_outcomes) ({len(self.child_outcomes)}) "
                f"!= total ({self.total})"
            )


# ---------------------------------------------------------------------------
# multi_step_dispatch - pure decision function
# ---------------------------------------------------------------------------


def multi_step_dispatch(
    children: Iterable[ChildProposal] | Sequence[ChildProposal],
    dept_capabilities: frozenset[str] | set[str],
) -> list[ChildPlan]:
    """Decide ``"start"`` vs ``"skip"`` for each child.

    Pure function (no I/O, no Temporal calls, no ``datetime`` /
    ``random`` / ``uuid``).  The workflow body consumes the returned
    list and calls ``start_child_workflow`` for entries whose
    ``action == "start"``.

    For every child:

    * If :attr:`ChildProposal.workflow_type` is ``"multi_step"`` →
      ``"skip"`` with reason :data:`REASON_NESTED_MULTI_STEP`.
      Nesting is forbidden by design; catching it here rather than at
      the schema layer keeps the dispatcher robust to future LLM
      drift.
    * Else if :attr:`ChildProposal.workflow_type` is **not** a key of
      :data:`WORKFLOW_TYPE_CAPABILITIES` → ``"skip"`` with reason
      :data:`REASON_UNKNOWN_WORKFLOW_TYPE`.
    * Else compute the simple-vocabulary capability requirement via
      :func:`temporal_shared.capabilities.required_capabilities` and
      take the set difference against ``dept_capabilities``.  When the
      difference is non-empty → ``"skip"`` with reason
      :data:`REASON_OUT_OF_SCOPE` and the missing caps.  Otherwise →
      ``"start"`` with reason :data:`REASON_DISPATCHED`.

    Total-length invariant: the returned list always
    has the same length as ``children``.  No child is silently
    dropped - graceful skip is the only contract.

    Parameters
    ----------
    children:
        Sequence (or iterable) of :class:`ChildProposal`.  May be
        empty, in which case the function returns ``[]``.
    dept_capabilities:
        The department's available capabilities in the simple
        vocabulary (``"jira"``, ``"bitbucket"``, ``"confluence"``,
        ``"execution"``, ``"web_search"``).  Accepts either
        :class:`frozenset` or :class:`set` for ergonomics.

    Returns
    -------
    list[ChildPlan]
        One plan per input child, in the original order.  Each plan is
        either a ``"start"`` or a ``"skip"``; the list never drops or
        reorders children.

    Examples
    --------
    >>> from temporal_shared.messages import ChildWorkflowSpec
    >>> spec_pr = ChildWorkflowSpec(
    ...     workflow_name="AgentRunnerWorkflow",
    ...     workflow_id="agent-pr-1",
    ...     task_queue="agent-runner-tq",
    ... )
    >>> spec_code = ChildWorkflowSpec(
    ...     workflow_name="AgentRunnerWorkflow",
    ...     workflow_id="agent-code-1",
    ...     task_queue="agent-runner-tq",
    ... )
    >>> children = [
    ...     ChildProposal("pr_review", spec_pr),
    ...     ChildProposal("code_change_with_test", spec_code),
    ... ]
    >>> plans = multi_step_dispatch(children, frozenset({"jira", "bitbucket"}))
    >>> [(p.action, p.reason) for p in plans]
    [('start', 'dispatched'), ('skip', 'out_of_scope')]
    >>> sorted(plans[1].missing_capabilities)
    ['execution']
    >>> len(plans) == len(children)
    True
    """

    # Normalise once so we don't keep re-converting inside the loop.
    have: frozenset[str] = (
        dept_capabilities
        if isinstance(dept_capabilities, frozenset)
        else frozenset(dept_capabilities)
    )

    plans: list[ChildPlan] = []
    for child in children:
        wf_type = child.workflow_type

        # Nested multi_step is forbidden by design - skip with a
        # dedicated reason so the audit log can distinguish it from
        # other skip categories.
        if wf_type == "multi_step":
            plans.append(
                ChildPlan(
                    action="skip",
                    child_spec=child.child_spec,
                    reason=REASON_NESTED_MULTI_STEP,
                    missing_capabilities=frozenset(),
                )
            )
            continue

        # Unknown workflow type - guard the KeyError so the dispatcher
        # remains total.  This matches graceful skip behavior:
        # the LLM may occasionally produce an unknown workflow type;
        # we record it and move on rather than aborting the whole run.
        if wf_type not in WORKFLOW_TYPE_CAPABILITIES:
            plans.append(
                ChildPlan(
                    action="skip",
                    child_spec=child.child_spec,
                    reason=REASON_UNKNOWN_WORKFLOW_TYPE,
                    missing_capabilities=frozenset(),
                )
            )
            continue

        # Compute the simple-vocabulary requirement and take the set
        # difference.  ``required_capabilities`` raises ``KeyError``
        # for unknown keys; the membership check above means we never
        # reach this branch with one.
        required = required_capabilities(wf_type)
        missing = required - have

        if missing:
            plans.append(
                ChildPlan(
                    action="skip",
                    child_spec=child.child_spec,
                    reason=REASON_OUT_OF_SCOPE,
                    missing_capabilities=frozenset(missing),
                )
            )
        else:
            plans.append(
                ChildPlan(
                    action="start",
                    child_spec=child.child_spec,
                    reason=REASON_DISPATCHED,
                    missing_capabilities=frozenset(),
                )
            )

    return plans


# ---------------------------------------------------------------------------
# aggregated_output - pure aggregator with invariant enforcement
# ---------------------------------------------------------------------------


def aggregated_output(
    child_outcomes: Iterable[ChildOutcome] | Sequence[ChildOutcome],
) -> AggregatedOutput:
    """Aggregate per-child outcomes into a single :class:`AggregatedOutput`.

    Pure function.  Counts ``started`` vs ``skipped`` outcomes and
    asserts the invariant ``started + skipped == total``.  Raises
    :class:`InvariantViolation` when an outcome
    carries an unrecognised :attr:`ChildOutcome.action` value, since
    the discriminator is the only signal the aggregator has and a
    violation would silently lose a child from the summary.

    Parameters
    ----------
    child_outcomes:
        Sequence (or iterable) of :class:`ChildOutcome` produced by
        the parent workflow body after each child has been dispatched
        or skipped.  Order is preserved in the returned aggregate's
        :attr:`AggregatedOutput.child_outcomes` field.

    Returns
    -------
    AggregatedOutput
        Aggregate carrying ``started``, ``skipped``, ``total``, and
        the original outcome tuple.

    Raises
    ------
    InvariantViolation
        When any outcome carries an action outside
        ``{"started", "skipped"}``, or when the
        :class:`AggregatedOutput` constructor's own invariant fires
        (which should be impossible given correct input - see the
        constructor for the full check list).

    Examples
    --------
    >>> from temporal_shared.messages import ChildWorkflowSpec
    >>> spec = ChildWorkflowSpec(
    ...     workflow_name="AgentRunnerWorkflow",
    ...     workflow_id="x",
    ...     task_queue="agent-runner-tq",
    ... )
    >>> outcomes = [
    ...     ChildOutcome("started", spec, REASON_DISPATCHED, status="completed"),
    ...     ChildOutcome("skipped", spec, REASON_OUT_OF_SCOPE,
    ...                  missing_capabilities=frozenset({"execution"})),
    ... ]
    >>> agg = aggregated_output(outcomes)
    >>> (agg.started, agg.skipped, agg.total)
    (1, 1, 2)
    >>> agg.started + agg.skipped == len(outcomes)
    True
    """

    outcomes_tuple: tuple[ChildOutcome, ...] = tuple(child_outcomes)

    started = 0
    skipped = 0
    for outcome in outcomes_tuple:
        if outcome.action == "started":
            started += 1
        elif outcome.action == "skipped":
            skipped += 1
        else:
            raise InvariantViolation(
                "ChildOutcome.action must be 'started' or 'skipped'; "
                f"got {outcome.action!r} for child_spec="
                f"{outcome.child_spec.workflow_id!r}"
            )

    return AggregatedOutput(
        started=started,
        skipped=skipped,
        total=len(outcomes_tuple),
        child_outcomes=outcomes_tuple,
    )
