"""Property test 6 — ``[fix]`` keyword family: debounce + diff-hash dedup.

**Validates: Requirements 5.2, 5.3, 5.4**

Property statement (design.md §"Property 6", tasks.md §6.5)
-----------------------------------------------------------

For any hypothesis-generated tuple
``(state, current_diff_hash, now, last_fix_trigger_at)`` the
``AgentRunnerWorkflow`` ``[fix]`` family — composed of the pure
helpers :func:`agent_runner_workflow._is_fix_debounced` and
:func:`agent_runner_workflow._fix_should_skip_retest` plus the signal
handler :meth:`AgentRunnerWorkflow._apply_fix_signal` — SHALL satisfy:

(P1) **Debounce** — ``_is_fix_debounced(state, now)`` returns ``True``
     iff ``state.last_fix_trigger_at`` is not ``None`` AND
     ``now - last_fix_trigger_at < FIX_DEBOUNCE_WINDOW`` (60 seconds).
     R5.4 (T6).

(P2) **Diff-hash dedup** — ``_fix_should_skip_retest(state,
     diff_hash)`` returns ``True`` iff ``diff_hash`` is non-empty AND
     ``diff_hash in state.test_results_by_diff_hash``. R5.3 (T1).

(P3) **Signal handler — debounce path** — When the debounce window is
     active, the handler:

     - leaves the workflow ``_iteration_state`` unchanged (no
       ``iter_count`` advance, no ``last_fix_trigger_at`` mutation),
     - queues exactly one ``FIX_DEBOUNCE_AUDIT_ACTION`` audit row,
     - leaves ``_pending_fix_diff_hash`` unchanged,
     - returns without raising. R5.4.

(P4) **Signal handler — re-test protection path** — When the debounce
     window is **not** active AND the current ``diff_hash`` is in the
     test-results cache, the handler:

     - does NOT advance ``iter_count`` (re-test is skipped),
     - records the trigger time at ``now`` (so the next ``[fix]`` is
       subject to the debounce window),
     - queues exactly one ``FIX_RETEST_PROTECTED_AUDIT_ACTION`` audit
       row,
     - clears ``_pending_fix_diff_hash`` (no child re-test is
       requested). R5.3.

(P5) **Signal handler — fresh-diff path** — When the debounce window
     is **not** active AND the current ``diff_hash`` is **not** in the
     test-results cache, the handler:

     - advances ``iter_count`` by exactly one (subject to MAX_ITER),
     - records the trigger time at ``now``,
     - sets ``_pending_fix_diff_hash`` to the supplied diff hash,
     - queues NO ``[fix]``-family audit row. R5.4 transition path.

(P6) **Sequential ``[fix]`` semantics** — A three-step sequence
     ``debounced → re_test_protected → fresh_diff`` produces a single
     terminal state where:

     - ``iter_count`` advanced by exactly one (only the third call
       took an iteration),
     - ``last_fix_trigger_at`` equals the wall-clock time of the
       *last* accepted ``[fix]`` (the third one — the second call's
       re-test path also writes the trigger time, but the third
       overwrites it),
     - the audit queue contains, in order, exactly
       ``[FIX_DEBOUNCE_AUDIT_ACTION, FIX_RETEST_PROTECTED_AUDIT_ACTION]``
       (the third call queues no ``[fix]``-family audit row). R5.3, R5.4.

Not in scope
------------

* The webhook-side ``[fix]`` regex match — owned by
  ``test_webhook_predicates.py`` (Property 3).
* The ``ExecutionRunWorkflow`` child dispatch and its
  ``test_results_by_diff_hash`` cache write — owned by the
  ``code_change_with_test`` body (task 7.5) and exercised by the
  unit tests under
  ``platform/workers/agent-runner-worker/tests/unit/``.
* Replay determinism of the workflow body — owned by Property 2
  (``test_workflow_determinism_static.py`` /
  ``test_workflow_determinism_replay.py``).

Implementation notes
--------------------

The signal handler tests instantiate ``AgentRunnerWorkflow`` as a
plain Python object. The handler's only Temporal dependency is
``workflow.now()`` which we monkey-patch onto a deterministic clock
via :func:`monkeypatch.setattr` on the ``temporalio.workflow`` module
attribute — the same pattern used by the worker's own unit-test
suite at
``platform/workers/agent-runner-worker/tests/unit/test_agent_runner_signal_handlers.py``.

Hypothesis runs each property at ``max_examples=100`` with
``deadline=None`` (the handler is fast but pytest's default
deadline trips on debug builds and CI cold-starts). Generators
constrain the input space intelligently — e.g. ``test_results_by_diff_hash``
is drawn from a small alphabet so the dedup hit / miss branches are
both well-covered without astronomically improbable collisions.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from contextlib import contextmanager
from typing import Iterator

from hypothesis import given, settings
from hypothesis import strategies as st
from temporalio import workflow as _temporal_workflow

# ---------------------------------------------------------------------------
# sys.path bootstrap — workers and shared libs are not pip-installed
# inside the test environment, so we expose their source trees the
# same way the existing property tests under ``tests/property/`` do
# (mirrors ``test_workflow_determinism_replay.py``,
# ``test_token_cap_fail_fast.py``, ``test_task_analysis_parser.py``).
# ---------------------------------------------------------------------------

# tests/property/test_fix_keyword.py → platform/
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


# noqa: E402 below — imports must follow the sys.path bootstrap.

from agent_runner.workflows.agent_runner_workflow import (  # noqa: E402
    FIX_DEBOUNCE_AUDIT_ACTION,
    FIX_DEBOUNCE_WINDOW,
    FIX_RETEST_PROTECTED_AUDIT_ACTION,
    MAX_ITER,
    AgentRunnerWorkflow,
    FixTriggeredSignal,
    _fix_should_skip_retest,
    _is_fix_debounced,
)
from temporal_shared.messages import IterationState  # noqa: E402


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Anchor wall-clock value used by every test. The hypothesis-drawn
#: ``last_fix_trigger_at`` and ``now`` are constructed as offsets from
#: this anchor so we keep the strategy space bounded and the search
#: focused on the debounce-window boundary (60s).
_ANCHOR_NOW: datetime = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


# Diff hash alphabet — kept small so the dedup hit / miss branches
# are both well-covered. Hypothesis would otherwise spend most of its
# budget on hashes that never collide with any pre-seeded entry.
_DIFF_HASH_ALPHABET: tuple[str, ...] = (
    "H1", "H2", "H3", "H4", "H5", "H6", "H7", "H8",
)


def _diff_hash_strategy() -> st.SearchStrategy[str]:
    """Strategy emitting one of the small alphabet diff hashes."""

    return st.sampled_from(_DIFF_HASH_ALPHABET)


def _optional_diff_hash_strategy() -> st.SearchStrategy[str | None]:
    """Diff hash strategy that occasionally yields ``None`` / empty.

    Both shapes exercise different branches of
    :func:`_fix_should_skip_retest` (``None`` / empty short-circuits
    to ``False``, regardless of cache state).
    """

    return st.one_of(
        st.none(),
        st.just(""),
        _diff_hash_strategy(),
    )


def _test_results_cache_strategy() -> st.SearchStrategy[dict[str, str]]:
    """Strategy emitting a ``test_results_by_diff_hash`` mapping.

    Keys are sampled from :data:`_DIFF_HASH_ALPHABET`; values are the
    valid :data:`temporal_shared.messages.ExecutionRunStatus` literals.
    The mapping size is capped at the alphabet length so hypothesis
    cannot generate impossibly large states.
    """

    return st.dictionaries(
        keys=_diff_hash_strategy(),
        values=st.sampled_from(("passed", "failed", "timeout")),
        max_size=len(_DIFF_HASH_ALPHABET),
    )


def _last_fix_at_strategy() -> st.SearchStrategy[datetime | None]:
    """Strategy emitting an optional ``last_fix_trigger_at`` timestamp.

    The non-None branch draws from a window centred on
    :data:`_ANCHOR_NOW`, ranging from 5 minutes before to 5 minutes
    after the anchor in 1-second granularity. The 60s debounce
    boundary therefore lies well inside the strategy space and both
    "inside the window" and "outside the window" outcomes are
    reachable.
    """

    delta = st.integers(min_value=-300, max_value=300)
    return st.one_of(
        st.none(),
        delta.map(lambda secs: _ANCHOR_NOW + timedelta(seconds=secs)),
    )


def _now_strategy() -> st.SearchStrategy[datetime]:
    """Strategy emitting a wall-clock value near :data:`_ANCHOR_NOW`.

    Constrained to ``[anchor, anchor + 10 minutes]`` so the
    pre-condition ``now >= last_fix_trigger_at`` is *not* always true
    — the debounce predicate must still produce the correct result
    when ``now < last_fix_trigger_at`` (the workflow guards against
    this by treating ``last_fix_trigger_at`` as an opaque marker, not
    a clock anchor).
    """

    return st.integers(min_value=0, max_value=600).map(
        lambda secs: _ANCHOR_NOW + timedelta(seconds=secs)
    )


def _iteration_state_strategy(
    *, allow_full_iter: bool = False
) -> st.SearchStrategy[IterationState]:
    """Strategy emitting an :class:`IterationState` for the handler tests.

    Parameters
    ----------
    allow_full_iter:
        When ``True`` ``iter_count`` is allowed to reach
        :data:`MAX_ITER` so the cap-reached branch can be exercised.
        When ``False`` (default) ``iter_count`` stays strictly below
        the cap so ``_should_advance_iter`` always returns
        ``advance=True`` — keeping the focus on the ``[fix]``-family
        invariants without the iter-cap branch interfering.
    """

    iter_max = MAX_ITER if allow_full_iter else MAX_ITER - 1
    return st.builds(
        IterationState,
        iter_count=st.integers(min_value=0, max_value=iter_max),
        last_fix_trigger_at=_last_fix_at_strategy(),
        test_results_by_diff_hash=_test_results_cache_strategy(),
        # We only exercise the ``[fix]`` family here, so leave the
        # explain cache empty and the needs_info streak at zero.
        explain_cache=st.just({}),
        needs_info_streak=st.integers(min_value=0, max_value=2),
    )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


@contextmanager
def _patched_workflow_now(fixed_now: datetime) -> Iterator[None]:
    """Context manager that swaps ``workflow.now`` for the duration of
    a hypothesis example.

    Hypothesis explicitly forbids function-scoped pytest fixtures
    inside ``@given``-decorated tests because the fixture state is not
    reset between generated inputs. We therefore restore the original
    ``workflow.now`` attribute manually around every example so each
    iteration starts from a clean slate. The save / restore dance
    keeps the patch idempotent and the tests safe to re-run.
    """

    sentinel = object()
    original = getattr(_temporal_workflow, "now", sentinel)
    _temporal_workflow.now = lambda: fixed_now  # type: ignore[assignment]
    try:
        yield
    finally:
        if original is sentinel:
            # ``workflow.now`` was not previously defined — remove the
            # attribute we added so the module returns to its initial
            # shape.
            try:
                delattr(_temporal_workflow, "now")
            except AttributeError:  # pragma: no cover - defensive only
                pass
        else:
            _temporal_workflow.now = original  # type: ignore[assignment]


def _build_workflow(initial_state: IterationState) -> AgentRunnerWorkflow:
    """Construct an :class:`AgentRunnerWorkflow` seeded with *initial_state*.

    Mirrors the ``make_wf`` fixture from the worker's unit-test
    suite. The workflow is created with default scaffolding and its
    iteration state is replaced with the hypothesis-drawn value
    *before* any handler call.
    """

    wf = AgentRunnerWorkflow()
    wf._iteration_state = initial_state
    return wf


# ---------------------------------------------------------------------------
# Property 6.1 — debounce predicate matches the 60-second window
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    last_fix_at=_last_fix_at_strategy(),
    now=_now_strategy(),
)
def test_is_fix_debounced_matches_60s_window(
    last_fix_at: datetime | None, now: datetime
) -> None:
    """``_is_fix_debounced`` returns ``True`` iff
    ``now - last_fix_trigger_at < FIX_DEBOUNCE_WINDOW``.

    Validates Requirements: 5.4 (T6).
    """

    state = IterationState(last_fix_trigger_at=last_fix_at)

    actual = _is_fix_debounced(state, now)

    if last_fix_at is None:
        # No prior ``[fix]`` recorded — debounce never fires.
        expected = False
    else:
        expected = (now - last_fix_at) < FIX_DEBOUNCE_WINDOW

    assert actual is expected, (
        f"_is_fix_debounced(last={last_fix_at!r}, now={now!r}) "
        f"returned {actual!r}; expected {expected!r}"
    )


# ---------------------------------------------------------------------------
# Property 6.2 — re-test guard matches diff-hash cache membership
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    cache=_test_results_cache_strategy(),
    diff_hash=_optional_diff_hash_strategy(),
)
def test_fix_should_skip_retest_matches_cache_membership(
    cache: dict[str, str], diff_hash: str | None
) -> None:
    """``_fix_should_skip_retest`` returns ``True`` iff the diff hash
    is non-empty AND present in ``state.test_results_by_diff_hash``.

    Validates Requirements: 5.3 (T1).
    """

    state = IterationState(test_results_by_diff_hash=cache)

    actual = _fix_should_skip_retest(state, diff_hash or "")

    if not diff_hash:
        # Empty / None hash short-circuits the predicate to False —
        # the workflow still treats it as a fresh test request.
        expected = False
    else:
        expected = diff_hash in cache

    assert actual is expected, (
        f"_fix_should_skip_retest(cache={cache!r}, diff_hash={diff_hash!r}) "
        f"returned {actual!r}; expected {expected!r}"
    )


# ---------------------------------------------------------------------------
# Property 6.3 — signal handler debounce path leaves state untouched
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    state=_iteration_state_strategy(),
    diff_hash=_diff_hash_strategy(),
    debounce_offset=st.integers(min_value=0, max_value=59),
)
def test_apply_fix_signal_debounced_keeps_state_unchanged(
    state: IterationState,
    diff_hash: str,
    debounce_offset: int,
) -> None:
    """When ``now - last_fix_trigger_at < 60s`` the handler drops the
    signal silently: no iter advance, no trigger time mutation, one
    ``fix_debounce_dropped`` audit queued.

    Validates Requirements: 5.4 (T6).
    """

    # Force the debounce window active by anchoring last_fix_trigger_at
    # to ``_ANCHOR_NOW`` and stepping ``now`` forward by less than 60s.
    state = replace(state, last_fix_trigger_at=_ANCHOR_NOW)
    fixed_now = _ANCHOR_NOW + timedelta(seconds=debounce_offset)

    with _patched_workflow_now(fixed_now):
        wf = _build_workflow(state)
        iter_before = wf._iteration_state.iter_count
        last_fix_before = wf._iteration_state.last_fix_trigger_at
        pending_fix_before = wf._pending_fix_diff_hash

        wf._apply_fix_signal(text="[fix]", diff_hash=diff_hash)

    # State invariants — handler must not mutate iteration state.
    assert wf._iteration_state.iter_count == iter_before
    assert wf._iteration_state.last_fix_trigger_at == last_fix_before
    assert wf._pending_fix_diff_hash == pending_fix_before

    # Audit invariants — exactly one debounce audit row queued, and
    # the re-test-protected audit is *not* present.
    assert (
        wf._pending_audit_actions.count(FIX_DEBOUNCE_AUDIT_ACTION) == 1
    )
    assert (
        FIX_RETEST_PROTECTED_AUDIT_ACTION not in wf._pending_audit_actions
    )


# ---------------------------------------------------------------------------
# Property 6.4 — re-test guard path skips iter and queues the audit
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    state=_iteration_state_strategy(),
    diff_hash=_diff_hash_strategy(),
    test_status=st.sampled_from(("passed", "failed", "timeout")),
)
def test_apply_fix_signal_retest_protected_skips_iter(
    state: IterationState,
    diff_hash: str,
    test_status: str,
) -> None:
    """When the diff hash is already in the test cache (and we are
    *not* inside the debounce window), the handler:

    - leaves ``iter_count`` unchanged,
    - records ``last_fix_trigger_at`` at ``now``,
    - queues exactly one ``fix_re_test_protected`` audit row,
    - clears ``_pending_fix_diff_hash``.

    Validates Requirements: 5.3 (T1).
    """

    # Force the debounce window inactive — anchor last_fix_trigger_at
    # well outside the 60s window. ``None`` would also work but we
    # prefer an explicit far-past anchor so the re-test path is the
    # *only* reason the handler short-circuits.
    state = replace(
        state,
        last_fix_trigger_at=_ANCHOR_NOW - timedelta(hours=1),
        # Ensure the diff hash is in the cache so the re-test guard
        # fires deterministically, regardless of the hypothesis-drawn
        # cache contents.
        test_results_by_diff_hash={
            **dict(state.test_results_by_diff_hash),
            diff_hash: test_status,
        },
    )

    with _patched_workflow_now(_ANCHOR_NOW):
        wf = _build_workflow(state)
        iter_before = wf._iteration_state.iter_count

        wf._apply_fix_signal(text="[fix] please rerun", diff_hash=diff_hash)

    # Iter unchanged — the cached test result is reused.
    assert wf._iteration_state.iter_count == iter_before

    # Trigger time *is* recorded so the next ``[fix]`` is subject to
    # the debounce window starting from this acceptance.
    assert wf._iteration_state.last_fix_trigger_at == _ANCHOR_NOW

    # Re-test protected audit is queued exactly once; debounce audit
    # is not present (we anchored ourselves outside the window).
    assert (
        wf._pending_audit_actions.count(
            FIX_RETEST_PROTECTED_AUDIT_ACTION
        )
        == 1
    )
    assert FIX_DEBOUNCE_AUDIT_ACTION not in wf._pending_audit_actions

    # No pending fix diff — the workflow body must not re-dispatch
    # the ExecutionRunWorkflow child for this diff.
    assert wf._pending_fix_diff_hash is None


# ---------------------------------------------------------------------------
# Property 6.5 — fresh-diff path advances iter and arms re-test
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    iter_count=st.integers(min_value=0, max_value=MAX_ITER - 1),
    diff_hash=_diff_hash_strategy(),
    cache_seed=_test_results_cache_strategy(),
)
def test_apply_fix_signal_fresh_diff_advances_iter(
    iter_count: int,
    diff_hash: str,
    cache_seed: dict[str, str],
) -> None:
    """When the debounce window is inactive AND the diff hash is *not*
    in the test cache, the handler advances ``iter_count`` by one,
    records ``last_fix_trigger_at`` at ``now``, sets
    ``_pending_fix_diff_hash`` to the supplied hash, and queues NO
    ``[fix]``-family audit row.

    Validates Requirements: 5.4 transition path.
    """

    # Strip the diff hash from the cache so the re-test guard does
    # NOT fire — we want to land on the fresh-diff branch every time.
    cache = {k: v for k, v in cache_seed.items() if k != diff_hash}

    state = IterationState(
        iter_count=iter_count,
        last_fix_trigger_at=None,  # No prior ``[fix]`` → not debounced.
        test_results_by_diff_hash=cache,
    )

    with _patched_workflow_now(_ANCHOR_NOW):
        wf = _build_workflow(state)

        wf._apply_fix_signal(text="[fix] new diff", diff_hash=diff_hash)

    # Iter advanced by exactly one.
    assert wf._iteration_state.iter_count == iter_count + 1

    # Trigger time recorded at ``now``.
    assert wf._iteration_state.last_fix_trigger_at == _ANCHOR_NOW

    # The fresh diff is staged for the next iteration body.
    assert wf._pending_fix_diff_hash == diff_hash

    # No ``[fix]``-family audit row queued — the fresh-diff path is
    # the only branch that consumes a real iteration and therefore
    # neither short-circuit audit fires.
    assert FIX_DEBOUNCE_AUDIT_ACTION not in wf._pending_audit_actions
    assert (
        FIX_RETEST_PROTECTED_AUDIT_ACTION not in wf._pending_audit_actions
    )

    # Signal pending edge flipped so the run body picks the change up.
    assert wf._signal_pending is True


# ---------------------------------------------------------------------------
# Property 6.6 — sequential ``[fix]`` semantics (debounced → protected → new)
# ---------------------------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    iter_count_seed=st.integers(min_value=0, max_value=MAX_ITER - 2),
    cached_diff=_diff_hash_strategy(),
    fresh_diff=_diff_hash_strategy(),
    test_status=st.sampled_from(("passed", "failed", "timeout")),
)
def test_sequential_fix_debounced_protected_then_new_iter(
    iter_count_seed: int,
    cached_diff: str,
    fresh_diff: str,
    test_status: str,
) -> None:
    """A three-step ``[fix]`` sequence

        (1) debounce_dropped  — fired inside the 60s window
        (2) re_test_protected — same diff hash as the cached test
        (3) new ExecutionRunWorkflow — fresh diff hash, window expired

    produces a single terminal state where ``iter_count`` advanced by
    exactly one and the audit queue contains, in order, exactly
    ``[FIX_DEBOUNCE_AUDIT_ACTION, FIX_RETEST_PROTECTED_AUDIT_ACTION]``.

    Validates Requirements: 5.3, 5.4.
    """

    # Ensure the two diff hashes differ so step (3) really lands on
    # the fresh-diff branch. Hypothesis filtering keeps the strategy
    # space dense without throwing away meaningful examples.
    if cached_diff == fresh_diff:
        # Bias the fresh diff to a deterministic alternative — keeps
        # the example shrinkable while preserving the invariant.
        fresh_diff = next(
            h for h in _DIFF_HASH_ALPHABET if h != cached_diff
        )

    # Anchor the workflow at iter=iter_count_seed with the cached diff
    # already tested.
    state = IterationState(
        iter_count=iter_count_seed,
        last_fix_trigger_at=_ANCHOR_NOW,  # → step (1) is debounced
        test_results_by_diff_hash={cached_diff: test_status},
    )
    wf = _build_workflow(state)

    # ----- Step (1): inside the debounce window → drop ----------------
    debounce_now = _ANCHOR_NOW + timedelta(seconds=10)
    with _patched_workflow_now(debounce_now):
        wf._apply_fix_signal(text="[fix]", diff_hash=cached_diff)

    assert wf._iteration_state.iter_count == iter_count_seed
    assert wf._iteration_state.last_fix_trigger_at == _ANCHOR_NOW
    assert wf._pending_audit_actions == [FIX_DEBOUNCE_AUDIT_ACTION]

    # ----- Step (2): outside the window, cached diff → protected ------
    protected_now = _ANCHOR_NOW + FIX_DEBOUNCE_WINDOW + timedelta(seconds=5)
    with _patched_workflow_now(protected_now):
        wf._apply_fix_signal(
            text="[fix] still broken?", diff_hash=cached_diff
        )

    assert wf._iteration_state.iter_count == iter_count_seed
    assert wf._iteration_state.last_fix_trigger_at == protected_now
    assert wf._pending_fix_diff_hash is None
    assert wf._pending_audit_actions == [
        FIX_DEBOUNCE_AUDIT_ACTION,
        FIX_RETEST_PROTECTED_AUDIT_ACTION,
    ]

    # ----- Step (3): outside the window, fresh diff → new run ---------
    fresh_now = (
        protected_now + FIX_DEBOUNCE_WINDOW + timedelta(seconds=1)
    )
    with _patched_workflow_now(fresh_now):
        wf._apply_fix_signal(
            text="[fix] try the new patch", diff_hash=fresh_diff
        )

    # Iter advanced by exactly one across the whole sequence — only
    # step (3) consumed an iteration.
    assert wf._iteration_state.iter_count == iter_count_seed + 1
    assert wf._iteration_state.last_fix_trigger_at == fresh_now
    assert wf._pending_fix_diff_hash == fresh_diff

    # Audit queue is unchanged — step (3) on the fresh-diff path queues
    # NO ``[fix]``-family audit row (the iter advance itself is the
    # observable side effect).
    assert wf._pending_audit_actions == [
        FIX_DEBOUNCE_AUDIT_ACTION,
        FIX_RETEST_PROTECTED_AUDIT_ACTION,
    ]
    assert wf._signal_pending is True
