"""Unit tests for AgentRunner Jira activities."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_WORKER_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC_DIR = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


@pytest.mark.asyncio
async def test_jira_add_comment_uses_mcp_body_argument() -> None:
    from activities.jira import jira_add_comment

    call = AsyncMock(return_value={"content": [{"text": "{}"}]})
    with patch("activities.jira.call_mcp_tool", call), patch(
        "activities.jira.activity.heartbeat"
    ):
        await jira_add_comment("KAN-1", "done", "test")

    call.assert_awaited_once_with(
        "jira_add_comment",
        {"issue_key": "KAN-1", "body": "done"},
        dept_id="test",
        service="jira",
    )
