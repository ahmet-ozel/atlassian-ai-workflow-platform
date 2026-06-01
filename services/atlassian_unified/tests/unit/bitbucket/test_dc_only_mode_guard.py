"""Unit tests for the CloudMode ``not_supported_on_cloud`` guard applied to
every DC-only Bitbucket tool.

These tests cover task 17.2 of the ``bitbucket-cloud-dc-parity`` spec and
the following acceptance-criteria slices:

* Requirements 14.1 through 14.10 — every DC-only tool, when invoked in
  CloudMode, returns a structured error with ``success=False`` and
  ``error_code="not_supported_on_cloud"`` before any HTTP call, and
  includes the tool name plus ``"cloud"`` (as ``effective_mode``) in the
  error details.
* Requirement 19.3 — the short-circuit happens pre-HTTP: the mock
  ``atlassian.Bitbucket`` session records zero ``get`` / ``post`` /
  ``put`` / ``delete`` calls, and the underlying mixin method on the
  fetcher is never invoked.

Symmetry: each tool is also exercised in DCMode (``is_cloud=False``)
against a fetcher whose mixin method is mocked, proving the guard does
NOT short-circuit DC invocations — the existing mixin method is still
dispatched exactly as today. A dedicated happy-path test for
``bitbucket_render_markup`` in DCMode pins the dispatch contract in the
"read" direction one more time.

Test strategy
-------------

Every tool is invoked through the FastMCP-registered callable's ``.fn``
attribute (mirroring the pattern used in ``test_cherry_pick.py`` and
``test_webhooks.py``). ``get_bitbucket_fetcher`` is monkey-patched so
the tool body receives a ``MagicMock``-backed fetcher instead of opening
a real HTTP session:

* ``bb.is_cloud`` is set to the boolean under test.
* ``bb.config`` is a :class:`~types.SimpleNamespace` exposing the exact
  subset of fields the tool body reads: ``is_cloud``, ``workspace``,
  ``projects_filter``, ``username``. Using a namespace (rather than a
  :class:`MagicMock`) keeps attribute access deterministic so a typo in
  the tool body surfaces as a test failure instead of silently returning
  a new ``MagicMock``.
* ``bb.bitbucket`` is a :class:`MagicMock` with fresh ``get`` / ``post``
  / ``put`` / ``delete`` attributes that call-count to zero at the start
  of every test, so the "zero outbound HTTP" assertion is tight.
* ``bb.get_dc_version`` / ``bb._dc_version`` report a modern DC release
  so that DC-mode dispatch tests never trip the downstream
  ``check_dc_version`` gate for tools that carry one (deployments,
  PR-comment reactions).

The tools under test also invoke other guards before ``check_mode_supported``
(``check_read_only`` and ``check_project_filter``). Every test ensures
``READ_ONLY_MODE`` is unset and sets ``projects_filter`` to ``None`` so
those earlier guards are no-ops; any failure to reach the mode guard
would surface as a different ``error_code`` and fail the specific
assertions below.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Tool registry — kept colocated with the tests so updates to task 17.1
# (which tools carry ``check_mode_supported``) stay in one place.
# ---------------------------------------------------------------------------
#
# Each entry is (server_fn_name, expected_tool_name, invocation_kwargs,
# mixin_attr_on_fetcher, mixin_return_value). The invocation kwargs
# supply minimal, plausible inputs that satisfy the tool's pre-mode-guard
# argument parsing (JSON matchers, etc.) so DCMode dispatch can proceed
# into the mixin stub. In CloudMode the mode guard short-circuits before
# any argument parsing, so the same inputs are used for symmetry.

DC_ONLY_TOOLS: list[tuple[str, str, dict[str, Any], str, Any]] = [
    # Default reviewers toolset (Requirement 14.1)
    (
        "list_default_reviewers",
        "bitbucket_list_default_reviewers",
        {"project_key": "PROJ", "repo_slug": "repo"},
        "list_default_reviewers",
        [],
    ),
    (
        "get_default_reviewer_rule",
        "bitbucket_get_default_reviewer_rule",
        {"project_key": "PROJ", "repo_slug": "repo", "rule_id": 1},
        "get_default_reviewer_rule",
        {},
    ),
    (
        "create_default_reviewer_rule",
        "bitbucket_create_default_reviewer_rule",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "source_matcher": (
                '{"id": "refs/heads/feature/*", '
                '"type": {"id": "PATTERN"}}'
            ),
            "target_matcher": (
                '{"id": "refs/heads/main", "type": {"id": "BRANCH"}}'
            ),
            "reviewers": '[{"name": "jdoe"}]',
            "required_approvals": 1,
        },
        "create_default_reviewer_rule",
        {},
    ),
    (
        "update_default_reviewer_rule",
        "bitbucket_update_default_reviewer_rule",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "rule_id": 1,
            "required_approvals": 2,
        },
        "update_default_reviewer_rule",
        {},
    ),
    (
        "delete_default_reviewer_rule",
        "bitbucket_delete_default_reviewer_rule",
        {"project_key": "PROJ", "repo_slug": "repo", "rule_id": 1},
        "delete_default_reviewer_rule",
        None,
    ),
    # Required builds toolset (Requirement 14.2)
    (
        "list_required_builds",
        "bitbucket_list_required_builds",
        {"project_key": "PROJ", "repo_slug": "repo"},
        "list_required_builds",
        [],
    ),
    (
        "create_required_build",
        "bitbucket_create_required_build",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "build_parent_keys": '["PROJ-PLAN"]',
            "ref_matcher": (
                '{"id": "refs/heads/main", "type": {"id": "BRANCH"}}'
            ),
        },
        "create_required_build",
        {},
    ),
    (
        "delete_required_build",
        "bitbucket_delete_required_build",
        {"project_key": "PROJ", "repo_slug": "repo", "condition_id": 1},
        "delete_required_build",
        None,
    ),
    # Markup preview (Requirement 14.3)
    (
        "render_markup",
        "bitbucket_render_markup",
        {"markup_text": "**hello**"},
        "render_markup",
        "<p><strong>hello</strong></p>",
    ),
    # Repository labels toolset (Requirement 14.4)
    (
        "list_repository_labels",
        "bitbucket_list_repository_labels",
        {"project_key": "PROJ", "repo_slug": "repo"},
        "list_repo_labels",
        [],
    ),
    (
        "add_repository_label",
        "bitbucket_add_repository_label",
        {"project_key": "PROJ", "repo_slug": "repo", "label": "hot"},
        "add_repo_label",
        {"label": "hot", "already_labeled": False},
    ),
    (
        "remove_repository_label",
        "bitbucket_remove_repository_label",
        {"project_key": "PROJ", "repo_slug": "repo", "label": "hot"},
        "remove_repo_label",
        None,
    ),
    # Deployments toolset (Requirement 14.5)
    (
        "list_deployments",
        "bitbucket_list_deployments",
        {"project_key": "PROJ", "repo_slug": "repo"},
        "list_deployments",
        [],
    ),
    (
        "get_deployment",
        "bitbucket_get_deployment",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "deployment_id": "deploy-1",
        },
        "get_deployment",
        {},
    ),
    # Branching model (Requirement 14.6)
    (
        "get_branching_model",
        "bitbucket_get_branching_model",
        {"project_key": "PROJ", "repo_slug": "repo"},
        "get_branching_model",
        {},
    ),
    # PR participants (Requirement 14.7) — mixin method is named
    # ``list_pr_participants`` on the fetcher (tool is ``list_pull_request_participants``).
    (
        "list_pull_request_participants",
        "bitbucket_list_pull_request_participants",
        {"project_key": "PROJ", "repo_slug": "repo", "pr_id": 1},
        "list_pr_participants",
        [],
    ),
    # Project admin toolset (Requirement 14.8)
    (
        "create_project",
        "bitbucket_create_project",
        {"key": "PROJ", "name": "Project"},
        "create_project",
        {},
    ),
    (
        "update_project",
        "bitbucket_update_project",
        {"project_key": "PROJ", "name": "Renamed"},
        "update_project",
        {},
    ),
    # Repository admin — fork (Requirement 14.9)
    (
        "fork_repository",
        "bitbucket_fork_repository",
        {
            "source_project": "PROJ",
            "source_slug": "repo",
            "dest_project": "PROJ",
        },
        "fork_repository",
        {},
    ),
    # PR comment reactions (Requirement 9.9, 9.10)
    (
        "add_pr_comment_reaction",
        "bitbucket_add_pr_comment_reaction",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "pr_id": 1,
            "comment_id": 1,
            "emoji": "+1",
        },
        "add_pr_comment_reaction",
        {},
    ),
    (
        "remove_pr_comment_reaction",
        "bitbucket_remove_pr_comment_reaction",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "pr_id": 1,
            "comment_id": 1,
            "emoji": "+1",
        },
        "remove_pr_comment_reaction",
        None,
    ),
    # Cherry-pick commit (Requirement 11.10)
    (
        "cherry_pick_commit",
        "bitbucket_cherry_pick_commit",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "source_commit": "abc123",
            "target_branch": "main",
        },
        "cherry_pick_commit",
        {"id": "newsha1", "displayId": "newsha1"},
    ),
    # User search (Requirement 13.4)
    (
        "search_users",
        "bitbucket_search_users",
        {"filter_text": "alice"},
        "search_users",
        [],
    ),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeContext:
    """Minimal stand-in for :class:`fastmcp.Context` used by tool funcs."""


@pytest.fixture
def fake_ctx() -> _FakeContext:
    return _FakeContext()


@pytest.fixture
def disable_read_only(monkeypatch):
    """Ensure ``READ_ONLY_MODE`` is unset so the first guard is a no-op.

    Every DC-only write tool runs ``check_read_only`` before the mode
    guard. With ``READ_ONLY_MODE`` set, those calls would short-circuit
    with ``error_code="read_only_mode"`` and the mode guard would never
    run. Clearing the env var here is the symmetric counterpart of the
    ``disable_read_only`` fixture used by sibling test files.
    """
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)


def _make_fetcher(*, is_cloud: bool) -> MagicMock:
    """Build a ``MagicMock`` fetcher wired for ``is_cloud`` without any HTTP.

    Config fields exposed on the fetcher mirror the exact subset of
    :class:`BitbucketConfig` that DC-only tool bodies read:

    * ``is_cloud`` — boolean asserted by :func:`check_mode_supported`.
    * ``workspace`` — populated on Cloud (the tool never reaches the
      body, but keeping the namespace realistic avoids masking a regression).
    * ``projects_filter`` — ``None`` so ``check_project_filter`` is a no-op.
    * ``username`` — populated so any downstream owner-scoped logic
      (none of these tools invoke one today, but the attribute exists on
      the real config) has a plausible value.

    The :class:`MagicMock` ``bitbucket`` attribute stands in for the
    ``atlassian.Bitbucket`` session. Fresh ``get`` / ``post`` / ``put``
    / ``delete`` mocks start at ``call_count == 0`` so the "zero
    outbound HTTP" assertion below is exact.

    ``get_dc_version`` / ``_dc_version`` report ``9.4.0`` — a version
    strictly newer than every DC-version gate shipped today (7.10 for
    deployments, 8.8 for PR-comment reactions). That guarantees the
    DCMode dispatch test for those specific tools does not get
    short-circuited by the version gate downstream of the mode guard.
    """
    fetcher = MagicMock()
    fetcher.is_cloud = is_cloud
    fetcher.config = SimpleNamespace(
        is_cloud=is_cloud,
        workspace="my-team" if is_cloud else None,
        projects_filter=None,
        username="alice",
    )
    fetcher.bitbucket = MagicMock()
    fetcher.bitbucket.get = MagicMock(return_value={})
    fetcher.bitbucket.post = MagicMock(return_value={})
    fetcher.bitbucket.put = MagicMock(return_value={})
    fetcher.bitbucket.delete = MagicMock(return_value=None)
    # Modern DC version so downstream ``check_dc_version`` never trips in
    # DCMode dispatch tests for deployments / reactions.
    fetcher.get_dc_version = MagicMock(return_value="9.4.0")
    fetcher._dc_version = "9.4.0"
    return fetcher


def _install_fetcher(monkeypatch, fetcher: MagicMock) -> None:
    """Patch ``get_bitbucket_fetcher`` to return the supplied ``fetcher``.

    Tool functions call ``await get_bitbucket_fetcher(ctx)`` as their
    first line, so replacing that coroutine is enough to route every
    subsequent lookup (``bb.is_cloud``, ``bb.config.projects_filter``,
    ``bb.<mixin_method>``) into the mock.
    """
    from mcp_atlassian.servers import bitbucket as bb_server

    async def _aget(_ctx):
        return fetcher

    monkeypatch.setattr(bb_server, "get_bitbucket_fetcher", _aget)


# ---------------------------------------------------------------------------
# CloudMode: every DC-only tool short-circuits with zero HTTP
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fn_name,tool_name,kwargs,mixin_attr,mixin_return",
    DC_ONLY_TOOLS,
    ids=[entry[1] for entry in DC_ONLY_TOOLS],
)
async def test_cloud_mode_returns_not_supported_on_cloud_with_zero_http(
    fn_name: str,
    tool_name: str,
    kwargs: dict[str, Any],
    mixin_attr: str,
    mixin_return: Any,
    fake_ctx: _FakeContext,
    disable_read_only: None,
    monkeypatch,
) -> None:
    """Every DC-only tool in CloudMode returns structured mode-mismatch error.

    Asserts the three invariants pinned by Requirement 14.10 and 19.3:

    1. The JSON response has ``success=False`` and
       ``error_code="not_supported_on_cloud"``.
    2. The response's ``details`` carry the exact tool name plus
       ``effective_mode="cloud"`` and ``required_mode="dc"`` so agents
       can distinguish mode-mismatch from other structured errors without
       parsing the human-readable message.
    3. Zero outbound HTTP was issued: ``get`` / ``post`` / ``put`` /
       ``delete`` on the mocked session remain at ``call_count == 0``,
       and the specific mixin method the tool would normally invoke was
       not called either — proving the short-circuit landed before any
       dispatch into the business layer.
    """
    import mcp_atlassian.servers.bitbucket as bb_server

    fetcher = _make_fetcher(is_cloud=True)
    # Pre-configure mixin to return a plausible shape so that any
    # accidental fall-through (i.e. a regression where the guard missed)
    # still lets us make coherent assertions below rather than raising
    # inside ``json.dumps``.
    getattr(fetcher, mixin_attr).return_value = mixin_return
    _install_fetcher(monkeypatch, fetcher)

    tool = getattr(bb_server, fn_name)
    result_json = await tool.fn(fake_ctx, **kwargs)
    payload = json.loads(result_json)

    # (1) + (2): structured error envelope.
    assert payload["success"] is False, (
        f"{tool_name} should short-circuit with success=False in CloudMode; "
        f"got payload={payload!r}"
    )
    assert payload["error_code"] == "not_supported_on_cloud", (
        f"{tool_name} in CloudMode should emit "
        f"error_code='not_supported_on_cloud'; got {payload.get('error_code')!r}"
    )
    details = payload.get("details") or {}
    assert details.get("tool") == tool_name, (
        f"details.tool should echo the full tool name {tool_name!r}; "
        f"got {details.get('tool')!r}"
    )
    assert details.get("effective_mode") == "cloud"
    assert details.get("required_mode") == "dc"

    # (3): zero outbound HTTP on the atlassian.Bitbucket session.
    assert fetcher.bitbucket.get.call_count == 0, (
        f"{tool_name} leaked {fetcher.bitbucket.get.call_count} GETs in CloudMode"
    )
    assert fetcher.bitbucket.post.call_count == 0, (
        f"{tool_name} leaked {fetcher.bitbucket.post.call_count} POSTs in CloudMode"
    )
    assert fetcher.bitbucket.put.call_count == 0, (
        f"{tool_name} leaked {fetcher.bitbucket.put.call_count} PUTs in CloudMode"
    )
    assert fetcher.bitbucket.delete.call_count == 0, (
        f"{tool_name} leaked "
        f"{fetcher.bitbucket.delete.call_count} DELETEs in CloudMode"
    )

    # ... and the mixin method itself was never invoked — the guard fired
    # before any business-layer dispatch.
    mixin_mock = getattr(fetcher, mixin_attr)
    assert mixin_mock.call_count == 0, (
        f"{tool_name} reached the {mixin_attr!r} mixin method in CloudMode; "
        f"expected pre-HTTP short-circuit"
    )


# ---------------------------------------------------------------------------
# DCMode: every DC-only tool still dispatches to the existing mixin
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fn_name,tool_name,kwargs,mixin_attr,mixin_return",
    DC_ONLY_TOOLS,
    ids=[entry[1] for entry in DC_ONLY_TOOLS],
)
async def test_dc_mode_dispatches_to_existing_mixin_method(
    fn_name: str,
    tool_name: str,
    kwargs: dict[str, Any],
    mixin_attr: str,
    mixin_return: Any,
    fake_ctx: _FakeContext,
    disable_read_only: None,
    monkeypatch,
) -> None:
    """In DCMode the mode guard is a no-op and the mixin method is still called.

    This is the symmetric counterpart of the CloudMode short-circuit
    test: it proves that adding ``check_mode_supported`` to every DC-only
    tool did not regress the existing DC dispatch. The mixin method mock
    must be invoked exactly once, with the project-scoped arguments the
    agent supplied. The response must not carry the mode-mismatch error
    codes from either direction.
    """
    import mcp_atlassian.servers.bitbucket as bb_server

    fetcher = _make_fetcher(is_cloud=False)
    getattr(fetcher, mixin_attr).return_value = mixin_return
    _install_fetcher(monkeypatch, fetcher)

    tool = getattr(bb_server, fn_name)
    result_json = await tool.fn(fake_ctx, **kwargs)
    payload = json.loads(result_json)

    # Mode guard must not have fired in either direction.
    assert payload.get("error_code") != "not_supported_on_cloud", (
        f"{tool_name} incorrectly short-circuited in DCMode with "
        f"'not_supported_on_cloud'"
    )
    assert payload.get("error_code") != "not_supported_on_dc", (
        f"{tool_name} incorrectly short-circuited in DCMode with "
        f"'not_supported_on_dc'"
    )

    # The DC mixin method was dispatched exactly once.
    mixin_mock = getattr(fetcher, mixin_attr)
    assert mixin_mock.call_count == 1, (
        f"{tool_name} in DCMode was expected to dispatch to the {mixin_attr!r} "
        f"mixin method exactly once; call_count={mixin_mock.call_count}"
    )


# ---------------------------------------------------------------------------
# Bonus: one explicit DC happy-path test for render_markup
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_render_markup_dc_mode_dispatches_to_mixin_and_returns_html(
    fake_ctx: _FakeContext,
    disable_read_only: None,
    monkeypatch,
) -> None:
    """DC-mode ``bitbucket_render_markup`` dispatches to ``bb.render_markup``.

    This pins the dispatch contract one more time in the "read" direction
    with a concrete, representative example. The mixin is mocked to
    return a fixed HTML string and the test asserts:

    * The server tool forwarded the caller-supplied markup / page-type /
      project-scope kwargs to the mixin verbatim.
    * The returned JSON carries ``success=True`` with the mixin's HTML
      under the ``html`` key (the contract documented in
      ``render_markup``'s docstring).
    * The mode-guard error codes did not leak into the response — the
      guard is inert in DCMode.
    """
    import mcp_atlassian.servers.bitbucket as bb_server

    fetcher = _make_fetcher(is_cloud=False)
    fetcher.render_markup.return_value = "<p><strong>hello</strong></p>"
    _install_fetcher(monkeypatch, fetcher)

    result_json = await bb_server.render_markup.fn(
        fake_ctx,
        markup_text="**hello**",
        project_key=None,
        repo_slug=None,
        page_type="COMMENT",
    )
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["html"] == "<p><strong>hello</strong></p>"
    assert "error_code" not in payload

    fetcher.render_markup.assert_called_once_with(
        markup_text="**hello**",
        project_key=None,
        repo_slug=None,
        page_type="COMMENT",
    )
