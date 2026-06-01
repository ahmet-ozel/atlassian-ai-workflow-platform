"""Property test P8 — broadcast-capable tools return reversible receipts.

Validates Requirements 2.5, 13.3, 17.1, 17.2, 17.3, 26.4, 28.4, 34.6, 47.1,
47.2 / design Property 8:

    Every broadcast-capable or destructive Write_Tool in the DC-parity
    surface MUST, on a successful happy-path invocation, return a JSON
    response whose top-level ``receipt`` key carries a dict with exactly
    the five fields documented by :func:`mcp_atlassian.utils.dc_guards.build_receipt`:

        {object_id, inverse_tool, inverse_args, note, recipient_scope}

    Every value MUST be JSON-serializable (the receipt is round-tripped
    through ``json.dumps`` as part of the tool response), and the enclosing
    toolset MUST NOT be in ``DEFAULT_TOOLSETS`` — these tools are opt-in
    per Requirement 47.1.

The six tools covered (one per design bullet):

    * ``bitbucket_create_webhook``            → inverse ``bitbucket_delete_webhook``
    * ``bitbucket_cherry_pick_commit``        → no inverse (history rewrite); note
                                                explains the non-retractable nature
    * ``jira_notify_issue``                   → no inverse (email sends); note
                                                says ``"Email sends are not retractable"``
    * ``jira_archive_issue``                  → inverse ``jira_restore_issue``
    * ``confluence_set_content_restrictions`` → receipt carries prior-state snapshot
                                                as ``inverse_args`` so the agent
                                                can restore the exact prior state
    * ``confluence_archive_page``             → inverse ``confluence_restore_archived_page``

Test shape
----------
For each tool:

1. A ``MagicMock`` fetcher is built with ``config.projects_filter=None``
   (so the filter precheck is a no-op) and a ``_dc_version`` / ``get_dc_version``
   pair set to ``"99.99.99"`` so any DC-version gate (e.g. 5.4 for webhooks
   or 9.4 for Jira archive) passes through.
2. The relevant fetcher method is stubbed to return a plausible happy-path
   payload — webhook with id, cherry-pick with commit hash, notify with
   ``recipient_count``, archive confirmation dict, restrictions with a
   populated ``prior_state`` snapshot, etc.
3. ``get_{product}_fetcher`` is monkeypatched on the relevant server
   module, ``READ_ONLY_MODE`` is removed from the environment, and the
   tool's ``.fn`` is invoked via ``asyncio.run``.
4. The JSON response is parsed and every P8 invariant is asserted.

Style reference: :mod:`tests.unit.properties.test_read_only_property`
(curated tool registry + ``importlib``/``monkeypatch`` wiring) and
:mod:`tests.unit.bitbucket.test_cherry_pick` (per-tool receipt shape
assertions).
"""

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from mcp_atlassian.utils.toolsets import DEFAULT_TOOLSETS, get_toolset_tag


# ---------------------------------------------------------------------------
# Per-tool fixture bundle.
#
# Each entry in ``_RECEIPT_TOOLS`` is a dict that fully parameterises one
# property-test case for one broadcast-capable / destructive tool:
#
#   attr              — undecorated attribute name on the server module
#                       (``.fn`` is the async implementation).
#   module_path       — dotted import path of the server module.
#   dep               — name of the ``get_{product}_fetcher`` symbol to
#                       monkeypatch on the server module.
#   method            — name of the fetcher method the tool calls. Its
#                       ``return_value`` is set to ``method_return`` so the
#                       happy-path body can splat the receipt cleanly.
#   method_return     — dict returned by the stubbed fetcher method.
#   tool_kwargs       — keyword arguments passed to the tool's ``.fn``.
#   expected_object_id — the receipt's ``object_id`` field on the happy path.
#   expected_inverse_tool — the receipt's ``inverse_tool`` field (or ``None``
#                       when the effect is not retractable).
#   expected_toolset  — the toolset name the tool belongs to (the
#                       ``toolset:<name>`` tag minus the prefix). Must be
#                       absent from ``DEFAULT_TOOLSETS`` (Req 47.1).
#   validate_note     — callable (note -> bool) that verifies the textual
#                       note matches the expectation for this tool. Allows
#                       tool-specific wording without coupling the test to
#                       exact strings that may be tweaked in the future.
#   validate_inverse_args — callable (inverse_args -> bool) that verifies
#                       the receipt's ``inverse_args`` has the right shape
#                       for the tool (either carries the reversing tool's
#                       required kwargs or is ``None`` for non-retractable
#                       effects).
#   extra_checks      — optional callable (payload -> None) for tool-specific
#                       assertions that do not fit the universal receipt
#                       contract (e.g. prior-state snapshot presence).
# ---------------------------------------------------------------------------


