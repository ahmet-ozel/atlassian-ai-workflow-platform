"""Property test P9 — DC-only tools emit ``not_supported_on_cloud`` pre-HTTP.

Validates Requirements 9.9, 9.10, 11.10, 13.4, 14.1, 14.2, 14.3, 14.4,
14.5, 14.6, 14.7, 14.8, 14.9, 14.10 of the
``bitbucket-cloud-dc-parity`` spec / design Property 9.

For every tool in the DC-only set (Section 5 of design "Components and
Interfaces", Requirements 14.1–14.9 and 9.9, 9.10, 11.10, 13.4),
invocation in CloudMode SHALL:

1. Return a JSON envelope whose parsed payload contains
   ``success == False`` and ``error_code == "not_supported_on_cloud"``.
2. Populate ``details`` with the exact tool name (``details["tool"]``),
   ``details["effective_mode"] == "cloud"``, and
   ``details["required_mode"] == "dc"`` (design "New error codes" table
   + Requirement 14.10).
3. Emit **zero** outbound HTTP calls on the underlying
   ``atlassian.Bitbucket`` session (``bb.bitbucket.get`` /
   ``post`` / ``put`` / ``delete`` all at ``call_count == 0``).
4. Never dispatch into the mixin method the tool would normally call.

These invariants are the `property-based` complement of the example-based
suite in :mod:`tests.unit.bitbucket.test_dc_only_mode_guard`: that file
pins the guarantees on one fixed example per tool, while this property
test randomises the plausible arg values (project keys, repo slugs, PR
/ comment / rule ids, reaction emojis, label strings, commit SHAs, etc.)
using Hypothesis and iterates the same invariants across every DC-only
tool. Randomising args guards against a regression where the guard only
fires for a specific arg shape (e.g. an ``int`` ``rule_id`` but not a
string-coerced one).

Scope — the 23 DC-only tools
----------------------------

The DC-only set as frozen by Requirements 14.1–14.9 plus the four
narrowed one-offs (9.9, 9.10, 11.10, 13.4):

* ``bitbucket_default_reviewers`` toolset (Req 14.1) — 5 tools.
* ``bitbucket_required_builds`` toolset (Req 14.2) — 3 tools.
* ``bitbucket_render_markup`` (Req 14.3) — 1 tool.
* ``bitbucket_repo_labels`` toolset (Req 14.4) — 3 tools.
* ``bitbucket_deployments`` toolset (Req 14.5) — 2 tools.
* ``bitbucket_get_branching_model`` (Req 14.6) — 1 tool.
* ``bitbucket_list_pull_request_participants`` (Req 14.7) — 1 tool.
* ``bitbucket_create_project`` / ``bitbucket_update_project`` (Req 14.8)
  — 2 tools.
* ``bitbucket_fork_repository`` (Req 14.9) — 1 tool.
* ``bitbucket_add_pr_comment_reaction`` /
  ``bitbucket_remove_pr_comment_reaction`` (Req 9.9, 9.10) — 2 tools.
* ``bitbucket_cherry_pick_commit`` (Req 11.10) — 1 tool.
* ``bitbucket_search_users`` (Req 13.4) — 1 tool.

Total: 23 DC-only tools.

Strategy
--------

Each tool is parametrised by pytest; Hypothesis generates plausible
per-tool keyword arguments via a dedicated ``@st.composite`` strategy.
The tool body is invoked through its FastMCP ``.fn`` attribute (bypassing
the decorator chain that otherwise requires a live server). The fetcher
is a :class:`MagicMock` with a :class:`types.SimpleNamespace` config —
the narrow config surface the tool bodies actually read — and fresh
HTTP mocks so ``call_count`` assertions are tight.

Style reference: :mod:`tests.unit.bitbucket.test_dc_only_mode_guard`
(which carries the exhaustive comment-block explaining each fixture).
"""

from __future__ import annotations

import asyncio
import json
import string
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Shared primitive strategies
# ---------------------------------------------------------------------------


# Bitbucket DC project keys: uppercase ASCII letters + digits, 2–10 chars,
# must start with a letter. The existing project-filter precheck upper-
# cases before comparison, so a narrower alphabet is fine here; the goal
# is merely to produce inputs that don't trip an earlier guard.
_PROJECT_KEY_ALPHABET = string.ascii_uppercase + string.digits

project_keys: st.SearchStrategy[str] = st.builds(
    lambda head, tail: head + tail,
    head=st.sampled_from(string.ascii_uppercase),
    tail=st.text(alphabet=_PROJECT_KEY_ALPHABET, min_size=1, max_size=9),
)

