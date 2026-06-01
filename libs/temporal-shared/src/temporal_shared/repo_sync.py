"""Pure repo-mapping diff helper for the auto-sync admin endpoint (N7).

This module is the **single source of truth** for the
``compute_repo_mapping_diff`` set-algebra computation used by the
``POST /admin/departments/{id}/repo-mappings/sync`` endpoint defined in
``platform-mimari-workflows`` design.md
§"Components and Interfaces — repo_mapping_sync API" and Requirement
10.7 (R10.7 — repo mapping auto-sync, MIMARI §16.16 N7).

The helper splits a Bitbucket workspace scan against the dept's current
``departments.json`` ``repo_mappings`` array into three disjoint sets:

* ``added``   — slugs scanned in Bitbucket that the dept does **not**
  yet have a mapping for. The admin should review and persist these.
* ``removed`` — slugs the dept **has** a mapping for but Bitbucket no
  longer surfaces (repo deleted / moved). The admin should review and
  prune these.
* ``unchanged`` — slugs present in both; nothing for the admin to do.

By isolating the decision into a **pure** function (no I/O, no clock,
no random / uuid, no Temporal calls) we get four wins at once:

1. The endpoint's dry-run mode is trivially testable without a live
   Bitbucket workspace or a Postgres connection.
2. The set-algebra invariants
   (``added ∩ removed == ∅``;
    ``added ∪ unchanged == scanned``;
    ``removed ∪ unchanged == current.set()``)
   can be exercised with Hypothesis as a single property suite — see
   ``platform/tests/property/test_repo_mapping_diff.py`` (Property test
   for task 14.3).
3. Idempotence on equal inputs (``scanned == current.set()`` =>
   both ``added`` and ``removed`` are empty and ``unchanged ==
   scanned``) is enforced by construction; running the diff twice in a
   row over the same workspace is a no-op.
4. The function is safe to call from inside Temporal workflow code if a
   later spec wants to schedule the auto-sync as a cron workflow
   (replay-safe by virtue of being pure).

Public API
----------
* :class:`RepoMapping` — frozen dataclass mirroring one entry of
  ``departments.json -> bitbucket.repo_mappings[]``. Carries the
  human-readable ``name`` alongside the canonical ``slug`` so the
  admin UI can render either field; the diff itself is computed on
  ``slug`` membership only (slug is the stable identifier; ``name``
  may drift when a repo is renamed in the Bitbucket UI).
* :class:`RepoMappingDiff` — frozen dataclass holding the three
  ``frozenset[str]`` partitions. Hashable and replay-safe.
* :func:`compute_repo_mapping_diff` — pure set-algebra computation.

Validates: Requirement 10.7 (R10.7 — repo mapping auto-sync N7).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "RepoMapping",
    "RepoMappingDiff",
    "compute_repo_mapping_diff",
]


# ---------------------------------------------------------------------------
# RepoMapping — frozen dataclass for one ``repo_mappings[]`` entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoMapping:
    """One entry of a department's Bitbucket ``repo_mappings`` array.

    Mirrors the schema of ``departments.json -> bot.bitbucket
    .repo_mappings[]`` at the structural level the diff helper needs:
    a stable slug (the diff key) plus a human-readable name (the UI
    label). Any extra columns the production schema may carry (e.g.
    ``jira_project_key``, ``default_branch``) are intentionally
    out-of-scope for this helper — the diff is purely about presence /
    absence of a slug, not about the metadata attached to it.

    The dataclass is frozen + slotted so instances are hashable and the
    helper can place them in tuples / sets without copy-on-mutate
    surprises. The slot lock-down is the same pattern used by
    :class:`temporal_shared.messages.OutputAction` and the rest of the
    workflow-shared dataclasses.

    Attributes
    ----------
    name:
        Human-readable repository name as it appears in the Bitbucket
        UI (e.g. ``"Payment Callbacks"``). Carried for the audit row
        and the admin UI; not used as the diff key. May be empty
        string when the source mapping omits a friendly name.
    slug:
        Canonical Bitbucket repository slug (e.g.
        ``"payment-callbacks"``). This is the diff key — slugs are
        what Bitbucket's API surfaces and what the admin will compare
        against the current ``departments.json`` document. Must be
        non-empty for a meaningful diff entry; the helper does not
        validate the slug regex itself (departments.schema.json
        already pins ``^[a-z0-9][a-z0-9-]*$``) so the diff stays
        composable with any future slug grammar.
    """

    name: str
    slug: str


# ---------------------------------------------------------------------------
# RepoMappingDiff — output of :func:`compute_repo_mapping_diff`
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoMappingDiff:
    """Three disjoint partitions of a Bitbucket scan vs current mappings.

    Returned by :func:`compute_repo_mapping_diff`. The three sets are
    pairwise disjoint by construction:

    * ``added ∩ removed == ∅``
    * ``added ∩ unchanged == ∅``
    * ``removed ∩ unchanged == ∅``

    And they reconstruct the inputs:

    * ``added ∪ unchanged == scanned_repos``
    * ``removed ∪ unchanged == {m.slug for m in current_mappings}``

    The aggregator is intentionally agnostic to the order in which
    ``current_mappings`` was provided (a tuple-of-mappings, not a set,
    purely so the caller's input shape mirrors ``departments.json``;
    the helper folds the tuple into a slug set internally).

    Attributes
    ----------
    added:
        Slugs that exist in the Bitbucket scan but **not** in the
        current dept mappings. The admin should review and persist
        these into ``departments.json`` (or accept them with
        ``?apply=true``).
    removed:
        Slugs that exist in the current dept mappings but **not** in
        the Bitbucket scan. The admin should review and prune these
        from ``departments.json`` (or accept the pruning with
        ``?apply=true``); a removal usually means the repo was
        deleted, archived, or moved to a different workspace.
    unchanged:
        Slugs present in both the scan and the current mappings.
        Carried for audit-trail completeness so the admin sees the
        full picture in the dry-run response.
    """

    added: frozenset[str]
    removed: frozenset[str]
    unchanged: frozenset[str]


# ---------------------------------------------------------------------------
# compute_repo_mapping_diff — pure set-algebra computation
# ---------------------------------------------------------------------------


def compute_repo_mapping_diff(
    scanned_repos: frozenset[str],
    current_mappings: tuple[RepoMapping, ...],
) -> RepoMappingDiff:
    """Return the three-way diff between a Bitbucket scan and current mappings.

    Pure function (no I/O, no clock, no Temporal calls). The decision
    is the textbook three-set partition:

    .. code-block:: text

        current_slugs = {m.slug for m in current_mappings}
        added         = scanned_repos - current_slugs
        removed       = current_slugs - scanned_repos
        unchanged     = scanned_repos & current_slugs

    Idempotence on equal inputs (Property invariant) follows by
    construction: when ``scanned_repos == current_slugs`` the set
    difference operators yield the empty set in both directions and
    the intersection equals the input.

    Duplicate slugs in ``current_mappings`` (e.g. two
    :class:`RepoMapping` entries with the same ``slug`` but different
    ``name``) collapse to a single entry in ``current_slugs`` because
    the helper folds the tuple into a set. This matches
    ``departments.schema.json`` which the wider system relies on to
    reject duplicate repo_mappings entries at validation time — the
    helper does not re-check that contract here, only that the
    set-algebra holds for whichever slugs the caller provided.

    Parameters
    ----------
    scanned_repos:
        :class:`frozenset` of slugs returned by a fresh Bitbucket
        workspace scan (e.g.
        ``{"payment-callbacks", "fraud-rules"}``). Must be a
        :class:`frozenset` so the caller has already deduplicated and
        the helper cannot accidentally mutate it.
    current_mappings:
        ``tuple`` of :class:`RepoMapping` entries from the dept's
        ``departments.json`` document, in the order they appear
        there. Order is not significant for the diff; the tuple
        shape is preserved so the caller's input mirrors the
        on-disk JSON array.

    Returns
    -------
    RepoMappingDiff
        Three pairwise-disjoint :class:`frozenset` partitions
        covering the union of ``scanned_repos`` and the current
        slugs. Every diff field is immutable so the caller can hand
        it straight to a JSON serializer or to an audit payload
        without defensive copying.

    Examples
    --------
    >>> mapping_a = RepoMapping(name="Payment Callbacks", slug="payment-callbacks")
    >>> mapping_b = RepoMapping(name="Fraud Rules", slug="fraud-rules")
    >>> diff = compute_repo_mapping_diff(
    ...     scanned_repos=frozenset({"payment-callbacks", "new-repo"}),
    ...     current_mappings=(mapping_a, mapping_b),
    ... )
    >>> sorted(diff.added)
    ['new-repo']
    >>> sorted(diff.removed)
    ['fraud-rules']
    >>> sorted(diff.unchanged)
    ['payment-callbacks']

    Idempotence on equal inputs:

    >>> diff_eq = compute_repo_mapping_diff(
    ...     scanned_repos=frozenset({"payment-callbacks", "fraud-rules"}),
    ...     current_mappings=(mapping_a, mapping_b),
    ... )
    >>> (diff_eq.added, diff_eq.removed) == (frozenset(), frozenset())
    True
    >>> diff_eq.unchanged == frozenset({"payment-callbacks", "fraud-rules"})
    True
    """

    # Fold the tuple into a slug set so the set-algebra below works
    # uniformly even if the caller passed duplicate ``RepoMapping``
    # entries. We use a set comprehension rather than a generator so
    # the dedup is explicit.
    current_slugs: frozenset[str] = frozenset(
        mapping.slug for mapping in current_mappings
    )

    return RepoMappingDiff(
        added=scanned_repos - current_slugs,
        removed=current_slugs - scanned_repos,
        unchanged=scanned_repos & current_slugs,
    )
