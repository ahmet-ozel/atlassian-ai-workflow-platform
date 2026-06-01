"""Pure iteration-state helpers for ``AgentRunnerWorkflow``.

This module is the **single source of truth** for the iteration
decision functions and ``IterationState`` evolution helpers spelled
out in ``platform-mimari-workflows`` design.md
§"temporal_shared.iteration" and tasks.md §6.1.

Every helper here is a **pure function**:

* No ``datetime.now()`` / ``time.time()`` / ``random.*`` / I/O — the
  caller passes ``now: datetime`` whenever a timestamp is needed.
* No module-level mutable state.
* Inputs are never mutated; callers receive a freshly-constructed
  :class:`temporal_shared.messages.IterationState` via
  :func:`dataclasses.replace`.

This shape lets the production workflow body import the module
inside ``workflow.unsafe.imports_passed_through()`` and call any
helper directly from a signal handler without breaking Temporal
replay determinism (Property 2 / R5.7).

Public API
----------

* :class:`IterDecision` — frozen dataclass returned by
  :func:`should_advance_iter`.
* :func:`should_advance_iter` — pure pre-condition for advancing
  ``state.iter_count``.
* :func:`is_fix_debounced` — 60-second ``[fix]`` debounce window.
* :func:`fix_should_skip_retest` — diff-hash re-test cache lookup.
* :func:`explain_should_skip_llm` — 5-minute ``[explain]`` cache TTL.
* :func:`needs_info_should_terminate` — consecutive ``needs_info``
  cap.
* :func:`record_explain_answer` — write a new entry into the
  bounded LRU ``explain_cache`` (``EXPLAIN_CACHE_MAXSIZE=32``).
* :data:`EXPLAIN_CACHE_MAXSIZE` — module-level cap.

Re-exports :class:`IterDecision` and :class:`IterationState` so
callers can ``from temporal_shared.iteration import IterDecision``
without reaching into the messages module.

Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6.
"""

from __future__ import annotations

import dataclasses
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from temporal_shared.messages import (
    ExplainCacheEntry,
    IterationState,
)

__all__ = [
    "EXPLAIN_CACHE_MAXSIZE",
    "FIX_DEBOUNCE_WINDOW",
    "EXPLAIN_CACHE_TTL",
    "NEEDS_INFO_MAX_STREAK",
    "IterDecision",
    "IterationState",
    "should_advance_iter",
    "is_fix_debounced",
    "fix_should_skip_retest",
    "explain_should_skip_llm",
    "needs_info_should_terminate",
    "record_explain_answer",
]


# ---------------------------------------------------------------------------
# Constants — defaults consumed by the helpers below.
# ---------------------------------------------------------------------------

#: Hard cap on the bounded ``[explain]`` LRU cache (R5.5, MIMARI §16.7).
#: Writing beyond this cap evicts the **oldest insertion** so the
#: workflow's history never balloons unbounded across long-lived runs.
EXPLAIN_CACHE_MAXSIZE: Final[int] = 32

#: Default ``[fix]`` debounce window (R5.4, MIMARI §16.15 T6).
FIX_DEBOUNCE_WINDOW: Final[timedelta] = timedelta(seconds=60)

#: Default ``[explain]`` cache TTL (R5.5, MIMARI §16.11 Z10).
EXPLAIN_CACHE_TTL: Final[timedelta] = timedelta(minutes=5)

#: Default ``needs_info`` consecutive-comment cap (R5.6, MIMARI §16.15
#: S12).
NEEDS_INFO_MAX_STREAK: Final[int] = 3


# ---------------------------------------------------------------------------
# Decision dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IterDecision:
    """Pure pre-condition result for advancing ``IterationState.iter_count``.

    Attributes
    ----------
    advance:
        ``True`` iff the workflow may safely transition to
        ``iter_count + 1``.  ``False`` for any of the documented
        denial reasons (cap reached, non-positive ``max_iter``).
    reason:
        Short human-readable discriminator.  Stable values:

        * ``"ok"`` — the cap has not been reached.
        * ``"max_iter_reached"`` — ``state.iter_count >= max_iter``.
        * ``"non_positive_max_iter"`` — caller passed ``max_iter <= 0``.

        The string is intentionally short so audit emitters can use
        it verbatim as an audit-action suffix without a translation
        layer.
    """

    advance: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# Decision helpers — pure pre-conditions consulted by the workflow body.
# ---------------------------------------------------------------------------


def should_advance_iter(
    state: IterationState, max_iter: int
) -> IterDecision:
    """Pure pre-condition: may we advance to ``state.iter_count + 1``?

    The check is purely arithmetic — no clock, no randomness — so
    callers can invoke it from inside a Temporal signal handler
    without breaking replay determinism (Property 2).

    Parameters
    ----------
    state:
        Current :class:`IterationState`.
    max_iter:
        Hard cap on iterations.  A non-positive value is rejected
        immediately so a misconfigured caller can never bypass the
        cap by passing zero or a negative number.

    Returns
    -------
    IterDecision
        ``advance=True`` with ``reason="ok"`` when the cap has not
        been reached; ``advance=False`` with the matching reason
        otherwise.
    """

    if max_iter <= 0:
        return IterDecision(advance=False, reason="non_positive_max_iter")
    if state.iter_count >= max_iter:
        return IterDecision(advance=False, reason="max_iter_reached")
    return IterDecision(advance=True, reason="ok")


