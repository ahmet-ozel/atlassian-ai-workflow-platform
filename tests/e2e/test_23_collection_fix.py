"""
Test 23: Verify pytest collection fix (R23).

Validates that pytest can collect all tests in the `tests/` directory without
errors and that a full test run completes without aborting. The fix ensures
that syntax/import errors in individual test files do not kill the entire
test suite collection.

Verification steps:
1. Run `pytest tests/ --collect-only` → assert exit 0
2. Run `pytest tests/ -v --tb=short` → assert run completes (no abort)
3. Emit evidence JSON to `e2e-evidence/23-collection-fix.json`

Requirements: R23.3, R23.4, R23.5
"""

import platform
import subprocess
import time
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_FILENAME = "23-collection-fix.json"
TEST_DIRECTORY = "tests/"
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

class TestPytestCollectionFix:
    """R23: Verify pytest collection fix - all tests collect without errors."""

    def test_collect_only_exits_zero(self, platform_root):
        """pytest tests/ --collect-only must exit 0 (no collection errors).

        This verifies that all test files in the tests/ directory can be
        imported and collected without syntax errors, import failures, or
        other collection-time exceptions.
        """
        result = _run_cmd(
            ["pytest", TEST_DIRECTORY, "--collect-only"],
            cwd=str(platform_root),
        )
        assert result.returncode == 0, (
            f"pytest --collect-only failed with exit code {result.returncode}.\n"
            f"This indicates a collection error (syntax/import issue in a test file).\n"
            f"stdout: {result.stdout[:2000]}\n"
            f"stderr: {result.stderr[:1000]}"
        )

    def test_run_completes_no_abort(self, platform_root):
        """pytest tests/ -v --tb=short must complete without aborting.

        Exit codes:
        - 0: all tests passed
        - 1: some tests failed (acceptable - tests ran to completion)
        - 5: no tests collected (acceptable - collection succeeded)
        - 2: test execution interrupted / collection error (NOT acceptable)
        - 3: internal error (NOT acceptable)
        - 4: usage error (NOT acceptable)

        The key assertion is that the run does NOT abort due to collection
        errors. Individual test failures are acceptable.
        """
        result = _run_cmd(
            ["pytest", TEST_DIRECTORY, "-v", "--tb=short", "-x" if False else "--no-header"],
            cwd=str(platform_root),
            timeout=COMMAND_TIMEOUT,
        )

        # Exit code 2 means interrupted/collection error, 3 = internal error
        assert result.returncode not in (2, 3, 4), (
            f"pytest aborted with exit code {result.returncode}.\n"
            f"Exit code 2 = collection/interrupt error, 3 = internal error, 4 = usage error.\n"
            f"The pytest collection fix should prevent this.\n"
            f"stdout (last 2000 chars): {result.stdout[-2000:]}\n"
            f"stderr: {result.stderr[:1000]}"
        )

        # Additionally verify no collection errors in output
        combined_output = result.stdout + result.stderr
        assert "ERRORS" not in combined_output.split("=")[-1] if "=" in combined_output else True, (
            f"Collection errors detected in pytest output.\n"
            f"Output snippet: {combined_output[-1500:]}"
        )


class TestCollectionFixEvidence:
    """R23.5: Emit structured evidence for the pytest collection fix."""

    def test_emit_evidence(self, evidence_collector, platform_root):
        """Collect pytest collection fix verification data and emit evidence JSON."""
        evidence_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "test_directory": TEST_DIRECTORY,
            "collect_only": {},
            "run_verbose": {},
            "overall_verdict": "pass",
        }

        # Step 1: Run collect-only
        collect_result = _run_cmd(
            ["pytest", TEST_DIRECTORY, "--collect-only"],
            cwd=str(platform_root),
        )
        evidence_data["collect_only"] = {
            "exit_code": collect_result.returncode,
            "stdout_snippet": collect_result.stdout[:3000],
            "stderr_snippet": collect_result.stderr[:500],
            "passed": collect_result.returncode == 0,
            "tests_collected": _count_collected_tests(collect_result.stdout),
        }

        # Step 2: Run verbose with short traceback
        run_result = _run_cmd(
            ["pytest", TEST_DIRECTORY, "-v", "--tb=short", "--no-header"],
            cwd=str(platform_root),
            timeout=COMMAND_TIMEOUT,
        )
        run_aborted = run_result.returncode in (2, 3, 4)
        evidence_data["run_verbose"] = {
            "exit_code": run_result.returncode,
            "stdout_snippet": run_result.stdout[-3000:],
            "stderr_snippet": run_result.stderr[:500],
            "aborted": run_aborted,
            "passed": not run_aborted,
        }

        # Overall verdict
        all_passed = (
            evidence_data["collect_only"]["passed"]
            and evidence_data["run_verbose"]["passed"]
        )
        evidence_data["overall_verdict"] = "pass" if all_passed else "fail"

        # Emit evidence
        evidence_collector.emit_json(
            requirement_id="R23.3,R23.4,R23.5",
            filename=EVIDENCE_FILENAME,
            data=evidence_data,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _count_collected_tests(stdout: str) -> int:
    """Parse pytest --collect-only output to count collected test items."""
    # pytest outputs lines like "<Module ...>" and "<Function ...>"
    # The summary line says "X tests collected" or "X items collected"
    import re
    match = re.search(r"(\d+)\s+tests?\s+collected", stdout)
    if match:
        return int(match.group(1))
    # Alternative pattern: "collected X items"
    match = re.search(r"collected\s+(\d+)\s+items?", stdout)
    if match:
        return int(match.group(1))
    return 0
