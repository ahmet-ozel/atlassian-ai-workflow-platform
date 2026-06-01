"""Property test P2 — Project and space filter universally blocks out-of-scope
operations with zero outbound HTTP.

Validates Requirements 1.6, 4.5, 43.1, 43.2, 43.3, 43.4 / design Property 2.

For every project/space-scoped DC-parity tool, when
``BITBUCKET_PROJECTS_FILTER`` / ``CONFLUENCE_SPACES_FILTER`` is set and the
caller supplies a key that is **not** in the allow-list, the tool must:

1. Return the structured ``filtered_out`` error envelope
   (``success=False``, ``error_code="filtered_out"``).
2. Issue **zero** outbound HTTP requests — the fetcher's
   ``method_calls`` ledger must remain empty so any accidental bypass of
   :func:`mcp_atlassian.utils.dc_guards.check_project_filter` fails the
   suite loudly.

Strategy
--------
The test is parametrised over a curated list of every project- and
space-scoped tool added by tracks 5–48 of the ``atlassian-dc-tool-parity``
spec (both read- and write-tagged; the filter guard is independent of
read/write, per Req 43.1–43.4). For each tool:

* ``monkeypatch.delenv("READ_ONLY_MODE", raising=False)`` — P2 is about
  the filter gate in isolation; P1 covers read-only separately.
* ``monkeypatch.setenv("BITBUCKET_PROJECTS_FILTER", "OTHER")`` **and**
  ``monkeypatch.setenv("CONFLUENCE_SPACES_FILTER", "OTHER")`` so the
  filter is active for both products regardless of which tool is
  dispatched.
* A :class:`MagicMock` fetcher is built with ``config.projects_filter``
  and ``config.spaces_filter`` both set to ``"OTHER"`` so
  :func:`check_project_filter` sees the allow-list even if the tool
  reads from ``fetcher.config.*_filter`` rather than the env var.
* ``get_{product}_fetcher`` is patched on the server module.
* The tool's ``.fn`` is invoked with the canonical key ``"PROJ"`` /
  ``"SP"`` (out-of-scope against ``"OTHER"``).
* Assertions: ``payload.success is False``,
  ``payload.error_code == "filtered_out"``, and
  ``fetcher.method_calls == []``.

A small positive-path fixture also verifies that when the filter **does**
include the supplied key (filter = ``"PROJ"``), the fetcher method **is**
invoked — guaranteeing the guard is not a no-op that always blocks.

Style reference: :mod:`tests.unit.properties.test_read_only_property`.
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
# Curated registry of every project/space-scoped tool added by the spec.
#
# Each entry is an 8-tuple of:
#
#   (attr, module_path, dep_name, kwargs,
#    filter_env_name, filter_env_value, config_filter_attr, fetcher_method)
#
# ``attr``              — the attribute name on the server module (the
#                          undecorated tool function whose ``.fn`` is the
#                          async implementation).
# ``module_path``       — dotted import path of the server module.
# ``dep_name``          — the ``get_{product}_fetcher`` symbol to patch
#                          on the server module.
# ``kwargs``            — minimum-viable keyword args. The scoping
#                          project/space key is always ``"PROJ"`` /
#                          ``"SP"`` so the filter (set to ``"OTHER"``)
#                          rejects the call. For ``fork_repository`` the
#                          **destination** project is what the tool
#                          filters on, so ``dest_project`` is set to
#                          ``"PROJ"``; for ``create_project`` the new
#                          ``key`` is the scoping field.
# ``filter_env_name``   — ``BITBUCKET_PROJECTS_FILTER`` or
#                          ``CONFLUENCE_SPACES_FILTER``.
# ``filter_env_value``  — ``"OTHER"`` so ``"PROJ"`` / ``"SP"`` is out
#                          of scope.
# ``config_filter_attr``— ``"projects_filter"`` or ``"spaces_filter"``
#                          — the attribute on ``fetcher.config`` the
#                          tool reads when composing its guard call.
# ``fetcher_method``    — the name of the mixin method that would be
#                          invoked on the success path. Used only by the
#                          positive-path sanity subset to assert that
#                          the method *is* called when the key is in
#                          scope. ``None`` for the parametric negative
#                          subset.
# ---------------------------------------------------------------------------


# Short-hand helpers used to build webhook / reviewer-rule JSON blobs.
_REVIEWER_SOURCE = '{"id": "ANY_REF", "type": {"id": "ANY_REF"}}'
_REVIEWER_TARGET = '{"id": "refs/heads/main", "type": {"id": "BRANCH"}}'
_REVIEWERS = '[{"name": "jdoe"}]'
_WEBHOOK_EVENTS = '["repo:refs_changed"]'
_BUILD_PARENTS = '["PROJ-PLAN"]'
_BUILD_REF_MATCHER = '{"id": "refs/heads/main", "type": {"id": "BRANCH"}}'


# ---- Bitbucket project-scoped tools (filter on ``project_key``) ----------

_BB_MOD = "mcp_atlassian.servers.bitbucket"
_BB_DEP = "get_bitbucket_fetcher"
_BB_ENV = "BITBUCKET_PROJECTS_FILTER"
_BB_CFG = "projects_filter"


_BITBUCKET_TOOLS: list[tuple[str, str, str, dict[str, Any], str, str, str]] = [
    # bitbucket_webhooks — create / update / delete / list / get
    (
        "create_webhook", _BB_MOD, _BB_DEP,
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "name": "hook-a",
            "url": "https://example.com/hook",
            "events": _WEBHOOK_EVENTS,
        },
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "update_webhook", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo", "webhook_id": 1},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "delete_webhook", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo", "webhook_id": 1},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "list_webhooks", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "get_webhook", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo", "webhook_id": 1},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    # bitbucket_default_reviewers — create / update / delete / list / get
    (
        "create_default_reviewer_rule", _BB_MOD, _BB_DEP,
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "source_matcher": _REVIEWER_SOURCE,
            "target_matcher": _REVIEWER_TARGET,
            "reviewers": _REVIEWERS,
            "required_approvals": 1,
        },
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "update_default_reviewer_rule", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo", "rule_id": 1},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "delete_default_reviewer_rule", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo", "rule_id": 1},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "list_default_reviewers", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "get_default_reviewer_rule", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo", "rule_id": 1},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    # bitbucket_required_builds — create / delete / list
    (
        "create_required_build", _BB_MOD, _BB_DEP,
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "build_parent_keys": _BUILD_PARENTS,
            "ref_matcher": _BUILD_REF_MATCHER,
        },
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "delete_required_build", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo", "condition_id": 1},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "list_required_builds", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    # bitbucket_repository_admin — create / update / fork
    (
        "create_repository", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "name": "new-repo"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "update_repository", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo", "name": "renamed"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    # fork_repository filters on ``dest_project``; supply it as the
    # out-of-scope key so the guard rejects. The source may freely sit
    # outside the scope because the fork lands in ``dest_project``.
    (
        "fork_repository", _BB_MOD, _BB_DEP,
        {
            "source_project": "SRC",
            "source_slug": "repo",
            "dest_project": "PROJ",
        },
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    # bitbucket_project_admin — create (filter on ``key``) / update
    (
        "create_project", _BB_MOD, _BB_DEP,
        {"key": "PROJ", "name": "new-project"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "update_project", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "name": "updated"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    # bitbucket_pull_requests — PR comment reactions + watch/unwatch PR
    (
        "add_pr_comment_reaction", _BB_MOD, _BB_DEP,
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "pr_id": 1,
            "comment_id": 1,
            "emoji": "+1",
        },
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "remove_pr_comment_reaction", _BB_MOD, _BB_DEP,
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "pr_id": 1,
            "comment_id": 1,
            "emoji": "+1",
        },
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "watch_pull_request", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo", "pr_id": 1},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "unwatch_pull_request", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo", "pr_id": 1},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    # bitbucket_pull_requests — participants (read)
    (
        "list_pull_request_participants", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo", "pr_id": 1},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    # bitbucket_repositories — watch/unwatch + labels + render_markup
    (
        "watch_repository", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "unwatch_repository", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "add_repository_label", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo", "label": "foo"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "remove_repository_label", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo", "label": "foo"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "list_repository_labels", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    # render_markup gates the filter ONLY when project_key is supplied;
    # providing ``project_key="PROJ"`` exercises that path.
    (
        "render_markup", _BB_MOD, _BB_DEP,
        {"markup_text": "hello", "project_key": "PROJ", "repo_slug": "repo"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    # bitbucket_commits — commit comments + cherry-pick
    (
        "add_commit_comment", _BB_MOD, _BB_DEP,
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "commit_id": "abc123",
            "text": "hello",
        },
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "update_commit_comment", _BB_MOD, _BB_DEP,
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "commit_id": "abc123",
            "comment_id": 1,
            "text": "updated",
            "version": 0,
        },
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "delete_commit_comment", _BB_MOD, _BB_DEP,
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "commit_id": "abc123",
            "comment_id": 1,
            "version": 0,
        },
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "list_commit_comments", _BB_MOD, _BB_DEP,
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "commit_id": "abc123",
        },
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "cherry_pick_commit", _BB_MOD, _BB_DEP,
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "source_commit": "abc123",
            "target_branch": "refs/heads/main",
        },
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    # bitbucket_branches — branching model
    (
        "get_branching_model", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    # bitbucket_deployments — list / get
    (
        "list_deployments", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
    (
        "get_deployment", _BB_MOD, _BB_DEP,
        {"project_key": "PROJ", "repo_slug": "repo", "deployment_id": "1"},
        _BB_ENV, "OTHER", _BB_CFG,
    ),
]


# ---- Confluence space-scoped tools (filter on ``space_key``) -------------

_CF_MOD = "mcp_atlassian.servers.confluence"
_CF_DEP = "get_confluence_fetcher"
_CF_ENV = "CONFLUENCE_SPACES_FILTER"
_CF_CFG = "spaces_filter"


_CONFLUENCE_TOOLS: list[tuple[str, str, str, dict[str, Any], str, str, str]] = [
    # confluence_space_admin — list permissions (read, space-scoped)
    (
        "list_space_permissions", _CF_MOD, _CF_DEP,
        {"space_key": "SP"},
        _CF_ENV, "OTHER", _CF_CFG,
    ),
    # confluence_templates — create page from template (space-scoped write)
    (
        "create_page_from_template", _CF_MOD, _CF_DEP,
        {
            "space_key": "SP",
            "title": "New Page",
            "template_id": "tpl-1",
        },
        _CF_ENV, "OTHER", _CF_CFG,
    ),
    # confluence_archive — archive a whole space
    (
        "archive_space", _CF_MOD, _CF_DEP,
        {"space_key": "SP"},
        _CF_ENV, "OTHER", _CF_CFG,
    ),
]


_ALL_TOOLS: list[tuple[str, str, str, dict[str, Any], str, str, str]] = (
    _BITBUCKET_TOOLS + _CONFLUENCE_TOOLS
)


# Parametrisation id — carries the MCP-registered tool name shape
# (``{product}_{attr}``) so pytest output is self-describing.
def _mcp_id(entry: tuple[str, str, str, dict[str, Any], str, str, str]) -> str:
    attr, module_path, *_ = entry
    product = module_path.rsplit(".", 1)[-1]
    if attr.startswith(f"{product}_"):
        return attr
    return f"{product}_{attr}"


# ---------------------------------------------------------------------------
# Fake context + fetcher fixtures
# ---------------------------------------------------------------------------


def _make_fake_ctx() -> SimpleNamespace:
    """Minimal ``fastmcp.Context`` stand-in.

    The ``@check_write_access`` decorator present on a small number of
    long-task Confluence tools reads
    ``ctx.request_context.lifespan_context`` and calls ``.get`` on it;
    an empty dict short-circuits that decorator transparently so the
    inner ``check_project_filter`` guard can handle the gate uniformly.
    """
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={}),
    )


def _make_fetcher_mock(
    projects_filter: str | None, spaces_filter: str | None
) -> MagicMock:
    """Build a ``MagicMock`` fetcher with both filter attributes shaped.

    The project-filter guard fires before any fetcher method is invoked,
    so the mock primarily exists as a safety net: if a regression slips
    the guard past the ``filtered_out`` check, ``method_calls`` would
    capture the stray invocation and the test would fail.

    Both ``projects_filter`` (Bitbucket/Jira) and ``spaces_filter``
    (Confluence) are populated so the same mock can back either product.
    ``username`` is provided because some read/write tools read it (for
    example owner-scoped delete guards — those do not fire here, but
    shaping the config exhaustively keeps the fixture reusable).
    """
    fetcher = MagicMock(name="dc-fetcher")
    fetcher.is_cloud = False
    fetcher.config = SimpleNamespace(
        is_cloud=False,
        projects_filter=projects_filter,
        spaces_filter=spaces_filter,
        username="tester",
    )
    # Modern DC — any downstream version gate would pass if it were
    # reached (it won't be, but avoid confusing secondary failures if a
    # regression slips the filter guard).
    fetcher.get_dc_version.return_value = "99.99.99"
    fetcher._dc_version = "99.99.99"
    return fetcher


# ---------------------------------------------------------------------------
# Property P2 — filter universally blocks out-of-scope keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    _ALL_TOOLS,
    ids=[_mcp_id(e) for e in _ALL_TOOLS],
)
def test_tool_blocked_by_filter_when_key_out_of_scope(
    entry: tuple[str, str, str, dict[str, Any], str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2: every project/space-scoped tool returns ``filtered_out`` with zero HTTP.

    Parametrised over the curated registry of project- and space-scoped
    tools added by the DC-parity spec. For each tool, with the filter
    env var set to ``"OTHER"`` and the caller supplying ``"PROJ"`` /
    ``"SP"`` (out of scope):

    * The response JSON has ``success=False`` and
      ``error_code="filtered_out"``.
    * The patched fetcher recorded **zero** method invocations —
      equivalent to zero outbound HTTP because each mixin method wraps
      exactly one DC REST call.
    """
    (
        attr,
        module_path,
        dep_name,
        kwargs,
        filter_env_name,
        filter_env_value,
        _config_filter_attr,
    ) = entry

    server_module = importlib.import_module(module_path)
    tool = getattr(server_module, attr)

    # READ_ONLY_MODE is out-of-scope for P2 — the filter gate must fire
    # on its own. (P1 covers the read-only gate separately.)
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)

    # Set BOTH filter env vars so the test is insensitive to which
    # product the current tool belongs to — the guard itself reads the
    # filter value via ``fetcher.config.{projects,spaces}_filter``, but
    # setting the env var too matches how real operators configure the
    # server and exercises the env→config plumbing if the tool ever
    # switches to reading the env directly.
    monkeypatch.setenv("BITBUCKET_PROJECTS_FILTER", filter_env_value)
    monkeypatch.setenv("CONFLUENCE_SPACES_FILTER", filter_env_value)

    # Build a mock fetcher whose ``config`` carries the allow-list so
    # :func:`check_project_filter` sees a filter that excludes the
    # canonical ``"PROJ"`` / ``"SP"`` key the test passes.
    fetcher = _make_fetcher_mock(
        projects_filter=filter_env_value,
        spaces_filter=filter_env_value,
    )

    async def _aget(_ctx: Any) -> MagicMock:
        return fetcher

    monkeypatch.setattr(server_module, dep_name, _aget)

    # Invoke the tool. ``.fn`` is the undecorated async function
    # attached by the FastMCP ``@tool`` decorator.
    fake_ctx = _make_fake_ctx()
    result_json = asyncio.run(tool.fn(fake_ctx, **kwargs))

    payload = json.loads(result_json)

    # Structured-error contract (Requirements 43.2, 43.3, 43.4).
    assert payload.get("success") is False, (
        f"{attr}: expected success=False when key is out of scope under "
        f"{filter_env_name}={filter_env_value!r}, got payload={payload!r}"
    )
    assert payload.get("error_code") == "filtered_out", (
        f"{attr}: expected error_code='filtered_out', got "
        f"{payload.get('error_code')!r}; payload={payload!r}"
    )

    # Zero-HTTP contract (Requirement 43.1). ``method_calls`` captures
    # every attribute-access-and-call chain on the mock, so a non-empty
    # ledger means the guard was bypassed.
    assert fetcher.method_calls == [], (
        f"{attr}: expected zero fetcher method calls under the filter "
        f"gate, got {fetcher.method_calls!r}"
    )


