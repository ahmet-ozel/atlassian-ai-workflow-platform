"""Property test 3 — UI banned tool list filtering parite.

**Validates: Requirements 1.2, 3.3**

The Streamlit Explorer page (read-only direct MCP) and the chat
page (proxy through assistant-service) MUST never expose a tool
in the foundation banned-tool list. The foundation's
``mcp_client.filter_tools`` is the authoritative gate; this
property test scans the page source for hard-coded references to
banned tools (eg. ``"bitbucket_merge_pr"``) — finding any string
literal naming a banned tool is a regression that bypasses the
filter.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_STREAMLIT_PAGES = (
    Path(__file__).resolve().parent.parent.parent
    / "ui"
    / "streamlit-app"
    / "pages"
)

#: Tools the foundation forbids. Mirrors
#: ``libs/mcp_client/src/mcp_client/banned.py`` (Spec 1 R7.2).
_BANNED_TOOLS = (
    "bitbucket_merge_pr",
    "confluence_delete_page",
)

_PAGES = (
    "1_chat.py",
    "2_task_creator.py",
    "3_explorer.py",
)


@pytest.mark.parametrize("page_name", _PAGES)
def test_no_banned_tool_string_literal(page_name: str) -> None:
    page = _STREAMLIT_PAGES / page_name
    if not page.is_file():
        pytest.skip(f"{page_name} not present yet")

    source = page.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Skip the module docstring — design notes legitimately mention
    # banned tool names. Only assert on string literals that appear
    # *inside* code (function bodies, list literals, expression
    # statements that are not the module docstring).
    docstring_node = (
        tree.body[0]
        if tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
        else None
    )

    leaks: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if node is docstring_node or (
            docstring_node is not None and node is docstring_node.value
        ):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for banned in _BANNED_TOOLS:
                # Whole-word match so "bitbucket_merge_pr_audit" stays
                # safe.
                if re.search(rf"\b{re.escape(banned)}\b", node.value):
                    leaks.append((banned, node.lineno))

    assert not leaks, (
        f"{page_name} hard-codes banned tool name(s) {leaks!r}; the "
        "foundation `mcp_client.filter_tools` must be the only place "
        "that decides which tools are visible. Drop the literal."
    )
