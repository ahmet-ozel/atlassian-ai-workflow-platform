"""Property test P12 — Copy page tree rejects ancestor-of-self targets.

Validates Requirement 31.3 / design Property 12:
:meth:`PageMoveCopyMixin.copy_page_tree` must raise an ``invalid_target:``
prefixed ``ValueError`` (which the server tool maps to the structured
``invalid_target`` error envelope) *before* issuing the write-side POST
when the supplied ``target_parent_id`` equals ``page_id`` or appears
anywhere in ``page_id``'s subtree — i.e. when ``page_id`` is an ancestor
of (or equal to) ``target_parent_id``. For targets that live outside
``page_id``'s subtree, the mixin must proceed to the POST.

Test shape
----------
The test exercises the mixin directly (same pattern as P13). A
Hypothesis strategy builds a random small tree of integer page ids and
then selects a ``(source, target)`` pair from that tree:

* **Property A** — *invalid pair*: ``source == target`` OR ``source`` is
  an ancestor of ``target`` (equivalently, ``target`` is a descendant of
  ``source``). The mixin must raise ``ValueError`` with the
  ``invalid_target:`` prefix AND the mocked HTTP surface must record
  **zero** POSTs. GETs against
  ``rest/api/content/{target}?expand=ancestors`` are allowed and
  expected (and should number at most one).
* **Property B (smoke)** — *valid pair*: ``source != target`` AND
  ``source`` is not in ``target``'s ancestor chain. The mixin must
  proceed to the POST (at least one) and must *not* raise the
  ``invalid_target`` error. At least one GET (ancestor pre-flight) is
  still expected.

Mocking
-------
Rather than exercise a full ``ConfluenceFetcher``, the test shims a
minimal ``self`` with a ``MagicMock`` in the ``confluence`` attribute.
``confluence.get(path, params=...)`` is wired to a side-effect that
decodes the trailing id segment from ``path`` and returns
``{"id": ..., "ancestors": [{"id": a}, ...]}`` reconstructed from the
Hypothesis-generated tree. ``confluence.post(path, data=...)`` returns
a stub ``{"longTaskId": "stub"}`` so the happy-path call completes.

The "zero POST on invalid" assertion observes ``confluence.post``
directly, which is the layer immediately above the wire and matches
how the mixin issues its write call.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from mcp_atlassian.confluence.page_move_copy import PageMoveCopyMixin


# ---------------------------------------------------------------------------
# Tree strategy
# ---------------------------------------------------------------------------
#
# A tree is represented as:
#
#   ids:       list[int]              — unique page ids (first is root)
#   ancestors: dict[int, list[int]]   — root-first ancestor chain per id
#                                       (the root's entry is an empty list)
#
# Trees are small (2-6 nodes) so Hypothesis can exhaust interesting
# shapes — singleton chains, stars, balanced trees — within the default
# example budget.


@st.composite
def _ancestor_tree(draw: st.DrawFn) -> tuple[list[int], dict[int, list[int]]]:
    ids = draw(
        st.lists(
            st.integers(min_value=1, max_value=9999),
            min_size=2,
            max_size=6,
            unique=True,
        )
    )
    # Build parent map by attaching every non-root id to a random earlier id.
    parents: dict[int, int | None] = {ids[0]: None}
    for i in range(1, len(ids)):
        parents[ids[i]] = draw(st.sampled_from(ids[:i]))

    ancestors: dict[int, list[int]] = {}
    for node in ids:
        chain: list[int] = []
        cur = parents[node]
        while cur is not None:
            chain.append(cur)
            cur = parents[cur]
        chain.reverse()  # root-first, matching the DC API's ``ancestors`` field
        ancestors[node] = chain
    return ids, ancestors


@st.composite
def _invalid_pair(
    draw: st.DrawFn,
) -> tuple[dict[int, list[int]], int, int]:
    """Draw ``(ancestors, source, target)`` where source is ancestor-of-target.

    Includes the degenerate ``source == target`` case.
    """
    ids, ancestors = draw(_ancestor_tree())
    # For every target, the invalid sources are: target itself + its ancestors.
    candidates: list[tuple[int, int]] = []
    for target in ids:
        for source in [target, *ancestors[target]]:
            candidates.append((source, target))
    # ``candidates`` is never empty: each id contributes the (id, id) pair.
    source, target = draw(st.sampled_from(candidates))
    return ancestors, source, target


@st.composite
def _valid_pair(
    draw: st.DrawFn,
) -> tuple[dict[int, list[int]], int, int]:
    """Draw ``(ancestors, source, target)`` where source is NOT ancestor-of-target."""
    ids, ancestors = draw(_ancestor_tree())
    candidates: list[tuple[int, int]] = []
    for source in ids:
        for target in ids:
            if source == target:
                continue
            if source in ancestors[target]:
                continue
            candidates.append((source, target))
    # With min_size=2 a valid pair always exists (the child→root direction),
    # but guard with ``assume`` so pathological shrinks don't misreport.
    assume(candidates)
    source, target = draw(st.sampled_from(candidates))
    return ancestors, source, target


# ---------------------------------------------------------------------------
# Shim helpers
# ---------------------------------------------------------------------------


def _build_get_side_effect(
    ancestors: dict[int, list[int]],
) -> Any:
    """Return a side-effect for ``confluence.get`` that serves the tree."""

    def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        # ``path`` is always of the form ``rest/api/content/{id}`` for the
        # ancestor probe; any other shape signals a regression we want
        # visible in the test output rather than swallowed.
        assert path.startswith("rest/api/content/"), path
        tail = path[len("rest/api/content/"):]
        try:
            node_id = int(tail)
        except ValueError as exc:  # pragma: no cover — defensive
            raise AssertionError(f"unexpected ancestor probe path: {path!r}") from exc
        chain = ancestors.get(node_id, [])
        return {
            "id": str(node_id),
            "ancestors": [{"id": str(a)} for a in chain],
        }

    return _get


def _make_fake_self(ancestors: dict[int, list[int]]) -> tuple[SimpleNamespace, MagicMock]:
    """Build a ``self`` shim for :meth:`PageMoveCopyMixin.copy_page_tree`."""
    confluence = MagicMock(name="confluence-client")
    confluence.get.side_effect = _build_get_side_effect(ancestors)
    confluence.post.return_value = {"longTaskId": "stub-long-task"}
    return SimpleNamespace(confluence=confluence), confluence


# ---------------------------------------------------------------------------
# Property A — ancestor-of-self targets raise BEFORE any POST
# ---------------------------------------------------------------------------


@given(_invalid_pair())
def test_ancestor_of_self_rejected_before_post(
    pair: tuple[dict[int, list[int]], int, int],
) -> None:
    """P12.A: invalid target → ``invalid_target`` and zero POSTs."""
    ancestors, source, target = pair
    fake_self, confluence_mock = _make_fake_self(ancestors)

    with pytest.raises(ValueError) as excinfo:
        PageMoveCopyMixin.copy_page_tree(
            fake_self,
            str(source),
            target_parent_id=str(target),
        )

    assert str(excinfo.value).startswith("invalid_target:"), (
        f"expected 'invalid_target:' prefix, got {excinfo.value!r}"
    )
    # The critical safety property: no write-side call was issued.
    assert confluence_mock.post.call_count == 0, (
        f"expected zero POSTs for ancestor-of-self rejection, "
        f"got {confluence_mock.post.call_count}"
    )
    # Ancestor pre-flight may issue at most one GET (skipped entirely when
    # ``source == target`` because the mixin short-circuits that case).
    assert confluence_mock.get.call_count <= 1, (
        f"expected at most one ancestor GET, got {confluence_mock.get.call_count}"
    )


# ---------------------------------------------------------------------------
# Property B (smoke) — valid targets proceed to at least one POST
# ---------------------------------------------------------------------------


@given(_valid_pair())
def test_non_ancestor_target_proceeds_to_post(
    pair: tuple[dict[int, list[int]], int, int],
) -> None:
    """P12.B: target outside source's subtree → the POST is issued."""
    ancestors, source, target = pair
    fake_self, confluence_mock = _make_fake_self(ancestors)

    result = PageMoveCopyMixin.copy_page_tree(
        fake_self,
        str(source),
        target_parent_id=str(target),
    )

    assert isinstance(result, dict)
    assert result.get("longTaskId") == "stub-long-task"
    # Exactly one ancestor probe (GET) and at least one POST.
    assert confluence_mock.get.call_count == 1
    assert confluence_mock.post.call_count >= 1
    # Sanity-check the POST targeted the copy endpoint for ``source``.
    ((post_path, *_rest), _kwargs) = confluence_mock.post.call_args
    assert post_path == f"rest/api/content/{source}/pagehierarchy/copy"
