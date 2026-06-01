"""Unit tests for platform/scripts/vps_report_generator.py.

Validates:
- R20.6: 🟢/🔴 prefix logic (0 fail → 🟢, ≥1 fail → 🔴)
- Verdict aggregation: critical/major Open_Issue → fail, only minor → partial, no issues → pass
- Severity grouping in Open Issues section render
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add scripts directory to sys.path so we can import the module under test.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import vps_report_generator as rg  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def evidence_dir(tmp_path):
    """Create a temporary evidence directory with minimal structure."""
    edir = tmp_path / "vps-test-evidence"
    edir.mkdir()
    return edir


def _write_open_issues(evidence_dir: Path, issues: list[dict]) -> None:
    """Helper to write open-issues.json into the evidence directory."""
    with open(evidence_dir / "open-issues.json", "w", encoding="utf-8") as f:
        json.dump(issues, f)


def _make_issue(
    id: int = 1,
    requirement_id: str = "R10",
    scenario_id: str | None = "JIRA-3",
    severity: str = "major",
    category: str = "integration",
    summary: str = "Test failure",
    evidence_path: str = "vps-test-evidence/10-jira.json",
    recommended_action: str = "manual_fix",
) -> dict:
    return {
        "id": id,
        "requirement_id": requirement_id,
        "scenario_id": scenario_id,
        "severity": severity,
        "category": category,
        "summary": summary,
        "evidence_path": evidence_path,
        "recommended_action": recommended_action,
        "logged_at_utc": "2025-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Verdict aggregation tests
# ---------------------------------------------------------------------------


class TestAggregateVerdict:
    """Tests for the aggregate_verdict function."""

    def test_pass_when_evidence_exists_and_no_issues(self, evidence_dir):
        """No open issues + evidence file exists → pass."""
        (evidence_dir / "01-preflight.txt").write_text("OK")
        result = rg.aggregate_verdict("R1", evidence_dir, "01-preflight.txt", [])
        assert result == "pass"

    def test_manual_pending_when_no_evidence(self, evidence_dir):
        """No evidence file → manual_pending."""
        result = rg.aggregate_verdict("R1", evidence_dir, "01-preflight.txt", [])
        assert result == "manual_pending"

    def test_fail_when_critical_issue(self, evidence_dir):
        """Critical open issue → fail regardless of evidence."""
        (evidence_dir / "10-jira.json").write_text("[]")
        issues = [_make_issue(severity="critical", requirement_id="R10")]
        result = rg.aggregate_verdict("R10", evidence_dir, "10-jira.json", issues)
        assert result == "fail"

    def test_fail_when_major_issue(self, evidence_dir):
        """Major open issue → fail."""
        (evidence_dir / "10-jira.json").write_text("[]")
        issues = [_make_issue(severity="major", requirement_id="R10")]
        result = rg.aggregate_verdict("R10", evidence_dir, "10-jira.json", issues)
        assert result == "fail"

    def test_partial_when_only_minor_issues(self, evidence_dir):
        """Only minor open issues → partial."""
        (evidence_dir / "10-jira.json").write_text("[]")
        issues = [_make_issue(severity="minor", requirement_id="R10")]
        result = rg.aggregate_verdict("R10", evidence_dir, "10-jira.json", issues)
        assert result == "partial"

    def test_issues_for_other_requirements_ignored(self, evidence_dir):
        """Open issues for different requirement don't affect this one."""
        (evidence_dir / "01-preflight.txt").write_text("OK")
        issues = [_make_issue(severity="critical", requirement_id="R10")]
        result = rg.aggregate_verdict("R1", evidence_dir, "01-preflight.txt", issues)
        assert result == "pass"


# ---------------------------------------------------------------------------
# Executive Summary prefix tests (R20.6)
# ---------------------------------------------------------------------------


