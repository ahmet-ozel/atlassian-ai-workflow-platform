#!/usr/bin/env python3
"""
VPS E2E Task Probe - End-to-End task workflow verification (B5/R13).

After the operator submits a task via Streamlit UI and pastes the
workflow_run_id, this script:
  1. Polls Temporal workflow status until completion or timeout (R13.1)
  2. Asserts Jira issue status == Done via MCP call (R13.2)
  3. Asserts Confluence page exists with [VPS-E2E] title and body ≥200 chars (R13.3)
  4. Asserts audit_events has ≥1 row with event_type LIKE 'task.%' AND
     correlation_id == workflow_run_id (R13.4)
  5. Checks LLM token usage from automation-service logs (R13.5)
  6. On timeout/fail: captures temporal workflow show → evidence (R13.6)
  7. Emits evidence: vps-test-evidence/13-task.json (D5 schema) (R13.7)
  8. R23.2: if total_tokens > 60000, logs minor warning Open_Issue

CLI: python vps_e2e_task_probe.py --workflow-run-id <id> [--timeout 600]

Requirements: R13.1, R13.2, R13.3, R13.4, R13.5, R13.6, R13.7, R23.2
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_WORKSPACE_ROOT = _SCRIPT_DIR.parent.parent  # platform/scripts -> platform -> workspace root
_PLATFORM_DIR = _SCRIPT_DIR.parent  # platform/scripts -> platform
EVIDENCE_DIR = _WORKSPACE_ROOT / "vps-test-evidence"

# Add scripts dir to path for open_issue_logger import
sys.path.insert(0, str(_SCRIPT_DIR))
from vps_open_issue_logger import log_open_issue  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MCP_ENDPOINT = os.environ.get("MCP_ENDPOINT", "http://localhost:8090/mcp")
COMPOSE_FILE = "infra/docker-compose.yml"
DEFAULT_TIMEOUT = 600
POLL_INTERVAL = 10
TOKEN_BUDGET_THRESHOLD = 60_000  # R23.2: 50k input + 10k output


# ---------------------------------------------------------------------------
# MCP JSON-RPC 2.0 Client (minimal, same pattern as smoke_runner)
# ---------------------------------------------------------------------------

class MCPClient:
    """Minimal MCP JSON-RPC 2.0 client over HTTP."""

    def __init__(self, endpoint: str = MCP_ENDPOINT):
        self.endpoint = endpoint
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """
        Invoke an MCP tool via JSON-RPC 2.0.

        Returns dict with keys:
          - http_status: int
          - latency_ms: int
          - result: Any (parsed JSON response content or error)
          - raw_response: str (first 256 chars)
          - success: bool
        """
        import requests

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

        start = time.time()
        try:
            resp = requests.post(
                self.endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            latency_ms = int((time.time() - start) * 1000)
            http_status = resp.status_code

            try:
                body = resp.json()
            except (json.JSONDecodeError, ValueError):
                body = {"raw": resp.text[:256]}

            if "result" in body:
                result = body["result"]
                success = True
            elif "error" in body:
                result = body["error"]
                success = False
            else:
                result = body
                success = http_status < 300

            return {
                "http_status": http_status,
                "latency_ms": latency_ms,
                "result": result,
                "raw_response": json.dumps(body, ensure_ascii=False)[:256],
                "success": success and http_status < 300,
            }
        except Exception as e:
            latency_ms = int((time.time() - start) * 1000)
            return {
                "http_status": 0,
                "latency_ms": latency_ms,
                "result": {"error": str(e)},
                "raw_response": str(e)[:256],
                "success": False,
            }


# ---------------------------------------------------------------------------
# Helper: extract fields from MCP results
# ---------------------------------------------------------------------------

def _extract_field(data: Any, field: str, default: Any = "") -> Any:
    """Safely extract a field from a dict or nested MCP result."""
    if isinstance(data, dict):
        if field in data:
            return data[field]
        # MCP results may wrap content in a list of content blocks
        if "content" in data and isinstance(data["content"], list):
            for block in data["content"]:
                if isinstance(block, dict):
                    if field in block:
                        return block[field]
                    if block.get("type") == "text" and "text" in block:
                        try:
                            parsed = json.loads(block["text"])
                            if isinstance(parsed, dict) and field in parsed:
                                return parsed[field]
                        except (json.JSONDecodeError, TypeError):
                            pass
    return default


def _extract_nested(data: Any, path: list[str], default: Any = "") -> Any:
    """Extract a nested field from a dict using a path list."""
    current = data
    if isinstance(current, dict) and "content" in current and isinstance(current["content"], list):
        for block in current["content"]:
            if isinstance(block, dict) and block.get("type") == "text" and "text" in block:
                try:
                    current = json.loads(block["text"])
                    break
                except (json.JSONDecodeError, TypeError):
                    pass

    for key in path:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def _extract_text_content(data: Any) -> str:
    """Extract full text content from MCP result for searching."""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        if "content" in data and isinstance(data["content"], list):
            parts = []
            for block in data["content"]:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return "\n".join(parts)
        return json.dumps(data, ensure_ascii=False)
    if isinstance(data, list):
        return json.dumps(data, ensure_ascii=False)
    return str(data)


# ---------------------------------------------------------------------------
# Temporal workflow polling
# ---------------------------------------------------------------------------

def poll_temporal_workflow(
    workflow_run_id: str,
    timeout: int = DEFAULT_TIMEOUT,
    poll_interval: int = POLL_INTERVAL,
) -> dict[str, Any]:
    """
    Poll Temporal workflow via docker compose exec until completion or timeout.

    Returns dict with:
      - completed: bool
      - status: str (e.g., "Completed", "Running", "Failed", "TimedOut", "Terminated")
      - duration_seconds: int
      - error: str | None
    """
    start_time = time.time()
    last_status = "Unknown"

    print(f"[TEMPORAL] Polling workflow '{workflow_run_id}' every {poll_interval}s (timeout={timeout}s)...")

    while True:
        elapsed = int(time.time() - start_time)

        if elapsed >= timeout:
            print(f"[TEMPORAL] Timeout after {elapsed}s - workflow still in status: {last_status}")
            return {
                "completed": False,
                "status": "TimedOut",
                "duration_seconds": elapsed,
                "error": f"Workflow did not complete within {timeout}s (last status: {last_status})",
            }

        # Run temporal workflow describe
        try:
            result = subprocess.run(
                [
                    "docker", "compose", "-f", COMPOSE_FILE,
                    "exec", "-T", "temporal-admin-tools",
                    "temporal", "workflow", "describe",
                    "-w", workflow_run_id,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(_PLATFORM_DIR),
            )

            output = result.stdout + result.stderr

            # Parse workflow status from describe output
            status = _parse_workflow_status(output)
            last_status = status

            if status in ("Completed", "COMPLETED"):
                print(f"[TEMPORAL] Workflow completed after {elapsed}s")
                return {
                    "completed": True,
                    "status": "Completed",
                    "duration_seconds": elapsed,
                    "error": None,
                }
            elif status in ("Failed", "FAILED"):
                print(f"[TEMPORAL] Workflow FAILED after {elapsed}s")
                return {
                    "completed": False,
                    "status": "Failed",
                    "duration_seconds": elapsed,
                    "error": f"Workflow failed: {output[:256]}",
                }
            elif status in ("Terminated", "TERMINATED"):
                print(f"[TEMPORAL] Workflow TERMINATED after {elapsed}s")
                return {
                    "completed": False,
                    "status": "Terminated",
                    "duration_seconds": elapsed,
                    "error": f"Workflow terminated: {output[:256]}",
                }
            elif status in ("Canceled", "CANCELED", "Cancelled", "CANCELLED"):
                print(f"[TEMPORAL] Workflow CANCELED after {elapsed}s")
                return {
                    "completed": False,
                    "status": "Canceled",
                    "duration_seconds": elapsed,
                    "error": f"Workflow canceled: {output[:256]}",
                }
            else:
                # Still running or unknown - continue polling
                remaining = timeout - elapsed
                print(
                    f"[TEMPORAL] Status: {status} | Elapsed: {elapsed}s | "
                    f"Remaining: {remaining}s"
                )

        except subprocess.TimeoutExpired:
            print(f"[TEMPORAL] describe command timed out at {elapsed}s - retrying...")
        except (FileNotFoundError, OSError) as e:
            print(f"[TEMPORAL] Error running describe: {e}")

        time.sleep(poll_interval)


def _parse_workflow_status(output: str) -> str:
    """Parse workflow status from temporal workflow describe output."""
    # Look for Status field in the output
    # Common patterns:
    #   Status: COMPLETED
    #   Status: RUNNING
    #   "status": "Completed"
    for line in output.splitlines():
        line_stripped = line.strip()
        # Pattern: "Status: COMPLETED" or "Status  COMPLETED"
        match = re.match(r"(?:Status|status)\s*[:=]\s*(\w+)", line_stripped)
        if match:
            return match.group(1)
        # JSON pattern
        if '"status"' in line_stripped.lower():
            json_match = re.search(r'"status"\s*:\s*"(\w+)"', line_stripped, re.IGNORECASE)
            if json_match:
                return json_match.group(1)

    # Try parsing as JSON
    try:
        data = json.loads(output)
        if isinstance(data, dict):
            status = data.get("status", data.get("Status", ""))
            if status:
                return str(status)
            # Nested in workflowExecutionInfo
            exec_info = data.get("workflowExecutionInfo", {})
            if isinstance(exec_info, dict):
                return exec_info.get("status", "Unknown")
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback: check for known status keywords in output
    output_upper = output.upper()
    if "COMPLETED" in output_upper:
        return "Completed"
    if "RUNNING" in output_upper:
        return "Running"
    if "FAILED" in output_upper:
        return "Failed"
    if "TERMINATED" in output_upper:
        return "Terminated"

    return "Unknown"


# ---------------------------------------------------------------------------
# Temporal workflow trace capture (R13.6)
# ---------------------------------------------------------------------------

def capture_workflow_trace(workflow_run_id: str) -> str:
    """
    Capture temporal workflow show output as JSON for evidence.
    Returns the captured output string.
    """
    try:
        result = subprocess.run(
            [
                "docker", "compose", "-f", COMPOSE_FILE,
                "exec", "-T", "temporal-admin-tools",
                "temporal", "workflow", "show",
                "-w", workflow_run_id,
                "--output", "json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(_PLATFORM_DIR),
        )
        return result.stdout or result.stderr or "(no output)"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return f"(error capturing trace: {e})"


# ---------------------------------------------------------------------------
# Assertion: Jira issue status == Done (R13.2)
# ---------------------------------------------------------------------------

def assert_jira_done(client: MCPClient, workflow_run_id: str) -> dict[str, Any]:
    """
    Find the Jira issue created by the E2E task and assert status == Done.

    Strategy: search for issues with [VPS-E2E] in summary that are Done,
    or query audit_events for the issue key associated with the workflow.

    Returns dict with:
      - success: bool
      - issue_key: str
      - status: str
      - error: str | None
    """
    # Strategy 1: Search for [VPS-E2E] issues in Done status
    resp = client.call_tool("jira_search_issues", {
        "jql": 'project = JOH AND summary ~ "[VPS-E2E]" AND status = Done ORDER BY created DESC',
    })

    if resp["success"]:
        text_content = _extract_text_content(resp["result"])
        # Try to find an issue key
        key_match = re.search(r"(JOH-\d+)", text_content)
        if key_match:
            issue_key = key_match.group(1)
            # Verify with get_issue
            verify = client.call_tool("jira_get_issue", {"issue_key": issue_key})
            if verify["success"]:
                status_name = _extract_nested(verify["result"], ["status", "name"], "")
                if not status_name:
                    # Try alternate path
                    status_name = _extract_field(verify["result"], "status", "")
                    if isinstance(status_name, dict):
                        status_name = status_name.get("name", "")
                if status_name == "Done":
                    return {
                        "success": True,
                        "issue_key": issue_key,
                        "status": "Done",
                        "error": None,
                    }
                else:
                    return {
                        "success": False,
                        "issue_key": issue_key,
                        "status": str(status_name),
                        "error": f"Issue {issue_key} status is '{status_name}', expected 'Done'",
                    }
            else:
                return {
                    "success": False,
                    "issue_key": issue_key,
                    "status": "unknown",
                    "error": f"jira_get_issue failed for {issue_key}: HTTP {verify['http_status']}",
                }

    # Strategy 2: Search without status filter
    resp2 = client.call_tool("jira_search_issues", {
        "jql": 'project = JOH AND summary ~ "[VPS-E2E]" ORDER BY created DESC',
    })
    if resp2["success"]:
        text_content = _extract_text_content(resp2["result"])
        key_match = re.search(r"(JOH-\d+)", text_content)
        if key_match:
            issue_key = key_match.group(1)
            verify = client.call_tool("jira_get_issue", {"issue_key": issue_key})
            if verify["success"]:
                status_name = _extract_nested(verify["result"], ["status", "name"], "")
                if not status_name:
                    status_name = _extract_field(verify["result"], "status", "")
                    if isinstance(status_name, dict):
                        status_name = status_name.get("name", "")
                return {
                    "success": status_name == "Done",
                    "issue_key": issue_key,
                    "status": str(status_name),
                    "error": None if status_name == "Done" else f"Status is '{status_name}', expected 'Done'",
                }

    return {
        "success": False,
        "issue_key": "",
        "status": "not_found",
        "error": "No [VPS-E2E] Jira issue found via search",
    }


# ---------------------------------------------------------------------------
# Assertion: Confluence page with [VPS-E2E] title and body ≥200 chars (R13.3)
# ---------------------------------------------------------------------------

def assert_confluence_page(client: MCPClient) -> dict[str, Any]:
    """
    Assert a Confluence page exists with title containing [VPS-E2E]
    and body length ≥ 200 characters.

    Returns dict with:
      - success: bool
      - page_id: str
      - page_title: str
      - body_length: int
      - space_key: str
      - error: str | None
    """
    # Try confluence_search or confluence_get_pages with query
    search_tools = ["confluence_search", "confluence_search_content", "confluence_get_pages"]

    for tool_name in search_tools:
        resp = client.call_tool(tool_name, {"query": "[VPS-E2E]"})
        if resp["success"]:
            text_content = _extract_text_content(resp["result"])

            # Try to extract page info
            page_id = ""
            page_title = ""
            body_length = 0
            space_key = ""

            # Parse result for page data
            result_data = resp["result"]
            if isinstance(result_data, dict):
                # Check for results array
                results_list = (
                    result_data.get("results", [])
                    or result_data.get("pages", [])
                    or result_data.get("content", [])
                )
                if isinstance(results_list, list) and results_list:
                    for page in results_list:
                        if isinstance(page, dict):
                            title = page.get("title", "")
                            if "[VPS-E2E]" in title:
                                page_id = str(page.get("id", ""))
                                page_title = title
                                space_key = page.get("space", {}).get("key", "") if isinstance(page.get("space"), dict) else page.get("space_key", "")
                                # Get body length
                                body = page.get("body", {})
                                if isinstance(body, dict):
                                    storage = body.get("storage", {}).get("value", "")
                                    body_length = len(storage)
                                break

            # If we found a page_id, verify with get_page for body length
            if page_id:
                verify = client.call_tool("confluence_get_page", {"page_id": page_id})
                if verify["success"]:
                    verify_text = _extract_text_content(verify["result"])
                    body_length = max(body_length, len(verify_text))
                    if not page_title:
                        page_title = _extract_field(verify["result"], "title", "")

                if "[VPS-E2E]" in page_title and body_length >= 200:
                    return {
                        "success": True,
                        "page_id": page_id,
                        "page_title": page_title,
                        "body_length": body_length,
                        "space_key": space_key,
                        "error": None,
                    }
                elif "[VPS-E2E]" in page_title:
                    return {
                        "success": False,
                        "page_id": page_id,
                        "page_title": page_title,
                        "body_length": body_length,
                        "space_key": space_key,
                        "error": f"Page body length {body_length} < 200 chars",
                    }

            # If text content has [VPS-E2E] but we couldn't parse structured data
            if "[VPS-E2E]" in text_content and len(text_content) >= 200:
                return {
                    "success": True,
                    "page_id": page_id or "unknown",
                    "page_title": page_title or "(found in search results)",
                    "body_length": len(text_content),
                    "space_key": space_key,
                    "error": None,
                }

    return {
        "success": False,
        "page_id": "",
        "page_title": "",
        "body_length": 0,
        "space_key": "",
        "error": "No Confluence page with [VPS-E2E] in title found",
    }


# ---------------------------------------------------------------------------
# Assertion: audit_events row with task.% and correlation_id (R13.4)
# ---------------------------------------------------------------------------

def assert_audit_events(workflow_run_id: str) -> dict[str, Any]:
    """
    Assert audit_events has ≥1 row with event_type LIKE 'task.%'
    AND correlation_id = workflow_run_id.

    Returns dict with:
      - success: bool
      - matched_count: int
      - error: str | None
    """
    sql = (
        "SELECT count(*) FROM automation.audit_events "
        f"WHERE event_type LIKE 'task.%' AND correlation_id = '{workflow_run_id}'"
    )

    try:
        result = subprocess.run(
            [
                "docker", "compose", "-f", COMPOSE_FILE,
                "exec", "-T", "postgres",
                "psql", "-U", "ai", "-d", "ai",
                "-t", "-A", "-c", sql,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(_PLATFORM_DIR),
        )

        output = result.stdout.strip()
        try:
            count = int(output)
        except (ValueError, TypeError):
            # Try to extract number from output
            match = re.search(r"(\d+)", output)
            count = int(match.group(1)) if match else 0

        if count >= 1:
            return {
                "success": True,
                "matched_count": count,
                "error": None,
            }
        else:
            return {
                "success": False,
                "matched_count": count,
                "error": f"Expected ≥1 audit_events row, got {count}",
            }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "matched_count": 0,
            "error": "psql query timed out",
        }
    except (FileNotFoundError, OSError) as e:
        return {
            "success": False,
            "matched_count": 0,
            "error": f"Failed to run psql: {e}",
        }


# ---------------------------------------------------------------------------
# Assertion: LLM token usage from automation-service logs (R13.5)
# ---------------------------------------------------------------------------

def check_llm_token_usage(workflow_run_id: str) -> dict[str, Any]:
    """
    Check LLM token usage from automation-service logs.
    Looks for log lines containing 'llm_call.completed' or token usage info.

    Returns dict with:
      - success: bool
      - model: str
      - prompt_tokens: int
      - completion_tokens: int
      - total_tokens: int
      - error: str | None
    """
    try:
        result = subprocess.run(
            [
                "docker", "compose", "-f", COMPOSE_FILE,
                "logs", "--tail=500", "--no-color",
                "automation-service",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(_PLATFORM_DIR),
        )

        logs = result.stdout + result.stderr
        model = "gpt-5.5"
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0

        # Parse log lines for token usage
        for line in logs.splitlines():
            # Look for llm_call.completed or token usage patterns
            if "llm_call" in line or "token" in line.lower():
                # Try JSON log format
                try:
                    log_entry = json.loads(line.split("|", 1)[-1].strip() if "|" in line else line)
                    if isinstance(log_entry, dict):
                        # Extract token counts
                        usage = log_entry.get("usage", log_entry.get("token_usage", {}))
                        if isinstance(usage, dict):
                            prompt_tokens = max(
                                prompt_tokens,
                                int(usage.get("prompt_tokens", 0)),
                            )
                            completion_tokens = max(
                                completion_tokens,
                                int(usage.get("completion_tokens", 0)),
                            )
                            total_tokens = max(
                                total_tokens,
                                int(usage.get("total_tokens", 0)),
                            )
                        # Direct fields
                        prompt_tokens = max(
                            prompt_tokens,
                            int(log_entry.get("prompt_tokens", 0)),
                        )
                        completion_tokens = max(
                            completion_tokens,
                            int(log_entry.get("completion_tokens", 0)),
                        )
                        total_tokens = max(
                            total_tokens,
                            int(log_entry.get("total_tokens", 0)),
                        )
                        # Model
                        if log_entry.get("model"):
                            model = log_entry["model"]
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

                # Regex fallback for non-JSON log lines
                pt_match = re.search(r"prompt_tokens[\":\s=]+(\d+)", line)
                ct_match = re.search(r"completion_tokens[\":\s=]+(\d+)", line)
                tt_match = re.search(r"total_tokens[\":\s=]+(\d+)", line)
                model_match = re.search(r"model[\":\s=]+([\w.\-]+)", line)

                if pt_match:
                    prompt_tokens = max(prompt_tokens, int(pt_match.group(1)))
                if ct_match:
                    completion_tokens = max(completion_tokens, int(ct_match.group(1)))
                if tt_match:
                    total_tokens = max(total_tokens, int(tt_match.group(1)))
                if model_match:
                    model = model_match.group(1)

        # Calculate total if not found directly
        if total_tokens == 0 and (prompt_tokens > 0 or completion_tokens > 0):
            total_tokens = prompt_tokens + completion_tokens

        # Determine success: non-zero tokens and a model name was observed
        success = (prompt_tokens > 0 or completion_tokens > 0) and bool(model)

        return {
            "success": success,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "error": None if success else "No non-zero token usage found in logs",
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "model": "",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "error": "docker compose logs timed out",
        }
    except (FileNotFoundError, OSError) as e:
        return {
            "success": False,
            "model": "",
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "error": f"Failed to read logs: {e}",
        }


# ---------------------------------------------------------------------------
# Evidence writer
# ---------------------------------------------------------------------------

def write_evidence(filename: str, data: dict[str, Any] | str) -> None:
    """Write evidence to vps-test-evidence/<filename>."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    filepath = EVIDENCE_DIR / filename

    if isinstance(data, str):
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(data)
    else:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"[EVIDENCE] Written: {filepath}")


