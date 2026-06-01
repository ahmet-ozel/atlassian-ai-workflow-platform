"""Property test: Temporary file cleanup guarantee for jira_add_attachment.

Feature: platform-completion, Property 10: For any artifact download and
upload attempt (regardless of success or failure), the temporary file
created during the operation SHALL be deleted after the operation
completes.

Validates: Requirements 4.4
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Make ``src`` importable without installing the service package.
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2] / "src"
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from tools.jira_attachment import JiraAttachmentTool  # noqa: E402


def _make_tool() -> JiraAttachmentTool:
    return JiraAttachmentTool(
        jira_base_url="https://example.atlassian.net",
        jira_email="bot@example.com",
        jira_api_token="test",
    )


@settings(max_examples=20, deadline=None)
@given(
    content=st.binary(min_size=1, max_size=1024),
    filename=st.from_regex(
        r"[a-z][a-z0-9_]{1,15}\.(pdf|md|csv|txt|json)", fullmatch=True
    ),
    upload_succeeds=st.booleans(),
)
def test_temp_file_cleaned_up(
    content: bytes, filename: str, upload_succeeds: bool
) -> None:
    """Temp file is deleted regardless of upload outcome."""
    tool = _make_tool()
    captured_temp_path: list[str] = []

    async def _fake_execute(issue_key: str, file_path: str, file_name=None):
        captured_temp_path.append(file_path)
        if upload_succeeds:
            return {"success": True, "id": "att-1"}
        return {"success": False, "error_code": "test_failed", "error": "fake"}

    with patch.object(tool, "execute", _fake_execute):
        asyncio.run(tool.execute_from_temp("PROJ-1", content, filename))

    # Temp file should be deleted after the operation regardless of outcome.
    assert len(captured_temp_path) == 1
    temp_path = captured_temp_path[0]
    assert not os.path.exists(temp_path), (
        f"Temp file {temp_path} was not cleaned up"
    )


@settings(max_examples=10, deadline=None)
@given(content=st.binary(min_size=1, max_size=1024))
def test_temp_file_cleaned_up_on_exception(content: bytes) -> None:
    """Temp file is deleted even when upload raises an exception."""
    tool = _make_tool()
    captured_temp_path: list[str] = []

    async def _failing_execute(issue_key: str, file_path: str, file_name=None):
        captured_temp_path.append(file_path)
        raise RuntimeError("upload failed")

    with patch.object(tool, "execute", _failing_execute):
        with pytest.raises(RuntimeError):
            asyncio.run(tool.execute_from_temp("PROJ-1", content, "f.pdf"))

    assert len(captured_temp_path) == 1
    assert not os.path.exists(captured_temp_path[0]), (
        f"Temp file {captured_temp_path[0]} was not cleaned up after exception"
    )
