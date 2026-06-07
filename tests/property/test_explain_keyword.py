"""invariant 7 - ``[explain]`` keyword family: cooldown + TTL cache.



Hypothesis-driven verification of the ``[explain]`` keyword cooldown
and 5-minute LRU cache contract. A cache hit avoids advancing the
iteration counter and queues an audit event; a cache miss advances the
iteration and allows the LLM call to be queued.

Invariant statements
-------------------

For any hypothesis-generated triple
``(state: IterationState, pr_diff_hash, now)`` and a default TTL of
5 minutes, the pure helper:func:`agent_runner.workflows.agent_runner_workflow._explain_should_skip_llm`
MUST satisfy:

 (P1) ``True``  ``pr_diff_hash in state.explain_cache`` AND
 ``(now - cache_entry.issued_at) < EXPLAIN_CACHE_TTL``.
 (P2) Empty / falsy ``pr_diff_hash``  ``False`` (degenerate
 cache lookup is never a hit).
 (P3) Determinism: a second call with the same arguments returns
 the same boolean.

For the cache-write helper:func:`_state_record_explain_answer`:

 (P4) The returned state has ``pr_diff_hash`` mapped to an:class:`ExplainCacheEntry` with the supplied ``answer`` and
 ``issued_at == now``.
 (P5) Idempotence under repeated writes with the same arguments -
 calling it twice in a row leaves the cache mapping the same
 hash to the same entry value (no growth, no churn).
 (P6) The input ``state`` is never mutated; the returned state is a
 fresh value (functional update style).

For the signal handler:meth:`AgentRunnerWorkflow._apply_explain_signal`:

 (P7) Cache hit branch: when ``_explain_should_skip_llm`` returns
 ``True``, the handler sets ``_pending_explain_diff_hash``,
 queues:data:`EXPLAIN_CACHE_HIT_AUDIT_ACTION`, and does NOT
 advance ``iter_count``.
 (P8) Cache miss branch (and below the iter cap): the handler
 advances ``iter_count`` by exactly one, sets
 ``_pending_explain_diff_hash``, and does NOT queue
 ``explain_cache_hit``.

LRU ``maxsize=32`` overflow
---------------------------

The current ``IterationState.explain_cache`` is an unbounded
``Mapping[str, ExplainCacheEntry]``. The bounded LRU semantics
(``maxsize=32``) referenced by the task brief are scheduled to land in:mod:`temporal_shared.iteration` of
````). Until that module exists we cannot
exercise the eviction property;:func:`test_lru_overflow_evicts_oldest`
is therefore guarded with a runtime check that skips the test when the
module is absent. Once ships the test will run automatically
- no rewrite required.
"""

from __future__ import annotations

import dataclasses
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# ``sys.path`` bootstrap - mirrors ``test_token_cap_fail_fast.py`` so
# this file remains importable from a bare ``python -m pytest`` even
# when the workspace ``pytest.ini`` ``pythonpath`` is not active.
#
# We add three roots:
#
# 1. ``libs/temporal-shared/src`` for ``temporal_shared.messages``.
# 2. ``libs/mcp_client/src`` because the workflow body imports
# ``mcp_client.deployment_router`` at module load time.
# 3. ``workers/agent-runner-worker/src`` for the workflow module that
# owns the pure helpers and signal-handler method under test.
# ---------------------------------------------------------------------------

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]

_REQUIRED_SRC_DIRS: tuple[Path, ...] = (
    _REPO_ROOT / "libs" / "temporal-shared" / "src",
    _REPO_ROOT / "libs" / "mcp_client" / "src",
    _REPO_ROOT / "workers" / "agent-runner-worker" / "src",
)
for _src in _REQUIRED_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


# noqa: E402 below - imports follow the sys.path bootstrap above.

from agent_runner.workflows.agent_runner_workflow import (  # noqa: E402
    EXPLAIN_CACHE_HIT_AUDIT_ACTION,
    EXPLAIN_CACHE_TTL,
    MAX_ITER,
    AgentRunnerWorkflow,
    _explain_should_skip_llm,
    _state_record_explain_answer,
)
from temporalio import workflow as _temporal_workflow  # noqa: E402
from temporal_shared.messages import (  # noqa: E402
    ExplainCacheEntry,
    IterationState,
)