# Repo slugs: kebab-case lowercase-ish; accept dots and underscores.
_SLUG_ALPHABET = string.ascii_lowercase + string.digits + "-._"

repo_slugs: st.SearchStrategy[str] = st.text(
    alphabet=_SLUG_ALPHABET,
    min_size=1,
    max_size=20,
).filter(lambda s: s[0].isalnum())

# Positive ids (rule_id / condition_id / pr_id / comment_id / deployment
# numeric id). Kept small-but-diverse so Hypothesis can shrink cleanly.
positive_ids: st.SearchStrategy[int] = st.integers(min_value=1, max_value=10_000)

# Usernames: alphanumeric + ``_.-``, length 1–24. Used as reviewer names
# and search-user filter text.
usernames: st.SearchStrategy[str] = st.text(
    alphabet=string.ascii_letters + string.digits + "_.-",
    min_size=1,
    max_size=24,
)

# Reaction emojis — Bitbucket accepts a curated vocabulary. Sampling a
# small set keeps arg generation realistic while still randomising which
# one is passed on any given example.
emoji_strategy: st.SearchStrategy[str] = st.sampled_from(
    ("+1", "-1", "smile", "heart", "hooray", "laugh", "confused", "rocket")
)

# Markup source text — at least one non-whitespace character so the tool
# body doesn't reject the payload on an earlier length check (it does not
# currently do so, but this keeps the test robust against such an
# addition in future).
markup_text_strategy: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
    min_size=1,
    max_size=120,
).filter(lambda s: s.strip() != "")

# Short labels — Bitbucket DC repo labels are <=32 chars lowercase/hyphen.
labels: st.SearchStrategy[str] = st.text(
    alphabet=string.ascii_lowercase + string.digits + "-",
    min_size=1,
    max_size=32,
).filter(lambda s: s[0].isalnum())

# 40-char hex commit SHA — the canonical Git SHA shape.
commit_shas: st.SearchStrategy[str] = st.text(
    alphabet="0123456789abcdef",
    min_size=7,
    max_size=40,
)

# Branch refs — realistic ``refs/heads/<name>`` or bare branch names.
branch_refs: st.SearchStrategy[str] = st.one_of(
    st.text(
        alphabet=string.ascii_lowercase + string.digits + "-_/.",
        min_size=1,
        max_size=30,
    ).filter(lambda s: s[0].isalnum() and not s.endswith("/")),
    st.sampled_from(("main", "master", "develop", "release/1.0", "refs/heads/main")),
)

# Deployment ids — Cloud uses UUIDs, DC uses numeric strings; both
# accepted by the tool signature (``str``). A small sampled pool keeps
# shrinkage readable.
deployment_ids: st.SearchStrategy[str] = st.one_of(
    st.text(alphabet=string.ascii_lowercase + string.digits + "-", min_size=1, max_size=40),
    st.sampled_from(("deploy-1", "deploy-2", "prod-1", "staging-1")),
)


# ---------------------------------------------------------------------------
# Per-tool composite strategies — each yields a (kwargs, mixin_return) tuple.
#
# The ``mixin_return`` is seeded on the mock so a regression slipping the
# guard wouldn't raise inside ``json.dumps`` and drown the structured-error
# assertions below.
# ---------------------------------------------------------------------------


def _matcher_json(ref_id: str, kind: str = "BRANCH") -> str:
    """Construct the source/target matcher JSON the reviewer tools expect."""
    return json.dumps({"id": ref_id, "type": {"id": kind}})


@st.composite
def _list_default_reviewers_args(draw: st.DrawFn) -> dict[str, Any]:
    return {"project_key": draw(project_keys), "repo_slug": draw(repo_slugs)}


@st.composite
def _get_default_reviewer_rule_args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "project_key": draw(project_keys),
        "repo_slug": draw(repo_slugs),
        "rule_id": draw(positive_ids),
    }


@st.composite
def _create_default_reviewer_rule_args(draw: st.DrawFn) -> dict[str, Any]:
    reviewer_names = draw(st.lists(usernames, min_size=1, max_size=3, unique=True))
    reviewer_json = json.dumps([{"name": n} for n in reviewer_names])
    return {
        "project_key": draw(project_keys),
        "repo_slug": draw(repo_slugs),
        "source_matcher": _matcher_json(
            draw(st.sampled_from(("refs/heads/feature/*", "ANY_REF", "refs/heads/main"))),
            draw(st.sampled_from(("PATTERN", "BRANCH", "ANY_REF"))),
        ),
        "target_matcher": _matcher_json("refs/heads/main", "BRANCH"),
        "reviewers": reviewer_json,
        "required_approvals": draw(st.integers(min_value=0, max_value=5)),
    }


