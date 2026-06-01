"""Unit tests for the ``mcp_client`` package.

Validates: Requirements 1.8 (banned MCP tool list) and 1.9 (PR draft
enforcement) — task 2.5 of
``.kiro/specs/platform-mimari-foundation/tasks.md``.

The tests cover three concerns:

1. :data:`BANNED_TOOLS` exposes the canonical pair from MIMARI §1
   Kural 9 and :func:`filter_tools` strips them across the supported
   tool-shape forms (string, ``dict``, attribute object).
2. :func:`enforce_pr_draft` rewrites ``draft`` to ``True`` for every
   well-formed input shape and emits a single ``pr_draft_enforced``
   audit event whenever it had to flip the field.
3. :class:`AtlassianClient` is the single chokepoint that binds the
   two helpers; its placeholder transport raises with a useful
   pointer to the spec that delivers the HTTP wiring.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from audit_logger import AuditEvent
from mcp_client import (
    AtlassianClient,
    BANNED_TOOLS,
    PR_DRAFT_AUDIT_ACTION,
    enforce_pr_draft,
    filter_tools,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CapturingAuditWriter:
    """In-memory ``AuditWriter`` used by the PR-draft tests.

    ``AuditLogger`` accepts any ``AuditWriter`` Protocol implementation;
    the capturing writer keeps the events around for assertions.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def insert_audit(self, event: AuditEvent) -> None:
        self.events.append(event)


def _make_logger() -> tuple[Any, _CapturingAuditWriter]:
    """Return a ready-to-use ``AuditLogger`` plus the capturing writer."""

    from audit_logger import AuditLogger

    writer = _CapturingAuditWriter()
    return AuditLogger(writer=writer), writer


# ---------------------------------------------------------------------------
# tool_filter — BANNED_TOOLS membership and filter_tools behaviour
# ---------------------------------------------------------------------------


class TestBannedTools:
    """``BANNED_TOOLS`` matches MIMARI §1 Rule 9 (Requirement 1.8)."""

    def test_banned_tools_contains_canonical_pair(self) -> None:
        """The two design.md members are present and only those."""

        assert BANNED_TOOLS == frozenset(
            {"bitbucket_merge_pr", "confluence_delete_page"}
        )

    def test_banned_tools_is_immutable_frozenset(self) -> None:
        """The constant is a ``frozenset`` so callers cannot mutate it."""

        assert isinstance(BANNED_TOOLS, frozenset)
        with pytest.raises(AttributeError):
            BANNED_TOOLS.add("rogue_tool")  # type: ignore[attr-defined]


class TestFilterTools:
    """``filter_tools`` strips banned tools and is shape-tolerant."""

    def test_filter_tools_drops_banned_strings(self) -> None:
        catalog = [
            "jira_get_issue",
            "bitbucket_merge_pr",
            "confluence_delete_page",
            "bitbucket_create_branch",
        ]
        assert filter_tools(catalog) == [
            "jira_get_issue",
            "bitbucket_create_branch",
        ]

    def test_filter_tools_drops_banned_dict_entries(self) -> None:
        catalog = [
            {"name": "jira_get_issue", "description": "..."},
            {"name": "bitbucket_merge_pr", "description": "..."},
            {"name": "confluence_get_page", "description": "..."},
        ]
        names = [tool["name"] for tool in filter_tools(catalog)]
        assert names == ["jira_get_issue", "confluence_get_page"]

    def test_filter_tools_drops_banned_attribute_objects(self) -> None:
        """Tools exposed as objects with a ``.name`` attribute work too."""

        class _Tool:
            def __init__(self, name: str) -> None:
                self.name = name

            def __repr__(self) -> str:  # pragma: no cover - debug aid
                return f"_Tool({self.name!r})"

        catalog = [
            _Tool("jira_get_issue"),
            _Tool("bitbucket_merge_pr"),
            _Tool("confluence_delete_page"),
            _Tool("confluence_get_page"),
        ]
        kept_names = [tool.name for tool in filter_tools(catalog)]
        assert kept_names == ["jira_get_issue", "confluence_get_page"]

    def test_filter_tools_preserves_order(self) -> None:
        """Original ordering of the kept entries is preserved."""

        catalog = [
            "a",
            "bitbucket_merge_pr",
            "b",
            "confluence_delete_page",
            "c",
        ]
        assert filter_tools(catalog) == ["a", "b", "c"]

    def test_filter_tools_is_a_no_op_when_no_banned_tools_present(self) -> None:
        catalog = ["jira_get_issue", "bitbucket_create_branch"]
        result = filter_tools(catalog)
        assert result == catalog
        # New list is returned (defensive copy semantics).
        assert result is not catalog

    def test_filter_tools_handles_empty_catalog(self) -> None:
        assert filter_tools([]) == []

    def test_filter_tools_handles_generator_input(self) -> None:
        """Iterables that aren't lists are exhausted into a fresh list."""

        def _gen() -> Any:
            yield "jira_get_issue"
            yield "bitbucket_merge_pr"
            yield "confluence_delete_page"

        assert filter_tools(_gen()) == ["jira_get_issue"]

    def test_filter_tools_keeps_tools_with_unknown_shape(self) -> None:
        """Entries whose name we cannot inspect cannot match a banned name."""

        catalog: list[Any] = [
            42,
            None,
            "jira_get_issue",
            {"name": "bitbucket_merge_pr"},
        ]
        result = filter_tools(catalog)
        # The integer and the ``None`` survive (they cannot be banned).
        # The dict-shaped banned entry is removed.
        assert result == [42, None, "jira_get_issue"]

    def test_filter_tools_keeps_dict_without_name_field(self) -> None:
        """Defensive: dict entries with no ``name`` key are retained."""

        catalog = [{"id": "x"}, {"name": "bitbucket_merge_pr"}, "ok"]
        result = filter_tools(catalog)
        assert result == [{"id": "x"}, "ok"]


