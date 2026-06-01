"""Capture pytest output for the test files owned by the platform-real-usage-gaps spec.

Invocation pattern matches the spec's own task 16.1 checkpoint:
    pytest tests/property/ tests/ci/ tests/integration/

This collects all tests in those directories at once (which keeps sys.path
shared across modules and avoids the cross-file ModuleNotFoundError that
appears when files are passed individually). After the run completes we
filter the per-test outcome lines down to those tests that live in
files owned by the platform-real-usage-gaps spec, producing the
229/229 oracle for clause 3.1.

Test ownership identified via:
  - grep "platform-real-usage-gaps" platform/tests/**/*.py and platform/workers/**/*.py
  - reading .kiro/specs/platform-real-usage-gaps/tasks.md to list every
    test file the spec creates (Property 1..16 + CI tests + integration tests).
"""
import re
import subprocess
import sys

output_file = sys.argv[1]

# Files owned by platform-real-usage-gaps (per its tasks.md):
PLATFORM_REAL_USAGE_GAPS_TEST_FILES = (
    # Property tests (Property 1..16; sequence from tasks.md)
    "tests/property/test_needs_info_timeout_parity.py",
    "tests/property/test_chat_intent_wiring.py",
    "tests/property/test_setup_wizard_flow.py",
    "tests/property/test_account_id_auto_probe.py",
    "tests/property/test_loop_guard_account_id_resolution.py",
    "tests/property/test_task_creator_assignee_card.py",
    "tests/property/test_ssh_key_rotation.py",
    "tests/property/test_webhook_secret_rotation_ui.py",
    "tests/property/test_external_provider_probe.py",
    "tests/property/test_dept_bulk_import.py",
    "tests/property/test_epic_auto_detect.py",
    "tests/property/test_budget_alarm_threshold.py",
    "tests/property/test_runner_queue_status.py",
    # Integration tests
    "tests/integration/test_compose_boot_only.py",
    # CI tests
    "tests/ci/test_task_creation_prompt_unique.py",
    "tests/ci/test_task_creation_prompt_sections.py",
    "tests/ci/test_template_parity.py",
    "tests/ci/test_template_placeholders.py",
    # Worker tests added/extended by platform-real-usage-gaps
    "workers/execution-runner-worker/tests/unit/test_ssh_dual_slot_fallback.py",
    # Note: workers/automation-worker/tests/property/test_needs_info_timeout_enforcement.py
    # was created by an earlier spec; only specific tests inside
    # workers/automation-worker/tests/unit/test_automation_workflow.py were
    # added by platform-real-usage-gaps. Those tests are filtered by name below.
)

# Specific tests owned by platform-real-usage-gaps inside files NOT exclusively
# owned by the spec (currently only the timeout-rename test in test_automation_workflow.py).
PLATFORM_REAL_USAGE_GAPS_INDIVIDUAL_TESTS = (
    "workers/automation-worker/tests/unit/test_automation_workflow.py::TestNeedsInfoConstants::test_timeout_is_seven_days",
    "workers/automation-worker/tests/unit/test_automation_workflow.py::TestNeedsInfoFormatters::test_timeout_comment_mentions_seven_days_and_stale",
)

cmd = [
    "python", "-m", "pytest",
    "-v", "--tb=no", "-rN",
    "--continue-on-collection-errors",
    "--timeout=30",
    "-p", "no:randomly",
    "tests/property/",
    "tests/ci/",
    "tests/integration/",
    "workers/",
    # Excluded files (SSL/network hangs unrelated to this spec)
    "--ignore=tests/property/test_client_factory.py",
    "--ignore=tests/property/test_credential_inject.py",
    "--ignore=tests/property/test_health_contract.py",
    "--ignore=tests/property/test_llm_call_paths.py",
    "--ignore=tests/property/test_llm_retry_fallback.py",
    "--ignore=tests/property/test_path_coverage.py",
    "--ignore=tests/integration/test_existing_suite_still_green.py",
]

print(f"Running directory-mode pytest...")
print(f"Output file: {output_file}")

result = subprocess.run(
    cmd,
    capture_output=True,
    text=True,
    cwd=r"C:\Users\ahmet\Desktop\yeni_atlassian\platform",
    timeout=900,
)

raw_combined = result.stdout + result.stderr


def _is_owned(line: str) -> bool:
    """Return True if the line reports a per-test outcome owned by platform-real-usage-gaps."""
    # Match lines like "tests/property/test_X.py::TestClass::test_method PASSED"
    if " PASSED" not in line and " FAILED" not in line and " SKIPPED" not in line and " ERROR" not in line and " XFAIL" not in line and " XPASS" not in line:
        return False
    # File ownership: any spec-owned file prefix
    for owned_file in PLATFORM_REAL_USAGE_GAPS_TEST_FILES:
        # Normalise to forward slashes for cross-platform matching
        norm = line.replace("\\", "/")
        if norm.lstrip().startswith(owned_file):
            return True
    # Individual-test ownership inside non-spec files
    for owned_test in PLATFORM_REAL_USAGE_GAPS_INDIVIDUAL_TESTS:
        norm = line.replace("\\", "/")
        if owned_test in norm:
            return True
    return False


owned_lines = [ln for ln in raw_combined.splitlines() if _is_owned(ln)]

# Tally outcomes for the spec-owned subset
outcomes = {"PASSED": 0, "FAILED": 0, "SKIPPED": 0, "ERROR": 0, "XFAIL": 0, "XPASS": 0}
for ln in owned_lines:
    for key in outcomes:
        if f" {key}" in ln:
            outcomes[key] += 1
            break

total = sum(outcomes.values())
header = (
    "# ORACLE: platform-real-usage-gaps test outcome snapshot (clause 3.1)\n"
    "# Captured BEFORE any fix is applied (UNFIXED code).\n"
    "# Invocation: directory-mode pytest matching the spec's task 16.1 pattern,\n"
    "#             then filtered to spec-owned test files / test names.\n"
    "#\n"
    f"# OWNED TEST OUTCOME COUNTS:  total={total}\n"
    + "".join(f"#   {k:8s} = {v}\n" for k, v in outcomes.items())
    + "#\n"
    "# Test files (owned by platform-real-usage-gaps spec):\n"
    + "".join(f"#   {f}\n" for f in PLATFORM_REAL_USAGE_GAPS_TEST_FILES)
    + "# Individual tests owned by the spec (inside shared files):\n"
    + "".join(f"#   {t}\n" for t in PLATFORM_REAL_USAGE_GAPS_INDIVIDUAL_TESTS)
    + "#\n"
    "# Generated by platform/run_real_usage_gaps.py (workspace is not a git repository).\n"
    "\n"
)

# Add a per-test summary section (only owned tests) so the file is byte-comparable
body = "\n".join(owned_lines) + "\n\n"

# Append the raw end-of-run summary line for sanity (last line that starts with "===" and contains counts)
summary_lines = [
    ln for ln in raw_combined.splitlines()[-20:]
    if ln.strip().startswith("=") and ("passed" in ln or "failed" in ln or "error" in ln)
]
tail = "# Raw pytest run-level summary (full directory mode):\n"
tail += "\n".join(summary_lines) + "\n"

with open(output_file, "w", encoding="utf-8") as f:
    f.write(header + body + tail)

print(f"Captured {len(header + body + tail)} chars to {output_file}")
print(f"Owned-test totals: {outcomes} (total={total})")
print(f"Return code: {result.returncode}")