@st.composite
def _update_default_reviewer_rule_args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "project_key": draw(project_keys),
        "repo_slug": draw(repo_slugs),
        "rule_id": draw(positive_ids),
        "required_approvals": draw(st.integers(min_value=0, max_value=5)),
    }


@st.composite
def _delete_default_reviewer_rule_args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "project_key": draw(project_keys),
        "repo_slug": draw(repo_slugs),
        "rule_id": draw(positive_ids),
    }


@st.composite
def _list_required_builds_args(draw: st.DrawFn) -> dict[str, Any]:
    return {"project_key": draw(project_keys), "repo_slug": draw(repo_slugs)}


@st.composite
def _create_required_build_args(draw: st.DrawFn) -> dict[str, Any]:
    parents = draw(
        st.lists(
            st.text(
                alphabet=string.ascii_uppercase + "-",
                min_size=3,
                max_size=20,
            ),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    return {
        "project_key": draw(project_keys),
        "repo_slug": draw(repo_slugs),
        "build_parent_keys": json.dumps(parents),
        "ref_matcher": _matcher_json("refs/heads/main", "BRANCH"),
    }


@st.composite
def _delete_required_build_args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "project_key": draw(project_keys),
        "repo_slug": draw(repo_slugs),
        "condition_id": draw(positive_ids),
    }


@st.composite
def _render_markup_args(draw: st.DrawFn) -> dict[str, Any]:
    return {"markup_text": draw(markup_text_strategy)}


@st.composite
def _list_repository_labels_args(draw: st.DrawFn) -> dict[str, Any]:
    return {"project_key": draw(project_keys), "repo_slug": draw(repo_slugs)}


@st.composite
def _add_repository_label_args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "project_key": draw(project_keys),
        "repo_slug": draw(repo_slugs),
        "label": draw(labels),
    }


@st.composite
def _remove_repository_label_args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "project_key": draw(project_keys),
        "repo_slug": draw(repo_slugs),
        "label": draw(labels),
    }


@st.composite
def _list_deployments_args(draw: st.DrawFn) -> dict[str, Any]:
    return {"project_key": draw(project_keys), "repo_slug": draw(repo_slugs)}


@st.composite
def _get_deployment_args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "project_key": draw(project_keys),
        "repo_slug": draw(repo_slugs),
        "deployment_id": draw(deployment_ids),
    }


@st.composite
def _get_branching_model_args(draw: st.DrawFn) -> dict[str, Any]:
    return {"project_key": draw(project_keys), "repo_slug": draw(repo_slugs)}


@st.composite
def _list_pull_request_participants_args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "project_key": draw(project_keys),
        "repo_slug": draw(repo_slugs),
        "pr_id": draw(positive_ids),
    }


@st.composite
def _create_project_args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "key": draw(project_keys),
        "name": draw(
            st.text(
                alphabet=string.ascii_letters + string.digits + " -_",
                min_size=1,
                max_size=30,
            ).filter(lambda s: s.strip() != "")
        ),
    }


@st.composite
def _update_project_args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "project_key": draw(project_keys),
        "name": draw(
            st.text(
                alphabet=string.ascii_letters + string.digits + " -_",
                min_size=1,
                max_size=30,
            ).filter(lambda s: s.strip() != "")
        ),
    }


@st.composite
def _fork_repository_args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "source_project": draw(project_keys),
        "source_slug": draw(repo_slugs),
        "dest_project": draw(project_keys),
    }


@st.composite
def _add_pr_comment_reaction_args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "project_key": draw(project_keys),
        "repo_slug": draw(repo_slugs),
        "pr_id": draw(positive_ids),
        "comment_id": draw(positive_ids),
        "emoji": draw(emoji_strategy),
    }


@st.composite
def _remove_pr_comment_reaction_args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "project_key": draw(project_keys),
        "repo_slug": draw(repo_slugs),
        "pr_id": draw(positive_ids),
        "comment_id": draw(positive_ids),
        "emoji": draw(emoji_strategy),
    }


@st.composite
def _cherry_pick_commit_args(draw: st.DrawFn) -> dict[str, Any]:
    return {
        "project_key": draw(project_keys),
        "repo_slug": draw(repo_slugs),
        "source_commit": draw(commit_shas),
        "target_branch": draw(branch_refs),
    }


