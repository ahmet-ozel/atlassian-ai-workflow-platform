"""End-to-end integration test for the ``confluence_doc_create`` workflow.


Scenario
--------

This file exercises the
:class:`agent_runner.workflows.agent_runner_workflow.AgentRunnerWorkflow`
``confluence_doc_create`` body end-to-end against a real (time-skipping)
Temporal cluster. The body is the one defined in
:meth:`AgentRunnerWorkflow._handle_confluence_doc_create`:

1. ``set_assignee_to_bot`` — claim the Jira issue.
2. ``jira_build_issue_link`` — resolve the canonical Jira URL so the
 provenance footer can deep-link back to the originating task
 .
3. ``llm_generate_doc`` — token-capped LLM call that returns the page
 body.
4. ``confluence_create_page`` — the body + provenance footer are
 appended together and written through the activity layer .
5. ``jira_add_comment`` — best-effort completion comment ( audit
 trail).

The integration tests drive the workflow with stub
``@activity.defn``-registered activities, capture every call in an
:class:`ActivityCallLog`, and assert:

* **Happy path** — exactly one
 ``confluence_create_page`` call whose body contains the verbatim
 Jira issue link returned by ``jira_build_issue_link`` (the
 provenance footer marker), terminal status ``"completed"``, and
 ``get_latest_confluence_page_id``-equivalent surface (the
 workflow's ``_latest_confluence_page_id`` field — round-tripped
 through :class:`AgentRunnerWorkflowOutput.confluence_page_id`)
 matches the stub page id.

* **Invalid topic ** — the workflow rejects topics with
 control / XML-reserved characters with
 ``failure_reason="confluence_title_invalid"`` (the
 :func:`format_page_title` validation contract).

The section-level dedup invariant for ``confluence_doc_update``
 is exercised exhaustively by the property suite under
``platform/tests/property/test_confluence_invariants.py``;
the integration layer here intentionally focuses on the create-flow
happy path so we do not couple the integration suite to two
workflow types in one file. The companion test
``test_e2e_confluence_doc_update.py`` (when added) extends the
coverage with a stateful update scenario.

Skip gate
---------

Mirrors the existing integration-test pattern in
``test_temporal_signal.py``: when the embedded
``temporal-test-server`` binary cannot start (sandboxed CI, missing
native deps, …) the tests ``pytest.skip`` cleanly so the integration
suite stays green on hosts that cannot host Temporal locally.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# sys.path bootstrap — agent-runner-worker tree + temporal-shared.
# Mirrors ``test_temporal_signal.py`` / ``test_temporal_loop_cap.py``.
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
# Skip gate — mirrors the predicate in ``test_temporal_loop_cap.py``.
# ---------------------------------------------------------------------------


def _temporal_env_available() -> bool:
    """Return ``True`` when the Temporal time-skipping env imports cleanly.

 The module-level ``pytest.mark.skipif`` backed by this predicate
 is applied to every test so hosts that cannot resolve the
 ``temporalio.testing`` sub-module skip cleanly at collection
 time. The runtime variant
 :func:`_start_time_skipping_or_skip` covers the case where the
 import succeeds but the embedded Temporal test server fails to
 start.
 """

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
 and ``.args_for(name)`` for payload assertions (the provenance
 footer body, the page title, etc.).
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
# Stub activity factory
# ---------------------------------------------------------------------------

#: Stub Jira issue link returned by ``jira_build_issue_link`` —
#: matches the format the workflow's
#: :func:`compute_provenance_footer` validator accepts (HTTPS URL with
#: ``/browse/{ISSUE_KEY}`` path) so the footer is rendered verbatim
#: into the page body.
_STUB_JIRA_LINK: str = "https://jira.example.com/browse/PAY-1"

#: Stub page id surfaced by ``confluence_create_page`` — the
#: workflow stashes this in ``_latest_confluence_page_id`` and the
#: terminal :class:`AgentRunnerWorkflowOutput` round-trips it on
#: ``confluence_page_id``.
_STUB_PAGE_ID: str = "12345"
_STUB_PAGE_URL: str = "https://confluence.example.com/x/abc"

#: Stub LLM body — the test asserts the workflow appends the
#: provenance footer to this verbatim.
_STUB_LLM_BODY: str = (
    "## Section A\n\nFirst paragraph.\n\n## Section B\n\nSecond paragraph."
)


def _make_activities(log: ActivityCallLog) -> list[Any]:
    """Build the stub activity bag the ``confluence_doc_create`` body needs.

 Activities registered:

 * ``set_assignee_to_bot`` — claim step (no return value).
 * ``jira_build_issue_link`` — returns :data:`_STUB_JIRA_LINK`
 (HTTPS URL pointing at a synthetic Jira issue) so the
 provenance footer renders against a known link string.
 * ``llm_generate_doc`` — returns ``{"body": _STUB_LLM_BODY}``
 so the workflow's body extractor lifts the body verbatim.
 * ``confluence_create_page`` — returns the
 ``{"page_id", "url"}`` shape the workflow's extractor
 consumes.
 * ``jira_add_comment`` — best-effort completion comment
 (recorded but otherwise a no-op).
 * ``audit_emit`` — best-effort audit row sink. The
 ``confluence_doc_create`` body itself does not emit audits
 directly, but the iter==3 banner / token-cap branches do, so
 registering the activity keeps the worker bootstrap forgiving.

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

    @activity.defn(name="llm_generate_doc")
    async def _llm_generate_doc(
        *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        log.record("llm_generate_doc", args, kwargs)
        return {"body": _STUB_LLM_BODY}

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
        _llm_generate_doc,
        _confluence_create_page,
        _jira_add_comment,
        _audit_emit,
    ]


