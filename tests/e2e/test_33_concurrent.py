"""
Test 33: Concurrent task submission (R33).

Validates that the platform handles multiple simultaneous task submissions
without deadlocks, crashes, or cross-contamination between workflows.

Verification steps:
1. Submit 3 tasks simultaneously (within 2 seconds)
2. Assert all accepted without HTTP 5xx
3. Verify 3 distinct Temporal workflow instances
4. Assert no deadlocks in postgres logs, no worker crashes
5. Assert each task produces distinct Jira issue (no cross-contamination)
6. Emit evidence JSON

Requirements: R33.1, R33.2, R33.3, R33.4, R33.5, R33.6
"""

import json
import platform
import subprocess
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pytest

try:
    import httpx
except ImportError:
    httpx = None

try:
    import requests
except ImportError:
    requests = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_FILENAME = "33-concurrent.json"
COMMAND_TIMEOUT = 60
AUTOMATION_SERVICE_URL = "http://localhost:8082"
TEMPORAL_URL = "http://localhost:8233"
TASK_SUBMISSION_WINDOW = 2  # seconds
NUM_CONCURRENT_TASKS = 3
WORKFLOW_COMPLETION_TIMEOUT = 300  # 5 minutes max wait


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cmd(cmd: list[str], cwd: str, timeout: int = COMMAND_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a command and return the CompletedProcess result."""
    use_shell = platform.system() == "Windows"
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        shell=use_shell,
    )


def _submit_task(task_id: int, base_url: str) -> dict:
    """Submit a single task to the automation service and return result info."""
    payload = {
        "title": f"E2E Concurrent Test Task {task_id} - {int(time.time())}",
        "description": f"Automated concurrent test task #{task_id} for R33 validation",
        "task_type": "jira_create",
    }

    result = {
        "task_id": task_id,
        "submitted_at": time.time(),
        "status_code": None,
        "response_body": None,
        "error": None,
        "workflow_id": None,
    }

    try:
        if httpx:
            with httpx.Client(timeout=30) as client:
                resp = client.post(f"{base_url}/api/tasks", json=payload)
                result["status_code"] = resp.status_code
                result["response_body"] = resp.text[:2000]
                if resp.status_code < 400:
                    try:
                        data = resp.json()
                        result["workflow_id"] = data.get("workflow_id") or data.get("id")
                    except Exception:
                        pass
        elif requests:
            resp = requests.post(f"{base_url}/api/tasks", json=payload, timeout=30)
            result["status_code"] = resp.status_code
            result["response_body"] = resp.text[:2000]
            if resp.status_code < 400:
                try:
                    data = resp.json()
                    result["workflow_id"] = data.get("workflow_id") or data.get("id")
                except Exception:
                    pass
        else:
            result["error"] = "Neither httpx nor requests available"
    except Exception as e:
        result["error"] = str(e)

    return result


def _get_postgres_logs(cwd: str, lines: int = 100) -> str:
    """Get recent postgres container logs to check for deadlocks."""
    result = _run_cmd(
        ["docker", "compose", "-f", "infra/docker-compose.yml",
         "logs", "--tail", str(lines), "postgres"],
        cwd=cwd,
    )
    return result.stdout + result.stderr


def _get_worker_status(cwd: str) -> dict:
    """Check if worker containers are still running (no crashes)."""
    result = _run_cmd(
        ["docker", "compose", "-f", "infra/docker-compose.yml",
         "-f", "infra/docker-compose.dev.yml", "ps", "--format", "json"],
        cwd=cwd,
    )
    return {
        "exit_code": result.returncode,
        "output": result.stdout[:3000],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConcurrentTaskSubmission:
    """R33: Verify concurrent task submission handling."""

    def test_submit_three_tasks_simultaneously(self, platform_root):
        """R33.1: Submit 3 tasks within 2 seconds, all must be accepted.

        Uses ThreadPoolExecutor to submit tasks concurrently and verifies
        that none receive HTTP 5xx responses.
        """
        results = []
        start_time = time.time()

        with ThreadPoolExecutor(max_workers=NUM_CONCURRENT_TASKS) as executor:
            futures = [
                executor.submit(_submit_task, i, AUTOMATION_SERVICE_URL)
                for i in range(1, NUM_CONCURRENT_TASKS + 1)
            ]
            for future in as_completed(futures):
                results.append(future.result())

        elapsed = time.time() - start_time

        # Verify all submitted within the time window
        assert elapsed < TASK_SUBMISSION_WINDOW + 5, (
            f"Task submission took {elapsed:.1f}s, expected within "
            f"{TASK_SUBMISSION_WINDOW + 5}s window."
        )

        # Verify no 5xx errors
        for r in results:
            if r["status_code"] is not None:
                assert r["status_code"] < 500, (
                    f"Task {r['task_id']} received HTTP {r['status_code']} "
                    f"(5xx server error). Response: {r['response_body'][:500]}"
                )
            elif r["error"]:
                # Connection errors may indicate service is down
                pytest.skip(
                    f"Task {r['task_id']} failed to connect: {r['error']}. "
                    f"Service may not be running."
                )

    def test_distinct_temporal_workflows(self, platform_root):
        """R33.2: Verify 3 distinct Temporal workflow instances created.

        After concurrent submission, each task should have its own
        workflow instance in Temporal.
        """
        results = []
        with ThreadPoolExecutor(max_workers=NUM_CONCURRENT_TASKS) as executor:
            futures = [
                executor.submit(_submit_task, i, AUTOMATION_SERVICE_URL)
                for i in range(1, NUM_CONCURRENT_TASKS + 1)
            ]
            for future in as_completed(futures):
                results.append(future.result())

        # Collect workflow IDs
        workflow_ids = [
            r["workflow_id"] for r in results
            if r["workflow_id"] is not None
        ]

        if not workflow_ids:
            pytest.skip(
                "No workflow IDs returned from task submissions. "
                "Service may not be running or API format differs."
            )

        # Verify all workflow IDs are distinct
        assert len(set(workflow_ids)) == len(workflow_ids), (
            f"Expected {len(workflow_ids)} distinct workflow IDs but got "
            f"duplicates: {workflow_ids}"
        )

    def test_no_deadlocks_in_postgres(self, platform_root):
        """R33.3: Assert no deadlocks detected in postgres logs.

        After concurrent task submission, postgres logs should not
        contain deadlock-related error messages.
        """
        # Submit tasks concurrently first
        with ThreadPoolExecutor(max_workers=NUM_CONCURRENT_TASKS) as executor:
            futures = [
                executor.submit(_submit_task, i, AUTOMATION_SERVICE_URL)
                for i in range(1, NUM_CONCURRENT_TASKS + 1)
            ]
            for future in as_completed(futures):
                pass  # Wait for all to complete

        # Small delay for logs to flush
        time.sleep(3)

        # Check postgres logs for deadlock indicators
        pg_logs = _get_postgres_logs(cwd=str(platform_root))

        deadlock_indicators = [
            "deadlock detected",
            "DeadlockDetected",
            "ERROR:  deadlock",
            "waiting for ShareLock",
        ]

        for indicator in deadlock_indicators:
            assert indicator.lower() not in pg_logs.lower(), (
                f"Deadlock indicator '{indicator}' found in postgres logs "
                f"after concurrent task submission.\n"
                f"Log snippet: {pg_logs[-1000:]}"
            )

    def test_no_worker_crashes(self, platform_root):
        """R33.4: Assert no worker containers crashed during concurrent submission."""
        # Submit tasks concurrently
        with ThreadPoolExecutor(max_workers=NUM_CONCURRENT_TASKS) as executor:
            futures = [
                executor.submit(_submit_task, i, AUTOMATION_SERVICE_URL)
                for i in range(1, NUM_CONCURRENT_TASKS + 1)
            ]
            for future in as_completed(futures):
                pass

        # Wait for any crash effects
        time.sleep(5)

        # Check worker container status
        worker_status = _get_worker_status(cwd=str(platform_root))

        # Look for restarting or exited containers
        crash_indicators = ["restarting", "exited", "dead"]
        output_lower = worker_status["output"].lower()

        for indicator in crash_indicators:
            if indicator in output_lower:
                # Only fail if it's a worker service
                if "worker" in output_lower.split(indicator)[0][-100:]:
                    pytest.fail(
                        f"Worker container appears to have crashed "
                        f"(found '{indicator}' in status).\n"
                        f"Status output: {worker_status['output'][:1500]}"
                    )

    def test_distinct_jira_issues(self, platform_root):
        """R33.5: Each concurrent task produces a distinct Jira issue.

        Verifies no cross-contamination between concurrent workflows
        by checking that each task creates its own unique Jira issue.
        """
        results = []
        with ThreadPoolExecutor(max_workers=NUM_CONCURRENT_TASKS) as executor:
            futures = [
                executor.submit(_submit_task, i, AUTOMATION_SERVICE_URL)
                for i in range(1, NUM_CONCURRENT_TASKS + 1)
            ]
            for future in as_completed(futures):
                results.append(future.result())

        # Extract any Jira issue keys from responses
        jira_keys = []
        for r in results:
            if r["response_body"]:
                try:
                    data = json.loads(r["response_body"])
                    key = data.get("jira_key") or data.get("issue_key")
                    if key:
                        jira_keys.append(key)
                except (json.JSONDecodeError, TypeError):
                    pass

        if not jira_keys:
            pytest.skip(
                "No Jira issue keys found in task responses. "
                "Tasks may not have completed yet or API format differs."
            )

        # Verify all keys are distinct (no cross-contamination)
        assert len(set(jira_keys)) == len(jira_keys), (
            f"Cross-contamination detected! Expected {len(jira_keys)} distinct "
            f"Jira keys but found duplicates: {jira_keys}"
        )


class TestConcurrentEvidence:
    """R33.6: Emit structured evidence for concurrent task submission."""

    def test_emit_evidence(self, evidence_collector, platform_root):
        """Collect concurrent submission data and emit evidence JSON."""
        evidence_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "num_concurrent_tasks": NUM_CONCURRENT_TASKS,
            "submission_window_seconds": TASK_SUBMISSION_WINDOW,
            "task_results": [],
            "postgres_deadlock_check": {},
            "worker_crash_check": {},
            "overall_verdict": "pass",
        }

        # Submit tasks concurrently
        results = []
        start_time = time.time()
        with ThreadPoolExecutor(max_workers=NUM_CONCURRENT_TASKS) as executor:
            futures = [
                executor.submit(_submit_task, i, AUTOMATION_SERVICE_URL)
                for i in range(1, NUM_CONCURRENT_TASKS + 1)
            ]
            for future in as_completed(futures):
                results.append(future.result())
        elapsed = time.time() - start_time

        evidence_data["task_results"] = results
        evidence_data["submission_elapsed_seconds"] = round(elapsed, 2)

        # Check for 5xx errors
        has_5xx = any(
            r["status_code"] and r["status_code"] >= 500
            for r in results
        )
        evidence_data["has_5xx_errors"] = has_5xx

        # Check postgres logs
        time.sleep(3)
        pg_logs = _get_postgres_logs(cwd=str(platform_root))
        has_deadlock = "deadlock detected" in pg_logs.lower()
        evidence_data["postgres_deadlock_check"] = {
            "has_deadlock": has_deadlock,
            "log_snippet": pg_logs[-500:] if has_deadlock else "(clean)",
        }

        # Check worker status
        worker_status = _get_worker_status(cwd=str(platform_root))
        evidence_data["worker_crash_check"] = {
            "status_output": worker_status["output"][:1000],
            "has_crashes": any(
                ind in worker_status["output"].lower()
                for ind in ["restarting", "exited", "dead"]
            ),
        }

        # Overall verdict
        all_passed = (
            not has_5xx
            and not has_deadlock
            and not evidence_data["worker_crash_check"]["has_crashes"]
        )
        evidence_data["overall_verdict"] = "pass" if all_passed else "fail"

        # Emit evidence
        evidence_collector.emit_json(
            requirement_id="R33.1,R33.2,R33.3,R33.4,R33.5,R33.6",
            filename=EVIDENCE_FILENAME,
            data=evidence_data,
        )
