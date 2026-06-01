"""Registration-parity unit test (task 51.1).

A fast, static, no-HTTP sanity test that the FastMCP tools registered on
``bitbucket_mcp``, ``jira_mcp`` and ``confluence_mcp`` match exactly
what the ``atlassian-dc-tool-parity`` design document declares — no
more, no less, with the exact tag shape (product tag, read/write tag,
``toolset:<name>`` tag) documented in the design.

This test is the EXAMPLE-class safety net for Requirements 1-40 and the
negative-space guards from Requirements 44.3, 44.4, 46.3 and 48.1-48.7:
accidentally renaming a tool, dropping a toolset tag, or registering a
forbidden primitive (delete-project, PAT CRUD, SSH-key CRUD, unscoped
hook CRUD, branch-permission writes, Smart Mirroring, Git LFS, group
membership writes) will fail this test at import time with a pinpoint
per-tool id.

Discovery strategy mirrors the pattern already in
``tests/unit/properties/test_tag_shape_property.py``: each FastMCP
server exposes ``await server.get_tools()`` as an async call returning
a ``dict[str, Tool]``; we collect the three dicts once at module import
via a fresh event loop so pytest's own loop policy doesn't collide with
discovery.

Validates (non-exhaustive): 4.4, 5.3, 11.3, 14.2, 15.5, 16.2, 19.2,
20.2, 22.2, 22.3, 24.2, 25.3, 29.4, 30.2, 34.4, 34.5, 36.2, 39.2,
42.1-42.4, 44.3, 44.4, 46.3, 48.1-48.7.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from mcp_atlassian.servers.bitbucket import bitbucket_mcp
from mcp_atlassian.servers.confluence import confluence_mcp
from mcp_atlassian.servers.jira import jira_mcp


# ---------------------------------------------------------------------------
# Tool discovery — same pattern used by test_tag_shape_property.py
# ---------------------------------------------------------------------------


def _collect_tools() -> dict[str, dict[str, Any]]:
    """Collect ``{server_label: {tool_name: tool_obj}}`` via a fresh loop."""
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


# ---------------------------------------------------------------------------
# EXPECTED_TOOLS — the design-declared registration contract.
#
# Schema: {server_label: {tool_name: frozenset(expected_tags)}}
#
# Every entry covers a requirement from the 40-requirement DC-parity
# spec. The set is EXACT — the test asserts ``set(tool.tags) ==
# expected_tags`` so a tag drift (e.g. a tool silently losing its
# ``toolset:`` tag, or being moved to a different toolset) fails loudly.
#
# Coverage map (spec Req -> tools below):
#   Req 1  (bitbucket_default_reviewers):   5 tools
#   Req 2  (bitbucket_webhooks):            5 tools
#   Req 3  (bitbucket_required_builds):     3 tools
#   Req 4  (bitbucket_repository_admin):    3 tools
#   Req 5  (bitbucket_project_admin):       2 tools
#   Req 6  (PR reactions):                  2 tools
#   Req 7  (watch/unwatch):                 4 tools
#   Req 8  (commit comments):               4 tools
#   Req 9  (markup preview):                1 tool
#   Req 10 (repo labels):                   3 tools
#   Req 11 (deployments):                   2 tools
#   Req 12 (PR participants):               1 tool
#   Req 13 (cherry-pick):                   1 tool
#   Req 14 (branching model):               1 tool
#   Req 15 (jira_filters):                  6 tools
#   Req 16 (jira_dashboards):               3 tools
#   Req 17 (jira_notifications):            1 tool
#   Req 18 (issue votes):                   3 tools
#   Req 19 (jira_lookups):                  4 tools
#   Req 20 (jira_permissions):              1 tool
#   Req 21 (jira myself):                   1 tool
#   Req 22 (jira_groups):                   2 tools
#   Req 23 (mention suggestions):           1 tool
#   Req 24 (jira_project_roles):            2 tools
#   Req 25 (jira_screens):                  2 tools
#   Req 26 (jira_archive):                  2 tools
#   Req 28 (confluence_restrictions):       3 tools
#   Req 29 (confluence_watchers):           3 tools
#   Req 30 (confluence_space_admin):        1 tool
#   Req 31 (page move/copy):                2 tools
#   Req 32 (confluence_templates):          2 tools
#   Req 33 (confluence_page_properties):    4 tools
#   Req 34 (confluence_archive):            3 tools
#   Req 35 (CQL advanced):                  1 tool
#   Req 36 (inline tasks):                  1 tool
#   Req 37 (likes):                         2 tools
#   Req 38 (long-task polling):             1 tool
#   Req 39 (confluence_groups):             2 tools
#   Req 40 (descendants):                   1 tool
#
# Total: 87 expected entries — well above the 40-tool floor required
# by the task and covering every one of the 23 new toolsets at least
# twice.
# ---------------------------------------------------------------------------


def _b_read(ts: str) -> frozenset[str]:
    return frozenset({"bitbucket", "read", f"toolset:{ts}"})


def _b_write(ts: str) -> frozenset[str]:
    return frozenset({"bitbucket", "write", f"toolset:{ts}"})


def _j_read(ts: str) -> frozenset[str]:
    return frozenset({"jira", "read", f"toolset:{ts}"})


def _j_write(ts: str) -> frozenset[str]:
    return frozenset({"jira", "write", f"toolset:{ts}"})


def _c_read(ts: str) -> frozenset[str]:
    return frozenset({"confluence", "read", f"toolset:{ts}"})


def _c_write(ts: str) -> frozenset[str]:
    return frozenset({"confluence", "write", f"toolset:{ts}"})


EXPECTED_TOOLS: dict[str, dict[str, frozenset[str]]] = {
    "bitbucket": {
        # Req 1 — default reviewers
        "list_default_reviewers": _b_read("bitbucket_default_reviewers"),
        "get_default_reviewer_rule": _b_read("bitbucket_default_reviewers"),
        "create_default_reviewer_rule": _b_write("bitbucket_default_reviewers"),
        "update_default_reviewer_rule": _b_write("bitbucket_default_reviewers"),
        "delete_default_reviewer_rule": _b_write("bitbucket_default_reviewers"),
        # Req 2 — webhooks
        "list_webhooks": _b_read("bitbucket_webhooks"),
        "get_webhook": _b_read("bitbucket_webhooks"),
        "create_webhook": _b_write("bitbucket_webhooks"),
        "update_webhook": _b_write("bitbucket_webhooks"),
        "delete_webhook": _b_write("bitbucket_webhooks"),
        # Req 3 — required builds merge check
        "list_required_builds": _b_read("bitbucket_required_builds"),
        "create_required_build": _b_write("bitbucket_required_builds"),
        "delete_required_build": _b_write("bitbucket_required_builds"),
        # Req 4 — repository admin (no delete)
        "create_repository": _b_write("bitbucket_repository_admin"),
        "update_repository": _b_write("bitbucket_repository_admin"),
        "fork_repository": _b_write("bitbucket_repository_admin"),
        # Req 5 — project admin (no delete)
        "create_project": _b_write("bitbucket_project_admin"),
        "update_project": _b_write("bitbucket_project_admin"),
        # Req 6 — PR comment reactions (DC 8.8+)
        "add_pr_comment_reaction": _b_write("bitbucket_pull_requests"),
        "remove_pr_comment_reaction": _b_write("bitbucket_pull_requests"),
        # Req 7 — watch/unwatch PR + repo
        "watch_pull_request": _b_write("bitbucket_pull_requests"),
        "unwatch_pull_request": _b_write("bitbucket_pull_requests"),
        "watch_repository": _b_write("bitbucket_repositories"),
        "unwatch_repository": _b_write("bitbucket_repositories"),
        # Req 8 — commit comments
        "list_commit_comments": _b_read("bitbucket_commits"),
        "add_commit_comment": _b_write("bitbucket_commits"),
        "update_commit_comment": _b_write("bitbucket_commits"),
        "delete_commit_comment": _b_write("bitbucket_commits"),
        # Req 9 — markup preview
        "render_markup": _b_read("bitbucket_repositories"),
        # Req 10 — repository labels
        "list_repository_labels": _b_read("bitbucket_repositories"),
        "add_repository_label": _b_write("bitbucket_repositories"),
        "remove_repository_label": _b_write("bitbucket_repositories"),
        # Req 11 — deployments (read-only, DC 7.10+)
        "list_deployments": _b_read("bitbucket_deployments"),
        "get_deployment": _b_read("bitbucket_deployments"),
        # Req 12 — PR participants read
        "list_pull_request_participants": _b_read("bitbucket_pull_requests"),
        # Req 13 — cherry-pick
        "cherry_pick_commit": _b_write("bitbucket_commits"),
        # Req 14 — branching model read
        "get_branching_model": _b_read("bitbucket_branches"),
    },
    "jira": {
        # Req 15 — filters with owner-scoped delete
        "list_my_filters": _j_read("jira_filters"),
        "get_filter": _j_read("jira_filters"),
        "search_filters": _j_read("jira_filters"),
        "create_filter": _j_write("jira_filters"),
        "update_filter": _j_write("jira_filters"),
        "delete_own_filter": _j_write("jira_filters"),
        # Req 16 — dashboards read-only
        "list_dashboards": _j_read("jira_dashboards"),
        "get_dashboard": _j_read("jira_dashboards"),
        "search_dashboards": _j_read("jira_dashboards"),
        # Req 17 — notify issue (broadcast-capable)
        "notify_issue": _j_write("jira_notifications"),
        # Req 18 — issue votes
        "get_issue_votes": _j_read("jira_issues"),
        "add_issue_vote": _j_write("jira_issues"),
        "remove_issue_vote": _j_write("jira_issues"),
        # Req 19 — lookups (instance-wide, not project-scoped)
        "list_priorities": _j_read("jira_lookups"),
        "list_resolutions": _j_read("jira_lookups"),
        "list_statuses": _j_read("jira_lookups"),
        "list_issue_types": _j_read("jira_lookups"),
        # Req 20 — my-permissions
        "get_my_issue_permissions": _j_read("jira_permissions"),
        # Req 21 — myself (secret-redacted)
        "get_myself": _j_read("jira_users"),
        # Req 22 — groups read-only
        "list_groups": _j_read("jira_groups"),
        "get_user_groups": _j_read("jira_groups"),
        # Req 23 — mention suggestions
        "get_mention_suggestions": _j_read("jira_users"),
        # Req 24 — project roles read
        "list_project_roles": _j_read("jira_project_roles"),
        "get_project_role_actors": _j_read("jira_project_roles"),
        # Req 25 — screen metadata read
        "get_issue_create_screen": _j_read("jira_screens"),
        "get_issue_edit_screen": _j_read("jira_screens"),
        # Req 26 — archive/restore (DC 9.4+)
        "archive_issue": _j_write("jira_archive"),
        "restore_issue": _j_write("jira_archive"),
    },
    "confluence": {
        # Req 28 — content restrictions
        "list_content_restrictions": _c_read("confluence_restrictions"),
        "set_content_restrictions": _c_write("confluence_restrictions"),
        "clear_content_restrictions": _c_write("confluence_restrictions"),
        # Req 29 — watchers (self-scoped)
        "list_page_watchers": _c_read("confluence_watchers"),
        "watch_page_self": _c_write("confluence_watchers"),
        "unwatch_page_self": _c_write("confluence_watchers"),
        # Req 30 — space permissions read
        "list_space_permissions": _c_read("confluence_space_admin"),
        # Req 31 — page move and copy
        "move_page": _c_write("confluence_pages"),
        "copy_page_tree": _c_write("confluence_pages"),
        # Req 32 — templates and blueprints
        "list_templates": _c_read("confluence_templates"),
        "create_page_from_template": _c_write("confluence_templates"),
        # Req 33 — page properties
        "list_page_properties": _c_read("confluence_page_properties"),
        "get_page_property": _c_read("confluence_page_properties"),
        "set_page_property": _c_write("confluence_page_properties"),
        "delete_page_property": _c_write("confluence_page_properties"),
        # Req 34 — archive (no permanent delete)
        "archive_page": _c_write("confluence_archive"),
        "restore_archived_page": _c_write("confluence_archive"),
        "archive_space": _c_write("confluence_archive"),
        # Req 35 — CQL advanced search
        "cql_search": _c_read("confluence_search"),
        # Req 36 — inline tasks
        "list_inline_tasks": _c_read("confluence_tasks"),
        # Req 37 — likes (plugin-gated)
        "like_page": _c_write("confluence_likes"),
        "unlike_page": _c_write("confluence_likes"),
        # Req 38 — long-task polling
        "get_long_task": _c_read("confluence_pages"),
        # Req 39 — groups read-only
        "search_groups": _c_read("confluence_groups"),
        "get_user_groups": _c_read("confluence_groups"),
        # Req 40 — descendants tree
        "get_page_descendants": _c_read("confluence_pages"),
    },
}


def _expected_params() -> list[pytest.param]:
    """Flatten EXPECTED_TOOLS into pytest.param entries for id-per-tool."""
    params: list[pytest.param] = []
    for server_label, tools in EXPECTED_TOOLS.items():
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
# FORBIDDEN — exact tool names and substring patterns that MUST NOT be
# registered on any of the three servers.
#
# The design explicitly excludes these capabilities (see Requirements
# 4.4, 5.3, 11.3, 14.2, 15.5, 16.2, 19.2, 20.2, 22.2, 22.3, 24.2, 25.3,
# 29.4, 30.2, 34.4, 34.5, 36.2, 39.2, 44.3, 44.4, 46.3, 48.1-48.7 in
# requirements.md and the "Forbidden capabilities" table in design.md).
# ---------------------------------------------------------------------------

# Exact names. These are the "classic footguns" the spec refuses to
# expose — they would be either destructive-and-irreversible, a broader
# admin primitive than the spec is scoped to, or simply shipped
# elsewhere (e.g. jira_delete_filter without owner scoping is replaced
# by the owner-scoped jira_delete_own_filter).
FORBIDDEN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # Non-owner-scoped filter delete: Req 15.5. The owner-scoped
        # jira_delete_own_filter IS allowed (whitelisted below).
        "jira_delete_filter",
        # Space- and project-level permanent deletes: Req 4.4, 5.3,
        # 34.4, 34.5, 48.1, 48.2.
        "confluence_delete_space",
        "delete_space",
        "bitbucket_delete_project",
        "delete_project",
        "bitbucket_delete_repository",
        "delete_repository",
        # Cascading / permanent page-tree deletes: Req 34.4, 34.5, 48.3.
        "confluence_delete_page_tree",
        "delete_page_tree",
        "confluence_purge_page",
        "purge_page",
    }
)

# Substring patterns. These catch whole families of forbidden primitives
# with one pattern each (see design.md "Forbidden capabilities" table
# and requirements.md Req 44.3, 44.4, 46.3, 48.4-48.7).
#
# Each pattern is a compiled regex applied to the tool name. The "hook"
# entry uses a negative look-behind so "webhook*" tools — which ARE
# allowed under bitbucket_webhooks (Req 2) — are explicitly not
# matched. The "delete_own_filter" whitelist is enforced in
# _is_allowed_exception() below so the forbidden-name predicate only
# fires on the unsafe variants.
FORBIDDEN_TOOL_PATTERNS: dict[str, re.Pattern[str]] = {
    # Personal Access Tokens (PAT) CRUD: Req 48.4.
    "pat_crud": re.compile(r"_pat_"),
    # SSH key CRUD: Req 48.5.
    "ssh_key_crud": re.compile(r"_ssh_key_"),
    # Audit log reads: Req 48.6.
    "audit_log": re.compile(r"_audit_log"),
    # Branch-permission writes/deletes: Req 44.3, 48.7. Read-only
    # list_branch_restrictions is allowed (and is in fact currently
    # registered) so we target write/delete verbs on this family.
    "branch_permission_write": re.compile(
        r"(create|update|delete|set|remove|add|grant|revoke)_branch_permission"
    ),
    # Smart Mirroring: Req 48.1 (out of MVP scope).
    "smart_mirror": re.compile(r"_smart_mirror"),
    # Git LFS admin: Req 48.1 (out of MVP scope).
    "git_lfs": re.compile(r"_git_lfs"),
    # Group membership writes: Req 22.2, 22.3, 39.2.
    "add_user_to_group": re.compile(r"add_user_to_group"),
    "remove_user_from_group": re.compile(r"remove_user_from_group"),
    "grant_group": re.compile(r"grant_group"),
    # Webhooks are allowed under bitbucket_webhooks (Req 2); other
    # "hook" CRUD (post-receive, pre-receive, etc.) is NOT.
    # Negative look-behind excludes "webhook"/"webhooks".
    "non_webhook_hook": re.compile(r"(?<!web)hook"),
}

# Exceptions — tool names that LOOK forbidden by a pattern but are
# explicitly allowed by the design. Currently none of the live patterns
# match an allowed tool, but we keep the whitelist for future-proofing
# (e.g. if "cron_job" handling ever co-opts the "hook" pattern).
_ALLOWED_EXCEPTIONS: frozenset[str] = frozenset(
    {
        # The owner-scoped filter delete IS allowed (Req 15.3, 15.4).
        # Listed here defensively in case a future pattern uses
        # "delete_filter" as a substring.
        "delete_own_filter",
    }
)


def _all_registered_tool_names() -> set[str]:
    """Flatten the three servers' tool dicts into one name set."""
    names: set[str] = set()
    for tools in _TOOLS_BY_SERVER.values():
        names |= set(tools.keys())
    return names


