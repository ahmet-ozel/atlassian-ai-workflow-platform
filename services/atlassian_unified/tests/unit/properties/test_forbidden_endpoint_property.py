"""Property test P7 — Forbidden-endpoint exclusion.

Validates design Property 7 and Requirements 4.4, 5.3, 11.3, 14.2,
15.5, 16.2, 19.2, 20.2, 22.2, 22.3, 24.2, 25.3, 29.4, 30.2, 34.4, 34.5,
36.2, 39.2, 44.3, 44.4, 46.3, 48.1-48.7.

Two static invariants are enforced over every tool registered on the
three FastMCP server instances (``bitbucket_mcp``, ``jira_mcp``,
``confluence_mcp``):

    **(a) No forbidden tool names.**
        No registered tool name may match any regex from
        :data:`FORBIDDEN_TOOL_PATTERNS` or any literal in
        :data:`FORBIDDEN_EXACT_NAMES`. The regex set covers whole
        families of disallowed primitives (PAT CRUD, SSH-key CRUD,
        audit-log reads, branch-permission writes, Smart Mirroring,
        Git LFS admin, group-membership writes, pre-/post-receive
        hooks); the exact-name set covers the one-off footguns
        (``delete_project``, ``delete_repository``, ``delete_space``,
        ``jira_delete_filter`` — the non-owner-scoped variant, etc.).
        :data:`EXCEPTIONS` whitelists
        ``jira_delete_own_filter`` — the sole owner-scoped destructive
        variant explicitly allowed by Req 15.3, 15.4, 44.3.

    **(b) Read-only toolsets contain zero write-tagged tools.**
        For every tool whose ``toolset:<name>`` tag suffix is in
        :data:`READ_ONLY_TOOLSETS`, the tool MUST NOT carry the
        ``"write"`` tag. The read-only toolset list mirrors the
        design's Property 7.b declaration and the ``default=False`` /
        description fields in :mod:`mcp_atlassian.utils.toolsets`.

Strategy — this is a *static* check. No HTTP is issued; no fetcher is
constructed. Pytest parametrises over every registered tool so a
regression surfaces with a pinpoint id like
``bitbucket::delete_repository`` rather than as an aggregate loop
failure.

Discovery pattern mirrors :mod:`tests.unit.properties.test_tag_shape_property`:
each FastMCP server exposes ``await server.get_tools()`` as an async
call returning ``dict[str, Tool]``; we collect the three dicts once at
module-import time via a fresh event loop so pytest's own loop policy
doesn't collide with discovery.

A sibling EXAMPLE-class test,
:mod:`tests.unit.servers.test_tool_registration_parity`, covers
invariant (a) against a sibling ``FORBIDDEN_TOOL_NAMES`` list; this
property test lives under ``tests/unit/properties/`` because it
additionally enforces invariant (b) — read-only-toolset purity — which
the registration-parity test does not cover. The two tests share
pattern intent but not literal implementation (different regexes,
different exception lists).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from mcp_atlassian.servers.bitbucket import bitbucket_mcp
from mcp_atlassian.servers.confluence import confluence_mcp
from mcp_atlassian.servers.jira import jira_mcp
from mcp_atlassian.utils.toolsets import TOOLSET_TAG_PREFIX


# ---------------------------------------------------------------------------
# Tool discovery — same pattern used by test_tag_shape_property.py
# ---------------------------------------------------------------------------


def _collect_tools() -> dict[str, dict[str, Any]]:
    """Collect ``{server_label: {tool_name: tool_obj}}`` via a fresh loop.

    A fresh event loop is used so pytest's asyncio plugin doesn't
    interfere with import-time discovery.
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


def _all_registered_tool_names() -> set[str]:
    """Flatten the three servers' tool dicts into a single name set."""
    names: set[str] = set()
    for tools in _TOOLS_BY_SERVER.values():
        names |= set(tools.keys())
    return names


_ALL_NAMES: set[str] = _all_registered_tool_names()


# ---------------------------------------------------------------------------
# Invariant (a) — forbidden tool-name regexes and exact names
# ---------------------------------------------------------------------------

