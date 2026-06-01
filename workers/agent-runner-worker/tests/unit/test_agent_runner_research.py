"""Unit tests for ``AgentRunnerWorkflow`` ``research_*`` flows (task 9.3).

Covers the four behaviour requirements pinned by task 9.3 / R9.1-R9.6:

    1. ``research_publish_confluence`` happy path — verifies the
       activity sequence (``set_assignee_to_bot`` → ``firecrawl_search``
       → ``firecrawl_scrape`` → ``confluence_create_page`` →
       ``jira_add_comment`` carrying the page link) and that the
       rendered body picks up the
       :func:`format_research_publish_confluence_body` Kaynaklar
       block (R9.4).
    2. ``research_publish_confluence`` 403 graceful path — a blocked
       URL produces the canonical
       ``🤖 {url} domain'i araştırma için izinli değil`` Jira comment,
       a ``research_minio_offload`` / ``firecrawl_blocked:{url}``
       partial-failure marker, and the workflow continues with the
       remaining URLs without raising (R9.3).
    3. ``research_summary_jira`` short-content path — when the
       summary fits within ``max_words`` *and* the source list fits
       within ``max_sources`` the comment carries no MinIO link and
       the offload activity is NOT invoked (R9.5).
    4. ``research_summary_jira`` long-content path — when the
       summary overflows the comment carries the MinIO URI returned
       by ``minio_put_research_summary`` and the comment text embeds
       it via the ``🔗 Tam içerik:`` line (R9.5).

The tests drive the body methods directly (i.e.
``_handle_research_publish_confluence`` /
``_handle_research_summary_jira``) without spinning up a Temporal
worker — mirroring the approach already used by
``test_agent_runner_confluence.py`` (task 8.4).

Validates Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from temporalio import workflow as _temporal_workflow


# ---------------------------------------------------------------------------
# sys.path bootstrap — mirrors ``test_agent_runner_confluence.py``.
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
_PLATFORM_ROOT: Path = _WORKER_ROOT.parents[1]
_TEMPORAL_SHARED_SRC: Path = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)
_MCP_CLIENT_SRC: Path = _PLATFORM_ROOT / "libs" / "mcp_client" / "src"

for _candidate in (_SRC_DIR, _TEMPORAL_SHARED_SRC, _MCP_CLIENT_SRC):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)

# noqa: E402 below — imports after sys.path bootstrap.

from agent_runner.workflows.agent_runner_workflow import (  # noqa: E402
    AgentRunnerWorkflow,
)
from temporal_shared.messages import (  # noqa: E402
    AgentRunnerWorkflowInput,
    LlmAnalysisResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_FIXED_NOW: datetime = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
_BLOCKED_URL: str = "https://blocked.example.com/article"
_ALLOWED_URL_A: str = "https://docs.example.com/a"
_ALLOWED_URL_B: str = "https://docs.example.com/b"
_BLOCKED_DOMAIN_NEEDLE: str = "domain'i araştırma için izinli değil"


@pytest.fixture
def fixed_now() -> datetime:
    return _FIXED_NOW


@pytest.fixture
def patched_workflow_now(fixed_now: datetime, monkeypatch: pytest.MonkeyPatch):
    """Replace ``workflow.now`` with a deterministic clock."""

    state = {"now": fixed_now}
    monkeypatch.setattr(_temporal_workflow, "now", lambda: state["now"])
    return state


def _make_input(
    *,
    workflow_type: str,
    title: str = "KVKK Yönetmelik Analizi",
    rationale: str = "Veri gizliliği rejimini karşılaştır.",
    target_space: str | None = "DOCS",
) -> AgentRunnerWorkflowInput:
    """Build a minimal :class:`AgentRunnerWorkflowInput` fixture."""

    analysis = LlmAnalysisResult(
        workflow_type=workflow_type,
        confidence="high",
        target_space=target_space,
        title=title,
        rationale=rationale,
        token_usage=120,
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id="automation-jira-RES-77",
        issue_key="RES-77",
        department_id="security",
        workflow_type=workflow_type,
        analysis=analysis,
        target_repo=None,
        target_branch=None,
        iteration=1,
        max_iter=5,
        default_language="tr",
    )


@pytest.fixture
def make_wf():
    """Factory returning a fresh :class:`AgentRunnerWorkflow`."""

    from dataclasses import replace

    def _build() -> AgentRunnerWorkflow:
        wf = AgentRunnerWorkflow()
        wf._iteration_state = replace(wf._iteration_state, iter_count=1)
        return wf

    return _build


def _activity_dispatcher(routes: dict[str, Any]) -> AsyncMock:
    """Return an ``AsyncMock`` resolving ``execute_activity`` calls.

    *routes* maps activity-name → return value (or 0-arg / 1-arg
    callable that yields it). Activities not present in *routes*
    return ``None`` so optional best-effort calls (audit_emit,
    jira_add_comment) never trip the test fixtures.
    """

    async def _fake_execute_activity(*args, **kwargs):
        name = args[0] if args else kwargs.get("activity")
        if name in routes:
            value = routes[name]
            if callable(value):
                # Allow stateful routes: pass the activity-args list
                # so the route can vary by URL / payload.
                a_list = (
                    list(kwargs.get("args") or [])
                    if "args" in kwargs
                    else list(args[1] if len(args) >= 2 else [])
                )
                try:
                    return value(a_list)
                except TypeError:
                    return value()
            return value
        return None

    return AsyncMock(side_effect=_fake_execute_activity)


def _drive(coro_factory) -> None:
    """Run an async coroutine to completion under a fresh event loop."""

    asyncio.run(coro_factory())


def _activity_args(call) -> list[Any]:
    """Pull the ``args`` keyword from a recorded ``execute_activity`` call."""

    if "args" in call.kwargs:
        return list(call.kwargs["args"])
    if len(call.args) >= 2:
        return list(call.args[1])
    return []


def _activity_calls_for(activity_mock: AsyncMock, name: str) -> list[Any]:
    """Filter the recorded calls for a specific activity name."""

    return [c for c in activity_mock.call_args_list if c.args[0] == name]


def _patch_runtime(
    activity_mock: AsyncMock,
    workflow_id: str = "automation-jira-RES-77",
):
    """Yield a context manager patching the temporal primitives."""

    info_stub = type("WfInfo", (), {"workflow_id": workflow_id})()
    return [
        patch.object(_temporal_workflow, "execute_activity", activity_mock),
        patch.object(_temporal_workflow, "info", lambda: info_stub),
        patch.object(
            _temporal_workflow,
            "execute_child_workflow",
            AsyncMock(),
        ),
    ]


# ---------------------------------------------------------------------------
# 1. ``research_publish_confluence`` — happy path
# ---------------------------------------------------------------------------


class TestResearchPublishConfluenceHappyPath:
    """Two allowed URLs → Confluence page + completion comment (R9.4)."""

    def test_happy_path_invokes_full_activity_sequence(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(workflow_type="research_publish_confluence")

        # Stateful scrape route — yields a different payload per URL
        # so the formatter has two distinct sources to render.
        scrape_payloads = {
            _ALLOWED_URL_A: {
                "kind": "success",
                "url": _ALLOWED_URL_A,
                "body": {
                    "title": "KVKK Madde 5 Özeti",
                    "content": "Madde 5 kişisel verilerin işlenme şartlarını düzenler.",
                },
            },
            _ALLOWED_URL_B: {
                "kind": "success",
                "url": _ALLOWED_URL_B,
                "body": {
                    "title": "KVKK Madde 6 Özeti",
                    "content": "Madde 6 özel nitelikli verilerin işlenme şartlarını düzenler.",
                },
            },
        }

        def _scrape_route(args_list: list[Any]) -> Any:
            url = args_list[0] if args_list else ""
            return scrape_payloads.get(
                str(url),
                {"kind": "success", "url": str(url), "body": {"content": ""}},
            )

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "jira_build_issue_link": "https://acme.atlassian.net/browse/RES-77",
            "firecrawl_search": [
                {"url": _ALLOWED_URL_A, "title": "KVKK Madde 5 Özeti"},
                {"url": _ALLOWED_URL_B, "title": "KVKK Madde 6 Özeti"},
            ],
            "firecrawl_scrape": _scrape_route,
            "confluence_create_page": {
                "id": "98765",
                "url": "https://acme.atlassian.net/wiki/spaces/DOCS/pages/98765",
            },
            "jira_add_comment": None,
            "audit_emit": None,
        }
        activity_mock = _activity_dispatcher(activity_routes)

        async def _run() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo",
                    (),
                    {"workflow_id": "automation-jira-RES-77"},
                )(),
            ):
                await wf._handle_research_publish_confluence(inp)

        _drive(_run)

        called_names = [c.args[0] for c in activity_mock.call_args_list]
        for required in (
            "set_assignee_to_bot",
            "firecrawl_search",
            "firecrawl_scrape",
            "confluence_create_page",
            "jira_add_comment",
        ):
            assert required in called_names, (
                f"expected {required!r} in activity sequence, got "
                f"{called_names!r}"
            )

        # Sequence invariants — assignee first, search before scrape,
        # scrape before create, create before completion comment.
        idx_assignee = called_names.index("set_assignee_to_bot")
        idx_search = called_names.index("firecrawl_search")
        idx_scrape_first = called_names.index("firecrawl_scrape")
        idx_create = called_names.index("confluence_create_page")
        idx_comment = called_names.index("jira_add_comment")
        assert idx_assignee < idx_search < idx_scrape_first
        assert idx_scrape_first < idx_create < idx_comment

        # Both URLs were scraped exactly once.
        scrape_calls = _activity_calls_for(activity_mock, "firecrawl_scrape")
        scraped_urls = [_activity_args(c)[0] for c in scrape_calls]
        assert sorted(scraped_urls) == sorted([_ALLOWED_URL_A, _ALLOWED_URL_B])

        # The Confluence body carries the formatter-rendered Kaynaklar
        # block with both URLs.
        create_calls = _activity_calls_for(
            activity_mock, "confluence_create_page"
        )
        assert len(create_calls) == 1
        # Signature: (space, title, body, dept_id).
        space, title, body, _dept = _activity_args(create_calls[0])
        assert space == "DOCS"
        assert "KVKK" in title
        assert "## Kaynaklar" in body
        assert _ALLOWED_URL_A in body
        assert _ALLOWED_URL_B in body

        # The Jira completion comment carries the new page URL.
        comment_calls = _activity_calls_for(activity_mock, "jira_add_comment")
        assert len(comment_calls) == 1
        comment_body = _activity_args(comment_calls[0])[1]
        assert (
            "98765" in comment_body
            or "https://acme.atlassian.net/wiki/spaces/DOCS/pages/98765"
            in comment_body
        )

        # The page id is mirrored onto workflow state for the terminal
        # output.
        assert wf._latest_confluence_page_id == "98765"


# ---------------------------------------------------------------------------
# 2. ``research_publish_confluence`` — 403 graceful degradation
# ---------------------------------------------------------------------------


class TestResearchPublishConfluenceBlockedDomain:
    """A blocked URL emits the canonical Jira comment + continues (R9.3)."""

    def test_blocked_url_yields_jira_comment_no_fail(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(workflow_type="research_publish_confluence")

        # Search returns one allowed + one blocked URL. The blocked
        # URL must NOT raise; the workflow should continue with the
        # allowed URL and finish the create step normally.
        scrape_payloads = {
            _ALLOWED_URL_A: {
                "kind": "success",
                "url": _ALLOWED_URL_A,
                "body": {
                    "title": "Allowed Doc",
                    "content": "Allowed body content here.",
                },
            },
            _BLOCKED_URL: {
                "kind": "egress_blocked",
                "url": _BLOCKED_URL,
                "host": "blocked.example.com",
                "reason": "not_in_allowlist",
                "dept_id": "security",
            },
        }

        def _scrape_route(args_list: list[Any]) -> Any:
            url = args_list[0] if args_list else ""
            return scrape_payloads.get(
                str(url),
                {"kind": "success", "url": str(url), "body": {"content": ""}},
            )

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "jira_build_issue_link": "https://acme.atlassian.net/browse/RES-77",
            "firecrawl_search": [
                {"url": _ALLOWED_URL_A},
                {"url": _BLOCKED_URL},
            ],
            "firecrawl_scrape": _scrape_route,
            "confluence_create_page": {
                "id": "11",
                "url": "https://x/11",
            },
            "jira_add_comment": None,
            "audit_emit": None,
        }
        activity_mock = _activity_dispatcher(activity_routes)

        async def _run() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type("WfInfo", (), {"workflow_id": "x"})(),
            ):
                # Critical: the body MUST NOT raise even when one of
                # the URLs returns an EgressBlocked outcome.
                await wf._handle_research_publish_confluence(inp)

        _drive(_run)

        # The blocked-URL Jira comment is present alongside the
        # standard completion comment — at least one comment quotes
        # the blocked URL with the canonical Turkish refusal text.
        comment_calls = _activity_calls_for(activity_mock, "jira_add_comment")
        comment_bodies = [_activity_args(c)[1] for c in comment_calls]
        blocked_messages = [
            body
            for body in comment_bodies
            if _BLOCKED_URL in body and _BLOCKED_DOMAIN_NEEDLE in body
        ]
        assert blocked_messages, (
            "expected at least one Jira comment naming the blocked URL "
            f"with the canonical refusal text; got: {comment_bodies!r}"
        )

        # The workflow still ran ``confluence_create_page`` for the
        # allowed URL — partial degradation, not full failure.
        create_calls = _activity_calls_for(
            activity_mock, "confluence_create_page"
        )
        assert len(create_calls) == 1, (
            "blocked URL must not prevent the create step from running"
        )
        body = _activity_args(create_calls[0])[2]
        assert _ALLOWED_URL_A in body
        assert _BLOCKED_URL not in body  # blocked URL excluded from body.

        # A partial-failure marker was recorded for the blocked URL.
        assert any(
            entry.startswith("firecrawl_blocked:")
            and _BLOCKED_URL in entry
            for entry in wf._output_actions_partial
        ), wf._output_actions_partial


# ---------------------------------------------------------------------------
# 3. ``research_summary_jira`` — short content (no MinIO link)
# ---------------------------------------------------------------------------


class TestResearchSummaryJiraShortContent:
    """Short summary + few sources → comment carries no MinIO link."""

    def test_short_content_does_not_call_minio_offload(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(workflow_type="research_summary_jira")

        # A small payload that comfortably fits within max_words=500
        # and max_sources=5.
        scrape_payload = {
            "kind": "success",
            "url": _ALLOWED_URL_A,
            "body": {
                "title": "Short Doc",
                "content": "Kısa bir araştırma özeti.",
            },
        }

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "firecrawl_search": [{"url": _ALLOWED_URL_A}],
            "firecrawl_scrape": scrape_payload,
            "minio_put_research_summary": "minio://should-not-be-called",
            "jira_add_comment": None,
            "audit_emit": None,
        }
        activity_mock = _activity_dispatcher(activity_routes)

        async def _run() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type("WfInfo", (), {"workflow_id": "x"})(),
            ):
                await wf._handle_research_summary_jira(inp)

        _drive(_run)

        called_names = [c.args[0] for c in activity_mock.call_args_list]

        # Short-content path: NO MinIO offload.
        assert "minio_put_research_summary" not in called_names, (
            "short content must not trigger the MinIO offload activity"
        )

        # The Jira comment fires and carries the summary text + the
        # Kaynaklar block but no MinIO URI.
        comment_calls = _activity_calls_for(activity_mock, "jira_add_comment")
        assert len(comment_calls) == 1
        body = _activity_args(comment_calls[0])[1]
        assert "Araştırma özeti" in body
        assert _ALLOWED_URL_A in body
        # No MinIO URI appended — the formatter returned ``None`` so
        # the workflow kept the comment short-form.
        assert "minio://" not in body
        assert "Tam içerik:" not in body


# ---------------------------------------------------------------------------
# 4. ``research_summary_jira`` — long content (MinIO link path)
# ---------------------------------------------------------------------------


class TestResearchSummaryJiraLongContent:
    """Long summary → comment carries the MinIO link from the offload."""

    def test_long_content_appends_minio_uri_to_comment(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(workflow_type="research_summary_jira")

        # Build a content body that exceeds max_words=500 so the
        # formatter signals overflow via a non-None minio_uri.
        long_body = " ".join(["kelime"] * 600)

        scrape_payload = {
            "kind": "success",
            "url": _ALLOWED_URL_A,
            "body": {
                "title": "Uzun Doküman",
                "content": long_body,
            },
        }

        # The MinIO offload activity returns a real URI that must be
        # threaded back into the final Jira comment.
        offload_uri = (
            "s3://platform-research/ai-runs/automation-jira-RES-77/output.json"
        )

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "firecrawl_search": [{"url": _ALLOWED_URL_A}],
            "firecrawl_scrape": scrape_payload,
            "minio_put_research_summary": offload_uri,
            "jira_add_comment": None,
            "audit_emit": None,
        }
        activity_mock = _activity_dispatcher(activity_routes)

        async def _run() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo",
                    (),
                    {"workflow_id": "automation-jira-RES-77"},
                )(),
            ):
                await wf._handle_research_summary_jira(inp)

        _drive(_run)

        called_names = [c.args[0] for c in activity_mock.call_args_list]

        # Long-content path: MinIO offload was invoked.
        assert "minio_put_research_summary" in called_names, (
            "long content must trigger the MinIO offload activity"
        )

        # The Jira comment carries the real MinIO URI returned by the
        # offload activity (not the formatter's placeholder sentinel).
        comment_calls = _activity_calls_for(activity_mock, "jira_add_comment")
        assert len(comment_calls) == 1
        body = _activity_args(comment_calls[0])[1]
        assert offload_uri in body, (
            f"expected MinIO URI {offload_uri!r} embedded in comment; "
            f"got: {body!r}"
        )
        # The placeholder sentinel from the formatter is replaced — it
        # must NOT leak into the final comment body.
        assert "minio://research-summary-pending" not in body