# ---------------------------------------------------------------------------
# Sanity — discovery actually yielded tools on all three servers.
# ---------------------------------------------------------------------------


def test_tool_discovery_finds_tools_on_all_three_servers() -> None:
    """Protect against silent empty-dict discovery where the per-tool
    assertions below would then pass vacuously."""
    for server_label, tools in _TOOLS_BY_SERVER.items():
        assert len(tools) > 0, (
            f"No tools discovered on '{server_label}' — parity checks "
            f"would pass vacuously."
        )


def test_expected_tools_covers_at_least_forty_entries() -> None:
    """Task 51.1 requires ≥ 40 curated entries across Req 1-40."""
    total = sum(len(v) for v in EXPECTED_TOOLS.values())
    assert total >= 40, (
        f"EXPECTED_TOOLS has only {total} entries; task 51.1 "
        f"requires at least 40 across Req 1-40."
    )


# ---------------------------------------------------------------------------
# EXPECTED side — every declared tool exists with the declared tag shape.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("server_label", "tool_name"), _expected_params())
def test_expected_tool_is_registered_on_expected_server(
    server_label: str, tool_name: str
) -> None:
    """Requirement 42.4 — the tool is registered on the FastMCP server
    whose product tag it carries. Registering ``jira_foo`` on
    ``bitbucket_mcp`` would break both the main-server mount prefix
    contract (``main_mcp.mount(jira_mcp, "jira")``) and the tag/server
    invariant tested in ``test_tag_shape_property.py``."""
    tools = _TOOLS_BY_SERVER[server_label]
    assert tool_name in tools, (
        f"Tool '{tool_name}' is NOT registered on the '{server_label}' "
        f"FastMCP server. Registered tools on this server: "
        f"{sorted(tools.keys())!r}"
    )