# Regex dict. Each pattern captures a whole family of disallowed
# primitives. Keys are human-readable labels used as the parametrize
# id; values are compiled regexes applied via ``.search()`` to the
# tool name.
#
# Requirement citations per pattern:
#
#   pat_crud                — Req 48.4. PAT (Personal Access Token)
#                             CRUD is out of MVP scope.
#   ssh_key_crud            — Req 48.5. SSH-key CRUD is out of MVP
#                             scope.
#   audit_log               — Req 48.6. Audit-log reads (and writes)
#                             are excluded.
#   branch_permission_write — Req 14.2, 44.3. Branch restrictions are
#                             surfaced read-only only; writes and
#                             deletes are explicitly forbidden.
#                             list_branch_restrictions (read) does NOT
#                             match this pattern.
#   smart_mirror            — Req 48.1. Smart Mirroring config is out
#                             of scope.
#   git_lfs                 — Req 48.1. Git LFS admin is out of scope.
#   add_user_to_group       — Req 22.2, 22.3, 39.2. Group-membership
#                             writes are excluded.
#   remove_user_from_group  — Req 22.2, 22.3, 39.2.
#   grant_group             — Req 22.2, 22.3, 39.2. Permission
#                             grant/revoke on groups is excluded.
#   non_webhook_hook        — Req 2 (allow-list for webhooks) combined
#                             with Req 48.7 (forbid pre-/post-receive
#                             hooks). Negative look-behind on "web"
#                             excludes the allowed ``*_webhook`` CRUD
#                             family (Req 2.1) while still catching
#                             hypothetical ``create_pre_receive_hook``
#                             etc.
#
# All patterns are applied with ``re.Pattern.search`` (substring
# semantics, anchored only where explicitly marked with ``^``/``$``).

FORBIDDEN_TOOL_PATTERNS: dict[str, re.Pattern[str]] = {
    # PAT CRUD — Req 48.4.
    "pat_crud": re.compile(r"_pat_"),
    # SSH-key CRUD — Req 48.5.
    "ssh_key_crud": re.compile(r"_ssh_key_"),
    # Audit log reads/writes — Req 48.6.
    "audit_log": re.compile(r"_audit_log"),
    # Branch-permission writes — Req 14.2, 44.3.
    # Covers create/update/delete/set/remove/add/grant/revoke verbs.
    # The read-only list_branch_restrictions tool is unaffected.
    "branch_permission_write": re.compile(
        r"(create|update|delete|set|remove|add|grant|revoke)_branch_permission"
    ),
    # Smart Mirroring — Req 48.1.
    "smart_mirror": re.compile(r"_smart_mirror"),
    # Git LFS admin — Req 48.1.
    "git_lfs": re.compile(r"_git_lfs"),
    # Group-membership writes — Req 22.2, 22.3, 39.2.
    "add_user_to_group": re.compile(r"add_user_to_group"),
    "remove_user_from_group": re.compile(r"remove_user_from_group"),
    "grant_group": re.compile(r"grant_group"),
    # Pre-/post-receive hook CRUD — Req 48.7.
    # Negative look-behind on "web" ensures the allowed ``*_webhook``
    # tools (Req 2) are NOT matched.
    "non_webhook_hook": re.compile(r"(?<!web)hook"),
}


# Exact tool names that are forbidden. These are the one-off footguns
# — typically destructive-and-irreversible broad-admin primitives —
# where a regex would be needlessly broad.
#
#   delete_project / bitbucket_delete_project        — Req 4.4, 48.2
#   delete_repository / bitbucket_delete_repository  — Req 5.3, 48.2
#   delete_space / confluence_delete_space           — Req 30.2, 34.4
#                                                      (no space delete)
#   delete_page_tree / confluence_delete_page_tree   — Req 34.4
#                                                      (no cascading
#                                                       page-tree delete)
#   purge_page / confluence_purge_page               — Req 34.5
#                                                      (no permanent
#                                                       purge primitive)
#   jira_delete_filter                               — Req 15.5, 44.3.
#                                                      Only the owner-
#                                                      scoped
#                                                      ``jira_delete_own_filter``
#                                                      variant is
#                                                      allowed
#                                                      (see EXCEPTIONS).

