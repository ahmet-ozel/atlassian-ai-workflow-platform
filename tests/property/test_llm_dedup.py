"""invariant 15 - LLM hash dedup family.



Invariant statement
------------------------------------------------------------

The ``pr_review`` body of:class:`AgentRunnerWorkflow` consults the
workflow-local ``previous_findings`` set before posting comments to a
PR so the same finding is never re-posted across iterations
( - §16.14 G13). The orphan-branches / commit-only flow
also caches per-diff-hash LLM summaries on
``self._diff_summary_cache`` so a follow-up ``[fix]`` against an
unchanged diff hits the cache and skips the LLM round-trip
( - §16.14.7 V7).

The pure helper under test is:func:`agent_runner.workflows.agent_runner_workflow._dedup_findings`,
which is the placeholder mirror of
``temporal_shared.llm_dedup.dedup_findings`` (the latter lands with. When the real module is missing the placeholder is the
production code path, so the invariants verified here are the
contract end users observe today.

For any hypothesis-generated pair ``(previous_hashes,
current_findings)`` the helper SHALL satisfy:

(P1) **Subset** - every entry in the returned list is also present
 in ``current_findings`` (no new findings are fabricated). The
 comparison is by *identity* (``is``) since the helper must not
 copy / mutate the entries it forwards.

(P2) **No previous hashes** - for every finding ``f`` in the output,
 ``f["hash"] not in previous_hashes``. Combined with P1 this is
 the set-difference invariant ``output ⊆ current_findings``
 minus ``{f: f["hash"] ∈ previous_hashes}``.

(P3) **Order preserved (first-seen-wins)** - the relative order of
 surviving findings matches their order in ``current_findings``.
 We deliberately do *not* deduplicate by hash *within* the
 current batch: the helper trusts the caller to have produced a
 canonical batch, and the unit-test layer covers the
 in-batch-duplicate edge case directly.

(P4) **Idempotence** - ``dedup(prev, dedup(prev, current)) ==
 dedup(prev, current)``. A second pass through the same filter
 never drops further entries because everything that survived
 the first call had ``hash ∉ prev`` already.

(P5) **No mutation** - neither ``previous_hashes`` nor
 ``current_findings`` is modified by the call. The helper
 receives both as borrowed references; mutating either would
 break Temporal replay determinism ( / invariant).

(P6) **Empty / falsy hash** - a finding whose ``hash`` field is
 missing, ``None``, or the empty string is dropped. The set
 ``{None, ""}`` is treated as "no stable identity" - without a
 hash the dedup contract cannot be satisfied so the entry is
 suppressed by design (mirrors the placeholder body).

For the per-workflow ``_diff_summary_cache`` instance attribute on:class:`AgentRunnerWorkflow` - a plain ``dict[str, str]`` LRU
placeholder until lands the bounded variant:func:`temporal_shared.llm_dedup.diff_summary_cache_get` - the
following invariants hold:

(P7) **Cache hit determinism** - once ``cache[h] = s`` is written,
 ``cache.get(h)`` returns ``s`` verbatim on every subsequent
 read until the entry is overwritten. Two separate workflow
 instances each maintain their own cache (no cross-instance
 leakage), preserving per-workflow isolation.

(P8) **Cache miss  ``None``** - ``cache.get(h)`` returns ``None``
 for any hash that was never written. This is the signal the
 ``code_change_commit_only`` body uses to decide whether to
 invoke the LLM-summarisation activity (cache miss) or reuse a
 prior summary (cache hit) - see
 ``test_diff_summary_cached_across_iterations`` in
 ``platform/workers/agent-runner-worker/tests/unit/test_agent_runner_code_change.py``.

Not in scope
------------

* The bounded-LRU eviction semantics scheduled for:mod:`temporal_shared.llm_dedup`. Until that module
 ships the workflow uses an unbounded ``dict``; an eviction
 property would have nothing to assert against. When the module
 appears the existing:func:`_LLM_DEDUP_MODULE_AVAILABLE` flag in
 the workflow file flips ``True`` and the placeholder is no longer
 exercised - a follow-up extension of this file will
 add the eviction property.
* The actual LLM-summarisation activity (``llm_summarize_diff`` or
 similar) - owned by the activity layer and exercised by the
 worker's unit tests.
* The ``pr_review`` body's posting / partial-failure path - owned
 by the activity tests and ``test_multi_iter_po_review.py``.
"""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# sys.path bootstrap - mirrors ``test_fix_keyword.py`` and
# ``test_explain_keyword.py``. The agent-runner-worker source tree is
# not pip-installed in the test environment so we expose it manually.
# ---------------------------------------------------------------------------

# tests/property/test_llm_dedup.py  platform/
_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]

_REQUIRED_SRC_DIRS: tuple[Path, ...] = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src",
    _PLATFORM_ROOT / "libs" / "mcp_client" / "src",
    _PLATFORM_ROOT / "workers" / "agent-runner-worker" / "src",
)
for _src in _REQUIRED_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


# noqa: E402 - imports must follow the sys.path bootstrap above.