def _webhook_method_return() -> dict[str, Any]:
    return {
        "id": 1,
        "name": "hook-a",
        "url": "https://example.com/hook",
        "events": ["repo:refs_changed"],
        "active": True,
        "configuration": {},
    }


def _cherry_pick_method_return() -> dict[str, Any]:
    return {
        "id": "newsha1",
        "displayId": "newsha1"[:7],
        "message": "Apply fix",
    }


def _notify_method_return() -> dict[str, Any]:
    return {"recipient_count": 3}


def _archive_issue_method_return() -> dict[str, Any]:
    return {"archived": True, "issue_key": "PROJ-1"}


def _restrictions_method_return() -> dict[str, Any]:
    # Representative prior-state snapshot as returned by
    # ``RestrictionsMixin.list_content_restrictions``: Confluence always
    # emits the ``read`` and ``update`` operation entries even when the
    # page is unrestricted, so the test fixture mirrors that shape and
    # populates a read-restriction principal so the tool's inverse-args
    # encoding exercises the non-empty branch.
    prior_state = {
        "results": [
            {
                "operation": "read",
                "restrictions": {
                    "user": {
                        "results": [
                            {"type": "known", "username": "alice"}
                        ]
                    },
                    "group": {"results": []},
                },
            },
            {
                "operation": "update",
                "restrictions": {
                    "user": {"results": []},
                    "group": {"results": []},
                },
            },
        ]
    }
    new_state = {
        "results": [
            {
                "operation": "read",
                "restrictions": {
                    "user": {"results": []},
                    "group": {
                        "results": [{"type": "group", "name": "devs"}]
                    },
                },
            },
            {
                "operation": "update",
                "restrictions": {
                    "user": {"results": []},
                    "group": {"results": []},
                },
            },
        ]
    }
    return {"prior_state": prior_state, "new_state": new_state}


def _archive_page_method_return() -> dict[str, Any]:
    return {
        "archived": True,
        "page_id": "123456",
        "response": {"id": "archive-task-1", "links": {}},
    }


