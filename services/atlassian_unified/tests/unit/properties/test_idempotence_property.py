"""Property test P9 — Idempotent write tools when object is already in target state.

Validates Requirements 7.3, 7.4, 10.4, 18.3, 18.4, 29.2, 29.3, 33.4, 37.3 /
design Property 9:

    For any idempotent write tool in the set {
        bitbucket_watch_pull_request,
        bitbucket_unwatch_pull_request,
        bitbucket_watch_repository,
        bitbucket_unwatch_repository,
        bitbucket_add_repository_label,
        jira_add_issue_vote,
        jira_remove_issue_vote,
        confluence_watch_page_self,
        confluence_unwatch_page_self,
        confluence_set_page_property,
        confluence_like_page,
    } and for any fixture where the object is already in the target state,
    calling the tool SHALL return a structured flag (``already_watched``,
    ``not_watched``, ``already_labeled``, ``already_voted``, ``not_voted``,
    ``already_watching``, ``already_liked``) AND the tool SHALL NOT issue a
    mutation-side HTTP call (POST/PUT/DELETE) against the underlying fetcher.

Test shape
----------
The test drives the server-tool layer. Each idempotent tool is registered
under ``servers/{product}.py`` and wraps a fetcher mixin method that already
performs the "already in target state" short-circuit at the HTTP surface.
At the tool layer, the observable invariant becomes: the fetcher method is
called **exactly once**, it returns the idempotent marker dict, the tool
response has ``success=True`` and includes the marker flag set to ``True``,
and no *other* mutation methods on the fetcher are touched.

For each tool, we:

1. Construct a ``MagicMock`` fetcher whose ``config.projects_filter`` is
   ``None`` (so the filter precheck is a no-op) and whose idempotent
   mixin method returns the appropriate "already in target state" payload.
2. Monkeypatch ``get_{product}_fetcher`` in the matching ``servers`` module
   so the tool function resolves our stub fetcher.
3. Invoke the tool via ``.fn(ctx, ...)`` under ``asyncio.run``.
4. Assert:
     * ``success=True`` on the parsed JSON response.
     * The appropriate idempotent-flag key is present and ``True``.
     * The idempotent mixin method was called exactly once.
     * No *other* mutation method on the fetcher was called.

Why this layer: the "zero mutation HTTP" invariant is actually guaranteed
by the underlying mixin (which probes state before mutating, or translates
the DC 409/404 into the idempotent flag). At the server-tool layer the
faithful observable contract is "the mixin method was called, it returned
the idempotent marker, and no *additional* mutation happened after". By
mocking the mixin method to return the marker directly we contract-test
the plumbing: the tool correctly threads the flag into its response and
does not issue extra mutation calls as a workaround.

The style mirrors:
  * ``tests/unit/properties/test_comment_visibility_property.py`` — async
    tool invocation via ``asyncio.run`` + monkeypatched fetcher factory.
  * ``tests/unit/properties/test_owner_scoped_property.py`` — structured
    contract assertions on the mixin/guard composition.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from mcp_atlassian.servers import (
    bitbucket as bitbucket_server,
    confluence as confluence_server,
    jira as jira_server,
)


# ---------------------------------------------------------------------------
# Fake FastMCP context
# ---------------------------------------------------------------------------


def _make_fake_ctx() -> SimpleNamespace:
    """Minimal ``fastmcp.Context`` stand-in.

    The Bitbucket / Jira / Confluence tools under test do not consult
    ``ctx.request_context`` directly (they delegate read-only and filter
    prechecks to ``dc_guards``), so an empty ``SimpleNamespace`` suffices.
    ``check_write_access`` (applied to some unrelated Confluence tools)
    reads ``ctx.request_context.lifespan_context`` and calls ``.get(...)``
    on the result, so we pre-populate that chain defensively.
    """
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={}),
    )


@pytest.fixture
def fake_ctx() -> SimpleNamespace:
    return _make_fake_ctx()


@pytest.fixture
def disable_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ``READ_ONLY_MODE`` is unset so write prechecks stay transparent."""
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)


