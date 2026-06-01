"""
Test 30: Report generation verification (R30).

Validates that the ReportGenerator produces a complete E2E_REPORT.md
with all required sections, proper formatting, and a verdict table
covering every catalog requirement (R1-R36 original + R37-R39 added
with the ``automation-service-wiring`` and ``llm-provider-management``
specs).

Verification steps:
1. Trigger report generation from collected test results
2. Assert E2E_REPORT.md exists at workspace root
3. Assert all required sections present
4. Assert verdict table has TOTAL_REQUIREMENTS rows (one per
   requirement in REQUIREMENTS_CATALOG)
5. Emit evidence

Requirements: R30.1, R30.2, R30.3, R30.4, R30.5, R30.6
"""

import time
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_FILENAME = "30-report-generation.json"

REQUIRED_SECTIONS = [
    "Executive Summary",
    "Pre-existing Bugs Fixed",
    "Requirements Verdict Table",
    "Error Path Results",
    "Property Test Results",
    "Evidence Index",
    "Timing & Cost",
]

# R1-R36 were the original ``local-e2e-real-test`` catalog; R37-R39
# were added after the ``automation-service-wiring`` and
# ``llm-provider-management`` specs landed; R40 was added with the
# Streamlit Task Creator E2E test (E5 — gereksinim.txt G8/G10) so the
# verdict table covers every spec the live stack now exercises.
TOTAL_REQUIREMENTS = 40


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReportGeneration:
    """R30: Verify E2E_REPORT.md generation."""

    def test_report_generator_produces_file(self, workspace_root):
        """R30.1: ReportGenerator.generate() produces E2E_REPORT.md.

        Invoke the report generator and verify it creates the report
        file at the workspace root.
        """
        from report_generator import ReportGenerator

        generator = ReportGenerator(workspace_root)
        report_path = generator.generate()

        assert report_path.exists(), (
            f"E2E_REPORT.md was not generated at {report_path}."
        )
        assert report_path.stat().st_size > 0, (
            "E2E_REPORT.md was generated but is empty."
        )

    def test_report_exists_at_workspace_root(self, workspace_root):
        """R30.2: E2E_REPORT.md exists at workspace root after generation."""
        from report_generator import ReportGenerator

        generator = ReportGenerator(workspace_root)
        generator.generate()

        report_path = workspace_root / "E2E_REPORT.md"
        assert report_path.exists(), (
            f"E2E_REPORT.md not found at workspace root: {workspace_root}"
        )

    def test_all_required_sections_present(self, workspace_root):
        """R30.3: Report contains all required sections.

        The report must include: Executive Summary, Pre-existing Bugs Fixed,
        Requirements Verdict Table, Error Path Results, Property Test Results,
        Evidence Index, and Timing & Cost.
        """
        from report_generator import ReportGenerator

        generator = ReportGenerator(workspace_root)
        generator.generate()

        report_path = workspace_root / "E2E_REPORT.md"
        content = report_path.read_text(encoding="utf-8")

        missing_sections = []
        for section in REQUIRED_SECTIONS:
            if section not in content:
                missing_sections.append(section)

        assert len(missing_sections) == 0, (
            f"Report is missing {len(missing_sections)} required section(s): "
            f"{missing_sections}"
        )

    def test_verdict_table_has_all_requirement_rows(self, workspace_root):
        """R30.4: Verdict table has one row per catalog requirement.

        Each requirement (R1-R36 original + R37-R39 added with the
        ``automation-service-wiring`` and ``llm-provider-management``
        specs) must have a row in the verdict table with ID, Title,
        Verdict, Duration, and Evidence Path. ``TOTAL_REQUIREMENTS``
        is the single source of truth so future spec additions only
        need to bump the catalog and the constant.
        """
        from report_generator import ReportGenerator

        generator = ReportGenerator(workspace_root)
        generator.generate()

        report_path = workspace_root / "E2E_REPORT.md"
        content = report_path.read_text(encoding="utf-8")

        # Find the verdict table section
        table_start = content.find("## Requirements Verdict Table")
        assert table_start != -1, "Requirements Verdict Table section not found."

        # Extract table content (from section start to next section)
        table_section = content[table_start:]
        next_section = table_section.find("\n## ", 3)
        if next_section != -1:
            table_section = table_section[:next_section]

        # Count data rows (lines starting with | R)
        table_lines = table_section.split("\n")
        data_rows = [
            line for line in table_lines
            if line.strip().startswith("| R") and not line.strip().startswith("| ID")
        ]

        assert len(data_rows) == TOTAL_REQUIREMENTS, (
            f"Verdict table has {len(data_rows)} data rows, "
            f"expected {TOTAL_REQUIREMENTS} "
            f"(R1-R36 original + R37-R39 spec extensions)."
        )

    def test_executive_summary_has_status_prefix(self, workspace_root):
        """R30.5: Executive Summary starts with 🔴 or 🟢 indicator.

        The executive summary must clearly indicate overall status
        with either '🔴 ISSUES FOUND' or '🟢 ALL CLEAR'.
        """
        from report_generator import ReportGenerator

        generator = ReportGenerator(workspace_root)
        generator.generate()

        report_path = workspace_root / "E2E_REPORT.md"
        content = report_path.read_text(encoding="utf-8")

        has_issues_indicator = "🔴 ISSUES FOUND" in content
        has_clear_indicator = "🟢 ALL CLEAR" in content

        assert has_issues_indicator or has_clear_indicator, (
            "Executive Summary missing status prefix. "
            "Expected '🔴 ISSUES FOUND' or '🟢 ALL CLEAR'."
        )

    def test_verdict_table_columns(self, workspace_root):
        """R30.6: Verdict table has correct column structure.

        Each row must have: ID, Title, Verdict, Duration, Evidence Path.
        """
        from report_generator import ReportGenerator

        generator = ReportGenerator(workspace_root)
        generator.generate()

        report_path = workspace_root / "E2E_REPORT.md"
        content = report_path.read_text(encoding="utf-8")

        # Check table header
        assert "| ID | Title | Verdict | Duration | Evidence Path |" in content, (
            "Verdict table missing expected column headers: "
            "ID, Title, Verdict, Duration, Evidence Path"
        )


