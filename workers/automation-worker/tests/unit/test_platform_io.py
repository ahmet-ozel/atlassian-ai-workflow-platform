"""Unit tests for platform I/O payload shaping."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_WORKER_ROOT = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _WORKER_ROOT.parents[1]
for _path in (
    _WORKER_ROOT / "src",
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from automation_worker.activities.platform_io import (  # noqa: E402
    _issue_payload_to_input,
    _unwrap_mcp_payload,
)
from temporal_shared.messages import AutomationWorkflowInput  # noqa: E402


def test_unwraps_structured_content_result_json_string() -> None:
    issue = {
        "key": "KAN-1",
        "summary": "Live task",
        "description": "---\nai-bot:\n  workflow_type: noop_test\n---",
    }
    result = {
        "structuredContent": {
            "result": json.dumps(issue),
        },
    }

    assert _unwrap_mcp_payload(result) == issue


def test_prepare_input_reads_description_from_mcp_result_string() -> None:
    description = "---\nai-bot:\n  workflow_type: noop_test\n---"
    payload = _unwrap_mcp_payload(
        {
            "structuredContent": {
                "result": json.dumps(
                    {
                        "key": "KAN-2",
                        "summary": "Live task",
                        "description": description,
                    }
                ),
            },
        }
    )
    workflow_input = AutomationWorkflowInput(
        issue_key="KAN-2",
        department_id="payment",
    )

    analysis_input = _issue_payload_to_input(workflow_input, payload, "")

    assert analysis_input.title == "Live task"
    assert analysis_input.description == description
