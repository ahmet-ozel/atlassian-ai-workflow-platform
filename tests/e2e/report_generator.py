"""
E2E Report Generator - produces E2E_REPORT.md at workspace root.

Generates a comprehensive markdown report summarizing all E2E test results,
including executive summary, bug fixes, requirements verdict table,
error path results, property test results, evidence index, and timing.
"""

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RequirementVerdict:
    """Verdict for a single requirement."""

    requirement_id: str
    title: str
    verdict: str  # "PASS", "FAIL", "SKIP", "ERROR"
    duration_seconds: float = 0.0
    evidence_path: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class TestResult:
    """Result of a single test execution."""

    test_name: str
    module: str
    status: str  # "passed", "failed", "skipped", "error"
    duration_seconds: float = 0.0
    error_message: Optional[str] = None
    requirement_ids: list[str] = field(default_factory=list)


@dataclass
class BugFixResult:
    """Result of a pre-existing bug fix."""

    bug_id: str
    title: str
    status: str  # "fixed", "partial", "not_fixed"
    description: str = ""


# ---------------------------------------------------------------------------
# Requirements catalog (-)
# ---------------------------------------------------------------------------

REQUIREMENTS_CATALOG = {
    "": "Docker Desktop Preflight",
    "": "Boot Bundle Startup",
    "": "Dashboard Access & Wizard",
    "": "Wizard Steps 1-3 (Infra)",
    "": "Wizard Step 4 (MCP Credentials)",
    "": "Wizard Steps 5-6 (Workers/Services)",
    "": "Wizard Step 7 (Department)",
    "": "Full Stack Healthcheck",
    "": "Jira CRUD Smoke",
    "": "Confluence CRUD Smoke",
    "": "Bitbucket Lifecycle Smoke",
    "": "OpenAI LLM Call",
    "": "SSH Connection Test",
    "": "Full AI Task Workflow",
    "": "Webhook Delivery",
    "": "Invalid Credential Error Paths",
    "": "Unreachable SSH Host",
    "": "Rate-Limited API Response",
    "": "Malformed Payload Validation",
    "": "httpx Import Fix",
    "": "Log Redaction Isolation Fix",
    "": "Confluence Space Fix",
    "": "pytest Collection Fix",
    "": "make down Profile Fix",
    "": "Volume Prefix Fix",
    "": "Credential Masking Fuzzing",
    "": "MCP Payload Fuzzing",
    "": "Compose Config Invariants",
    "": "Evidence Collection",
    "": "Report Generation",
    "": "Graceful Teardown",
    "": "Docker Build Context Fix",
    "": "Concurrent Task Submission",
    "": "Service Crash/Restart",
    "": "DB Connection Reconnect",
    "": "Token Expiry/Re-auth",
    # ------------------------------------------------------------------
    # Coverage added after the original catalog so the verdict table
    # includes automation wiring and LLM provider management checks.
    # ------------------------------------------------------------------
    "": (
        "Automation-Service Lifespan Wiring "
        "(all 9 app.state.* slots populated; no Router_Not_Wired_Error; "
        "healthz/readyz post-startup; admin/departments round-trip)"
    ),
    "": (
        "LLM Provider Management - Backend "
        "(CRUD + saved/unsaved test + dept override + audit + redaction)"
    ),
    "": (
        "LLM Provider Management - Admin Dashboard UI "
        "(/admin/llm-providers page renders, table + modal + "
        "delete-conflict toast)"
    ),
    "": (
        "Streamlit Task Creator - End-to-End "
        "(streamlit /task_creator reachable; form + create_task chain "
        "intact; page hydrates; no credential leak)"
    ),
}


# ---------------------------------------------------------------------------
# Report Generator
# ---------------------------------------------------------------------------