FORBIDDEN_EXACT_NAMES: frozenset[str] = frozenset(
    {
        # Repository / project / space permanent deletes.
        "delete_project",
        "bitbucket_delete_project",
        "delete_repository",
        "bitbucket_delete_repository",
        "delete_space",
        "confluence_delete_space",
        # Cascading / permanent page-tree operations.
        "delete_page_tree",
        "confluence_delete_page_tree",
        "purge_page",
        "confluence_purge_page",
        # Non-owner-scoped filter delete. The owner-scoped variant
        # ``jira_delete_own_filter`` is in EXCEPTIONS below.
        "jira_delete_filter",
    }
)


# Whitelist — tools that LOOK forbidden (either via a pattern or an
# exact-name match) but are explicitly allowed by the design. Every
# entry must cite the authorising requirement.
#
#   jira_delete_own_filter  — Req 15.3, 15.4, 44.3. Owner-scoped
#                             destructive tool; resolves the filter's
#                             owner and short-circuits with a
#                             ``not_filter_owner`` StructuredError
#                             before issuing the DELETE for any other
#                             user's filter.

EXCEPTIONS: frozenset[str] = frozenset({"jira_delete_own_filter"})


# ---------------------------------------------------------------------------
# Invariant (b) — read-only toolsets must contain zero write-tagged tools
# ---------------------------------------------------------------------------

# Toolsets declared read-only by the design. A tool whose
# ``toolset:<name>`` tag suffix matches one of these names MUST NOT
# carry the ``"write"`` tag. Requirement citations:
#
#   bitbucket_deployments       — Req 11.1, 11.3. Deployments are
#                                 read-only (DC 7.10+).
#   confluence_space_admin      — Req 30.1, 30.2. Only list_space_permissions;
#                                 no space write primitives.
#   confluence_tasks            — Req 36.1, 36.2. Inline tasks are
#                                 read-only.
#   confluence_groups           — Req 39.1, 39.2. Group discovery is
#                                 read-only; no membership writes.
#   jira_dashboards             — Req 16.1, 16.2. Dashboards are
#                                 discovery-only.
#   jira_lookups                — Req 19.1, 19.2. Instance-wide
#                                 lookups are read-only.
#   jira_permissions            — Req 20.1, 20.2. My-permissions is
#                                 a read endpoint.
#   jira_groups                 — Req 22.1, 22.2, 22.3. Group
#                                 discovery is read-only; no
#                                 membership writes.
#   jira_project_roles          — Req 24.1, 24.2. Project roles are
#                                 read-only.
#   jira_screens                — Req 25.1, 25.3. Screen metadata is
#                                 read-only.

READ_ONLY_TOOLSETS: frozenset[str] = frozenset(
    {
        "bitbucket_deployments",
        "confluence_space_admin",
        "jira_dashboards",
        "jira_lookups",
        "jira_permissions",
        "jira_groups",
        "jira_project_roles",
        "jira_screens",
        "confluence_tasks",
        "confluence_groups",
    }
)


# ---------------------------------------------------------------------------
# Parametrize helpers
# ---------------------------------------------------------------------------


