"""Unit tests for the ``output_actions`` activity (task 4.1).

Validates Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8,
                        3.9, 3.10, 3.11

Strategy
--------

The activity depends on an MCP caller that makes HTTP calls to the
MCP Server. We replace it with an in-memory fake registered through
the module-level ``set_mcp_caller`` setter. The activity runs as a
plain coroutine — ``@activity.defn`` does not change the calling
contract for direct invocation.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
_PLATFORM_ROOT: Path = _WORKER_ROOT.parents[1]
_DB_SHARED_SRC: Path = _PLATFORM_ROOT / "libs" / "db-shared" / "src"

for _candidate in (_SRC_DIR, _DB_SHARED_SRC):
    _str = str(_candidate)
    if _candidate.is_dir() and _str not in sys.path:
        sys.path.insert(0, _str)

from automation_worker.activities.output_actions import (  # noqa: E402
    ACTION_TIMEOUT_SECONDS,
    ActionResult,
    ExecutionBatchInput,
    ExecutionBatchResult,
    MAX_ACTIONS_PER_BATCH,
    OutputAction,
    execute_output_actions,
    set_mcp_caller,
)
from db_shared.enums import ActionType  # noqa: E402


# ---------------------------------------------------------------------------
# In-memory fake MCP caller
# ---------------------------------------------------------------------------


@dataclass
class _FakeMCPCaller:
    """Records MCP tool calls and returns scripted responses."""

    calls: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)
    responses: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)
    delays: dict[str, float] = field(default_factory=dict)

    async def call_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        dept_id: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        self.calls.append((tool_name, params, dept_id))

        # Simulate delay (for timeout testing)
        if tool_name in self.delays:
            await asyncio.sleep(self.delays[tool_name])

        # Simulate error
        if tool_name in self.errors:
            raise self.errors[tool_name]

        # Return scripted response
        return self.responses.get(tool_name, {"ok": True})


@pytest.fixture
def fake_caller() -> _FakeMCPCaller:
    caller = _FakeMCPCaller()
    set_mcp_caller(caller)
    return caller


def _make_batch_input(
    actions: list[OutputAction] | None = None,
    issue_key: str = "PAY-123",
    dept_id: str = "payments",
    workflow_id: str = "wf-test-001",
) -> ExecutionBatchInput:
    return ExecutionBatchInput(
        actions=actions or [],
        issue_key=issue_key,
        dept_id=dept_id,
        workflow_id=workflow_id,
    )


# ---------------------------------------------------------------------------
# Tests: Empty/null actions list (Requirement 3.10)
# ---------------------------------------------------------------------------


class TestEmptyActionsList:
    """Empty or null action list completes successfully."""

    def test_empty_list_returns_success(self, fake_caller: _FakeMCPCaller) -> None:
        """Requirement 3.10: empty list → successful completion."""
        inp = _make_batch_input(actions=[])
        result = asyncio.run(execute_output_actions(inp))

        assert result.all_succeeded is True
        assert result.results == []
        assert result.failed_actions == []
        assert fake_caller.calls == []


# ---------------------------------------------------------------------------
# Tests: Sequential execution order (Requirement 3.1)
# ---------------------------------------------------------------------------


class TestSequentialExecution:
    """Actions are executed in strict index order."""

    def test_actions_executed_in_index_order(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        """Requirement 3.1: actions executed in index order."""
        actions = [
            OutputAction(type=ActionType.JIRA_COMMENT, params={"body": "c2"}, index=2),
            OutputAction(type=ActionType.JIRA_COMMENT, params={"body": "c0"}, index=0),
            OutputAction(type=ActionType.JIRA_COMMENT, params={"body": "c1"}, index=1),
        ]
        inp = _make_batch_input(actions=actions)
        result = asyncio.run(execute_output_actions(inp))

        # All succeeded
        assert result.all_succeeded is True
        assert len(result.results) == 3

        # Verify execution order by checking call params
        assert fake_caller.calls[0][1]["body"] == "c0"
        assert fake_caller.calls[1][1]["body"] == "c1"
        assert fake_caller.calls[2][1]["body"] == "c2"

    def test_max_20_actions_enforced(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        """Requirement 3.1: max 20 actions per batch."""
        actions = [
            OutputAction(
                type=ActionType.JIRA_COMMENT,
                params={"body": f"comment-{i}"},
                index=i,
            )
            for i in range(25)
        ]
        inp = _make_batch_input(actions=actions)
        result = asyncio.run(execute_output_actions(inp))

        # Only first 20 actions executed (plus failure comment if any)
        action_calls = [
            c for c in fake_caller.calls if c[0] == "jira_add_comment"
        ]
        assert len(action_calls) == MAX_ACTIONS_PER_BATCH
        assert len(result.results) == MAX_ACTIONS_PER_BATCH


# ---------------------------------------------------------------------------
# Tests: Action handlers (Requirements 3.2-3.6)
# ---------------------------------------------------------------------------


class TestActionHandlers:
    """Each action type dispatches to the correct MCP tool."""

    def test_jira_comment_calls_jira_add_comment(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        """Requirement 3.2: jira_comment → jira_add_comment tool."""
        actions = [
            OutputAction(
                type=ActionType.JIRA_COMMENT,
                params={"issue_key": "PAY-1", "body": "hello"},
                index=0,
            )
        ]
        inp = _make_batch_input(actions=actions)
        asyncio.run(execute_output_actions(inp))

        assert fake_caller.calls[0][0] == "jira_add_comment"
        assert fake_caller.calls[0][1] == {"issue_key": "PAY-1", "body": "hello"}

    def test_jira_attachment_calls_jira_add_attachment(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        """Requirement 3.3: jira_attachment → jira_add_attachment tool."""
        actions = [
            OutputAction(
                type=ActionType.JIRA_ATTACHMENT,
                params={"issue_key": "PAY-1", "file_path": "/tmp/report.pdf"},
                index=0,
            )
        ]
        inp = _make_batch_input(actions=actions)
        asyncio.run(execute_output_actions(inp))

        assert fake_caller.calls[0][0] == "jira_add_attachment"

    def test_jira_attachment_with_minio_ref_uses_pipeline_activity(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        """Requirement 3.3: when ``bucket``/``key`` are present the
        attachment action is dispatched to the
        ``upload_artifact_to_jira`` agent-runner activity (MinIO →
        tempfile → Jira). The legacy ``jira_add_attachment`` MCP tool
        is **not** called for MinIO-sourced artifacts.
        """
        actions = [
            OutputAction(
                type=ActionType.JIRA_ATTACHMENT,
                params={
                    "issue_key": "PAY-1",
                    "bucket": "ai-runs",
                    "key": "artifacts/PAY-1/iter-1/report.md",
                    "file_name": "report.md",
                    "dept_id": "payments",
                },
                index=0,
            )
        ]
        inp = _make_batch_input(actions=actions)
        asyncio.run(execute_output_actions(inp))

        # Pipeline activity is invoked …
        assert fake_caller.calls[0][0] == "upload_artifact_to_jira"
        # … with the original params forwarded verbatim.
        assert fake_caller.calls[0][1]["bucket"] == "ai-runs"
        assert fake_caller.calls[0][1]["key"] == (
            "artifacts/PAY-1/iter-1/report.md"
        )
        assert "dept_id" not in fake_caller.calls[0][1]
        # The legacy MCP tool is NOT called for MinIO-sourced uploads.
        legacy_calls = [
            c for c in fake_caller.calls if c[0] == "jira_add_attachment"
        ]
        assert legacy_calls == []

    def test_bitbucket_pr_calls_bitbucket_create_pr(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        """Requirement 3.4: bitbucket_pr → bitbucket_create_pr tool."""
        actions = [
            OutputAction(
                type=ActionType.BITBUCKET_PR,
                params={"source": "feature/x", "target": "main"},
                index=0,
            )
        ]
        inp = _make_batch_input(actions=actions)
        asyncio.run(execute_output_actions(inp))

        assert fake_caller.calls[0][0] == "bitbucket_create_pr"
        assert fake_caller.calls[0][1]["from_branch"] == "feature/x"
        assert fake_caller.calls[0][1]["to_branch"] == "main"

    def test_non_jira_actions_drop_workflow_context_params(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        actions = [
            OutputAction(
                type=ActionType.CONFLUENCE_PAGE,
                params={
                    "issue_key": "PAY-1",
                    "dept_id": "payments",
                    "space_key": "DEV",
                    "title": "Result",
                    "content": "ok",
                },
                index=0,
            ),
            OutputAction(
                type=ActionType.BITBUCKET_COMMIT,
                params={
                    "issue_key": "PAY-1",
                    "dept_id": "payments",
                    "project_key": "PAY",
                    "repo_slug": "api",
                    "file_path": "out.md",
                    "content": "ok",
                    "message": "publish",
                    "branch": "ai/PAY-1",
                },
                index=1,
            ),
        ]
        inp = _make_batch_input(actions=actions)
        asyncio.run(execute_output_actions(inp))

        for _, params, _ in fake_caller.calls:
            assert "issue_key" not in params
            assert "dept_id" not in params

    def test_bitbucket_commit_calls_put_file_content(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        """bitbucket_commit publishes one file through the MCP file-write tool."""
        actions = [
            OutputAction(
                type=ActionType.BITBUCKET_COMMIT,
                params={
                    "project_key": "PAY",
                    "repo_slug": "payments-api",
                    "path": "reports/PAY-1.md",
                    "content": "# Results",
                    "commit_message": "PAY-1 publish results",
                    "target_branch": "ai/PAY-1",
                },
                index=0,
            )
        ]
        inp = _make_batch_input(actions=actions)
        asyncio.run(execute_output_actions(inp))

        assert fake_caller.calls[0][0] == "bitbucket_put_file_content"
        assert fake_caller.calls[0][1]["file_path"] == "reports/PAY-1.md"
        assert fake_caller.calls[0][1]["message"] == "PAY-1 publish results"
        assert fake_caller.calls[0][1]["branch"] == "ai/PAY-1"

    def test_confluence_page_with_page_id_calls_update(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        """Requirement 3.5: confluence_page with page_id → update."""
        actions = [
            OutputAction(
                type=ActionType.CONFLUENCE_PAGE,
                params={"page_id": "12345", "body": "updated content"},
                index=0,
            )
        ]
        inp = _make_batch_input(actions=actions)
        asyncio.run(execute_output_actions(inp))

        assert fake_caller.calls[0][0] == "confluence_update_page"

    def test_confluence_page_without_page_id_calls_create(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        """Requirement 3.5: confluence_page without page_id → create."""
        actions = [
            OutputAction(
                type=ActionType.CONFLUENCE_PAGE,
                params={"space_key": "DEV", "title": "New Page", "body": "content"},
                index=0,
            )
        ]
        inp = _make_batch_input(actions=actions)
        asyncio.run(execute_output_actions(inp))

        assert fake_caller.calls[0][0] == "confluence_create_page"

    def test_jira_transition_calls_jira_transition_issue(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        """Requirement 3.6: jira_transition → jira_transition_issue tool."""
        actions = [
            OutputAction(
                type=ActionType.JIRA_TRANSITION,
                params={"issue_key": "PAY-1", "target_status": "done"},
                index=0,
            )
        ]
        inp = _make_batch_input(actions=actions)
        asyncio.run(execute_output_actions(inp))

        assert fake_caller.calls[0][0] == "jira_transition_issue"


# ---------------------------------------------------------------------------
# Tests: Error handling (Requirements 3.7, 3.8)
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Errors are logged and execution continues to next action."""

    def test_failed_action_continues_to_next(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        """Requirement 3.7: log error, continue to next action."""
        fake_caller.errors["jira_add_comment"] = RuntimeError("API 500")
        fake_caller.responses["bitbucket_create_pr"] = {"id": 42}

        actions = [
            OutputAction(
                type=ActionType.JIRA_COMMENT,
                params={"body": "fail"},
                index=0,
            ),
            OutputAction(
                type=ActionType.BITBUCKET_PR,
                params={"source": "a", "target": "b"},
                index=1,
            ),
        ]
        inp = _make_batch_input(actions=actions)
        result = asyncio.run(execute_output_actions(inp))

        assert result.all_succeeded is False
        assert len(result.failed_actions) == 1
        assert result.results[0].status == "failed"
        assert result.results[1].status == "success"

        # Failure summary comment posted to Jira (last call after the
        # error in jira_add_comment is for the bitbucket_create_pr,
        # then the failure summary)
        # The error dict only applies to "jira_add_comment" tool name,
        # so the failure summary call also uses "jira_add_comment" and
        # will also fail — but that's handled gracefully.

    def test_failure_summary_posted_to_jira(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        """Requirement 3.7: failure list posted as Jira comment."""
        # Make bitbucket_create_pr fail but jira_add_comment succeed
        fake_caller.errors["bitbucket_create_pr"] = RuntimeError("PR error")

        actions = [
            OutputAction(
                type=ActionType.BITBUCKET_PR,
                params={"source": "a", "target": "b"},
                index=0,
            ),
        ]
        inp = _make_batch_input(actions=actions)
        asyncio.run(execute_output_actions(inp))

        # The last call should be the failure summary comment
        summary_calls = [
            c for c in fake_caller.calls
            if c[0] == "jira_add_comment" and "başarısız" in c[1].get("body", "")
        ]
        assert len(summary_calls) == 1
        assert "bitbucket_pr" in summary_calls[0][1]["body"]

    def test_timeout_marks_action_as_timeout(
        self, fake_caller: _FakeMCPCaller, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement 3.8: 30s timeout → action marked as timeout."""
        import automation_worker.activities.output_actions as oa_mod

        # Temporarily reduce timeout to 0.5s for fast testing
        monkeypatch.setattr(oa_mod, "ACTION_TIMEOUT_SECONDS", 0.5)
        fake_caller.delays["jira_add_comment"] = 2.0

        actions = [
            OutputAction(
                type=ActionType.JIRA_COMMENT,
                params={"body": "slow"},
                index=0,
            ),
        ]
        inp = _make_batch_input(actions=actions)
        result = asyncio.run(execute_output_actions(inp))

        assert result.results[0].status == "timeout"
        assert result.all_succeeded is False


# ---------------------------------------------------------------------------
# Tests: Audit completeness (Requirement 3.9)
# ---------------------------------------------------------------------------


class TestAuditCompleteness:
    """Every action gets a result with timestamp regardless of outcome."""

    def test_all_results_have_timestamps(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        """Requirement 3.9: each result has a timestamp."""
        fake_caller.errors["bitbucket_create_pr"] = RuntimeError("fail")

        actions = [
            OutputAction(
                type=ActionType.JIRA_COMMENT,
                params={"body": "ok"},
                index=0,
            ),
            OutputAction(
                type=ActionType.BITBUCKET_PR,
                params={"source": "a", "target": "b"},
                index=1,
            ),
        ]
        inp = _make_batch_input(actions=actions)
        result = asyncio.run(execute_output_actions(inp))

        for r in result.results:
            assert r.timestamp is not None
            assert isinstance(r.timestamp, datetime)
            assert r.timestamp.tzinfo == timezone.utc

    def test_results_contain_action_type_and_index(
        self, fake_caller: _FakeMCPCaller
    ) -> None:
        """Requirement 3.9: each result has action_type and index."""
        actions = [
            OutputAction(
                type=ActionType.JIRA_COMMENT,
                params={"body": "x"},
                index=5,
            ),
        ]
        inp = _make_batch_input(actions=actions)
        result = asyncio.run(execute_output_actions(inp))

        assert result.results[0].action_type == ActionType.JIRA_COMMENT
        assert result.results[0].index == 5
        assert result.results[0].status == "success"
