"""Property test P1 — Read-only mode universally blocks writes with zero outbound HTTP.

Validates Requirements 41.1, 41.2 / design Property 1: every write-tagged
DC-parity tool must, when ``READ_ONLY_MODE=true``, return the structured
``read_only_mode`` error WITHOUT issuing any outbound HTTP request. This
is the universal belt-and-suspenders guard defined in
:mod:`mcp_atlassian.utils.dc_guards` and invoked by every write tool
before any mixin method is called.

Strategy
--------
The test is parametrised over a curated list of the 46 new write tools
added by the ``atlassian-dc-tool-parity`` spec (tracks 8–48). For each
tool:

1. ``READ_ONLY_MODE`` is set to ``"true"`` via ``monkeypatch.setenv``.
2. ``get_{product}_fetcher`` is patched on the server module to return a
   ``MagicMock`` so any accidental fetcher access would be visible (but
   would still perform zero real HTTP).
3. The tool's ``.fn`` callable is invoked through ``asyncio.run`` with
   minimum-viable canonical arguments that satisfy the Pydantic
   signature. Because the ``read_only`` guard fires *before* any mixin
   method is called, these canonical values are never forwarded upstream.
4. The JSON envelope is parsed. The test asserts both:
   * ``success`` is ``False`` and ``error_code`` is ``"read_only_mode"``.
   * The mocked fetcher received **zero** method calls (no outbound HTTP).

Tools that layer the FastMCP ``@check_write_access`` decorator above
``check_read_only`` (``move_page``, ``copy_page_tree``) require a fake
``fastmcp.Context`` with an empty ``lifespan_context`` so the decorator
short-circuits transparently and lets the inner ``check_read_only``
handle the read-only gate.

Style reference: :mod:`tests.unit.properties.test_comment_visibility_property`.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Curated registry of every write tool added by the DC parity feature.
#
# Each entry is a 4-tuple of:
#
#   (tool_name, server_module_path, fetcher_dependency_name, kwargs)
#
# ``tool_name``     — the attribute name on the server module (the
#                      undecorated tool function whose ``.fn`` is the
#                      async implementation). NOT the registered MCP
#                      tool name (which carries the ``{product}_`` prefix
#                      added when the sub-server is mounted).
# ``server_module`` — dotted import path of the server module the tool
#                      lives in.
# ``fetcher_dep``   — the name of the ``get_{product}_fetcher`` symbol to
#                      monkeypatch on the ``servers.dependencies`` module
#                      AND on the server module (it's re-imported there).
# ``kwargs``        — minimum-viable keyword arguments required by the
#                      tool signature. These are never forwarded to the
#                      fetcher because the read-only guard rejects the
#                      call first; they only need to satisfy Pydantic
#                      validation so the function body executes long
#                      enough to reach ``check_read_only``.
# ---------------------------------------------------------------------------


_WRITE_TOOLS: list[tuple[str, str, str, dict[str, Any]]] = [
    # ---------------- Bitbucket ----------------
    # bitbucket_default_reviewers
    (
        "create_default_reviewer_rule",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "source_matcher": '{"id": "ANY_REF", "type": {"id": "ANY_REF"}}',
            "target_matcher": '{"id": "refs/heads/main", "type": {"id": "BRANCH"}}',
            "reviewers": '[{"name": "jdoe"}]',
            "required_approvals": 1,
        },
    ),
    (
        "update_default_reviewer_rule",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "rule_id": 1,
        },
    ),
    (
        "delete_default_reviewer_rule",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {"project_key": "PROJ", "repo_slug": "repo", "rule_id": 1},
    ),
    # bitbucket_webhooks
    (
        "create_webhook",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "name": "hook-a",
            "url": "https://example.com/hook",
            "events": '["repo:refs_changed"]',
        },
    ),
    (
        "update_webhook",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {"project_key": "PROJ", "repo_slug": "repo", "webhook_id": 1},
    ),
    (
        "delete_webhook",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {"project_key": "PROJ", "repo_slug": "repo", "webhook_id": 1},
    ),
    # bitbucket_required_builds
    (
        "create_required_build",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "build_parent_keys": '["PROJ-PLAN"]',
            "ref_matcher": '{"id": "refs/heads/main", "type": {"id": "BRANCH"}}',
        },
    ),
    (
        "delete_required_build",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {"project_key": "PROJ", "repo_slug": "repo", "condition_id": 1},
    ),
    # bitbucket_repository_admin
    (
        "create_repository",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {"project_key": "PROJ", "name": "new-repo"},
    ),
    (
        "update_repository",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {"project_key": "PROJ", "repo_slug": "repo"},
    ),
    (
        "fork_repository",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {
            "source_project": "PROJ",
            "source_slug": "repo",
            "dest_project": "DEST",
        },
    ),
    # bitbucket_project_admin
    (
        "create_project",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {"key": "NEWP", "name": "new-project"},
    ),
    (
        "update_project",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {"project_key": "PROJ"},
    ),
    # bitbucket_pull_requests — PR comment reactions + watch/unwatch PR
    (
        "add_pr_comment_reaction",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "pr_id": 1,
            "comment_id": 1,
            "emoji": "+1",
        },
    ),
    (
        "remove_pr_comment_reaction",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "pr_id": 1,
            "comment_id": 1,
            "emoji": "+1",
        },
    ),
    (
        "watch_pull_request",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {"project_key": "PROJ", "repo_slug": "repo", "pr_id": 1},
    ),
    (
        "unwatch_pull_request",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {"project_key": "PROJ", "repo_slug": "repo", "pr_id": 1},
    ),
    # bitbucket_repositories — repo watch/unwatch + label write
    (
        "watch_repository",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {"project_key": "PROJ", "repo_slug": "repo"},
    ),
    (
        "unwatch_repository",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {"project_key": "PROJ", "repo_slug": "repo"},
    ),
    (
        "add_repository_label",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {"project_key": "PROJ", "repo_slug": "repo", "label": "foo"},
    ),
    (
        "remove_repository_label",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {"project_key": "PROJ", "repo_slug": "repo", "label": "foo"},
    ),
    # bitbucket_commits — commit comments + cherry-pick
    (
        "add_commit_comment",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "commit_id": "abc123",
            "text": "hello",
        },
    ),
    (
        "update_commit_comment",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "commit_id": "abc123",
            "comment_id": 1,
            "text": "updated",
            "version": 0,
        },
    ),
    (
        "delete_commit_comment",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "commit_id": "abc123",
            "comment_id": 1,
            "version": 0,
        },
    ),
    (
        "cherry_pick_commit",
        "mcp_atlassian.servers.bitbucket",
        "get_bitbucket_fetcher",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "source_commit": "abc123",
            "target_branch": "refs/heads/main",
        },
    ),
    # ---------------- Jira ----------------
    # jira_filters
    (
        "jira_create_filter",
        "mcp_atlassian.servers.jira",
        "get_jira_fetcher",
        {"name": "my filter", "jql": "project = PROJ"},
    ),
    (
        "jira_update_filter",
        "mcp_atlassian.servers.jira",
        "get_jira_fetcher",
        {"filter_id": "10001"},
    ),
    (
        "jira_delete_own_filter",
        "mcp_atlassian.servers.jira",
        "get_jira_fetcher",
        {"filter_id": "10001"},
    ),
    # jira_notifications
    (
        "jira_notify_issue",
        "mcp_atlassian.servers.jira",
        "get_jira_fetcher",
        {
            "issue_key": "PROJ-1",
            "subject": "Test",
            "text_body": "hello",
        },
    ),
    # jira (issue-level votes + archive/restore)
    (
        "jira_add_issue_vote",
        "mcp_atlassian.servers.jira",
        "get_jira_fetcher",
        {"issue_key": "PROJ-1"},
    ),
    (
        "jira_remove_issue_vote",
        "mcp_atlassian.servers.jira",
        "get_jira_fetcher",
        {"issue_key": "PROJ-1"},
    ),
    (
        "jira_archive_issue",
        "mcp_atlassian.servers.jira",
        "get_jira_fetcher",
        {"issue_key": "PROJ-1"},
    ),
    (
        "jira_restore_issue",
        "mcp_atlassian.servers.jira",
        "get_jira_fetcher",
        {"issue_key": "PROJ-1"},
    ),
    # ---------------- Confluence ----------------
    # confluence_restrictions
    (
        "set_content_restrictions",
        "mcp_atlassian.servers.confluence",
        "get_confluence_fetcher",
        {"page_id": "123456"},
    ),
    (
        "clear_content_restrictions",
        "mcp_atlassian.servers.confluence",
        "get_confluence_fetcher",
        {"page_id": "123456"},
    ),
    # confluence_watchers
    (
        "watch_page_self",
        "mcp_atlassian.servers.confluence",
        "get_confluence_fetcher",
        {"page_id": "123456"},
    ),
    (
        "unwatch_page_self",
        "mcp_atlassian.servers.confluence",
        "get_confluence_fetcher",
        {"page_id": "123456"},
    ),
    # confluence_pages (async long-task surface)
    (
        "move_page",
        "mcp_atlassian.servers.confluence",
        "get_confluence_fetcher",
        {"page_id": "123456", "target_parent_id": "654321"},
    ),
    (
        "copy_page_tree",
        "mcp_atlassian.servers.confluence",
        "get_confluence_fetcher",
        {"page_id": "123456", "target_parent_id": "654321"},
    ),
    # confluence_templates
    (
        "create_page_from_template",
        "mcp_atlassian.servers.confluence",
        "get_confluence_fetcher",
        {
            "space_key": "SP",
            "title": "New Page",
            "template_id": "tpl-1",
        },
    ),
    # confluence_page_properties
    (
        "set_page_property",
        "mcp_atlassian.servers.confluence",
        "get_confluence_fetcher",
        {"page_id": "123456", "key": "agent-state", "value": {"foo": "bar"}},
    ),
    (
        "delete_page_property",
        "mcp_atlassian.servers.confluence",
        "get_confluence_fetcher",
        {"page_id": "123456", "key": "agent-state"},
    ),
    # confluence_archive
    (
        "archive_page",
        "mcp_atlassian.servers.confluence",
        "get_confluence_fetcher",
        {"page_id": "123456"},
    ),
    (
        "restore_archived_page",
        "mcp_atlassian.servers.confluence",
        "get_confluence_fetcher",
        {"page_id": "123456"},
    ),
    (
        "archive_space",
        "mcp_atlassian.servers.confluence",
        "get_confluence_fetcher",
        {"space_key": "SP"},
    ),
    # confluence_likes
    (
        "like_page",
        "mcp_atlassian.servers.confluence",
        "get_confluence_fetcher",
        {"page_id": "123456"},
    ),
    (
        "unlike_page",
        "mcp_atlassian.servers.confluence",
        "get_confluence_fetcher",
        {"page_id": "123456"},
    ),
]


# Public parametrisation id — the registered MCP tool name (with the
# ``{product}_`` prefix that mounting applies). Keeps test output
# readable while the tuple itself carries the undecorated attribute.
def _mcp_id(entry: tuple[str, str, str, dict[str, Any]]) -> str:
    attr, module_path, _dep, _kwargs = entry
    product = module_path.rsplit(".", 1)[-1]
    # Jira tools are already prefixed ``jira_`` at the source level, so
    # avoid double-prefixing; the bitbucket and confluence tools are bare.
    if attr.startswith(f"{product}_"):
        return attr
    return f"{product}_{attr}"


# ---------------------------------------------------------------------------
# Fake context + fetcher fixtures
# ---------------------------------------------------------------------------


def _make_fake_ctx() -> SimpleNamespace:
    """Build a minimal ``fastmcp.Context`` stand-in.

    ``check_write_access`` (on ``move_page`` / ``copy_page_tree``) reads
    ``ctx.request_context.lifespan_context`` and calls ``.get`` on it;
    an empty dict short-circuits that decorator transparently so the
    inner ``check_read_only`` guard can handle the read-only gate
    uniformly with the rest of the suite.
    """
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={}),
    )


def _make_fetcher_mock() -> MagicMock:
    """Build a ``MagicMock`` fetcher whose methods are tracked.

    The read-only guard fires before any fetcher method is invoked, so
    this mock primarily exists as a safety net: if a regression slips
    the guard past the ``read_only`` check, ``method_calls`` would
    capture the stray invocation and the test would fail.
    """
    fetcher = MagicMock(name="dc-fetcher")
    fetcher.is_cloud = False
    # Shape the config so any accidental precheck attribute access
    # (``config.projects_filter``) does not raise ``AttributeError``
    # before the test's assertions can run.
    fetcher.config = SimpleNamespace(is_cloud=False, projects_filter=None, username="tester")
    # Modern DC — any downstream version gate would pass if it were
    # reached (it won't be, but avoid confusing secondary failures if a
    # regression slips the read-only guard).
    fetcher.get_dc_version.return_value = "99.99.99"
    fetcher._dc_version = "99.99.99"
    return fetcher


# ---------------------------------------------------------------------------
# Property P1 — READ_ONLY_MODE universally blocks writes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    _WRITE_TOOLS,
    ids=[_mcp_id(e) for e in _WRITE_TOOLS],
)
def test_write_tool_blocked_in_read_only_mode(
    entry: tuple[str, str, str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: every write-tagged DC tool returns ``read_only_mode`` with zero HTTP.

    Parametrised over the 46 write tools added by the DC-parity spec.
    For each tool, under ``READ_ONLY_MODE=true``:

    * The response JSON has ``success=False`` and
      ``error_code="read_only_mode"``.
    * The patched fetcher recorded **zero** method invocations —
      equivalent to zero outbound HTTP because each fetcher method
      wraps exactly one DC REST call.
    """
    attr, module_path, dep_name, kwargs = entry

    server_module = importlib.import_module(module_path)
    tool = getattr(server_module, attr)

    # 1. Put the server into read-only mode. ``check_read_only`` reads
    # this env var every call (no caching), so a single monkeypatch is
    # sufficient for the whole test body.
    monkeypatch.setenv("READ_ONLY_MODE", "true")

    # 2. Patch ``get_{product}_fetcher`` on the server module so any
    # accidental fetcher access would hit this MagicMock (not the live
    # network). Because the guard fires first, this is never awaited
    # in the success case; the mock's method-call ledger acts as a
    # regression canary.
    fetcher = _make_fetcher_mock()

    async def _aget(_ctx: Any) -> MagicMock:
        return fetcher

    monkeypatch.setattr(server_module, dep_name, _aget)

    # 3. Invoke the tool. ``.fn`` is the undecorated async function
    # attached by the FastMCP ``@tool`` decorator.
    fake_ctx = _make_fake_ctx()
    result_json = asyncio.run(tool.fn(fake_ctx, **kwargs))

    payload = json.loads(result_json)

    # 4. Structured-error contract (Requirement 41.2).
    assert payload.get("success") is False, (
        f"{attr}: expected success=False under READ_ONLY_MODE=true, "
        f"got payload={payload!r}"
    )
    assert payload.get("error_code") == "read_only_mode", (
        f"{attr}: expected error_code='read_only_mode', "
        f"got {payload.get('error_code')!r}; payload={payload!r}"
    )

    # 5. Zero-HTTP contract (Requirement 41.1). ``method_calls`` captures
    # every attribute-access-and-call chain on the mock, so a non-empty
    # ledger means the guard was bypassed.
    assert fetcher.method_calls == [], (
        f"{attr}: expected zero fetcher method calls under read-only mode, "
        f"got {fetcher.method_calls!r}"
    )


# ---------------------------------------------------------------------------
# Meta-assertion — every curated entry resolves to a real registered tool.
# Keeps the suite honest: typos in ``_WRITE_TOOLS`` would otherwise pass
# silently as pytest skips missing attributes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", _WRITE_TOOLS, ids=[_mcp_id(e) for e in _WRITE_TOOLS])
def test_curated_tool_has_write_tag(
    entry: tuple[str, str, str, dict[str, Any]],
) -> None:
    """Every curated tool must carry the ``write`` tag (sanity check)."""
    attr, module_path, _dep, _kwargs = entry
    server_module = importlib.import_module(module_path)
    tool = getattr(server_module, attr)

    tags = getattr(tool, "tags", set())
    assert "write" in tags, (
        f"{attr}: expected 'write' in tool.tags, got {tags!r}"
    )
