"""Regression tests for Temporal JSON payloads in output actions."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_WORKER_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = _WORKER_ROOT / "src"
_PLATFORM_ROOT = _WORKER_ROOT.parents[1]
_DB_SHARED_SRC = _PLATFORM_ROOT / "libs" / "db-shared" / "src"

for _candidate in (_SRC_DIR, _DB_SHARED_SRC):
    _path = str(_candidate)
    if _candidate.is_dir() and _path not in sys.path:
        sys.path.insert(0, _path)

from automation_worker.activities.output_actions import (  # noqa: E402
    ExecutionBatchInput,
    execute_output_actions,
    set_mcp_caller,
)


@dataclass
class _FakeCaller:
    calls: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)

    async def call_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
        *,
        dept_id: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        self.calls.append((tool_name, params, dept_id))
        return {"ok": True}


def test_json_decoded_actions_are_normalised_before_execution() -> None:
    caller = _FakeCaller()
    set_mcp_caller(caller)
    payload = ExecutionBatchInput(
        actions=[
            {"type": ["JIRA_COMMENT", "jira_comment"], "params": {"body": "ok"}, "index": "2"},
            {"type": "confluence_create_page", "params": {"title": "T"}, "index": 1},
        ],
        issue_key="KAN-1",
        dept_id="payment",
        workflow_id="wf-1",
    )

    result = asyncio.run(execute_output_actions(payload))

    assert result.all_succeeded is True
    assert [call[0] for call in caller.calls] == [
        "confluence_create_page",
        "jira_add_comment",
    ]


def test_character_list_action_type_is_joined_before_execution() -> None:
    caller = _FakeCaller()
    set_mcp_caller(caller)
    payload = ExecutionBatchInput(
        actions=[
            {
                "type": list("jira_comment"),
                "params": {"issue_key": "KAN-1", "body": "ok"},
                "index": 0,
            },
        ],
        issue_key="KAN-1",
        dept_id="payment",
        workflow_id="wf-1",
    )

    result = asyncio.run(execute_output_actions(payload))

    assert result.all_succeeded is True
    assert [call[0] for call in caller.calls] == ["jira_add_comment"]
