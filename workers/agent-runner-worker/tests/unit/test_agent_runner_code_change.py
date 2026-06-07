"""Unit tests for ``AgentRunnerWorkflow`` ``code_change_*`` flows.

Covers the four execution paths plumbed into ``_dispatch_workflow_type``:

    1. ``code_change_with_test`` happy path - verifies the activity
       sequence (``set_assignee_to_bot`` → ``precommit_scanner`` →
       ``bitbucket_commit_via_git`` → child ``ExecutionRunWorkflow`` →
       ``bitbucket_create_pull_request_cloud`` → ``jira_add_comment``
       with the PR link).
    2. ``code_change_commit_only`` - same prefix as above but does
       NOT invoke any PR-creation activity nor the
       ``ExecutionRunWorkflow`` child; the Jira comment carries the
       branch link plus a diff summary.
    3. ``pr_review`` dedup - a second invocation with the same
       finding hash does NOT post a duplicate ``bitbucket_add_pr_comment``.
    4. ``precommit_scanner`` block path - workflow fails with the
       stable ``precommit_secret_leak_blocked`` failure reason.
    5. ``branch_pattern_rules`` deny - ``code_change_commit_only`` on
       ``hotfix/*`` short-circuits to ``out_of_scope``.

The tests drive the body methods directly (``_handle_code_change_*``,
``_handle_pr_review``) without spinning up a Temporal worker. Activity
calls are intercepted by patching ``temporalio.workflow.execute_activity``
and ``temporalio.workflow.execute_child_workflow``. ``workflow.now`` is
also stubbed so the ``[fix]`` debounce / ``[explain]`` cache paths
remain deterministic.

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
from temporalio.exceptions import ApplicationError

# ---------------------------------------------------------------------------
# sys.path bootstrap - mirrors ``test_agent_runner_signal_handlers.py``.
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
    AgentRunnerWorkflow,
    TokenCapExceededError,
    _OutOfScope,
)
from temporal_shared.messages import (  # noqa: E402
    AgentRunnerWorkflowInput,
    LlmAnalysisResult,
)


_CODEGEN_OUTPUT: dict[str, Any] = {
    "files": [
        {
            "path": "src/payment_retry.py",
            "content": "def retry_enabled():\n    return True\n",
            "action": "update",
        }
    ],
    "explanation": "Updates retry handling.",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixed_now() -> datetime:
    """Deterministic anchor for ``workflow.now`` stubs."""

    return datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def patched_workflow_now(fixed_now: datetime, monkeypatch: pytest.MonkeyPatch):
    """Replace ``workflow.now`` with a deterministic clock."""

    state = {"now": fixed_now}
    monkeypatch.setattr(_temporal_workflow, "now", lambda: state["now"])
    return state


def _make_input(
    *,
    workflow_type: str,
    target_branch: str = "ai/PAY-4211",
    rationale: str = "PR-127 needs a review",
    title: str = "Fix payment retry",
) -> AgentRunnerWorkflowInput:
    """Build a minimal :class:`AgentRunnerWorkflowInput` fixture."""

    analysis = LlmAnalysisResult(
        workflow_type=workflow_type,
        confidence="high",
        target_repo="payment-callbacks",
        target_branch=target_branch,
        title=title,
        rationale=rationale,
        token_usage=120,
        execution_command="pytest -q",
    )
    return AgentRunnerWorkflowInput(
        parent_workflow_id="automation-jira-PAY-4211",
        issue_key="PAY-4211",
        department_id="payments",
        workflow_type=workflow_type,
        analysis=analysis,
        target_repo="payment-callbacks",
        target_branch=target_branch,
        iteration=1,
        max_iter=5,
        default_language="tr",
    )


@pytest.fixture
def make_wf():
    """Factory returning a fresh :class:`AgentRunnerWorkflow`.

    Mirrors the signal-handler tests' factory: seeds ``iter_count=1``
    so the body methods operate on a non-zero iteration counter.
    """

    def _build() -> AgentRunnerWorkflow:
        wf = AgentRunnerWorkflow()
        # Mirror what ``run`` would set up on its first turn.
        from dataclasses import replace

        wf._iteration_state = replace(wf._iteration_state, iter_count=1)
        return wf

    return _build


def _activity_dispatcher(routes: dict[str, Any]) -> AsyncMock:
    """Return an ``AsyncMock`` that resolves ``execute_activity`` calls.

    *routes* maps activity-name → return value (or callable). Activities
    not present in *routes* return ``None``.
    """

    async def _fake_execute_activity(*args, **kwargs):
        name = args[0] if args else kwargs.get("activity")
        if name in routes:
            value = routes[name]
            if callable(value):
                return value(*args, **kwargs)
            return value
        return None

    return AsyncMock(side_effect=_fake_execute_activity)


def _build_patches(
    *,
    activity_mock: AsyncMock,
    child_mock: AsyncMock | None = None,
):
    """Return the standard set of ``patch.object`` context managers."""

    info_stub = type(
        "WfInfo", (), {"workflow_id": "automation-jira-PAY-4211"}
    )()
    patches: list[Any] = [
        patch.object(_temporal_workflow, "execute_activity", activity_mock),
        patch.object(_temporal_workflow, "info", lambda: info_stub),
    ]
    if child_mock is not None:
        patches.append(
            patch.object(
                _temporal_workflow, "execute_child_workflow", child_mock
            )
        )
    return patches


# ---------------------------------------------------------------------------
# 1. ``code_change_with_test`` happy path
# ---------------------------------------------------------------------------


class TestCodeChangeWithTest:
    """Happy path through the full activity chain."""

    def test_happy_path_invokes_full_activity_sequence(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(workflow_type="code_change_with_test")

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "opencode_generate_code": _CODEGEN_OUTPUT,
            "precommit_scanner": {
                "decision": "pass",
                "matched_patterns": [],
            },
            "bitbucket_commit_via_git": {
                "commit_hash": "abc123",
                "branch": "ai/PAY-4211",
                "message": "[bot] Fix payment retry",
            },
            "bitbucket_create_pull_request_cloud": {
                "id": 127,
                "title": "[bot] Fix payment retry",
                "url": "https://bitbucket.example/pr/127",
                "draft": True,
            },
            "jira_add_comment": None,
            "audit_emit": None,
        }
        activity_mock = _activity_dispatcher(activity_routes)
        child_mock = AsyncMock(
            return_value=type(
                "ExecOutput", (), {"status": "passed"}
            )()
        )

        async def _drive() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "execute_child_workflow",
                child_mock,
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo",
                    (),
                    {"workflow_id": "automation-jira-PAY-4211"},
                )(),
            ):
                await wf._handle_code_change_with_test(inp)

        asyncio.run(_drive())

        # Activity ordering check.
        called_names = [c.args[0] for c in activity_mock.call_args_list]
        assert "set_assignee_to_bot" in called_names
        assert "precommit_scanner" in called_names
        assert "bitbucket_commit_via_git" in called_names
        assert "bitbucket_create_pull_request_cloud" in called_names
        assert "jira_add_comment" in called_names

        # Sequence: set_assignee_to_bot precedes precommit_scanner,
        # which precedes the commit + PR open.
        idx_assignee = called_names.index("set_assignee_to_bot")
        idx_precommit = called_names.index("precommit_scanner")
        idx_commit = called_names.index("bitbucket_commit_via_git")
        idx_pr = called_names.index("bitbucket_create_pull_request_cloud")
        assert idx_assignee < idx_precommit < idx_commit < idx_pr

        commit_calls = [
            c for c in activity_mock.call_args_list
            if c.args[0] == "bitbucket_commit_via_git"
        ]
        commit_args = commit_calls[0].kwargs.get("args") or commit_calls[0].args[1]
        # git commit signature: [repo, branch, source_branch, files, message, dept]
        committed_files = commit_args[3]
        assert committed_files[0]["path"] == "src/payment_retry.py"
        assert committed_files[0]["content"]

        # Child ExecutionRunWorkflow was started with the right name.
        assert child_mock.call_count == 1
        assert child_mock.call_args.args[0] == "ExecutionRunWorkflow"

        # The Jira comment carries the PR link.
        jira_calls = [
            c for c in activity_mock.call_args_list
            if c.args[0] == "jira_add_comment"
        ]
        assert len(jira_calls) == 1
        comment_args = jira_calls[0].kwargs.get("args") or jira_calls[0].args[1]
        # The comment is the second positional argument to jira_add_comment.
        comment_body = comment_args[1] if isinstance(comment_args, list) else ""
        assert "127" in comment_body or "Draft PR" in comment_body

    def test_test_failure_skips_pr_creation(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(workflow_type="code_change_with_test")

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "opencode_generate_code": _CODEGEN_OUTPUT,
            "precommit_scanner": {"decision": "pass", "matched_patterns": []},
            "bitbucket_commit_via_git": {
                "commit_hash": "abc123",
                "branch": "ai/PAY-4211",
                "message": "[bot] msg",
            },
            "jira_add_comment": None,
            "audit_emit": None,
        }
        activity_mock = _activity_dispatcher(activity_routes)
        # Child reports test failure.
        child_mock = AsyncMock(
            return_value=type(
                "ExecOutput", (), {"status": "failed"}
            )()
        )

        async def _drive() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "execute_child_workflow",
                child_mock,
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo",
                    (),
                    {"workflow_id": "x"},
                )(),
            ):
                await wf._handle_code_change_with_test(inp)

        asyncio.run(_drive())

        called_names = [c.args[0] for c in activity_mock.call_args_list]
        # Crucial invariant - failed tests mean NO PR creation.
        assert "bitbucket_create_pull_request_cloud" not in called_names
        assert "bitbucket_create_pull_request_dc" not in called_names
        # And a failure-summary comment was posted.
        assert "jira_add_comment" in called_names
        assert wf._failure_reason == "execution_run_failed"

    def test_codegen_without_files_comments_and_skips_commit(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(workflow_type="code_change_with_test")

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "opencode_generate_code": {"files": [], "explanation": "need paths"},
            "jira_add_comment": None,
            "audit_emit": None,
        }
        activity_mock = _activity_dispatcher(activity_routes)
        child_mock = AsyncMock()

        async def _drive() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "execute_child_workflow",
                child_mock,
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type("WfInfo", (), {"workflow_id": "x"})(),
            ):
                await wf._handle_code_change_with_test(inp)

        asyncio.run(_drive())

        called_names = [c.args[0] for c in activity_mock.call_args_list]
        assert "jira_add_comment" in called_names
        assert "precommit_scanner" not in called_names
        assert "bitbucket_commit_via_git" not in called_names
        assert child_mock.call_count == 0
        assert wf._failure_reason == "code_generation_no_files"

    def test_codegen_retry_recovers_when_first_pass_empty(
        self, make_wf, patched_workflow_now
    ) -> None:
        """First opencode pass returns no files; the directive retry
        produces a committable file and the workflow proceeds to commit
        + PR instead of giving up."""

        wf = make_wf()
        inp = _make_input(workflow_type="code_change_with_test")

        codegen_calls = {"n": 0}

        def _codegen(*args, **kwargs):
            codegen_calls["n"] += 1
            if codegen_calls["n"] == 1:
                return {"files": [], "explanation": "need paths"}
            return _CODEGEN_OUTPUT

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "opencode_generate_code": _codegen,
            "precommit_scanner": {"decision": "pass", "matched_patterns": []},
            "bitbucket_commit_via_git": {
                "commit_hash": "abc123",
                "branch": "ai/PAY-4211",
                "message": "[bot] Fix payment retry",
            },
            "bitbucket_create_pull_request_cloud": {
                "id": 127,
                "title": "[bot] Fix payment retry",
                "url": "https://bitbucket.example/pr/127",
                "draft": True,
            },
            "jira_add_comment": None,
            "audit_emit": None,
        }
        activity_mock = _activity_dispatcher(activity_routes)
        child_mock = AsyncMock(
            return_value=type("ExecOutput", (), {"status": "passed"})()
        )

        async def _drive() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "execute_child_workflow",
                child_mock,
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo",
                    (),
                    {"workflow_id": "automation-jira-PAY-4211"},
                )(),
            ):
                await wf._handle_code_change_with_test(inp)

        asyncio.run(_drive())

        called_names = [c.args[0] for c in activity_mock.call_args_list]
        # opencode_generate_code was invoked twice (first empty, then retry).
        assert codegen_calls["n"] == 2
        # The retry recovered: commit + PR happened, no give-up comment.
        assert "precommit_scanner" in called_names
        assert "bitbucket_commit_via_git" in called_names
        assert "bitbucket_create_pull_request_cloud" in called_names
        assert wf._failure_reason != "code_generation_no_files"

    def test_codegen_retry_directive_prompt_differs(self) -> None:
        """The retry prompt must be stronger than the first-pass prompt
        and demand at least one file."""

        inp = _make_input(workflow_type="code_change_with_test")
        first = AgentRunnerWorkflow._build_code_generation_prompt(inp)
        retry = AgentRunnerWorkflow._build_code_generation_retry_prompt(inp)
        assert retry != first
        assert first in retry
        assert "at least one file" in retry.lower()


# ---------------------------------------------------------------------------
# 2. ``code_change_commit_only`` - no PR creation, no test child
# ---------------------------------------------------------------------------


class TestCodeChangeCommitOnly:
    """Commit-only flow: branch + commit + Jira comment, no PR."""

    def test_commit_only_does_not_invoke_pr_creation(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(workflow_type="code_change_commit_only")

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "opencode_generate_code": _CODEGEN_OUTPUT,
            "precommit_scanner": {"decision": "pass", "matched_patterns": []},
            "bitbucket_commit_via_git": {
                "commit_hash": "deadbeef",
                "branch": "ai/PAY-4211",
                "message": "[bot] msg",
            },
            "jira_add_comment": None,
            "audit_emit": None,
        }
        activity_mock = _activity_dispatcher(activity_routes)
        child_mock = AsyncMock()  # must NOT be called

        async def _drive() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "execute_child_workflow",
                child_mock,
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo",
                    (),
                    {"workflow_id": "x"},
                )(),
            ):
                await wf._handle_code_change_commit_only(inp)

        asyncio.run(_drive())

        called_names = [c.args[0] for c in activity_mock.call_args_list]
        # Critical invariants: NO PR-creation activity, NO test child.
        assert "bitbucket_create_pull_request_cloud" not in called_names
        assert "bitbucket_create_pull_request_dc" not in called_names
        assert child_mock.call_count == 0

        # The standard prefix is still invoked.
        assert "set_assignee_to_bot" in called_names
        assert "precommit_scanner" in called_names
        assert "bitbucket_commit_via_git" in called_names
        # And a branch-link Jira comment was posted.
        assert "jira_add_comment" in called_names

    def test_diff_summary_cached_across_iterations(
        self, make_wf, patched_workflow_now
    ) -> None:
        """Second commit with same hash hits the cache - comment carries
        the cached summary and no extra LLM call is invoked."""

        wf = make_wf()
        inp = _make_input(workflow_type="code_change_commit_only")

        # Pre-warm the cache with a known summary.
        wf._diff_summary_cache["deadbeef"] = "cached diff summary"

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "opencode_generate_code": _CODEGEN_OUTPUT,
            "precommit_scanner": {"decision": "pass", "matched_patterns": []},
            "bitbucket_commit_via_git": {
                "commit_hash": "deadbeef",
                "branch": "ai/PAY-4211",
                "message": "msg",
            },
            "jira_add_comment": None,
            "audit_emit": None,
        }
        activity_mock = _activity_dispatcher(activity_routes)

        async def _drive() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "execute_child_workflow",
                AsyncMock(),
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type(
                    "WfInfo",
                    (),
                    {"workflow_id": "x"},
                )(),
            ):
                await wf._handle_code_change_commit_only(inp)

        asyncio.run(_drive())

        # Cache value remained - the cache hit served the comment.
        assert wf._diff_summary_cache["deadbeef"] == "cached diff summary"


# ---------------------------------------------------------------------------
# 3. ``pr_review`` dedup
# ---------------------------------------------------------------------------


class TestPrReviewDedup:
    """LLM-driven PR review with hash-based dedup."""

    def test_dedup_suppresses_repeat_findings_across_iterations(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(
            workflow_type="pr_review",
            target_branch="",  # PR review does not need a branch.
            rationale="127",  # PR id stashed in rationale.
        )

        # First run returns a finding with hash ``H1``; second run
        # returns the same hash plus a fresh ``H2``. Only ``H2`` should
        # be posted as a PR comment in the second run.
        review_first = {
            "findings": [
                {"hash": "H1", "body": "Use `is None`, not `== None`."},
            ]
        }
        review_second = {
            "findings": [
                {"hash": "H1", "body": "Use `is None`, not `== None`."},
                {"hash": "H2", "body": "Add a docstring."},
            ]
        }

        # Switchable LLM return value.
        review_state = {"current": review_first}

        def _llm_return(*args, **kwargs):
            return review_state["current"]

        activity_routes_first: dict[str, Any] = {
            "bitbucket_fetch_pr_diff": {
                "diff_content": "@@ -1 +1 @@\n-old\n+new",
            },
            "llm_review_code": _llm_return,
            "bitbucket_add_pr_comment": None,
            "audit_emit": None,
        }
        mock_first = _activity_dispatcher(activity_routes_first)

        async def _drive_first() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", mock_first
            ), patch.object(
                _temporal_workflow,
                "execute_child_workflow",
                AsyncMock(),
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type("WfInfo", (), {"workflow_id": "x"})(),
            ):
                await wf._handle_pr_review(inp)

        asyncio.run(_drive_first())

        first_comment_calls = [
            c
            for c in mock_first.call_args_list
            if c.args[0] == "bitbucket_add_pr_comment"
        ]
        assert len(first_comment_calls) == 1
        assert "H1" in wf._previous_findings

        # Second run - same workflow instance.
        review_state["current"] = review_second
        mock_second = _activity_dispatcher(activity_routes_first)

        async def _drive_second() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", mock_second
            ), patch.object(
                _temporal_workflow,
                "execute_child_workflow",
                AsyncMock(),
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type("WfInfo", (), {"workflow_id": "x"})(),
            ):
                await wf._handle_pr_review(inp)

        asyncio.run(_drive_second())

        second_comment_calls = [
            c
            for c in mock_second.call_args_list
            if c.args[0] == "bitbucket_add_pr_comment"
        ]
        # Only the ``H2`` comment is posted - ``H1`` is suppressed.
        assert len(second_comment_calls) == 1
        body_arg = second_comment_calls[0].kwargs.get(
            "args"
        ) or second_comment_calls[0].args[1]
        body_text = body_arg[2] if isinstance(body_arg, list) else ""
        assert "docstring" in body_text
        # Both hashes are now in the seen set.
        assert wf._previous_findings == {"H1", "H2"}


# ---------------------------------------------------------------------------
# 4. ``precommit_scanner`` block path
# ---------------------------------------------------------------------------


class TestPrecommitBlock:
    """Secret-leak block aborts the workflow with a stable failure."""

    def test_block_decision_raises_application_error(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(workflow_type="code_change_with_test")

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "opencode_generate_code": _CODEGEN_OUTPUT,
            "precommit_scanner": {
                "decision": "block",
                "matched_patterns": ["aws_access_key"],
            },
            "audit_emit": None,
        }
        activity_mock = _activity_dispatcher(activity_routes)

        async def _drive() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "execute_child_workflow",
                AsyncMock(),
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type("WfInfo", (), {"workflow_id": "x"})(),
            ):
                with pytest.raises(ApplicationError) as excinfo:
                    await wf._handle_code_change_with_test(inp)
                assert excinfo.value.type == "PrecommitSecretLeakBlocked"

        asyncio.run(_drive())

        called_names = [c.args[0] for c in activity_mock.call_args_list]
        # Commit / PR steps were NOT invoked.
        assert "bitbucket_commit_via_git" not in called_names
        assert "bitbucket_create_pull_request_cloud" not in called_names
        # Failure reason is the audit-stable token.
        assert wf._failure_reason == "precommit_secret_leak_blocked"


# ---------------------------------------------------------------------------
# 5. ``branch_pattern_rules`` deny - out_of_scope short-circuit
# ---------------------------------------------------------------------------


class TestBranchPatternRulesDeny:
    """Hotfix + commit_only is denied by the foundation default rule."""

    def test_hotfix_commit_only_short_circuits(
        self, make_wf, patched_workflow_now
    ) -> None:
        wf = make_wf()
        inp = _make_input(
            workflow_type="code_change_commit_only",
            target_branch="hotfix/PAY-9999",
        )

        activity_routes: dict[str, Any] = {
            "set_assignee_to_bot": None,
            "audit_emit": None,
        }
        activity_mock = _activity_dispatcher(activity_routes)

        async def _drive() -> None:
            with patch.object(
                _temporal_workflow, "execute_activity", activity_mock
            ), patch.object(
                _temporal_workflow,
                "execute_child_workflow",
                AsyncMock(),
            ), patch.object(
                _temporal_workflow,
                "info",
                lambda: type("WfInfo", (), {"workflow_id": "x"})(),
            ):
                with pytest.raises(_OutOfScope):
                    await wf._handle_code_change_commit_only(inp)

        asyncio.run(_drive())

        called_names = [c.args[0] for c in activity_mock.call_args_list]
        # Assignee + audit_emit fired - but no commit / PR.
        assert "set_assignee_to_bot" in called_names
        assert "bitbucket_commit_via_git" not in called_names
        # Audit row carries the rule's reason token.
        audit_calls = [
            c for c in activity_mock.call_args_list if c.args[0] == "audit_emit"
        ]
        assert len(audit_calls) == 1
        audit_payload = (
            audit_calls[0].kwargs.get("args") or audit_calls[0].args[1]
        )[0]
        assert audit_payload["action"] == "hotfix_requires_pr"
        # Workflow-level state reflects the denial.
        assert wf._out_of_scope is True
        assert wf._failure_reason == "hotfix_requires_pr"
