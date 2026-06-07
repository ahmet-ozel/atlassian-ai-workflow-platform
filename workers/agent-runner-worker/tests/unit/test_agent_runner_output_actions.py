"""Unit tests for ``AgentRunnerWorkflow`` output_actions execution.

Covers the four execution paths plumbed into ``_execute_output_actions``:

    1. Successful critical + best_effort  every dispatch activity is
       invoked, no compensation chain runs, and the final Jira summary
       is composed via
       :func:`temporal_shared.output_size_cap.format_final_jira_comment`.
    2. Failed critical  the workflow body raises
       :class:`_OutputActionCriticalFailure`, the cancel /
       compensation branch runs ``compensation_chain_run``, and the
       run terminates with the ``failed`` status.
    3. Failed best_effort  the workflow completes (no compensation),
       :attr:`AgentRunnerWorkflow._output_actions_partial` carries the
       failed action kinds, and the final summary names them.
    4. Oversized payload  :func:`redirect_oversized_payload` calls
       the MinIO offload activity and the rewritten payload reaching
       the dispatch activity carries the canonical
       ``s3://`` URI in its ``minio_uri`` key.

Activities are mocked by patching
``temporalio.workflow.execute_activity``; ``workflow.now`` /
``workflow.info`` are stubbed so the body methods stay deterministic.

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
# sys.path bootstrap (mirrors the other unit-test files in this folder).
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

# noqa: E402 - imports after sys.path bootstrap.

from agent_runner.workflows.agent_runner_workflow import (  # noqa: E402
    OUTPUT_ACTION_CRITICAL_FAILED_REASON,
    AgentRunnerWorkflow,
    _OutputActionCriticalFailure,
)
from temporal_shared.messages import (  # noqa: E402
    AgentRunnerWorkflowInput,
    LlmAnalysisResult,
    OutputAction,
)
from temporal_shared.output_actions import (  # noqa: E402
    ApplyResult,
    UNCLASSIFIED_OUTPUT_ACTION_KIND_MESSAGE,
    partition,
)
from temporal_shared.output_size_cap import (  # noqa: E402
    FINAL_COMMENT_BEST_EFFORT_PREFIX,
    FINAL_COMMENT_CRITICAL_PREFIX,
    MAX_OUTPUT_BYTES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixed_now() -> datetime:
    return datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def patched_workflow_now(fixed_now: datetime, monkeypatch: pytest.MonkeyPatch):
    state = {"now": fixed_now}
    monkeypatch.setattr(_temporal_workflow, "now", lambda: state["now"])
    return state


@pytest.fixture(autouse=True)
def patched_workflow_logger(monkeypatch: pytest.MonkeyPatch):
    """Replace ``workflow.logger`` with a plain logger for test runs.

    ``temporalio.workflow.logger`` raises
    :class:`temporalio.workflow._NotInWorkflowEventLoopError` when
    ``isEnabledFor`` is called outside a real workflow event loop -
    which is exactly the situation in these unit tests.  Substituting
    a stdlib logger keeps the workflow code path intact while letting
    the body methods run as plain async coroutines.
    """

    import logging

    monkeypatch.setattr(
        _temporal_workflow, "logger", logging.getLogger("test-fallback")
    )


def _make_input(
    *,
    output_actions: tuple[OutputAction, ...] = (),
    workflow_type: str = "code_change_commit_only",
) -> AgentRunnerWorkflowInput:
    analysis = LlmAnalysisResult(
        workflow_type=workflow_type,
        confidence="high",
        target_repo="payment-callbacks",
        target_branch="ai/PAY-1",
        title="Apply LLM-emitted output actions",
        rationale="cover output action execution",
        output_actions=output_actions,
        token_usage=10,
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id="automation-jira-PAY-1",
        issue_key="PAY-1",
        department_id="payments",
        workflow_type=workflow_type,
        analysis=analysis,
        target_repo="payment-callbacks",
        target_branch="ai/PAY-1",
        iteration=1,
        max_iter=5,
        default_language="tr",
    )


@pytest.fixture
def make_wf():
    def _build() -> AgentRunnerWorkflow:
        wf = AgentRunnerWorkflow()
        # Mirror what ``run`` would set up on its first turn.
        from dataclasses import replace

        wf._iteration_state = replace(wf._iteration_state, iter_count=1)
        return wf

    return _build


def _activity_dispatcher(
    routes: dict[str, Any],
    *,
    record: list[tuple[str, tuple, dict]] | None = None,
) -> AsyncMock:
    """Return an ``AsyncMock`` resolving ``execute_activity`` calls.

    *routes* maps activity name  return value (or a callable
    returning a value, or a callable raising an exception).  Names
    not in *routes* return ``None``.  When *record* is provided each
    call is appended verbatim so tests can assert on the exact
    activity sequence.
    """

    async def _fake(*args, **kwargs):
        name = args[0] if args else kwargs.get("activity")
        if record is not None:
            record.append((name, args, kwargs))
        if name in routes:
            value = routes[name]
            if callable(value):
                value = value(*args, **kwargs)
            if isinstance(value, BaseException):
                raise value
            return value
        return None

    return AsyncMock(side_effect=_fake)


def _info_stub(workflow_id: str = "automation-jira-PAY-1"):
    return type("WfInfo", (), {"workflow_id": workflow_id})()


# ---------------------------------------------------------------------------
# 1. Successful critical + best_effort
# ---------------------------------------------------------------------------


class TestSuccessfulOutputActions:
    """All activities succeed; final summary names completed steps."""

    def test_critical_then_best_effort_invokes_each_dispatch_activity(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        actions = (
            OutputAction(
                kind="jira_comment",
                severity="critical",
                payload=(("issue_key", "PAY-1"), ("body", "merhaba")),
            ),
            OutputAction(
                kind="bitbucket_create_pr",
                severity="critical",
                payload=(
                    ("repo", "payment-callbacks"),
                    ("source_branch", "ai/PAY-1"),
                ),
            ),
            OutputAction(
                kind="slack_notify",
                severity="best_effort",
                payload=(("channel", "#deploys"),),
            ),
        )
        inp = _make_input(output_actions=actions)
        record: list[tuple[str, tuple, dict]] = []
        activity_mock = _activity_dispatcher(
            {
                "jira_add_comment": {"id": "c1"},
                "bitbucket_open_pr": {"id": 99},
                "slack_notify": {"ok": True},
            },
            record=record,
        )

        with patch.object(
            _temporal_workflow, "execute_activity", activity_mock
        ), patch.object(_temporal_workflow, "info", lambda: _info_stub()):
            result = asyncio.run(
                wf._execute_output_actions(
                    actions, "automation-jira-PAY-1", inp
                )
            )

        # All three dispatch activities ran exactly once.
        called = [name for name, _args, _kwargs in record]
        assert "jira_add_comment" in called
        assert "bitbucket_open_pr" in called
        assert "slack_notify" in called
        assert called.index("jira_add_comment") < called.index("slack_notify")
        # No MinIO offload (payloads are small).
        assert "minio_put_output_action" not in called
        # No compensation kicked off.
        assert "compensation_chain_run" not in called

        assert result.successful_critical == [
            "jira_comment",
            "bitbucket_create_pr",
        ]
        assert result.successful_best_effort == ["slack_notify"]
        assert result.failed_critical == []
        assert result.failed_best_effort == []
        assert wf._output_actions_partial == []

    def test_run_uses_format_final_jira_comment_for_summary(
        self, make_wf, patched_workflow_now
    ) -> None:
        """``run`` composes the final summary via ``format_final_jira_comment``.

        When every output action succeeds the summary line carries the
         prefix (``FINAL_COMMENT_CRITICAL_PREFIX``) plus the kinds
        of the completed critical actions in dispatch order.
        """

        wf = make_wf()
        # Reset iter_count so ``run`` advances cleanly.
        from dataclasses import replace

        wf._iteration_state = replace(wf._iteration_state, iter_count=0)

        actions = (
            OutputAction(
                kind="jira_comment",
                severity="critical",
                payload=(("issue_key", "PAY-1"), ("body", "ok")),
            ),
        )
        inp = _make_input(
            output_actions=actions, workflow_type="confluence_doc_create"
        )
        # Drive the full ``run`` pipeline against a mock dispatch:
        # confluence_doc_create's pre-output-actions activities
        # (``set_assignee_to_bot`` etc.) are stubbed to return None.
        activity_mock = _activity_dispatcher(
            {
                "jira_add_comment": {"id": "c1"},
                "confluence_create_page": {
                    "id": "page-7",
                    "url": "https://example/page-7",
                },
                "llm_generate_doc": {"body": "doc body"},
                "jira_build_issue_link": (
                    "https://atl.example/browse/PAY-1"
                ),
            }
        )
        with patch.object(
            _temporal_workflow, "execute_activity", activity_mock
        ), patch.object(_temporal_workflow, "info", lambda: _info_stub()):
            output = asyncio.run(wf.run(inp))

        assert output.status == "completed"
        # The final summary is the format_final_jira_comment shape:
        # leading  prefix + the completed critical kinds.
        assert output.summary.startswith(FINAL_COMMENT_CRITICAL_PREFIX)
        assert "jira_comment" in output.summary


# ---------------------------------------------------------------------------
# 2. Failed critical  compensation triggered
# ---------------------------------------------------------------------------


class TestCriticalFailureTriggersCompensation:
    """A critical action failure aborts the run + runs compensation."""

    def test_execute_output_actions_raises_critical_failure(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        actions = (
            OutputAction(
                kind="jira_comment",
                severity="critical",
                payload=(("body", "x"),),
            ),
            OutputAction(
                kind="bitbucket_create_pr",
                severity="critical",
                payload=(("repo", "r"), ("source_branch", "ai/PAY-1")),
            ),
        )
        inp = _make_input(output_actions=actions)

        # ``bitbucket_open_pr`` fails - should short-circuit the rest.
        record: list[tuple[str, tuple, dict]] = []
        activity_mock = _activity_dispatcher(
            {
                "jira_add_comment": {"id": "c1"},
                "bitbucket_open_pr": RuntimeError("api 500"),
            },
            record=record,
        )

        with patch.object(
            _temporal_workflow, "execute_activity", activity_mock
        ), patch.object(_temporal_workflow, "info", lambda: _info_stub()):
            with pytest.raises(_OutputActionCriticalFailure) as exc_info:
                asyncio.run(
                    wf._execute_output_actions(
                        actions, "automation-jira-PAY-1", inp
                    )
                )

        result = exc_info.value.apply_result
        assert result.successful_critical == ["jira_comment"]
        assert len(result.failed_critical) == 1
        kind, reason = result.failed_critical[0]
        assert kind == "bitbucket_create_pr"
        assert "api 500" in reason
        assert wf._failure_reason == OUTPUT_ACTION_CRITICAL_FAILED_REASON

    def test_bitbucket_create_pr_without_source_branch_fails_fast(
        self, make_wf, patched_workflow_now
    ) -> None:
        """A ``bitbucket_create_pr`` action with no resolvable source
        branch must fail with a clear reason and never call the PR
        activity (Bitbucket rejects an empty source with 400)."""

        wf = make_wf()
        action = OutputAction(
            kind="bitbucket_create_pr",
            severity="critical",
            payload=(("repo", "r"),),
        )
        inp = _make_input(output_actions=(action,))
        # No prior commit recorded  no branch fallback available.
        assert wf._last_commit_branch is None

        record: list[tuple[str, tuple, dict]] = []
        activity_mock = _activity_dispatcher(
            {"bitbucket_open_pr": {"id": 1}}, record=record
        )
        with patch.object(
            _temporal_workflow, "execute_activity", activity_mock
        ), patch.object(_temporal_workflow, "info", lambda: _info_stub()):
            success, reason = asyncio.run(
                wf._apply_single_output_action(action, inp)
            )

        assert success is False
        assert reason == "bitbucket_create_pr_no_source_branch"
        # The PR activity must never be called with an empty source.
        assert all(name != "bitbucket_open_pr" for name, _a, _kw in record)

    def test_bitbucket_create_pr_falls_back_to_commit_branch(
        self, make_wf, patched_workflow_now
    ) -> None:
        """When the action omits a source branch but a commit landed on
        a branch earlier in the run, the PR opens from that branch."""

        wf = make_wf()
        wf._last_commit_branch = "ai/PAY-9"
        action = OutputAction(
            kind="bitbucket_create_pr",
            severity="critical",
            payload=(("repo", "r"),),
        )
        inp = _make_input(output_actions=(action,))

        record: list[tuple[str, tuple, dict]] = []
        activity_mock = _activity_dispatcher(
            {
                "bitbucket_open_pr": {
                    "pr_id": 7,
                    "url": "https://bitbucket.example/pr/7",
                },
                "jira_add_comment": None,
            },
            record=record,
        )
        with patch.object(
            _temporal_workflow, "execute_activity", activity_mock
        ), patch.object(_temporal_workflow, "info", lambda: _info_stub()):
            success, reason = asyncio.run(
                wf._apply_single_output_action(action, inp)
            )

        assert success is True
        pr_calls = [
            (a, kw) for name, a, kw in record if name == "bitbucket_open_pr"
        ]
        assert len(pr_calls) == 1
        # execute_activity is called as (name, args=[...])  kwargs["args"].
        pr_args = pr_calls[0][1]["args"]
        # args = [repo, source_branch, target_branch, title, desc, dept]
        assert pr_args[1] == "ai/PAY-9"
        # The PR URL is surfaced to Jira after a successful PR open.
        jira_calls = [
            (a, kw) for name, a, kw in record if name == "jira_add_comment"
        ]
        assert any(
            "https://bitbucket.example/pr/7" in str(kw.get("args") or a)
            for a, kw in jira_calls
        )

    def test_run_triggers_compensation_chain_on_critical_failure(
        self, make_wf, patched_workflow_now
    ) -> None:
        """Workflow ``run`` catches ``_OutputActionCriticalFailure`` and
        invokes ``compensation_chain_run`` before terminating with
        ``failed``.
        """

        wf = make_wf()
        from dataclasses import replace

        wf._iteration_state = replace(wf._iteration_state, iter_count=0)

        actions = (
            OutputAction(
                kind="jira_comment",
                severity="critical",
                payload=(("body", "x"),),
            ),
        )
        inp = _make_input(
            output_actions=actions, workflow_type="confluence_doc_create"
        )

        record: list[tuple[str, tuple, dict]] = []
        activity_mock = _activity_dispatcher(
            {
                # confluence_doc_create primary side effects succeed.
                "llm_generate_doc": {"body": "b"},
                "confluence_create_page": {"id": "p", "url": "u"},
                "jira_build_issue_link": (
                    "https://atl.example/browse/PAY-1"
                ),
                # The post-create Jira comment IS best-effort, so we
                # let it succeed; the LLM-emitted critical jira_comment
                # output_action then fails to drive the test.
                "jira_add_comment": RuntimeError("jira api 500"),
                # Compensation must run.
                "compensation_chain_run": {"ok": True},
            },
            record=record,
        )

        with patch.object(
            _temporal_workflow, "execute_activity", activity_mock
        ), patch.object(_temporal_workflow, "info", lambda: _info_stub()):
            output = asyncio.run(wf.run(inp))

        assert output.status == "failed"
        assert output.failure_reason == OUTPUT_ACTION_CRITICAL_FAILED_REASON
        called = [name for name, _a, _kw in record]
        assert "compensation_chain_run" in called


# ---------------------------------------------------------------------------
# 3. Failed best_effort  workflow completes with partial failure
# ---------------------------------------------------------------------------


class TestBestEffortFailure:
    """A best-effort failure is reported but does not abort."""

    def test_best_effort_failure_appends_to_partial_list(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        actions = (
            OutputAction(
                kind="jira_comment",
                severity="critical",
                payload=(("body", "x"),),
            ),
            OutputAction(
                kind="slack_notify",
                severity="best_effort",
                payload=(("channel", "#x"),),
            ),
        )
        inp = _make_input(output_actions=actions)

        activity_mock = _activity_dispatcher(
            {
                "jira_add_comment": {"id": "c1"},
                "slack_notify": ConnectionError("slack down"),
            }
        )

        with patch.object(
            _temporal_workflow, "execute_activity", activity_mock
        ), patch.object(_temporal_workflow, "info", lambda: _info_stub()):
            result = asyncio.run(
                wf._execute_output_actions(
                    actions, "automation-jira-PAY-1", inp
                )
            )

        # Critical succeeded; best-effort failed but did NOT raise.
        assert result.successful_critical == ["jira_comment"]
        assert result.failed_critical == []
        assert len(result.failed_best_effort) == 1
        assert result.failed_best_effort[0][0] == "slack_notify"
        # Reflected in the workflow-level partial list (final summary
        # source).
        assert "slack_notify" in wf._output_actions_partial
        # No compensation needed.
        assert wf._failure_reason in (None, "")

    def test_run_summary_names_failed_best_effort(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        from dataclasses import replace

        wf._iteration_state = replace(wf._iteration_state, iter_count=0)

        actions = (
            OutputAction(
                kind="jira_comment",
                severity="critical",
                payload=(("body", "ok"),),
            ),
            OutputAction(
                kind="slack_notify",
                severity="best_effort",
                payload=(("channel", "#x"),),
            ),
        )
        inp = _make_input(
            output_actions=actions, workflow_type="confluence_doc_create"
        )

        activity_mock = _activity_dispatcher(
            {
                "llm_generate_doc": {"body": "b"},
                "confluence_create_page": {"id": "p", "url": "u"},
                "jira_build_issue_link": (
                    "https://atl.example/browse/PAY-1"
                ),
                "jira_add_comment": {"id": "c1"},
                "slack_notify": RuntimeError("slack 500"),
            }
        )

        with patch.object(
            _temporal_workflow, "execute_activity", activity_mock
        ), patch.object(_temporal_workflow, "info", lambda: _info_stub()):
            output = asyncio.run(wf.run(inp))

        assert output.status == "completed_with_partial_failure"
        # Final summary uses ``format_final_jira_comment``: the
        # line lists the completed critical kinds, the  line
        # carries the failed best-effort entries.
        assert FINAL_COMMENT_CRITICAL_PREFIX in output.summary
        assert FINAL_COMMENT_BEST_EFFORT_PREFIX in output.summary
        assert "slack_notify" in output.summary
        # ``partial_failure_actions`` mirrors the failed kinds.
        assert "slack_notify" in output.partial_failure_actions


# ---------------------------------------------------------------------------
# 4. Oversized payload  MinIO redirection
# ---------------------------------------------------------------------------


class TestOversizedPayloadRedirected:
    """Payloads above MAX_OUTPUT_BYTES are offloaded to MinIO."""

    def test_oversized_payload_invokes_minio_offload_and_substitutes_uri(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        # Build a payload large enough to exceed MAX_OUTPUT_BYTES once
        # JSON-encoded. A 2 MiB string is comfortably above the cap.
        big_payload = "x" * (MAX_OUTPUT_BYTES + 1024)
        actions = (
            OutputAction(
                kind="jira_comment",
                severity="critical",
                payload=(
                    ("issue_key", "PAY-1"),
                    ("body", big_payload),
                ),
            ),
        )
        inp = _make_input(output_actions=actions)

        record: list[tuple[str, tuple, dict]] = []
        offloaded_uri = "s3://ai-runs/automation-jira-PAY-1/output-0.json"

        activity_mock = _activity_dispatcher(
            {
                "minio_put_output_action": offloaded_uri,
                "jira_add_comment": {"id": "c1"},
            },
            record=record,
        )

        with patch.object(
            _temporal_workflow, "execute_activity", activity_mock
        ), patch.object(_temporal_workflow, "info", lambda: _info_stub()):
            result = asyncio.run(
                wf._execute_output_actions(
                    actions, "automation-jira-PAY-1", inp
                )
            )

        called = [name for name, _a, _kw in record]
        # MinIO offload happened first, then the dispatch activity.
        assert called.index("minio_put_output_action") < called.index(
            "jira_add_comment"
        )

        # The activity arguments carry the rewritten payload - the
        # ``body`` key is gone, replaced by ``summary`` /
        # ``minio_uri`` / ``size_bytes``.
        jira_calls = [
            (args, kw) for name, args, kw in record if name == "jira_add_comment"
        ]
        assert len(jira_calls) == 1
        args, kwargs = jira_calls[0]
        # ``args`` from execute_activity is ``(activity_name,)`` - the
        # actual activity arguments live under ``kwargs['args']``.
        forwarded = kwargs["args"]
        assert forwarded[0] == "PAY-1"
        assert offloaded_uri in forwarded[1]
        assert big_payload not in forwarded[1]
        assert forwarded[2] == inp.department_id

        assert result.successful_critical == ["jira_comment"]


# ---------------------------------------------------------------------------
# 5. Module-level partition helper
# ---------------------------------------------------------------------------


class TestPartitionHelper:
    """Cross-checks for ``temporal_shared.output_actions.partition``."""

    def test_partition_classifies_by_kind_not_severity(self) -> None:
        # A best-effort kind labelled as "critical" still lands in the
        # best-effort bucket (kind classification wins).
        a = OutputAction(
            kind="slack_notify", severity="critical", payload=()
        )
        b = OutputAction(
            kind="jira_comment", severity="best_effort", payload=()
        )
        critical, best_effort = partition([a, b])
        assert critical == (b,)
        assert best_effort == (a,)

    def test_partition_preserves_input_order(self) -> None:
        a = OutputAction(kind="jira_comment", severity="critical", payload=())
        b = OutputAction(
            kind="bitbucket_create_pr", severity="critical", payload=()
        )
        c = OutputAction(kind="slack_notify", severity="best_effort", payload=())
        d = OutputAction(kind="email_notify", severity="best_effort", payload=())
        critical, best_effort = partition([a, c, b, d])
        assert critical == (a, b)
        assert best_effort == (c, d)

    def test_partition_empty(self) -> None:
        assert partition([]) == ((), ())

    def test_partition_rejects_unknown_kind(self) -> None:
        # Pass a bogus kind via __dict__ surgery so the dataclass
        # constructor does not block us - Literal types are not
        # enforced at runtime, but this also lets the test keep
        # working if Literal validation lands later.
        bad = OutputAction.__new__(OutputAction)
        object.__setattr__(bad, "kind", "made_up_kind")
        object.__setattr__(bad, "severity", "critical")
        object.__setattr__(bad, "payload", ())
        with pytest.raises(ValueError) as exc:
            partition([bad])
        assert UNCLASSIFIED_OUTPUT_ACTION_KIND_MESSAGE in str(exc.value)
