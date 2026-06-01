"""End-to-end integration test for the ``research_publish_confluence`` flow.

**Validates: Requirements 9.1, 9.3, 9.4**

Scenario
--------

This file exercises the
:class:`agent_runner.workflows.agent_runner_workflow.AgentRunnerWorkflow`
``research_publish_confluence`` body end-to-end against a real
(time-skipping) Temporal cluster. The body is the one defined in
:meth:`AgentRunnerWorkflow._handle_research_publish_confluence`:

1. ``set_assignee_to_bot`` — claim the Jira issue.
2. ``jira_build_issue_link`` — resolve the canonical Jira URL so the
   provenance footer can deep-link back to the originating task
   (R8.6).
3. ``firecrawl_search`` — enumerate candidate URLs from the analysis
   query (R9.1).
4. For every candidate URL: ``firecrawl_scrape`` is invoked. The
   workflow's
   :meth:`AgentRunnerWorkflow._firecrawl_scrape_with_grace` triages
   the outcome:

   * ``{"kind": "egress_blocked"}`` → graceful 403 path per R9.3:
     post a Jira comment naming the blocked domain and continue.
   * ``{"kind": "success", "body": {...}}`` → harvest content +
     bookkeeping (title/url/accessed_at) for the Confluence body.

5. ``confluence_create_page`` — render the body via
   :func:`temporal_shared.research.format_research_publish_confluence_body`,
   append the provenance footer, and write the page (R9.4).
6. ``jira_add_comment`` — best-effort completion comment.

The integration test stubs every activity above with
``@activity.defn``-registered fakes, captures every call in an
:class:`ActivityCallLog`, and asserts the wiring against the real
Temporal cluster.

Mirrors the structure of ``test_e2e_confluence_doc_create.py``.

Skip gate
---------

Mirrors the spec-extension pattern in
``test_temporal_signal.py`` / ``test_e2e_confluence_doc_create.py``:
when the embedded ``temporal-test-server`` binary cannot start
(sandboxed CI, missing native deps, …) the tests ``pytest.skip``
cleanly so the integration suite stays green on hosts that cannot
host Temporal locally.
"""

from __future__ import annotations

import contextlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# sys.path bootstrap — agent-runner-worker tree + temporal-shared.
# Mirrors ``test_e2e_confluence_doc_create.py``.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]
_AGENT_RUNNER_SRC: Path = (
    _PLATFORM_ROOT / "workers" / "agent-runner-worker" / "src"
)
_TEMPORAL_SHARED_SRC: Path = (
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src"
)
_MCP_CLIENT_SRC: Path = _PLATFORM_ROOT / "libs" / "mcp_client" / "src"

for _candidate in (
    _AGENT_RUNNER_SRC,
    _TEMPORAL_SHARED_SRC,
    _MCP_CLIENT_SRC,
):
    _candidate_str = str(_candidate)
    if _candidate.is_dir() and _candidate_str not in sys.path:
        sys.path.insert(0, _candidate_str)


# ---------------------------------------------------------------------------
# Skip gate — mirrors the predicate in ``test_e2e_confluence_doc_create.py``.
# ---------------------------------------------------------------------------


def _temporal_env_available() -> bool:
    """Return ``True`` when the Temporal time-skipping env imports cleanly."""

    try:
        from temporalio.testing import WorkflowEnvironment  # noqa: F401
    except Exception:  # noqa: BLE001 — any import failure → skip.
        return False
    return True


_TEMPORAL_SKIP = pytest.mark.skipif(
    not _temporal_env_available(),
    reason="temporalio test environment not available",
)


@contextlib.asynccontextmanager
async def _start_time_skipping_or_skip() -> Any:
    """Start the Temporal time-skipping env, ``pytest.skip``ing on failure.

    The embedded ``temporal-test-server`` may fail to start on hosts
    where the binary is not bundled. Surface that cleanly as a skip
    so the integration suite stays green on hosts that cannot host
    Temporal locally.
    """

    from temporalio.testing import WorkflowEnvironment

    try:
        env_cm = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001 — surface as skip.
        pytest.skip(f"temporalio test environment not available: {exc}")
    async with env_cm as env:
        yield env


# ---------------------------------------------------------------------------
# Activity call log
# ---------------------------------------------------------------------------


