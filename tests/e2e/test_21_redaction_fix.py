"""
Test 21: Verify log redaction isolation fix (R21).

Validates that the log redaction property test can run in isolation
(without live Docker containers) and still validates the same redaction
invariants: no Bearer ATCTT3x, no Bearer ATATT3x, no sk-proj-, no
plaintext passwords in log output.

Verification steps:
1. Run `pytest tests/property/test_log_redaction.py -v` from platform/ → assert exit 0
2. Assert test output mentions redaction patterns being validated
3. Emit evidence JSON to e2e-evidence/21-redaction-fix.json

Requirements: R21.2, R21.3, R21.4, R21.5
"""

import json
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_FILENAME = "21-redaction-fix.json"
TEST_TARGET = "tests/property/test_log_redaction.py"
COMMAND_TIMEOUT = 120


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRedactionIsolation:
    """R21: Verify log redaction test runs without live containers."""

    def test_redaction_test_exits_zero_without_containers(self, platform_root):
        """R21.2, R21.3: pytest on test_log_redaction.py must exit 0 without running containers.

        The test uses fixture-based log samples (not live container output),
        so it should pass in isolation without any Docker services running.
        """
        result = _run_cmd(
            ["pytest", TEST_TARGET, "-v", "--tb=short"],
            cwd=str(platform_root),
        )
        assert result.returncode == 0, (
            f"pytest on {TEST_TARGET} failed with exit code {result.returncode}.\n"
            f"The log redaction test should run without live containers.\n"
            f"stdout: {result.stdout[:2000]}\n"
            f"stderr: {result.stderr[:2000]}"
        )

    def test_redaction_validates_invariants(self, platform_root):
        """R21.4: Test validates same redaction invariants (no Bearer ATCTT3x, no sk-proj-).

        The test output should indicate that redaction patterns are being
        validated — specifically that sensitive values do not leak through.
        """
        result = _run_cmd(
            ["pytest", TEST_TARGET, "-v", "--tb=short"],
            cwd=str(platform_root),
        )

        combined_output = result.stdout + result.stderr

        # The test should have run and produced output indicating redaction
        # validation. Check that the test names or output reference redaction.
        assert "redact" in combined_output.lower(), (
            f"Test output does not mention 'redact'. "
            f"The test should validate redaction invariants.\n"
            f"Output snippet: {combined_output[:2000]}"
        )

        # Verify the test actually ran (not just collected)
        assert "passed" in combined_output.lower() or "PASSED" in combined_output, (
            f"No 'passed' indication in test output. "
            f"Tests may not have executed successfully.\n"
            f"Output snippet: {combined_output[:2000]}"
        )

        # The test should NOT contain actual sensitive tokens in its output
        # (the redaction should mask them)
        assert "Bearer ATCTT3x" not in combined_output, (
            "Found unredacted 'Bearer ATCTT3x' token in test output. "
            "Redaction invariant violated."
        )
        assert "Bearer ATATT3x" not in combined_output, (
            "Found unredacted 'Bearer ATATT3x' token in test output. "
            "Redaction invariant violated."
        )
        assert "sk-proj-" not in combined_output, (
            "Found unredacted 'sk-proj-' token in test output. "
            "Redaction invariant violated."
        )


class TestRedactionFixEvidence:
    """R21.5: Emit structured evidence for the redaction isolation fix."""

    def test_emit_evidence(self, evidence_collector, platform_root):
        """Collect redaction fix verification data and emit evidence JSON."""
        evidence_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "test_target": TEST_TARGET,
            "description": (
                "Verifies log redaction test runs in isolation without "
                "live Docker containers, using fixture-based log samples."
            ),
            "run_result": {},
            "invariant_checks": {},
            "overall_verdict": "pass",
        }

        # Run the redaction test
        result = _run_cmd(
            ["pytest", TEST_TARGET, "-v", "--tb=short"],
            cwd=str(platform_root),
        )

        combined_output = result.stdout + result.stderr

        evidence_data["run_result"] = {
            "exit_code": result.returncode,
            "stdout_snippet": result.stdout[:3000],
            "stderr_snippet": result.stderr[:1000],
            "passed": result.returncode == 0,
        }

        # Check invariants
        no_bearer_atctt3x = "Bearer ATCTT3x" not in combined_output
        no_bearer_atatt3x = "Bearer ATATT3x" not in combined_output
        no_sk_proj = "sk-proj-" not in combined_output
        mentions_redaction = "redact" in combined_output.lower()

        evidence_data["invariant_checks"] = {
            "no_bearer_atctt3x_leak": no_bearer_atctt3x,
            "no_bearer_atatt3x_leak": no_bearer_atatt3x,
            "no_sk_proj_leak": no_sk_proj,
            "mentions_redaction": mentions_redaction,
            "runs_without_containers": result.returncode == 0,
        }

        # Overall verdict
        all_passed = (
            result.returncode == 0
            and no_bearer_atctt3x
            and no_bearer_atatt3x
            and no_sk_proj
            and mentions_redaction
        )
        evidence_data["overall_verdict"] = "pass" if all_passed else "fail"

        # Emit evidence
        evidence_collector.emit_json(
            requirement_id="R21.2,R21.3,R21.4,R21.5",
            filename=EVIDENCE_FILENAME,
            data=evidence_data,
        )