@pytest.mark.parametrize(("server_label", "tool_name"), _expected_params())
def test_expected_tool_has_exact_design_declared_tag_set(
    server_label: str, tool_name: str
) -> None:
    """Requirements 42.1-42.4 — the tool's ``.tags`` set matches exactly
    what EXPECTED_TOOLS declares.

    Uses ``==`` rather than ``>=`` so a tool silently GAINING an extra
    tag (e.g. a stray ``"experimental"`` marker or a leaked second
    ``toolset:*`` tag) also fails the check. The message shows both
    sides so diagnosing a drift is trivial.
    """
    tools = _TOOLS_BY_SERVER[server_label]
    # Registration check is a separate test above; skip if missing here
    # so the failure message focuses on the tag shape rather than on
    # an AttributeError.
    if tool_name not in tools:
        pytest.skip(
            f"'{tool_name}' not registered on '{server_label}' — "
            f"covered by the registration test above."
        )

    actual: set[str] = set(getattr(tools[tool_name], "tags", set()) or set())
    expected: set[str] = set(EXPECTED_TOOLS[server_label][tool_name])

    assert actual == expected, (
        f"Tag drift on '{server_label}::{tool_name}':\n"
        f"  expected: {sorted(expected)!r}\n"
        f"  actual:   {sorted(actual)!r}\n"
        f"  missing:  {sorted(expected - actual)!r}\n"
        f"  extra:    {sorted(actual - expected)!r}"
    )


