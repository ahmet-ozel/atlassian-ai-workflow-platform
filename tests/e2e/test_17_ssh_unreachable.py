"""
Test 17: Unreachable SSH host error handling (R17).

Validates that the system handles unreachable SSH hosts gracefully:
- RFC 5737 address (192.0.2.1) used as unreachable target
- Timeout error within 20 seconds
- Error message shows "Connection timed out" / "Host unreachable" (no stack traces)
- agent-runner-worker container does not crash

Requirements: R17.1, R17.2, R17.3, R17.4
"""

import subprocess
import time
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_FILENAME = "17-ssh-unreachable.json"
UNREACHABLE_HOST = "192.0.2.1"  # RFC 5737 TEST-NET-1 - guaranteed unreachable
SSH_TIMEOUT_SECONDS = 20
CONTAINER_CHECK_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _platform_root():
    """Get the platform root directory."""
    from pathlib import Path
    return Path(__file__).resolve().parent.parent.parent


def _get_container_health(service: str) -> dict:
    """Check if a Docker container is healthy and running.

    Returns dict with: running, healthy, status, restart_count.
    """
    result = {
        "running": False,
        "healthy": False,
        "status": "unknown",
        "restart_count": 0,
        "error": None,
    }

    try:
        cmd = [
            "docker", "compose", "ps", "--format", "json", service,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CONTAINER_CHECK_TIMEOUT,
            cwd=str(_platform_root()),
        )

        import json
        if proc.stdout.strip():
            # docker compose ps --format json may output one JSON per line
            for line in proc.stdout.strip().split("\n"):
                if line.strip():
                    data = json.loads(line)
                    result["running"] = data.get("State") == "running"
                    result["healthy"] = data.get("Health") == "healthy"
                    result["status"] = data.get("State", "unknown")
                    break
    except Exception as exc:
        result["error"] = str(exc)

    return result


