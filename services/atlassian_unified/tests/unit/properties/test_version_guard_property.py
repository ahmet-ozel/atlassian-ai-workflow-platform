"""Property test P4 — DC version guard emits structured error before HTTP.

Validates Requirements 2.6, 6.3, 11.4, 26.3, 45.1, 45.2, 45.3 / design
Property 4: every DC-version-gated tool (``check_dc_version(required=...)``
in its prelude) must, when the fetcher's probed DC version is strictly
below the declared minimum, return a structured
``{"success": False, "error_code": "dc_version_too_old", "details":
{"required_version": ..., "detected_version": ...}}`` envelope **before**
issuing any outbound HTTP traffic to the business endpoint.

Coverage
--------
The property is exercised over every version-gated tool introduced by
the ``atlassian-dc-tool-parity`` spec:

* **Webhooks** (DC 5.4+; Req 2.6): ``bitbucket_list_webhooks``,
  ``bitbucket_get_webhook``, ``bitbucket_create_webhook``,
  ``bitbucket_update_webhook``, ``bitbucket_delete_webhook``
* **Pull-request reactions** (DC 8.8+; Req 6.3):
  ``bitbucket_add_pr_comment_reaction``,
  ``bitbucket_remove_pr_comment_reaction``
* **Deployments** (DC 7.10+; Req 11.4): ``bitbucket_list_deployments``,
  ``bitbucket_get_deployment``
* **Jira archive** (DC 9.4+; Req 26.3): ``jira_archive_issue``,
  ``jira_restore_issue``

Each case is parametrized as
``(tool_name, server_module, tool_fn_name, required_version,
too_old_version, kwargs, mutation_method_name)``. For every row the
test asserts:

1. The tool returns ``success=False`` with ``error_code ==
   "dc_version_too_old"``.
2. ``details.required_version`` echoes the declared minimum.
3. ``details.detected_version`` echoes the fetcher's cached version.
4. The fetcher's mutation method (the one that would issue the
   outbound call) was **not** invoked — the hard "zero HTTP on
   reject" invariant.

Indeterminate fall-through
--------------------------
Per Requirement 45.3, when the fetcher reports ``_dc_version is None``
(probe not yet run / failed), :func:`dc_guards.check_dc_version` must
return ``None`` so the tool body proceeds to the upstream call and any
resulting 404/501 is mapped to ``dc_version_unknown``. One smoke test
per product verifies that direct invariant on ``check_dc_version``
itself (the mapping to ``dc_version_unknown`` happens in the response
layer of each tool — it is not this property's concern).

Style reference: :mod:`tests.unit.properties.test_comment_visibility_property`
(monkeypatched fetcher + ``get_{product}_fetcher`` shim,
``_call_tool`` helper driving ``.fn`` via ``asyncio.run``).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from mcp_atlassian.servers import bitbucket as bb_server
from mcp_atlassian.servers import jira as jira_server
from mcp_atlassian.utils import dc_guards


# ---------------------------------------------------------------------------
# Test matrix — every version-gated tool, its minimum, and a too-old version
# ---------------------------------------------------------------------------
#
# Each row is:
#
#   (tool_id, product, tool_fn_name, required_version, too_old_version,
#    kwargs, mutation_method_name)
#
# ``tool_id`` is the public tool name as registered with FastMCP; the
# parametrize ``id`` is built from it so ``pytest -v`` output is
# self-explanatory.
# ``tool_fn_name`` is the name of the Python function inside the server
# module (pre-``@tool`` decoration — some tools use shorter internal
# names like ``list_webhooks`` while the registered tool is
# ``bitbucket_list_webhooks``).
# ``mutation_method_name`` is the fetcher method that would issue the
# outbound HTTP on the happy path — the one whose ``call_count`` must be
# exactly zero on reject.


_VERSION_GATE_CASES: list[tuple[str, str, str, str, str, dict[str, Any], str]] = [
    # ---- Webhooks (DC 5.4+) ------------------------------------------------
    (
        "bitbucket_list_webhooks",
        "bitbucket",
        "list_webhooks",
        "5.4",
        "5.3",
        {"project_key": "PROJ", "repo_slug": "repo"},
        "list_webhooks",
    ),
    (
        "bitbucket_get_webhook",
        "bitbucket",
        "get_webhook",
        "5.4",
        "5.3",
        {"project_key": "PROJ", "repo_slug": "repo", "webhook_id": 1},
        "get_webhook",
    ),
    (
        "bitbucket_create_webhook",
        "bitbucket",
        "create_webhook",
        "5.4",
        "5.3",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "name": "hook",
            "url": "https://ci.example.com/hook",
            "events": '["repo:refs_changed"]',
        },
        "create_webhook",
    ),
    (
        "bitbucket_update_webhook",
        "bitbucket",
        "update_webhook",
        "5.4",
        "5.3",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "webhook_id": 1,
            "name": "renamed",
        },
        "update_webhook",
    ),
    (
        "bitbucket_delete_webhook",
        "bitbucket",
        "delete_webhook",
        "5.4",
        "5.3",
        {"project_key": "PROJ", "repo_slug": "repo", "webhook_id": 1},
        "delete_webhook",
    ),
    # ---- PR comment reactions (DC 8.8+) ------------------------------------
    (
        "bitbucket_add_pr_comment_reaction",
        "bitbucket",
        "add_pr_comment_reaction",
        "8.8",
        "8.7",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "pr_id": 42,
            "comment_id": 100,
            "emoji": "+1",
        },
        "add_pr_comment_reaction",
    ),
    (
        "bitbucket_remove_pr_comment_reaction",
        "bitbucket",
        "remove_pr_comment_reaction",
        "8.8",
        "8.7",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "pr_id": 42,
            "comment_id": 100,
            "emoji": "+1",
        },
        "remove_pr_comment_reaction",
    ),
    # ---- Deployments (DC 7.10+) -------------------------------------------
    (
        "bitbucket_list_deployments",
        "bitbucket",
        "list_deployments",
        "7.10",
        "7.9",
        {"project_key": "PROJ", "repo_slug": "repo"},
        "list_deployments",
    ),
    (
        "bitbucket_get_deployment",
        "bitbucket",
        "get_deployment",
        "7.10",
        "7.9",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "deployment_id": "deploy-1",
        },
        "get_deployment",
    ),
    # ---- Jira archive (DC 9.4+) -------------------------------------------
    (
        "jira_archive_issue",
        "jira",
        "jira_archive_issue",
        "9.4",
        "9.3",
        {"issue_key": "PROJ-1"},
        "archive_issue",
    ),
    (
        "jira_restore_issue",
        "jira",
        "jira_restore_issue",
        "9.4",
        "9.3",
        {"issue_key": "PROJ-1"},
        "restore_issue",
    ),
]


# ---------------------------------------------------------------------------
# Fixtures — fake context, fetcher shim, patched get_{product}_fetcher
# ---------------------------------------------------------------------------


class _FakeContext:
    """Minimal stand-in for :class:`fastmcp.Context` used by tool funcs.

    None of the version-gate preludes read anything off the context
    beyond identity (it is threaded into ``get_{product}_fetcher`` which
    we monkeypatch). A bare object suffices.
    """


@pytest.fixture
def fake_ctx() -> _FakeContext:
    return _FakeContext()


@pytest.fixture
def disable_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ``READ_ONLY_MODE`` is unset so the first guard is transparent.

    The version-gate test focuses on precheck #3 (``check_dc_version``).
    Leaving ``READ_ONLY_MODE`` set would short-circuit at precheck #1
    for write-tagged tools, masking the version-gate assertion.
    """
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)