class ReportGenerator:
    """Generates E2E_REPORT.md from collected test results and evidence.

 Sections produced:
 1. Executive Summary ( ISSUES FOUND or  ALL CLEAR)
 2. Pre-existing Bugs Fixed
 3. Requirements Verdict Table
 4. Error Path Results
 5. Property Test Results
 6. Evidence Index
 7. Timing & Cost
 """

    def __init__(self, workspace_root: Path):
        """
 Initialize the report generator.

 Args:
 workspace_root: Path to the workspace root directory.
 Report will be written to workspace_root/E2E_REPORT.md.
 """
        self.workspace_root = workspace_root
        self.evidence_dir = workspace_root / "e2e-evidence"
        self.report_path = workspace_root / "E2E_REPORT.md"

        self._test_results: list[TestResult] = []
        self._bug_fixes: list[BugFixResult] = []
        self._requirement_verdicts: dict[str, RequirementVerdict] = {}
        self._start_time: Optional[float] = None
        self._end_time: Optional[float] = None
        self._total_cost_usd: float = 0.0

    def add_test_result(self, result: TestResult) -> None:
        """Add a test result to the report data."""
        self._test_results.append(result)

    def add_bug_fix(self, fix: BugFixResult) -> None:
        """Add a bug fix result to the report data."""
        self._bug_fixes.append(fix)

    def set_requirement_verdict(self, verdict: RequirementVerdict) -> None:
        """Set the verdict for a specific requirement."""
        self._requirement_verdicts[verdict.requirement_id] = verdict

    def set_timing(self, start_time: float, end_time: float) -> None:
        """Set the overall test suite timing."""
        self._start_time = start_time
        self._end_time = end_time

    def set_cost(self, cost_usd: float) -> None:
        """Set the estimated cost (e.g., OpenAI API usage)."""
        self._total_cost_usd = cost_usd

    def generate(self) -> Path:
        """Generate the E2E_REPORT.md file.

 Reads evidence files from e2e-evidence/ to populate verdicts
 for any requirements not explicitly set via set_requirement_verdict.

 Returns:
 Path to the generated E2E_REPORT.md file.
 """
        self._auto_populate_verdicts()

        sections = [
            self._generate_header(),
            self._generate_executive_summary(),
            self._generate_bug_fixes_section(),
            self._generate_verdict_table(),
            self._generate_error_path_results(),
            self._generate_property_test_results(),
            self._generate_evidence_index(),
            self._generate_timing_section(),
        ]

        report_content = "\n\n".join(sections)
        self.report_path.write_text(report_content, encoding="utf-8")

        return self.report_path

    # ─── Section generators ────────────────────────────────────────────

    def _generate_header(self) -> str:
        """Generate report header."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return (
            "# E2E Test Report\n\n"
            f"**Generated:** {timestamp}  \n"
            f"**Workspace:** `{self.workspace_root}`  \n"
            f"**Total Tests:** {len(self._test_results)}  \n"
            f"**Framework:** pytest + Playwright MCP + Hypothesis"
        )

    def _generate_executive_summary(self) -> str:
        """Generate executive summary with pass/fail indicator."""
        total = len(self._requirement_verdicts)
        passed = sum(
            1 for v in self._requirement_verdicts.values()
            if v.verdict == "PASS"
        )
        failed = sum(
            1 for v in self._requirement_verdicts.values()
            if v.verdict == "FAIL"
        )
        skipped = sum(
            1 for v in self._requirement_verdicts.values()
            if v.verdict == "SKIP"
        )

        if failed > 0:
            prefix = " ISSUES FOUND"
            summary_line = f"**{failed}** requirement(s) failed out of **{total}** total."
        else:
            prefix = " ALL CLEAR"
            summary_line = f"All **{passed}** tested requirements passed."

        lines = [
            "## Executive Summary",
            "",
            f"### {prefix}",
            "",
            summary_line,
            "",
            f"| Metric | Count |",
            f"|--------|-------|",
            f"|  Passed | {passed} |",
            f"|  Failed | {failed} |",
            f"|  Skipped | {skipped} |",
            f"|  Total | {total} |",
        ]

        return "\n".join(lines)

    def _generate_bug_fixes_section(self) -> str:
        """Generate pre-existing bugs fixed section."""
        lines = [
            "## Pre-existing Bugs Fixed",
            "",
        ]

        if not self._bug_fixes:
            lines.append("_No bug fixes recorded._")
            return "\n".join(lines)

        lines.extend([
            "| Bug ID | Title | Status | Description |",
            "|--------|-------|--------|-------------|",
        ])

        for fix in self._bug_fixes:
            status_icon = {
                "fixed": "",
                "partial": "",
                "not_fixed": "",
            }.get(fix.status, "")

            lines.append(
                f"| {fix.bug_id} | {fix.title} | {status_icon} {fix.status} | "
                f"{fix.description[:80]} |"
            )

        return "\n".join(lines)

    def _generate_verdict_table(self) -> str:
        """Generate requirements verdict table with one row per -."""
        lines = [
            "## Requirements Verdict Table",
            "",
            "| ID | Title | Verdict | Duration | Evidence Path |",
            "|----|-------|---------|----------|---------------|",
        ]

        for req_id in sorted(REQUIREMENTS_CATALOG.keys(), key=lambda x: int(x[1:])):
            title = REQUIREMENTS_CATALOG[req_id]
            verdict = self._requirement_verdicts.get(req_id)

            if verdict:
                verdict_icon = {
                    "PASS": " PASS",
                    "FAIL": " FAIL",
                    "SKIP": " SKIP",
                    "ERROR": " ERROR",
                }.get(verdict.verdict, verdict.verdict)

                duration = f"{verdict.duration_seconds:.1f}s" if verdict.duration_seconds else "-"
                evidence = f"`{verdict.evidence_path}`" if verdict.evidence_path else "-"
            else:
                verdict_icon = " SKIP"
                duration = "-"
                evidence = "-"

            lines.append(f"| {req_id} | {title} | {verdict_icon} | {duration} | {evidence} |")

        return "\n".join(lines)

    def _generate_error_path_results(self) -> str:
        """Generate error path test results section."""
        lines = [
            "## Error Path Results",
            "",
        ]

        error_tests = [
            r for r in self._test_results
            if any(rid.startswith("") and int(rid[1:].split(".")[0]) in range(16, 20)
                   for rid in r.requirement_ids)
            or "error" in r.module.lower()
            or "bad_creds" in r.module.lower()
            or "malformed" in r.module.lower()
            or "rate_limit" in r.module.lower()
            or "ssh_unreachable" in r.module.lower()
        ]

        if not error_tests:
            lines.append("_No error path test results recorded._")
            return "\n".join(lines)

        lines.extend([
            "| Test | Status | Duration | Error |",
            "|------|--------|----------|-------|",
        ])

        for test in error_tests:
            status_icon = "" if test.status == "passed" else ""
            duration = f"{test.duration_seconds:.1f}s"
            error = test.error_message[:60] if test.error_message else "-"
            lines.append(f"| {test.test_name} | {status_icon} {test.status} | {duration} | {error} |")

        return "\n".join(lines)

    def _generate_property_test_results(self) -> str:
        """Generate property/fuzzing test results section."""
        lines = [
            "## Property Test Results",
            "",
        ]

        property_tests = [
            r for r in self._test_results
            if "fuzzing" in r.module.lower()
            or "property" in r.module.lower()
            or "invariant" in r.module.lower()
            or "hypothesis" in r.module.lower()
        ]

        if not property_tests:
            lines.append("_No property test results recorded._")
            return "\n".join(lines)

        lines.extend([
            "| Test | Status | Duration | Examples | Error |",
            "|------|--------|----------|----------|-------|",
        ])

        for test in property_tests:
            status_icon = "" if test.status == "passed" else ""
            duration = f"{test.duration_seconds:.1f}s"
            error = test.error_message[:50] if test.error_message else "-"
            lines.append(
                f"| {test.test_name} | {status_icon} {test.status} | "
                f"{duration} | - | {error} |"
            )

        return "\n".join(lines)

    def _generate_evidence_index(self) -> str:
        """Generate evidence index section listing all evidence files."""
        lines = [
            "## Evidence Index",
            "",
        ]

        if not self.evidence_dir.exists():
            lines.append("_No evidence directory found._")
            return "\n".join(lines)

        evidence_files = sorted(self.evidence_dir.rglob("*"))
        evidence_files = [f for f in evidence_files if f.is_file()]

        if not evidence_files:
            lines.append("_No evidence files found._")
            return "\n".join(lines)

        lines.append(f"**Total evidence files:** {len(evidence_files)}")
        lines.append("")
        lines.extend([
            "| File | Size | Type |",
            "|------|------|------|",
        ])

        for filepath in evidence_files[:50]:  # Cap at 50 entries
            rel_path = filepath.relative_to(self.evidence_dir)
            size = filepath.stat().st_size
            size_str = self._format_size(size)
            file_type = filepath.suffix.lstrip(".")
            lines.append(f"| `{rel_path}` | {size_str} | {file_type} |")

        if len(evidence_files) > 50:
            lines.append(f"| ... | ... | ... |")
            lines.append(f"| _{len(evidence_files) - 50} more files_ | | |")

        return "\n".join(lines)

    def _generate_timing_section(self) -> str:
        """Generate timing and cost section."""
        lines = [
            "## Timing & Cost",
            "",
        ]

        if self._start_time and self._end_time:
            elapsed = self._end_time - self._start_time
            minutes = int(elapsed // 60)
            seconds = int(elapsed % 60)
            lines.append(f"**Total Duration:** {minutes}m {seconds}s")
        else:
            lines.append("**Total Duration:** _not recorded_")

        lines.append("")

        if self._total_cost_usd > 0:
            lines.append(f"**Estimated API Cost:** ${self._total_cost_usd:.4f} USD")
        else:
            lines.append("**Estimated API Cost:** _not tracked_")

        lines.append("")

        # Per-test timing breakdown
        if self._test_results:
            total_test_time = sum(r.duration_seconds for r in self._test_results)
            lines.append(f"**Total Test Execution Time:** {total_test_time:.1f}s")

            # Top 5 slowest tests
            slowest = sorted(self._test_results, key=lambda r: r.duration_seconds, reverse=True)[:5]
            if slowest:
                lines.append("")
                lines.append("**Slowest Tests:**")
                lines.append("")
                for r in slowest:
                    lines.append(f"- `{r.test_name}`: {r.duration_seconds:.1f}s")

        return "\n".join(lines)

    # ─── Private helpers ───────────────────────────────────────────────

    def _auto_populate_verdicts(self) -> None:
        """Auto-populate requirement verdicts from evidence files and test results."""
        # Populate from test results
        for result in self._test_results:
            for req_id in result.requirement_ids:
                # Extract base requirement (e.g., "" from "")
                base_req = req_id.split(".")[0]
                if base_req not in self._requirement_verdicts:
                    verdict = "PASS" if result.status == "passed" else (
                        "SKIP" if result.status == "skipped" else "FAIL"
                    )
                    self._requirement_verdicts[base_req] = RequirementVerdict(
                        requirement_id=base_req,
                        title=REQUIREMENTS_CATALOG.get(base_req, "Unknown"),
                        verdict=verdict,
                        duration_seconds=result.duration_seconds,
                    )

        # Populate from evidence files
        if self.evidence_dir.exists():
            for evidence_file in self.evidence_dir.glob("*.json"):
                try:
                    data = json.loads(evidence_file.read_text(encoding="utf-8"))
                    req_ids = data.get("requirement_id", "")
                    if req_ids:
                        for req_id in req_ids.split(","):
                            base_req = req_id.strip().split(".")[0]
                            if base_req and base_req not in self._requirement_verdicts:
                                inner_data = data.get("data", {})
                                verdict_str = inner_data.get("overall_verdict", "pass")
                                verdict = "PASS" if verdict_str == "pass" else "FAIL"
                                self._requirement_verdicts[base_req] = RequirementVerdict(
                                    requirement_id=base_req,
                                    title=REQUIREMENTS_CATALOG.get(base_req, "Unknown"),
                                    verdict=verdict,
                                    evidence_path=str(evidence_file.name),
                                )
                except (json.JSONDecodeError, OSError):
                    continue

        # Fill remaining requirements as SKIP
        for req_id, title in REQUIREMENTS_CATALOG.items():
            if req_id not in self._requirement_verdicts:
                self._requirement_verdicts[req_id] = RequirementVerdict(
                    requirement_id=req_id,
                    title=title,
                    verdict="SKIP",
                )

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """Format byte count as human-readable string."""
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