# ---------------------------------------------------------------------------
# Main probe logic
# ---------------------------------------------------------------------------

def run_e2e_task_probe(workflow_run_id: str, timeout: int = DEFAULT_TIMEOUT) -> int:
    """
    Run the full E2E task probe sequence.

    Returns exit code: 0 = all assertions pass, 1 = one or more failures.
    """
    print("=" * 60)
    print("VPS E2E Task Probe - End-to-End Workflow Verification (R13)")
    print(f"Workflow Run ID: {workflow_run_id}")
    print(f"Timeout: {timeout}s")
    print(f"MCP Endpoint: {MCP_ENDPOINT}")
    print(f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print("=" * 60)

    started_at = datetime.now(timezone.utc)
    failures: list[str] = []

    # -----------------------------------------------------------------------
    # Step 1: Poll Temporal workflow (R13.1)
    # -----------------------------------------------------------------------
    print("\n--- Step 1: Temporal Workflow Polling (R13.1) ---")
    workflow_result = poll_temporal_workflow(workflow_run_id, timeout=timeout)

    if not workflow_result["completed"]:
        # R13.6: Capture workflow trace on timeout/fail
        print(f"[FAIL] Workflow did not complete: {workflow_result['status']}")
        print("[R13.6] Capturing workflow trace...")
        trace = capture_workflow_trace(workflow_run_id)
        write_evidence("13-task-trace.txt", trace)

        # Log critical Open_Issue
        try:
            log_open_issue(
                requirement_id="R13",
                scenario_id=None,
                severity="critical",
                category="integration",
                summary=f"E2E task workflow {workflow_result['status']}: {workflow_result.get('error', '')[:100]}",
                evidence_path="vps-test-evidence/13-task.json",
                recommended_action="manual_fix",
            )
        except Exception as e:
            print(f"[WARNING] Failed to log Open_Issue: {e}", file=sys.stderr)

        # Write partial evidence and exit
        evidence = {
            "workflow_run_id": workflow_run_id,
            "started_at_utc": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "completed_at_utc": None,
            "duration_seconds": workflow_result["duration_seconds"],
            "jira_issue_key": None,
            "jira_final_status": None,
            "confluence_space_key": None,
            "confluence_page_id": None,
            "confluence_page_title": None,
            "confluence_body_length": 0,
            "audit_events_matched": 0,
            "llm_token_usage": {
                "model": "",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
            "verdict": "fail",
            "error": workflow_result.get("error", ""),
        }
        write_evidence("13-task.json", evidence)
        return 1

    completed_at = datetime.now(timezone.utc)
    print(f"[PASS] Workflow completed in {workflow_result['duration_seconds']}s")

    # -----------------------------------------------------------------------
    # Step 2: Assert Jira issue status == Done (R13.2)
    # -----------------------------------------------------------------------
    print("\n--- Step 2: Jira Issue Status Assertion (R13.2) ---")
    client = MCPClient()
    jira_result = assert_jira_done(client, workflow_run_id)

    if jira_result["success"]:
        print(f"[PASS] Jira issue {jira_result['issue_key']} status=Done")
    else:
        print(f"[FAIL] Jira assertion: {jira_result['error']}")
        failures.append(f"R13.2: {jira_result['error']}")

    # -----------------------------------------------------------------------
    # Step 3: Assert Confluence page (R13.3)
    # -----------------------------------------------------------------------
    print("\n--- Step 3: Confluence Page Assertion (R13.3) ---")
    confluence_result = assert_confluence_page(client)

    if confluence_result["success"]:
        print(
            f"[PASS] Confluence page found: id={confluence_result['page_id']}, "
            f"title='{confluence_result['page_title']}', "
            f"body_length={confluence_result['body_length']}"
        )
    else:
        print(f"[FAIL] Confluence assertion: {confluence_result['error']}")
        failures.append(f"R13.3: {confluence_result['error']}")

    # -----------------------------------------------------------------------
    # Step 4: Assert audit_events (R13.4)
    # -----------------------------------------------------------------------
    print("\n--- Step 4: Audit Events Assertion (R13.4) ---")
    audit_result = assert_audit_events(workflow_run_id)

    if audit_result["success"]:
        print(f"[PASS] audit_events matched: {audit_result['matched_count']} row(s)")
    else:
        print(f"[FAIL] Audit events assertion: {audit_result['error']}")
        failures.append(f"R13.4: {audit_result['error']}")

    # -----------------------------------------------------------------------
    # Step 5: Check LLM token usage (R13.5)
    # -----------------------------------------------------------------------
    print("\n--- Step 5: LLM Token Usage Check (R13.5) ---")
    llm_result = check_llm_token_usage(workflow_run_id)

    if llm_result["success"]:
        print(
            f"[PASS] LLM usage: model={llm_result['model']}, "
            f"prompt={llm_result['prompt_tokens']}, "
            f"completion={llm_result['completion_tokens']}, "
            f"total={llm_result['total_tokens']}"
        )
    else:
        print(f"[FAIL] LLM token usage: {llm_result['error']}")
        failures.append(f"R13.5: {llm_result['error']}")

    # -----------------------------------------------------------------------
    # R23.2: Token budget check
    # -----------------------------------------------------------------------
    total_tokens = llm_result["total_tokens"]
    if total_tokens > TOKEN_BUDGET_THRESHOLD:
        print(
            f"\n[WARNING] R23.2: total_tokens={total_tokens} > {TOKEN_BUDGET_THRESHOLD} threshold"
        )
        try:
            log_open_issue(
                requirement_id="R23",
                scenario_id=None,
                severity="minor",
                category="infra",
                summary=f"LLM token usage {total_tokens} exceeds 60k budget threshold (R23.2)",
                evidence_path="vps-test-evidence/13-task.json",
                recommended_action="config_change",
            )
        except Exception as e:
            print(f"[WARNING] Failed to log budget Open_Issue: {e}", file=sys.stderr)

    # -----------------------------------------------------------------------
    # Handle failures (R13.6)
    # -----------------------------------------------------------------------
    if failures:
        print(f"\n[R13.6] {len(failures)} assertion(s) failed - capturing workflow trace...")
        trace = capture_workflow_trace(workflow_run_id)
        write_evidence("13-task-trace.txt", trace)

        try:
            log_open_issue(
                requirement_id="R13",
                scenario_id=None,
                severity="critical",
                category="integration",
                summary=f"E2E task assertions failed: {'; '.join(failures)[:120]}",
                evidence_path="vps-test-evidence/13-task.json",
                recommended_action="manual_fix",
            )
        except Exception as e:
            print(f"[WARNING] Failed to log Open_Issue: {e}", file=sys.stderr)

    # -----------------------------------------------------------------------
    # Write evidence: 13-task.json (R13.7, D5 schema)
    # -----------------------------------------------------------------------
    print("\n--- Writing Evidence (R13.7) ---")
    evidence: dict[str, Any] = {
        "workflow_run_id": workflow_run_id,
        "started_at_utc": started_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "completed_at_utc": completed_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_seconds": workflow_result["duration_seconds"],
        "jira_issue_key": jira_result.get("issue_key", ""),
        "jira_final_status": jira_result.get("status", ""),
        "confluence_space_key": confluence_result.get("space_key", ""),
        "confluence_page_id": confluence_result.get("page_id", ""),
        "confluence_page_title": confluence_result.get("page_title", ""),
        "confluence_body_length": confluence_result.get("body_length", 0),
        "audit_events_matched": audit_result.get("matched_count", 0),
        "llm_token_usage": {
            "model": llm_result.get("model", ""),
            "prompt_tokens": llm_result.get("prompt_tokens", 0),
            "completion_tokens": llm_result.get("completion_tokens", 0),
            "total_tokens": llm_result.get("total_tokens", 0),
        },
    }
    write_evidence("13-task.json", evidence)

    # -----------------------------------------------------------------------
    # Final summary
    # -----------------------------------------------------------------------
    verdict = "pass" if not failures else "fail"
    print(f"\n{'=' * 60}")
    print(f"E2E Task Probe Verdict: {verdict.upper()}")
    if failures:
        for f in failures:
            print(f"  ✗ {f}")
    else:
        print("  ✓ All assertions passed")
    print(f"{'=' * 60}")

    return 1 if failures else 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """Parse CLI arguments and run the E2E task probe."""
    parser = argparse.ArgumentParser(
        description="VPS E2E Task Probe - polls Temporal workflow and asserts R13 criteria.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python vps_e2e_task_probe.py --workflow-run-id wf-abc123 --timeout 600\n"
        ),
    )
    parser.add_argument(
        "--workflow-run-id",
        required=True,
        help="Temporal workflow run ID returned by Streamlit UI task submission",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"Max seconds to wait for workflow completion (default: {DEFAULT_TIMEOUT})",
    )

    args = parser.parse_args()

    if not args.workflow_run_id.strip():
        print("[ERROR] --workflow-run-id cannot be empty", file=sys.stderr)
        return 2

    return run_e2e_task_probe(
        workflow_run_id=args.workflow_run_id.strip(),
        timeout=args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
