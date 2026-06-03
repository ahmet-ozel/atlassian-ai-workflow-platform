"""Property-based tests for the banned MCP tool list.

This file owns the **banned tool list** behavior. Related policy tests are:

- ``test_pr_draft_enforcement.py`` — PR draft enforcement
- ``test_webhook_predicates.py`` (extended) — webhook loop guard behavior

Universal property
------------------

For all tool catalogs ``T`` — regardless of how individual entries are
shaped (plain string, ``dict`` with a ``name`` field, or attribute
object) — :func:`mcp_client.filter_tools` returns a list whose
*name set* is disjoint from :data:`mcp_client.BANNED_TOOLS`:

.. code-block:: text

    ∀ T:  names(filter_tools(T)) ∩ BANNED_TOOLS == ∅

The Hypothesis strategies in this file deliberately mix banned and
allowed names across all three supported shapes so the generated inputs
span the entire LLM tool-catalog surface. Any drift in
:data:`BANNED_TOOLS` or in :func:`filter_tools`'s
shape handling shows up as a counter-example here before reaching
the integration suite.
"""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mcp_client import BANNED_TOOLS, filter_tools


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Allowed (non-banned) tool names. Constrained to the shape the MCP
#: catalog actually emits — lower-case ASCII identifiers with optional
#: underscores. Hypothesis filter excludes any draw that would collide
#: with :data:`BANNED_TOOLS` so the property is meaningful for the
#: "allowed" branch of the catalog.
_allowed_names = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="_"),
    min_size=1,
    max_size=40,
).filter(lambda s: s not in BANNED_TOOLS)

#: Banned tool names sampled from the canonical constant. Sampling
#: from ``BANNED_TOOLS`` itself (rather than hard-coding the names)
#: keeps the property aligned with whatever is checked in.
_banned_names = st.sampled_from(sorted(BANNED_TOOLS))


def _as_string(name: str) -> str:
    """Tool-shape #1 — plain string."""

    return name


def _as_dict(name: str) -> dict[str, Any]:
    """Tool-shape #2 — JSON dict the way most MCP servers emit."""

    return {"name": name, "description": "..."}


class _AttrTool:
    """Tool-shape #3 — object with a ``.name`` attribute (mcp.types.Tool-style)."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __eq__(self, other: object) -> bool:  # pragma: no cover - debug aid
        return isinstance(other, _AttrTool) and self.name == other.name

    def __hash__(self) -> int:  # pragma: no cover - debug aid
        return hash(("_AttrTool", self.name))

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"_AttrTool({self.name!r})"


def _wrap_in_random_shape(name: str, shape_index: int) -> Any:
    """Wrap ``name`` in shape #1, #2, or #3 deterministically."""

    if shape_index == 0:
        return _as_string(name)
    if shape_index == 1:
        return _as_dict(name)
    return _AttrTool(name)


@st.composite
def _tool_catalogs(draw: st.DrawFn) -> list[Any]:
    """Build a mixed catalog of allowed + banned tools across all 3 shapes.

    The catalog is a list of arbitrary length (0–30) where each entry
    is independently chosen as allowed-or-banned and wrapped in one
    of the three supported shapes. This is the most general input
    space :func:`filter_tools` is expected to handle.
    """

    n = draw(st.integers(min_value=0, max_value=30))
    catalog: list[Any] = []
    for _ in range(n):
        # 30% banned, 70% allowed — keeps banned entries densely
        # represented without crowding out the allowed branch.
        is_banned = draw(st.booleans()) and draw(st.booleans())
        name = draw(_banned_names) if is_banned else draw(_allowed_names)
        shape = draw(st.integers(min_value=0, max_value=2))
        catalog.append(_wrap_in_random_shape(name, shape))
    return catalog


def _name_of(tool: Any) -> str | None:
    """Return the inspectable name of a tool, mirroring filter_tools' logic."""

    if isinstance(tool, str):
        return tool
    if isinstance(tool, dict):
        n = tool.get("name")
        return n if isinstance(n, str) else None
    n = getattr(tool, "name", None)
    return n if isinstance(n, str) else None


# ---------------------------------------------------------------------------
# Banned tool list invariants
# ---------------------------------------------------------------------------