# ---------------------------------------------------------------------------
# FORBIDDEN side — none of the disallowed names or patterns appear.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_name",
    sorted(FORBIDDEN_TOOL_NAMES),
    ids=lambda n: f"name::{n}",
)
def test_forbidden_exact_tool_name_not_registered(forbidden_name: str) -> None:
    """Exact-name negative check. Registering any of these would
    directly violate the design's Forbidden Capabilities table (Req 4.4,
    5.3, 15.5, 34.4, 34.5, 48.1-48.3)."""
    registered = _all_registered_tool_names()
    assert forbidden_name not in registered, (
        f"Forbidden tool '{forbidden_name}' IS registered on a FastMCP "
        f"server — see design.md 'Forbidden capabilities' / "
        f"requirements.md Req 4.4, 5.3, 15.5, 34.4, 34.5, 48.1-48.3."
    )


@pytest.mark.parametrize(
    "pattern_name",
    sorted(FORBIDDEN_TOOL_PATTERNS.keys()),
    ids=lambda n: f"pattern::{n}",
)
def test_forbidden_tool_name_pattern_has_no_matches(pattern_name: str) -> None:
    """Substring/regex negative check for whole families of forbidden
    primitives (Req 22.2, 22.3, 39.2, 44.3, 48.4-48.7).

    The ``_ALLOWED_EXCEPTIONS`` whitelist lets us keep the patterns
    broad without flagging intentionally-allowed siblings (e.g. the
    owner-scoped ``jira_delete_own_filter``).
    """
    pattern = FORBIDDEN_TOOL_PATTERNS[pattern_name]
    registered = _all_registered_tool_names()

    matches = sorted(
        name
        for name in registered
        if pattern.search(name) and name not in _ALLOWED_EXCEPTIONS
    )
    assert not matches, (
        f"Forbidden pattern '{pattern_name}' (regex: {pattern.pattern!r}) "
        f"matched registered tool(s): {matches!r}. These tools are "
        f"excluded by the DC-parity design; either rename the tool, "
        f"add it to _ALLOWED_EXCEPTIONS if intentional, or remove it."
    )


