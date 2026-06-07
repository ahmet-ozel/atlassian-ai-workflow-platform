#!/usr/bin/env python3
"""
VPS Report Generator - TEST_REPORT.md renderer (B6).

Reads all evidence artifacts from vps-test-evidence/ and produces the
canonical TEST_REPORT.md at the workspace root.

CLI: python vps_report_generator.py --evidence-dir ./vps-test-evidence --output ./TEST_REPORT.md

Requirements: R20.1, R20.2, R20.3, R20.4, R20.5, R20.6, R23.3
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Requirement metadata: (id, title, evidence_file_pattern)
REQUIREMENTS: list[tuple[str, str, str]] = [
    ("R1", "VPS pre-flight donanım, OS ve port doğrulaması", "01-preflight.txt"),
    ("R2", "VPS yazılım stack kurulumu", "02-install.txt"),
    ("R3", "Repo transfer ve platform/ dizin senkronizasyonu", "03-sync.txt"),
    ("R4", "platform/.env workspace-level credential dosyası", "04-workspace-env-keys.txt"),
    ("R5", "Atlassian_MCP credential ve Bitbucket token seçimi", "05-token-selection.json"),
    ("R6", "Credential leak ve .gitignore doğrulaması", "06-leakcheck.txt"),
    ("R7", "Boot_Bundle başlatılması ve healthcheck", "07-boot.txt"),
    ("R8", "Setup_Wizard yedi adımının yeşillenmesi", "08-wizard.txt"),
    ("R9", "Profile-gated servislerin tam stack durumu", "09-stack.txt"),
    ("R10", "Jira smoke test (5 senaryo)", "10-jira.json"),
    ("R11", "Confluence smoke test (4 senaryo)", "11-confluence.json"),
    ("R12", "Bitbucket smoke test (9 senaryo, çift token)", "12-bitbucket.json"),
    ("R13", "End-to-End task (AI orchestration)", "13-task.json"),
    ("R14", "Jira webhook subscription ve event teslimi", "14-jira-webhook.json"),
    ("R15", "Bitbucket webhook subscription ve event teslimi", "15-bitbucket-webhook.json"),
    ("R16", "Open_Issues ve root-cause sınıflandırması", "open-issues.json"),
    ("R17", "Observability - log, Postgres, Temporal", "17-observability.json"),
    ("R18", "make down ile graceful shutdown", "18-shutdown.txt"),
    ("R19", "VPS imha, faturalama ve credential sweep", "19-teardown.txt"),
]

PROPERTY_TESTS = [
    "test_env_coverage",
    "test_sensitive_key_parity",
    "test_compose_healthcheck_shape",
    "test_log_redaction",
]

# Verdict priority (higher index = higher priority in aggregation)
VERDICT_PRIORITY = ["pass", "manual_pending", "n/a", "partial", "fail"]


# ---------------------------------------------------------------------------
# Verdict aggregation logic
# ---------------------------------------------------------------------------


def aggregate_verdict(
    requirement_id: str,
    evidence_dir: Path,
    evidence_file: str,
    open_issues: list[dict[str, Any]],
) -> str:
    """
    Determine verdict for a requirement based on evidence and open issues.

    Aggregation rule (from design doc):
      fail (≥1 critical/major Open_Issue) > partial (only minor) > n/a > manual_pending > pass
    """
    # Filter open issues for this requirement
    req_issues = [
        oi for oi in open_issues if oi.get("requirement_id") == requirement_id
    ]

    has_critical_or_major = any(
        oi.get("severity") in ("critical", "major") for oi in req_issues
    )
    has_minor_only = (
        len(req_issues) > 0
        and all(oi.get("severity") == "minor" for oi in req_issues)
    )

    if has_critical_or_major:
        return "fail"
    if has_minor_only:
        return "partial"

    # Check if evidence file exists
    evidence_path = evidence_dir / evidence_file
    if not evidence_path.exists():
        return "manual_pending"

    return "pass"


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def render_executive_summary(
    verdicts: dict[str, str],
    open_issues: list[dict[str, Any]],
) -> str:
    """Render the Executive Summary section with 🟢/🔴 prefix (R20.6)."""
    has_fail = any(v == "fail" for v in verdicts.values())

    if has_fail:
        prefix = "🔴 NOT GO-LIVE READY"
    else:
        prefix = "🟢 GO-LIVE READY"

    critical_count = sum(1 for oi in open_issues if oi.get("severity") == "critical")
    major_count = sum(1 for oi in open_issues if oi.get("severity") == "major")
    minor_count = sum(1 for oi in open_issues if oi.get("severity") == "minor")

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# Executive Summary",
        f"",
        f"{prefix}",
        f"",
        f"- Tarih: {now_utc}",
        f"- Toplam Open_Issues: critical={critical_count}, major={major_count}, minor={minor_count}",
        f"",
    ]
    return "\n".join(lines)


def render_token_selection(evidence_dir: Path) -> str:
    """Render Token Selection Result section (R20.3)."""
    lines = ["## Token Selection Result", ""]

    token_file = evidence_dir / "05-token-selection.json"
    if not token_file.exists():
        lines.append("_Token selection evidence not found._")
        lines.append("")
        return "\n".join(lines)

    try:
        with open(token_file, "r", encoding="utf-8") as f:
            token_data = json.load(f)
    except (json.JSONDecodeError, OSError):
        lines.append("_Token selection evidence could not be parsed._")
        lines.append("")
        return "\n".join(lines)

    selection_verdict = token_data.get("selection_verdict", "unknown")
    selected_label = token_data.get("selected_token_label", "unknown")

    lines.append(f"- selection_verdict: **{selection_verdict}**")
    lines.append(f"- selected_token_label: **{selected_label}**")
    lines.append("")

    # Cross-token matrix from 12-bitbucket.json
    lines.append("### Bitbucket cross-token verdict matrix")
    lines.append("")

    bb_file = evidence_dir / "12-bitbucket.json"
    if bb_file.exists():
        try:
            with open(bb_file, "r", encoding="utf-8") as f:
                bb_data = json.load(f)

            # Build cross-token matrix for BB-1, BB-4, BB-5 (R12.9)
            cross_scenarios = ["BB-1", "BB-4", "BB-5"]
            selected_verdicts: dict[str, str] = {}
            alternate_verdicts: dict[str, str] = {}

            for entry in bb_data:
                scenario = entry.get("scenario", "")
                token_mode = entry.get("token_mode", "selected")
                verdict = entry.get("verdict", "n/a")

                if scenario in cross_scenarios:
                    if token_mode == "selected":
                        selected_verdicts[scenario] = verdict
                    elif token_mode == "alternate":
                        alternate_verdicts[scenario] = verdict

            lines.append("| Scenario | Selected | Alternate |")
            lines.append("|---|---|---|")
            for sc in cross_scenarios:
                sel_v = selected_verdicts.get(sc, "n/a")
                alt_v = alternate_verdicts.get(sc, "n/a")
                lines.append(f"| {sc} | {sel_v} | {alt_v} |")
            lines.append("")
        except (json.JSONDecodeError, OSError):
            lines.append("_Bitbucket evidence could not be parsed for cross-token matrix._")
            lines.append("")
    else:
        lines.append("_Bitbucket evidence not found for cross-token matrix._")
        lines.append("")

    return "\n".join(lines)


def render_requirements_verdict_table(verdicts: dict[str, str], evidence_dir: Path) -> str:
    """Render Requirements Verdict Table (R20.2)."""
    lines = [
        "## Requirements Verdict Table",
        "",
        "| Req ID | Title | Verdict | Evidence Path | Notes |",
        "|--------|-------|---------|---------------|-------|",
    ]

    for req_id, title, evidence_file in REQUIREMENTS:
        verdict = verdicts.get(req_id, "manual_pending")
        evidence_path = f"vps-test-evidence/{evidence_file}"
        # Check if evidence exists for notes
        notes = ""
        if not (evidence_dir / evidence_file).exists():
            notes = "evidence not found"
        lines.append(f"| {req_id} | {title} | {verdict} | {evidence_path} | {notes} |")

    lines.append("")
    return "\n".join(lines)


def render_property_tests(evidence_dir: Path, open_issues: list[dict[str, Any]]) -> str:
    """Render Property Tests (R21) sub-table (R21.5)."""
    lines = [
        "### Property Tests (R21)",
        "",
        "| Test | Verdict | Notes |",
        "|------|---------|-------|",
    ]

    # Try to parse property test results from evidence
    pt_file = evidence_dir / "21-property-tests.txt"
    pt_results: dict[str, tuple[str, str]] = {}  # test_name -> (verdict, notes)

    if pt_file.exists():
        try:
            content = pt_file.read_text(encoding="utf-8")
            for test_name in PROPERTY_TESTS:
                if f"{test_name}" in content:
                    # Check if test passed or failed
                    if f"{test_name} PASSED" in content or f"{test_name}::test" in content:
                        # Look for FAILED marker
                        if f"FAILED" in content and test_name in content:
                            # More precise: check if this specific test failed
                            failed_lines = [
                                ln for ln in content.splitlines()
                                if "FAILED" in ln and test_name in ln
                            ]
                            if failed_lines:
                                pt_results[test_name] = ("fail", "see 21-property-tests.txt")
                            else:
                                pt_results[test_name] = ("pass", "")
                        else:
                            pt_results[test_name] = ("pass", "")
                    else:
                        pt_results[test_name] = ("pass", "")
                else:
                    # Test not found in output - might not exist in repo
                    pt_results[test_name] = ("n/a", "not in repo")
        except OSError:
            pass

    # Cross-reference with open issues for R21
    r21_issues = [oi for oi in open_issues if oi.get("requirement_id") == "R21"]

    for test_name in PROPERTY_TESTS:
        if test_name in pt_results:
            verdict, notes = pt_results[test_name]
        else:
            verdict = "n/a"
            notes = "evidence not available"

        # Check if there's an open issue referencing this test
        for oi in r21_issues:
            if test_name in oi.get("summary", ""):
                issue_id = oi.get("id", "?")
                verdict = "n/a" if verdict == "n/a" else "fail"
                notes = f"see Open_Issue #{issue_id}"
                break

        lines.append(f"| {test_name} | {verdict} | {notes} |")

    lines.append("")
    return "\n".join(lines)


def render_open_issues(open_issues: list[dict[str, Any]]) -> str:
    """Render Open Issues section grouped by severity (R20.4)."""
    lines = ["## Open Issues", ""]

    if not open_issues:
        lines.append("_No open issues recorded._")
        lines.append("")
        return "\n".join(lines)

    # Group by severity: critical → major → minor
    severity_order = ["critical", "major", "minor"]
    grouped: dict[str, list[dict[str, Any]]] = {s: [] for s in severity_order}

    for oi in open_issues:
        sev = oi.get("severity", "minor")
        if sev in grouped:
            grouped[sev].append(oi)
        else:
            grouped["minor"].append(oi)

    for severity in severity_order:
        issues = grouped[severity]
        if not issues:
            continue

        lines.append(f"### {severity}")
        lines.append("")
        for oi in issues:
            oi_id = oi.get("id", "?")
            req_id = oi.get("requirement_id", "?")
            scenario_id = oi.get("scenario_id")
            summary = oi.get("summary", "")
            category = oi.get("category", "?")
            action = oi.get("recommended_action", "?")

            ref = f"{req_id}/{scenario_id}" if scenario_id else req_id
            lines.append(
                f"- #{oi_id} [{ref}] {summary} "
                f"(category={category}, action={action})"
            )
        lines.append("")

    return "\n".join(lines)


def render_evidence_index(evidence_dir: Path) -> str:
    """Render Evidence Index - list all files in vps-test-evidence/ (R20.1)."""
    lines = ["## Evidence Index", ""]

    if not evidence_dir.exists():
        lines.append("_Evidence directory not found._")
        lines.append("")
        return "\n".join(lines)

    evidence_files = sorted(evidence_dir.iterdir())
    if not evidence_files:
        lines.append("_No evidence files found._")
        lines.append("")
        return "\n".join(lines)

    for fp in evidence_files:
        if fp.is_file():
            lines.append(f"- vps-test-evidence/{fp.name}")

    lines.append("")
    return "\n".join(lines)


def render_cost_and_cleanup(evidence_dir: Path) -> str:
    """Render Cost & Cleanup section (R20.5, R23.3)."""
    lines = ["## Cost & Cleanup", ""]

    # Try to extract billing info from 19-teardown.txt
    teardown_file = evidence_dir / "19-teardown.txt"
    billed_eur = "N/A"
    billed_hours = "N/A"
    credential_sweep = "NOT performed"

    if teardown_file.exists():
        try:
            content = teardown_file.read_text(encoding="utf-8")
            # Look for EUR amount pattern
            for line in content.splitlines():
                line_lower = line.lower()
                if "eur" in line_lower or "€" in line:
                    # Try to extract numeric value
                    import re
                    eur_match = re.search(r"(\d+[.,]\d+)\s*(?:EUR|€)", line, re.IGNORECASE)
                    if eur_match:
                        billed_eur = f"{eur_match.group(1)} EUR"
                if "hour" in line_lower or "saat" in line_lower or "stunde" in line_lower:
                    import re
                    hours_match = re.search(r"(\d+[.,]?\d*)\s*(?:hour|saat|h\b)", line, re.IGNORECASE)
                    if hours_match:
                        billed_hours = f"{hours_match.group(1)} saat"
                if "[CREDENTIAL SWEEP]" in line:
                    credential_sweep = "performed"
        except OSError:
            pass

    lines.append(f"- Hetzner billed: {billed_eur} ({billed_hours})")
    lines.append(f"- Credential sweep: {credential_sweep}")

    # R23.3: Budget warning if hours > 8
    try:
        hours_num = float(billed_hours.split()[0].replace(",", "."))
        if hours_num > 8:
            lines.append(
                "- ⚠️ [BUDGET] VPS runtime exceeded 8h target - review test efficiency"
            )
    except (ValueError, IndexError):
        pass

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main report generation
# ---------------------------------------------------------------------------


def generate_report(evidence_dir: Path, output_path: Path) -> None:
    """Generate the full TEST_REPORT.md from evidence artifacts."""
    # Load open issues
    open_issues_file = evidence_dir / "open-issues.json"
    open_issues: list[dict[str, Any]] = []
    if open_issues_file.exists():
        try:
            with open(open_issues_file, "r", encoding="utf-8") as f:
                open_issues = json.load(f)
        except (json.JSONDecodeError, OSError):
            open_issues = []

    # Compute verdicts for each requirement
    verdicts: dict[str, str] = {}
    for req_id, _title, evidence_file in REQUIREMENTS:
        verdicts[req_id] = aggregate_verdict(
            req_id, evidence_dir, evidence_file, open_issues
        )

    # Assemble report sections
    sections = [
        render_executive_summary(verdicts, open_issues),
        render_token_selection(evidence_dir),
        render_requirements_verdict_table(verdicts, evidence_dir),
        render_property_tests(evidence_dir, open_issues),
        render_open_issues(open_issues),
        render_evidence_index(evidence_dir),
        render_cost_and_cleanup(evidence_dir),
    ]

    report_content = "\n".join(sections)

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    print(f"[REPORT] Generated: {output_path}")
    print(f"[REPORT] Requirements: {len(verdicts)} evaluated")
    print(f"[REPORT] Open Issues: {len(open_issues)} total")

    # Summary of verdicts
    verdict_counts = {}
    for v in verdicts.values():
        verdict_counts[v] = verdict_counts.get(v, 0) + 1
    for v_name, v_count in sorted(verdict_counts.items()):
        print(f"[REPORT]   {v_name}: {v_count}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vps_report_generator",
        description="Generate TEST_REPORT.md from vps-test-evidence/ artifacts (R20)",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=Path("./vps-test-evidence"),
        help="Path to the evidence directory (default: ./vps-test-evidence)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("./TEST_REPORT.md"),
        help="Output path for TEST_REPORT.md (default: ./TEST_REPORT.md)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry-point. Returns 0 on success."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    evidence_dir = args.evidence_dir.resolve()
    output_path = args.output.resolve()

    if not evidence_dir.exists():
        print(f"WARNING: Evidence directory not found: {evidence_dir}", file=sys.stderr)
        print("Creating empty evidence directory and generating report with manual_pending verdicts.")
        evidence_dir.mkdir(parents=True, exist_ok=True)

    generate_report(evidence_dir, output_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