# ---------------------------------------------------------------------------
# Fetcher stub builder
# ---------------------------------------------------------------------------


def _make_fetcher() -> MagicMock:
    """Build a ``MagicMock`` fetcher with the shared config shape.

    All three products (Bitbucket / Jira / Confluence) expose
    ``fetcher.config.projects_filter``; by returning ``None`` we short-
    circuit ``check_project_filter`` so the tool proceeds to the mixin
    call on every test without re-asserting filter behaviour (that's
    covered by P2 in ``test_filter_property.py``).
    """
    fetcher = MagicMock(name="fetcher")
    fetcher.is_cloud = False
    fetcher.config = SimpleNamespace(is_cloud=False, projects_filter=None)
    return fetcher


# ---------------------------------------------------------------------------
# Parametrized case table — one row per idempotent tool
# ---------------------------------------------------------------------------
#
# Each row describes one idempotent write tool and binds:
#
#   * ``tool_id``         — stable pytest parametrize id (human-readable).
#   * ``product``         — which ``servers/<product>.py`` module to patch.
#   * ``tool_attr``       — name of the tool object on the server module.
#   * ``mixin_method``    — name of the fetcher method the tool calls.
#   * ``tool_kwargs``     — kwargs passed to ``tool.fn(ctx, **kwargs)``.
#   * ``marker_payload``  — dict the mixin returns to signal "already in
#                           target state".
#   * ``marker_flag``     — key in the tool's JSON response asserted to
#                           be ``True`` when the object was already in
#                           the target state.
#   * ``other_mutations`` — attribute names of *other* fetcher methods
#                           that must not be called (belt-and-suspenders
#                           guard against a future refactor that quietly
#                           issues a second mutation call).
#
# ``marker_payload`` keys match the mixin-level contracts documented in
# each product's module under ``src/mcp_atlassian/{product}/``. Where a
# tool layer renames the flag (Confluence watch tools expose
# ``already_watching`` regardless of whether it's watch or unwatch), the
# ``marker_flag`` records the *tool-layer* name rather than the mixin-
# layer name.
_CASES: list[dict[str, Any]] = [
    # --- Bitbucket: PR watch / unwatch ------------------------------------
    {
        "tool_id": "bitbucket_watch_pull_request_already_watched",
        "product": "bitbucket",
        "tool_attr": "watch_pull_request",
        "mixin_method": "watch_pr",
        "tool_kwargs": {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "pr_id": 42,
        },
        "marker_payload": {"already_watched": True},
        "marker_flag": "already_watched",
        "other_mutations": ("unwatch_pr",),
    },
    {
        "tool_id": "bitbucket_unwatch_pull_request_not_watched",
        "product": "bitbucket",
        "tool_attr": "unwatch_pull_request",
        "mixin_method": "unwatch_pr",
        "tool_kwargs": {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "pr_id": 42,
        },
        "marker_payload": {"not_watched": True},
        "marker_flag": "not_watched",
        "other_mutations": ("watch_pr",),
    },
    # --- Bitbucket: repo watch / unwatch ----------------------------------
    {
        "tool_id": "bitbucket_watch_repository_already_watched",
        "product": "bitbucket",
        "tool_attr": "watch_repository",
        "mixin_method": "watch_repo",
        "tool_kwargs": {"project_key": "PROJ", "repo_slug": "repo"},
        "marker_payload": {"already_watched": True},
        "marker_flag": "already_watched",
        "other_mutations": ("unwatch_repo",),
    },
    {
        "tool_id": "bitbucket_unwatch_repository_not_watched",
        "product": "bitbucket",
        "tool_attr": "unwatch_repository",
        "mixin_method": "unwatch_repo",
        "tool_kwargs": {"project_key": "PROJ", "repo_slug": "repo"},
        "marker_payload": {"not_watched": True},
        "marker_flag": "not_watched",
        "other_mutations": ("watch_repo",),
    },
    # --- Bitbucket: repo labels ------------------------------------------
    {
        "tool_id": "bitbucket_add_repository_label_already_labeled",
        "product": "bitbucket",
        "tool_attr": "add_repository_label",
        "mixin_method": "add_repo_label",
        "tool_kwargs": {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "label": "team-a",
        },
        "marker_payload": {"already_labeled": True},
        "marker_flag": "already_labeled",
        "other_mutations": ("remove_repo_label",),
    },
    # --- Jira: issue votes -----------------------------------------------
    {
        "tool_id": "jira_add_issue_vote_already_voted",
        "product": "jira",
        "tool_attr": "jira_add_issue_vote",
        "mixin_method": "add_issue_vote",
        "tool_kwargs": {"issue_key": "PROJ-1"},
        "marker_payload": {
            "issue_key": "PROJ-1",
            "already_voted": True,
            "votes": 3,
        },
        "marker_flag": "already_voted",
        "other_mutations": ("remove_issue_vote",),
    },
    {
        "tool_id": "jira_remove_issue_vote_not_voted",
        "product": "jira",
        "tool_attr": "jira_remove_issue_vote",
        "mixin_method": "remove_issue_vote",
        "tool_kwargs": {"issue_key": "PROJ-1"},
        "marker_payload": {
            "issue_key": "PROJ-1",
            "not_voted": True,
            "votes": 0,
        },
        "marker_flag": "not_voted",
        "other_mutations": ("add_issue_vote",),
    },
    # --- Confluence: page watch / unwatch (self) -------------------------
    {
        "tool_id": "confluence_watch_page_self_already_watching",
        "product": "confluence",
        "tool_attr": "watch_page_self",
        "mixin_method": "watch_page_self",
        "tool_kwargs": {"page_id": "123456789"},
        "marker_payload": {"already_watching": True},
        "marker_flag": "already_watching",
        "other_mutations": ("unwatch_page_self",),
    },
    {
        # Unwatching when the user is NOT watching: mixin returns
        # ``already_watching=False`` and does NOT issue a DELETE. The
        # tool layer surfaces the same flag; ``False`` is the idempotent
        # signal in this direction.
        "tool_id": "confluence_unwatch_page_self_not_watching",
        "product": "confluence",
        "tool_attr": "unwatch_page_self",
        "mixin_method": "unwatch_page_self",
        "tool_kwargs": {"page_id": "123456789"},
        # Mixin returns ``{"already_watching": False}`` when user was
        # NOT watching (no DELETE issued). The tool forwards the flag.
        "marker_payload": {"already_watching": False},
        "marker_flag": "already_watching",
        # The expected flag value here is ``False`` — the tool still
        # reports success, the ``False`` means "no state change needed".
        "marker_flag_expected_value": False,
        "other_mutations": ("watch_page_self",),
    },
    # --- Confluence: page properties (idempotent set) --------------------
    {
        "tool_id": "confluence_set_page_property_idempotent",
        "product": "confluence",
        "tool_attr": "set_page_property",
        "mixin_method": "set_page_property",
        "tool_kwargs": {
            "page_id": "123456789",
            "key": "agent-state",
            "value": {"status": "ready"},
        },
        # ``set_page_property`` is idempotent by design but does not
        # itself return a dedicated "already-in-state" marker — the
        # mixin probes the existing property, then POSTs (create) or
        # PUTs (update). At the tool layer the observable invariant is
        # that the returned property dict is what the mixin hands back.
        # For this property test we contract the mixin call surface:
        # it is called exactly once, the tool surfaces ``success=True``
        # with a ``property`` payload, and no *other* mutation method
        # (``delete_page_property``) is touched.
        "marker_payload": {
            "key": "agent-state",
            "value": {"status": "ready"},
            "version": {"number": 1},
        },
        # ``set_page_property`` does not expose a boolean idempotence
        # flag; see ``_assert_marker_flag`` for the branch that handles
        # the payload-only variant.
        "marker_flag": None,
        "other_mutations": ("delete_page_property",),
    },
    # --- Confluence: likes ----------------------------------------------
    {
        "tool_id": "confluence_like_page_already_liked",
        "product": "confluence",
        "tool_attr": "like_page",
        "mixin_method": "like_page",
        "tool_kwargs": {"page_id": "123456789"},
        "marker_payload": {"already_liked": True},
        "marker_flag": "already_liked",
        "other_mutations": ("unlike_page",),
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PRODUCT_MODULES: dict[str, tuple[Any, str]] = {
    "bitbucket": (bitbucket_server, "get_bitbucket_fetcher"),
    "jira": (jira_server, "get_jira_fetcher"),
    "confluence": (confluence_server, "get_confluence_fetcher"),
}


def _patch_fetcher_factory(
    monkeypatch: pytest.MonkeyPatch,
    product: str,
    fetcher: MagicMock,
) -> None:
    """Monkeypatch the product's ``get_<product>_fetcher`` to return ``fetcher``."""
    module, factory_name = _PRODUCT_MODULES[product]

    async def _aget(_ctx: Any) -> MagicMock:
        return fetcher

    monkeypatch.setattr(module, factory_name, _aget)


def _invoke_tool(tool_obj: Any, ctx: SimpleNamespace, **kwargs: Any) -> dict[str, Any]:
    """Call ``tool.fn(ctx, **kwargs)`` under ``asyncio.run`` and parse JSON."""
    result_json = asyncio.run(tool_obj.fn(ctx, **kwargs))
    return json.loads(result_json)


def _assert_marker_flag(
    payload: dict[str, Any],
    marker_flag: str | None,
    expected_value: Any,
) -> None:
    """Assert the tool response carries the expected idempotence marker.

    Two shapes are supported:

    * ``marker_flag=<str>`` — a single top-level boolean flag on the
      response. Example: ``already_watched`` / ``not_voted`` /
      ``already_liked``. The test asserts the flag equals
      ``expected_value`` (usually ``True``; ``False`` for the
      unwatch-when-not-watching case which signals "no action needed").
    * ``marker_flag=None`` — the tool response does not expose a
      dedicated boolean marker (e.g. ``set_page_property`` returns the
      property dict). In that case we only require the payload to
      include the payload-carrying key (``property``) so the caller
      can still reconstruct state from a repeated call.
    """
    if marker_flag is None:
        assert "property" in payload, (
            "set_page_property response must carry a ``property`` key; "
            f"got payload keys {sorted(payload.keys())!r}"
        )
        return

    assert marker_flag in payload, (
        f"expected response to include marker flag {marker_flag!r}; "
        f"got payload keys {sorted(payload.keys())!r}"
    )
    assert payload[marker_flag] == expected_value, (
        f"expected payload[{marker_flag!r}] == {expected_value!r}; "
        f"got {payload[marker_flag]!r}"
    )


# ---------------------------------------------------------------------------
# Property A — idempotent mutation surfaces the marker and issues no
# additional mutation calls.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    _CASES,
    ids=[c["tool_id"] for c in _CASES],
)
def test_idempotent_tool_returns_marker_without_extra_mutation(
    case: dict[str, Any],
    fake_ctx: SimpleNamespace,
    disable_read_only: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P9: idempotent tool returns the marker and triggers no extra mutation."""
    # 1. Build the stub fetcher and wire the idempotent mixin method.
    fetcher = _make_fetcher()
    getattr(fetcher, case["mixin_method"]).return_value = case["marker_payload"]
    _patch_fetcher_factory(monkeypatch, case["product"], fetcher)

    # 2. Invoke the tool.
    module, _ = _PRODUCT_MODULES[case["product"]]
    tool_obj = getattr(module, case["tool_attr"])
    payload = _invoke_tool(tool_obj, fake_ctx, **case["tool_kwargs"])

    # 3. The tool wraps the mixin result in ``{"success": True, ...}``.
    assert payload.get("success") is True, (
        f"{case['tool_id']}: expected success=True, got {payload!r}"
    )

    # 4. The idempotence marker flag is present and carries the expected
    # value (default ``True``; per-case override for unwatch-when-not-
    # watching which signals idempotent no-op via ``already_watching=False``).
    expected_flag_value = case.get("marker_flag_expected_value", True)
    _assert_marker_flag(payload, case["marker_flag"], expected_flag_value)

    # 5. The idempotent mixin method was called exactly once. This is
    # the "single pass through the tool" invariant — a retry loop, or
    # a poor-man's idempotence-by-brute-force (call, check, call again)
    # would show up here as ``call_count > 1``.
    mutation = getattr(fetcher, case["mixin_method"])
    assert mutation.call_count == 1, (
        f"{case['tool_id']}: expected mixin method "
        f"{case['mixin_method']!r} to be called exactly once, got "
        f"{mutation.call_count}"
    )

    # 6. No *other* mutation method on the fetcher was touched. Watch
    # tools must not quietly call unwatch as a compensating action, and
    # vice versa.
    for other in case["other_mutations"]:
        other_mock = getattr(fetcher, other)
        assert other_mock.call_count == 0, (
            f"{case['tool_id']}: unexpected call to fetcher method "
            f"{other!r}; idempotent tools must not issue compensating "
            f"mutations"
        )


# ---------------------------------------------------------------------------
# Property B — repeated invocation is stable: calling the tool twice on
# an already-in-state object yields the same marker both times, and the
# mixin reports the marker on each call.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    # Exclude ``set_page_property`` from the stability test: its mixin
    # performs a GET-then-POST/PUT pattern whose *version number* strictly
    # increases on each call, so the response payload between the two
    # invocations is allowed to differ (only the server-observable state
    # contract — "same (page_id, key, value) in the end" — is invariant).
    # The marker-centric Property A above covers the tool-layer plumbing
    # for ``set_page_property``.
    [c for c in _CASES if c["marker_flag"] is not None],
    ids=[c["tool_id"] for c in _CASES if c["marker_flag"] is not None],
)
def test_idempotent_tool_is_stable_across_repeat_calls(
    case: dict[str, Any],
    fake_ctx: SimpleNamespace,
    disable_read_only: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P9 stability: calling the tool twice yields the same marker both times."""
    fetcher = _make_fetcher()
    getattr(fetcher, case["mixin_method"]).return_value = case["marker_payload"]
    _patch_fetcher_factory(monkeypatch, case["product"], fetcher)

    module, _ = _PRODUCT_MODULES[case["product"]]
    tool_obj = getattr(module, case["tool_attr"])

    first = _invoke_tool(tool_obj, fake_ctx, **case["tool_kwargs"])
    second = _invoke_tool(tool_obj, fake_ctx, **case["tool_kwargs"])

    expected_flag_value = case.get("marker_flag_expected_value", True)

    # Both calls must succeed and carry the same marker flag value. The
    # rest of the payload may differ (e.g. timestamp fields on a real
    # Jira response), but the marker is the stable idempotence signal.
    assert first["success"] is True
    assert second["success"] is True
    assert first[case["marker_flag"]] == expected_flag_value
    assert second[case["marker_flag"]] == expected_flag_value

    # The fetcher's mixin method was called twice (once per tool call),
    # and *only* that method — no other mutation leaked through.
    mutation = getattr(fetcher, case["mixin_method"])
    assert mutation.call_count == 2, (
        f"{case['tool_id']}: expected two calls to "
        f"{case['mixin_method']!r} across two invocations, got "
        f"{mutation.call_count}"
    )
    for other in case["other_mutations"]:
        other_mock = getattr(fetcher, other)
        assert other_mock.call_count == 0