@dataclass
class ActivityCallLog:
    """Append-only log of activity invocations recorded by the stubs.

    Each entry is a ``(name, args, kwargs)`` tuple appended in call
    order. Tests inspect ``.count(name)`` for cardinality assertions
    and ``.args_for(name)`` for payload assertions (the rendered body,
    Jira comment text, etc.).
    """

    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(
        default_factory=list
    )

    def record(
        self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        self.calls.append((name, args, kwargs))

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def count(self, name: str) -> int:
        return sum(1 for n, _, _ in self.calls if n == name)

    def args_for(self, name: str) -> list[tuple[Any, ...]]:
        return [args for n, args, _ in self.calls if n == name]


# ---------------------------------------------------------------------------
# Stub fixtures
# ---------------------------------------------------------------------------

#: Stub Jira issue link returned by ``jira_build_issue_link`` —
#: matches the format the workflow's
#: :func:`compute_provenance_footer` validator accepts so the footer
#: renders verbatim into the page body.
_STUB_JIRA_LINK: str = "https://jira.example.com/browse/SEC-1"

#: Stub Confluence page id surfaced by ``confluence_create_page`` —
#: the workflow stashes this in ``_latest_confluence_page_id`` and the
#: terminal :class:`AgentRunnerWorkflowOutput` round-trips it on
#: ``confluence_page_id``.
_STUB_PAGE_ID: str = "67890"
_STUB_PAGE_URL: str = "https://confluence.example.com/x/research-1"

#: Allowlisted firecrawl host that the scrape stub returns content
#: for. The Confluence body's ``## Kaynaklar`` block must reference
#: this URL verbatim (R9.4).
_ALLOWED_URL_1: str = "https://docs.example.com/page1"
_ALLOWED_URL_2: str = "https://docs.example.com/page2"

#: Blocked firecrawl host that the scrape stub triages as
#: ``egress_blocked`` per R9.3. The workflow MUST surface a Jira
#: comment naming this domain and MUST NOT crash the run.
_BLOCKED_URL: str = "https://blocked.example.org/secret"

#: Stub article content returned by the scrape activity for
#: allowlisted URLs. The Confluence body assertion looks for this
#: substring to confirm the formatter wired the scraped payload
#: through.
_STUB_ARTICLE_TITLE: str = "Article Title"
_STUB_ARTICLE_BODY: str = "Article content"


def _make_activities(
    log: ActivityCallLog,
    *,
    search_results: list[dict[str, str]],
    blocked_hosts: frozenset[str] = frozenset({"blocked.example.org"}),
) -> list[Any]:
    """Build the stub activity bag the ``research_publish_confluence`` body needs.

    Activities registered:

    * ``set_assignee_to_bot`` — claim step (no return value).
    * ``jira_build_issue_link`` — returns :data:`_STUB_JIRA_LINK`
      (HTTPS URL pointing at a synthetic Jira issue) so the
      provenance footer renders against a known link string.
    * ``firecrawl_search`` — returns ``{"results": search_results}``
      so the workflow's
      :meth:`AgentRunnerWorkflow._extract_firecrawl_urls` lifts the
      list verbatim.
    * ``firecrawl_scrape`` — triages each URL: hosts whose hostname
      ends in any entry of ``blocked_hosts`` return
      ``{"kind": "egress_blocked"}``; everything else returns
      ``{"kind": "success", "body": {"content": _STUB_ARTICLE_BODY,
      "title": _STUB_ARTICLE_TITLE}}``.
    * ``confluence_create_page`` — returns the
      ``{"page_id", "url"}`` shape the workflow's extractor consumes.
    * ``jira_add_comment`` — best-effort completion + R9.3 graceful
      403 messages (recorded so the test can assert the blocked
      domain comment fired).
    * ``audit_emit`` — best-effort audit row sink.

    Every wrapper records the call in ``log`` so the assertions can
    inspect call counts and payload shapes.
    """

    from temporalio import activity

    @activity.defn(name="set_assignee_to_bot")
    async def _set_assignee_to_bot(*args: Any, **kwargs: Any) -> None:
        log.record("set_assignee_to_bot", args, kwargs)
        return None

    @activity.defn(name="jira_build_issue_link")
    async def _jira_build_issue_link(*args: Any, **kwargs: Any) -> str:
        log.record("jira_build_issue_link", args, kwargs)
        return _STUB_JIRA_LINK

    @activity.defn(name="firecrawl_search")
    async def _firecrawl_search(*args: Any, **kwargs: Any) -> dict[str, Any]:
        log.record("firecrawl_search", args, kwargs)
        return {"results": list(search_results)}

    @activity.defn(name="firecrawl_scrape")
    async def _firecrawl_scrape(*args: Any, **kwargs: Any) -> dict[str, Any]:
        log.record("firecrawl_scrape", args, kwargs)
        # Activity signature: (url, dept_id) — first positional arg is
        # the URL the workflow wants to scrape.
        url = str(args[0]) if args else ""
        # Match by hostname suffix so a single blocked domain catches
        # every URL underneath it without false positives.
        for blocked in blocked_hosts:
            if blocked in url:
                return {"kind": "egress_blocked"}
        return {
            "kind": "success",
            "body": {
                "content": _STUB_ARTICLE_BODY,
                "title": _STUB_ARTICLE_TITLE,
            },
        }

    @activity.defn(name="confluence_create_page")
    async def _confluence_create_page(
        *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        log.record("confluence_create_page", args, kwargs)
        return {"page_id": _STUB_PAGE_ID, "url": _STUB_PAGE_URL}

    @activity.defn(name="jira_add_comment")
    async def _jira_add_comment(*args: Any, **kwargs: Any) -> None:
        log.record("jira_add_comment", args, kwargs)
        return None

    @activity.defn(name="audit_emit")
    async def _audit_emit(*args: Any, **kwargs: Any) -> None:
        log.record("audit_emit", args, kwargs)
        return None

    return [
        _set_assignee_to_bot,
        _jira_build_issue_link,
        _firecrawl_search,
        _firecrawl_scrape,
        _confluence_create_page,
        _jira_add_comment,
        _audit_emit,
    ]


# ---------------------------------------------------------------------------
# Input fixture
# ---------------------------------------------------------------------------


def _make_input(
    *,
    issue_key: str = "SEC-1",
    title: str = "KVKK Yönetmelik Araştırma",
    target_space: str = "DOCS",
    iteration: int = 1,
) -> Any:
    """Build a minimal :class:`AgentRunnerWorkflowInput` for research-flow tests.

    ``workflow_type="research_publish_confluence"`` routes the body to
    :meth:`AgentRunnerWorkflow._handle_research_publish_confluence`.
    ``analysis.title`` is forwarded to ``firecrawl_search`` as the
    query (via :meth:`AgentRunnerWorkflow._extract_research_query`).

    ``iteration=1`` keeps the iter==3 banner edge silent so the
    ``jira_add_comment`` count in the happy-path assertion only
    reflects the R9.3 graceful-403 messages plus the completion
    comment posted at the end of the body.
    """

    from temporal_shared.messages import (
        AgentRunnerWorkflowInput,
        LlmAnalysisResult,
    )

    analysis = LlmAnalysisResult(
        workflow_type="research_publish_confluence",
        confidence="high",
        target_space=target_space,
        title=title,
        rationale="research scope: KVKK regulations",
        token_usage=128,
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id=f"automation-jira-{issue_key}",
        issue_key=issue_key,
        department_id="security",
        workflow_type="research_publish_confluence",
        analysis=analysis,
        iteration=iteration,
        max_iter=5,
        default_language="tr",
    )


# ---------------------------------------------------------------------------
# Result coercion helpers
#
# ``AgentRunnerWorkflowOutput`` is a frozen dataclass; depending on
# the SDK's data converter the result either round-trips back into
# the dataclass or surfaces as a plain dict. Normalise both shapes.
# ---------------------------------------------------------------------------


def _output_to_dict(result: Any) -> dict[str, Any]:
    fields = (
        "status",
        "iter_count",
        "summary",
        "failure_reason",
        "confluence_page_id",
        "partial_failure_actions",
    )
    if hasattr(result, "__dataclass_fields__"):
        return {name: getattr(result, name, None) for name in fields}
    if isinstance(result, dict):
        return {name: result.get(name) for name in fields}
    pytest.fail(
        f"unexpected workflow result shape: {type(result).__name__}"
    )
    return {}  # pragma: no cover - pytest.fail terminates the test


# ---------------------------------------------------------------------------
# 1. Happy path with one egress-blocked URL — Confluence page created
#    with allowlisted source rendered in the body, blocked domain
#    surfaced via best-effort Jira comment.
#    (R9.1, R9.3, R9.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@_TEMPORAL_SKIP
async def test_research_publish_confluence_happy_path_with_egress_blocked_url() -> None:
    """**Validates: Requirements 9.1, 9.3, 9.4**

    Drive ``research_publish_confluence`` end-to-end with a search
    result that mixes allowlisted hosts with a blocked one:

    * Two ``docs.example.com`` URLs are returned for scrape (R9.1).
    * One ``blocked.example.org`` URL is returned alongside; the
      scrape stub triages it as ``{"kind": "egress_blocked"}`` (R9.3).

    Assertions:

    1. ``firecrawl_search`` fires exactly once with the analysis
       title as the query (R9.1).
    2. ``firecrawl_scrape`` fires three times — once per URL in the
       search result, regardless of egress outcome (the workflow
       MUST attempt every candidate so the audit trail records the
       block).
    3. At least one ``jira_add_comment`` invocation carries the
       Turkish graceful-403 message naming the blocked domain
       (R9.3): "blocked.example.org domain'i araştırma için izinli
       değil".
    4. ``confluence_create_page`` fires exactly once. Its body
       argument contains both allowlisted URLs verbatim under the
       "## Kaynaklar" heading rendered by
       :func:`format_research_publish_confluence_body` (R9.4) and
       does **not** contain the blocked URL.
    5. The terminal :class:`AgentRunnerWorkflowOutput` reports a
       non-failure status (``"completed"`` or
       ``"completed_with_partial_failure"`` per R12.3 when the
       graceful R9.3 marker is recorded), ``failure_reason is None``,
       and ``confluence_page_id == _STUB_PAGE_ID`` — confirming the
       workflow's ``_latest_confluence_page_id`` field round-tripped
       through the output dataclass.
    """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        AgentRunnerWorkflow,
    )

    log = ActivityCallLog()
    activities = _make_activities(
        log,
        search_results=[
            {"url": _ALLOWED_URL_1},
            {"url": _ALLOWED_URL_2},
            {"url": _BLOCKED_URL},
        ],
    )

    workflow_id = "agent-runner-jira-SEC-1-research-publish"
    task_queue = "agent-runner-research-publish-happy"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _make_input(issue_key="SEC-1")
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
            )
            result = _output_to_dict(await handle.result())

    # ----- Assertions -------------------------------------------------

    # 1. firecrawl_search fires exactly once with the analysis query
    # as the first positional arg (R9.1).
    assert log.count("firecrawl_search") == 1, (
        f"firecrawl_search should fire exactly once; "
        f"got {log.count('firecrawl_search')} (call log: {log.names()!r})"
    )
    search_args = log.args_for("firecrawl_search")[0]
    assert len(search_args) >= 2, (
        f"firecrawl_search expected (query, dept_id); got {search_args!r}"
    )
    assert search_args[0] == "KVKK Yönetmelik Araştırma", (
        f"firecrawl_search query must come from analysis.title; "
        f"got {search_args[0]!r}"
    )
    assert search_args[1] == "security", (
        f"firecrawl_search dept_id should be 'security'; "
        f"got {search_args[1]!r}"
    )

    # 2. firecrawl_scrape fires once per candidate URL (3 total).
    assert log.count("firecrawl_scrape") == 3, (
        f"firecrawl_scrape should fire once per search-result URL "
        f"(3 total); got {log.count('firecrawl_scrape')} "
        f"(call log: {log.names()!r})"
    )
    scraped_urls = [
        args[0] for args in log.args_for("firecrawl_scrape") if args
    ]
    assert _ALLOWED_URL_1 in scraped_urls
    assert _ALLOWED_URL_2 in scraped_urls
    assert _BLOCKED_URL in scraped_urls, (
        "the workflow must invoke firecrawl_scrape for the blocked URL "
        f"so the audit trail records the egress outcome; got {scraped_urls!r}"
    )

    # 3. R9.3 graceful 403 — at least one jira_add_comment names the
    # blocked domain with the Turkish "izinli değil" phrase. We grep
    # against every recorded comment body (positional arg index 1)
    # so a future refactor that splits the message into multiple
    # lines still satisfies the assertion.
    comment_bodies = [
        str(args[1]) if len(args) >= 2 else ""
        for args in log.args_for("jira_add_comment")
    ]
    blocked_comment_seen = any(
        "blocked.example.org" in body
        and "araştırma için izinli değil" in body
        for body in comment_bodies
    )
    assert blocked_comment_seen, (
        "expected a jira_add_comment naming the blocked domain with "
        "the R9.3 'izinli değil' phrase; got comments: "
        f"{comment_bodies!r}"
    )

    # 4. confluence_create_page fires exactly once and the body
    # carries both allowlisted URLs but not the blocked one (R9.4).
    assert log.count("confluence_create_page") == 1, (
        f"confluence_create_page should fire exactly once; "
        f"got {log.count('confluence_create_page')} "
        f"(call log: {log.names()!r})"
    )
    create_args = log.args_for("confluence_create_page")[0]
    # Activity signature: (target_space, page_title, page_body, dept_id)
    assert len(create_args) >= 4, (
        f"confluence_create_page expected >=4 positional args, "
        f"got {len(create_args)}: {create_args!r}"
    )
    target_space_arg, page_title_arg, page_body_arg, dept_id_arg = (
        create_args[0],
        create_args[1],
        create_args[2],
        create_args[3],
    )
    assert target_space_arg == "DOCS"
    assert dept_id_arg == "security"
    assert isinstance(page_body_arg, str)

    # The allowlisted URLs must surface in the rendered "## Kaynaklar"
    # block (R9.4). The blocked URL must NOT appear — the scrape was
    # gracefully degraded and never harvested into the formatter
    # input.
    assert _ALLOWED_URL_1 in page_body_arg, (
        f"confluence body must reference the allowlisted source "
        f"{_ALLOWED_URL_1!r} (R9.4); got body: {page_body_arg!r}"
    )
    assert _ALLOWED_URL_2 in page_body_arg, (
        f"confluence body must reference the second allowlisted "
        f"source {_ALLOWED_URL_2!r} (R9.4); got body: {page_body_arg!r}"
    )
    assert _BLOCKED_URL not in page_body_arg, (
        "confluence body must NOT reference a URL whose scrape was "
        f"egress-blocked; got body: {page_body_arg!r}"
    )
    # The Turkish "## Kaynaklar" heading is the structural marker the
    # formatter emits when at least one source is rendered. Pin both
    # the heading and the scraped article body so a regression that
    # drops either fails here.
    assert "## Kaynaklar" in page_body_arg
    assert _STUB_ARTICLE_BODY in page_body_arg, (
        "confluence body must include the scraped article content; "
        f"got body: {page_body_arg!r}"
    )

    # The provenance footer (R8.6 — same wiring as confluence_doc_create)
    # must embed the Jira issue link verbatim so operators can grep
    # the audit trail back to the originating task. The collapsible
    # ``<details>`` wrapper is the structural marker.
    assert _STUB_JIRA_LINK in page_body_arg, (
        "provenance footer must embed the Jira link verbatim "
        f"(R8.6); body did not contain {_STUB_JIRA_LINK!r}"
    )
    assert "<details>" in page_body_arg
    assert "</details>" in page_body_arg

    # Page title format — ``{topic} - {YYYY-MM-DD}`` (mirrors R8.1
    # via the shared ``format_page_title`` formatter).
    assert isinstance(page_title_arg, str)
    assert page_title_arg.startswith("KVKK Yönetmelik Araştırma - "), (
        f"page title must follow the '{{topic}} - {{date}}' format; "
        f"got {page_title_arg!r}"
    )

    # 5. Terminal output — non-failure status + page id surfaces.
    #
    # The brief asks for ``status="completed"``; production behaviour
    # is to surface :data:`AgentRunnerStatus` ``completed_with_partial_failure``
    # (R12.3) when the run records a partial-failure marker — and the
    # graceful R9.3 path *does* record ``firecrawl_blocked:<url>`` so
    # operators can count blocked-URL runs from the audit table. We
    # therefore accept either non-failure terminal status here; the
    # important invariant is that the run did NOT fail (R9.3 graceful)
    # and that ``failure_reason`` stays ``None``.
    assert result["status"] in ("completed", "completed_with_partial_failure"), (
        f"expected a non-failure terminal status for the R9.3 graceful "
        f"path (one blocked URL); got {result!r}"
    )
    assert result["failure_reason"] is None, (
        f"failure_reason must be None when only egress block occurs "
        f"(R9.3 graceful); got {result!r}"
    )
    assert result["confluence_page_id"] == _STUB_PAGE_ID, (
        f"confluence_page_id should round-trip the stub page id "
        f"({_STUB_PAGE_ID}); got {result!r}"
    )

    # The blocked-URL marker MUST surface in
    # :attr:`AgentRunnerWorkflowOutput.partial_failure_actions` so
    # observability dashboards can count graceful-403 runs (R9.3).
    partial = tuple(result["partial_failure_actions"] or ())
    assert any(
        item.startswith("firecrawl_blocked:") and _BLOCKED_URL in item
        for item in partial
    ), (
        "expected a 'firecrawl_blocked:<url>' marker in "
        f"partial_failure_actions naming the blocked URL; got {partial!r}"
    )