class TestReportGenerationEvidence:
    """R30.6: Emit structured evidence for report generation."""

    def test_emit_evidence(self, evidence_collector, workspace_root):
        """Collect report generation data and emit evidence JSON."""
        from report_generator import ReportGenerator

        evidence_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "report_generation": {},
            "sections_check": {},
            "verdict_table_check": {},
            "overall_verdict": "pass",
        }

        # Generate report
        generator = ReportGenerator(workspace_root)
        report_path = generator.generate()

        evidence_data["report_generation"] = {
            "report_path": str(report_path),
            "exists": report_path.exists(),
            "size_bytes": report_path.stat().st_size if report_path.exists() else 0,
        }

        if report_path.exists():
            content = report_path.read_text(encoding="utf-8")

            # Check sections
            sections_found = []
            sections_missing = []
            for section in REQUIRED_SECTIONS:
                if section in content:
                    sections_found.append(section)
                else:
                    sections_missing.append(section)

            evidence_data["sections_check"] = {
                "found": sections_found,
                "missing": sections_missing,
                "all_present": len(sections_missing) == 0,
            }

            # Check verdict table rows
            table_start = content.find("## Requirements Verdict Table")
            if table_start != -1:
                table_section = content[table_start:]
                next_section = table_section.find("\n## ", 3)
                if next_section != -1:
                    table_section = table_section[:next_section]

                table_lines = table_section.split("\n")
                data_rows = [
                    line for line in table_lines
                    if line.strip().startswith("| R")
                    and not line.strip().startswith("| ID")
                ]

                evidence_data["verdict_table_check"] = {
                    "row_count": len(data_rows),
                    "expected_rows": TOTAL_REQUIREMENTS,
                    "passed": len(data_rows) == TOTAL_REQUIREMENTS,
                }
            else:
                evidence_data["verdict_table_check"] = {
                    "error": "Verdict table section not found",
                    "passed": False,
                }

            # Check status prefix
            has_prefix = "🔴 ISSUES FOUND" in content or "🟢 ALL CLEAR" in content
            evidence_data["has_status_prefix"] = has_prefix

            # Overall verdict
            all_passed = (
                evidence_data["sections_check"]["all_present"]
                and evidence_data["verdict_table_check"].get("passed", False)
                and has_prefix
            )
            evidence_data["overall_verdict"] = "pass" if all_passed else "fail"
        else:
            evidence_data["overall_verdict"] = "fail"

        # Emit evidence
        evidence_collector.emit_json(
            requirement_id="R30.1,R30.2,R30.3,R30.4,R30.5,R30.6",
            filename=EVIDENCE_FILENAME,
            data=evidence_data,
        )