def _build_fetcher(too_old_version: str) -> MagicMock:
    """Build a MagicMock fetcher with a below-required DC version.

    Both the callable ``get_dc_version()`` and the cached ``_dc_version``
    attribute are populated to the same value. :func:`check_dc_version`
    prefers the callable when present (and ``MagicMock`` always makes
    it present), so the explicit return value is what will drive the
    comparison; ``_dc_version`` is set redundantly to keep the shim
    correct under any future refactor that prefers the attribute.

    The ``config.projects_filter`` is ``None`` so precheck #2
    (``check_project_filter``) stays transparent and precheck #3 is the
    one actually exercised.
    """
    fetcher = MagicMock(name="dc-gated-fetcher")
    fetcher.is_cloud = False
    fetcher.config = SimpleNamespace(is_cloud=False, projects_filter=None)
    fetcher.get_dc_version.return_value = too_old_version
    fetcher._dc_version = too_old_version
    return fetcher


def _patch_fetcher(
    monkeypatch: pytest.MonkeyPatch, product: str, fetcher: MagicMock
) -> None:
    """Patch ``get_{product}_fetcher`` on the correct server module.

    The tool functions call ``await get_bitbucket_fetcher(ctx)`` or
    ``await get_jira_fetcher(ctx)`` to resolve the client; we replace
    that call with an async shim that returns our MagicMock.
    """

    async def _aget(_ctx: Any) -> MagicMock:
        return fetcher

    if product == "bitbucket":
        monkeypatch.setattr(bb_server, "get_bitbucket_fetcher", _aget)
    elif product == "jira":
        monkeypatch.setattr(jira_server, "get_jira_fetcher", _aget)
    else:  # pragma: no cover — parametrize rows are closed-set
        raise AssertionError(f"unknown product {product!r}")


def _resolve_tool_fn(product: str, tool_fn_name: str) -> Any:
    """Resolve the server-module attribute for the tool.

    The decorator wraps the async function in a ``Tool`` object whose
    ``.fn`` attribute is the original coroutine function we want to
    drive directly.
    """
    module = bb_server if product == "bitbucket" else jira_server
    return getattr(module, tool_fn_name)


