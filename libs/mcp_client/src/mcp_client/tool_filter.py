"""Banned MCP tool list.

The :data:`BANNED_TOOLS` constant is the **single source of truth** for
the tools the LLM is never allowed to call. Anything that hands a tool
catalog to an LLM (assistant-service, agent-runner-worker, future
admin-dashboard tooling) must route the catalog through
:func:`filter_tools` first.

Two members are always banned:

- ``bitbucket_merge_pr`` - merging a pull request is a human
  decision; coupling it with always-draft PR creation keeps the platform
  from auto-merging anything an LLM produces.
- ``confluence_delete_page`` - page deletes are irreversible at the
  Confluence API level. Probe artifacts are cleaned up via the
  admin "Probe Artifacts" UI, never via this MCP tool.

Adding a tool to this list is intentionally a *code* change so the
review trail is preserved and tests catch any drift.
"""

from __future__ import annotations

from typing import Any, Final, Iterable

# ---------------------------------------------------------------------------
# Banned tool list
# ---------------------------------------------------------------------------

#: Tools the LLM is *never* allowed to invoke.
#:
#: ``frozenset`` is used so the constant is hashable, immutable, and
#: O(1) for the membership checks performed by :func:`filter_tools`.
BANNED_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "bitbucket_merge_pr",
        "confluence_delete_page",
    }
)


# ---------------------------------------------------------------------------
# filter_tools - strip banned tools from a catalog
# ---------------------------------------------------------------------------


def _tool_name(tool: Any) -> str | None:
    """Return the tool's ``name`` regardless of how it is shaped.

    The MCP catalog can hand us tools as plain strings (eg. when only
    names are listed for a UI), as ``dict`` payloads (the JSON shape
    most MCP servers emit), or as objects with a ``.name`` attribute
    (eg. an ``mcp.types.Tool`` instance).

    Returning ``None`` lets the caller decide what to do with shapes
    we cannot inspect - :func:`filter_tools` keeps such entries
    untouched (they cannot match a banned name and therefore cannot be
    a banned tool).
    """

    if isinstance(tool, str):
        return tool
    if isinstance(tool, dict):
        name = tool.get("name")
        return name if isinstance(name, str) else None
    name = getattr(tool, "name", None)
    return name if isinstance(name, str) else None


def filter_tools(tools: Iterable[Any]) -> list[Any]:
    """Return ``tools`` with every entry whose name is in
    :data:`BANNED_TOOLS` removed.

    The function is intentionally tolerant of catalog shape - see
    :func:`_tool_name` for the supported input forms. Tools whose name
    we cannot inspect are kept as-is; they cannot match a banned name
    and therefore cannot violate the ban.

    Args:
        tools: An iterable of tool descriptors. Strings, ``dict``
            payloads, and objects with a ``.name`` attribute are all
            understood. The iterable is materialised into a list, so
            generators are exhausted by this call.

    Returns:
        A new ``list`` containing every input entry whose name is not
        in :data:`BANNED_TOOLS`. Original ordering is preserved.

    Raises:
        TypeError: If ``tools`` is not iterable.

    Example::

        >>> filter_tools(
        ...     [
        ...         "jira_get_issue",
        ...         "bitbucket_merge_pr",
        ...         {"name": "confluence_delete_page"},
        ...     ]
        ... )
        ['jira_get_issue']

    Notes:
        :data:`BANNED_TOOLS` is the *single source of truth*. Callers must not maintain a private deny
        list - adding a tool there is intentionally a code change so
        tests catch any drift.
    """

    return [tool for tool in tools if _tool_name(tool) not in BANNED_TOOLS]