class TestFilterToolsBannedTools:
    """``filter_tools`` strips banned tools across all catalog shapes.

    """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(catalog=_tool_catalogs())
    def test_output_names_are_disjoint_from_banned_tools(
        self, catalog: list[Any]
    ) -> None:
        """For any catalog, the set of names in
        :func:`filter_tools`'s output has empty intersection with
        :data:`BANNED_TOOLS`. This is the universally quantified
        banned-tool policy statement.
        """

        result = filter_tools(catalog)
        result_names = {_name_of(t) for t in result if _name_of(t) is not None}
        assert result_names.isdisjoint(BANNED_TOOLS)

    @settings(max_examples=200, deadline=2000)
    @given(catalog=_tool_catalogs())
    def test_filter_tools_is_idempotent(self, catalog: list[Any]) -> None:
        """Filtering an already-filtered catalog is a no-op: the
        operation reaches a fixed point in one step, so chaining
        through multiple LLM call sites is safe.
        """

        once = filter_tools(catalog)
        twice = filter_tools(once)
        assert once == twice

    @settings(max_examples=200, deadline=2000)
    @given(catalog=_tool_catalogs())
    def test_filter_tools_preserves_relative_order(
        self, catalog: list[Any]
    ) -> None:
        """Allowed tools appear in the output in the same order they
        appeared in the input. Order matters for UI rendering — the
        LLM tool catalog is often shown to operators in the original
        listing order.
        """

        expected_kept = [t for t in catalog if _name_of(t) not in BANNED_TOOLS]
        assert filter_tools(catalog) == expected_kept

    @settings(max_examples=200, deadline=2000)
    @given(catalog=_tool_catalogs())
    def test_filter_tools_output_is_subset_of_input(
        self, catalog: list[Any]
    ) -> None:
        """Every entry in the filtered output came from the input —
        :func:`filter_tools` never *adds* a tool. The check uses
        ``id``-based identity for shape #3 (objects) and value
        equality for shapes #1 / #2.
        """

        result = filter_tools(catalog)
        # ``in`` on a list uses ``__eq__``; for ``_AttrTool`` we fall
        # back to identity. We accept either.
        for tool in result:
            assert any(tool is t or tool == t for t in catalog)

    @settings(max_examples=200, deadline=2000)
    @given(catalog=_tool_catalogs())
    def test_filter_tools_size_never_exceeds_input(
        self, catalog: list[Any]
    ) -> None:
        """:func:`filter_tools` is a *strip* operation; it can never
        produce more entries than it received. Combined with the
        subset-of-input property this rules out duplication.
        """

        assert len(filter_tools(catalog)) <= len(catalog)

    @settings(max_examples=200, deadline=2000)
    @given(
        catalog=_tool_catalogs(),
        banned=_banned_names,
        shape=st.integers(min_value=0, max_value=2),
    )
    def test_inserting_a_banned_tool_does_not_increase_filtered_size(
        self, catalog: list[Any], banned: str, shape: int
    ) -> None:
        """Inserting a banned tool anywhere into the catalog cannot
        increase the size of the filtered output — the inserted
        entry is, by definition, dropped. This is a stronger version
        of the "banned ∩ output == ∅" property: it ties the act of
        adding a banned tool to a measurable invariant.
        """

        baseline = filter_tools(catalog)
        injected_tool = _wrap_in_random_shape(banned, shape)
        # Any insertion position is fine; pick the middle so we
        # exercise both prefix and suffix preservation.
        midpoint = len(catalog) // 2
        injected_catalog = catalog[:midpoint] + [injected_tool] + catalog[midpoint:]
        injected_result = filter_tools(injected_catalog)
        assert len(injected_result) == len(baseline)
        assert injected_result == baseline

    @settings(max_examples=100, deadline=2000)
    @given(name=_allowed_names, shape=st.integers(min_value=0, max_value=2))
    def test_singleton_allowed_tool_passes_through(
        self, name: str, shape: int
    ) -> None:
        """A singleton catalog containing only an allowed tool is
        returned unchanged. This pins the "no false positives" branch
        of the property.
        """

        tool = _wrap_in_random_shape(name, shape)
        result = filter_tools([tool])
        assert len(result) == 1
        assert _name_of(result[0]) == name

    @settings(max_examples=100, deadline=2000)
    @given(name=_banned_names, shape=st.integers(min_value=0, max_value=2))
    def test_singleton_banned_tool_is_dropped(
        self, name: str, shape: int
    ) -> None:
        """A singleton catalog containing only a banned tool is filtered
        to an empty list — the "no false negatives" pin for the
        property. Combined with the previous test it covers both
        sides of the predicate completely.
        """

        tool = _wrap_in_random_shape(name, shape)
        assert filter_tools([tool]) == []

    @settings(max_examples=200, deadline=2000)
    @given(catalog=_tool_catalogs())
    def test_filter_tools_returns_a_new_list(
        self, catalog: list[Any]
    ) -> None:
        """:func:`filter_tools` always returns a freshly constructed list
        so callers can mutate the result without aliasing the input.
        Defensive copy semantics protect concurrent LLM call paths.
        """

        result = filter_tools(catalog)
        assert isinstance(result, list)
        assert result is not catalog


class TestBannedToolsConstant:
    """Static invariants on :data:`BANNED_TOOLS` itself.

    """

    def test_banned_tools_has_canonical_members(self) -> None:
        """``bitbucket_merge_pr`` and ``confluence_delete_page`` are the canonical pair.

        This test pins them so a future PR cannot quietly
        shrink the set without updating the policy.
        """

        assert "bitbucket_merge_pr" in BANNED_TOOLS
        assert "confluence_delete_page" in BANNED_TOOLS

    def test_banned_tools_is_frozenset(self) -> None:
        """``frozenset`` keeps the constant immutable and hashable —
        prerequisites for using it as the single source of truth
        across multiple LLM call sites.
        """

        assert isinstance(BANNED_TOOLS, frozenset)
