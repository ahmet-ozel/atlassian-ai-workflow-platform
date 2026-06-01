"""Property 5: Iteration cap & needs_info loop cap (state machine).

**Validates: Requirements 5.1, 5.6, 5.7, 5.8**

This module exercises :class:`AgentRunnerWorkflow`'s signal handlers
under a Hypothesis :class:`~hypothesis.stateful.RuleBasedStateMachine`
that drives a random sequence of comment / fix / explain / needs_info
/ cancel signals. After each step the state machine asserts the three
loop-cap invariants the spec demands:

1. ``iter_count <= MAX_ITER`` always — the workflow can never advance
   past the design ceiling regardless of which signals were received
   in which order (R5.1).
2. When ``out_of_scope`` is True it is only because either
   ``iter_count >= MAX_ITER`` was reached (R5.1) or
   ``needs_info_streak >= NEEDS_INFO_MAX_STREAK`` was reached (R5.6).
3. ``iter_warning_at_three`` is True iff the workflow ever reached
   ``iter_count >= ITER_WARNING_THRESHOLD`` during the run (R5.7) —
   once latched it stays latched, but it never flips on without
   the threshold actually being crossed.

The state machine drives the workflow by calling the signal handler
methods directly. Signals never ``await`` activities in the
implementation — they only mutate workflow state and flip edge flags
— so we can exercise them outside a Temporal worker once
``temporalio.workflow.now`` is stubbed with a deterministic clock.

We also drain the ``_iter_warning_pending`` edge (the body normally
does this after posting the Jira banner) on each step so the
``iter_warning_at_three`` latch's invariant matches what an external
observer would see in production.

Run target (from ``platform/``):

    python -m pytest tests/property/test_temporal_loop_cap.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
)
from temporalio import workflow as _temporal_workflow

# ---------------------------------------------------------------------------
# ``sys.path`` bootstrapping — the canonical workflow ships under
# ``platform/workers/agent-runner-worker/src/agent_runner/...``. ``pytest.ini``
# already injects every ``platform/libs/<name>/src`` onto ``sys.path``,
# but the worker's own source tree is not pre-installed, so we add it
# here mirroring ``tests/property/test_workflow_determinism_replay.py``.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]
_AGENT_RUNNER_SRC: Path = (
    _PLATFORM_ROOT / "workers" / "agent-runner-worker" / "src"
)
_TEMPORAL_SHARED_SRC: Path = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)

for _candidate in (_AGENT_RUNNER_SRC, _TEMPORAL_SHARED_SRC):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)


# noqa: E402 below — imports must follow the ``sys.path`` bootstrap.

from agent_runner.workflows.agent_runner_workflow import (  # noqa: E402
    ITER_WARNING_THRESHOLD,
    MAX_ITER,
    NEEDS_INFO_MAX_STREAK,
    AgentRunnerWorkflow,
    CancelRequestedSignal,
    CommentAddedSignal,
    ExplainTriggeredSignal,
    FixTriggeredSignal,
)


# ---------------------------------------------------------------------------
# Deterministic ``workflow.now`` clock
# ---------------------------------------------------------------------------
#
# The signal handlers consult ``workflow.now()`` to enforce the
# ``[fix]`` 60-second debounce window and the ``[explain]`` 5-minute
# cache TTL. In a unit test there is no Temporal runtime, so we stub
# the function with a mutable clock that the state machine advances
# between rules. The clock is monkey-patched onto the
# :mod:`temporalio.workflow` module module-globally; because each
# state machine instance creates a fresh
# :class:`AgentRunnerWorkflow` and resets the clock in ``__init__``,
# tests stay independent.
# ---------------------------------------------------------------------------


_FIXED_EPOCH: datetime = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


class _StubClock:
    """Mutable wrapper around :data:`_FIXED_EPOCH` plus an offset.

    ``advance`` mutates the offset; ``value`` returns the current time.
    The state machine installs one of these onto
    ``temporalio.workflow.now`` per :class:`AgentRunnerWorkflow`
    instance so every replay reaches the same series of timestamps.
    """

    __slots__ = ("_now",)

    def __init__(self) -> None:
        self._now: datetime = _FIXED_EPOCH

    @property
    def value(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class IterationCapStateMachine(RuleBasedStateMachine):
    """Random signal sequences must respect every iter-cap invariant.

    Rules
    -----
    * :py:meth:`comment_plain` — plain comment, advances iter and
      resets ``needs_info_streak``.
    * :py:meth:`comment_needs_info` — ``[needs_info]`` keyword
      comment; increments the streak but does NOT advance iter
      (R5.6).
    * :py:meth:`fix_signal` — ``[fix]`` keyword; advances iter unless
      the 60s debounce window or the diff-hash dedup blocks it.
    * :py:meth:`explain_signal` — ``[explain]`` keyword; advances iter
      unless the cache hit short-circuits it.
    * :py:meth:`cancel_signal` — cancel signal; locks out further
      mutation (other handlers no-op once cancelled).

    Each rule advances the deterministic clock by a small random
    delta so the debounce + TTL paths are explored.

    Invariants (asserted via :func:`invariant`):

    * ``iter_count`` never exceeds :data:`MAX_ITER`.
    * ``out_of_scope`` ⇒ either ``iter_count >= MAX_ITER`` or
      ``needs_info_streak >= NEEDS_INFO_MAX_STREAK``.
    * ``iter_warning_at_three`` latch is True iff the workflow
      reached ``iter_count >= ITER_WARNING_THRESHOLD`` at some point
      during the run (we drain the pending edge each step to mirror
      what the body does in production).

    Validates Requirements: 5.1, 5.6, 5.7, 5.8.
    """

    def __init__(self) -> None:
        super().__init__()

        # Fresh clock + workflow per state machine instance. We
        # monkey-patch ``temporalio.workflow.now`` with a closure over
        # this clock so signal handlers consult the stub instead of
        # the real Temporal SDK call. Each state machine instance
        # gets its own clock; because state machines run sequentially
        # within a single process, the last patch wins per instance —
        # which is what we want.
        self._clock: _StubClock = _StubClock()
        _temporal_workflow.now = lambda: self._clock.value  # type: ignore[assignment]

        self._wf: AgentRunnerWorkflow = AgentRunnerWorkflow()
        # Mirror the ``run`` body's first turn: it advances iter once
        # to account for the initial work unit. Starting at 1 means
        # the cap (5) is reached after 4 additional advances, which
        # is what the production workflow observes too.
        self._wf._advance_iter_with_banner_check()

        # Ghost variables — what *we* believe the workflow's history
        # has been. We compute these alongside the workflow's own
        # state so the invariants can compare the latch against an
        # independently-derived ground truth.
        self._max_iter_seen: int = self._wf._iteration_state.iter_count

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _drain_iter_warning_edge(self) -> None:
        """Mirror the body's banner-post turn.

        In production :meth:`AgentRunnerWorkflow._maybe_post_iter_warning_banner`
        consumes ``_iter_warning_pending`` and flips
        ``_iter_warning_at_three`` to True after posting the Jira
        comment. We perform that bookkeeping inline here so the
        latch invariant matches what an external observer would see
        once the run completes.
        """

        if self._wf._iter_warning_pending:
            self._wf._iter_warning_pending = False
            self._wf._iter_warning_at_three = True

    def _record_history(self) -> None:
        """Update ghost variables tracking the maximum iter seen."""

        self._max_iter_seen = max(
            self._max_iter_seen, self._wf._iteration_state.iter_count
        )

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------

    @rule(advance_seconds=st.integers(min_value=0, max_value=600))
    def comment_plain(self, advance_seconds: int) -> None:
        """A plain comment advances iter and resets the needs_info streak."""

        self._clock.advance(timedelta(seconds=advance_seconds))
        self._wf.comment_added(
            CommentAddedSignal(
                comment_text="lorem ipsum dolor",
                actor_account_id="user-1",
            )
        )
        self._drain_iter_warning_edge()
        self._record_history()

    @rule(advance_seconds=st.integers(min_value=0, max_value=600))
    def comment_needs_info(self, advance_seconds: int) -> None:
        """A ``[needs_info]`` keyword bumps the streak but not iter."""

        self._clock.advance(timedelta(seconds=advance_seconds))
        self._wf.comment_added(
            CommentAddedSignal(
                comment_text="[needs_info] please clarify the scope",
                actor_account_id="reporter-1",
            )
        )
        self._drain_iter_warning_edge()
        self._record_history()

    @rule(
        advance_seconds=st.integers(min_value=0, max_value=600),
        diff_hash=st.sampled_from(
            ("hash-A", "hash-B", "hash-C", "hash-D", "hash-E")
        ),
    )
    def fix_signal(self, advance_seconds: int, diff_hash: str) -> None:
        """A ``[fix]`` keyword: maybe advances iter (debounce + dedup)."""

        self._clock.advance(timedelta(seconds=advance_seconds))
        self._wf.fix_triggered(
            FixTriggeredSignal(
                comment_text="[fix] please rerun the failing test",
                actor_account_id="reviewer-1",
                diff_hash=diff_hash,
            )
        )
        self._drain_iter_warning_edge()
        self._record_history()

    @rule(
        advance_seconds=st.integers(min_value=0, max_value=600),
        pr_diff_hash=st.sampled_from(
            ("pr-A", "pr-B", "pr-C", "pr-D", "pr-E")
        ),
    )
    def explain_signal(self, advance_seconds: int, pr_diff_hash: str) -> None:
        """An ``[explain]`` keyword: maybe advances iter (cache hit)."""

        self._clock.advance(timedelta(seconds=advance_seconds))
        self._wf.explain_triggered(
            ExplainTriggeredSignal(
                comment_text="[explain] what changed in this PR?",
                actor_account_id="reviewer-1",
                pr_diff_hash=pr_diff_hash,
            )
        )
        self._drain_iter_warning_edge()
        self._record_history()

    @rule(advance_seconds=st.integers(min_value=0, max_value=600))
    def cancel_signal(self, advance_seconds: int) -> None:
        """A cancel signal latches the workflow into the cancel path.

        Cancel never violates the iter cap on its own — subsequent
        handlers observe ``_cancel_requested`` and short-circuit, so
        ``iter_count`` is frozen at the time of cancel.
        """

        self._clock.advance(timedelta(seconds=advance_seconds))
        self._wf.cancel_requested(
            CancelRequestedSignal(
                actor_id="user-1",
                actor_role="end_user",
                reason="user_cancel",
            )
        )
        # Cancel does not arm the iter==3 banner, but still drain in
        # case a previous rule left the edge un-drained (defensive).
        self._drain_iter_warning_edge()
        self._record_history()

    # ------------------------------------------------------------------
    # Invariants — checked after every rule
    # ------------------------------------------------------------------

    @invariant()
    def iter_count_never_exceeds_max(self) -> None:
        """R5.1 — the iteration cap holds across every signal sequence."""

        assert self._wf._iteration_state.iter_count <= MAX_ITER, (
            f"iter_count={self._wf._iteration_state.iter_count} "
            f"exceeds MAX_ITER={MAX_ITER}"
        )

    @invariant()
    def out_of_scope_implies_a_cap_reached(self) -> None:
        """R5.1 + R5.6 — out_of_scope only fires from a real cap.

        ``out_of_scope`` is the workflow's terminal "give up" state;
        it must only be reachable via one of two pre-conditions:
        the iteration cap (R5.1) or the consecutive ``needs_info``
        streak (R5.6). Any third path would mean the workflow is
        terminating without an authorised cause.
        """

        if self._wf._out_of_scope:
            iter_cap_reached = (
                self._wf._iteration_state.iter_count >= MAX_ITER
            )
            needs_info_cap_reached = (
                self._wf._iteration_state.needs_info_streak
                >= NEEDS_INFO_MAX_STREAK
            )
            assert iter_cap_reached or needs_info_cap_reached, (
                "out_of_scope is True but neither cap is met: "
                f"iter_count={self._wf._iteration_state.iter_count}, "
                f"needs_info_streak={self._wf._iteration_state.needs_info_streak}"
            )

    @invariant()
    def iter_warning_latch_matches_history(self) -> None:
        """R5.7 — the iter==3 banner latches iff iter ever reached 3.

        We drain ``_iter_warning_pending`` into ``_iter_warning_at_three``
        on every rule (mirroring what the body does in production), so
        the latch's truthiness should match the ghost variable
        ``_max_iter_seen``: latch == (iter ever reached the threshold).
        """

        ever_crossed = self._max_iter_seen >= ITER_WARNING_THRESHOLD
        assert self._wf._iter_warning_at_three == ever_crossed, (
            f"iter_warning_at_three={self._wf._iter_warning_at_three} "
            f"but max iter seen={self._max_iter_seen}, "
            f"threshold={ITER_WARNING_THRESHOLD}"
        )

    @invariant()
    def needs_info_streak_never_exceeds_max(self) -> None:
        """R5.6 — the needs_info streak never advances past its cap.

        Signal handlers refuse to apply a ``[needs_info]`` once the
        streak has already triggered ``out_of_scope``, so the field
        is bounded by :data:`NEEDS_INFO_MAX_STREAK` for the lifetime
        of the workflow.
        """

        assert (
            self._wf._iteration_state.needs_info_streak
            <= NEEDS_INFO_MAX_STREAK
        ), (
            "needs_info_streak="
            f"{self._wf._iteration_state.needs_info_streak} "
            f"exceeds NEEDS_INFO_MAX_STREAK={NEEDS_INFO_MAX_STREAK}"
        )


# ---------------------------------------------------------------------------
# Settings — explore at least 50 examples × 30 stateful steps so the
# random signal sequences cover the cap edge densely.
# ---------------------------------------------------------------------------


IterationCapStateMachine.TestCase.settings = settings(
    max_examples=50,
    stateful_step_count=30,
    deadline=None,
    suppress_health_check=[
        # Each ``__init__`` builds a fresh workflow + monkey-patches
        # ``temporalio.workflow.now``; that's intentional and not the
        # data-generation cost Hypothesis warns about.
        HealthCheck.too_slow,
    ],
)


# Pytest auto-discovers the generated TestCase class.
TestIterationCap = IterationCapStateMachine.TestCase