# ---------------------------------------------------------------------------
# Positive sanity — filter ALLOWS matching keys through
# ---------------------------------------------------------------------------
#
# The parametric negative test would silently pass if ``check_project_filter``
# always rejected every key. This small positive-path subset confirms that
# when the filter *includes* the supplied key, the tool proceeds to call the
# fetcher method — i.e. the guard is genuinely evaluating membership rather
# than acting as an always-deny no-op.
#
# Three tools are sufficient to cross-cover the Bitbucket project-scoped
# path, the Bitbucket project-admin filter-on-``key`` path, and the
# Confluence space-scoped path.

_POSITIVE_SUBSET: list[
    tuple[str, str, str, dict[str, Any], str, str, str, str, Any]
] = [
    # bitbucket_cherry_pick_commit — representative project-scoped write
    # that goes through ``bb.cherry_pick_commit``.
    (
        "cherry_pick_commit",
        _BB_MOD,
        _BB_DEP,
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "source_commit": "abc123",
            "target_branch": "refs/heads/main",
        },
        _BB_ENV,
        "PROJ",
        _BB_CFG,
        "cherry_pick_commit",
        {"id": "newsha1", "displayId": "newsha1", "message": "Apply fix"},
    ),
    # bitbucket_create_project — filter on the new ``key`` argument.
    (
        "create_project",
        _BB_MOD,
        _BB_DEP,
        {"key": "PROJ", "name": "new-project"},
        _BB_ENV,
        "PROJ",
        _BB_CFG,
        "create_project",
        {"key": "PROJ", "name": "new-project"},
    ),
    # confluence_archive_space — representative space-scoped write.
    (
        "archive_space",
        _CF_MOD,
        _CF_DEP,
        {"space_key": "SP"},
        _CF_ENV,
        "SP",
        _CF_CFG,
        "archive_space",
        {"archived": True, "response": {}},
    ),
]


