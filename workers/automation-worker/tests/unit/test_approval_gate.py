"""Unit tests for ``ApprovalGateWorkflow`` pure helpers and data classes.

Tests exercise the pure helper functions (``match_approval_paths``,
``is_authorized_approver``, ``parse_approval_decision``) without
spinning up a Temporal worker. These functions contain the core logic
that the workflow delegates to.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrapping
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from automation_worker.workflows.approval_gate import (  # noqa: E402
    ApprovalGateInput,
    ApprovalGateResult,
    is_authorized_approver,
    match_approval_paths,
    parse_approval_decision,
)


# ===========================================================================
# 1. match_approval_paths tests
# ===========================================================================


class TestMatchApprovalPaths:
    """Tests for regex-based file path matching."""

    def test_basic_match(self) -> None:
        """Files matching a pattern are returned."""
        files = ["src/core/auth.py", "src/utils/helper.py", "README.md"]
        patterns = [r"^src/core/.*"]
        result = match_approval_paths(files, patterns)
        assert result == ["src/core/auth.py"]

    def test_multiple_matches(self) -> None:
        """Multiple files can match patterns."""
        files = [
            "src/core/auth.py",
            "src/core/db.py",
            "config/production.yaml",
            "docs/readme.md",
        ]
        patterns = [r"^src/core/.*", r"^config/production\..*"]
        result = match_approval_paths(files, patterns)
        assert result == [
            "src/core/auth.py",
            "src/core/db.py",
            "config/production.yaml",
        ]

    def test_no_matches(self) -> None:
        """No files match - returns empty list."""
        files = ["docs/readme.md", "tests/test_foo.py"]
        patterns = [r"^src/core/.*"]
        result = match_approval_paths(files, patterns)
        assert result == []

    def test_empty_patterns_returns_empty(self) -> None:
        """Empty patterns list returns empty."""
        files = ["src/core/auth.py"]
        result = match_approval_paths(files, [])
        assert result == []

    def test_empty_files_returns_empty(self) -> None:
        """Empty files list - returns empty."""
        patterns = [r"^src/core/.*"]
        result = match_approval_paths([], patterns)
        assert result == []

    def test_invalid_regex_skipped(self) -> None:
        """Invalid regex patterns are skipped gracefully."""
        files = ["src/core/auth.py", "src/utils/helper.py"]
        patterns = [r"[invalid", r"^src/core/.*"]
        result = match_approval_paths(files, patterns)
        assert result == ["src/core/auth.py"]

    def test_file_matches_only_once(self) -> None:
        """A file matching multiple patterns appears only once."""
        files = ["src/core/auth.py"]
        patterns = [r"^src/.*", r".*auth.*"]
        result = match_approval_paths(files, patterns)
        assert result == ["src/core/auth.py"]


# ===========================================================================
# 2. is_authorized_approver tests
# ===========================================================================


class TestIsAuthorizedApprover:
    """Tests for authorization checking."""

    def test_authorized_user(self) -> None:
        """User in approvers list is authorized."""
        assert is_authorized_approver("user-123", ["user-123", "user-456"])

    def test_unauthorized_user(self) -> None:
        """User not in approvers list is unauthorized."""
        assert not is_authorized_approver("user-789", ["user-123", "user-456"])

    def test_empty_approvers_list(self) -> None:
        """Empty approvers list - no one is authorized."""
        assert not is_authorized_approver("user-123", [])

    def test_empty_user_id(self) -> None:
        """Empty user ID is not authorized."""
        assert not is_authorized_approver("", ["user-123"])


# ===========================================================================
# 3. parse_approval_decision tests
# ===========================================================================


class TestParseApprovalDecision:
    """Tests for parsing [approve] and [reject] markers."""

    def test_approve_lowercase(self) -> None:
        assert parse_approval_decision("[approve]") == "approve"

    def test_approve_uppercase(self) -> None:
        assert parse_approval_decision("[APPROVE]") == "approve"

    def test_approve_mixed_case(self) -> None:
        assert parse_approval_decision("[Approve]") == "approve"

    def test_approve_in_sentence(self) -> None:
        assert parse_approval_decision("I [approve] this change") == "approve"

    def test_reject_lowercase(self) -> None:
        assert parse_approval_decision("[reject]") == "reject"

    def test_reject_uppercase(self) -> None:
        assert parse_approval_decision("[REJECT]") == "reject"

    def test_reject_mixed_case(self) -> None:
        assert parse_approval_decision("[Reject]") == "reject"

    def test_reject_in_sentence(self) -> None:
        assert parse_approval_decision("I [reject] this PR") == "reject"

    def test_no_marker(self) -> None:
        assert parse_approval_decision("Looks good to me") is None

    def test_empty_string(self) -> None:
        assert parse_approval_decision("") is None

    def test_approve_takes_priority_over_reject(self) -> None:
        """If both markers present, [approve] wins (appears first)."""
        result = parse_approval_decision("[approve] but also [reject]")
        assert result == "approve"


# ===========================================================================
# 4. Data class tests
# ===========================================================================


class TestApprovalGateInput:
    """Tests for ApprovalGateInput dataclass."""

    def test_defaults(self) -> None:
        inp = ApprovalGateInput(
            issue_key="PAY-123",
            dept_id="payment",
            workflow_id="wf-001",
        )
        assert inp.commit_files == []
        assert inp.approval_required_paths == []
        assert inp.approvers == []

    def test_with_values(self) -> None:
        inp = ApprovalGateInput(
            issue_key="PAY-123",
            dept_id="payment",
            workflow_id="wf-001",
            commit_files=["src/core/auth.py"],
            approval_required_paths=[r"^src/core/.*"],
            approvers=["user-1"],
        )
        assert inp.commit_files == ["src/core/auth.py"]
        assert inp.approval_required_paths == [r"^src/core/.*"]
        assert inp.approvers == ["user-1"]


class TestApprovalGateResult:
    """Tests for ApprovalGateResult dataclass."""

    def test_approved_result(self) -> None:
        result = ApprovalGateResult(
            approved=True,
            timed_out=False,
            approver_id="user-1",
            matched_paths=["src/core/auth.py"],
        )
        assert result.approved is True
        assert result.timed_out is False
        assert result.approver_id == "user-1"

    def test_timeout_result(self) -> None:
        result = ApprovalGateResult(
            approved=False,
            timed_out=True,
        )
        assert result.approved is False
        assert result.timed_out is True
        assert result.approver_id is None
        assert result.matched_paths == []