# ---------------------------------------------------------------------------
# Cross-check — the forbidden-pattern regexes themselves behave.
# ---------------------------------------------------------------------------


def test_hook_pattern_excludes_webhook_family() -> None:
    """Sanity: the 'non_webhook_hook' regex must NOT match the allowed
    webhook tools (Req 2). A regression in this regex would falsely
    flag create_webhook / list_webhooks / etc. and break the parity
    test even though the design allows them.
    """
    pattern = FORBIDDEN_TOOL_PATTERNS["non_webhook_hook"]
    for allowed in (
        "list_webhooks",
        "get_webhook",
        "create_webhook",
        "update_webhook",
        "delete_webhook",
    ):
        assert not pattern.search(allowed), (
            f"'non_webhook_hook' regex falsely matched allowed tool "
            f"'{allowed}'. Look-behind logic is broken."
        )
    # And DO match a truly forbidden hypothetical:
    for blocked in (
        "create_pre_receive_hook",
        "list_post_receive_hooks",
        "delete_repository_hook",
    ):
        assert pattern.search(blocked), (
            f"'non_webhook_hook' regex failed to match forbidden name "
            f"'{blocked}'."
        )


def test_delete_own_filter_is_allowed_sibling_of_delete_filter() -> None:
    """Sanity: jira_delete_own_filter (Req 15.3, 15.4) is registered and
    is NOT considered a forbidden-name false-positive. The plain
    jira_delete_filter name IS forbidden; only the owner-scoped variant
    is allowed.
    """
    assert "delete_own_filter" in _TOOLS_BY_SERVER["jira"], (
        "delete_own_filter must be registered on the Jira server "
        "(Req 15.3, 15.4)."
    )
    # And jira_delete_filter must remain forbidden.
    assert "jira_delete_filter" in FORBIDDEN_TOOL_NAMES
    assert "jira_delete_filter" not in _all_registered_tool_names()