def _parametrized_tools() -> list[pytest.param]:
    """Flatten the tools map into a pytest-parametrize list.

    Emits one parametrize entry per ``(server_label, tool_name)`` pair
    so a failing tool surfaces with an id like
    ``bitbucket::delete_repository`` rather than being aggregated into
    a single loop assertion.
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
# Sanity — discovery and registry non-empty
# ---------------------------------------------------------------------------


def test_tool_discovery_finds_tools_on_all_three_servers() -> None:
    """Guard against silent empty-dict discovery where per-tool
    assertions below would pass vacuously."""
    for server_label, tools in _TOOLS_BY_SERVER.items():
        assert len(tools) > 0, (
            f"No tools discovered on '{server_label}' — forbidden-"
            f"endpoint checks would pass vacuously."
        )


def test_forbidden_pattern_registry_is_non_empty() -> None:
    """Guard against the pattern dict being empty (e.g. cleared
    during refactor)."""
    assert len(FORBIDDEN_TOOL_PATTERNS) > 0, (
        "FORBIDDEN_TOOL_PATTERNS is empty — invariant (a) would pass "
        "vacuously. See Req 48.1-48.7 for the minimum set."
    )
    for essential in (
        "pat_crud",
        "ssh_key_crud",
        "audit_log",
        "branch_permission_write",
    ):
        assert essential in FORBIDDEN_TOOL_PATTERNS, (
            f"Essential forbidden pattern '{essential}' missing."
        )


def test_read_only_toolsets_registry_is_non_empty() -> None:
    """Guard against READ_ONLY_TOOLSETS being empty (would make
    invariant (b) pass vacuously)."""
    assert len(READ_ONLY_TOOLSETS) > 0, (
        "READ_ONLY_TOOLSETS is empty — invariant (b) would pass "
        "vacuously."
    )


# ---------------------------------------------------------------------------
# Invariant (a) — no forbidden tool-name patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern_name",
    sorted(FORBIDDEN_TOOL_PATTERNS.keys()),
    ids=lambda n: f"pattern::{n}",
)
def test_no_forbidden_tool_name_patterns(pattern_name: str) -> None:
    """P7.a: no registered tool name matches a forbidden-family regex.

    Validates Requirements 14.2, 22.2, 22.3, 39.2, 44.3, 44.4, 48.1,
    48.4, 48.5, 48.6, 48.7.

    The ``EXCEPTIONS`` whitelist lets us keep the patterns broad
    without flagging intentionally-allowed siblings (the owner-scoped
    ``jira_delete_own_filter`` — though the current pattern set does
    not match that name, the exception is kept for future-proofing).

    A failure here indicates a regression: somebody registered a tool
    surfacing one of the explicitly-excluded capabilities. Fix by
    removing the offending ``@<server>_mcp.tool`` registration OR, if
    the tool is a legitimately-scoped variant, add it to ``EXCEPTIONS``
    with a citation to the authorising requirement.
    """
    pattern = FORBIDDEN_TOOL_PATTERNS[pattern_name]
    matches = sorted(
        name
        for name in _ALL_NAMES
        if pattern.search(name) and name not in EXCEPTIONS
    )
    assert not matches, (
        f"Forbidden pattern '{pattern_name}' (regex: "
        f"{pattern.pattern!r}) matched registered tool(s): "
        f"{matches!r}. These tools are excluded by the DC-parity "
        f"design (Req 48.1-48.7, 44.3, 44.4). Remove the "
        f"registration or, if the tool is a legitimately-scoped "
        f"variant, add it to EXCEPTIONS with a requirement citation."
    )


# ---------------------------------------------------------------------------
# Invariant (a) — no forbidden exact tool names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forbidden_name",
    sorted(FORBIDDEN_EXACT_NAMES),
    ids=lambda n: f"name::{n}",
)
def test_no_forbidden_exact_tool_names(forbidden_name: str) -> None:
    """P7.a: no exact forbidden tool name is registered.

    Validates Requirements 4.4, 5.3, 15.5, 30.2, 34.4, 34.5, 48.2.

    Exact-name check for the one-off footguns: permanent deletes of
    projects / repositories / spaces, cascading page-tree deletes,
    page purges, and the non-owner-scoped filter delete. The
    owner-scoped ``jira_delete_own_filter`` (Req 15.3, 15.4) is a
    sibling tool and is covered by a separate positive assertion below.
    """
    # Exceptions shouldn't appear in the forbidden-exact set — guard
    # against an author accidentally putting an allowed name in both
    # lists.
    assert forbidden_name not in EXCEPTIONS, (
        f"'{forbidden_name}' is in both FORBIDDEN_EXACT_NAMES and "
        f"EXCEPTIONS. Remove it from one of the two."
    )
    assert forbidden_name not in _ALL_NAMES, (
        f"Forbidden tool name '{forbidden_name}' IS registered on a "
        f"FastMCP server. This capability is explicitly excluded by "
        f"the DC-parity design (Req 4.4, 5.3, 15.5, 30.2, 34.4, "
        f"34.5, 48.2). Remove the registration."
    )


# ---------------------------------------------------------------------------
# Invariant (b) — read-only toolsets have zero write-tagged tools
# ---------------------------------------------------------------------------


def _toolset_name(tags: set[str]) -> str | None:
    """Extract the ``<name>`` suffix of the tool's ``toolset:*`` tag."""
    for tag in tags:
        if tag.startswith(TOOLSET_TAG_PREFIX):
            return tag[len(TOOLSET_TAG_PREFIX) :]
    return None