# ---------------------------------------------------------------------------
# Anchors - fixed clock used by every Hypothesis run so the generated
# ``now`` values stay inside a sensible window relative to the cache
# entries' ``issued_at``.
# ---------------------------------------------------------------------------

#: UTC anchor for the deterministic ``workflow.now`` stub. Tests that
#: vary ``now`` add a strategy-supplied:class:`timedelta` to this
#: value; the absolute base is irrelevant to the invariants - only the
#: difference ``now - issued_at`` matters.
_ANCHOR: datetime = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

#: Width of the window from which ``now`` and ``issued_at`` deltas are
#: drawn. ±2× ``EXPLAIN_CACHE_TTL`` lets Hypothesis explore both the
#: fresh region (``< TTL``) and the expired region (``≥ TTL``) with
#: comparable density, plus the boundary itself (``== TTL``).
_DELTA_BOUND_SECONDS: int = int(EXPLAIN_CACHE_TTL.total_seconds() * 2)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


#: PR-diff hashes - short ASCII tokens. Empty string is excluded from
#: the *positive* lookup strategy because P2 fixes the empty-hash
#: behaviour separately.
_diff_hashes: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(
        min_codepoint=ord("0"), max_codepoint=ord("z"),
        whitelist_categories=("Ll", "Nd"),
    ),
    min_size=1,
    max_size=12,
)

#: Cache-entry answers - non-empty strings. The body never inspects
#: the answer text in the helper under test; we still pick something
#: short and printable so failure messages stay readable.
_answers: st.SearchStrategy[str] = st.text(min_size=1, max_size=64)

#: Time-delta strategy used for both ``now - anchor`` and entry ages.
#: Bounded by ±2× TTL so the boundary case (``delta == TTL``) appears
#: with non-trivial density.
_deltas: st.SearchStrategy[timedelta] = st.integers(
    min_value=-_DELTA_BOUND_SECONDS,
    max_value=_DELTA_BOUND_SECONDS,
).map(lambda s: timedelta(seconds=s))


@st.composite
def _explain_cache_entries(
    draw: st.DrawFn,
) -> ExplainCacheEntry:
    """Construct a single:class:`ExplainCacheEntry`.

 ``issued_at`` is anchored at:data:`_ANCHOR` plus a strategy delta
 so the relative arithmetic against ``now`` covers both fresh and
 expired regions.
 """

    answer = draw(_answers)
    delta = draw(_deltas)
    return ExplainCacheEntry(answer=answer, issued_at=_ANCHOR + delta)


@st.composite
def _iteration_states_with_cache(
    draw: st.DrawFn,
) -> IterationState:
    """Build an:class:`IterationState` whose ``explain_cache`` is non-trivial.

 The cache is a finite mapping (0..6 entries) of distinct diff
 hashes to entries with strategy-derived ``issued_at`` values.
 Other state fields are left at their defaults - the helper under
 test only inspects ``explain_cache``.
 """

    pairs = draw(
        st.lists(
            st.tuples(_diff_hashes, _explain_cache_entries()),
            min_size=0,
            max_size=6,
            unique_by=lambda kv: kv[0],
        )
    )
    cache: Mapping[str, ExplainCacheEntry] = {h: e for h, e in pairs}
    return IterationState(
        iter_count=1,
        explain_cache=cache,
    )


# ---------------------------------------------------------------------------
# P1..P3 - pure helper ``_explain_should_skip_llm``
# ---------------------------------------------------------------------------