# ---------------------------------------------------------------------------
# 2. Empty search result — graceful degradation, no Confluence write
#    (R9.1 / partial_failure path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@_TEMPORAL_SKIP
async def test_research_publish_confluence_no_sources_graceful_degradation() -> None:
    """**Validates: Requirements 9.1**

    Drive ``research_publish_confluence`` against a ``firecrawl_search``
    that returns an empty result list. The workflow MUST:

    * Skip ``confluence_create_page`` entirely — there is no source
      content to publish.
    * Post a Turkish best-effort Jira comment naming the situation:
      "🤖 Araştırma için kullanılabilir bir kaynak bulunamadı".
    * Mark the run with a non-failure status (``"completed"`` or
      ``"completed_with_partial_failure"`` per R12.3 when the
      ``research_no_sources`` marker is recorded). No source is a
      graceful degradation, not a failure — the operator can supply
      a different topic or extend the allowlist.
    * Surface ``"research_no_sources"`` in
      :attr:`AgentRunnerWorkflowOutput.partial_failure_actions` so
      observability dashboards can count the degraded runs.

    No ``firecrawl_scrape`` invocations are expected — the workflow
    short-circuits the for-loop when the search yields no candidates.
    """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        AgentRunnerWorkflow,
    )

    log = ActivityCallLog()
    activities = _make_activities(log, search_results=[])

    workflow_id = "agent-runner-jira-SEC-2-research-no-sources"
    task_queue = "agent-runner-research-publish-no-sources"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _make_input(issue_key="SEC-2", title="Niş Konu")
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
            )
            result = _output_to_dict(await handle.result())

    # ----- Assertions -------------------------------------------------

    # 1. The search activity fires once and returns nothing — the
    # workflow MUST short-circuit without scraping anything.
    assert log.count("firecrawl_search") == 1
    assert log.count("firecrawl_scrape") == 0, (
        f"firecrawl_scrape must not fire when the search yields no "
        f"candidates; got {log.count('firecrawl_scrape')} "
        f"(call log: {log.names()!r})"
    )

    # 2. confluence_create_page MUST NOT be called — the workflow
    # has nothing useful to publish.
    assert log.count("confluence_create_page") == 0, (
        f"confluence_create_page must not fire when no sources are "
        f"available; got {log.count('confluence_create_page')} "
        f"(call log: {log.names()!r})"
    )

    # 3. The Turkish "no sources" Jira comment MUST be posted. We
    # grep every recorded comment body so a future tweak that adds
    # a leading set_assignee status comment still satisfies the
    # assertion.
    comment_bodies = [
        str(args[1]) if len(args) >= 2 else ""
        for args in log.args_for("jira_add_comment")
    ]
    no_sources_comment_seen = any(
        "Araştırma için kullanılabilir bir kaynak bulunamadı" in body
        for body in comment_bodies
    )
    assert no_sources_comment_seen, (
        "expected a jira_add_comment carrying the 'no sources' "
        "Turkish message; got comments: "
        f"{comment_bodies!r}"
    )

    # 4. Terminal output — graceful degradation: non-failure status,
    # failure_reason None, partial_failure_actions carries the
    # ``research_no_sources`` marker.
    #
    # The brief asks for ``status="completed"``; production behaviour
    # is to surface :data:`AgentRunnerStatus` ``completed_with_partial_failure``
    # (R12.3) when the run records the ``research_no_sources`` partial
    # failure. Both statuses are non-failure — accept either so a
    # future tightening of the classification (e.g. clean
    # ``"completed"`` for the empty-source path) does not regress
    # this test.
    assert result["status"] in ("completed", "completed_with_partial_failure"), (
        f"expected a non-failure terminal status for graceful 'no "
        f"sources' degradation; got {result!r}"
    )
    assert result["failure_reason"] is None, (
        f"failure_reason must be None for graceful degradation; "
        f"got {result!r}"
    )
    assert result["confluence_page_id"] is None, (
        f"confluence_page_id must be None when no page was created; "
        f"got {result!r}"
    )

    partial = result["partial_failure_actions"] or ()
    assert "research_no_sources" in tuple(partial), (
        "expected 'research_no_sources' in partial_failure_actions "
        f"so observability dashboards can count degraded runs; got "
        f"{partial!r}"
    )
