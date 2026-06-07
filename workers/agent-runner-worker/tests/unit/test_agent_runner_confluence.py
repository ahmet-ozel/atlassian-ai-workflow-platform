"""Unit tests for ``AgentRunnerWorkflow`` ``confluence_doc_*`` flows.

Covers the main Confluence behaviors:

    1. ``confluence_doc_create`` happy path - verifies the activity
       sequence (``set_assignee_to_bot`` → ``jira_build_issue_link``
       → ``llm_generate_doc`` → ``confluence_create_page`` →
       ``jira_add_comment`` carrying the page link) and that the
       provenance footer is appended to the page body before the
       create call.
    2. ``confluence_doc_update`` overwrite-protection skip - when
       :func:`should_skip_overwrite` fires (a non-bot user edited the
       page in the last 5 minutes) the update activity is **not**
       called, the ``confluence_overwrite_protected`` audit row is
       emitted, and a needs_info-style Jira comment is posted
       for recent human edits.
    3. ``confluence_doc_update`` section-dedup - when a section's
       content_hash is already present in the workflow's
       ``_confluence_section_hashes`` set the per-section update
       activity is skipped for that section and the
       ``confluence_section_dedup_skip`` audit row is emitted.
    4. ``confluence_doc_update`` ``_AI_PROBE_*`` skip - a probe-titled
       page short-circuits the update path before the dedup loop
       runs; no ``confluence_update_page`` is invoked.
    5. Provenance footer present in BOTH create and update bodies -
       the footer text from
       :func:`temporal_shared.confluence.compute_provenance_footer`
       must appear verbatim in the body passed to
       ``confluence_create_page`` and ``confluence_update_page`` so
       the bot's authorship is auditable on the rendered page
       on the update call.

The tests drive the body methods directly
(``_handle_confluence_doc_create`` / ``_handle_confluence_doc_update``)
without spinning up a Temporal worker. ``temporalio.workflow``
primitives (``execute_activity``, ``info``, ``now``) are stubbed with
:func:`unittest.mock.patch` so the workflow body is exercised as plain
Python - the mirroring approach already used by
``test_agent_runner_code_change.py``.

"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from temporalio import workflow as _temporal_workflow

# ---------------------------------------------------------------------------
# sys.path bootstrap - mirrors ``test_agent_runner_code_change.py``.
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

# noqa: E402 below - import after sys.path bootstrap.

from agent_runner.workflows.agent_runner_workflow import (  # noqa: E402
    CONFLUENCE_PROBE_PAGE_SKIPPED_AUDIT_ACTION,
    AgentRunnerWorkflow,
)
from temporal_shared.confluence_dedup import (  # noqa: E402
    AUDIT_CONFLUENCE_OVERWRITE_PROTECTED,
    AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP,
)
from temporal_shared.messages import (  # noqa: E402
    AgentRunnerWorkflowInput,
    LlmAnalysisResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_FIXED_NOW: datetime = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
_JIRA_LINK: str = "https://acme.atlassian.net/browse/PAY-4211"
_PROVENANCE_NEEDLE: str = (
    "🤖 Bu sayfa AI asistanı yardımıyla yazılmıştır. Kaynak:"
)


@pytest.fixture
def fixed_now() -> datetime:
    """Deterministic anchor for ``workflow.now`` stubs."""

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
    rationale: str = "Bu sayfa için detaylı bir araştırma yapıldı.",
    target_space: str | None = "DOCS",
    target_page_id: str | None = None,
    default_language: str = "tr",
) -> AgentRunnerWorkflowInput:
    """Build a minimal :class:`AgentRunnerWorkflowInput` fixture.

    Mirrors the helper from ``test_agent_runner_code_change.py`` but
    populates the Confluence-relevant fields on the embedded
    :class:`LlmAnalysisResult` (``target_space`` / ``target_page_id``)
    so the ``confluence_doc_*`` body methods have the data they need
    without a fragile rationale-encoded fallback.
    """

    analysis = LlmAnalysisResult(
        workflow_type=workflow_type,
        confidence="high",
        target_space=target_space,
        target_page_id=target_page_id,
        title=title,
        rationale=rationale,
        token_usage=120,
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id="automation-jira-PAY-4211",
        issue_key="PAY-4211",
        department_id="payments",
        workflow_type=workflow_type,
        analysis=analysis,
        target_repo=None,
        target_branch=None,
        iteration=1,
        max_iter=5,
        default_language=default_language,
    )


@pytest.fixture
def make_wf():
    """Factory returning a fresh :class:`AgentRunnerWorkflow`.

    Seeds ``iter_count=1`` so the body methods operate on a non-zero
    iteration counter (the body is normally invoked after
    :meth:`AgentRunnerWorkflow.run` has advanced through its first
    ``_advance_iter_with_banner_check`` call).
    """

    from dataclasses import replace

    def _build() -> AgentRunnerWorkflow:
        wf = AgentRunnerWorkflow()
        wf._iteration_state = replace(wf._iteration_state, iter_count=1)
        return wf

    return _build


def _activity_dispatcher(routes: dict[str, Any]) -> AsyncMock:
    """Return an ``AsyncMock`` that resolves ``execute_activity`` calls.

    *routes* maps activity-name → return value (or 0-arg callable that
    yields the return value). Activities not present in *routes*
    return ``None`` so optional best-effort calls (audit_emit,
    jira_add_comment) never blow up the test fixtures.
    """

    async def _fake_execute_activity(*args, **kwargs):
        name = args[0] if args else kwargs.get("activity")
        if name in routes:
            value = routes[name]
            if callable(value):
                return value()
            return value
        return None

    return AsyncMock(side_effect=_fake_execute_activity)


def _patch_workflow_runtime(
    activity_mock: AsyncMock,
    workflow_id: str = "automation-jira-PAY-4211",
):
    """Return the standard ``patch.object`` triple for the workflow body.

    Stubs out the three Temporal primitives the body relies on:

    * ``workflow.execute_activity`` - replaced with *activity_mock*.
    * ``workflow.info`` - returns a tiny stub carrying ``workflow_id``
      so :func:`should_skip_section_update` and the audit emit see a
      stable id.
    * ``workflow.execute_child_workflow`` - replaced with a no-op
      ``AsyncMock`` so a body that accidentally tries to start a
      child fails the test loudly via the mock's call assertion.
    """

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


def _drive(coro_factory) -> None:
    """Run an async coroutine to completion under a fresh event loop."""

    asyncio.run(coro_factory())


def _activity_args(call) -> list[Any]:
    """Pull the ``args`` keyword from a recorded ``execute_activity`` call.

    The workflow always passes activity arguments via the ``args=[...]``
    keyword so Temporal's data converter sees a homogeneous list.
    Falls back to the second positional argument when the body has
    been called without the keyword (mostly belt-and-braces - the
    workflow as written never does this).
    """

    if "args" in call.kwargs:
        return list(call.kwargs["args"])
    if len(call.args) >= 2:
        return list(call.args[1])
    return []


def _activity_calls_for(activity_mock: AsyncMock, name: str) -> list[Any]:
    """Filter the recorded calls for a specific activity name."""

    return [c for c in activity_mock.call_args_list if c.args[0] == name]


# ---------------------------------------------------------------------------
# 1. ``confluence_doc_create`` happy path
# ---------------------------------------------------------------------------


class TestConfluenceDocCreate:
    """Happy path through the create body."""

    def test_happy_path_invokes_full_activity_sequence(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(workflow_type="confluence_doc_create")

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "jira_build_issue_link": _JIRA_LINK,
            "llm_generate_doc": {
                "body": "# KVKK\n\nİlgili madde özetleri burada yer alır."
            },
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
                    {"workflow_id": "automation-jira-PAY-4211"},
                )(),
            ):
                await wf._handle_confluence_doc_create(inp)

        _drive(_run)

        # Activity ordering check.
        called_names = [c.args[0] for c in activity_mock.call_args_list]
        for required in (
            "set_assignee_to_bot",
            "jira_build_issue_link",
            "llm_generate_doc",
            "confluence_create_page",
            "jira_add_comment",
        ):
            assert required in called_names, (
                f"expected {required!r} in activity sequence, got {called_names}"
            )

        # Sequence invariants - assignee MUST come first; the issue
        # link must be resolved before the create call so the
        # provenance footer can be appended; jira completion comment
        # comes last.
        idx_assignee = called_names.index("set_assignee_to_bot")
        idx_link = called_names.index("jira_build_issue_link")
        idx_llm = called_names.index("llm_generate_doc")
        idx_create = called_names.index("confluence_create_page")
        idx_comment = called_names.index("jira_add_comment")
        assert idx_assignee < idx_create
        assert idx_link < idx_create
        assert idx_llm < idx_create
        assert idx_create < idx_comment

        # The Jira completion comment carries the page link.
        comment_calls = _activity_calls_for(activity_mock, "jira_add_comment")
        assert len(comment_calls) == 1
        comment_args = _activity_args(comment_calls[0])
        # ``jira_add_comment`` signature: (issue_key, body, dept_id).
        comment_body = comment_args[1] if len(comment_args) >= 2 else ""
        assert "Confluence" in comment_body or "98765" in comment_body or (
            "https://acme.atlassian.net/wiki/spaces/DOCS/pages/98765"
            in comment_body
        )

    def test_create_page_body_carries_provenance_footer(
        self, make_wf, patched_workflow_now
    ) -> None:
        """The body passed to ``confluence_create_page`` MUST embed the
        provenance footer so the rendered page surfaces the
        bot's authorship to readers."""

        wf = make_wf()
        inp = _make_input(workflow_type="confluence_doc_create")

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "jira_build_issue_link": _JIRA_LINK,
            "llm_generate_doc": {"body": "Sayfa gövdesi."},
            "confluence_create_page": {"id": "1", "url": "https://x/1"},
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
                await wf._handle_confluence_doc_create(inp)

        _drive(_run)

        # Pull the create call and verify the body passed in.
        create_calls = _activity_calls_for(
            activity_mock, "confluence_create_page"
        )
        assert len(create_calls) == 1
        # Signature: (space, title, body, dept_id).
        args = _activity_args(create_calls[0])
        assert len(args) >= 3, f"unexpected arg count: {args!r}"
        space, title, body = args[0], args[1], args[2]

        # Title format: "{topic} - {YYYY-MM-DD}".
        assert isinstance(title, str)
        assert "KVKK Yönetmelik Analizi" in title
        assert "2026-05-14" in title

        # Body must contain both the LLM output and the provenance
        # footer text + the Jira issue link.
        assert isinstance(body, str)
        assert "Sayfa gövdesi." in body
        assert _PROVENANCE_NEEDLE in body
        assert _JIRA_LINK in body

        # Space passes through from the analysis.
        assert space == "DOCS"

    def test_create_page_id_is_recorded_on_workflow_state(
        self, make_wf, patched_workflow_now
    ) -> None:
        """The created page id should be surfaced via
        :attr:`_latest_confluence_page_id` so the terminal output can
        include it."""

        wf = make_wf()
        inp = _make_input(workflow_type="confluence_doc_create")

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "jira_build_issue_link": _JIRA_LINK,
            "llm_generate_doc": {"body": "Body."},
            "confluence_create_page": {
                "id": "98765",
                "url": "https://x/98765",
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
                await wf._handle_confluence_doc_create(inp)

        _drive(_run)

        assert wf._latest_confluence_page_id == "98765"


# ---------------------------------------------------------------------------
# 2. ``confluence_doc_update`` overwrite-protection skip
# ---------------------------------------------------------------------------


class TestConfluenceDocUpdateOverwriteProtection:
    """Recent human edit blocks the update path."""

    def test_recent_human_edit_skips_update_with_audit(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(
            workflow_type="confluence_doc_update",
            target_page_id="42",
        )

        # Page was edited 2 minutes ago (within the 5-minute freshness
        # window) by a non-bot account; ``should_skip_overwrite`` must
        # therefore return ``skip=True``.
        recent_edit = _FIXED_NOW - timedelta(minutes=2)

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "confluence_get_page": {
                "title": "Quarterly Review",
                "last_editor_account_id": "human-acct-1",
                "last_edit_at": recent_edit.isoformat(),
                "sections": [
                    {
                        "section_path": "§1/Intro",
                        "content": "Mevcut içerik.",
                        "content_hash": "abc",
                    }
                ],
            },
            "jira_build_issue_link": _JIRA_LINK,
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
                await wf._handle_confluence_doc_update(inp)

        _drive(_run)

        called_names = [c.args[0] for c in activity_mock.call_args_list]

        # Critical invariant - no per-section update activity was
        # invoked while the overwrite guard fires.
        assert "confluence_update_page" not in called_names

        # ``confluence_overwrite_protected`` audit row was emitted.
        audit_calls = _activity_calls_for(activity_mock, "audit_emit")
        audit_actions = [
            (_activity_args(c)[0] or {}).get("action") for c in audit_calls
        ]
        assert AUDIT_CONFLUENCE_OVERWRITE_PROTECTED in audit_actions

        # A best-effort Jira comment was posted to surface the skip.
        assert "jira_add_comment" in called_names

    def test_stale_human_edit_proceeds_with_update(
        self, make_wf, patched_workflow_now
    ) -> None:
        """A human edit older than 5 minutes does NOT block - the
        update path proceeds normally."""

        wf = make_wf()
        inp = _make_input(
            workflow_type="confluence_doc_update",
            target_page_id="42",
        )

        stale_edit = _FIXED_NOW - timedelta(minutes=30)

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "confluence_get_page": {
                "title": "Quarterly Review",
                "last_editor_account_id": "human-acct-1",
                "last_edit_at": stale_edit.isoformat(),
                "sections": [
                    {
                        "section_path": "§1/Intro",
                        "content": "Yeni içerik.",
                        "content_hash": "fresh-hash",
                    }
                ],
            },
            "jira_build_issue_link": _JIRA_LINK,
            "confluence_update_page": None,
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
                await wf._handle_confluence_doc_update(inp)

        _drive(_run)

        called_names = [c.args[0] for c in activity_mock.call_args_list]
        assert "confluence_update_page" in called_names

        # The overwrite-protected audit must NOT fire for a stale edit.
        audit_calls = _activity_calls_for(activity_mock, "audit_emit")
        audit_actions = [
            (_activity_args(c)[0] or {}).get("action") for c in audit_calls
        ]
        assert AUDIT_CONFLUENCE_OVERWRITE_PROTECTED not in audit_actions


# ---------------------------------------------------------------------------
# 3. ``confluence_doc_update`` section-dedup
# ---------------------------------------------------------------------------


class TestConfluenceDocUpdateSectionDedup:
    """Identical content_hash skips the per-section write."""

    def test_second_run_with_same_hash_skips_update(
        self, make_wf, patched_workflow_now
    ) -> None:
        """Pre-load the workflow's section-hash set with the section's
        content hash and verify the body skips the update activity for
        that section while emitting the dedup audit row."""

        wf = make_wf()
        inp = _make_input(
            workflow_type="confluence_doc_update",
            target_page_id="42",
        )

        # Pre-warm the workflow's section-hash set - mimics the state
        # after a successful first iteration that wrote this exact
        # content.
        seen_hash = "content-hash-already-seen"
        wf._confluence_section_hashes.add(seen_hash)

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "confluence_get_page": {
                "title": "Project Plan",
                "last_editor_account_id": None,
                "last_edit_at": None,
                "sections": [
                    {
                        "section_path": "§Overview",
                        "content": "Aynı içerik.",
                        "content_hash": seen_hash,
                    }
                ],
            },
            "jira_build_issue_link": _JIRA_LINK,
            "confluence_update_page": None,
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
                await wf._handle_confluence_doc_update(inp)

        _drive(_run)

        called_names = [c.args[0] for c in activity_mock.call_args_list]

        # The update activity was NOT invoked - the section was skipped.
        assert "confluence_update_page" not in called_names

        # The dedup audit row was emitted.
        audit_calls = _activity_calls_for(activity_mock, "audit_emit")
        audit_actions = [
            (_activity_args(c)[0] or {}).get("action") for c in audit_calls
        ]
        assert AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP in audit_actions

        # The Jira summary comment still fires (best-effort).
        assert "jira_add_comment" in called_names

    def test_new_hash_proceeds_and_records_in_state(
        self, make_wf, patched_workflow_now
    ) -> None:
        """A fresh content_hash should result in an actual update call
        and the hash should be added to ``_confluence_section_hashes``
        so a subsequent iteration with the same hash dedups."""

        wf = make_wf()
        inp = _make_input(
            workflow_type="confluence_doc_update",
            target_page_id="42",
        )

        new_hash = "brand-new-hash"

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "confluence_get_page": {
                "title": "Project Plan",
                "last_editor_account_id": None,
                "last_edit_at": None,
                "sections": [
                    {
                        "section_path": "§Overview",
                        "content": "Yeni içerik.",
                        "content_hash": new_hash,
                    }
                ],
            },
            "jira_build_issue_link": _JIRA_LINK,
            "confluence_update_page": None,
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
                await wf._handle_confluence_doc_update(inp)

        _drive(_run)

        called_names = [c.args[0] for c in activity_mock.call_args_list]
        assert "confluence_update_page" in called_names

        # The new hash was recorded so the next iteration with the
        # same hash hits the dedup branch.
        assert new_hash in wf._confluence_section_hashes


# ---------------------------------------------------------------------------
# 4. ``_AI_PROBE_*`` page skip
# ---------------------------------------------------------------------------


class TestConfluenceDocUpdateProbeSkip:
    """Probe-titled pages must NOT be overwritten."""

    def test_probe_title_short_circuits_update(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(
            workflow_type="confluence_doc_update",
            target_page_id="probe-page-1",
        )

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "confluence_get_page": {
                # Canonical probe sentinel format.
                "title": "_AI_PROBE_1700000000_DELETE_ME",
                "last_editor_account_id": "bot-acct",
                "last_edit_at": _FIXED_NOW.isoformat(),
                "sections": [
                    {
                        "section_path": "§Anything",
                        "content": "irrelevant",
                        "content_hash": "h",
                    }
                ],
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
                await wf._handle_confluence_doc_update(inp)

        _drive(_run)

        called_names = [c.args[0] for c in activity_mock.call_args_list]

        # No update activity should ever fire on a probe page.
        assert "confluence_update_page" not in called_names

        # The probe-skip audit row is emitted, and the per-section
        # dedup audit / overwrite-protected audit are NOT (the body
        # short-circuits before those branches).
        audit_calls = _activity_calls_for(activity_mock, "audit_emit")
        audit_actions = [
            (_activity_args(c)[0] or {}).get("action") for c in audit_calls
        ]
        assert CONFLUENCE_PROBE_PAGE_SKIPPED_AUDIT_ACTION in audit_actions
        assert AUDIT_CONFLUENCE_SECTION_DEDUP_SKIP not in audit_actions
        assert AUDIT_CONFLUENCE_OVERWRITE_PROTECTED not in audit_actions


# ---------------------------------------------------------------------------
# 5. Provenance footer present in update body
# ---------------------------------------------------------------------------


class TestProvenanceFooterInUpdateBody:
    """The provenance footer must be appended to every section body
    written via ``confluence_update_page``."""

    def test_update_section_body_contains_footer(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(
            workflow_type="confluence_doc_update",
            target_page_id="42",
        )

        captured_bodies: list[str] = []

        async def _capture_update(*args, **kwargs):
            name = args[0] if args else kwargs.get("activity")
            if name == "confluence_update_page":
                params = (
                    list(kwargs.get("args") or [])
                    if kwargs.get("args") is not None
                    else list(args[1] if len(args) >= 2 else [])
                )
                # Signature: (page_id, section_path, body, dept_id).
                if len(params) >= 3:
                    captured_bodies.append(str(params[2]))
                return None
            if name == "set_assignee_to_bot":
                return None
            if name == "confluence_get_page":
                return {
                    "title": "Project Plan",
                    "last_editor_account_id": None,
                    "last_edit_at": None,
                    "sections": [
                        {
                            "section_path": "§Section A",
                            "content": "Section A content",
                            "content_hash": "hash-a",
                        },
                        {
                            "section_path": "§Section B",
                            "content": "Section B content",
                            "content_hash": "hash-b",
                        },
                    ],
                }
            if name == "jira_build_issue_link":
                return _JIRA_LINK
            return None

        activity_mock = AsyncMock(side_effect=_capture_update)

        async def _run() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type("WfInfo", (), {"workflow_id": "x"})(),
            ):
                await wf._handle_confluence_doc_update(inp)

        _drive(_run)

        # Both sections were written.
        assert len(captured_bodies) == 2

        # Each captured body contains the original section content
        # AND the provenance footer with the issue link.
        assert any(
            "Section A content" in b and _PROVENANCE_NEEDLE in b
            for b in captured_bodies
        ), f"Section A body missing footer: {captured_bodies!r}"
        assert any(
            "Section B content" in b and _PROVENANCE_NEEDLE in b
            for b in captured_bodies
        ), f"Section B body missing footer: {captured_bodies!r}"
        # The Jira issue link is embedded in the footer.
        assert all(_JIRA_LINK in b for b in captured_bodies)