_RECEIPT_TOOLS: list[dict[str, Any]] = [
    # ------------------------------------------------------------------
    # bitbucket_create_webhook — inverse: bitbucket_delete_webhook
    # ------------------------------------------------------------------
    {
        "attr": "create_webhook",
        "module_path": "mcp_atlassian.servers.bitbucket",
        "dep": "get_bitbucket_fetcher",
        "method": "create_webhook",
        "method_return": _webhook_method_return(),
        "tool_kwargs": {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "name": "hook-a",
            "url": "https://example.com/hook",
            "events": '["repo:refs_changed"]',
        },
        "expected_object_id": "1",
        "expected_inverse_tool": "bitbucket_delete_webhook",
        "expected_toolset": "bitbucket_webhooks",
        "validate_note": lambda note: note is None,
        "validate_inverse_args": lambda args: (
            isinstance(args, dict)
            and args.get("project_key") == "PROJ"
            and args.get("repo_slug") == "repo"
            and args.get("webhook_id") == 1
        ),
        # recipient_scope must summarise the broadcast target (url +
        # events) without leaking any HMAC secret back to the agent.
        "extra_checks": lambda payload: (
            # Tool echoes the redacted webhook object itself
            payload.get("webhook") is not None
            and payload["receipt"]["recipient_scope"]
            == {"url": "https://example.com/hook", "events": ["repo:refs_changed"]}
        ),
    },
    # ------------------------------------------------------------------
    # bitbucket_cherry_pick_commit — no inverse (history rewrite)
    # ------------------------------------------------------------------
    {
        "attr": "cherry_pick_commit",
        "module_path": "mcp_atlassian.servers.bitbucket",
        "dep": "get_bitbucket_fetcher",
        "method": "cherry_pick_commit",
        "method_return": _cherry_pick_method_return(),
        "tool_kwargs": {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "source_commit": "abc123",
            "target_branch": "main",
        },
        # Req 13.3: the receipt surfaces the resulting commit hash on the
        # target branch as its ``object_id``.
        "expected_object_id": "newsha1",
        "expected_inverse_tool": None,
        "expected_toolset": "bitbucket_commits",
        # Cherry-pick cannot be reversed with a single tool call; the
        # note must be non-empty and explain the non-retractable nature.
        "validate_note": lambda note: (
            isinstance(note, str) and "retractable" in note.lower()
        ),
        "validate_inverse_args": lambda args: args is None,
        "extra_checks": lambda payload: (
            payload["receipt"]["recipient_scope"]
            == {"source_commit": "abc123", "target_branch": "main"}
        ),
    },
    # ------------------------------------------------------------------
    # jira_notify_issue — no inverse (email sends are not retractable)
    # ------------------------------------------------------------------
    {
        "attr": "jira_notify_issue",
        "module_path": "mcp_atlassian.servers.jira",
        "dep": "get_jira_fetcher",
        "method": "notify_issue",
        "method_return": _notify_method_return(),
        "tool_kwargs": {
            "issue_key": "PROJ-1",
            "subject": "Status update",
            "text_body": "The issue has moved to In Review.",
            "to_watchers": True,
        },
        "expected_object_id": "PROJ-1",
        "expected_inverse_tool": None,
        "expected_toolset": "jira_notifications",
        # Req 17.3: the note explicitly identifies the non-retractable
        # nature of the email send.
        "validate_note": lambda note: (
            isinstance(note, str) and "retractable" in note.lower()
        ),
        "validate_inverse_args": lambda args: args is None,
        # Req 17.3: recipient_scope must carry at least the recipient
        # count; the tool also emits the input descriptor fields so the
        # agent has a full audit trail of who was targeted.
        "extra_checks": lambda payload: (
            isinstance(payload["receipt"]["recipient_scope"], dict)
            and payload["receipt"]["recipient_scope"].get("recipient_count") == 3
            and payload["receipt"]["recipient_scope"].get("to_watchers") is True
        ),
    },
    # ------------------------------------------------------------------
    # jira_archive_issue — inverse: jira_restore_issue
    # ------------------------------------------------------------------
    {
        "attr": "jira_archive_issue",
        "module_path": "mcp_atlassian.servers.jira",
        "dep": "get_jira_fetcher",
        "method": "archive_issue",
        "method_return": _archive_issue_method_return(),
        "tool_kwargs": {"issue_key": "PROJ-1"},
        "expected_object_id": "PROJ-1",
        "expected_inverse_tool": "jira_restore_issue",
        "expected_toolset": "jira_archive",
        "validate_note": lambda note: note is None,
        "validate_inverse_args": lambda args: (
            isinstance(args, dict) and args == {"issue_key": "PROJ-1"}
        ),
        "extra_checks": lambda payload: payload.get("archived") is True,
    },
    # ------------------------------------------------------------------
    # confluence_set_content_restrictions — receipt carries prior-state snapshot
    # ------------------------------------------------------------------
    {
        "attr": "set_content_restrictions",
        "module_path": "mcp_atlassian.servers.confluence",
        "dep": "get_confluence_fetcher",
        "method": "set_content_restrictions",
        "method_return": _restrictions_method_return(),
        "tool_kwargs": {
            "page_id": "123456",
            "read_groups": ["devs"],
        },
        "expected_object_id": "123456",
        # Req 28.4: inverse is the same tool, re-invoked with the prior
        # principals (or ``confluence_clear_content_restrictions`` when
        # the prior state was empty — the fixture populates a read user
        # so the non-empty branch is exercised here).
        "expected_inverse_tool": "confluence_set_content_restrictions",
        "expected_toolset": "confluence_restrictions",
        # Prior-state snapshot note clarifies the receipt's purpose.
        "validate_note": lambda note: (
            isinstance(note, str) and "prior" in note.lower()
        ),
        "validate_inverse_args": lambda args: (
            isinstance(args, dict)
            and args.get("page_id") == "123456"
            # The fixture's prior_state populates a read user; the tool
            # must pass it through in inverse_args so a caller can
            # restore the exact previous restrictions.
            and args.get("read_users") == ["alice"]
            and args.get("read_groups") == []
            and args.get("update_users") == []
            and args.get("update_groups") == []
        ),
        # Req 28.4: the response body exposes the prior-state snapshot
        # separately so an operator or agent can inspect it without
        # re-parsing the receipt.
        "extra_checks": lambda payload: (
            isinstance(payload.get("prior_state"), dict)
            and payload.get("prior_state")  # non-empty
        ),
    },
    # ------------------------------------------------------------------
    # confluence_archive_page — inverse: confluence_restore_archived_page
    # ------------------------------------------------------------------
    {
        "attr": "archive_page",
        "module_path": "mcp_atlassian.servers.confluence",
        "dep": "get_confluence_fetcher",
        "method": "archive_page",
        "method_return": _archive_page_method_return(),
        "tool_kwargs": {"page_id": "123456"},
        "expected_object_id": "123456",
        "expected_inverse_tool": "confluence_restore_archived_page",
        "expected_toolset": "confluence_archive",
        "validate_note": lambda note: note is None,
        "validate_inverse_args": lambda args: (
            isinstance(args, dict) and args == {"page_id": "123456"}
        ),
        "extra_checks": lambda payload: payload.get("archived") is True,
    },
]


