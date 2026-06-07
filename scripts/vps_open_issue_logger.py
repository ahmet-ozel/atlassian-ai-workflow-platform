"""
VPS Open Issue Logger - cross-cutting helper (B6).

Provides a single API for all harness scripts to log Open Issues
into `vps-test-evidence/open-issues.json` with strict schema validation.

Requirements: R16.1, R16.2, R16.3, R16.4
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEVERITY_VALUES = ("critical", "major", "minor")
CATEGORY_VALUES = ("config", "code", "doc", "infra", "integration")
RECOMMENDED_ACTION_VALUES = (
    "manual_fix",
    "code_change_required",
    "config_change",
    "doc_update",
    "upstream_atlassian_issue",
)

REQUIREMENT_ID_REGEX = re.compile(r"^R(1[0-9]|2[0-3]|[1-9])$")
EVIDENCE_PATH_PREFIX = "vps-test-evidence/"
MAX_SUMMARY_LENGTH = 160

# Evidence directory and file path - relative to workspace root.
# The workspace root is determined by walking up from this script's location
# until we find the `vps-test-evidence` parent or default to two levels up
# from `platform/scripts/`.
_SCRIPT_DIR = Path(__file__).resolve().parent
_WORKSPACE_ROOT = _SCRIPT_DIR.parent.parent  # platform/scripts -> platform -> workspace root
EVIDENCE_DIR = _WORKSPACE_ROOT / "vps-test-evidence"
OPEN_ISSUES_FILE = EVIDENCE_DIR / "open-issues.json"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_severity(value: str) -> str:
    if value not in SEVERITY_VALUES:
        raise ValueError(
            f"Invalid severity '{value}'. Must be one of: {', '.join(SEVERITY_VALUES)}"
        )
    return value


def _validate_category(value: str) -> str:
    if value not in CATEGORY_VALUES:
        raise ValueError(
            f"Invalid category '{value}'. Must be one of: {', '.join(CATEGORY_VALUES)}"
        )
    return value


def _validate_recommended_action(value: str) -> str:
    if value not in RECOMMENDED_ACTION_VALUES:
        raise ValueError(
            f"Invalid recommended_action '{value}'. Must be one of: {', '.join(RECOMMENDED_ACTION_VALUES)}"
        )
    return value


def _validate_summary(value: str) -> str:
    if len(value) > MAX_SUMMARY_LENGTH:
        raise ValueError(
            f"Summary exceeds {MAX_SUMMARY_LENGTH} characters (got {len(value)})"
        )
    if not value.strip():
        raise ValueError("Summary must not be empty or whitespace-only")
    return value


def _validate_evidence_path(value: str) -> str:
    if not value.startswith(EVIDENCE_PATH_PREFIX):
        raise ValueError(
            f"evidence_path must start with '{EVIDENCE_PATH_PREFIX}', got '{value}'"
        )
    return value


def _validate_requirement_id(value: str) -> str:
    if not REQUIREMENT_ID_REGEX.match(value):
        raise ValueError(
            f"requirement_id '{value}' does not match pattern {REQUIREMENT_ID_REGEX.pattern}"
        )
    return value


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def log_open_issue(
    requirement_id: str,
    scenario_id: str | None,
    severity: Literal["critical", "major", "minor"],
    category: Literal["config", "code", "doc", "infra", "integration"],
    summary: str,
    evidence_path: str,
    recommended_action: Literal[
        "manual_fix",
        "code_change_required",
        "config_change",
        "doc_update",
        "upstream_atlassian_issue",
    ],
) -> int:
    """
    Append a new Open Issue entry to vps-test-evidence/open-issues.json.

    Returns the monotonically increasing issue id (starting from 1).

    Raises ValueError if any argument fails validation.
    """
    # Validate all inputs
    _validate_requirement_id(requirement_id)
    _validate_severity(severity)
    _validate_category(category)
    _validate_summary(summary)
    _validate_evidence_path(evidence_path)
    _validate_recommended_action(recommended_action)

    # Ensure evidence directory exists
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing issues (append-only - R16.4)
    if OPEN_ISSUES_FILE.exists():
        with open(OPEN_ISSUES_FILE, "r", encoding="utf-8") as f:
            issues: list[dict] = json.load(f)
    else:
        issues = []

    # Determine next monotonic id (starting from 1)
    if issues:
        next_id = max(issue["id"] for issue in issues) + 1
    else:
        next_id = 1

    # Build entry
    entry = {
        "id": next_id,
        "requirement_id": requirement_id,
        "scenario_id": scenario_id,
        "severity": severity,
        "category": category,
        "summary": summary,
        "evidence_path": evidence_path,
        "recommended_action": recommended_action,
        "logged_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    issues.append(entry)

    # Write back (atomic-ish: write to same file)
    with open(OPEN_ISSUES_FILE, "w", encoding="utf-8") as f:
        json.dump(issues, f, indent=2, ensure_ascii=False)

    # R16.3: critical severity → stdout alert
    if severity == "critical":
        print(f"[CRITICAL OPEN ISSUE] R{next_id}")

    return next_id


# ---------------------------------------------------------------------------
# CLI entry-point: python -m vps_open_issue_logger ...
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vps_open_issue_logger",
        description="Log an Open Issue to vps-test-evidence/open-issues.json",
    )
    parser.add_argument(
        "--requirement",
        required=True,
        help="Requirement ID, e.g. R10 (must match ^R(1[0-9]|2[0-3]|[1-9])$)",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        help="Scenario ID, e.g. JIRA-3 (optional)",
    )
    parser.add_argument(
        "--severity",
        required=True,
        choices=SEVERITY_VALUES,
        help="Issue severity",
    )
    parser.add_argument(
        "--category",
        required=True,
        choices=CATEGORY_VALUES,
        help="Issue category",
    )
    parser.add_argument(
        "--summary",
        required=True,
        help="Issue summary (max 160 chars)",
    )
    parser.add_argument(
        "--evidence-path",
        required=True,
        help="Path to evidence file (must start with 'vps-test-evidence/')",
    )
    parser.add_argument(
        "--recommended-action",
        required=True,
        choices=RECOMMENDED_ACTION_VALUES,
        help="Recommended action to resolve the issue",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry-point. Returns exit code 0 on success, 1 on validation error."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        issue_id = log_open_issue(
            requirement_id=args.requirement,
            scenario_id=args.scenario,
            severity=args.severity,
            category=args.category,
            summary=args.summary,
            evidence_path=args.evidence_path,
            recommended_action=args.recommended_action,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"Logged Open Issue #{issue_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