class TestExecutiveSummaryPrefix:
    """Tests for the 🟢/🔴 prefix logic."""

    def test_green_when_zero_failures(self):
        """0 fail verdicts → 🟢 GO-LIVE READY."""
        verdicts = {"R1": "pass", "R2": "pass", "R3": "manual_pending"}
        result = rg.render_executive_summary(verdicts, [])
        assert "🟢 GO-LIVE READY" in result
        assert "🔴" not in result

    def test_red_when_any_failure(self):
        """≥1 fail verdict → 🔴 NOT GO-LIVE READY."""
        verdicts = {"R1": "pass", "R10": "fail"}
        issues = [_make_issue(severity="critical")]
        result = rg.render_executive_summary(verdicts, issues)
        assert "🔴 NOT GO-LIVE READY" in result
        assert "🟢" not in result

    def test_issue_counts_in_summary(self):
        """Open issue counts are rendered correctly."""
        verdicts = {"R1": "fail"}
        issues = [
            _make_issue(id=1, severity="critical"),
            _make_issue(id=2, severity="major"),
            _make_issue(id=3, severity="minor"),
            _make_issue(id=4, severity="minor"),
        ]
        result = rg.render_executive_summary(verdicts, issues)
        assert "critical=1" in result
        assert "major=1" in result
        assert "minor=2" in result


# ---------------------------------------------------------------------------
# Open Issues severity grouping tests
# ---------------------------------------------------------------------------


class TestOpenIssuesRendering:
    """Tests for severity-grouped Open Issues rendering."""

    def test_empty_issues(self):
        """No issues → informational message."""
        result = rg.render_open_issues([])
        assert "No open issues recorded" in result

    def test_severity_ordering(self):
        """Issues are grouped critical → major → minor."""
        issues = [
            _make_issue(id=1, severity="minor", summary="Minor issue"),
            _make_issue(id=2, severity="critical", summary="Critical issue"),
            _make_issue(id=3, severity="major", summary="Major issue"),
        ]
        result = rg.render_open_issues(issues)
        # critical should appear before major, major before minor
        crit_pos = result.index("### critical")
        major_pos = result.index("### major")
        minor_pos = result.index("### minor")
        assert crit_pos < major_pos < minor_pos

    def test_issue_fields_rendered(self):
        """Each issue renders id, requirement_id, scenario_id, summary, category, action."""
        issues = [
            _make_issue(
                id=5,
                requirement_id="R12",
                scenario_id="BB-4",
                severity="major",
                summary="PR creation failed",
                category="integration",
                recommended_action="code_change_required",
            )
        ]
        result = rg.render_open_issues(issues)
        assert "#5" in result
        assert "R12/BB-4" in result
        assert "PR creation failed" in result
        assert "category=integration" in result
        assert "action=code_change_required" in result


# ---------------------------------------------------------------------------
# Full report generation integration test
# ---------------------------------------------------------------------------


class TestFullReportGeneration:
    """Integration test for the full report generation pipeline."""

    def test_generates_all_sections(self, evidence_dir, tmp_path):
        """Generated report contains all required D6 sections."""
        # Create minimal evidence
        (evidence_dir / "01-preflight.txt").write_text("nproc=4\nRAM=8192")
        _write_open_issues(evidence_dir, [])

        output = tmp_path / "TEST_REPORT.md"
        rg.generate_report(evidence_dir, output)

        content = output.read_text(encoding="utf-8")
        assert "# Executive Summary" in content
        assert "## Token Selection Result" in content
        assert "## Requirements Verdict Table" in content
        assert "### Property Tests (R21)" in content
        assert "## Open Issues" in content
        assert "## Evidence Index" in content
        assert "## Cost & Cleanup" in content

    def test_evidence_index_lists_files(self, evidence_dir, tmp_path):
        """Evidence Index section lists all files in the evidence directory."""
        (evidence_dir / "01-preflight.txt").write_text("OK")
        (evidence_dir / "07-boot.txt").write_text("OK")
        _write_open_issues(evidence_dir, [])

        output = tmp_path / "TEST_REPORT.md"
        rg.generate_report(evidence_dir, output)

        content = output.read_text(encoding="utf-8")
        assert "vps-test-evidence/01-preflight.txt" in content
        assert "vps-test-evidence/07-boot.txt" in content