@pytest.mark.parametrize(
    "entry",
    _POSITIVE_SUBSET,
    ids=[_mcp_id(e[:7]) for e in _POSITIVE_SUBSET],
)
def test_tool_proceeds_when_key_in_scope(
    entry: tuple[str, str, str, dict[str, Any], str, str, str, str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: with a matching filter, the tool proceeds to call the fetcher.

    Guards the negative parametric test against a regression where the
    filter guard always denies — in that world, the negative subset
    would pass vacuously. Here we require the fetcher method to be
    invoked at least once when the key is explicitly in scope.
    """
    (
        attr,
        module_path,
        dep_name,
        kwargs,
        filter_env_name,
        filter_env_value,
        config_filter_attr,
        fetcher_method,
        return_value,
    ) = entry

    server_module = importlib.import_module(module_path)
    tool = getattr(server_module, attr)

    monkeypatch.delenv("READ_ONLY_MODE", raising=False)
    monkeypatch.setenv(filter_env_name, filter_env_value)

    fetcher = _make_fetcher_mock(
        projects_filter=(
            filter_env_value if config_filter_attr == _BB_CFG else None
        ),
        spaces_filter=(
            filter_env_value if config_filter_attr == _CF_CFG else None
        ),
    )
    getattr(fetcher, fetcher_method).return_value = return_value

    async def _aget(_ctx: Any) -> MagicMock:
        return fetcher

    monkeypatch.setattr(server_module, dep_name, _aget)

    fake_ctx = _make_fake_ctx()
    result_json = asyncio.run(tool.fn(fake_ctx, **kwargs))
    payload = json.loads(result_json)

    # With the filter matching, ``filtered_out`` must NOT appear.
    assert payload.get("error_code") != "filtered_out", (
        f"{attr}: filter matched the supplied key but tool still "
        f"returned filtered_out; payload={payload!r}"
    )

    # The fetcher method must have been invoked — proof the guard is
    # not an always-deny no-op.
    called_mock = getattr(fetcher, fetcher_method)
    assert called_mock.called, (
        f"{attr}: expected {fetcher_method!r} to be called when the "
        f"supplied key is within the {filter_env_name} allow-list, "
        f"but it was not. method_calls={fetcher.method_calls!r}"
    )