@st.composite
def _search_users_args(draw: st.DrawFn) -> dict[str, Any]:
    return {"filter_text": draw(usernames)}


# ---------------------------------------------------------------------------
# DC-only tool registry
#
# Each entry is:
#   (server_fn_name, expected_tool_name, args_strategy,
#    mixin_attr_on_fetcher, mixin_return_value)
#
# ``mixin_attr`` is the attribute name on the fetcher that the tool would
# dispatch to if the mode guard were absent. Asserting the mock's
# ``call_count == 0`` on this attribute is the final, mixin-level check
# that the short-circuit landed before any business logic ran.
# ---------------------------------------------------------------------------


DC_ONLY_TOOLS: list[tuple[str, str, st.SearchStrategy[dict[str, Any]], str, Any]] = [
    # Default reviewers (Req 14.1)
    (
        "list_default_reviewers",
        "bitbucket_list_default_reviewers",
        _list_default_reviewers_args(),
        "list_default_reviewers",
        [],
    ),
    (
        "get_default_reviewer_rule",
        "bitbucket_get_default_reviewer_rule",
        _get_default_reviewer_rule_args(),
        "get_default_reviewer_rule",
        {},
    ),
    (
        "create_default_reviewer_rule",
        "bitbucket_create_default_reviewer_rule",
        _create_default_reviewer_rule_args(),
        "create_default_reviewer_rule",
        {},
    ),
    (
        "update_default_reviewer_rule",
        "bitbucket_update_default_reviewer_rule",
        _update_default_reviewer_rule_args(),
        "update_default_reviewer_rule",
        {},
    ),
    (
        "delete_default_reviewer_rule",
        "bitbucket_delete_default_reviewer_rule",
        _delete_default_reviewer_rule_args(),
        "delete_default_reviewer_rule",
        None,
    ),
    # Required builds (Req 14.2)
    (
        "list_required_builds",
        "bitbucket_list_required_builds",
        _list_required_builds_args(),
        "list_required_builds",
        [],
    ),
    (
        "create_required_build",
        "bitbucket_create_required_build",
        _create_required_build_args(),
        "create_required_build",
        {},
    ),
    (
        "delete_required_build",
        "bitbucket_delete_required_build",
        _delete_required_build_args(),
        "delete_required_build",
        None,
    ),
    # Markup preview (Req 14.3)
    (
        "render_markup",
        "bitbucket_render_markup",
        _render_markup_args(),
        "render_markup",
        "<p>hello</p>",
    ),
    # Repository labels (Req 14.4)
    (
        "list_repository_labels",
        "bitbucket_list_repository_labels",
        _list_repository_labels_args(),
        "list_repo_labels",
        [],
    ),
    (
        "add_repository_label",
        "bitbucket_add_repository_label",
        _add_repository_label_args(),
        "add_repo_label",
        {"label": "hot", "already_labeled": False},
    ),
    (
        "remove_repository_label",
        "bitbucket_remove_repository_label",
        _remove_repository_label_args(),
        "remove_repo_label",
        None,
    ),
    # Deployments (Req 14.5)
    (
        "list_deployments",
        "bitbucket_list_deployments",
        _list_deployments_args(),
        "list_deployments",
        [],
    ),
    (
        "get_deployment",
        "bitbucket_get_deployment",
        _get_deployment_args(),
        "get_deployment",
        {},
    ),
    # Branching model (Req 14.6)
    (
        "get_branching_model",
        "bitbucket_get_branching_model",
        _get_branching_model_args(),
        "get_branching_model",
        {},
    ),
    # PR participants (Req 14.7) — mixin attr is ``list_pr_participants``
    (
        "list_pull_request_participants",
        "bitbucket_list_pull_request_participants",
        _list_pull_request_participants_args(),
        "list_pr_participants",
        [],
    ),
    # Project admin (Req 14.8)
    (
        "create_project",
        "bitbucket_create_project",
        _create_project_args(),
        "create_project",
        {},
    ),
    (
        "update_project",
        "bitbucket_update_project",
        _update_project_args(),
        "update_project",
        {},
    ),
    # Fork (Req 14.9)
    (
        "fork_repository",
        "bitbucket_fork_repository",
        _fork_repository_args(),
        "fork_repository",
        {},
    ),
    # PR comment reactions (Req 9.9, 9.10)
    (
        "add_pr_comment_reaction",
        "bitbucket_add_pr_comment_reaction",
        _add_pr_comment_reaction_args(),
        "add_pr_comment_reaction",
        {},
    ),
    (
        "remove_pr_comment_reaction",
        "bitbucket_remove_pr_comment_reaction",
        _remove_pr_comment_reaction_args(),
        "remove_pr_comment_reaction",
        None,
    ),
    # Cherry-pick (Req 11.10)
    (
        "cherry_pick_commit",
        "bitbucket_cherry_pick_commit",
        _cherry_pick_commit_args(),
        "cherry_pick_commit",
        {"id": "newsha1", "displayId": "newsha1"},
    ),
    # User search (Req 13.4)
    (
        "search_users",
        "bitbucket_search_users",
        _search_users_args(),
        "search_users",
        [],
    ),
]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