from agent_runner.workflows.agent_runner_workflow import (  # noqa: E402
    AgentRunnerWorkflow,
    _dedup_findings,
)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Small alphabet of finding hashes - keeps the hit / miss branches
#: of ``_dedup_findings`` both well-covered. Hypothesis would
#: otherwise spend most of its budget on hashes that never collide
#: with any pre-seeded entry.
_HASH_ALPHABET: tuple[str, ...] = (
    "h1", "h2", "h3", "h4", "h5", "h6", "h7", "h8",
)


def _hash_strategy() -> st.SearchStrategy[str]:
    """Strategy emitting one of the small alphabet hashes."""

    return st.sampled_from(_HASH_ALPHABET)


def _previous_hashes_strategy() -> st.SearchStrategy[set[str]]:
    """Strategy emitting a ``previous_hashes`` set.

 Capped at the alphabet length so hypothesis cannot generate
 states the workflow could not reach in practice.
 """

    return st.sets(
        elements=_hash_strategy(),
        max_size=len(_HASH_ALPHABET),
    )


def _finding_strategy() -> st.SearchStrategy[dict]:
    """Strategy emitting a single finding dict.

 The ``hash`` field is mandatory and drawn from the small
 alphabet; the ``body`` field is an arbitrary short text payload
 so we never pin the helper's behaviour to a particular shape of
 finding body. A small ``severity`` token rounds out the
 structure so the dict mirrors the real PR-review payload
 (``hash``, ``body``, ``severity``).
 """

    return st.fixed_dictionaries({
        "hash": _hash_strategy(),
        "body": st.text(max_size=32),
        "severity": st.sampled_from(("info", "warning", "error")),
    })


def _findings_list_strategy() -> st.SearchStrategy[list[dict]]:
    """Strategy emitting a list of findings, possibly with duplicate hashes."""

    return st.lists(_finding_strategy(), max_size=16)


def _hashless_finding_strategy() -> st.SearchStrategy[dict]:
    """Strategy emitting a finding with a missing / empty / None hash.

 Exercises the P6 branch in:func:`_dedup_findings` where the
 helper drops entries lacking a stable identity.
 """

    return st.one_of(
        # Missing key entirely.
        st.fixed_dictionaries({"body": st.text(max_size=8)}),
        # ``hash`` present but empty.
        st.fixed_dictionaries({
            "hash": st.just(""),
            "body": st.text(max_size=8),
        }),
        # ``hash`` present but ``None``.
        st.fixed_dictionaries({
            "hash": st.none(),
            "body": st.text(max_size=8),
        }),
    )


# ---------------------------------------------------------------------------
# Properties - ``_dedup_findings``
# ---------------------------------------------------------------------------


@given(
    previous_hashes=_previous_hashes_strategy(),
    current_findings=_findings_list_strategy(),
)
@settings(max_examples=100, deadline=None)
def test_output_is_subset_of_current_findings(
    previous_hashes: set[str],
    current_findings: list[dict],
) -> None:
    """P1 - every returned finding is *the same object* from the input list.

 The helper is forbidden from fabricating new entries: each
 output element must be one of the borrowed inputs (identity
 comparison), preserving any reference semantics callers rely on.
 """

    output = _dedup_findings(previous_hashes, current_findings)

    for finding in output:
        assert any(finding is candidate for candidate in current_findings), (
            f"output entry {finding!r} not present (by identity) in "
            f"current_findings"
        )


@given(
    previous_hashes=_previous_hashes_strategy(),
    current_findings=_findings_list_strategy(),
)
@settings(max_examples=100, deadline=None)
def test_no_finding_with_previous_hash_in_output(
    previous_hashes: set[str],
    current_findings: list[dict],
) -> None:
    """P2 - no entry whose ``hash`` is in ``previous_hashes`` survives."""

    output = _dedup_findings(previous_hashes, current_findings)

    for finding in output:
        assert finding["hash"] not in previous_hashes, (
            f"finding hash {finding['hash']!r} should have been dropped "
            f"(in previous_hashes={previous_hashes!r})"
        )


@given(
    previous_hashes=_previous_hashes_strategy(),
    current_findings=_findings_list_strategy(),
)
@settings(max_examples=100, deadline=None)
def test_relative_order_is_preserved(
    previous_hashes: set[str],
    current_findings: list[dict],
) -> None:
    """P3 - surviving findings appear in their original order.

 First-seen-wins: if ``current_findings = [a, b, c]`` and ``b``
 is dropped, the output is ``[a, c]`` - never ``[c, a]``.
 """

    output = _dedup_findings(previous_hashes, current_findings)

    # Recover the input indices of the surviving entries (by
    # identity - the helper does not copy).
    indices: list[int] = []
    for finding in output:
        for idx, candidate in enumerate(current_findings):
            if finding is candidate and idx not in indices:
                indices.append(idx)
                break

    assert indices == sorted(indices), (
        f"output order {indices!r} does not match input order"
    )
    assert len(indices) == len(output), (
        "every output element should map back to a unique input index"
    )


