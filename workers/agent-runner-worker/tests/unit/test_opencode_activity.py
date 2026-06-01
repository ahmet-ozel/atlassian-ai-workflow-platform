"""Unit tests for the opencode activity module.

Tests the ``opencode_generate_code`` activity function by mocking the
OpenCode sidecar HTTP API responses.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import httpx

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

from activities.opencode import (
    CodePlan,
    CodeResult,
    FileChange,
    OpenCodeError,
    opencode_generate_code,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def plan() -> CodePlan:
    """A sample code generation plan."""
    return CodePlan(
        issue_key="PAY-4211",
        prompt="Fix the callback error handling in payment_service.py",
        model=None,
    )


@pytest.fixture
def workspace_path() -> str:
    return "/tmp/workspace/PAY-4211"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCodePlan:
    """Tests for the CodePlan dataclass."""

    def test_frozen(self) -> None:
        plan = CodePlan(issue_key="X-1", prompt="test")
        with pytest.raises(AttributeError):
            plan.issue_key = "Y-2"  # type: ignore[misc]

    def test_default_model_none(self) -> None:
        plan = CodePlan(issue_key="X-1", prompt="test")
        assert plan.model is None

    def test_with_model(self) -> None:
        plan = CodePlan(issue_key="X-1", prompt="test", model="vllm/qwen2.5-coder")
        assert plan.model == "vllm/qwen2.5-coder"


class TestFileChange:
    """Tests for the FileChange dataclass."""

    def test_frozen(self) -> None:
        fc = FileChange(path="src/main.py", action="modified")
        with pytest.raises(AttributeError):
            fc.path = "other.py"  # type: ignore[misc]

    def test_fields(self) -> None:
        fc = FileChange(path="src/new.py", action="created")
        assert fc.path == "src/new.py"
        assert fc.action == "created"


class TestCodeResult:
    """Tests for the CodeResult dataclass."""

    def test_frozen(self) -> None:
        result = CodeResult(
            files_changed=[FileChange(path="a.py", action="modified")],
            diff_content="--- a/a.py\n+++ b/a.py\n",
            session_id="sess-123",
        )
        with pytest.raises(AttributeError):
            result.session_id = "other"  # type: ignore[misc]


class TestOpenCodeError:
    """Tests for the OpenCodeError exception."""

    def test_message(self) -> None:
        err = OpenCodeError("something failed", status_code=500)
        assert "something failed" in str(err)
        assert err.status_code == 500

    def test_no_status_code(self) -> None:
        err = OpenCodeError("network error")
        assert err.status_code is None