@pytest.mark.parametrize(
    ("server_label", "tool_name"),
    _parametrized_tools(),
)
def test_read_only_toolsets_have_no_write_tagged_tools(
    server_label: str, tool_name: str
) -> None:
    """P7.b: tools in a read-only toolset carry no ``"write"`` tag.

    Validates Requirements 11.3, 16.2, 19.2, 20.2, 22.2, 24.2, 25.3,
    30.2, 36.2, 39.2, 44.4.

    Read-only toolsets are declared in :data:`READ_ONLY_TOOLSETS`. A
    tool tagged ``"write"`` inside one of these toolsets is a
    registration bug — either the tool belongs in a different
    (writable) toolset, or the toolset's read-only classification is
    wrong.

    Tools without a toolset tag, or tools whose toolset is not in the
    read-only list, are skipped (the former is Property 6's concern).
    """
    tool = _TOOLS_BY_SERVER[server_label][tool_name]
    tags: set[str] = set(getattr(tool, "tags", set()) or set())

    toolset_name = _toolset_name(tags)
    if toolset_name is None:
        pytest.skip(
            f"Tool '{tool_name}' on '{server_label}' has no toolset "
            f"tag — covered by Property 6."
        )

    if toolset_name not in READ_ONLY_TOOLSETS:
        pytest.skip(
            f"Tool '{tool_name}' on '{server_label}' lives in "
            f"'{toolset_name}' which is not a read-only toolset."
        )

    assert "write" not in tags, (
        f"Tool '{tool_name}' on '{server_label}' is in read-only "
        f"toolset '{toolset_name}' but carries the 'write' tag "
        f"(full tag set: {sorted(tags)!r}). Read-only toolsets MUST "
        f"contain zero write-tagged tools — either move this tool to "
        f"a writable toolset or change the tool to read-only."
    )


# ---------------------------------------------------------------------------
# Positive sanity — the owner-scoped filter delete IS registered
# ---------------------------------------------------------------------------


def test_jira_delete_own_filter_is_registered() -> None:
    """Sanity: the owner-scoped filter delete exists on Jira.

    Validates Req 15.3, 15.4, 22.3, 44.3. The broad
    ``jira_delete_filter`` is forbidden (see FORBIDDEN_EXACT_NAMES),
    but the owner-scoped ``jira_delete_own_filter`` variant IS
    allowed — it resolves the filter's owner and short-circuits with
    a ``not_filter_owner`` StructuredError before issuing the DELETE
    for any other user's filter.

    This positive assertion guards against a future commit that drops
    the owner-scoped tool while leaving the forbidden-pattern check
    untouched (which would otherwise let the spec's delete-filter
    capability disappear silently).
    """
    jira_tools = _TOOLS_BY_SERVER["jira"]
    assert "delete_own_filter" in jira_tools, (
        "Owner-scoped filter delete 'delete_own_filter' is NOT "
        "registered on the Jira MCP server. Req 15.3, 15.4, 22.3, "
        "44.3 require this tool as the only allowed filter-delete "
        "variant."
    )
    # And the forbidden broad variant must remain unregistered.
    assert "jira_delete_filter" not in _ALL_NAMES, (
        "Broad 'jira_delete_filter' IS registered — Req 15.5 "
        "forbids it; only the owner-scoped variant is allowed."
    )


# ---------------------------------------------------------------------------
# Hook regex sanity — webhooks are allowed, pre/post-receive hooks are not
# ---------------------------------------------------------------------------


def test_hook_pattern_excludes_webhook_family() -> None:
    """Sanity: the ``non_webhook_hook`` regex must NOT match the
    allowed webhook tools (Req 2). A regression in this regex would
    falsely flag ``create_webhook`` / ``list_webhooks`` / etc. and
    break the parity test even though the design explicitly allows
    them.
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
            f"'{allowed}'. The look-behind on 'web' is broken."
        )
    # And DO match truly-forbidden hypothetical hook names.
    for blocked in (
        "create_pre_receive_hook",
        "list_post_receive_hooks",
        "delete_repository_hook",
    ):
        assert pattern.search(blocked), (
            f"'non_webhook_hook' regex failed to match forbidden "
            f"name '{blocked}'."
        )
