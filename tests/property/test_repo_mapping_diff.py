"""Property tests for ``compute_repo_mapping_diff`` (R10.7 / N7).

**Validates: Requirements 10.7**

This file owns the property-based test surface for the pure
set-algebra helper :func:`temporal_shared.repo_sync.compute_repo_mapping_diff`
(workflows spec, task 14.3 — MIMARI §16.16 N7 — repo mapping
auto-sync). The helper is the engine behind the dry-run + apply
modes of the ``POST /admin/departments/{id}/repo-mappings/sync``
admin endpoint; its correctness is the precondition for the
endpoint's "diff is the source of truth" contract.

Universal properties
--------------------

For every input pair ``(scanned_repos, current_mappings)`` the
helper must satisfy the following invariants. Each one is encoded
as a separate Hypothesis ``@given`` test below so a failure points
at the specific algebraic axiom that broke.

1. **Disjointness** — the three partitions are pairwise disjoint::

       added ∩ removed   == ∅
       added ∩ unchanged == ∅
       removed ∩ unchanged == ∅

2. **Reconstruction** — the partitions reconstruct both inputs::

       added ∪ unchanged == scanned_repos
       removed ∪ unchanged == {m.slug for m in current_mappings}

3. **Idempotence on equal inputs** — when ``scanned_repos`` equals
   the set of current slugs, both ``added`` and ``removed`` are
   empty and ``unchanged`` equals the shared set.

4. **Determinism** — calling the helper twice with the same input
   produces equal outputs (no clocks, no random, no I/O).

5. **Empty inputs** — degenerate cases (empty scan, empty mappings,
   both empty) produce the expected empty / projection partitions.

Together these properties exhaustively pin the helper's contract:
any function that satisfies them is observationally equivalent to
the textbook three-set partition. Hypothesis runs each property
with ``max_examples=200`` per design.md §"Property → Test
eşlemesi"; the suite is fast (~ms / example) because the helper is
pure Python set arithmetic.

Source of truth
---------------

* :mod:`temporal_shared.repo_sync` — module under test.
* ``platform-mimari-workflows/requirements.md`` — Requirement 10.7.
* ``platform-mimari-workflows/design.md`` — §"Components and
  Interfaces — repo_mapping_sync API".
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Path setup — make ``temporal_shared`` importable without a wheel
# install. Mirrors the bootstrap used by sibling property tests.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT = Path(__file__).resolve().parents[2]
_TEMPORAL_SHARED_SRC = _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
_TEMPORAL_SHARED_STR = str(_TEMPORAL_SHARED_SRC)
if _TEMPORAL_SHARED_SRC.is_dir() and _TEMPORAL_SHARED_STR not in sys.path:
    sys.path.insert(0, _TEMPORAL_SHARED_STR)


from temporal_shared.repo_sync import (  # noqa: E402
    RepoMapping,
    RepoMappingDiff,
    compute_repo_mapping_diff,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Bitbucket repo slug grammar mirrors the schema used elsewhere in the
# platform: ``^[a-z0-9][a-z0-9-]*$``. We bound length to keep the
# search space tractable; the helper has no length sensitivity so
# 1..16 is enough to exercise short / hyphenated / numeric variants.
_SLUG = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789-"),
    min_size=1,
    max_size=16,
).filter(lambda s: s[0] in "abcdefghijklmnopqrstuvwxyz0123456789")

# Human-readable repo name — any printable character is fine for the
# diff helper since it operates on slugs only. We keep it short so
# the generated counterexamples remain readable.
_NAME = st.text(min_size=0, max_size=20)


@st.composite
def _repo_mapping(draw: st.DrawFn) -> RepoMapping:
    """Build one :class:`RepoMapping` with a generated slug + name."""

    slug = draw(_SLUG)
    name = draw(_NAME)
    return RepoMapping(name=name, slug=slug)


@st.composite
def _scanned_repos(draw: st.DrawFn) -> frozenset[str]:
    """Build a :class:`frozenset` of slugs (the Bitbucket scan input)."""

    return frozenset(draw(st.sets(_SLUG, min_size=0, max_size=12)))


@st.composite
def _current_mappings(
    draw: st.DrawFn,
) -> tuple[RepoMapping, ...]:
    """Build a tuple of :class:`RepoMapping` (the dept's current array).

    The strategy may produce duplicates (two entries with the same
    slug but different names) — the helper folds the tuple into a
    set internally so duplicates collapse, but generating them is
    the easiest way to exercise that branch.
    """

    return tuple(draw(st.lists(_repo_mapping(), min_size=0, max_size=12)))


def _slug_set(mappings: tuple[RepoMapping, ...]) -> frozenset[str]:
    """Project a mapping tuple to its canonical slug set."""

    return frozenset(m.slug for m in mappings)


# ---------------------------------------------------------------------------
# Property 1 — disjointness
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(scanned=_scanned_repos(), current=_current_mappings())
def test_partitions_are_pairwise_disjoint(
    scanned: frozenset[str],
    current: tuple[RepoMapping, ...],
) -> None:
    """**Validates: Requirements 10.7**

    For every input the three partitions ``added``, ``removed``, and
    ``unchanged`` are pairwise disjoint. Any overlap would mean a
    slug is reported as both "to add" and "already there" (or
    similar contradiction), corrupting the admin's decision input.
    """

    diff = compute_repo_mapping_diff(scanned, current)

    assert diff.added.isdisjoint(diff.removed), (
        "added ∩ removed must be empty; got "
        f"added={sorted(diff.added)!r} removed={sorted(diff.removed)!r}"
    )
    assert diff.added.isdisjoint(diff.unchanged), (
        "added ∩ unchanged must be empty; got "
        f"added={sorted(diff.added)!r} unchanged={sorted(diff.unchanged)!r}"
    )
    assert diff.removed.isdisjoint(diff.unchanged), (
        "removed ∩ unchanged must be empty; got "
        f"removed={sorted(diff.removed)!r} unchanged={sorted(diff.unchanged)!r}"
    )


# ---------------------------------------------------------------------------
# Property 2a — reconstruction of ``scanned_repos``
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(scanned=_scanned_repos(), current=_current_mappings())
def test_added_union_unchanged_equals_scanned(
    scanned: frozenset[str],
    current: tuple[RepoMapping, ...],
) -> None:
    """**Validates: Requirements 10.7**

    ``added ∪ unchanged == scanned_repos``. Equivalently: every slug
    the Bitbucket scan reported lands in exactly one of the two
    partitions; nothing gets dropped on the floor.
    """

    diff = compute_repo_mapping_diff(scanned, current)
    assert diff.added | diff.unchanged == scanned, (
        f"added ∪ unchanged must equal scanned; got "
        f"added={sorted(diff.added)!r} unchanged={sorted(diff.unchanged)!r} "
        f"scanned={sorted(scanned)!r}"
    )


# ---------------------------------------------------------------------------
# Property 2b — reconstruction of ``current_mappings`` slug set
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(scanned=_scanned_repos(), current=_current_mappings())
def test_removed_union_unchanged_equals_current_slugs(
    scanned: frozenset[str],
    current: tuple[RepoMapping, ...],
) -> None:
    """**Validates: Requirements 10.7**

    ``removed ∪ unchanged == {m.slug for m in current_mappings}``.
    Every slug present in the dept's current ``departments.json``
    array lands in exactly one of the two partitions; nothing is
    silently dropped from the operator's view.
    """

    diff = compute_repo_mapping_diff(scanned, current)
    current_slugs = _slug_set(current)
    assert diff.removed | diff.unchanged == current_slugs, (
        f"removed ∪ unchanged must equal current slug set; got "
        f"removed={sorted(diff.removed)!r} "
        f"unchanged={sorted(diff.unchanged)!r} "
        f"current_slugs={sorted(current_slugs)!r}"
    )


# ---------------------------------------------------------------------------
# Property 3 — idempotence on equal inputs
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(current=_current_mappings())
def test_idempotent_when_scanned_equals_current(
    current: tuple[RepoMapping, ...],
) -> None:
    """**Validates: Requirements 10.7**

    When the Bitbucket scan returns exactly the dept's current slug
    set, the diff is a no-op: ``added`` and ``removed`` are both
    empty and ``unchanged`` equals the shared set. This is the
    precondition for the apply-mode contract "running sync twice in
    a row over an unchanged workspace must be a no-op the second
    time" (the second invocation finds added/removed empty so
    ``update_repo_mappings`` is called with the same list it
    already holds).
    """

    current_slugs = _slug_set(current)
    diff = compute_repo_mapping_diff(current_slugs, current)

    assert diff.added == frozenset(), (
        f"added must be empty when scanned == current_slugs; "
        f"got added={sorted(diff.added)!r}"
    )
    assert diff.removed == frozenset(), (
        f"removed must be empty when scanned == current_slugs; "
        f"got removed={sorted(diff.removed)!r}"
    )
    assert diff.unchanged == current_slugs, (
        f"unchanged must equal the shared slug set; "
        f"got unchanged={sorted(diff.unchanged)!r} "
        f"current_slugs={sorted(current_slugs)!r}"
    )


# ---------------------------------------------------------------------------
# Property 4 — determinism (replay safety)
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(scanned=_scanned_repos(), current=_current_mappings())
def test_deterministic_two_invocations(
    scanned: frozenset[str],
    current: tuple[RepoMapping, ...],
) -> None:
    """**Validates: Requirements 10.7**

    Two invocations with identical inputs return equal
    :class:`RepoMappingDiff` instances. Equivalent to "the helper is
    pure" — no clock, no random, no global state. Replay-safety for
    any future caller that wants to schedule the auto-sync as a
    Temporal cron workflow follows from this.
    """

    first = compute_repo_mapping_diff(scanned, current)
    second = compute_repo_mapping_diff(scanned, current)
    assert first == second, (
        f"helper must be deterministic; got first={first!r} second={second!r}"
    )


# ---------------------------------------------------------------------------
# Property 5 — empty edge cases
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(scanned=_scanned_repos())
def test_empty_current_mappings_means_everything_is_added(
    scanned: frozenset[str],
) -> None:
    """**Validates: Requirements 10.7**

    First-time sync (no current mappings, full scan): every scanned
    slug is ``added``; ``removed`` and ``unchanged`` are empty.
    """

    diff = compute_repo_mapping_diff(scanned, current_mappings=())
    assert diff.added == scanned
    assert diff.removed == frozenset()
    assert diff.unchanged == frozenset()


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(current=_current_mappings())
def test_empty_scan_means_everything_is_removed(
    current: tuple[RepoMapping, ...],
) -> None:
    """**Validates: Requirements 10.7**

    Bitbucket workspace empty (or unreachable for the moment): every
    current slug is ``removed``; ``added`` and ``unchanged`` are
    empty. (The endpoint's apply mode would prune all mappings —
    operator review of the dry-run output is what stops a
    catastrophic prune from happening automatically.)
    """

    diff = compute_repo_mapping_diff(frozenset(), current)
    assert diff.added == frozenset()
    assert diff.removed == _slug_set(current)
    assert diff.unchanged == frozenset()


def test_both_empty_yields_all_empty_partitions() -> None:
    """**Validates: Requirements 10.7**

    Boundary: empty scan and empty current mappings produce three
    empty partitions. Pinned as a non-Hypothesis test so the
    counterexample shrinker cannot waste cycles re-deriving this
    case.
    """

    diff = compute_repo_mapping_diff(frozenset(), current_mappings=())
    assert diff == RepoMappingDiff(
        added=frozenset(),
        removed=frozenset(),
        unchanged=frozenset(),
    )


# ---------------------------------------------------------------------------
# Property 6 — duplicate slugs in current_mappings collapse to one
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(slug=_SLUG, name1=_NAME, name2=_NAME, scanned=_scanned_repos())
def test_duplicate_current_mappings_collapse(
    slug: str,
    name1: str,
    name2: str,
    scanned: frozenset[str],
) -> None:
    """**Validates: Requirements 10.7**

    When ``current_mappings`` contains two entries with the same
    slug (different names), the helper folds them into a single
    slug in the underlying set so the diff partitions never
    double-count. This matches the wider system contract where
    ``departments.schema.json`` rejects duplicate ``repo_mappings``
    entries at validation time, but the helper itself stays
    composable with any input.
    """

    duplicates = (
        RepoMapping(name=name1, slug=slug),
        RepoMapping(name=name2, slug=slug),
    )
    diff = compute_repo_mapping_diff(scanned, duplicates)

    # Every slug in any of the partitions must come from the union
    # of ``scanned`` and ``{slug}`` — and ``slug`` itself must
    # appear in exactly one partition.
    assert diff.added | diff.removed | diff.unchanged <= scanned | {slug}
    if slug in scanned:
        assert slug in diff.unchanged
        assert slug not in diff.added
        assert slug not in diff.removed
    else:
        assert slug in diff.removed
        assert slug not in diff.added
        assert slug not in diff.unchanged
