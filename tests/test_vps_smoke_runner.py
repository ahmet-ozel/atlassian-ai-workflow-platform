"""Unit tests for platform/scripts/vps_smoke_runner.py chain dependency logic.

Validates:
- R10.6: When JIRA-1 fails, all dependent scenarios (JIRA-2/3/4/5) get
  verdict=manual_pending due to chain dependency.
- R11.5: When MCP_BANNED_TOOLS contains 'confluence_delete_page', CONF-4
  gets verdict=n/a (intentional skip, not a failure).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add scripts directory to sys.path so we can import the module under test.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import vps_smoke_runner  # noqa: E402
from vps_smoke_runner import (  # noqa: E402
    FAIL,
    MANUAL_PENDING,
    NA,
    PASS,
    MCPClient,
    run_confluence_scenarios,
    run_jira_scenarios,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_evidence_dir(tmp_path, monkeypatch):
    """Redirect EVIDENCE_DIR to tmp_path so tests don't write to real dirs."""
    evidence_dir = tmp_path / "vps-test-evidence"
    evidence_dir.mkdir()
    monkeypatch.setattr(vps_smoke_runner, "EVIDENCE_DIR", evidence_dir)

    # Also isolate the open_issue_logger to avoid side effects
    import vps_open_issue_logger

    monkeypatch.setattr(vps_open_issue_logger, "EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(
        vps_open_issue_logger,
        "OPEN_ISSUES_FILE",
        evidence_dir / "open-issues.json",
    )


def _make_failing_response(http_status: int = 500) -> dict:
    """Create a mock MCP response that indicates failure."""
    return {
        "http_status": http_status,
        "latency_ms": 100,
        "result": {"error": "Internal Server Error"},
        "raw_response": '{"error": "Internal Server Error"}',
        "success": False,
    }


def _make_success_response(result: dict | None = None) -> dict:
    """Create a mock MCP response that indicates success."""
    if result is None:
        result = {"key": "JOH-42"}
    return {
        "http_status": 200,
        "latency_ms": 150,
        "result": result,
        "raw_response": json.dumps(result)[:256],
        "success": True,
    }


# ---------------------------------------------------------------------------
# R10.6: Chain dependency — JIRA-1 fail → JIRA-2/3/4/5 manual_pending
# ---------------------------------------------------------------------------


class TestJiraChainDependency:
    """Validates: Requirements R10.6"""

    def test_jira1_fail_cascades_manual_pending(self):
        """When JIRA-1 fails (non-2xx), JIRA-2/3/4/5 all get manual_pending."""
        client = MCPClient()

        # Mock call_tool to always return failure
        client.call_tool = MagicMock(return_value=_make_failing_response(500))

        results = run_jira_scenarios(client)

        assert len(results) == 5

        # JIRA-1 should be fail
        assert results[0]["scenario"] == "JIRA-1"
        assert results[0]["verdict"] == FAIL

        # JIRA-2 through JIRA-5 should all be manual_pending
        for i in range(1, 5):
            assert results[i]["scenario"] == f"JIRA-{i + 1}"
            assert results[i]["verdict"] == MANUAL_PENDING, (
                f"Expected JIRA-{i + 1} to be manual_pending but got "
                f"{results[i]['verdict']}"
            )

    def test_jira1_invalid_key_cascades_manual_pending(self):
        """When JIRA-1 returns success but invalid key, chain breaks."""
        client = MCPClient()

        # Return success but with an invalid issue key (not matching ^JOH-\\d+$)
        bad_key_response = _make_success_response({"key": "INVALID-KEY"})
        client.call_tool = MagicMock(return_value=bad_key_response)

        results = run_jira_scenarios(client)

        assert len(results) == 5

        # JIRA-1 should be fail (key mismatch)
        assert results[0]["scenario"] == "JIRA-1"
        assert results[0]["verdict"] == FAIL

        # JIRA-2 through JIRA-5 should all be manual_pending
        for i in range(1, 5):
            assert results[i]["scenario"] == f"JIRA-{i + 1}"
            assert results[i]["verdict"] == MANUAL_PENDING

    def test_jira1_success_allows_chain_to_continue(self):
        """When JIRA-1 succeeds with valid key, JIRA-2 is NOT manual_pending."""
        client = MCPClient()

        # JIRA-1 returns valid key, JIRA-2 returns search results
        def side_effect(tool_name, arguments):
            if tool_name == "jira_create_issue":
                return _make_success_response({"key": "JOH-99"})
            elif tool_name == "jira_search_issues":
                return _make_success_response({"issues": [{"key": "JOH-99"}]})
            elif tool_name == "jira_add_comment":
                return _make_success_response({"id": "10042"})
            elif tool_name == "jira_transition_issue":
                return _make_success_response({})
            elif tool_name == "jira_get_issue":
                return _make_success_response({"status": {"name": "Done"}})
            elif tool_name == "jira_delete_issue":
                return _make_success_response({})
            return _make_failing_response()

        client.call_tool = MagicMock(side_effect=side_effect)

        results = run_jira_scenarios(client)

        # JIRA-1 should pass
        assert results[0]["scenario"] == "JIRA-1"
        assert results[0]["verdict"] == PASS

        # JIRA-2 should NOT be manual_pending (chain not broken)
        assert results[1]["scenario"] == "JIRA-2"
        assert results[1]["verdict"] != MANUAL_PENDING

    def test_jira1_http_zero_cascades_manual_pending(self):
        """When JIRA-1 gets connection error (http_status=0), chain breaks."""
        client = MCPClient()

        # Simulate connection error
        connection_error = {
            "http_status": 0,
            "latency_ms": 5000,
            "result": {"error": "Connection refused"},
            "raw_response": "Connection refused",
            "success": False,
        }
        client.call_tool = MagicMock(return_value=connection_error)

        results = run_jira_scenarios(client)

        assert results[0]["scenario"] == "JIRA-1"
        assert results[0]["verdict"] == FAIL

        for i in range(1, 5):
            assert results[i]["verdict"] == MANUAL_PENDING


