"""Pure LLM hash-dedup helpers — finding dedup + diff-summary cache.

This module is the **single source of truth** for the two
hash-dedup helpers used by PR review finding suppression and
diff-summary caching:

* :func:`dedup_findings` — set-difference filter that suppresses
  PR-review findings whose ``hash`` was already posted in a prior
  iteration.
* :func:`compute_diff_summary` — pure cache-aware wrapper around an
  LLM-summarisation callback so a follow-up ``[fix]`` against an
  unchanged diff hash is served from the cache without a second
  LLM round-trip.

Both helpers are **pure**:

* No I/O; the LLM round-trip is supplied as a caller callable.
* No mutation of the input arguments; cache writes return a *new*
  cache so the caller decides whether to swap.
* No clock or randomness — replay-deterministic from inside a
  Temporal workflow body.
"""

from __future__ import annotations

from typing import Callable, Mapping

__all__ = [
    "dedup_findings",
    "compute_diff_summary",
]


# ---------------------------------------------------------------------------
# Finding dedup — set difference with order preservation.
# ---------------------------------------------------------------------------


def dedup_findings(
    previous_hashes: set[str],
    current_findings: list[dict],
) -> list[dict]:
    """Return the findings whose ``hash`` is *not* in ``previous_hashes``.

    Pure set-difference filter, mirrored from the placeholder in
    :func:`agent_runner.workflows.agent_runner_workflow._dedup_findings`.

    Contract:

    * **Subset / identity** — every entry in the output is the same
      *object* as one of the inputs (no copy, no fabrication).
    * **No previous hashes** — ``f["hash"] not in previous_hashes``
      for every ``f`` in the output.
    * **Order preserved** — surviving entries appear in their
      original ``current_findings`` order (first-seen-wins).
    * **Idempotent** — ``dedup(prev, dedup(prev, current)) ==
      dedup(prev, current)``.
    * **No mutation** — neither argument is modified.
    * **Empty / falsy hash dropped** — entries lacking a stable
      identity (``hash`` missing, ``None``, or empty string) are
      suppressed; the dedup contract cannot be satisfied without a
      hash so the safest default is to drop the entry.

    Parameters
    ----------
    previous_hashes:
        Set of hashes already posted in prior iterations.
    current_findings:
        List of finding dicts produced by the current iteration.
        Each dict is expected to carry a ``hash`` key; entries
        without one (or with ``None`` / empty string) are dropped.

    Returns
    -------
    list[dict]
        Filtered findings, in the original order, sharing object
        identity with the input entries.  The input list is left
        untouched.
    """

    new_findings: list[dict] = []
    for finding in current_findings:
        finding_hash = (
            finding.get("hash") if isinstance(finding, dict) else None
        )
        if not finding_hash or finding_hash in previous_hashes:
            continue
        new_findings.append(finding)
    return new_findings


# ---------------------------------------------------------------------------
# Diff-summary cache — pure cache-aware wrapper around an LLM callback.
# ---------------------------------------------------------------------------


def compute_diff_summary(
    diff_hash: str,
    cache: Mapping[str, str],
    llm_callback: Callable[[], str],
) -> tuple[str, dict[str, str]]:
    """Return ``(summary, new_cache)`` for the supplied diff hash.

    Pure helper consumed by the ``code_change_commit_only`` flow.
    The contract:

    * **Cache hit** — ``diff_hash`` already in ``cache`` → return
      the cached summary verbatim and a *copy* of the input cache.
      The LLM callback is **not** invoked.
    * **Cache miss** — ``diff_hash`` absent → invoke
      ``llm_callback()``, write the result into a *copy* of the
      cache, and return the new ``(summary, cache)`` pair.
    * **Never mutate** — the input ``cache`` is treated as borrowed
      and is *not* modified in place.  The returned cache is a
      fresh ``dict`` so the caller decides whether to swap (e.g.
      via :func:`dataclasses.replace` on a frozen state container).

    Empty / falsy ``diff_hash`` is allowed but always misses
    (mirrors :func:`dedup_findings`'s "no stable identity" rule);
    the resulting summary is still cached under the empty key so
    repeated calls within the same workflow stay deterministic.

    Parameters
    ----------
    diff_hash:
        Stable identity for the diff being summarised.
    cache:
        Existing diff-hash → summary mapping.  Treated as
        read-only.
    llm_callback:
        Zero-argument callable invoked exactly once on a cache
        miss; expected to return the LLM-produced summary string.

    Returns
    -------
    tuple[str, dict[str, str]]
        ``(summary, new_cache)``.  ``new_cache`` is a freshly
        constructed ``dict`` containing every entry of the input
        cache plus (on a miss) the new summary.
    """

    if diff_hash in cache:
        # Cache hit — copy the cache so the caller never observes
        # the input mapping aliased into their state.
        return cache[diff_hash], dict(cache)

    summary = llm_callback()
    new_cache: dict[str, str] = dict(cache)
    new_cache[diff_hash] = summary
    return summary, new_cache
