"""Unit tests for platform/scripts/vps_open_issue_logger.py.

Validates:
- R16.1: Valid entries are accepted and monotonic id increments correctly.
- R16.2: Invalid severity, category, recommended_action, summary > 160 chars,
  and requirement_id regex violations each raise ValueError.
- R16.3: severity=critical prints correct prefix to stdout.
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

import vps_open_issue_logger  # noqa: E402
from vps_open_issue_logger import log_open_issue  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_evidence_dir(tmp_path, monkeypatch):
    """Redirect EVIDENCE_DIR and OPEN_ISSUES_FILE to tmp_path for isolation."""
    evidence_dir = tmp_path / "vps-test-evidence"
    evidence_dir.mkdir()
    open_issues_file = evidence_dir / "open-issues.json"

    monkeypatch.setattr(vps_open_issue_logger, "EVIDENCE_DIR", evidence_dir)
    monkeypatch.setattr(vps_open_issue_logger, "OPEN_ISSUES_FILE", open_issues_file)


def _valid_kwargs() -> dict:
    """Return a minimal valid set of keyword arguments for log_open_issue."""
    return {
        "requirement_id": "R10",
        "scenario_id": "JIRA-3",
        "severity": "major",
        "category": "integration",
        "summary": "[VPS-E2E] Smoke test failure on Jira add_comment",
        "evidence_path": "vps-test-evidence/10-jira.json",
        "recommended_action": "manual_fix",
    }


# ---------------------------------------------------------------------------
# R16.1: Valid entry acceptance and monotonic id
# ---------------------------------------------------------------------------


class TestValidEntryAndMonotonicId:
    """Validates: Requirements R16.1"""

    def test_first_entry_returns_id_1(self):
        issue_id = log_open_issue(**_valid_kwargs())
        assert issue_id == 1

    def test_monotonic_id_increments(self):
        id1 = log_open_issue(**_valid_kwargs())
        id2 = log_open_issue(**_valid_kwargs())
        id3 = log_open_issue(**_valid_kwargs())
        assert id1 == 1
        assert id2 == 2
        assert id3 == 3

    def test_entry_persisted_to_json(self, tmp_path):
        log_open_issue(**_valid_kwargs())

        open_issues_file = tmp_path / "vps-test-evidence" / "open-issues.json"
        data = json.loads(open_issues_file.read_text(encoding="utf-8"))
        assert len(data) == 1
        entry = data[0]
        assert entry["id"] == 1
        assert entry["requirement_id"] == "R10"
        assert entry["scenario_id"] == "JIRA-3"
        assert entry["severity"] == "major"
        assert entry["category"] == "integration"
        assert entry["recommended_action"] == "manual_fix"
        assert "logged_at_utc" in entry

    def test_scenario_id_none_is_accepted(self):
        kwargs = _valid_kwargs()
        kwargs["scenario_id"] = None
        issue_id = log_open_issue(**kwargs)
        assert issue_id == 1

    def test_all_valid_severity_values_accepted(self):
        for sev in ("critical", "major", "minor"):
            kwargs = _valid_kwargs()
            kwargs["severity"] = sev
            # Should not raise
            log_open_issue(**kwargs)

    def test_all_valid_category_values_accepted(self):
        for cat in ("config", "code", "doc", "infra", "integration"):
            kwargs = _valid_kwargs()
            kwargs["category"] = cat
            log_open_issue(**kwargs)

    def test_all_valid_recommended_action_values_accepted(self):
        for action in (
            "manual_fix",
            "code_change_required",
            "config_change",
            "doc_update",
            "upstream_atlassian_issue",
        ):
            kwargs = _valid_kwargs()
            kwargs["recommended_action"] = action
            log_open_issue(**kwargs)

    def test_valid_requirement_ids_accepted(self):
        for req_id in ("R1", "R9", "R10", "R19", "R20", "R23"):
            kwargs = _valid_kwargs()
            kwargs["requirement_id"] = req_id
            log_open_issue(**kwargs)


# ---------------------------------------------------------------------------
# R16.2: Validation — each invalid input raises ValueError
# ---------------------------------------------------------------------------


class TestValidationErrors:
    """Validates: Requirements R16.2"""

    def test_invalid_severity_raises(self):
        kwargs = _valid_kwargs()
        kwargs["severity"] = "high"
        with pytest.raises(ValueError, match="Invalid severity"):
            log_open_issue(**kwargs)

    def test_invalid_category_raises(self):
        kwargs = _valid_kwargs()
        kwargs["category"] = "network"
        with pytest.raises(ValueError, match="Invalid category"):
            log_open_issue(**kwargs)

    def test_invalid_recommended_action_raises(self):
        kwargs = _valid_kwargs()
        kwargs["recommended_action"] = "restart_server"
        with pytest.raises(ValueError, match="Invalid recommended_action"):
            log_open_issue(**kwargs)

    def test_summary_exceeding_160_chars_raises(self):
        kwargs = _valid_kwargs()
        kwargs["summary"] = "x" * 161
        with pytest.raises(ValueError, match="exceeds 160 characters"):
            log_open_issue(**kwargs)

    def test_summary_exactly_160_chars_accepted(self):
        kwargs = _valid_kwargs()
        kwargs["summary"] = "x" * 160
        # Should not raise
        issue_id = log_open_issue(**kwargs)
        assert issue_id >= 1

    def test_requirement_id_invalid_format_raises(self):
        invalid_ids = ["R0", "R24", "R100", "10", "RR10", "r10", "R"]
        for bad_id in invalid_ids:
            kwargs = _valid_kwargs()
            kwargs["requirement_id"] = bad_id
            with pytest.raises(ValueError, match="does not match pattern"):
                log_open_issue(**kwargs)


# ---------------------------------------------------------------------------
# R16.3: Critical severity stdout prefix
# ---------------------------------------------------------------------------


class TestCriticalSeverityStdout:
    """Validates: Requirements R16.1, R16.2 (R16.3 behavior)"""

    def test_critical_severity_prints_prefix(self, capsys):
        kwargs = _valid_kwargs()
        kwargs["severity"] = "critical"
        issue_id = log_open_issue(**kwargs)

        captured = capsys.readouterr()
        assert f"[CRITICAL OPEN ISSUE] R{issue_id}" in captured.out

    def test_non_critical_severity_no_prefix(self, capsys):
        kwargs = _valid_kwargs()
        kwargs["severity"] = "major"
        log_open_issue(**kwargs)

        captured = capsys.readouterr()
        assert "[CRITICAL OPEN ISSUE]" not in captured.out

    def test_minor_severity_no_prefix(self, capsys):
        kwargs = _valid_kwargs()
        kwargs["severity"] = "minor"
        log_open_issue(**kwargs)

        captured = capsys.readouterr()
        assert "[CRITICAL OPEN ISSUE]" not in captured.out