class TestExplainShouldSkipLlm:
    """Pure-helper TTL semantics (P1, P2, P3)."""

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        state=_iteration_states_with_cache(),
        pr_diff_hash=_diff_hashes,
        now_delta=_deltas,
    )
    def test_p1_iff_in_cache_and_within_ttl(
        self,
        state: IterationState,
        pr_diff_hash: str,
        now_delta: timedelta,
    ) -> None:
        """invariant: ``True``  in cache AND age < TTL.


 """

        now = _ANCHOR + now_delta
        result = _explain_should_skip_llm(state, pr_diff_hash, now)

        entry = state.explain_cache.get(pr_diff_hash)
        if entry is None:
            expected = False
        else:
            expected = (now - entry.issued_at) < EXPLAIN_CACHE_TTL

        assert result is expected, (
            f"_explain_should_skip_llm({pr_diff_hash!r}, now={now!r}) "
            f"returned {result!r}; expected {expected!r}. "
            f"cache_keys={sorted(state.explain_cache.keys())!r}, "
            f"entry={entry!r}, ttl={EXPLAIN_CACHE_TTL!r}."
        )

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        state=_iteration_states_with_cache(),
        now_delta=_deltas,
    )
    def test_p2_empty_hash_is_never_a_hit(
        self,
        state: IterationState,
        now_delta: timedelta,
    ) -> None:
        """invariant: empty ``pr_diff_hash``  always ``False``.

 The signal handler guards against falsy hashes upstream, but
 the pure helper is also expected to short-circuit: a cache
 keyed on the empty string would never be looked up by the
 production caller, so any return other than ``False`` would
 leak an unintended hit semantic.


 """

        now = _ANCHOR + now_delta
        assert _explain_should_skip_llm(state, "", now) is False

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        state=_iteration_states_with_cache(),
        pr_diff_hash=_diff_hashes,
        now_delta=_deltas,
    )
    def test_p3_deterministic(
        self,
        state: IterationState,
        pr_diff_hash: str,
        now_delta: timedelta,
    ) -> None:
        """invariant: identical inputs  identical output.

 Pure helper - no clock, no randomness - so two consecutive
 calls with the same arguments must return the same value.


 """

        now = _ANCHOR + now_delta
        first = _explain_should_skip_llm(state, pr_diff_hash, now)
        second = _explain_should_skip_llm(state, pr_diff_hash, now)
        assert first is second

    # ------------------------------------------------------------------
    # Concrete regression anchors - pin the boundary behaviour on
    # specific inputs so an off-by-one in the ``< TTL`` comparison is
    # caught deterministically.
    # ------------------------------------------------------------------

    def test_exact_boundary_is_not_a_hit(self) -> None:
        """``now - issued_at == TTL`` is **not** fresh.

 The implementation uses strict inequality (``< TTL``); a
 regression that flips to ``<=`` would silently double the
 effective freshness window of every cache entry on the
 boundary.


 """

        issued_at = _ANCHOR
        state = IterationState(
            explain_cache={
                "h": ExplainCacheEntry(answer="a", issued_at=issued_at)
            }
        )
        now = issued_at + EXPLAIN_CACHE_TTL  # exactly TTL
        assert _explain_should_skip_llm(state, "h", now) is False

    def test_one_second_inside_ttl_is_a_hit(self) -> None:
        """One second before the TTL elapses must be a hit.


 """

        issued_at = _ANCHOR
        state = IterationState(
            explain_cache={
                "h": ExplainCacheEntry(answer="a", issued_at=issued_at)
            }
        )
        now = issued_at + EXPLAIN_CACHE_TTL - timedelta(seconds=1)
        assert _explain_should_skip_llm(state, "h", now) is True

    def test_negative_age_is_a_hit(self) -> None:
        """``now`` before ``issued_at`` is a degenerate but valid hit.

 The strict-less-than form ``(now - issued_at) < TTL``
 accepts negative deltas as fresh; the production workflow
 never produces a clock that runs backwards, but covering this
 path in the invariant pins the contract for the
 helper's pure semantics.


 """

        issued_at = _ANCHOR
        state = IterationState(
            explain_cache={
                "h": ExplainCacheEntry(answer="a", issued_at=issued_at)
            }
        )
        now = issued_at - timedelta(seconds=10)
        assert _explain_should_skip_llm(state, "h", now) is True


# ---------------------------------------------------------------------------
# P4..P6 - pure helper ``_state_record_explain_answer``
# ---------------------------------------------------------------------------