def is_fix_debounced(
    state: IterationState,
    now: datetime,
    window: timedelta = FIX_DEBOUNCE_WINDOW,
) -> bool:
    """Pure: ``True`` iff the most recent ``[fix]`` is inside the window.

    Returns ``False`` when no ``[fix]`` has fired yet
    (``state.last_fix_trigger_at is None``) so a fresh trigger is
    always accepted.

    Parameters
    ----------
    state:
        Current :class:`IterationState`.
    now:
        Caller-supplied timestamp — must come from
        ``workflow.now()`` inside a workflow body so replay stays
        deterministic.
    window:
        Debounce window — defaults to :data:`FIX_DEBOUNCE_WINDOW`
        (60 seconds).
    """

    last = state.last_fix_trigger_at
    if last is None:
        return False
    return (now - last) < window


def fix_should_skip_retest(
    state: IterationState, current_diff_hash: str
) -> bool:
    """Pure: ``True`` iff a prior test result exists for this diff hash.

    Used by the ``code_change_with_test`` flow (R5.3, MIMARI
    §16.15 T1): re-running a ``[fix]`` against an unchanged diff
    must reuse the cached test outcome instead of dispatching a
    second :class:`ExecutionRunWorkflow`.

    Empty / falsy ``current_diff_hash`` returns ``False`` — without
    a stable identity the cache cannot answer the question, so the
    safest default is to run the test.
    """

    if not current_diff_hash:
        return False
    return current_diff_hash in state.test_results_by_diff_hash


def explain_should_skip_llm(
    state: IterationState,
    pr_diff_hash: str,
    now: datetime,
    ttl: timedelta = EXPLAIN_CACHE_TTL,
) -> bool:
    """Pure: ``True`` iff a cached ``[explain]`` answer is still fresh.

    The boundary is **strict** (``< ttl``): a delta of exactly
    ``ttl`` is *not* fresh.  This keeps the freshness window
    identical across replays even when the workflow's clock advances
    by a single tick on the boundary.

    Empty / falsy ``pr_diff_hash`` returns ``False`` — same rationale
    as :func:`fix_should_skip_retest`.

    Parameters
    ----------
    state:
        Current :class:`IterationState`.
    pr_diff_hash:
        Hash of the PR diff at the time the ``[explain]`` keyword
        fired.
    now:
        Caller-supplied timestamp (``workflow.now()``).
    ttl:
        Freshness window — defaults to :data:`EXPLAIN_CACHE_TTL`
        (5 minutes).
    """

    if not pr_diff_hash:
        return False
    entry = state.explain_cache.get(pr_diff_hash)
    if entry is None:
        return False
    return (now - entry.issued_at) < ttl


def needs_info_should_terminate(
    state: IterationState, max_streak: int = NEEDS_INFO_MAX_STREAK
) -> bool:
    """Pure: ``True`` iff the consecutive ``needs_info`` cap is reached.

    R5.6 / MIMARI §16.15 S12: when the bot has emitted ``max_streak``
    ``needs_info`` comments in a row without a substantive reply
    from the user, the workflow transitions to ``out_of_scope`` and
    stops asking.
    """

    return state.needs_info_streak >= max_streak


# ---------------------------------------------------------------------------
# State-evolution helpers — bounded LRU write for ``explain_cache``.
# ---------------------------------------------------------------------------


def record_explain_answer(
    state: IterationState,
    pr_diff_hash: str,
    answer: str,
    now: datetime,
) -> IterationState:
    """Return a new state with the ``[explain]`` cache extended.

    Implements the bounded LRU contract from R5.5 / tasks.md §6.1:

    * The cache is capped at :data:`EXPLAIN_CACHE_MAXSIZE` (32)
      entries.
    * Insertion order is the eviction order — the oldest entry is
      dropped when a fresh write would exceed the cap.
    * Writing the same key twice **refreshes** the entry (key moves
      to the most-recent slot) so a follow-up ``[explain]`` against
      the same diff stays fresh in the cache.
    * The input ``state`` is never mutated; callers receive a new
      :class:`IterationState` via :func:`dataclasses.replace`.

    The bounded LRU is implemented via :class:`collections.OrderedDict`
    so the eviction policy is the ordinary "first inserted is first
    evicted" semantics of an ordered mapping — no custom data
    structure is required.

    Parameters
    ----------
    state:
        Current :class:`IterationState`.
    pr_diff_hash:
        Hash of the PR diff to use as the cache key.
    answer:
        Cached explanation text.
    now:
        Caller-supplied timestamp (``workflow.now()``) used as the
        new entry's ``issued_at``.

    Returns
    -------
    IterationState
        Fresh state with the supplied entry written and (when the
        cap is exceeded) the oldest entry evicted.
    """

    # Build a new OrderedDict so insertion order is the eviction
    # order.  Drop the existing entry first so a refresh-write moves
    # the key to the most-recent slot rather than retaining the
    # original insertion position.
    cache: OrderedDict[str, ExplainCacheEntry] = OrderedDict(state.explain_cache)
    if pr_diff_hash in cache:
        del cache[pr_diff_hash]
    cache[pr_diff_hash] = ExplainCacheEntry(answer=answer, issued_at=now)

    while len(cache) > EXPLAIN_CACHE_MAXSIZE:
        cache.popitem(last=False)  # evict oldest insertion

    return dataclasses.replace(state, explain_cache=cache)
