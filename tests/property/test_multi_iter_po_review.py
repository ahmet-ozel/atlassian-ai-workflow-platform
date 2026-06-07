"""invariant for invariant - Multi-iter / PO Review invariants.

This file pins the four invariants of ````
**invariant**:

1.:func:`temporal_shared.po_review.compute_orphan_branches` -
 deterministic, idempotent set-algebra filter for the
 ``GET /api/orphan-branches`` endpoint /).
2.:func:`temporal_shared.po_review.compute_po_review_inbox` -
 deterministic filter for the ``GET /api/po-review-inbox``
 endpoint /).
3.:func:`automation_worker.workflows.bot_branch_retention.should_delete_branch`
 - pure predicate for the daily ``ai/*`` branch retention cron
 /).
4. The pure idempotency guards inside:func:`agent_runner.activities.iter_advance.iter_advance_pr_supersede`
 -:func:`_build_banner` is a deterministic formatter and:func:`_description_already_banners` is a pure idempotency check
 that prevents a doubly-prefixed description on retry /).



Invariant statements (design.md §"invariant")
---------------------------------------------

* **PR supersede idempotency ** - re-running the iter-advance
 banner machinery on an already-superseded PR description is a
 no-op:

 1. ``_build_banner(new_pr_id)`` is a pure formatter - same input
 always returns the same string.
 2. ``_description_already_banners(_build_banner(n) + body, n)``
 is always ``True`` (the prepend-once guard).
 3. Prepending the banner to the same description twice via the
 guard yields the same single-banner result (idempotent).

* **Branch retention predicate ** -:func:`should_delete_branch` returns ``True`` iff the branch age
 exceeds 30 days **and** the linked Jira issue status is in
 ``{Done, Closed}`` (case-insensitive). Concretely:

 1. ``age <= 30 days``  ``False`` (boundary kept - strict
 inequality).
 2. ``status ∉ {Done, Closed}`` (case-folded)  ``False``.
 3. Negative / zero age  ``False`` (clock-skew guard).
 4. Non-string ``status``  ``False``.
 5. Deterministic - same input always returns the same bool.

* **Orphan-branch filter ** -:func:`compute_orphan_branches`
 is a deterministic, idempotent set-algebra filter over the
 caller-provided ``branches`` and ``prs`` iterables. Concretely:

 1. Every returned branch's name starts with ``"ai/"``.
 2. No returned branch's name is the ``source_branch`` of any PR in
 the input.
 3. The result is a subset of the input branches.
 4. Two calls with the same input return equal:class:`frozenset`
 results.
 5. Feeding the result back as ``branches`` returns the same set
 (idempotency).
 6. PR draftness / author / merge state never affects the decision -
 any PR claims its source branch.

* **PO Review Inbox filter ** -:func:`compute_po_review_inbox`
 is a deterministic filter over the caller-provided ``prs`` iterable
 and ``bot_ids`` set. Concretely:

 1. Every returned PR is a draft (``is_draft is True``).
 2. Every returned PR's author is in ``bot_ids``.
 3. The result is a subset of the input PRs.
 4. Two calls with the same input return equal:class:`frozenset`
 results.
 5. Empty ``bot_ids`` always yields the empty set.
 6. Idempotent - feeding the result back returns the same set.

Hypothesis is used to drive every property across hundreds of
``(branches, prs, bot_ids, age, status, pr_ids, descriptions)`` shapes.
The example-based tests at the bottom of each section pin the canonical
cases so a doctest-style assertion remains in the suite even when
Hypothesis examples are minimised.

Replay-safe / dep-free
----------------------

The:mod:`temporal_shared.po_review` helpers are pure (no clock, no
I/O, no globals) so they can be imported directly via ``pytest.ini``'s
``pythonpath`` setting. The ``should_delete_branch`` predicate and the
``_build_banner`` / ``_description_already_banners`` helpers live under
``platform/workers/*/src`` which is **not** on the workspace pythonpath,
so the test file appends the worker source roots to ``sys.path`` at
import time (mirrors the bootstrap pattern in ``test_llm_dedup.py`` /
``test_temporal_loop_cap.py``).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap - expose the worker source trees so the predicate
# and pure helpers can be imported without pip-installing each worker
# package. Mirrors ``test_llm_dedup.py`` / ``test_temporal_loop_cap.py``.
# ---------------------------------------------------------------------------

# tests/property/test_multi_iter_po_review.py  platform/
_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_REQUIRED_SRC_DIRS: Final[tuple[Path, ...]] = (
    _PLATFORM_ROOT / "workers" / "automation-worker" / "src",
    _PLATFORM_ROOT / "workers" / "agent-runner-worker" / "src",
)
for _src in _REQUIRED_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


# noqa: E402 - imports must follow the sys.path bootstrap above.

from temporal_shared.po_review import (  # noqa: E402
    AI_BRANCH_PREFIX,
    Branch,
    PullRequest,
    compute_orphan_branches,
    compute_po_review_inbox,
)

from automation_worker.workflows.bot_branch_retention import (  # noqa: E402
    BRANCH_RETENTION_DAYS,
    CLOSED_JIRA_STATUSES,
    should_delete_branch,
)

from agent_runner.activities.iter_advance import (  # noqa: E402
    BANNER_PREFIX_TEMPLATE,
    SUPERSEDE_LABEL_TEMPLATE,
    _build_banner,
    _description_already_banners,
)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------
#
# The strategies below intentionally constrain to small alphabets and
# bounded sizes so Hypothesis shrinks counterexamples quickly and the
# invariant finishes inside the per-property deadline. The
# helpers themselves do not constrain string content, so the alphabet
# choice is purely a performance optimisation.

# Issue keys mirror Jira's canonical format: PROJECT-NNN. Bounded to
# a small set of distinct keys so the orphan-branch decision actually
# exercises the "claimed vs orphan" branch (otherwise every example
# would be trivially orphan).
_ISSUE_KEYS: Final = st.sampled_from(
    [f"PAY-{i}" for i in range(1, 6)]
    + [f"OPS-{i}" for i in range(1, 6)]
    + [f"WEB-{i}" for i in range(1, 6)]
)

# Branches come in two flavours: bot-authored (``ai/{issue_key}``) and
# manual (any non-``ai/`` prefix). We sample both from the same
# strategy so the orphan filter sees a realistic mixture and the
# "name does not start with ai/" branch is exercised.
_AI_BRANCH_NAMES = _ISSUE_KEYS.map(lambda k: f"{AI_BRANCH_PREFIX}{k}")
_AI_ITER_BRANCH_NAMES = st.tuples(
    _ISSUE_KEYS, st.integers(min_value=2, max_value=5)
).map(lambda t: f"{AI_BRANCH_PREFIX}{t[0]}-iter{t[1]}")

_MANUAL_BRANCH_NAMES = st.sampled_from(
    [
        "main",
        "master",
        "develop",
        "feature/login",
        "feature/payments",
        "hotfix/urgent",
        "release/2026.05",
        "bugfix/typo",
    ]
)

_BRANCH_NAMES = st.one_of(
    _AI_BRANCH_NAMES,
    _AI_ITER_BRANCH_NAMES,
    _MANUAL_BRANCH_NAMES,
)


def _branch_strategy() -> st.SearchStrategy[Branch]:
    """Generate:class:`Branch` values with optional ``last_commit_at``."""
    base_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return st.builds(
        Branch,
        name=_BRANCH_NAMES,
        last_commit_at=st.one_of(
            st.none(),
            st.integers(min_value=0, max_value=365).map(
                lambda d: base_dt + timedelta(days=d)
            ),
        ),
    )


# Account ids - small set so bot-id membership is non-trivial.
_ACCOUNT_IDS: Final = st.sampled_from(
    [f"acct-{i}" for i in range(1, 11)]
)

_PR_IDS = st.integers(min_value=1, max_value=10_000)


def _pull_request_strategy() -> st.SearchStrategy[PullRequest]:
    """Generate:class:`PullRequest` values across the full input space."""
    return st.builds(
        PullRequest,
        id=_PR_IDS,
        source_branch=_BRANCH_NAMES,
        is_draft=st.booleans(),
        author_account_id=_ACCOUNT_IDS,
        title=st.text(max_size=20),
    )


def _bot_ids_strategy() -> st.SearchStrategy[frozenset[str]]:
    """Generate the bot account-id set (may be empty)."""
    return st.sets(_ACCOUNT_IDS, min_size=0, max_size=5).map(frozenset)


# Bound the iterables so Hypothesis examples shrink quickly while
# still exercising the "many branches, many PRs" path.
_BRANCH_LISTS = st.lists(_branch_strategy(), max_size=20)
_PR_LISTS = st.lists(_pull_request_strategy(), max_size=20)


# ---------------------------------------------------------------------------
# compute_orphan_branches - set-algebra invariants
# ---------------------------------------------------------------------------


class TestComputeOrphanBranchesProperties:
    """invariant-based invariants for ``compute_orphan_branches``.


 """

    @given(branches=_BRANCH_LISTS, prs=_PR_LISTS)
    @settings(max_examples=200, deadline=None)
    def test_every_orphan_starts_with_ai_prefix(
        self, branches: list[Branch], prs: list[PullRequest]
    ) -> None:
        """Every returned branch's name starts with ``"ai/"``.


 """
        result = compute_orphan_branches(branches, prs)
        assert all(b.name.startswith(AI_BRANCH_PREFIX) for b in result)

    @given(branches=_BRANCH_LISTS, prs=_PR_LISTS)
    @settings(max_examples=200, deadline=None)
    def test_no_orphan_is_a_pr_source_branch(
        self, branches: list[Branch], prs: list[PullRequest]
    ) -> None:
        """No returned branch matches any PR's ``source_branch``.


 """
        result = compute_orphan_branches(branches, prs)
        pr_sources = {pr.source_branch for pr in prs}
        assert all(b.name not in pr_sources for b in result)

    @given(branches=_BRANCH_LISTS, prs=_PR_LISTS)
    @settings(max_examples=200, deadline=None)
    def test_result_is_subset_of_input_branches(
        self, branches: list[Branch], prs: list[PullRequest]
    ) -> None:
        """The returned set is a subset of the input branches.

 No branch is ever synthesised by the helper - every result
 element comes from the input ``branches`` iterable.


 """
        result = compute_orphan_branches(branches, prs)
        assert result <= frozenset(branches)

    @given(branches=_BRANCH_LISTS, prs=_PR_LISTS)
    @settings(max_examples=200, deadline=None)
    def test_deterministic(
        self, branches: list[Branch], prs: list[PullRequest]
    ) -> None:
        """Two calls with the same input return equal:class:`frozenset`.


 """
        first = compute_orphan_branches(branches, prs)
        second = compute_orphan_branches(branches, prs)
        assert first == second
        # The result type is:class:`frozenset` - pinned so the API
        # endpoint can rely on hashability.
        assert isinstance(first, frozenset)

    @given(branches=_BRANCH_LISTS, prs=_PR_LISTS)
    @settings(max_examples=200, deadline=None)
    def test_idempotent_when_fed_back(
        self, branches: list[Branch], prs: list[PullRequest]
    ) -> None:
        """Feeding the orphan set back as ``branches`` yields the same set.

 Once a branch is determined to be orphan, re-running the helper
 on the orphan subset (with the same PR list) must return the
 same set - the helper does not have any state that could flip
 a branch from orphan to non-orphan on a second pass.


 """
        first = compute_orphan_branches(branches, prs)
        second = compute_orphan_branches(first, prs)
        assert first == second

    @given(branches=_BRANCH_LISTS, prs=_PR_LISTS)
    @settings(max_examples=200, deadline=None)
    def test_pr_draftness_and_author_do_not_affect_orphan_decision(
        self, branches: list[Branch], prs: list[PullRequest]
    ) -> None:
        """Any PR (draft or not, bot or human) claims its source branch.:func:`compute_orphan_branches` only consults:attr:`PullRequest.source_branch` - flipping any other field
 on every PR must not change the orphan set.


 """
        baseline = compute_orphan_branches(branches, prs)
        flipped_prs = [
            PullRequest(
                id=pr.id,
                source_branch=pr.source_branch,
                is_draft=not pr.is_draft,
                # Rotate author to a deterministic alternative so the
                # "different author" axis is exercised.
                author_account_id=f"x-{pr.author_account_id}",
                title=pr.title,
            )
            for pr in prs
        ]
        flipped = compute_orphan_branches(branches, flipped_prs)
        assert baseline == flipped


# ---------------------------------------------------------------------------
# compute_po_review_inbox - filter invariants
# ---------------------------------------------------------------------------


class TestComputePoReviewInboxProperties:
    """invariant-based invariants for ``compute_po_review_inbox``.


 """

    @given(prs=_PR_LISTS, bot_ids=_bot_ids_strategy())
    @settings(max_examples=200, deadline=None)
    def test_every_result_is_a_draft(
        self, prs: list[PullRequest], bot_ids: frozenset[str]
    ) -> None:
        """Every returned PR has ``is_draft=True``.


 """
        result = compute_po_review_inbox(prs, bot_ids)
        assert all(pr.is_draft for pr in result)

    @given(prs=_PR_LISTS, bot_ids=_bot_ids_strategy())
    @settings(max_examples=200, deadline=None)
    def test_every_result_author_is_in_bot_ids(
        self, prs: list[PullRequest], bot_ids: frozenset[str]
    ) -> None:
        """Every returned PR's author is in ``bot_ids``.


 """
        result = compute_po_review_inbox(prs, bot_ids)
        assert all(pr.author_account_id in bot_ids for pr in result)

    @given(prs=_PR_LISTS, bot_ids=_bot_ids_strategy())
    @settings(max_examples=200, deadline=None)
    def test_result_is_subset_of_input_prs(
        self, prs: list[PullRequest], bot_ids: frozenset[str]
    ) -> None:
        """The returned set is a subset of the input PRs.


 """
        result = compute_po_review_inbox(prs, bot_ids)
        assert result <= frozenset(prs)

    @given(prs=_PR_LISTS, bot_ids=_bot_ids_strategy())
    @settings(max_examples=200, deadline=None)
    def test_deterministic(
        self, prs: list[PullRequest], bot_ids: frozenset[str]
    ) -> None:
        """Two calls with the same input return equal:class:`frozenset`.


 """
        first = compute_po_review_inbox(prs, bot_ids)
        second = compute_po_review_inbox(prs, bot_ids)
        assert first == second
        assert isinstance(first, frozenset)

    @given(prs=_PR_LISTS)
    @settings(max_examples=100, deadline=None)
    def test_empty_bot_ids_yields_empty_result(
        self, prs: list[PullRequest]
    ) -> None:
        """An empty ``bot_ids`` set always yields the empty inbox.

 Whatever drafts the PR list contains, none of them can be in
 the inbox if no bot accounts are configured.


 """
        result = compute_po_review_inbox(prs, frozenset())
        assert result == frozenset()

    @given(prs=_PR_LISTS, bot_ids=_bot_ids_strategy())
    @settings(max_examples=200, deadline=None)
    def test_idempotent_when_fed_back(
        self, prs: list[PullRequest], bot_ids: frozenset[str]
    ) -> None:
        """Feeding the inbox back as ``prs`` yields the same inbox.

 Every PR in the inbox is, by construction, a draft authored
 by a bot - so a second filter pass must be a no-op.


 """
        first = compute_po_review_inbox(prs, bot_ids)
        second = compute_po_review_inbox(first, bot_ids)
        assert first == second


# ---------------------------------------------------------------------------
# Canonical example-based cases (pinned by the task description)
# ---------------------------------------------------------------------------


class TestComputeOrphanBranchesExamples:
    """Concrete examples for the orphan-branch helper.


 """

    def test_empty_inputs_yields_empty_result(self) -> None:
        """No branches  no orphans, regardless of PR list.


 """
        assert compute_orphan_branches([], []) == frozenset()
        assert compute_orphan_branches(
            [],
            [
                PullRequest(
                    id=1,
                    source_branch="ai/PAY-1",
                    is_draft=True,
                    author_account_id="bot-acct",
                )
            ],
        ) == frozenset()

    def test_all_orphan_when_no_prs(self) -> None:
        """Empty PR list  every ``ai/*`` branch is orphan; manual ones excluded.


 """
        ai_one = Branch(name="ai/PAY-1")
        ai_two = Branch(name="ai/PAY-2-iter3")
        manual = Branch(name="feature/manual")
        result = compute_orphan_branches([ai_one, ai_two, manual], [])
        assert result == frozenset({ai_one, ai_two})

    def test_all_merged_when_every_branch_has_pr(self) -> None:
        """Every ``ai/*`` branch claimed by a PR  empty orphan set.


 """
        ai_one = Branch(name="ai/PAY-1")
        ai_two = Branch(name="ai/PAY-2")
        prs = [
            PullRequest(
                id=10,
                source_branch="ai/PAY-1",
                is_draft=True,
                author_account_id="bot-acct",
            ),
            PullRequest(
                id=11,
                source_branch="ai/PAY-2",
                is_draft=False,
                author_account_id="human-1",
            ),
        ]
        assert compute_orphan_branches([ai_one, ai_two], prs) == frozenset()

    def test_manual_branch_never_orphan_even_without_pr(self) -> None:
        """Branches without the ``ai/`` prefix are never returned.

 Manual branches (``main``, ``feature/*``, etc.) live outside
 the bot-authored namespace and the Orphan Branches API never
 surfaces them, even when no PR claims them.


 """
        manual = Branch(name="feature/manual")
        assert compute_orphan_branches([manual], []) == frozenset()

    def test_iter_variant_branch_is_recognised_as_ai(self) -> None:
        """``ai/PAY-1-iter3`` matches the ``ai/`` prefix and is orphan-eligible.


 """
        iter_branch = Branch(name="ai/PAY-1-iter3")
        assert compute_orphan_branches([iter_branch], []) == frozenset(
            {iter_branch}
        )


class TestComputePoReviewInboxExamples:
    """Concrete examples for the PO review inbox helper.


 """

    def test_empty_inputs_yields_empty_result(self) -> None:
        """No PRs  empty inbox regardless of bot id set.


 """
        assert compute_po_review_inbox([], frozenset()) == frozenset()
        assert compute_po_review_inbox(
            [], frozenset({"bot-acct"})
        ) == frozenset()

    def test_only_draft_bot_prs_make_it_in(self) -> None:
        """Filter keeps drafts authored by a bot; drops the rest.


 """
        bot = "bot-acct"
        human = "human-1"
        draft_bot = PullRequest(
            id=1, source_branch="ai/PAY-1", is_draft=True, author_account_id=bot
        )
        open_bot = PullRequest(
            id=2,
            source_branch="ai/PAY-2",
            is_draft=False,
            author_account_id=bot,
        )
        draft_human = PullRequest(
            id=3,
            source_branch="feature/x",
            is_draft=True,
            author_account_id=human,
        )
        result = compute_po_review_inbox(
            [draft_bot, open_bot, draft_human], frozenset({bot})
        )
        assert result == frozenset({draft_bot})

    def test_multiple_bots_all_included(self) -> None:
        """Drafts from any account in ``bot_ids`` are surfaced.


 """
        bot_a = "bot-a"
        bot_b = "bot-b"
        draft_a = PullRequest(
            id=1, source_branch="ai/PAY-1", is_draft=True, author_account_id=bot_a
        )
        draft_b = PullRequest(
            id=2, source_branch="ai/OPS-1", is_draft=True, author_account_id=bot_b
        )
        result = compute_po_review_inbox(
            [draft_a, draft_b], frozenset({bot_a, bot_b})
        )
        assert result == frozenset({draft_a, draft_b})

    def test_open_bot_prs_excluded_even_when_author_is_in_bot_ids(self) -> None:
        """Non-draft (open) PRs never appear in the inbox.

 Bitbucket may flip a PR from draft to open if a human takes
 ownership; from that moment the platform should treat the PR
 as a normal review, not as a bot-prepared draft.


 """
        bot = "bot-acct"
        open_pr = PullRequest(
            id=1,
            source_branch="ai/PAY-1",
            is_draft=False,
            author_account_id=bot,
        )
        assert compute_po_review_inbox([open_pr], frozenset({bot})) == frozenset()


# ---------------------------------------------------------------------------
# should_delete_branch - pure predicate invariants
# ---------------------------------------------------------------------------
#
# The retention predicate sits on the daily ``BotBranchRetention`` cron
# and gates the deletion of stale ``ai/*`` branches. Both
# conditions must hold (age > 30 days **and** issue closed) and the
# predicate is pure so it is replay-safe inside the workflow body.

# Branch ages - bounded so Hypothesis shrinks counterexamples quickly
# but still exercise both sides of the boundary (≤ 30 days vs > 30
# days) and the negative-age clock-skew guard.
_AGE_DAYS = st.integers(min_value=-5, max_value=120)


def _age_strategy() -> st.SearchStrategy[timedelta]:
    """Hypothesis strategy for branch ages around the 30-day boundary."""
    return _AGE_DAYS.map(lambda d: timedelta(days=d))


# Status alphabet covers (a) the canonical closed values, (b) common
# casing variants, (c) typical open statuses, (d) the empty string.
# The case variants exercise the case-fold comparison; the open
# statuses exercise the False branch.
_CLOSED_STATUS_VARIANTS: Final[tuple[str, ...]] = (
    "Done",
    "Closed",
    "DONE",
    "CLOSED",
    "done",
    "closed",
    "DoNe",
    "cLoSeD",
)
_OPEN_STATUS_VARIANTS: Final[tuple[str, ...]] = (
    "To Do",
    "In Progress",
    "Blocked",
    "In Review",
    "Open",
    "Reopened",
    "Backlog",
    "",
)
_STATUS_STRATEGY = st.one_of(
    st.sampled_from(_CLOSED_STATUS_VARIANTS),
    st.sampled_from(_OPEN_STATUS_VARIANTS),
)


class TestShouldDeleteBranchProperties:
    """invariant-based invariants for ``should_delete_branch``.


 """

    @given(age=_age_strategy(), status=_STATUS_STRATEGY)
    @settings(max_examples=200, deadline=None)
    def test_returns_bool(self, age: timedelta, status: str) -> None:
        """The predicate always returns a plain:class:`bool`.

 The cron workflow uses the result as a boolean guard around
 the ``delete_ai_branch`` activity; returning ``None`` or any
 truthy non-bool would silently mis-trigger deletion.


 """
        result = should_delete_branch(age, status)
        assert isinstance(result, bool)

    @given(age=_age_strategy(), status=_STATUS_STRATEGY)
    @settings(max_examples=200, deadline=None)
    def test_deterministic(self, age: timedelta, status: str) -> None:
        """Two calls with the same input return the same result.

 The predicate has no clock, no I/O, no globals - replay-safe.


 """
        first = should_delete_branch(age, status)
        second = should_delete_branch(age, status)
        assert first == second

    @given(
        age=st.integers(min_value=0, max_value=BRANCH_RETENTION_DAYS).map(
            lambda d: timedelta(days=d)
        ),
        status=_STATUS_STRATEGY,
    )
    @settings(max_examples=200, deadline=None)
    def test_age_at_or_below_boundary_is_kept(
        self, age: timedelta, status: str
    ) -> None:
        """``age <= 30 days``  ``False`` regardless of issue status.

 The boundary uses *strict* inequality - a branch whose age is
 exactly 30 days survives the current cron tick and is
 reconsidered tomorrow. This mirrors the the operational rule language
 ("30 günden eski" excludes the boundary itself).


 """
        assert should_delete_branch(age, status) is False

    @given(
        age=st.integers(
            min_value=BRANCH_RETENTION_DAYS + 1, max_value=365
        ).map(lambda d: timedelta(days=d)),
        status=st.sampled_from(_OPEN_STATUS_VARIANTS),
    )
    @settings(max_examples=200, deadline=None)
    def test_open_status_is_kept_regardless_of_age(
        self, age: timedelta, status: str
    ) -> None:
        """Status ∉ {Done, Closed}  ``False`` regardless of age.

 An aged branch on an open issue is kept so the bot does not
 accidentally remove ongoing work.


 """
        assert should_delete_branch(age, status) is False

    @given(
        age=st.integers(
            min_value=BRANCH_RETENTION_DAYS + 1, max_value=365
        ).map(lambda d: timedelta(days=d)),
        status=st.sampled_from(_CLOSED_STATUS_VARIANTS),
    )
    @settings(max_examples=200, deadline=None)
    def test_aged_and_closed_yields_true(
        self, age: timedelta, status: str
    ) -> None:
        """``age > 30 days`` AND status ∈ {Done, Closed} (any case)  ``True``.

 Both conditions hold, so the cron is allowed to delete the
 branch. Casing variants exercise the case-fold comparison.


 """
        assert should_delete_branch(age, status) is True

    @given(
        age=st.integers(min_value=-30, max_value=-1).map(
            lambda d: timedelta(days=d)
        ),
        status=_STATUS_STRATEGY,
    )
    @settings(max_examples=100, deadline=None)
    def test_negative_age_is_kept(
        self, age: timedelta, status: str
    ) -> None:
        """Negative age (clock skew)  ``False`` regardless of status.

 A negative duration means ``last_commit_at`` is in the future
 relative to ``workflow.now``. The predicate treats this as
 "too young" rather than risking deletion of a freshly pushed
 branch caught on a wall-clock anomaly.


 """
        assert should_delete_branch(age, status) is False

    @given(age=_age_strategy(), status=_STATUS_STRATEGY)
    @settings(max_examples=200, deadline=None)
    def test_decision_matches_compound_predicate(
        self, age: timedelta, status: str
    ) -> None:
        """Result equals ``(age > 30d) AND (status case-folded ∈ {done, closed})``.

 Cross-checks the predicate against an inline reproduction of
 the rule so a future refactor of the helper cannot drift from
 the spec without this test failing.


 """
        expected_aged = age > timedelta(days=BRANCH_RETENTION_DAYS)
        expected_closed = isinstance(status, str) and status.casefold() in {
            s.casefold() for s in CLOSED_JIRA_STATUSES
        }
        expected = expected_aged and expected_closed
        assert should_delete_branch(age, status) is expected


class TestShouldDeleteBranchExamples:
    """Concrete examples for ``should_delete_branch``.


 """

    def test_aged_done_yields_true(self) -> None:
        """31 days old, status ``Done``  eligible for deletion.


 """
        assert should_delete_branch(timedelta(days=31), "Done") is True

    def test_aged_closed_yields_true(self) -> None:
        """31 days old, status ``Closed``  eligible for deletion.


 """
        assert should_delete_branch(timedelta(days=31), "Closed") is True

    def test_boundary_day_30_is_kept(self) -> None:
        """Exactly 30 days old  kept (strict inequality).


 """
        assert should_delete_branch(timedelta(days=30), "Done") is False

    def test_aged_open_status_is_kept(self) -> None:
        """31 days old but status ``In Progress``  kept.


 """
        assert (
            should_delete_branch(timedelta(days=31), "In Progress") is False
        )

    def test_young_closed_is_kept(self) -> None:
        """Only 5 days old, status ``Done``  kept (too young).


 """
        assert should_delete_branch(timedelta(days=5), "Done") is False

    def test_case_insensitive_done(self) -> None:
        """``DONE`` (upper) should match ``Done`` (canonical).


 """
        assert should_delete_branch(timedelta(days=31), "DONE") is True

    def test_empty_status_is_kept(self) -> None:
        """Empty status string  kept.


 """
        assert should_delete_branch(timedelta(days=31), "") is False


# ---------------------------------------------------------------------------
# iter_advance pure idempotency helpers
# ---------------------------------------------------------------------------
#
# The full ``iter_advance_pr_supersede`` activity does Bitbucket I/O
# and a Postgres ledger insert, both of which are exercised by the
# unit tests in
# ``platform/workers/agent-runner-worker/tests/unit/test_iter_advance_pr_supersede.py``.
# The invariant here pins the **pure** half of the contract:
#
# *:func:`_build_banner` is a deterministic formatter - same input
# yields the same string, banner contains the new PR id, banner
# embeds the canonical Turkish supersede notice.
# *:func:`_description_already_banners` is a pure idempotency guard
# - once a banner is present in the description, a second
# prepend-via-the-guard is a no-op (no double-prefix).
#
# Together, these properties are sufficient to prove that an
# ``iter_advance_pr_supersede`` retry never produces a doubly-prefixed
# description even when the upstream Bitbucket PUT succeeds and then
# Temporal retries the activity (which is the loop the
# idempotency the operational rule actually guards against).

# PR ids - bounded so Hypothesis shrinks quickly; the formatter does
# not constrain the integer range, so any positive int is valid.
_PR_ID_STRATEGY = st.integers(min_value=1, max_value=1_000_000)

# Description bodies - small alphabet, bounded length so Hypothesis
# can shrink counterexamples to readable strings. ``max_size=80``
# keeps the tests fast while still exercising long descriptions.
_DESCRIPTION_STRATEGY = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "Z"),
        blacklist_characters="\x00",
    ),
    max_size=80,
)


class TestIterAdvanceBannerProperties:
    """invariant-based invariants for ``_build_banner``.

 The full Bitbucket-side idempotency contract relies on the banner
 formatter being:

 1. **Deterministic** - same input always yields the same string.
 2. **Embedding the new PR id** - the banner mentions ``#{new_pr_id}``
 so a human reading the description sees which iter superseded.
 3. **Containing the canonical Turkish notice** - design.md §
 "AgentRunner modülü" pins the exact wording.


 """

    @given(new_pr_id=_PR_ID_STRATEGY)
    @settings(max_examples=200, deadline=None)
    def test_deterministic(self, new_pr_id: int) -> None:
        """Two calls with the same ``new_pr_id`` return the same string.


 """
        first = _build_banner(new_pr_id)
        second = _build_banner(new_pr_id)
        assert first == second

    @given(new_pr_id=_PR_ID_STRATEGY)
    @settings(max_examples=200, deadline=None)
    def test_banner_embeds_new_pr_id(self, new_pr_id: int) -> None:
        """The banner string contains ``#{new_pr_id}``.


 """
        assert f"#{new_pr_id}" in _build_banner(new_pr_id)

    @given(new_pr_id=_PR_ID_STRATEGY)
    @settings(max_examples=100, deadline=None)
    def test_banner_contains_canonical_supersede_phrase(
        self, new_pr_id: int
    ) -> None:
        """The banner contains the canonical Turkish supersede phrase.

 Pins the design.md wording so a future refactor cannot drop
 the notice without this property failing.


 """
        banner = _build_banner(new_pr_id)
        # Ascii-only fragments survive any Unicode normalisation that
        # might be applied to the template; we check the bot-facing
        # ``PR #N`` token plus an unambiguous Turkish word from the
        # canonical message.
        assert f"PR #{new_pr_id}" in banner
        assert "iterasyon" in banner

    @given(
        new_pr_id_a=_PR_ID_STRATEGY,
        new_pr_id_b=_PR_ID_STRATEGY,
    )
    @settings(max_examples=200, deadline=None)
    def test_distinct_pr_ids_produce_distinct_banners(
        self, new_pr_id_a: int, new_pr_id_b: int
    ) -> None:
        """Different ``new_pr_id``  different banner strings.

 Without this invariant, an iter-advance retry against a
 *different* new PR id could fail the ``already-banners``
 guard and be silently skipped.


 """
        if new_pr_id_a == new_pr_id_b:
            return
        assert _build_banner(new_pr_id_a) != _build_banner(new_pr_id_b)


class TestDescriptionAlreadyBannersProperties:
    """invariant-based invariants for ``_description_already_banners``.

 The guard returns ``True`` when the description already contains
 the banner for the given ``new_pr_id``. The properties below
 pin the idempotency contract that prevents the iter-advance
 activity from prepending a second copy of the banner on retry.


 """

    @given(
        description=_DESCRIPTION_STRATEGY, new_pr_id=_PR_ID_STRATEGY
    )
    @settings(max_examples=200, deadline=None)
    def test_returns_bool(
        self, description: str, new_pr_id: int
    ) -> None:
        """The guard always returns a plain:class:`bool`.


 """
        assert isinstance(
            _description_already_banners(description, new_pr_id), bool
        )

    @given(
        description=_DESCRIPTION_STRATEGY, new_pr_id=_PR_ID_STRATEGY
    )
    @settings(max_examples=200, deadline=None)
    def test_deterministic(
        self, description: str, new_pr_id: int
    ) -> None:
        """Two calls with the same input return the same result.


 """
        first = _description_already_banners(description, new_pr_id)
        second = _description_already_banners(description, new_pr_id)
        assert first == second

    @given(
        description=_DESCRIPTION_STRATEGY, new_pr_id=_PR_ID_STRATEGY
    )
    @settings(max_examples=200, deadline=None)
    def test_banner_prepended_then_guard_is_true(
        self, description: str, new_pr_id: int
    ) -> None:
        """After prepending the banner, the guard returns ``True``.

 This is the core of the prepend-once invariant: once the
 banner is in the description, ``_description_already_banners``
 sees it and a retried iter-advance will skip the description
 update.


 """
        new_description = _build_banner(new_pr_id) + description
        assert (
            _description_already_banners(new_description, new_pr_id)
            is True
        )

    @given(
        description=_DESCRIPTION_STRATEGY, new_pr_id=_PR_ID_STRATEGY
    )
    @settings(max_examples=200, deadline=None)
    def test_idempotent_prepend_via_guard(
        self, description: str, new_pr_id: int
    ) -> None:
        """Prepending under the guard yields the same result twice.

 Simulates the iter-advance activity logic:::

 if not _description_already_banners(desc, n):
 desc = _build_banner(n) + desc

 Running this guarded prepend twice in succession must produce
 the same string both times - exactly one banner copy. This
 is the property actually requires from the production
 retry path.


 """

        def _guarded_prepend(desc: str, new_id: int) -> str:
            if not _description_already_banners(desc, new_id):
                return _build_banner(new_id) + desc
            return desc

        once = _guarded_prepend(description, new_pr_id)
        twice = _guarded_prepend(once, new_pr_id)
        assert once == twice
        # And the resulting description carries exactly one banner.
        banner = _build_banner(new_pr_id)
        assert twice.count(banner) == 1

    @given(
        description=_DESCRIPTION_STRATEGY,
        new_pr_id_a=_PR_ID_STRATEGY,
        new_pr_id_b=_PR_ID_STRATEGY,
    )
    @settings(max_examples=200, deadline=None)
    def test_different_pr_id_is_not_already_bannered(
        self,
        description: str,
        new_pr_id_a: int,
        new_pr_id_b: int,
    ) -> None:
        """A banner for ``new_pr_id_a`` does not satisfy the guard for ``new_pr_id_b``.

 Without this invariant, an iter-advance against a *new*
 iteration's PR id would mistakenly skip when an *older*
 iteration's banner is still in the description, leaving the
 PO without a notice for the latest iter.


 """
        if new_pr_id_a == new_pr_id_b:
            return
        # Build a description bannered for ``a``.
        bannered_for_a = _build_banner(new_pr_id_a) + description
        # The guard for ``b`` must return False (no banner for ``b``
        # is present), so a fresh prepend is required.
        # Hypothesis may produce a description that *coincidentally*
        # contains the substring ``PR #{b}`` - in that case skip. In
        # practice the BANNER_PREFIX_TEMPLATE is long enough that
        # accidental collisions are negligible, but we guard anyway.
        if _build_banner(new_pr_id_b) in bannered_for_a:
            return
        assert (
            _description_already_banners(bannered_for_a, new_pr_id_b)
            is False
        )


class TestIterAdvanceBannerExamples:
    """Concrete examples for the iter-advance pure helpers.


 """

    def test_banner_contains_new_pr_id_token(self) -> None:
        """``_build_banner(127)`` mentions ``PR #127``.


 """
        banner = _build_banner(127)
        assert "PR #127" in banner
        assert "iterasyon" in banner

    def test_label_template_uses_new_pr_id(self) -> None:
        """The Bitbucket label template embeds ``new_pr_id`` verbatim.

 Pins the label format the PO Review Inbox greps for.


 """
        assert (
            SUPERSEDE_LABEL_TEMPLATE.format(new_pr_id=42)
            == "superseded-by-pr-42"
        )

    def test_banner_template_format_is_stable(self) -> None:
        """The banner template formats deterministically for a fixed id.


 """
        assert _build_banner(7) == BANNER_PREFIX_TEMPLATE.format(new_pr_id=7)

    def test_empty_description_is_not_already_bannered(self) -> None:
        """Empty description  guard returns ``False`` for any id.


 """
        assert _description_already_banners("", 1) is False

    def test_description_with_banner_returns_true(self) -> None:
        """Description that starts with the banner  guard returns ``True``.


 """
        body = "Original PR description.\n\nMore details here."
        new_description = _build_banner(99) + body
        assert _description_already_banners(new_description, 99) is True

    def test_description_with_other_pr_banner_does_not_match(self) -> None:
        """A banner for PR #1 does not satisfy the guard for PR #2.


 """
        body = "Original PR description."
        new_description = _build_banner(1) + body
        assert _description_already_banners(new_description, 2) is False

    def test_guarded_prepend_idempotent(self) -> None:
        """Running the guarded prepend twice yields a single banner.

 Mirrors the iter-advance retry path in the production
 activity.


 """
        body = "Original PR description.\n\nMore details here."

        def _guarded_prepend(desc: str, new_id: int) -> str:
            if not _description_already_banners(desc, new_id):
                return _build_banner(new_id) + desc
            return desc

        once = _guarded_prepend(body, 42)
        twice = _guarded_prepend(once, 42)
        assert once == twice
        assert twice.count(_build_banner(42)) == 1