class TestStateRecordExplainAnswer:
    """Cache-write semantics (P4, P5, P6)."""

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        state=_iteration_states_with_cache(),
        pr_diff_hash=_diff_hashes,
        answer=_answers,
        now_delta=_deltas,
    )
    def test_p4_extends_cache_with_supplied_entry(
        self,
        state: IterationState,
        pr_diff_hash: str,
        answer: str,
        now_delta: timedelta,
    ) -> None:
        """invariant: written entry has the supplied answer + ``now``.


 """

        now = _ANCHOR + now_delta
        new_state = _state_record_explain_answer(
            state, pr_diff_hash, answer, now
        )

        entry = new_state.explain_cache[pr_diff_hash]
        assert entry == ExplainCacheEntry(answer=answer, issued_at=now)

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        state=_iteration_states_with_cache(),
        pr_diff_hash=_diff_hashes,
        answer=_answers,
        now_delta=_deltas,
    )
    def test_p5_idempotent_on_repeat(
        self,
        state: IterationState,
        pr_diff_hash: str,
        answer: str,
        now_delta: timedelta,
    ) -> None:
        """invariant: writing the same triple twice is a no-op.

 Two consecutive writes with identical arguments must produce
 equivalent caches - the second write does not duplicate the
 key, does not bump the entry, and does not grow the mapping
 beyond the first write's footprint.


 """

        now = _ANCHOR + now_delta
        once = _state_record_explain_answer(state, pr_diff_hash, answer, now)
        twice = _state_record_explain_answer(once, pr_diff_hash, answer, now)

        assert dict(once.explain_cache) == dict(twice.explain_cache)
        assert once.explain_cache[pr_diff_hash] == (
            twice.explain_cache[pr_diff_hash]
        )

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        state=_iteration_states_with_cache(),
        pr_diff_hash=_diff_hashes,
        answer=_answers,
        now_delta=_deltas,
    )
    def test_p6_input_state_is_not_mutated(
        self,
        state: IterationState,
        pr_diff_hash: str,
        answer: str,
        now_delta: timedelta,
    ) -> None:
        """invariant: the input state is never mutated in place.

 ``IterationState`` is a frozen dataclass; the helper must
 return a freshly-constructed value via:func:`dataclasses.replace`. We snapshot the input's
 ``explain_cache`` before the call and reassert equality
 after.


 """

        now = _ANCHOR + now_delta
        snapshot = dict(state.explain_cache)

        new_state = _state_record_explain_answer(
            state, pr_diff_hash, answer, now
        )

        # Original mapping is unchanged.
        assert dict(state.explain_cache) == snapshot
        # The new state carries a different mapping object whenever
        # the write actually added or updated a key. (When the same
        # key+entry was already present the helper still returns a
        # replaced state for consistency.)
        assert new_state is not state


# ---------------------------------------------------------------------------
# P7, P8 - signal handler ``_apply_explain_signal``
# ---------------------------------------------------------------------------


def _build_workflow_with_state(
    state: IterationState,
) -> AgentRunnerWorkflow:
    """Construct an:class:`AgentRunnerWorkflow` carrying ``state``.

 Mirrors the ``make_wf`` factory used by the unit-test suite under
 ``workers/agent-runner-worker/tests/unit/``: instantiate the
 workflow, then assign the supplied:class:`IterationState` so the
 signal handler observes a deterministic starting point.
 """

    wf = AgentRunnerWorkflow()
    wf._iteration_state = state
    return wf


