"""Property test P6 — Tool tag shape invariant.

Validates Requirements 42.1, 42.2, 42.3, 42.4 / design Property 6:
every tool registered by the DC-parity spec on one of the three FastMCP
server instances (``bitbucket_mcp``, ``jira_mcp``, ``confluence_mcp``)
MUST carry a ``.tags`` set that obeys the documented shape:

1. Exactly one product tag from ``{"bitbucket", "jira", "confluence"}``
   (Req 42.4).
2. Exactly one read/write tag — ``"read"`` XOR ``"write"`` (Req 42.3).
3. Exactly one ``toolset:<name>`` tag (Req 42.1).
4. The ``<name>`` suffix of the ``toolset:*`` tag MUST be a registered
   toolset in :data:`mcp_atlassian.utils.toolsets.ALL_TOOLSETS` (this is
   the truth source consulted by ``get_enabled_toolsets`` for
   Req 42.2 — operators enabling a subset by name would silently drop
   tools whose toolset tags don't appear in the registry).

Discovery strategy
------------------
FastMCP exposes all registered tools via the async ``get_tools()``
method on each server instance, returning a ``dict[str, Tool]`` keyed
by tool name, where each ``Tool`` exposes a ``.tags: set[str]``
attribute. We collect the three dicts once at module import time via a
fresh event loop (mirroring the approach already used in
``tests/unit/utils/test_toolsets.py``) and pyparametrize the resulting
``(server, tool_name)`` pairs so pytest emits one test item per tool
for pinpoint diagnostics on failure.

The test therefore covers EVERY registered tool — not just the tools
added by this feature. This is intentional: the shape invariant is
already true for pre-existing tools, and enforcing it globally guards
against regressions where a future tool is registered without a
toolset tag (Req 42.1) or with more than one product tag.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mcp_atlassian.servers.bitbucket import bitbucket_mcp
from mcp_atlassian.servers.confluence import confluence_mcp
from mcp_atlassian.servers.jira import jira_mcp
from mcp_atlassian.utils.toolsets import (
    ALL_TOOLSETS,
    TOOLSET_TAG_PREFIX,
)


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------

_PRODUCT_TAGS: frozenset[str] = frozenset({"bitbucket", "jira", "confluence"})
_READ_WRITE_TAGS: frozenset[str] = frozenset({"read", "write"})


def _collect_tools() -> dict[str, dict[str, Any]]:
    """Collect registered tools from all three FastMCP server instances.

    Returns a mapping of ``{server_label: {tool_name: tool_obj}}`` where
    each ``tool_obj`` exposes a ``.tags`` attribute. Uses a fresh event
    loop per call so pytest's own loop policy doesn't interfere with
    import-time discovery.
    """
    servers: dict[str, Any] = {
        "bitbucket": bitbucket_mcp,
        "jira": jira_mcp,
        "confluence": confluence_mcp,
    }

    async def _gather() -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for label, server in servers.items():
            result[label] = await server.get_tools()
        return result

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_gather())
    finally:
        loop.close()


_TOOLS_BY_SERVER: dict[str, dict[str, Any]] = _collect_tools()


def _parametrized_tools() -> list[pytest.param]:
    """Flatten the tools map into a pytest-parametrize list.

    Emits one parametrize entry per ``(server_label, tool_name)`` pair
    so a failing tool surfaces with an id like
    ``bitbucket::create_webhook`` rather than being aggregated into a
    single loop assertion.
    """
    params: list[pytest.param] = []
    for server_label, tools in _TOOLS_BY_SERVER.items():
        for tool_name in sorted(tools.keys()):
            params.append(
                pytest.param(
                    server_label,
                    tool_name,
                    id=f"{server_label}::{tool_name}",
                )
            )
    return params


# ---------------------------------------------------------------------------
# Sanity: discovery actually found tools on all three servers
# ---------------------------------------------------------------------------


def test_tool_discovery_finds_tools_on_all_three_servers() -> None:
    """Guard against a silently-empty discovery — a dict with zero tools
    would make the parametrize list empty and the shape checks below
    would pass vacuously. Require at least one tool per server."""
    for server_label, tools in _TOOLS_BY_SERVER.items():
        assert len(tools) > 0, (
            f"No tools discovered on '{server_label}' server — "
            f"shape-invariant checks would pass vacuously."
        )


# ---------------------------------------------------------------------------
# Property 6 — per-tool tag shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("server_label", "tool_name"),
    _parametrized_tools(),
)
def test_tool_has_exactly_one_product_tag(
    server_label: str, tool_name: str
) -> None:
    """P6.a (Req 42.4): exactly one tag from {bitbucket, jira, confluence}.

    The product tag also MUST match the server the tool is registered
    on — a Jira tool on the Bitbucket server would break downstream
    filter logic that trusts ``jira``-tagged tools to live on
    ``jira_mcp`` (and vice versa).
    """
    tool = _TOOLS_BY_SERVER[server_label][tool_name]
    tags: set[str] = set(getattr(tool, "tags", set()) or set())

    product_tags = tags & _PRODUCT_TAGS
    assert len(product_tags) == 1, (
        f"Tool '{tool_name}' on '{server_label}' has {len(product_tags)} "
        f"product tags (expected 1): {sorted(product_tags)!r} "
        f"(full tag set: {sorted(tags)!r})"
    )

    # Product tag must match the server it's registered on.
    (product_tag,) = product_tags
    assert product_tag == server_label, (
        f"Tool '{tool_name}' on '{server_label}' server carries product "
        f"tag {product_tag!r} — product tag must match the host server."
    )


@pytest.mark.parametrize(
    ("server_label", "tool_name"),
    _parametrized_tools(),
)
def test_tool_has_exactly_one_read_or_write_tag(
    server_label: str, tool_name: str
) -> None:
    """P6.b (Req 42.3): exactly one of {"read", "write"} — XOR.

    Both tags set, or neither set, would break the ``READ_ONLY_MODE``
    filter in ``_list_tools_mcp`` which partitions tools on this
    boolean.
    """
    tool = _TOOLS_BY_SERVER[server_label][tool_name]
    tags: set[str] = set(getattr(tool, "tags", set()) or set())

    rw_tags = tags & _READ_WRITE_TAGS
    assert len(rw_tags) == 1, (
        f"Tool '{tool_name}' on '{server_label}' has {len(rw_tags)} "
        f"read/write tags (expected exactly 1 of "
        f"{{'read','write'}}): {sorted(rw_tags)!r} "
        f"(full tag set: {sorted(tags)!r})"
    )


@pytest.mark.parametrize(
    ("server_label", "tool_name"),
    _parametrized_tools(),
)
def test_tool_has_exactly_one_toolset_tag(
    server_label: str, tool_name: str
) -> None:
    """P6.c (Req 42.1): exactly one ``toolset:<name>`` tag.

    Tools missing a toolset tag can't be opted in/out by operators
    (Req 42.2); tools carrying more than one are ambiguous for the
    ``should_include_tool_by_toolset`` gate.
    """
    tool = _TOOLS_BY_SERVER[server_label][tool_name]
    tags: set[str] = set(getattr(tool, "tags", set()) or set())

    toolset_tags = [t for t in tags if t.startswith(TOOLSET_TAG_PREFIX)]
    assert len(toolset_tags) == 1, (
        f"Tool '{tool_name}' on '{server_label}' has {len(toolset_tags)} "
        f"toolset tags (expected 1, matching '{TOOLSET_TAG_PREFIX}<name>'): "
        f"{toolset_tags!r} (full tag set: {sorted(tags)!r})"
    )


@pytest.mark.parametrize(
    ("server_label", "tool_name"),
    _parametrized_tools(),
)
def test_toolset_tag_suffix_matches_registered_toolset(
    server_label: str, tool_name: str
) -> None:
    """P6.d (Req 42.2): the ``<name>`` portion must be a known toolset.

    ``ALL_TOOLSETS`` is the truth source consulted by
    :func:`get_enabled_toolsets`. A tool tagged with an unknown toolset
    name would be silently dropped when operators enable a specific
    subset, breaking Req 42.2.
    """
    tool = _TOOLS_BY_SERVER[server_label][tool_name]
    tags: set[str] = set(getattr(tool, "tags", set()) or set())

    toolset_tags = [t for t in tags if t.startswith(TOOLSET_TAG_PREFIX)]
    # P6.c asserts len == 1 above; if that assertion failed this test
    # would fail with an IndexError which is fine — but guard with a
    # clearer message in case tests run out of order or selectively.
    assert toolset_tags, (
        f"Tool '{tool_name}' on '{server_label}' has no toolset tag; "
        f"cannot validate suffix against ALL_TOOLSETS."
    )
    toolset_name = toolset_tags[0][len(TOOLSET_TAG_PREFIX) :]

    assert toolset_name in ALL_TOOLSETS, (
        f"Tool '{tool_name}' on '{server_label}' has toolset tag "
        f"'{TOOLSET_TAG_PREFIX}{toolset_name}' which is NOT registered "
        f"in ALL_TOOLSETS. Known toolsets: "
        f"{sorted(ALL_TOOLSETS.keys())!r}"
    )