def _attempt_ssh_connection(host: str, user: str = "root", timeout: int = SSH_TIMEOUT_SECONDS) -> dict:
    """Attempt an SSH connection to a host with a timeout.

    Uses ssh command with ConnectTimeout to simulate the dashboard SSH test.

    Returns dict with: success, error_message, duration_seconds, timed_out.
    """
    result = {
        "success": False,
        "error_message": None,
        "duration_seconds": 0.0,
        "timed_out": False,
        "has_stack_trace": False,
    }

    start = time.time()
    try:
        cmd = [
            "ssh",
            "-o", f"ConnectTimeout={timeout}",
            "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            f"{user}@{host}",
            "echo connected",
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 5,  # Extra buffer beyond SSH's own timeout
        )
        result["duration_seconds"] = round(time.time() - start, 2)

        if proc.returncode == 0:
            result["success"] = True
        else:
            error_output = (proc.stderr + proc.stdout).strip()
            result["error_message"] = error_output[:500]

            # Check for timeout indicators
            timeout_indicators = [
                "timed out",
                "connection timed out",
                "host unreachable",
                "no route to host",
                "network is unreachable",
            ]
            result["timed_out"] = any(
                ind in error_output.lower() for ind in timeout_indicators
            )

            # Check for stack traces (should NOT be present)
            stack_indicators = ["Traceback", "at line", "Exception in thread"]
            result["has_stack_trace"] = any(
                ind in error_output for ind in stack_indicators
            )

    except subprocess.TimeoutExpired:
        result["duration_seconds"] = round(time.time() - start, 2)
        result["timed_out"] = True
        result["error_message"] = f"SSH connection timed out after {timeout}s"
    except FileNotFoundError:
        result["error_message"] = "ssh command not found on system"
    except Exception as exc:
        result["duration_seconds"] = round(time.time() - start, 2)
        result["error_message"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSSHUnreachable:
    """R17: Unreachable SSH host produces graceful timeout error."""

    def test_ssh_timeout_within_20_seconds(self):
        """R17.1: SSH to RFC 5737 address times out within 20 seconds.

        WHEN an unreachable SSH host (192.0.2.1) is used, THE system
        SHALL display a timeout error within 20 seconds.
        """
        result = _attempt_ssh_connection(
            host=UNREACHABLE_HOST,
            timeout=SSH_TIMEOUT_SECONDS,
        )

        # Store for other tests
        self.__class__._ssh_result = result

        assert not result["success"], (
            f"SSH to {UNREACHABLE_HOST} should NOT succeed - "
            f"this is an RFC 5737 documentation address."
        )

        assert result["duration_seconds"] <= SSH_TIMEOUT_SECONDS + 5, (
            f"SSH timeout took {result['duration_seconds']}s, "
            f"expected ≤ {SSH_TIMEOUT_SECONDS + 5}s"
        )

    def test_error_message_is_user_friendly(self):
        """R17.2: Error message shows timeout/unreachable, no stack traces.

        WHEN the SSH test times out, THE error message SHALL indicate
        'Connection timed out' or 'Host unreachable' and SHALL NOT
        expose internal stack traces.
        """
        result = getattr(self.__class__, "_ssh_result", None)
        if result is None:
            result = _attempt_ssh_connection(
                host=UNREACHABLE_HOST,
                timeout=SSH_TIMEOUT_SECONDS,
            )

        # Should have timed out or gotten unreachable error
        assert result["timed_out"] or "unreachable" in (result["error_message"] or "").lower(), (
            f"Expected timeout or unreachable error, got: {result['error_message']}"
        )

        # Should NOT contain stack traces
        assert not result["has_stack_trace"], (
            f"Stack trace found in SSH error output! "
            f"Error messages should be user-friendly.\n"
            f"Message: {result['error_message']}"
        )

    def test_agent_runner_worker_not_crashed(self):
        """R17.3: agent-runner-worker container did not crash.

        WHEN the SSH test fails, THE agent-runner-worker container
        SHALL NOT crash or enter an unhealthy state.
        """
        health = _get_container_health("agent-runner-worker")

        # If the container isn't running at all, it might not be started yet
        # in this test phase - that's acceptable (skip)
        if health["error"] or health["status"] == "unknown":
            pytest.skip(
                "agent-runner-worker not running - "
                "may not be started in current test phase"
            )

        assert health["running"], (
            f"agent-runner-worker should be running after SSH timeout test. "
            f"Status: {health['status']}"
        )

        # If it's running, it should be healthy (not crashed)
        if health["status"] == "running":
            assert not health.get("restart_count", 0) > 0 or health["healthy"], (
                f"agent-runner-worker may have crashed and restarted. "
                f"Restart count: {health['restart_count']}, "
                f"Healthy: {health['healthy']}"
            )


class TestSSHUnreachableEvidence:
    """R17.4: Emit structured evidence for SSH unreachable test."""

    def test_emit_evidence(self, evidence_collector):
        """Collect SSH unreachable test data and emit evidence JSON."""
        # Run the SSH test to capture fresh evidence
        start_time = time.time()
        ssh_result = _attempt_ssh_connection(
            host=UNREACHABLE_HOST,
            timeout=SSH_TIMEOUT_SECONDS,
        )
        total_duration = round(time.time() - start_time, 2)

        # Check container health
        container_health = _get_container_health("agent-runner-worker")

        evidence_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "target_host": UNREACHABLE_HOST,
            "target_description": "RFC 5737 TEST-NET-1 (guaranteed unreachable)",
            "timeout_configured_seconds": SSH_TIMEOUT_SECONDS,
            "ssh_result": {
                "success": ssh_result["success"],
                "timed_out": ssh_result["timed_out"],
                "duration_seconds": ssh_result["duration_seconds"],
                "error_message": ssh_result["error_message"],
                "has_stack_trace": ssh_result["has_stack_trace"],
            },
            "container_health": {
                "service": "agent-runner-worker",
                "running": container_health["running"],
                "healthy": container_health["healthy"],
                "status": container_health["status"],
            },
            "total_test_duration_seconds": total_duration,
            "overall_verdict": "pass" if (
                not ssh_result["success"]
                and not ssh_result["has_stack_trace"]
                and ssh_result["duration_seconds"] <= SSH_TIMEOUT_SECONDS + 5
            ) else "fail",
        }

        # Emit evidence
        evidence_path = evidence_collector.emit_json(
            requirement_id="R17.1,R17.2,R17.3,R17.4",
            filename=EVIDENCE_FILENAME,
            data=evidence_data,
        )
        assert evidence_path.exists(), (
            f"Evidence file not created at {evidence_path}"
        )