# ---------------------------------------------------------------------------
# pr_draft — enforce_pr_draft coerces draft=True and audits flips
# ---------------------------------------------------------------------------


class TestEnforcePrDraftCoercion:
    """``enforce_pr_draft`` always returns ``draft=True`` (Requirement 1.9)."""

    @pytest.mark.parametrize(
        "given",
        [
            {"title": "Fix bug", "draft": False},
            {"title": "Fix bug", "draft": True},
            {"title": "Fix bug"},  # field absent
            {"title": "Fix bug", "draft": None},
            {"title": "Fix bug", "draft": "false"},
            {"title": "Fix bug", "draft": 0},
            {"title": "Fix bug", "draft": []},
        ],
    )
    def test_enforce_pr_draft_returns_true(self, given: dict[str, Any]) -> None:
        result = asyncio.run(enforce_pr_draft(given))
        assert result["draft"] is True

    def test_enforce_pr_draft_preserves_other_fields(self) -> None:
        payload = {
            "title": "Fix bug",
            "description": "lorem",
            "source_branch": "feat/x",
            "destination_branch": "main",
            "reviewers": [{"uuid": "u1"}, {"uuid": "u2"}],
            "draft": False,
        }
        result = asyncio.run(enforce_pr_draft(payload))
        assert result["title"] == "Fix bug"
        assert result["description"] == "lorem"
        assert result["source_branch"] == "feat/x"
        assert result["destination_branch"] == "main"
        assert result["reviewers"] == [{"uuid": "u1"}, {"uuid": "u2"}]
        assert result["draft"] is True

    def test_enforce_pr_draft_returns_new_mapping(self) -> None:
        """Caller's mapping is left unchanged (defensive copy)."""

        payload = {"title": "Fix bug", "draft": False}
        result = asyncio.run(enforce_pr_draft(payload))
        assert result is not payload
        assert payload == {"title": "Fix bug", "draft": False}, (
            "input mapping must not be mutated"
        )

    def test_enforce_pr_draft_deep_copies_nested_structures(self) -> None:
        """Mutating the result must not leak back to the caller."""

        reviewers = [{"uuid": "u1"}]
        payload = {"title": "Fix", "reviewers": reviewers, "draft": False}
        result = asyncio.run(enforce_pr_draft(payload))
        result["reviewers"].append({"uuid": "u2"})
        assert reviewers == [{"uuid": "u1"}], (
            "deep copy must isolate nested structures"
        )


class TestEnforcePrDraftAuditing:
    """``enforce_pr_draft`` emits audit events only when it had to flip."""

    def test_writes_audit_event_when_draft_was_false(self) -> None:
        logger, writer = _make_logger()
        asyncio.run(
            enforce_pr_draft(
                {"title": "X", "draft": False},
                audit_logger=logger,
                actor_id="bot.payment.bitbucket",
                actor_role="system",
                dept_id="payment",
                resource="bitbucket:payment/api",
            )
        )

        assert len(writer.events) == 1
        event = writer.events[0]
        assert event.action == PR_DRAFT_AUDIT_ACTION == "pr_draft_enforced"
        assert event.actor_id == "bot.payment.bitbucket"
        assert event.actor_role == "system"
        assert event.dept_id == "payment"
        assert event.resource == "bitbucket:payment/api"
        assert event.result == "ok"
        assert event.payload == {"original_draft": False}

    def test_writes_audit_event_when_draft_was_missing(self) -> None:
        logger, writer = _make_logger()
        asyncio.run(
            enforce_pr_draft(
                {"title": "X"},  # no ``draft`` key
                audit_logger=logger,
            )
        )

        assert len(writer.events) == 1
        event = writer.events[0]
        assert event.action == "pr_draft_enforced"
        # Missing field is normalised to ``None`` in the audit payload
        # (the private sentinel never escapes to callers).
        assert event.payload == {"original_draft": None}

    def test_does_not_write_audit_event_when_draft_was_already_true(self) -> None:
        """No-op flip → no operator-facing audit event."""

        logger, writer = _make_logger()
        result = asyncio.run(
            enforce_pr_draft(
                {"title": "X", "draft": True},
                audit_logger=logger,
            )
        )

        assert result["draft"] is True
        assert writer.events == [], (
            "no audit event should be emitted when the rule did not flip"
        )

    def test_works_without_audit_logger(self) -> None:
        """Passing ``audit_logger=None`` keeps the function callable."""

        result = asyncio.run(
            enforce_pr_draft({"title": "X", "draft": False}, audit_logger=None)
        )
        assert result["draft"] is True


