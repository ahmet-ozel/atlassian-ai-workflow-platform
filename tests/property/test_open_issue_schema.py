"""invariant for Open_Issue schema validity.



Uses Hypothesis strategies to generate valid and invalid Open_Issue entries,
verifying that:
1. Valid entries are accepted by the logger and produce correct schema output.
2. Monotonic id invariant holds: entry[i+1].id > entry[i].id for consecutive entries.
3. Invalid entries are rejected with ValueError.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

# Add scripts directory to sys.path so we can import the logger module.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import vps_open_issue_logger  # noqa: E402
from vps_open_issue_logger import (  # noqa: E402
    log_open_issue,
    SEVERITY_VALUES,
    CATEGORY_VALUES,
    RECOMMENDED_ACTION_VALUES,
    REQUIREMENT_ID_REGEX,
    EVIDENCE_PATH_PREFIX,
    MAX_SUMMARY_LENGTH,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Valid the operational rule IDs: -, -, -
_valid_requirement_ids = st.sampled_from(
    [f"R{i}" for i in range(1, 24)]
)

_valid_severities = st.sampled_from(list(SEVERITY_VALUES))
_valid_categories = st.sampled_from(list(CATEGORY_VALUES))
_valid_recommended_actions = st.sampled_from(list(RECOMMENDED_ACTION_VALUES))

# Summary: non-empty, max 160 chars, printable text
_valid_summaries = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z", "S")),
    min_size=1,
    max_size=MAX_SUMMARY_LENGTH,
).filter(lambda s: s.strip())

# Evidence path: must start with "vps-test-evidence/"
_valid_evidence_paths = st.builds(
    lambda suffix: f"{EVIDENCE_PATH_PREFIX}{suffix}",
    st.from_regex(r"[a-z0-9\-_/]+\.[a-z]{2,4}", fullmatch=True),
)

# Scenario ID: optional, e.g. "JIRA-3", "BB-1", "CONF-2", or None
_valid_scenario_ids = st.one_of(
    st.none(),
    st.from_regex(r"[A-Z]{2,5}-\d{1,3}", fullmatch=True),
)

# Full valid entry strategy
_valid_entry_strategy = st.fixed_dictionaries({
    "requirement_id": _valid_requirement_ids,
    "scenario_id": _valid_scenario_ids,
    "severity": _valid_severities,
    "category": _valid_categories,
    "summary": _valid_summaries,
    "evidence_path": _valid_evidence_paths,
    "recommended_action": _valid_recommended_actions,
})

# Invalid strategies — each violates exactly one constraint
_invalid_severity = st.text(min_size=1, max_size=20).filter(
    lambda s: s not in SEVERITY_VALUES
)
_invalid_category = st.text(min_size=1, max_size=20).filter(
    lambda s: s not in CATEGORY_VALUES
)
_invalid_recommended_action = st.text(min_size=1, max_size=30).filter(
    lambda s: s not in RECOMMENDED_ACTION_VALUES
)
_invalid_summary_too_long = st.text(
    min_size=MAX_SUMMARY_LENGTH + 1,
    max_size=MAX_SUMMARY_LENGTH + 50,
)
_invalid_evidence_path = st.text(min_size=1, max_size=50).filter(
    lambda s: not s.startswith(EVIDENCE_PATH_PREFIX)
)
_invalid_requirement_id = st.text(min_size=1, max_size=10).filter(
    lambda s: not REQUIREMENT_ID_REGEX.match(s)
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _isolate_logger(tmp_dir: Path):
    """Patch the logger module to use an isolated temp directory."""
    evidence_dir = tmp_dir / "vps-test-evidence"
    evidence_dir.mkdir(exist_ok=True)
    open_issues_file = evidence_dir / "open-issues.json"
    return (
        patch.object(vps_open_issue_logger, "EVIDENCE_DIR", evidence_dir),
        patch.object(vps_open_issue_logger, "OPEN_ISSUES_FILE", open_issues_file),
        open_issues_file,
    )


# ---------------------------------------------------------------------------
# invariant: Valid entries are accepted and produce correct schema
# ---------------------------------------------------------------------------


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(entry=_valid_entry_strategy)
def test_valid_entry_accepted_and_schema_correct(entry):
    """Valid Open_Issue entries are accepted by the logger without error.


 """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        patch_ed, patch_file, open_issues_file = _isolate_logger(tmp_path)

        with patch_ed, patch_file:
            issue_id = log_open_issue(**entry)

            # id must be a positive integer
            assert isinstance(issue_id, int)
            assert issue_id >= 1

            # Verify persisted entry matches schema
            data = json.loads(open_issues_file.read_text(encoding="utf-8"))
            assert len(data) == 1
            persisted = data[0]

            assert persisted["id"] == issue_id
            assert persisted["requirement_id"] == entry["requirement_id"]
            assert persisted["scenario_id"] == entry["scenario_id"]
            assert persisted["severity"] in SEVERITY_VALUES
            assert persisted["category"] in CATEGORY_VALUES
            assert persisted["recommended_action"] in RECOMMENDED_ACTION_VALUES
            assert len(persisted["summary"]) <= MAX_SUMMARY_LENGTH
            assert persisted["evidence_path"].startswith(EVIDENCE_PATH_PREFIX)
            assert REQUIREMENT_ID_REGEX.match(persisted["requirement_id"])
            assert "logged_at_utc" in persisted


# ---------------------------------------------------------------------------
# invariant: Monotonic id invariant across consecutive entries
# ---------------------------------------------------------------------------


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(entries=st.lists(_valid_entry_strategy, min_size=2, max_size=10))
def test_monotonic_id_invariant(entries):
    """Consecutive Open_Issue entries have strictly increasing ids.


 """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        patch_ed, patch_file, open_issues_file = _isolate_logger(tmp_path)

        with patch_ed, patch_file:
            ids = []
            for entry in entries:
                issue_id = log_open_issue(**entry)
                ids.append(issue_id)

            # Verify monotonic increase
            for i in range(len(ids) - 1):
                assert ids[i + 1] > ids[i], (
                    f"Monotonic id violated: entry[{i}].id={ids[i]} "
                    f">= entry[{i+1}].id={ids[i+1]}"
                )

            # Also verify from persisted file
            data = json.loads(open_issues_file.read_text(encoding="utf-8"))
            assert len(data) == len(entries)
            for i in range(len(data) - 1):
                assert data[i + 1]["id"] > data[i]["id"]


# ---------------------------------------------------------------------------
# invariant: Invalid entries are rejected with ValueError
# ---------------------------------------------------------------------------


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(bad_severity=_invalid_severity)
def test_invalid_severity_rejected(bad_severity):
    """Invalid severity values are rejected with ValueError.


 """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        patch_ed, patch_file, _ = _isolate_logger(tmp_path)

        with patch_ed, patch_file:
            with pytest.raises(ValueError):
                log_open_issue(
                    requirement_id="the operational rule",
                    scenario_id=None,
                    severity=bad_severity,
                    category="config",
                    summary="Test invalid severity",
                    evidence_path="vps-test-evidence/test.json",
                    recommended_action="manual_fix",
                )


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(bad_category=_invalid_category)
def test_invalid_category_rejected(bad_category):
    """Invalid category values are rejected with ValueError.


 """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        patch_ed, patch_file, _ = _isolate_logger(tmp_path)

        with patch_ed, patch_file:
            with pytest.raises(ValueError):
                log_open_issue(
                    requirement_id="the operational rule",
                    scenario_id=None,
                    severity="major",
                    category=bad_category,
                    summary="Test invalid category",
                    evidence_path="vps-test-evidence/test.json",
                    recommended_action="manual_fix",
                )


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(bad_action=_invalid_recommended_action)
def test_invalid_recommended_action_rejected(bad_action):
    """Invalid recommended_action values are rejected with ValueError.


 """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        patch_ed, patch_file, _ = _isolate_logger(tmp_path)

        with patch_ed, patch_file:
            with pytest.raises(ValueError):
                log_open_issue(
                    requirement_id="the operational rule",
                    scenario_id=None,
                    severity="major",
                    category="config",
                    summary="Test invalid action",
                    evidence_path="vps-test-evidence/test.json",
                    recommended_action=bad_action,
                )


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(bad_summary=_invalid_summary_too_long)
def test_invalid_summary_too_long_rejected(bad_summary):
    """Summaries exceeding 160 characters are rejected with ValueError.


 """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        patch_ed, patch_file, _ = _isolate_logger(tmp_path)

        with patch_ed, patch_file:
            with pytest.raises(ValueError):
                log_open_issue(
                    requirement_id="the operational rule",
                    scenario_id=None,
                    severity="major",
                    category="config",
                    summary=bad_summary,
                    evidence_path="vps-test-evidence/test.json",
                    recommended_action="manual_fix",
                )


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(bad_path=_invalid_evidence_path)
def test_invalid_evidence_path_rejected(bad_path):
    """Evidence paths not starting with 'vps-test-evidence/' are rejected.


 """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        patch_ed, patch_file, _ = _isolate_logger(tmp_path)

        with patch_ed, patch_file:
            with pytest.raises(ValueError):
                log_open_issue(
                    requirement_id="the operational rule",
                    scenario_id=None,
                    severity="major",
                    category="config",
                    summary="Test invalid path",
                    evidence_path=bad_path,
                    recommended_action="manual_fix",
                )


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(bad_req_id=_invalid_requirement_id)
def test_invalid_requirement_id_rejected(bad_req_id):
    """the operational rule IDs not matching ^R(1[0-9]|2[0-3]|[1-9])$ are rejected.


 """
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        patch_ed, patch_file, _ = _isolate_logger(tmp_path)

        with patch_ed, patch_file:
            with pytest.raises(ValueError):
                log_open_issue(
                    requirement_id=bad_req_id,
                    scenario_id=None,
                    severity="major",
                    category="config",
                    summary="Test invalid the operational rule id",
                    evidence_path="vps-test-evidence/test.json",
                    recommended_action="manual_fix",
                )