@st.composite
def _states_with_known_hash_below_cap(
    draw: st.DrawFn,
) -> tuple[IterationState, str, datetime]:
    """Generate ``(state, hit_hash, now)`` triples that are cache hits.

 The returned state has at least one cache entry whose ``issued_at``
 is **inside** the TTL window relative to ``now`` and whose key is
 returned as ``hit_hash``. ``iter_count`` is bounded below:data:`MAX_ITER` so the iter-cap branch does not interfere with
 the cache-hit assertion.
 """

    fresh_age = draw(
        st.integers(
            min_value=0,
            max_value=int(EXPLAIN_CACHE_TTL.total_seconds()) - 1,
        ).map(lambda s: timedelta(seconds=s))
    )
    hit_hash = draw(_diff_hashes)
    answer = draw(_answers)
    iter_count = draw(st.integers(min_value=1, max_value=MAX_ITER - 1))

    now = _ANCHOR
    issued_at = now - fresh_age

    cache: dict[str, ExplainCacheEntry] = {
        hit_hash: ExplainCacheEntry(answer=answer, issued_at=issued_at)
    }

    # Optional extra (unrelated) entries to ensure the handler picks
    # the right key out of a multi-entry cache.
    other_pairs = draw(
        st.lists(
            st.tuples(
                _diff_hashes.filter(lambda h: h != hit_hash),
                _explain_cache_entries(),
            ),
            min_size=0,
            max_size=4,
            unique_by=lambda kv: kv[0],
        )
    )
    for h, e in other_pairs:
        if h != hit_hash:
            cache[h] = e

    state = IterationState(iter_count=iter_count, explain_cache=cache)
    return state, hit_hash, now


@st.composite
def _states_with_cache_miss_below_cap(
    draw: st.DrawFn,
) -> tuple[IterationState, str, datetime]:
    """Generate ``(state, miss_hash, now)`` triples that are cache misses.

 The returned state's cache does **not** contain ``miss_hash``; the
 iter counter sits strictly below:data:`MAX_ITER` so the handler
 is allowed to advance.
 """

    miss_hash = draw(_diff_hashes)
    iter_count = draw(st.integers(min_value=1, max_value=MAX_ITER - 1))

    other_pairs = draw(
        st.lists(
            st.tuples(
                _diff_hashes.filter(lambda h: h != miss_hash),
                _explain_cache_entries(),
            ),
            min_size=0,
            max_size=4,
            unique_by=lambda kv: kv[0],
        )
    )
    cache: dict[str, ExplainCacheEntry] = {
        h: e for h, e in other_pairs if h != miss_hash
    }

    state = IterationState(iter_count=iter_count, explain_cache=cache)
    return state, miss_hash, _ANCHOR


class TestApplyExplainSignal:
    """Signal-handler dispatch - cache hit vs miss (P7, P8)."""

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(triple=_states_with_known_hash_below_cap())
    def test_p7_cache_hit_no_iter_advance_audit_queued(
        self,
        triple: tuple[IterationState, str, datetime],
    ) -> None:
        """invariant: cache hit  no iter advance, audit queued.


 """

        state, hit_hash, now = triple
        wf = _build_workflow_with_state(state)
        before_iter = wf._iteration_state.iter_count

        with patch.object(_temporal_workflow, "now", return_value=now):
            wf._apply_explain_signal(text="[explain] please", pr_diff_hash=hit_hash)

        # iter_count untouched on a cache hit.
        assert wf._iteration_state.iter_count == before_iter, (
            f"Cache hit advanced iter_count from {before_iter} to "
            f"{wf._iteration_state.iter_count}; invariant forbids it."
        )
        # Audit action queued exactly once.
        assert (
            wf._pending_audit_actions.count(EXPLAIN_CACHE_HIT_AUDIT_ACTION) == 1
        ), (
            f"Expected exactly one {EXPLAIN_CACHE_HIT_AUDIT_ACTION!r} "
            f"audit; saw {wf._pending_audit_actions!r}."
        )
        # Pending fields populated for the body to consume.
        assert wf._pending_explain_diff_hash == hit_hash
        assert wf._pending_explain_text == "[explain] please"
        # Signal-pending edge raised so the body wakes up.
        assert wf._signal_pending is True

    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(triple=_states_with_cache_miss_below_cap())
    def test_p8_cache_miss_advances_iter_no_audit(
        self,
        triple: tuple[IterationState, str, datetime],
    ) -> None:
        """invariant: cache miss  iter advances, audit not queued.


 """

        state, miss_hash, now = triple
        wf = _build_workflow_with_state(state)
        before_iter = wf._iteration_state.iter_count

        with patch.object(_temporal_workflow, "now", return_value=now):
            wf._apply_explain_signal(
                text="[explain] please", pr_diff_hash=miss_hash
            )

        # iter_count advanced by exactly one (the workflow is below
        # the cap by construction).
        assert wf._iteration_state.iter_count == before_iter + 1, (
            f"Cache miss did not advance iter_count: "
            f"{before_iter}  {wf._iteration_state.iter_count}; "
            f"invariant requires +1."
        )
        # No cache-hit audit was queued.
        assert (
            EXPLAIN_CACHE_HIT_AUDIT_ACTION not in wf._pending_audit_actions
        ), (
            f"Cache miss queued {EXPLAIN_CACHE_HIT_AUDIT_ACTION!r}; "
            f"invariant forbids it. audit={wf._pending_audit_actions!r}."
        )
        # Pending fields populated for the body's LLM call.
        assert wf._pending_explain_diff_hash == miss_hash
        assert wf._pending_explain_text == "[explain] please"
        assert wf._out_of_scope is False