# ---------------------------------------------------------------------------
# atlassian_client — single chokepoint binding R1.8 + R1.9
# ---------------------------------------------------------------------------


class TestAtlassianClientSkeleton:
    """``AtlassianClient`` binds the two enforcement helpers together."""

    def test_available_tools_routes_through_filter_tools(self) -> None:
        client = AtlassianClient(client_source="test-suite")
        catalog = [
            "jira_get_issue",
            "bitbucket_merge_pr",
            "confluence_delete_page",
            "bitbucket_create_branch",
        ]
        assert client.available_tools(catalog) == [
            "jira_get_issue",
            "bitbucket_create_branch",
        ]

    def test_open_pull_request_enforces_draft_then_raises(self) -> None:
        """Skeleton: enforcement helper runs, then NotImplementedError."""

        logger, writer = _make_logger()
        client = AtlassianClient(client_source="test-suite")

        with pytest.raises(NotImplementedError, match="HTTP wiring"):
            asyncio.run(
                client.open_pull_request(
                    {"title": "X", "draft": False},
                    audit_logger=logger,
                    actor_id="bot.payment.bitbucket",
                    actor_role="system",
                    dept_id="payment",
                )
            )

        # The helper ran *before* the exception, so the audit event
        # was written even though the HTTP layer is not implemented.
        assert len(writer.events) == 1
        assert writer.events[0].action == "pr_draft_enforced"

    def test_open_pull_request_no_audit_when_already_draft(self) -> None:
        """Already-draft payload skips the audit but still raises."""

        logger, writer = _make_logger()
        client = AtlassianClient(client_source="test-suite")

        with pytest.raises(NotImplementedError):
            asyncio.run(
                client.open_pull_request(
                    {"title": "X", "draft": True},
                    audit_logger=logger,
                )
            )

        assert writer.events == []

    def test_constructor_captures_mcp_base_url(self) -> None:
        """Optional MCP base URL is stored for the future HTTP layer."""

        client = AtlassianClient(
            client_source="test-suite",
            mcp_base_url="http://atlassian-mcp:8090",
        )
        assert client._mcp_base_url == "http://atlassian-mcp:8090"

    # ------------------------------------------------------------------
    # G6 — client_source enforcement (platform-quick-fixes)
    # ------------------------------------------------------------------

    def test_constructor_requires_client_source(self) -> None:
        """Missing ``client_source`` keyword fails at import time, not at
        first network call (G6 — see
        ``platform/docs/api-contracts/mcp-credential-headers.md`` §1)."""

        with pytest.raises(TypeError):
            # ``client_source`` is keyword-only and required; the
            # call site below intentionally omits it.
            AtlassianClient()  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        "bad_source",
        [
            "",
            "   ",
            "Component",  # uppercase rejected
            "agent_runner",  # underscore rejected — kebab-case only
            "agent-runner:",  # trailing colon with empty sub-context
            ":sub-context",  # empty component
            "agent runner",  # whitespace
            "agent-runner@payment",  # @ outside sub-context slot
        ],
    )
    def test_constructor_rejects_malformed_client_source(self, bad_source: str) -> None:
        """The pattern matches lowercase kebab-case with an optional
        ``:<sub-context>`` suffix; everything else is a caller bug."""

        with pytest.raises(ValueError, match="client_source"):
            AtlassianClient(client_source=bad_source)

    @pytest.mark.parametrize(
        "good_source",
        [
            "agent-runner-worker",
            "automation-service",
            "automation-service:webhook-jira",
            "streamlit-ui:user@payment",
            "test-suite",
            "ide-proxy:dev.machine-01",
        ],
    )
    def test_constructor_accepts_documented_client_source(self, good_source: str) -> None:
        """Every shape from
        ``platform/docs/api-contracts/mcp-credential-headers.md`` §1
        examples is accepted."""

        client = AtlassianClient(client_source=good_source)
        assert client.client_source == good_source

    def test_client_source_stripped_of_surrounding_whitespace(self) -> None:
        """The stored value is normalised but the format check is strict."""

        client = AtlassianClient(client_source="  agent-runner-worker  ")
        assert client.client_source == "agent-runner-worker"
