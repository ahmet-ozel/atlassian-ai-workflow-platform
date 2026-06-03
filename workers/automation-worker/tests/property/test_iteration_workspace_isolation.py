r"""Invariant test: Iteration workspace isolation.

Feature:, — *For any* iteration ``N`` and
``N+1`` on the same Jira issue, their workspace paths SHALL be
distinct and SHALL NOT share mutable state. The:func:`build_iteration_workspace_path` helper is the single source of
truth for the canonical ``{base}/{issue_key}/iter-{N}`` layout used by
both ``automation-worker`` (which records the path in
``shared.workflow_iterations``) and ``execution-runner-worker`` (which
provisions the workspace on the SSH host); if it ever returns the same
string for two different iterations, two re-runs would race for the
same on-disk directory and bleed state across iterations.

Generators
----------

* ``_BASE_PATH_STRATEGY`` — a small handful of representative root
 paths (``/var/ai-runner``, ``/runner``, ``/var/ai-runner///``
 trailing-slash variant). The implementation strips trailing slashes
 so equivalent bases must collapse to the same canonical prefix.
* ``_ISSUE_KEY_STRATEGY`` — Jira-style keys matching the
 ``^[A-Z][A-Z0-9_]*-\d+$`` validator the helper enforces (covers
 classic ``PAY-4211`` and underscored ``OPS_CORE-12`` shapes).
* ``_ITER_STRATEGY`` — integers in ``[1, MAX_ITERATION_NUMBER]``,
 the inclusive bound the helper accepts.

Properties checked
------------------

1. **Pairwise distinct paths.** For any two iteration numbers ``N``
 and ``M`` with ``N != M`` (both within
 ``[1, MAX_ITERATION_NUMBER]``) and the same ``base`` + ``issue_key``,
 the resulting paths are distinct strings.
2. **Consecutive iterations are non-prefixed.** For any ``N`` in
 ``[1, MAX_ITERATION_NUMBER - 1]``, ``path(N)`` and ``path(N+1)``
 are distinct *and* neither is a string prefix of the other. This
 forbids accidental collisions like ``.../iter-1`` being a prefix
 of ``.../iter-10`` from creeping in for *consecutive* iterations
 — a subtle directory-tree foot-gun the design explicitly calls
 out for the iter-N → iter-N+1 hand-off.

These two properties together guarantee: for the
consecutive case the paths cannot even share a filesystem prefix, and
for the general case they cannot share an exact path — so two
iterations can never accidentally write to the same workspace.

**"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings, strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap — same shape as sibling Invariant tests so the test
# is importable without an editable install of the worker package.
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from automation_worker.activities.iteration_manager import (  # noqa: E402
    MAX_ITERATION_NUMBER,
    build_iteration_workspace_path,
)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


# Representative bases including the trailing-slash variant the helper
# is documented to canonicalise.
_BASE_PATH_STRATEGY: st.SearchStrategy[str] = st.sampled_from(
    [
        "/var/ai-runner",
        "/var/ai-runner/",
        "/var/ai-runner///",
        "/runner",
        "/srv/workspaces",
    ]
)

# Issue keys that satisfy ``^[A-Z][A-Z0-9_]*-\d+$``. The numeric
# component is constrained to ``[1, 99999]`` to keep generated examples
# readable; that is wider than any realistic Jira project counter and
# still exercises both single- and multi-digit issue numbers.
_ISSUE_KEY_STRATEGY: st.SearchStrategy[str] = st.from_regex(
    r"[A-Z][A-Z0-9_]{0,7}-[1-9][0-9]{0,4}",
    fullmatch=True,
)

# Iteration numbers across the *full* documented domain so the property
# is exercised at the boundaries (``1`` and ``MAX_ITERATION_NUMBER``)
# as well as everywhere in between.
_ITER_STRATEGY: st.SearchStrategy[int] = st.integers(
    min_value=1,
    max_value=MAX_ITERATION_NUMBER,
)


@st.composite
def _two_distinct_iterations(draw: st.DrawFn) -> tuple[int, int]:
    """Draw two iteration numbers ``(N, M)`` with ``N != M``.

 Both lie in ``[1, MAX_ITERATION_NUMBER]`` — the full domain
 accepted by:func:`build_iteration_workspace_path`. The shrinker
 naturally pushes towards small adjacent integers (e.g. ``(1, 2)``)
 which produces the most informative counter-examples.
 """
    n = draw(_ITER_STRATEGY)
    m = draw(_ITER_STRATEGY.filter(lambda x: x != n))
    return n, m


# A separate strategy for the consecutive-pair case: we draw ``N`` from
# ``[1, MAX_ITERATION_NUMBER - 1]`` and pair it with ``N + 1`` so the
# generator only ever yields valid consecutive iterations (the helper
# rejects ``MAX_ITERATION_NUMBER + 1`` so we must stop one short).
_CONSECUTIVE_ITER_STRATEGY: st.SearchStrategy[int] = st.integers(
    min_value=1,
    max_value=MAX_ITERATION_NUMBER - 1,
)


# ---------------------------------------------------------------------------
# — pairwise distinctness for arbitrary iteration pairs
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(
    base=_BASE_PATH_STRATEGY,
    issue_key=_ISSUE_KEY_STRATEGY,
    iters=_two_distinct_iterations(),
)
def test_distinct_iterations_yield_distinct_paths(
    base: str,
    issue_key: str,
    iters: tuple[int, int],
) -> None:
    """.

 For any two iteration numbers N != M (both in
 ``[1, MAX_ITERATION_NUMBER]``) the produced workspace paths must
 be different strings — i.e.
 ``build_iteration_workspace_path`` is injective over the iteration
 component when ``base`` and ``issue_key`` are held fixed.
 """
    n, m = iters
    path_n = build_iteration_workspace_path(base, issue_key, n)
    path_m = build_iteration_workspace_path(base, issue_key, m)

    assert path_n != path_m, (
        f"Workspace paths collided for distinct iterations "
        f"N={n} and M={m} (issue_key={issue_key!r}, base={base!r}): "
        f"both rendered as {path_n!r}"
    )


# ---------------------------------------------------------------------------
# — consecutive iterations are mutually non-prefixed
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(
    base=_BASE_PATH_STRATEGY,
    issue_key=_ISSUE_KEY_STRATEGY,
    n=_CONSECUTIVE_ITER_STRATEGY,
)
def test_consecutive_iterations_are_mutually_non_prefixed(
    base: str,
    issue_key: str,
    n: int,
) -> None:
    """(consecutive iter-N → iter-N+1).

 For any iteration ``N`` with a valid successor ``N+1``, the two
 rendered paths must:

 * differ as strings (the basic injectivity check), and
 * neither be a string prefix of the other — so ``iter-N`` and
 ``iter-N+1`` cannot accidentally land on the same filesystem
 sub-tree (the way ``iter-1`` would otherwise prefix
 ``iter-10`` for *non-consecutive* pairs).

 Together with the pairwise-distinctness property above this is
 the strict reading of: consecutive iterations cannot
 share *any* state, exact path or otherwise.
 """
    path_n = build_iteration_workspace_path(base, issue_key, n)
    path_next = build_iteration_workspace_path(base, issue_key, n + 1)

    assert path_n != path_next, (
        f"Consecutive iterations N={n} and N+1={n + 1} produced the "
        f"same workspace path {path_n!r} "
        f"(issue_key={issue_key!r}, base={base!r})"
    )

    assert not path_n.startswith(path_next), (
        f"Workspace path for iter-{n} ({path_n!r}) is a string "
        f"prefix of iter-{n + 1} ({path_next!r}) — consecutive "
        f"iterations must not share a filesystem prefix "
        f"(issue_key={issue_key!r}, base={base!r})"
    )
    assert not path_next.startswith(path_n), (
        f"Workspace path for iter-{n + 1} ({path_next!r}) is a "
        f"string prefix of iter-{n} ({path_n!r}) — consecutive "
        f"iterations must not share a filesystem prefix "
        f"(issue_key={issue_key!r}, base={base!r})"
    )