def _call_tool(tool: Any, ctx: _FakeContext, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Invoke ``tool.fn(ctx, **kwargs)`` and return the decoded envelope."""
    result_json = asyncio.run(tool.fn(ctx, **kwargs))
    return json.loads(result_json)


# ---------------------------------------------------------------------------
# Property — below-required version ⇒ dc_version_too_old, zero HTTP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "tool_id",
        "product",
        "tool_fn_name",
        "required_version",
        "too_old_version",
        "kwargs",
        "mutation_method_name",
    ),
    _VERSION_GATE_CASES,
    ids=[row[0] for row in _VERSION_GATE_CASES],
)
def test_version_gate_blocks_below_required_with_zero_http(
    monkeypatch: pytest.MonkeyPatch,
    fake_ctx: _FakeContext,
    disable_read_only: None,
    tool_id: str,
    product: str,
    tool_fn_name: str,
    required_version: str,
    too_old_version: str,
    kwargs: dict[str, Any],
    mutation_method_name: str,
) -> None:
    """P4: ``detected < required`` ⇒ ``dc_version_too_old`` + zero HTTP.

    The fetcher's mutation method is the direct proxy for the outbound
    HTTP call; its ``call_count == 0`` is the hard safety property.
    """
    fetcher = _build_fetcher(too_old_version)
    _patch_fetcher(monkeypatch, product, fetcher)
    tool = _resolve_tool_fn(product, tool_fn_name)

    payload = _call_tool(tool, fake_ctx, kwargs)

    # 1. Structured-envelope contract.
    assert payload["success"] is False, (
        f"{tool_id}: expected success=False, got payload={payload!r}"
    )
    assert payload["error_code"] == "dc_version_too_old", (
        f"{tool_id}: expected error_code='dc_version_too_old', "
        f"got {payload.get('error_code')!r}"
    )

    # 2. Details carry both the declared minimum and the detected value.
    details = payload.get("details", {})
    assert details.get("required_version") == required_version, (
        f"{tool_id}: expected details.required_version={required_version!r}, "
        f"got {details.get('required_version')!r}"
    )
    assert details.get("detected_version") == too_old_version, (
        f"{tool_id}: expected details.detected_version={too_old_version!r}, "
        f"got {details.get('detected_version')!r}"
    )

    # 3. Hard safety invariant: the fetcher's mutation method was not
    # invoked — zero outbound HTTP to the business endpoint.
    mutation_method = getattr(fetcher, mutation_method_name)
    assert mutation_method.call_count == 0, (
        f"{tool_id}: expected zero calls to {mutation_method_name!r} on "
        f"version-gate reject, got {mutation_method.call_count}"
    )


# ---------------------------------------------------------------------------
# Indeterminate fall-through — ``_dc_version is None`` ⇒ check returns None
# ---------------------------------------------------------------------------
#
# Requirement 45.3: when the fetcher cannot resolve a DC version,
# ``check_dc_version`` must return ``None`` (fall-through) so the tool
# body proceeds to the upstream call and can map a 404/501 to
# ``dc_version_unknown``. This is asserted directly against the guard
# function (one case per product-specific minimum) because the mapping
# to ``dc_version_unknown`` happens in the tool's error-handling layer,
# not in the guard — this property is strictly about the guard's
# fall-through contract.


class _FakeFetcherNoVersion:
    """Fetcher-shaped stand-in with no cached DC version.

    Deliberately *not* a ``MagicMock``: a MagicMock would auto-create a
    callable ``get_dc_version`` attribute whose default return value is
    another MagicMock (truthy, unparseable), which would exercise the
    unparseable branch rather than the ``None`` branch we care about
    here. A plain class with ``_dc_version = None`` and no
    ``get_dc_version`` method forces ``check_dc_version`` through the
    attribute-read path and straight into the "indeterminate" return.
    """

    _dc_version: str | None = None


@pytest.mark.parametrize(
    ("product", "required_version"),
    [
        ("bitbucket_webhooks", "5.4"),
        ("bitbucket_reactions", "8.8"),
        ("bitbucket_deployments", "7.10"),
        ("jira_archive", "9.4"),
    ],
)
def test_check_dc_version_falls_through_when_detected_is_none(
    product: str, required_version: str
) -> None:
    """Req 45.3: ``_dc_version is None`` ⇒ ``check_dc_version`` returns None.

    The caller (each DC-gated tool) maps that fall-through to the
    business call and, on upstream 404/501, to ``dc_version_unknown``;
    this test pins the guard's contribution — the guard itself must
    yield ``None`` rather than a ``dc_version_too_old`` error when the
    detected version is missing.
    """
    fetcher = _FakeFetcherNoVersion()

    result = dc_guards.check_dc_version(fetcher, required=required_version)

    assert result is None, (
        f"{product}: expected check_dc_version to fall through (None) when "
        f"_dc_version is None, got {result!r}"
    )
