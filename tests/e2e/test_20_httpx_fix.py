"""
Test 20: Verify httpx import fix (R20).

Validates that the httpx dependency has been correctly added to the test
environment so that `tests/property/test_log_redaction.py` can be collected
and executed without ImportError.

Verification steps:
1. Run `pytest tests/property/test_log_redaction.py --collect-only` → assert exit 0
2. Run `pytest tests/property/test_log_redaction.py -v` → assert no import errors
3. Emit evidence JSON

Requirements: R20.3, R20.4, R20.5
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

EVIDENCE_FILENAME = "20-httpx-fix.json"
TEST_TARGET = "tests/property/test_log_redaction.py"
COMMAND_TIMEOUT = 60


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

class TestHttpxImportFix:
    """R20: Verify httpx import fix allows test collection and execution."""

    def test_collect_only_exits_zero(self, platform_root):
        """pytest --collect-only on test_log_redaction.py must exit 0 (no import errors)."""
        result = _run_cmd(
            ["pytest", TEST_TARGET, "--collect-only"],
            cwd=str(platform_root),
        )
        assert result.returncode == 0, (
            f"pytest --collect-only failed with exit code {result.returncode}.\n"
            f"This indicates an import error (likely missing httpx dependency).\n"
            f"stdout: {result.stdout[:1000]}\n"
            f"stderr: {result.stderr[:1000]}"
        )

    def test_run_no_import_errors(self, platform_root):
        """pytest -v on test_log_redaction.py must not produce ImportError."""
        result = _run_cmd(
            ["pytest", TEST_TARGET, "-v", "--tb=short"],
            cwd=str(platform_root),
        )

        # Check that there are no ImportError or ModuleNotFoundError in output
        combined_output = result.stdout + result.stderr
        assert "ImportError" not in combined_output, (
            f"ImportError found in test output. The httpx fix may be incomplete.\n"
            f"Output snippet: {combined_output[:1500]}"
        )
        assert "ModuleNotFoundError" not in combined_output, (
            f"ModuleNotFoundError found in test output. Missing dependency.\n"
            f"Output snippet: {combined_output[:1500]}"
        )

        # The test should at least collect successfully (exit 0 or test failures
        # are acceptable — import errors are not)
        # Exit code 5 means "no tests collected" which is also acceptable
        assert result.returncode in (0, 1, 5), (
            f"pytest exited with unexpected code {result.returncode}.\n"
            f"Exit codes 0 (pass), 1 (test failures), 5 (no tests) are acceptable.\n"
            f"Exit code 2+ indicates collection/import errors.\n"
            f"stderr: {result.stderr[:1000]}"
        )


class TestHttpxFixEvidence:
    """R20.5: Emit structured evidence for the httpx fix verification."""

    def test_emit_evidence(self, evidence_collector, platform_root):
        """Collect httpx fix verification data and emit evidence JSON."""
        evidence_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "test_target": TEST_TARGET,
            "collect_only": {},
            "run_verbose": {},
            "overall_verdict": "pass",
        }

        # Run collect-only
        collect_result = _run_cmd(
            ["pytest", TEST_TARGET, "--collect-only"],
            cwd=str(platform_root),
        )
        evidence_data["collect_only"] = {
            "exit_code": collect_result.returncode,
            "stdout_snippet": collect_result.stdout[:2000],
            "stderr_snippet": collect_result.stderr[:500],
            "passed": collect_result.returncode == 0,
        }

        # Run verbose
        run_result = _run_cmd(
            ["pytest", TEST_TARGET, "-v", "--tb=short"],
            cwd=str(platform_root),
        )
        combined = run_result.stdout + run_result.stderr
        has_import_error = "ImportError" in combined or "ModuleNotFoundError" in combined

        evidence_data["run_verbose"] = {
            "exit_code": run_result.returncode,
            "stdout_snippet": run_result.stdout[:2000],
            "stderr_snippet": run_result.stderr[:500],
            "has_import_error": has_import_error,
            "passed": not has_import_error and run_result.returncode in (0, 1, 5),
        }

        # Overall verdict
        all_passed = (
            evidence_data["collect_only"]["passed"]
            and evidence_data["run_verbose"]["passed"]
        )
        evidence_data["overall_verdict"] = "pass" if all_passed else "fail"

        # Emit evidence
        evidence_collector.emit_json(
            requirement_id="R20.3,R20.4,R20.5",
            filename=EVIDENCE_FILENAME,
            data=evidence_data,
        )