# Readable parametrisation ids: prefix bitbucket/confluence tools with
# the product so the test output reads like the registered MCP tool name
# even though the source-level attribute is bare. Jira tools already
# carry the ``jira_`` prefix at the source level.
def _param_id(entry: dict[str, Any]) -> str:
    attr: str = entry["attr"]
    product = entry["module_path"].rsplit(".", 1)[-1]
    if attr.startswith(f"{product}_"):
        return attr
    return f"{product}_{attr}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_fake_ctx() -> SimpleNamespace:
    """Minimal ``fastmcp.Context`` stand-in.

    ``check_write_access`` (layered above some tools) reads
    ``ctx.request_context.lifespan_context`` and calls ``.get`` on it;
    an empty dict short-circuits that decorator transparently so the
    inner read-only guard (disabled via ``monkeypatch.delenv`` in each
    test) stays a no-op on the happy path.
    """
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={}),
    )


def _make_fetcher_mock(method_name: str, method_return: Any) -> MagicMock:
    """Build a ``MagicMock`` fetcher suitable for the DC-guard prelude.

    The returned mock:
      * Has ``config.projects_filter=None`` / ``config.spaces_filter=None``
        so the filter precheck allows every key through.
      * Reports a DC version well above every tool's minimum
        (webhooks 5.4, cherry-pick N/A, notify N/A, Jira archive 9.4)
        so ``check_dc_version`` never rejects the call.
      * Has exactly one mixin method stubbed out — the one the tool
        actually invokes — returning the fixture payload.
    """
    fetcher = MagicMock(name="dc-fetcher")
    fetcher.is_cloud = False
    fetcher.config = SimpleNamespace(
        is_cloud=False,
        projects_filter=None,
        spaces_filter=None,
        username="tester",
    )
    # Both paths into the DC-version guard: a callable accessor
    # (preferred) and the cached attribute fallback.
    fetcher.get_dc_version.return_value = "99.99.99"
    fetcher._dc_version = "99.99.99"
    getattr(fetcher, method_name).return_value = method_return
    return fetcher