# ---------------------------------------------------------------------------
# Input fixture
# ---------------------------------------------------------------------------


def _make_input(
    *,
    issue_key: str = "PAY-1",
    title: str = "KVKK Yönetmelik Analizi",
    target_space: str = "DOCS",
    iteration: int = 1,
) -> Any:
    """Build a minimal :class:`AgentRunnerWorkflowInput` for create-flow tests.

 ``workflow_type="confluence_doc_create"`` routes the body to
 :meth:`AgentRunnerWorkflow._handle_confluence_doc_create`. The
 ``analysis.title`` is the page topic embedded in the page title;
 callers override it to drive the invalid-topic branch.

 ``iteration=1`` keeps the iter==3 banner edge silent so the
 ``jira_add_comment`` count in the happy-path assertion only
 reflects the completion comment posted at the end of the
 create-flow body.
 """

    from temporal_shared.messages import (
        AgentRunnerWorkflowInput,
        LlmAnalysisResult,
    )

    analysis = LlmAnalysisResult(
        workflow_type="confluence_doc_create",
        confidence="high",
        target_space=target_space,
        title=title,
        rationale="e2e confluence_doc_create test",
        token_usage=42,
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id=f"automation-jira-{issue_key}",
        issue_key=issue_key,
        department_id="payments",
        workflow_type="confluence_doc_create",
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
# 1. Happy path — provenance footer attached, status="completed"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@_TEMPORAL_SKIP
async def test_confluence_doc_create_happy_path_includes_provenance_footer() -> None:
    """Drive ``confluence_doc_create`` end-to-end:

 1. ``set_assignee_to_bot`` is called once (claim step).
 2. ``jira_build_issue_link`` is called once and returns
 :data:`_STUB_JIRA_LINK`.
 3. ``llm_generate_doc`` is called once and returns
 ``{"body": _STUB_LLM_BODY}``.
 4. ``confluence_create_page`` is called exactly once. Its body
 argument is the LLM body **plus** a provenance footer that
 embeds :data:`_STUB_JIRA_LINK` verbatim — the marker the 
 audit grep relies on.
 5. The terminal :class:`AgentRunnerWorkflowOutput` reports
 ``status="completed"``, ``failure_reason is None``, and
 ``confluence_page_id == _STUB_PAGE_ID`` — confirming the
 workflow's ``_latest_confluence_page_id`` field round-tripped
 through the output dataclass (the test's stand-in for the
 ``get_latest_confluence_page_id`` query mentioned in the
 workflow behavior; the value is exposed via the output instead
 of a dedicated query, so we assert against the output here).
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        AgentRunnerWorkflow,
    )

    log = ActivityCallLog()
    activities = _make_activities(log)

    workflow_id = "agent-runner-jira-PAY-1-doc-create"
    task_queue = "agent-runner-confluence-doc-create-happy"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _make_input(issue_key="PAY-1")
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
            )
            result = _output_to_dict(await handle.result())

    # ----- Assertions -------------------------------------------------

    # 1. Claim + link-resolution + LLM + create_page each fire once.
    assert log.count("set_assignee_to_bot") == 1, (
        f"set_assignee_to_bot should fire exactly once; "
        f"got {log.count('set_assignee_to_bot')} "
        f"(call log: {log.names()!r})"
    )
    assert log.count("jira_build_issue_link") == 1, (
        f"jira_build_issue_link should fire exactly once; "
        f"got {log.count('jira_build_issue_link')} "
        f"(call log: {log.names()!r})"
    )
    assert log.count("llm_generate_doc") == 1, (
        f"llm_generate_doc should fire exactly once; "
        f"got {log.count('llm_generate_doc')} "
        f"(call log: {log.names()!r})"
    )
    assert log.count("confluence_create_page") == 1, (
        f"confluence_create_page should fire exactly once; "
        f"got {log.count('confluence_create_page')} "
        f"(call log: {log.names()!r})"
    )

    # 2. Provenance footer — the body arg of
    # confluence_create_page must contain the Jira link verbatim.
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
    assert dept_id_arg == "payments"
    assert isinstance(page_body_arg, str)
    assert _STUB_JIRA_LINK in page_body_arg, (
        "provenance footer must embed the Jira link verbatim "
        f"; body did not contain {_STUB_JIRA_LINK!r}: "
        f"{page_body_arg!r}"
    )
    # The collapsible <details> wrapper from
    # :func:`compute_provenance_footer` is the structural marker that
    # operators grep for; assert both the open and close tags are
    # present so a regression that drops the wrapper but keeps the
    # link still fails the test.
    assert "<details>" in page_body_arg
    assert "</details>" in page_body_arg
    # The original LLM body must be present unchanged — the footer
    # is appended, not substituted.
    assert _STUB_LLM_BODY in page_body_arg, (
        f"LLM body must be preserved verbatim; got {page_body_arg!r}"
    )

    # 3. Page title format — ``{topic} - {YYYY-MM-DD}`` .
    assert isinstance(page_title_arg, str)
    assert page_title_arg.startswith("KVKK Yönetmelik Analizi - "), (
        f"page title must follow the '{{topic}} - {{date}}' format "
        f"; got {page_title_arg!r}"
    )

    # 4. Terminal output — completed + page id surfaces correctly.
    assert result["status"] == "completed", (
        f"expected status=completed for happy path, got {result!r}"
    )
    assert result["failure_reason"] is None, (
        f"failure_reason must be None for happy path, got {result!r}"
    )
    assert result["confluence_page_id"] == _STUB_PAGE_ID, (
        f"confluence_page_id should round-trip the stub page id "
        f"({_STUB_PAGE_ID}); got {result!r}"
    )


# ---------------------------------------------------------------------------
# 2. Invalid topic — failure_reason="confluence_title_invalid" 
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
@_TEMPORAL_SKIP
async def test_confluence_doc_create_with_invalid_topic_fails() -> None:
    """The :func:`temporal_shared.confluence.format_page_title` validator
 rejects topics that contain control characters or XML-reserved
 characters (``<``, ``>``, ``&``, ``"``). When the LLM emits a
 structurally invalid title the workflow body must surface this
 cleanly with ``failure_reason="confluence_title_invalid"`` rather
 than leaking a raw ``InvalidTopicError`` to the terminal output —
 the failure category is the audit-stable name the rest of the
 platform (audit table, ops dashboards) keys off of.

 Note on test fixture choice
 ---------------------------

 The original test note suggested *"set analysis.title to empty
 string"* to drive this branch. That alone does not trigger the
 failure: the workflow body falls back to ``inp.issue_key`` when
 ``analysis.title`` is empty (see
 :meth:`AgentRunnerWorkflow._handle_confluence_doc_create`), so a
 blank title produces a valid topic from the issue key. To
 genuinely trigger :class:`InvalidTopicError` we pass a topic
 containing a forbidden ``<`` character, which is the smallest
 perturbation that exercises the
 ``confluence_title_invalid`` branch end-to-end through a real
 Temporal cluster. This deviation is documented in the test
 body so a future reader does not change it back to an empty
 string.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        AgentRunnerWorkflow,
    )

    log = ActivityCallLog()
    activities = _make_activities(log)

    workflow_id = "agent-runner-jira-PAY-2-invalid-title"
    task_queue = "agent-runner-confluence-doc-create-invalid"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            # ``<script>`` is rejected by the format_page_title
            # validator's _DISALLOWED_TITLE_CHARS_RE pattern; the
            # workflow must surface this as a stable failure_reason.
            inp = _make_input(
                issue_key="PAY-2",
                title="<script>alert(1)</script>",
            )
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
            )
            result = _output_to_dict(await handle.result())

    # ----- Assertions -------------------------------------------------

    # The title-validation branch fires *after* set_assignee_to_bot
    # and jira_build_issue_link (steps 1-2) but *before* the LLM
    # body call (step 4) and the confluence_create_page call
    # (step 5). Pin those expectations so a regression in the
    # ordering is caught here.
    assert log.count("set_assignee_to_bot") == 1
    # ``jira_build_issue_link`` runs in step 2 and is best-effort —
    # exact count >=1 is accepted in case future refactors add a
    # retry, but the workflow body today fires it exactly once.
    assert log.count("jira_build_issue_link") == 1
    # The LLM and create-page calls MUST NOT have been made — the
    # title validation aborts the body before reaching them.
    assert log.count("llm_generate_doc") == 0, (
        f"llm_generate_doc must not fire when the title is invalid; "
        f"call log: {log.names()!r}"
    )
    assert log.count("confluence_create_page") == 0, (
        f"confluence_create_page must not fire when the title is "
        f"invalid; call log: {log.names()!r}"
    )

    # Terminal output — failed + stable failure category.
    assert result["status"] == "failed", (
        f"expected status=failed for invalid title, got {result!r}"
    )
    assert result["failure_reason"] == "confluence_title_invalid", (
        f"expected failure_reason=confluence_title_invalid; "
        f"got {result!r}"
    )
    # No page id was created — the field round-trips ``None``.
    assert result["confluence_page_id"] is None, (
        f"confluence_page_id must be None for failed runs; "
        f"got {result!r}"
    )