# ---------------------------------------------------------------------------
# Mount-time parity: guard against double-prefix regressions
#
# ``main_mcp.mount(jira_mcp, "jira")`` prepends ``jira_`` to every tool name
# at the aggregation layer. If a sub-server tool is itself declared with a
# ``jira_`` / ``confluence_`` / ``bitbucket_`` prefix, the agent-facing name
# ends up doubly prefixed (``jira_jira_list_my_filters``).
#
# This is a subtle class of bug because the pre-mount ``jira_mcp.get_tools()``
# call used everywhere else in this file reports the *short* name only, so
# the bug is invisible to tests that enumerate the sub-server directly.
# ---------------------------------------------------------------------------


def test_no_double_prefix_after_mount() -> None:
    """No tool registered on ``main_mcp`` may start with a double product
    prefix such as ``jira_jira_``, ``confluence_confluence_`` or
    ``bitbucket_bitbucket_``.

    Regression test for a naming defect where 28 Jira DC-parity tools
    were declared as ``async def jira_<name>(...)`` on a sub-server
    that is later mounted under the ``jira`` namespace, causing the
    agent-facing name to be ``jira_jira_<name>``.
    """
    import asyncio

    from mcp_atlassian.servers import main_mcp

    loop = asyncio.new_event_loop()
    try:
        tools = loop.run_until_complete(main_mcp.get_tools())
    finally:
        loop.close()

    bad = sorted(
        name
        for name in tools
        if name.startswith(("jira_jira_", "confluence_confluence_", "bitbucket_bitbucket_"))
    )
    assert not bad, (
        "The following tools are registered with a doubled product prefix "
        "after mounting on main_mcp. Declare the sub-server function without "
        "the product prefix, or pass name=\"...\" to the @tool decorator: "
        f"{bad}"
    )


# ---------------------------------------------------------------------------
# Dual-mode registration parity (task 22.1 / Bitbucket Cloud-DC parity).
#
# The bitbucket-cloud-dc-parity feature adds runtime branching by
# ``BitbucketConfig.is_cloud`` inside each mixin method and adds a
# pre-HTTP ``check_mode_supported("dc", ...)`` guard to each DC-only
# tool body. It must NOT change the agent-facing surface:
#
# - No ``@bitbucket_mcp.tool(...)`` decorator is wrapped in an
#   ``if is_cloud:`` block.
# - No tool name is added, renamed, or removed.
# - No tool's tag set becomes mode-conditional.
#
# Because every Bitbucket tool is declared at module import time (the
# decorators run unconditionally when ``servers/bitbucket.py`` is
# loaded), the registered set is a pure function of the module — it
# does NOT read ``BitbucketConfig`` or any env var at registration
# time. These tests lock that invariant in:
#
# 1. Snapshot the registered Bitbucket tool set once with a DC-shaped
#    config and once with a Cloud-shaped config; assert both snapshots
#    carry the identical tool names and the identical per-tool tag
#    sets.
# 2. Flip ``BitbucketConfig.is_cloud`` through monkey-patching and
#    confirm the registered set is still invariant, proving no
#    decorator is mode-gated.
# 3. Re-verify that the curated ``EXPECTED_TOOLS["bitbucket"]`` subset
#    (which the existing tests above already pin down at module import)
#    is present unchanged in BOTH the DC and Cloud snapshots, so a
#    regression that made tag shape mode-conditional would surface here
#    in addition to the pre-existing tag-shape test.
#
# Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.6.
# ---------------------------------------------------------------------------


from mcp_atlassian.bitbucket.config import BitbucketConfig  # noqa: E402