class _FakeContext:
    """Minimal ``fastmcp.Context`` stand-in — the DC-only tools don't read it."""


def _make_fetcher(*, is_cloud: bool) -> MagicMock:
    """Build a ``MagicMock`` fetcher wired for ``is_cloud`` with zero HTTP.

    Mirrors the helper in :mod:`tests.unit.bitbucket.test_dc_only_mode_guard`.
    Uses :class:`SimpleNamespace` for ``config`` so attribute typos surface
    as test failures rather than silently returning fresh ``MagicMock``s.
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
    # Modern DC version so downstream ``check_dc_version`` never trips on
    # the deployments / reactions gates (not invoked here, but keeps the
    # fetcher realistic).
    fetcher.get_dc_version = MagicMock(return_value="9.4.0")
    fetcher._dc_version = "9.4.0"
    return fetcher


def _install_fetcher(monkeypatch, fetcher: MagicMock) -> None:
    """Patch ``get_bitbucket_fetcher`` on the bitbucket server module."""
    from mcp_atlassian.servers import bitbucket as bb_server

    async def _aget(_ctx):
        return fetcher

    monkeypatch.setattr(bb_server, "get_bitbucket_fetcher", _aget)


# ---------------------------------------------------------------------------
# Property P9 — CloudMode short-circuit with zero outbound HTTP
# ---------------------------------------------------------------------------
#
# One parametrised test per DC-only tool; each one wraps an inner
# Hypothesis ``@given`` over that tool's args strategy. This split lets
# pytest surface a failing tool by name (``bitbucket_cherry_pick_commit``)
# while Hypothesis iterates example shapes within it.


@pytest.mark.parametrize(
    "fn_name,tool_name,args_strategy,mixin_attr,mixin_return",
    DC_ONLY_TOOLS,
    ids=[entry[1] for entry in DC_ONLY_TOOLS],
)
def test_dc_only_tool_returns_not_supported_on_cloud_with_zero_http(
    fn_name: str,
    tool_name: str,
    args_strategy: st.SearchStrategy[dict[str, Any]],
    mixin_attr: str,
    mixin_return: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P9: every DC-only tool in CloudMode short-circuits pre-HTTP.

    For every arg shape drawn from the per-tool Hypothesis strategy,
    invoking the tool with ``is_cloud=True`` must:

    * Return ``success=False`` and ``error_code="not_supported_on_cloud"``.
    * Populate ``details`` with ``tool=<tool_name>``,
      ``effective_mode="cloud"``, ``required_mode="dc"``.
    * Leave every HTTP method on ``bb.bitbucket`` at ``call_count == 0``.
    * Never invoke the mixin method ``mixin_attr``.

    Validates: Requirements 9.9, 9.10, 11.10, 13.4, 14.1, 14.2, 14.3,
    14.4, 14.5, 14.6, 14.7, 14.8, 14.9, 14.10.
    """
    import mcp_atlassian.servers.bitbucket as bb_server

    # ``READ_ONLY_MODE`` must be unset so earlier ``check_read_only`` guards
    # don't short-circuit with a different error code and mask the mode-
    # guard failure mode we are verifying.
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)

    tool = getattr(bb_server, fn_name)

    @given(kwargs=args_strategy)
    @settings(
        max_examples=25,
        deadline=None,
        suppress_health_check=(
            HealthCheck.function_scoped_fixture,
            HealthCheck.too_slow,
        ),
    )
    def _property(kwargs: dict[str, Any]) -> None:
        # Fresh fetcher per example so ``call_count`` assertions are
        # isolated from prior-example state.
        fetcher = _make_fetcher(is_cloud=True)
        getattr(fetcher, mixin_attr).return_value = mixin_return
        _install_fetcher(monkeypatch, fetcher)

        fake_ctx = _FakeContext()
        result_json = asyncio.run(tool.fn(fake_ctx, **kwargs))
        payload = json.loads(result_json)

        # (1) + (2): structured error envelope.
        assert payload.get("success") is False, (
            f"{tool_name}: expected success=False in CloudMode; "
            f"payload={payload!r}; kwargs={kwargs!r}"
        )
        assert payload.get("error_code") == "not_supported_on_cloud", (
            f"{tool_name}: expected error_code='not_supported_on_cloud', "
            f"got {payload.get('error_code')!r}; kwargs={kwargs!r}"
        )

        details = payload.get("details") or {}
        assert details.get("tool") == tool_name, (
            f"{tool_name}: details.tool should echo {tool_name!r}, "
            f"got {details.get('tool')!r}"
        )
        assert details.get("effective_mode") == "cloud", (
            f"{tool_name}: details.effective_mode should be 'cloud', "
            f"got {details.get('effective_mode')!r}"
        )
        assert details.get("required_mode") == "dc", (
            f"{tool_name}: details.required_mode should be 'dc', "
            f"got {details.get('required_mode')!r}"
        )

        # (3): zero outbound HTTP on the atlassian.Bitbucket session.
        assert fetcher.bitbucket.get.call_count == 0, (
            f"{tool_name} leaked {fetcher.bitbucket.get.call_count} GETs "
            f"in CloudMode; kwargs={kwargs!r}"
        )
        assert fetcher.bitbucket.post.call_count == 0, (
            f"{tool_name} leaked {fetcher.bitbucket.post.call_count} POSTs "
            f"in CloudMode; kwargs={kwargs!r}"
        )
        assert fetcher.bitbucket.put.call_count == 0, (
            f"{tool_name} leaked {fetcher.bitbucket.put.call_count} PUTs "
            f"in CloudMode; kwargs={kwargs!r}"
        )
        assert fetcher.bitbucket.delete.call_count == 0, (
            f"{tool_name} leaked {fetcher.bitbucket.delete.call_count} "
            f"DELETEs in CloudMode; kwargs={kwargs!r}"
        )

        # (4): the mixin method itself was never invoked.
        mixin_mock = getattr(fetcher, mixin_attr)
        assert mixin_mock.call_count == 0, (
            f"{tool_name} reached the {mixin_attr!r} mixin method in "
            f"CloudMode; kwargs={kwargs!r}"
        )

    _property()