# ---------------------------------------------------------------------------
# 3. confluence_doc_update — section hash dedup skips repeated content
# ---------------------------------------------------------------------------
#
# The full property-based coverage for lives at
# ``platform/tests/property/test_confluence_invariants.py``.
# The integration check here is intentionally narrow: it confirms the
# end-to-end wiring of the dedup logic when two sections in a single
# ``confluence_doc_update`` run carry identical ``content_hash``
# values — the second section MUST be skipped without invoking
# ``confluence_update_page`` a second time.
#
# Constraint: the workflow's ``_confluence_section_hashes`` set is
# in-memory state that resets per workflow run. A "second iteration"
# (signal-driven re-entry or a fresh workflow) cannot trivially be
# tested without rebuilding the gateway dispatch — that scenario is
# the property test's domain. The integration layer here pins the
# single-run dedup path: same content_hash for two sections → exactly
# one update activity call.
# ---------------------------------------------------------------------------


def _make_update_input(
    *,
    issue_key: str = "PAY-3",
    title: str = "Section Update Topic",
    target_page_id: str = "p-update-1",
    iteration: int = 1,
) -> Any:
    """Build an ``AgentRunnerWorkflowInput`` for the update flow.

 ``workflow_type="confluence_doc_update"`` routes the body to
 :meth:`AgentRunnerWorkflow._handle_confluence_doc_update`.
 ``analysis.target_page_id`` is required by the body; without it
 the workflow returns ``failure_reason="confluence_page_id_missing"``.
 """

    from temporal_shared.messages import (
        AgentRunnerWorkflowInput,
        LlmAnalysisResult,
    )

    analysis = LlmAnalysisResult(
        workflow_type="confluence_doc_update",
        confidence="high",
        target_page_id=target_page_id,
        title=title,
        rationale="e2e confluence_doc_update dedup test",
        token_usage=42,
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id=f"automation-jira-{issue_key}",
        issue_key=issue_key,
        department_id="payments",
        workflow_type="confluence_doc_update",
        analysis=analysis,
        iteration=iteration,
        max_iter=5,
        default_language="tr",
    )