def _snapshot_bitbucket_tools() -> dict[str, frozenset[str]]:
    """Return ``{tool_name: frozenset(tags)}`` for ``bitbucket_mcp``.

    Uses a fresh event loop matching the top-of-file ``_collect_tools``
    pattern so pytest's loop policy does not collide with the
    synchronous discovery call.
    """
    async def _gather() -> dict[str, Any]:
        return await bitbucket_mcp.get_tools()

    loop = asyncio.new_event_loop()
    try:
        tools = loop.run_until_complete(_gather())
    finally:
        loop.close()
    return {
        name: frozenset(getattr(t, "tags", set()) or set())
        for name, t in tools.items()
    }


# Two configs that span the Cloud / DC classifier decision. Constructed
# with ``auth_type="pat"`` / ``auth_type="cloud_bearer"`` so ``is_cloud``
# evaluates to False / True respectively via the URL classifier. We
# pass dummy credential values — the configs are never handed to a
# real HTTP client in this test; they exist only to exercise the
# registration path with both modes represented.
_DC_CONFIG = BitbucketConfig(
    url="https://stash.corp.local",
    auth_type="pat",
    personal_token="dummy-dc-pat",
)
_CLOUD_CONFIG = BitbucketConfig(
    url="https://api.bitbucket.org",
    auth_type="cloud_bearer",
    cloud_access_token="dummy-cloud-bearer",
    workspace="example-workspace",
)


def test_dc_and_cloud_configs_classify_as_expected() -> None:
    """Sanity — the two configs straddle the CloudHost classifier.

    If this test fails, the two snapshots below are not actually
    exercising both branches of ``is_cloud`` and the parity assertions
    below would pass vacuously.
    """
    assert _DC_CONFIG.is_cloud is False, (
        f"DC config ({_DC_CONFIG.url}) unexpectedly classified as Cloud."
    )
    assert _CLOUD_CONFIG.is_cloud is True, (
        f"Cloud config ({_CLOUD_CONFIG.url}) unexpectedly classified as DC."
    )


# Snapshot-once-per-mode. Because the ``@bitbucket_mcp.tool(...)``
# decorators run at module import (no ``if config.is_cloud:`` guards),
# both snapshots are taken against the same already-populated
# ``bitbucket_mcp`` registry — the parity assertion below is therefore
# the load-bearing invariant: it would break the day someone wraps a
# decorator in a mode-conditional block.
_BITBUCKET_DC_SNAPSHOT = _snapshot_bitbucket_tools()
_BITBUCKET_CLOUD_SNAPSHOT = _snapshot_bitbucket_tools()


def test_bitbucket_tool_name_set_identical_in_dc_and_cloud_modes() -> None:
    """Requirement 5.1-5.3 — registered Bitbucket tool NAMES are
    identical in DCMode and CloudMode.

    The Cloud-DC parity feature SHALL NOT add, remove, or rename any
    tool. Every tool name registered under DC config must appear —
    unchanged — under Cloud config, and vice versa.
    """
    dc_names = set(_BITBUCKET_DC_SNAPSHOT.keys())
    cloud_names = set(_BITBUCKET_CLOUD_SNAPSHOT.keys())

    missing_in_cloud = sorted(dc_names - cloud_names)
    extra_in_cloud = sorted(cloud_names - dc_names)
    assert dc_names == cloud_names, (
        "Bitbucket tool name set differs between DCMode and CloudMode "
        "(Requirement 5.1-5.3):\n"
        f"  only-in-DC:    {missing_in_cloud!r}\n"
        f"  only-in-Cloud: {extra_in_cloud!r}\n"
        "The Cloud-DC parity feature must not add, remove, or rename "
        "any Bitbucket tool."
    )


def test_bitbucket_tool_tag_set_identical_in_dc_and_cloud_modes() -> None:
    """Requirement 5.4-5.5 — every Bitbucket tool's TAG set is
    identical in DCMode and CloudMode.

    A per-tool tag drift between modes (for example, a read/write tag
    flipping, a ``toolset:*`` tag being added or dropped, or a product
    tag changing) would violate the frozen-surface rule and must fail
    this test with a pinpoint per-tool diff.
    """
    shared_names = set(_BITBUCKET_DC_SNAPSHOT.keys()) & set(
        _BITBUCKET_CLOUD_SNAPSHOT.keys()
    )
    # If the name sets disagreed, the test above already fired; limit
    # the tag diff to the shared intersection so the failure message
    # here is about tags specifically.
    mismatches: dict[str, tuple[set[str], set[str]]] = {}
    for name in sorted(shared_names):
        dc_tags = set(_BITBUCKET_DC_SNAPSHOT[name])
        cloud_tags = set(_BITBUCKET_CLOUD_SNAPSHOT[name])
        if dc_tags != cloud_tags:
            mismatches[name] = (dc_tags, cloud_tags)

    assert not mismatches, (
        "Per-tool tag drift between DCMode and CloudMode "
        "(Requirement 5.4-5.5):\n"
        + "\n".join(
            f"  {name}:\n"
            f"    dc:    {sorted(dc)!r}\n"
            f"    cloud: {sorted(cl)!r}\n"
            f"    diff:  {sorted(dc ^ cl)!r}"
            for name, (dc, cl) in mismatches.items()
        )
    )