# ---------------------------------------------------------------------------
# Property P8 — receipt shape + opt-in toolset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    _RECEIPT_TOOLS,
    ids=[_param_id(e) for e in _RECEIPT_TOOLS],
)
def test_broadcast_tool_returns_reversible_receipt(
    entry: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P8: broadcast-capable tools return a well-shaped reversible receipt.

    For each of the six curated tools, invoking ``.fn`` with a
    happy-path fetcher stub must:

      1. Produce a JSON envelope with ``success=True`` and a ``receipt``
         key whose dict has exactly the five documented fields.
      2. The ``inverse_tool`` / ``inverse_args`` pair must match the
         tool's documented reversibility contract — a named inverse
         tool for retractable writes (webhook create, Jira archive,
         Confluence restrictions + archive) or ``None`` plus an
         explanatory note for non-retractable writes (cherry-pick,
         email notify).
      3. The full receipt dict must round-trip through ``json.dumps``
         without raising — every value MUST be JSON-serializable
         because the tool serialises the whole response and emits it
         as a string (Req 47.2).
      4. The enclosing toolset must NOT be in ``DEFAULT_TOOLSETS`` —
         these tools are broadcast-capable or destructive and are
         opt-in per Requirement 47.1.
    """
    attr: str = entry["attr"]
    module_path: str = entry["module_path"]
    dep_name: str = entry["dep"]
    method: str = entry["method"]
    method_return: Any = entry["method_return"]
    tool_kwargs: dict[str, Any] = entry["tool_kwargs"]

    # 0. Happy-path prelude: ensure the read-only guard is a no-op.
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)

    server_module = importlib.import_module(module_path)
    tool = getattr(server_module, attr)

    # 1. Build + install the fetcher stub.
    fetcher = _make_fetcher_mock(method, method_return)

    async def _aget(_ctx: Any) -> MagicMock:
        return fetcher

    monkeypatch.setattr(server_module, dep_name, _aget)

    # 2. Invoke the tool.
    fake_ctx = _make_fake_ctx()
    result_json = asyncio.run(tool.fn(fake_ctx, **tool_kwargs))
    payload = json.loads(result_json)

    # 3. Happy-path success envelope.
    assert payload.get("success") is True, (
        f"{attr}: expected success=True, got payload={payload!r}"
    )
    assert "receipt" in payload, (
        f"{attr}: expected top-level 'receipt' key, got keys={sorted(payload)!r}"
    )

    receipt = payload["receipt"]
    assert isinstance(receipt, dict), (
        f"{attr}: receipt must be a dict, got {type(receipt).__name__}"
    )

    # 4. Receipt shape — exactly the five documented keys, in any order.
    expected_keys = {
        "object_id",
        "inverse_tool",
        "inverse_args",
        "note",
        "recipient_scope",
    }
    assert set(receipt.keys()) == expected_keys, (
        f"{attr}: receipt keys {sorted(receipt)!r} do not match "
        f"expected {sorted(expected_keys)!r}"
    )

    # 5. Per-tool ``object_id`` invariant (webhook id / new-commit sha /
    # issue key / page id, all stringified).
    assert receipt["object_id"] == entry["expected_object_id"], (
        f"{attr}: receipt.object_id={receipt['object_id']!r} does not "
        f"match expected {entry['expected_object_id']!r}"
    )

    # 6. Inverse-tool invariant (named inverse or None depending on tool).
    assert receipt["inverse_tool"] == entry["expected_inverse_tool"], (
        f"{attr}: receipt.inverse_tool={receipt['inverse_tool']!r} does "
        f"not match expected {entry['expected_inverse_tool']!r}"
    )

    # 7. Inverse-args invariant — delegated to the per-tool predicate so
    # each entry pins its own shape without over-coupling the suite.
    assert entry["validate_inverse_args"](receipt["inverse_args"]), (
        f"{attr}: receipt.inverse_args={receipt['inverse_args']!r} does "
        f"not satisfy this tool's inverse-args contract"
    )

    # 8. Note invariant — delegated to the per-tool predicate.
    assert entry["validate_note"](receipt["note"]), (
        f"{attr}: receipt.note={receipt['note']!r} does not satisfy "
        f"this tool's note contract"
    )

    # 9. JSON round-trip — every receipt value MUST be JSON-serializable
    # because the tool splats it into its JSON response. A dedicated
    # round-trip guards against regressions that slip a non-serialisable
    # value (datetime, MagicMock, set, bytes) into any receipt field.
    reserialised = json.dumps(receipt)
    assert json.loads(reserialised) == receipt, (
        f"{attr}: receipt did not round-trip through json.dumps: {receipt!r}"
    )

    # 10. Per-tool extra invariants (webhook redaction shape, notify
    # recipient_scope counts, restrictions prior-state snapshot, etc.).
    extra = entry.get("extra_checks")
    if extra is not None:
        assert extra(payload), (
            f"{attr}: per-tool extra check failed; payload={payload!r}"
        )

    # 11. Opt-in toolset invariant (Req 47.1). Extract the toolset name
    # from the tool's own tag set via the same helper the server uses
    # at filter time, so the test reads the authoritative value rather
    # than duplicating the mapping.
    tool_tags = set(getattr(tool, "tags", set()))
    toolset = get_toolset_tag(tool_tags)
    assert toolset == entry["expected_toolset"], (
        f"{attr}: toolset tag {toolset!r} does not match expected "
        f"{entry['expected_toolset']!r}; tags={tool_tags!r}"
    )
    assert toolset not in DEFAULT_TOOLSETS, (
        f"{attr}: toolset {toolset!r} is in DEFAULT_TOOLSETS; "
        f"broadcast-capable tools must be opt-in (Req 47.1). "
        f"DEFAULT_TOOLSETS={sorted(DEFAULT_TOOLSETS)!r}"
    )


# ---------------------------------------------------------------------------
# Meta-assertion — every curated entry actually resolves to a registered tool
# with the ``write`` tag. Keeps the suite honest: typos in ``_RECEIPT_TOOLS``
# would otherwise mask a missing tool behind a silent AttributeError.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    _RECEIPT_TOOLS,
    ids=[_param_id(e) for e in _RECEIPT_TOOLS],
)
def test_curated_tool_is_registered_with_write_tag(
    entry: dict[str, Any],
) -> None:
    """Each curated tool must exist on its server module and be write-tagged."""
    attr = entry["attr"]
    module_path = entry["module_path"]
    server_module = importlib.import_module(module_path)
    tool = getattr(server_module, attr)
    tags = set(getattr(tool, "tags", set()))
    assert "write" in tags, (
        f"{attr}: expected 'write' in tool.tags, got {tags!r}"
    )