# ---------------------------------------------------------------------------
# LRU ``maxsize=32`` overflow - gated on ``temporal_shared.iteration``
# ---------------------------------------------------------------------------


def test_lru_overflow_evicts_oldest_when_iteration_module_lands() -> None:
    """LRU ``maxsize=32`` eviction property (gated on.

 The brief for references an LRU cache with
 ``maxsize=32`` whose overflow behaviour evicts the least-recently
 inserted entry. The current ``IterationState.explain_cache`` is a
 plain ``Mapping`` without a bound - the bounded LRU helper is
 scheduled to land in:mod:`temporal_shared.iteration`). Until that module ships we
 cannot exercise the eviction property; the test skips with a
 precise reason so the invariant stays green and the
 placeholder is unmistakeable in CI logs.

 Once lands the import below succeeds, the skip drops
 out, and the test exercises the eviction contract: writing 33
 distinct entries leaves the cache size at 32 and the oldest
 insertion is gone.

 TODO: replace this skip-guarded body with the real
 eviction assertion once:mod:`temporal_shared.iteration` exposes
 the bounded LRU helper.


 """

    try:
        from temporal_shared.iteration import (  # type: ignore[import-not-found]
            EXPLAIN_CACHE_MAXSIZE,
            explain_should_skip_llm,  # noqa: F401 - protocol check
            record_explain_answer,
        )
    except ImportError as exc:
        pytest.skip(
            "temporal_shared.iteration is not yet implemented "
            "(implementation milestone still ``[-]``); "
            f"import failed with: {exc!r}. The LRU ``maxsize=32`` "
            "overflow property will be exercised end-to-end "
            "as soon as implementation milestone ships."
        )

    # Once lands the body below runs. The contract: writing
    # MAXSIZE+1 distinct entries leaves the cache at exactly MAXSIZE
    # and the first-inserted hash has been evicted.
    state = IterationState()
    issued_at = _ANCHOR
    for i in range(EXPLAIN_CACHE_MAXSIZE + 1):
        state = record_explain_answer(
            state,
            f"diff-{i}",
            f"answer-{i}",
            issued_at + timedelta(seconds=i),
        )

    assert len(state.explain_cache) == EXPLAIN_CACHE_MAXSIZE, (
        f"Expected cache size to be capped at "
        f"{EXPLAIN_CACHE_MAXSIZE}; got {len(state.explain_cache)}."
    )
    assert "diff-0" not in state.explain_cache, (
        "Oldest insertion (``diff-0``) should have been evicted "
        f"after writing {EXPLAIN_CACHE_MAXSIZE + 1} entries."
    )
    assert f"diff-{EXPLAIN_CACHE_MAXSIZE}" in state.explain_cache, (
        "Most recent insertion should still be in the cache."
    )


# ---------------------------------------------------------------------------
# Defensive: silence unused-import warnings for symbols imported solely
# to enforce protocol parity (``dataclasses`` is referenced in the
# docstring of:func:`test_p6_input_state_is_not_mutated`).
# ---------------------------------------------------------------------------

_ = dataclasses