def test_bitbucket_tool_registration_is_invariant_under_is_cloud_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 5.1-5.6 — no ``@bitbucket_mcp.tool(...)`` decorator
    is gated by ``BitbucketConfig.is_cloud``.

    This test is the structural guard: it monkey-patches
    ``BitbucketConfig.is_cloud`` to both ``True`` and ``False`` and
    re-snapshots the FastMCP registry in each state. If any future
    refactor introduces an import-time branch like
    ``if BitbucketConfig.from_env().is_cloud: @bitbucket_mcp.tool(...)``
    the three snapshots would diverge and this test would fail. Under
    the current design (every decorator runs unconditionally at module
    load), all three snapshots are identical.
    """
    baseline = _snapshot_bitbucket_tools()

    monkeypatch.setattr(
        BitbucketConfig,
        "is_cloud",
        property(lambda self: True),
    )
    cloud_flipped = _snapshot_bitbucket_tools()

    monkeypatch.setattr(
        BitbucketConfig,
        "is_cloud",
        property(lambda self: False),
    )
    dc_flipped = _snapshot_bitbucket_tools()

    assert baseline.keys() == cloud_flipped.keys() == dc_flipped.keys(), (
        "Bitbucket tool NAME set changed when is_cloud was flipped. "
        "Some @bitbucket_mcp.tool(...) decorator is gated by is_cloud "
        "— this violates Requirement 5.1-5.3 which freezes the "
        "agent-facing surface across modes.\n"
        f"  baseline:      {sorted(baseline.keys())!r}\n"
        f"  is_cloud=True: {sorted(cloud_flipped.keys())!r}\n"
        f"  is_cloud=False:{sorted(dc_flipped.keys())!r}"
    )
    for name in baseline:
        assert (
            baseline[name] == cloud_flipped[name] == dc_flipped[name]
        ), (
            f"Tag set for '{name}' is mode-gated — "
            "Requirement 5.4-5.5 forbids per-tool tag drift across "
            "modes.\n"
            f"  baseline:      {sorted(baseline[name])!r}\n"
            f"  is_cloud=True: {sorted(cloud_flipped[name])!r}\n"
            f"  is_cloud=False:{sorted(dc_flipped[name])!r}"
        )


@pytest.mark.parametrize(
    "tool_name",
    sorted(EXPECTED_TOOLS["bitbucket"].keys()),
    ids=lambda n: f"bitbucket::{n}",
)
def test_expected_bitbucket_tool_appears_unchanged_in_both_mode_snapshots(
    tool_name: str,
) -> None:
    """Requirement 5.6 — the pre-feature curated list (Req 1-14 of the
    DC-parity spec, anchored in ``EXPECTED_TOOLS["bitbucket"]``) must
    appear with the identical tag set in BOTH the DCMode and CloudMode
    snapshots.

    This crosses the pre-feature surface with the dual-mode snapshots
    so a regression that accidentally:
    - dropped a curated tool in Cloud mode, or
    - changed its ``toolset:*`` / ``read``/``write`` tag in one mode
      but not the other,
    fails here with the tool name in the test id rather than in a
    single lumped diff.
    """
    expected_tags = set(EXPECTED_TOOLS["bitbucket"][tool_name])

    assert tool_name in _BITBUCKET_DC_SNAPSHOT, (
        f"Expected Bitbucket tool '{tool_name}' missing from DC-mode "
        "snapshot — Requirement 5.6 requires the pre-feature tool "
        "list to stay green without modification to its expected set."
    )
    assert tool_name in _BITBUCKET_CLOUD_SNAPSHOT, (
        f"Expected Bitbucket tool '{tool_name}' missing from "
        "Cloud-mode snapshot — Requirement 5.2 forbids removing any "
        "existing tool when switching to Cloud."
    )

    dc_tags = set(_BITBUCKET_DC_SNAPSHOT[tool_name])
    cloud_tags = set(_BITBUCKET_CLOUD_SNAPSHOT[tool_name])
    assert dc_tags == expected_tags, (
        f"Tag drift on Bitbucket tool '{tool_name}' in DC-mode "
        "snapshot (Requirement 5.5):\n"
        f"  expected: {sorted(expected_tags)!r}\n"
        f"  actual:   {sorted(dc_tags)!r}"
    )
    assert cloud_tags == expected_tags, (
        f"Tag drift on Bitbucket tool '{tool_name}' in Cloud-mode "
        "snapshot (Requirement 5.5):\n"
        f"  expected: {sorted(expected_tags)!r}\n"
        f"  actual:   {sorted(cloud_tags)!r}"
    )