def _make_update_activities(
    log: ActivityCallLog,
    *,
    sections: list[dict[str, str]],
) -> list[Any]:
    """Build the activity bag the ``confluence_doc_update`` body needs.

 Activities registered (in addition to the ones already covered
 by :func:`_make_activities`):

 * ``confluence_get_page`` — returns the page metadata + the
 caller-supplied list of sections (each with a precomputed
 ``content_hash`` so the workflow does not have to recompute,
 and the test can stage two sections with identical hashes).
 ``last_editor_account_id`` is set to a synthetic non-bot
 account but ``last_edit_at`` is left ``None`` so the
 overwrite-protection branch falls through to the proceed
 decision (no recent edit).
 * ``confluence_update_page`` — recorded so the test can count
 invocations.
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

    @activity.defn(name="confluence_get_page")
    async def _confluence_get_page(
        *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        log.record("confluence_get_page", args, kwargs)
        return {
            "page_id": args[0] if args else "p-update-1",
            "title": "Section Update Topic",
            "last_editor_account_id": None,
            "last_edit_at": None,
            "sections": sections,
        }

    @activity.defn(name="confluence_update_page")
    async def _confluence_update_page(*args: Any, **kwargs: Any) -> None:
        log.record("confluence_update_page", args, kwargs)
        return None

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
        _confluence_get_page,
        _confluence_update_page,
        _jira_add_comment,
        _audit_emit,
    ]


@pytest.mark.asyncio
@pytest.mark.integration
@_TEMPORAL_SKIP
async def test_confluence_doc_update_dedup_skips_seen_section() -> None:
    """Drive ``confluence_doc_update`` against a page whose
 ``confluence_get_page`` activity returns two sections carrying
 the **same** ``content_hash`` value. The workflow body adds the
 hash of the first section to ``_confluence_section_hashes``
 after the first ``confluence_update_page`` succeeds; when the
 body iterates to the second section the
 :func:`temporal_shared.confluence_dedup.should_skip_section_update`
 predicate sees the four-tuple
 ``(workflow_id, page_id, section_path_2, shared_hash)`` —
 Wait: the section_path is part of the dedup key, so two
 sections with different paths but identical hashes are
 technically not collisions per the four-tuple contract.

 To exercise the dedup path end-to-end with a single workflow
 run we therefore stage **two sections that share both
 ``section_path`` AND ``content_hash``** (the same
 section listed twice — a degenerate but valid input the
 activity layer can produce when the page tree contains a
 repeated section). The first occurrence updates the page; the
 second occurrence hits the workflow's in-memory hash set and
 is skipped via the
 :data:`temporal_shared.confluence_dedup.AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP`
 audit row.

 Assertions:

 * ``confluence_update_page`` is called exactly **once** despite
 two sections being returned by ``confluence_get_page``.
 * The workflow's
 :meth:`AgentRunnerWorkflow.get_confluence_section_hashes`
 query reports the single hash (sorted, deduplicated tuple).
 * Terminal status is ``"completed"``.
 * The :data:`AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP` audit row
 was emitted at least once for the skipped section.

 Single-run versus multi-run dedup
 ---------------------------------

 The workflow can also be re-entered in a pattern where a fresh
 workflow rerun observes the prior run's hashes. That scenario
 cannot be tested without persisting the hash set across
 workflow runs (today the set is per-instance state). The
 multi-run invariant is exercised by the property test at
 ``platform/tests/property/test_confluence_invariants.py``
 which generates the hash table directly. This
 integration test is the single-run end-to-end pin: it
 confirms the production dedup wiring fires on a real Temporal
 cluster.
 """

    from temporalio.worker import Worker

    from agent_runner.workflows.agent_runner_workflow import (
        AgentRunnerWorkflow,
    )

    log = ActivityCallLog()
    # Two sections with identical (section_path, content_hash).
    # The workflow's body hashes the four-tuple
    # (workflow_id, page_id, section_path, content_hash); identical
    # hashes on the same path collide on the second iteration.
    shared_hash = "a" * 64  # 64-char sha256-shaped sentinel hex string
    sections = [
        {
            "section_path": "§1/Overview",
            "content": "Overview body — first occurrence.",
            "content_hash": shared_hash,
        },
        {
            "section_path": "§1/Overview",
            "content": "Overview body — second occurrence (dedup).",
            "content_hash": shared_hash,
        },
    ]
    activities = _make_update_activities(log, sections=sections)

    workflow_id = "agent-runner-jira-PAY-3-update-dedup"
    task_queue = "agent-runner-confluence-doc-update-dedup"

    async with _start_time_skipping_or_skip() as env:
        async with Worker(
            env.client,
            task_queue=task_queue,
            workflows=[AgentRunnerWorkflow],
            activities=activities,
        ):
            inp = _make_update_input(
                issue_key="PAY-3", target_page_id="p-update-1"
            )
            handle = await env.client.start_workflow(
                AgentRunnerWorkflow.__name__,
                inp,
                id=workflow_id,
                task_queue=task_queue,
            )
            result = _output_to_dict(await handle.result())
            section_hashes = await handle.query(
                "get_confluence_section_hashes"
            )

    # ----- Assertions -------------------------------------------------

    # 1. Update activity fires exactly once — the second section
    # was deduplicated.
    assert log.count("confluence_update_page") == 1, (
        f"confluence_update_page must fire exactly once when two "
        f"sections share the same content_hash; got "
        f"{log.count('confluence_update_page')} (call log: "
        f"{log.names()!r})"
    )

    # 2. The workflow query reports the single hash that was
    # written. The query returns a tuple of strings (sorted).
    assert isinstance(section_hashes, (tuple, list))
    assert len(section_hashes) == 1, (
        f"get_confluence_section_hashes should report one entry for "
        f"a single distinct hash; got {section_hashes!r}"
    )
    assert section_hashes[0] == shared_hash, (
        f"section hash should match the staged sentinel; got "
        f"{section_hashes!r}"
    )

    # 3. The dedup audit row must have fired for the skipped
    # section. The body emits via ``audit_emit`` with a
    # payload dict; we look for the action name in any recorded
    # call.
    dedup_action_seen = any(
        any(
            isinstance(arg, dict)
            and arg.get("action") == "confluence_section_dedup_skip"
            for arg in args
        )
        for args in log.args_for("audit_emit")
    )
    assert dedup_action_seen, (
        "confluence_section_dedup_skip audit row must fire for the "
        f"skipped section; audit calls: "
        f"{log.args_for('audit_emit')!r}"
    )

    # 4. Terminal status — the dedup branch is a happy-path skip;
    # the workflow completes cleanly.
    assert result["status"] == "completed", (
        f"expected status=completed for a successful update with "
        f"dedup; got {result!r}"
    )
    assert result["failure_reason"] is None
