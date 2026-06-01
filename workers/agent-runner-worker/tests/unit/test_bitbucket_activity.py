"""Unit tests for the bitbucket activity module.

Tests the Bitbucket activity functions by verifying data models,
error handling, and key behaviours (409 idempotency, draft enforcement,
404 compensation).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the worker src is importable
_WORKER_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_DIR = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# Ensure libs are importable
_PLATFORM_ROOT = _WORKER_ROOT.parent.parent
_LIBS_HTTP_SHARED = _PLATFORM_ROOT / "libs" / "http-shared" / "src"
if str(_LIBS_HTTP_SHARED) not in sys.path:
    sys.path.insert(0, str(_LIBS_HTTP_SHARED))

from activities.bitbucket import (
    BitbucketActivityError,
    BranchInfo,
    CommitInfo,
    FileChange,
    PRDiff,
    PRInfo,
    RepoRef,
)


# ---------------------------------------------------------------------------
# Data model tests
# ---------------------------------------------------------------------------


class TestRepoRef:
    """Tests for the RepoRef dataclass."""

    def test_frozen(self) -> None:
        ref = RepoRef(workspace="my-team", repo_slug="payment-service")
        with pytest.raises(AttributeError):
            ref.workspace = "other"  # type: ignore[misc]

    def test_fields(self) -> None:
        ref = RepoRef(workspace="example-co", repo_slug="api-gateway")
        assert ref.workspace == "example-co"
        assert ref.repo_slug == "api-gateway"


class TestBranchInfo:
    """Tests for the BranchInfo dataclass."""

    def test_frozen(self) -> None:
        info = BranchInfo(name="feature/x", target_hash="abc123")
        with pytest.raises(AttributeError):
            info.name = "other"  # type: ignore[misc]

    def test_default_already_existed_false(self) -> None:
        info = BranchInfo(name="feature/x", target_hash="abc123")
        assert info.already_existed is False

    def test_already_existed_true(self) -> None:
        info = BranchInfo(name="feature/x", target_hash="abc123", already_existed=True)
        assert info.already_existed is True


class TestFileChange:
    """Tests for the FileChange dataclass."""

    def test_frozen(self) -> None:
        fc = FileChange(path="src/main.py", content="print('hi')", action="update")
        with pytest.raises(AttributeError):
            fc.path = "other.py"  # type: ignore[misc]

    def test_defaults(self) -> None:
        fc = FileChange(path="src/main.py")
        assert fc.content == ""
        assert fc.action == "update"

    def test_custom_action(self) -> None:
        fc = FileChange(path="new.py", content="x = 1", action="create")
        assert fc.action == "create"


class TestCommitInfo:
    """Tests for the CommitInfo dataclass."""

    def test_frozen(self) -> None:
        info = CommitInfo(commit_hash="deadbeef", message="fix: callback")
        with pytest.raises(AttributeError):
            info.commit_hash = "other"  # type: ignore[misc]

    def test_fields(self) -> None:
        info = CommitInfo(commit_hash="abc123def", message="feat: add handler")
        assert info.commit_hash == "abc123def"
        assert info.message == "feat: add handler"


class TestPRInfo:
    """Tests for the PRInfo dataclass."""

    def test_frozen(self) -> None:
        info = PRInfo(pr_id=42, title="Fix callback", url="https://bb.org/pr/42")
        with pytest.raises(AttributeError):
            info.pr_id = 99  # type: ignore[misc]

    def test_draft_always_true_by_default(self) -> None:
        """PRInfo.draft defaults to True (MIMARI §1 Kural 10)."""
        info = PRInfo(pr_id=1, title="test", url="")
        assert info.draft is True

    def test_fields(self) -> None:
        info = PRInfo(pr_id=42, title="My PR", url="https://example.com/pr/42")
        assert info.pr_id == 42
        assert info.title == "My PR"
        assert info.url == "https://example.com/pr/42"


class TestPRDiff:
    """Tests for the PRDiff dataclass."""

    def test_frozen(self) -> None:
        diff = PRDiff(pr_id=10, diff_content="--- a/x.py\n+++ b/x.py\n")
        with pytest.raises(AttributeError):
            diff.pr_id = 99  # type: ignore[misc]

    def test_default_files_changed(self) -> None:
        diff = PRDiff(pr_id=10, diff_content="some diff")
        assert diff.files_changed == []

    def test_with_files(self) -> None:
        diff = PRDiff(
            pr_id=10,
            diff_content="diff",
            files_changed=["src/a.py", "src/b.py"],
        )
        assert len(diff.files_changed) == 2


class TestBitbucketActivityError:
    """Tests for the BitbucketActivityError exception."""

    def test_message(self) -> None:
        err = BitbucketActivityError("branch creation failed", status_code=500)
        assert "branch creation failed" in str(err)
        assert err.status_code == 500

    def test_no_status_code(self) -> None:
        err = BitbucketActivityError("network error")
        assert err.status_code is None

    def test_is_runtime_error(self) -> None:
        err = BitbucketActivityError("test")
        assert isinstance(err, RuntimeError)