@given(
    previous_hashes=_previous_hashes_strategy(),
    current_findings=_findings_list_strategy(),
)
@settings(max_examples=100, deadline=None)
def test_idempotent_under_repeated_dedup(
    previous_hashes: set[str],
    current_findings: list[dict],
) -> None:
    """P4 - ``dedup(prev, dedup(prev, current)) == dedup(prev, current)``."""

    once = _dedup_findings(previous_hashes, current_findings)
    twice = _dedup_findings(previous_hashes, once)

    assert twice == once, (
        f"second-pass dedup changed the result: {twice!r} != {once!r}"
    )


@given(
    previous_hashes=_previous_hashes_strategy(),
    current_findings=_findings_list_strategy(),
)
@settings(max_examples=100, deadline=None)
def test_inputs_are_not_mutated(
    previous_hashes: set[str],
    current_findings: list[dict],
) -> None:
    """P5 - the helper does not mutate either of its arguments."""

    prev_snapshot = set(previous_hashes)
    current_snapshot = deepcopy(current_findings)

    _dedup_findings(previous_hashes, current_findings)

    assert previous_hashes == prev_snapshot, (
        "previous_hashes was mutated by _dedup_findings"
    )
    assert current_findings == current_snapshot, (
        "current_findings was mutated by _dedup_findings"
    )


@given(
    previous_hashes=_previous_hashes_strategy(),
    hashless_findings=st.lists(_hashless_finding_strategy(), max_size=8),
    well_formed_findings=_findings_list_strategy(),
)
@settings(max_examples=100, deadline=None)
def test_findings_without_hash_are_dropped(
    previous_hashes: set[str],
    hashless_findings: list[dict],
    well_formed_findings: list[dict],
) -> None:
    """P6 - entries lacking a stable hash are suppressed.

 Combined with the well-formed batch we also confirm that the
 presence of hash-less entries does not corrupt the surviving
 output: every retained entry has a non-empty hash that is *not*
 in ``previous_hashes``.
 """

    # Interleave the two lists so the dedup walk encounters mixed
    # entries - exercises the per-element guard, not just a
    # contiguous "all-bad / all-good" prefix.
    mixed: list[dict] = []
    for a, b in zip(hashless_findings, well_formed_findings):
        mixed.append(a)
        mixed.append(b)
    # Append the leftover from whichever list was longer.
    longer_tail = (
        well_formed_findings[len(hashless_findings):]
        if len(well_formed_findings) > len(hashless_findings)
        else hashless_findings[len(well_formed_findings):]
    )
    mixed.extend(longer_tail)

    output = _dedup_findings(previous_hashes, mixed)

    for finding in output:
        finding_hash = finding.get("hash")
        assert finding_hash, (
            f"finding without a stable hash slipped through: {finding!r}"
        )
        assert finding_hash not in previous_hashes, (
            f"finding hash {finding_hash!r} should have been dropped"
        )


# ---------------------------------------------------------------------------
# Properties - ``AgentRunnerWorkflow._diff_summary_cache``
# ---------------------------------------------------------------------------


@given(
    diff_hash=_hash_strategy(),
    summary=st.text(max_size=64),
)
@settings(max_examples=100, deadline=None)
def test_diff_summary_cache_hit_returns_value_verbatim(
    diff_hash: str,
    summary: str,
) -> None:
    """P7 - once written the cache returns the same summary verbatim.

 The ``code_change_commit_only`` body relies on this identity
 contract to skip a redundant LLM call when the diff hash matches
 a prior iteration. A second read returns the *same* string -
 no copy, no transformation.
 """

    wf = AgentRunnerWorkflow()

    # Cache miss baseline - the workflow uses ``None`` as the signal
    # to invoke the LLM.
    assert wf._diff_summary_cache.get(diff_hash) is None

    # Write + first read.
    wf._diff_summary_cache[diff_hash] = summary
    assert wf._diff_summary_cache.get(diff_hash) == summary

    # Second read returns the same value (deterministic per
    # workflow instance).
    assert wf._diff_summary_cache.get(diff_hash) == summary

    # A second workflow instance has its own empty cache - no
    # cross-instance leakage. demands per-workflow isolation
    # so two concurrent workflows never serve each other's
    # summaries.
    other = AgentRunnerWorkflow()
    assert other._diff_summary_cache.get(diff_hash) is None


@given(
    diff_hash=_hash_strategy(),
)
@settings(max_examples=100, deadline=None)
def test_diff_summary_cache_miss_returns_none(
    diff_hash: str,
) -> None:
    """P8 - an unwritten hash yields ``None`` so the body invokes the LLM.

 The workflow body checks ``cache.get(h) is None`` (see
 ``_handle_code_change_commit_only`` in
 ``agent_runner_workflow.py``) to decide whether to fall back to
 the synthesised default summary. Any other miss sentinel would
 silently bypass the fallback path.
 """

    wf = AgentRunnerWorkflow()

    assert wf._diff_summary_cache.get(diff_hash) is None
    # ``dict.get`` with an explicit default also honours the
    # empty-cache contract - the cache never aliases a sentinel.
    assert wf._diff_summary_cache.get(diff_hash, "fallback") == "fallback"