# ---------------------------------------------------------------------------
# R11.5: MCP_BANNED_TOOLS → CONF-4 verdict=n/a
# ---------------------------------------------------------------------------


class TestConfluenceBannedTools:
    """Validates: Requirements R11.5"""

    @patch.object(vps_smoke_runner, "get_banned_tools")
    def test_conf4_na_when_delete_page_banned(self, mock_banned):
        """CONF-4 gets verdict=n/a when confluence_delete_page is banned."""
        mock_banned.return_value = {"confluence_delete_page"}

        client = MCPClient()

        # Mock list_tools to return confluence tools
        client.list_tools = MagicMock(
            return_value=[
                "confluence_get_spaces",
                "confluence_create_page",
                "confluence_update_page",
                "confluence_get_page",
                "confluence_delete_page",
            ]
        )

        # Mock call_tool for CONF-1, CONF-2, CONF-3 to succeed
        def side_effect(tool_name, arguments):
            if tool_name == "confluence_get_spaces":
                return _make_success_response(
                    {"results": [{"key": "JT", "name": "Test Space"}]}
                )
            elif tool_name == "confluence_create_page":
                return _make_success_response({"id": "12345"})
            elif tool_name == "confluence_update_page":
                return _make_success_response({"id": "12345", "version": {"number": 2}})
            elif tool_name == "confluence_get_page":
                return _make_success_response(
                    {
                        "id": "12345",
                        "version": {"number": 2},
                        "body": "Update Verification content here",
                    }
                )
            return _make_failing_response()

        client.call_tool = MagicMock(side_effect=side_effect)

        results = run_confluence_scenarios(client)

        # Find CONF-4 result
        conf4_results = [r for r in results if r["scenario"] == "CONF-4"]
        assert len(conf4_results) == 1

        conf4 = conf4_results[0]
        assert conf4["verdict"] == NA
        assert "MCP_BANNED_TOOLS" in conf4["evidence_excerpt"]

    @patch.object(vps_smoke_runner, "get_banned_tools")
    def test_conf4_not_na_when_delete_page_not_banned(self, mock_banned):
        """CONF-4 does NOT get n/a when confluence_delete_page is not banned."""
        mock_banned.return_value = set()  # No banned tools

        client = MCPClient()

        client.list_tools = MagicMock(
            return_value=[
                "confluence_get_spaces",
                "confluence_create_page",
                "confluence_update_page",
                "confluence_get_page",
                "confluence_delete_page",
            ]
        )

        # All calls succeed, including delete
        def side_effect(tool_name, arguments):
            if tool_name == "confluence_get_spaces":
                return _make_success_response(
                    {"results": [{"key": "JT", "name": "Test Space"}]}
                )
            elif tool_name == "confluence_create_page":
                return _make_success_response({"id": "12345"})
            elif tool_name == "confluence_update_page":
                return _make_success_response({"id": "12345", "version": {"number": 2}})
            elif tool_name == "confluence_get_page":
                # After update: return updated content
                # After delete: return 404
                if arguments.get("page_id") == "12345":
                    # Check if this is a post-delete verification
                    # We'll use call count to differentiate
                    return _make_success_response(
                        {
                            "id": "12345",
                            "version": {"number": 2},
                            "body": "Update Verification content here",
                        }
                    )
                return _make_failing_response(404)
            elif tool_name == "confluence_delete_page":
                return _make_success_response({})
            return _make_failing_response()

        client.call_tool = MagicMock(side_effect=side_effect)

        results = run_confluence_scenarios(client)

        # Find CONF-4 result
        conf4_results = [r for r in results if r["scenario"] == "CONF-4"]
        assert len(conf4_results) == 1

        conf4 = conf4_results[0]
        # Should NOT be n/a since tool is not banned
        assert conf4["verdict"] != NA

    @patch.object(vps_smoke_runner, "get_banned_tools")
    def test_conf4_na_evidence_mentions_intentional(self, mock_banned):
        """CONF-4 n/a evidence notes the restriction is intentional."""
        mock_banned.return_value = {"confluence_delete_page"}

        client = MCPClient()

        client.list_tools = MagicMock(
            return_value=["confluence_get_spaces", "confluence_create_page",
                          "confluence_update_page", "confluence_get_page",
                          "confluence_delete_page"]
        )

        def side_effect(tool_name, arguments):
            if tool_name == "confluence_get_spaces":
                return _make_success_response(
                    {"results": [{"key": "JT", "name": "Test Space"}]}
                )
            elif tool_name == "confluence_create_page":
                return _make_success_response({"id": "12345"})
            elif tool_name == "confluence_update_page":
                return _make_success_response({"id": "12345", "version": {"number": 2}})
            elif tool_name == "confluence_get_page":
                return _make_success_response(
                    {
                        "id": "12345",
                        "version": {"number": 2},
                        "body": "Update Verification content here",
                    }
                )
            return _make_failing_response()

        client.call_tool = MagicMock(side_effect=side_effect)

        results = run_confluence_scenarios(client)

        conf4 = [r for r in results if r["scenario"] == "CONF-4"][0]
        assert conf4["verdict"] == NA
        # The evidence should mention it's intentional, not a failure
        assert "intentional" in conf4["evidence_excerpt"].lower() or \
               "NOT a failure" in conf4["evidence_excerpt"]