# ---------------------------------------------------------------------------
# Sanity — the curated registry covers exactly the DC-only set.
# ---------------------------------------------------------------------------


def test_registry_covers_the_expected_dc_only_tools() -> None:
    """The curated registry must cover the 23 DC-only tools (Req 14.1–14.9,
    9.9, 9.10, 11.10, 13.4). A drift in either direction — extra or
    missing — surfaces here rather than silently skewing the property.
    """
    expected = {
        # Req 14.1
        "bitbucket_list_default_reviewers",
        "bitbucket_get_default_reviewer_rule",
        "bitbucket_create_default_reviewer_rule",
        "bitbucket_update_default_reviewer_rule",
        "bitbucket_delete_default_reviewer_rule",
        # Req 14.2
        "bitbucket_list_required_builds",
        "bitbucket_create_required_build",
        "bitbucket_delete_required_build",
        # Req 14.3
        "bitbucket_render_markup",
        # Req 14.4
        "bitbucket_list_repository_labels",
        "bitbucket_add_repository_label",
        "bitbucket_remove_repository_label",
        # Req 14.5
        "bitbucket_list_deployments",
        "bitbucket_get_deployment",
        # Req 14.6
        "bitbucket_get_branching_model",
        # Req 14.7
        "bitbucket_list_pull_request_participants",
        # Req 14.8
        "bitbucket_create_project",
        "bitbucket_update_project",
        # Req 14.9
        "bitbucket_fork_repository",
        # Req 9.9, 9.10
        "bitbucket_add_pr_comment_reaction",
        "bitbucket_remove_pr_comment_reaction",
        # Req 11.10
        "bitbucket_cherry_pick_commit",
        # Req 13.4
        "bitbucket_search_users",
    }
    registered = {entry[1] for entry in DC_ONLY_TOOLS}
    assert registered == expected, (
        f"DC-only registry drift. Extra: {registered - expected!r}; "
        f"Missing: {expected - registered!r}"
    )
    assert len(DC_ONLY_TOOLS) == 23, (
        f"DC-only registry should have exactly 23 tools; got {len(DC_ONLY_TOOLS)}"
    )
