"""Pure set-algebra helpers for Orphan Branches + PO Review Inbox.

This module hosts two **pure** decision helpers used by the
``automation-service`` API endpoints
``GET /api/orphan-branches`` and ``GET /api/po-review-inbox``
for post-commit bot output:

* :func:`compute_orphan_branches` - set-algebra helper that returns
  the subset of ``ai/*`` branches that **do not** appear as the
  ``source_branch`` of any pull request.  These are the "orphan"
  bot branches the platform produced via ``code_change_commit_only``
  but for which no PR was ever opened.  The Orphan Branches Streamlit
  page surfaces them so the PO can decide whether to open a draft PR
  or let the cron retention reaper sweep them out.
* :func:`compute_po_review_inbox` - pure filter that returns the
  draft pull requests authored by a known bot account.  These are
  the PRs the bot has prepared but the PO has not yet reviewed; the
  PO Review Inbox Streamlit page surfaces them with the "PR Aç
  (Draft)", "Düzeltme İste", "Onaylama notu" actions.

Why a separate module?
----------------------
``temporal_shared.identifiers`` owns the **string-shape** contract
for ``ai/{issue_key}`` branch names; ``temporal_shared.code_change``
owns the **commit-message** formatter.  This module owns the
**aggregation** contract for the two PO-facing API endpoints.  The
three concerns share a domain (post-commit bot output) but have
unrelated input / output shapes, so they are split into adjacent
modules to keep each file's surface area narrow.  All three modules
are re-exported from :mod:`temporal_shared` so call sites can import
either set of helpers without learning the internal layout.

Purity contract
---------------
Every public function in this module is **pure**:

* No I/O - the caller reads the branches / PRs from Bitbucket via
  :mod:`mcp_client` and passes them in as plain dataclass values.
* No clocks - the helpers do not consult ``last_commit_at``.  The
  branch ordering is the API endpoint's responsibility (the endpoint
  sorts the returned set by :attr:`Branch.last_commit_at` so the
  oldest orphan surfaces first); the helpers themselves are
  set-algebra operators.
* No randomness, no UUIDs, no globals.

This makes the helpers safe to call from anywhere the runtime imposes
replay determinism (Temporal workflow body, Hypothesis property test,
HTTP handler).  The matching AST replay-determinism property test will
fire if a future edit introduces a forbidden import here.

Returning ``frozenset``
-----------------------

Both helpers return a :class:`frozenset` rather than a bare ``set``:

* The dataclasses are frozen, so :class:`frozenset` is well-defined
  on them.
* Returning an immutable container makes the determinism invariant
  trivially provable - the caller cannot mutate the
  returned aggregate, so two calls with the same input always return
  equal aggregates.
* The HTTP endpoint converts the result to a list and sorts it by
  ``last_commit_at`` (oldest-first) before serialising; ``frozenset``
  is a valid input to :func:`sorted` so the conversion is one line.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

__all__ = [
    # dataclasses
    "Branch",
    "PullRequest",
    # constants
    "AI_BRANCH_PREFIX",
    # pure decision helpers
    "compute_orphan_branches",
    "compute_po_review_inbox",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Prefix every bot-authored branch name carries.  Mirrors the
#: ``ai/{issue_key}`` shape produced by
#: :func:`temporal_shared.identifiers.branch_name` and
#: :func:`temporal_shared.code_change.compute_branch_name`.
#:
#: The orphan-branch helper uses ``startswith(AI_BRANCH_PREFIX)`` to
#: include both the iter-1 form ``ai/PAY-1`` **and** the multi-iter
#: form ``ai/PAY-1-iter3`` in a single membership check.
AI_BRANCH_PREFIX: Final[str] = "ai/"


# ---------------------------------------------------------------------------
# Branch - Bitbucket branch as the API endpoint sees it
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Branch:
    """One Bitbucket branch viewed by the Orphan Branches API.

    Attributes
    ----------
    name:
        Full branch ref name as Bitbucket reports it (e.g.
        ``"ai/PAY-1"`` or ``"ai/PAY-1-iter3"``).  The orphan-branch
        helper compares this verbatim against
        :attr:`PullRequest.source_branch`, so any normalisation
        (stripping ``refs/heads/`` etc.) is the caller's
        responsibility - typically performed inside
        :mod:`mcp_client.bitbucket` when the branch list is fetched.
    last_commit_at:
        Timestamp of the most recent commit on this branch.  Optional
        (``None`` is allowed for branches whose commit metadata has
        not been hydrated yet) so the dataclass can be constructed
        from a partial Bitbucket response.  The helper functions in
        this module do **not** consult :attr:`last_commit_at`; the
        API endpoint reads it on the returned set to surface the
        oldest orphan first.

    Notes
    -----
    The dataclass is :class:`frozen` so it is hashable and can live
    inside a :class:`frozenset`.  ``slots=True`` keeps the per-row
    memory footprint small when the platform sweeps a large workspace
    via the orphan-branch endpoint.  The dataclass intentionally does
    **not** carry the repo slug or the commit sha - those are part of
    the surrounding API response envelope, not part of the
    set-algebra decision.
    """

    name: str
    last_commit_at: datetime | None = None


# ---------------------------------------------------------------------------
# PullRequest - Bitbucket PR as the API endpoint sees it
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PullRequest:
    """One Bitbucket pull request viewed by the PO Review Inbox API.

    Attributes
    ----------
    id:
        Bitbucket-assigned numeric PR id.  Stable per repo and used as
        the natural key when the PO Review Inbox endpoint deduplicates
        the inbox or composes the per-PR action endpoints
        (``/api/po-review-inbox/{pr_id}/open-draft`` etc.).
    source_branch:
        Branch the PR pulls from - compared verbatim against
        :attr:`Branch.name` by :func:`compute_orphan_branches` so the
        caller is responsible for stripping any ``refs/heads/``
        prefix before constructing this dataclass.
    is_draft:
        Whether Bitbucket reports the PR as a draft.  The platform
        only ever opens draft PRs, but this field is read from Bitbucket rather
        than assumed so a hypothetical human-promoted PR is correctly
        excluded from the PO Review Inbox.
    author_account_id:
        Atlassian / Bitbucket ``account_id`` of the PR author.
        Compared against the bot account-id set by
        :func:`compute_po_review_inbox`; opaque token, not validated
        for shape (different Atlassian deployments use different
        formats).
    title:
        PR title.  Carried so the API response can include it without
        a second Bitbucket round-trip; the helpers themselves do not
        consult it.  Defaults to the empty string for callers that
        only have the partial PR shape.

    Notes
    -----
    The dataclass is :class:`frozen` so two PRs with identical fields
    are equal and the inbox aggregate behaves like a true mathematical
    set.  ``slots=True`` keeps the per-row memory footprint small.
    """

    id: int
    source_branch: str
    is_draft: bool
    author_account_id: str
    title: str = field(default="")


# ---------------------------------------------------------------------------
# compute_orphan_branches
# ---------------------------------------------------------------------------


def compute_orphan_branches(
    branches: Iterable[Branch],
    prs: Iterable[PullRequest],
) -> frozenset[Branch]:
    """Return the ``ai/*`` branches that have no associated PR.

    Pure set-algebra operator.  A branch is **orphan** iff:

    1. its :attr:`Branch.name` starts with :data:`AI_BRANCH_PREFIX`
       (i.e. it is a bot-authored branch produced by
       :func:`temporal_shared.identifiers.branch_name` or
       :func:`temporal_shared.code_change.compute_branch_name`), **and**
    2. no pull request in ``prs`` lists it as
       :attr:`PullRequest.source_branch`.

    The helper does not consult :attr:`PullRequest.is_draft` or any
    other PR field - a closed, declined, merged, or draft PR all
    "claim" their source branch, so the branch is not orphan.  The
    Orphan Branches API endpoint wraps the returned set with sorting by
    :attr:`Branch.last_commit_at` (oldest-first) so the PO surfaces the
    longest-orphan branches first.

    Set-algebra form
    ----------------

    .. math::

        \\text{orphan}(B, P) =
        \\{ b \\in B \\mid b.\\text{name} \\in \\text{AI prefix} \\}
        \\setminus
        \\{ b \\in B \\mid \\exists p \\in P :
            p.\\text{source\\_branch} = b.\\text{name} \\}

    Equivalent in Python::

        {b for b in branches
         if b.name.startswith(AI_BRANCH_PREFIX)
         and not any(pr.source_branch == b.name for pr in prs)}

    Implementation note
    -------------------

    The pull-request branch names are materialised once into a
    :class:`frozenset` so the per-branch membership check is
    :math:`O(1)` rather than :math:`O(|P|)` - the orphan-branch
    endpoint may sweep a large Bitbucket workspace and the naive
    ``any(...)`` form is :math:`O(|B| \\cdot |P|)`.

    Determinism / idempotency invariants
    -------------------------------------------------

    * **Determinism.** Calling the function twice with the same input
      returns equal :class:`frozenset` instances.  No clock, no
      randomness, no globals.
    * **Idempotency.** ``compute_orphan_branches(orphans, prs) ==
      orphans`` whenever ``orphans`` is itself a subset of
      ``branches`` and no PR claims any branch in ``orphans``.
    * **Subset invariant.** The returned set is always a subset of
      the input ``branches`` interpreted as a set - no branch is
      synthesised.
    * **Filter invariants.** Every returned branch satisfies
      ``b.name.startswith(AI_BRANCH_PREFIX)`` and ``b.name`` is
      **not** the ``source_branch`` of any PR in ``prs``.

    Parameters
    ----------
    branches:
        Iterable of :class:`Branch` values.  May be empty, in which
        case the function returns an empty :class:`frozenset`.
        Duplicates are tolerated - the helper materialises the input
        once via :class:`frozenset` semantics so a duplicate branch
        contributes a single entry to the result.
    prs:
        Iterable of :class:`PullRequest` values.  May be empty, in
        which case every ``ai/*`` branch becomes orphan.  Both
        branches and PRs come from the same Bitbucket workspace; the
        caller is responsible for restricting both lists to the same
        repo / dept boundary before calling.

    Returns
    -------
    frozenset[Branch]
        The subset of ``branches`` that are bot-authored and not
        referenced by any PR.  Returned as :class:`frozenset` so the
        result is hashable, immutable, and trivially deterministic.

    Examples
    --------
    >>> b1 = Branch(name="ai/PAY-1")
    >>> b2 = Branch(name="ai/PAY-2")
    >>> b3 = Branch(name="feature/manual")  # not bot-authored
    >>> p1 = PullRequest(
    ...     id=1, source_branch="ai/PAY-1", is_draft=True,
    ...     author_account_id="bot-acct",
    ... )
    >>> orphans = compute_orphan_branches([b1, b2, b3], [p1])
    >>> orphans == {b2}
    True
    >>> # Idempotent - feeding the orphans back returns the same set.
    >>> compute_orphan_branches(orphans, [p1]) == orphans
    True
    >>> # Empty PR list → every ai/* branch is orphan.
    >>> compute_orphan_branches([b1, b2, b3], []) == {b1, b2}
    True
    >>> # Empty branch list → empty result regardless of PR list.
    >>> compute_orphan_branches([], [p1]) == frozenset()
    True
    """
    # Materialise the PR source-branch names into a frozenset once so
    # the per-branch membership check is O(1).  We extract the names
    # eagerly (rather than lazily) because the caller may pass a
    # generator for ``prs``; a generator would be exhausted by the
    # first ``in`` check otherwise.
    pr_source_branches: frozenset[str] = frozenset(pr.source_branch for pr in prs)

    return frozenset(
        b
        for b in branches
        if b.name.startswith(AI_BRANCH_PREFIX) and b.name not in pr_source_branches
    )


# ---------------------------------------------------------------------------
# compute_po_review_inbox
# ---------------------------------------------------------------------------


def compute_po_review_inbox(
    prs: Iterable[PullRequest],
    bot_ids: frozenset[str],
) -> frozenset[PullRequest]:
    """Return the draft pull requests authored by a known bot account.

    Pure filter.  A PR is in the **PO Review Inbox** iff:

    1. it is marked as a draft on Bitbucket
       (:attr:`PullRequest.is_draft` is ``True``), **and**
    2. its :attr:`PullRequest.author_account_id` is in the
       caller-provided ``bot_ids`` set.

    The helper does not look at the PR title, description, source
    branch, or any other field - the PO Review Inbox is intentionally
    "every draft PR the bot prepared", and the human-vs-bot promotion
    check belongs to the API endpoint that surfaces the inbox, not to
    this set-algebra primitive.

    Filter form
    -----------

    .. math::

        \\text{inbox}(P, B) = \\{ pr \\in P \\mid
            pr.\\text{is\\_draft} \\land
            pr.\\text{author\\_account\\_id} \\in B \\}

    Equivalent in Python::

        {pr for pr in prs
         if pr.is_draft and pr.author_account_id in bot_ids}

    Determinism / filter invariants
    --------------------------------------------

    * **Every result is a draft.** ``all(pr.is_draft for pr in
      result)`` is always True.
    * **Every result author is a bot.** ``all(pr.author_account_id
      in bot_ids for pr in result)`` is always True.
    * **Subset invariant.** ``result <= set(prs)`` - no PR is
      synthesised.
    * **Determinism.** Two calls with the same input return equal
      :class:`frozenset` instances.

    Parameters
    ----------
    prs:
        Iterable of :class:`PullRequest` values.  May be empty.
        Duplicates are tolerated - the result is a :class:`frozenset`,
        so an identical PR contributes a single entry.
    bot_ids:
        :class:`frozenset` of bot ``account_id`` values for the
        department.  Typed as :class:`frozenset` rather than the more
        permissive ``Iterable`` because the caller already loads the
        bot account-id set into an immutable container at startup
        (foundation ``departments.json`` ingestion); requiring
        :class:`frozenset` at the call site keeps the helper
        allocation-free.  An empty ``bot_ids`` set yields an empty
        result regardless of how many drafts ``prs`` contains.

    Returns
    -------
    frozenset[PullRequest]
        The subset of ``prs`` that are drafts authored by a known
        bot.  Returned as :class:`frozenset` so the result is
        hashable, immutable, and trivially deterministic.

    Examples
    --------
    >>> bot = "bot-acct"
    >>> human = "human-1"
    >>> draft_bot = PullRequest(
    ...     id=1, source_branch="ai/PAY-1", is_draft=True,
    ...     author_account_id=bot,
    ... )
    >>> open_bot = PullRequest(
    ...     id=2, source_branch="ai/PAY-2", is_draft=False,
    ...     author_account_id=bot,
    ... )
    >>> draft_human = PullRequest(
    ...     id=3, source_branch="feature/x", is_draft=True,
    ...     author_account_id=human,
    ... )
    >>> inbox = compute_po_review_inbox(
    ...     [draft_bot, open_bot, draft_human], frozenset({bot})
    ... )
    >>> inbox == {draft_bot}
    True
    >>> # Empty bot_ids → empty result even when drafts exist.
    >>> compute_po_review_inbox(
    ...     [draft_bot, draft_human], frozenset()
    ... ) == frozenset()
    True
    >>> # Empty PR list → empty result.
    >>> compute_po_review_inbox([], frozenset({bot})) == frozenset()
    True
    """
    return frozenset(
        pr for pr in prs if pr.is_draft and pr.author_account_id in bot_ids
    )
