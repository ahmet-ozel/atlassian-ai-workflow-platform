"""Unit tests for O5 — MCPHttpError permission-denied detection.

Confluence smoke tests sporadically surface
``"The calling user does not have permission to view the content"`` as
an opaque MCP failure (VPS TEST_REPORT R11). The fix attaches a
structured ``is_permission_denied`` flag to :class:`MCPHttpError` so
downstream Jira-comment helpers can show a clear operator-facing
hint. These tests pin the detection patterns.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_worker_path() -> None:
    """Local sys.path injection (K3-compatible) — see
    tests/property/test_workflow_type_parity.py for the rationale."""
    repo_root = Path(__file__).resolve().parents[2]
    src = repo_root / "workers" / "automation-worker" / "src"
    s = str(src)
    if src.is_dir() and s not in sys.path:
        sys.path.insert(0, s)


_ensure_worker_path()

from automation_worker.activities.mcp_caller import (  # noqa: E402
    MCPHttpError,
)


def test_status_403_marks_permission_denied() -> None:
    exc = MCPHttpError("confluence_create_page", 403, "Forbidden")
    assert exc.is_permission_denied is True


def test_status_401_marks_permission_denied() -> None:
    exc = MCPHttpError("jira_add_comment", 401, "Authentication failed")
    assert exc.is_permission_denied is True


def test_status_500_with_permission_text_marks_permission_denied() -> None:
    # Atlassian sometimes wraps the permission error in a 500-shaped
    # envelope when MCP fans the upstream error back as-is.
    exc = MCPHttpError(
        "confluence_create_page",
        500,
        "Failed to create page: The calling user does not have permission",
    )
    assert exc.is_permission_denied is True


def test_status_500_unrelated_does_not_mark_permission() -> None:
    exc = MCPHttpError("jira_get_issue", 500, "Database connection lost")
    assert exc.is_permission_denied is False


def test_status_200_with_empty_detail_does_not_mark_permission() -> None:
    exc = MCPHttpError("noop_tool", 200, "")
    assert exc.is_permission_denied is False


def test_turkish_hint_in_message() -> None:
    """The structured hint must be human-readable Turkish — operators
    skimming a Jira comment shouldn't need to decode an HTTP status."""
    exc = MCPHttpError(
        "confluence_create_page",
        403,
        "The calling user does not have permission to view the content",
    )
    assert "permission_denied" in str(exc)
    assert "yetkisi yok" in str(exc)


def test_non_permission_error_keeps_legacy_format() -> None:
    """Non-permission errors must NOT carry the hint suffix so the
    existing parsers (which match on the legacy ``failed (status=...)``
    prefix) keep working unchanged."""
    exc = MCPHttpError("jira_get_issue", 500, "Database connection lost")
    msg = str(exc)
    assert "permission_denied" not in msg
    assert "yetkisi yok" not in msg
