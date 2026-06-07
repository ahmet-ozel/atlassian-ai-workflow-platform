"""
VPS Webhook Probe - webhook delivery polling + audit_events query (R14/R15).

This script verifies that Jira and Bitbucket webhook subscriptions deliver
events to the automation-service and that those events are recorded in the
``automation.audit_events`` table with correct event_type and payload fields.

Requirements:
  R14.1-R14.6 - Jira webhook subscription and real event delivery
  R15.1-R15.5 - Bitbucket webhook subscription and real event delivery

Usage:
  # Jira webhook probe
  python vps_webhook_probe.py --provider jira --public-endpoint https://xyz.trycloudflare.com --secret <hmac>

  # Bitbucket webhook probe
  python vps_webhook_probe.py --provider bitbucket --public-endpoint https://xyz.trycloudflare.com --secret <hmac>

The script runs on VPS_Host and expects:
  - PostgreSQL accessible via docker compose exec
  - automation-service running and receiving webhook deliveries
  - Operator has already subscribed the webhook in the respective admin panel
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = SCRIPT_DIR.parent.parent  # platform/scripts -> platform -> workspace root
EVIDENCE_DIR = WORKSPACE_ROOT / "vps-test-evidence"
PLATFORM_DIR = SCRIPT_DIR.parent  # platform/

COMPOSE_FILE = "infra/docker-compose.yml"
PSQL_CMD_PREFIX = [
    "docker", "compose", "-f", COMPOSE_FILE,
    "exec", "-T", "postgres",
    "psql", "-U", "ai", "-d", "ai", "-t", "-A", "-c",
]

POLL_INTERVAL_S = 5
POLL_TIMEOUT_S = 30

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_psql(sql: str, cwd: Path | None = None) -> str:
    """Execute a psql query via docker compose exec and return stdout."""
    cmd = PSQL_CMD_PREFIX + [sql]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd or PLATFORM_DIR,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"psql query failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _capture_automation_logs(tail: int = 500, grep_pattern: str | None = None) -> str:
    """Capture automation-service logs, optionally filtering by pattern."""
    cmd = [
        "docker", "compose", "-f", COMPOSE_FILE,
        "logs", "--tail", str(tail), "--no-color", "automation-service",
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=PLATFORM_DIR,
        timeout=30,
    )
    output = result.stdout + result.stderr
    if grep_pattern:
        lines = [line for line in output.splitlines() if grep_pattern in line]
        return "\n".join(lines)
    return output


def _ensure_evidence_dir() -> None:
    """Create evidence directory if it doesn't exist."""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)


def _write_evidence(filename: str, data: Any) -> Path:
    """Write JSON evidence file."""
    _ensure_evidence_dir()
    filepath = EVIDENCE_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return filepath


def _write_text_evidence(filename: str, content: str) -> Path:
    """Write text evidence file."""
    _ensure_evidence_dir()
    filepath = EVIDENCE_DIR / filename
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath


def _log_open_issue(
    requirement_id: str,
    scenario_id: str | None,
    severity: str,
    category: str,
    summary: str,
    evidence_path: str,
    recommended_action: str,
) -> int:
    """Log an open issue using the vps_open_issue_logger module."""
    sys.path.insert(0, str(SCRIPT_DIR))
    from vps_open_issue_logger import log_open_issue
    return log_open_issue(
        requirement_id=requirement_id,
        scenario_id=scenario_id,
        severity=severity,
        category=category,
        summary=summary,
        evidence_path=evidence_path,
        recommended_action=recommended_action,
    )


def _now_utc() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Jira Webhook Probe (R14)
# ---------------------------------------------------------------------------


def probe_jira_webhook(public_endpoint: str, secret: str) -> int:
    """
    Probe Jira webhook delivery and audit_events recording.

    Workflow:
    1. Operator has already subscribed webhook in Atlassian admin panel (R14.1)
    2. Operator creates a test issue in project JOH (R14.2)
    3. This script polls audit_events for webhook.jira.received (R14.2, R14.3)
    4. On failure: capture automation-service logs (R14.4)
    5. Emit evidence file (R14.6)

    Returns exit code: 0 = pass, 1 = fail.
    """
    print("=" * 60)
    print("JIRA WEBHOOK PROBE (R14)")
    print("=" * 60)
    print()
    print(f"Public Endpoint: {public_endpoint}")
    print(f"Secret configured: {'yes' if secret else 'no'}")
    print()

    # Record webhook config evidence (R14.1)
    config_evidence = {
        "provider": "jira",
        "public_endpoint": public_endpoint,
        "webhook_url": f"{public_endpoint}/api/v1/webhooks/jira",
        "jql_filter": "project = JOH",
        "secret_configured": bool(secret),
        "recorded_at_utc": _now_utc(),
    }
    _write_evidence("14-jira-webhook-config.json", config_evidence)
    print("[INFO] Webhook config recorded to 14-jira-webhook-config.json")

    # R14.1: Prompt operator to subscribe webhook in Atlassian admin panel
    print()
    print("=" * 60)
    print("STEP 1 - SUBSCRIBE JIRA WEBHOOK (R14.1)")
    print("=" * 60)
    print()
    print("  Operator action: Subscribe a Jira webhook in the Atlassian admin panel.")
    print()
    print("  1. Open: https://example.atlassian.net/plugins/servlet/webhooks")
    print("  2. Click 'Create a Webhook' and configure:")
    print(f"     • Name:   VPS-E2E Jira Webhook")
    print(f"     • URL:    {public_endpoint}/api/v1/webhooks/jira")
    print(f"     • Secret: {secret if secret else '(leave empty or set HMAC)'}")
    print("     • Events: Issue → created, updated")
    print("     • JQL:    project = JOH")
    print("  3. Save the webhook.")
    print()
    print("-" * 60)
    print("  HTTPS FALLBACK (R14.5):")
    print()
    print("  If Atlassian Cloud rejects the URL with error:")
    print("    'Webhook URL must use HTTPS'")
    print()
    print("  Then provision a cloudflared tunnel on VPS:")
    print("    $ cloudflared tunnel --url http://localhost:80")
    print()
    print("  Use the generated HTTPS URL (e.g. https://xxx.trycloudflare.com)")
    print("  as the webhook base URL. Re-run this script with:")
    print("    --public-endpoint https://<tunnel-url>")
    print("-" * 60)
    print()

    # Prompt operator to create test issue (R14.2)
    print("=" * 60)
    print("STEP 2 - CREATE TEST ISSUE (R14.2)")
    print("=" * 60)
    print()
    print("  Operator action: Create a Jira issue in project JOH with summary:")
    print("    [VPS-E2E] webhook test")
    print()
    print("  Then press ENTER to start polling for webhook delivery...")
    print("=" * 60)

    try:
        input()
    except EOFError:
        pass

    # Poll audit_events for webhook.jira.received (R14.2, R14.3)
    print(f"\n[INFO] Polling audit_events for webhook.jira.received (timeout={POLL_TIMEOUT_S}s)...")

    sql = (
        "SELECT id, event_type, payload::text "
        "FROM automation.audit_events "
        "WHERE event_type = 'webhook.jira.received' "
        "ORDER BY id DESC LIMIT 5;"
    )

    start_time = time.time()
    audit_row_id: str | None = None
    audit_payload: str | None = None

    while time.time() - start_time < POLL_TIMEOUT_S:
        try:
            result = _run_psql(sql)
            if result and "webhook.jira.received" in result:
                # Parse the first matching row
                lines = [l for l in result.strip().splitlines() if l.strip()]
                if lines:
                    parts = lines[0].split("|")
                    audit_row_id = parts[0].strip() if len(parts) > 0 else None
                    audit_payload = parts[2].strip() if len(parts) > 2 else None
                    print(f"[PASS] Webhook event found! audit_row_id={audit_row_id}")
                    break
        except RuntimeError as e:
            print(f"[WARN] psql query error: {e}")

        time.sleep(POLL_INTERVAL_S)
        elapsed = int(time.time() - start_time)
        print(f"  ... polling ({elapsed}s / {POLL_TIMEOUT_S}s)")

    # Evaluate result
    if audit_row_id:
        # Success - emit evidence (R14.6)
        evidence = {
            "provider": "jira",
            "verdict": "pass",
            "subscription_id": "operator-configured",
            "test_issue_key": "JOH-xxx (operator-created)",
            "audit_row_id": audit_row_id,
            "public_endpoint": public_endpoint,
            "payload_excerpt": (audit_payload or "")[:256],
            "poll_duration_s": int(time.time() - start_time),
            "captured_at_utc": _now_utc(),
        }
        _write_evidence("14-jira-webhook.json", evidence)
        print("\n[PASS] Jira webhook probe PASSED")
        print(f"  Evidence: vps-test-evidence/14-jira-webhook.json")
        return 0
    else:
        # Failure - capture logs (R14.4)
        print(f"\n[FAIL] No webhook.jira.received event found within {POLL_TIMEOUT_S}s")
        logs = _capture_automation_logs(tail=500, grep_pattern="webhook")
        fail_log_path = _write_text_evidence("14-jira-webhook-fail.log", logs)
        print(f"  Failure logs: {fail_log_path}")

        # Log Open Issue (R14.4)
        _log_open_issue(
            requirement_id="R14",
            scenario_id=None,
            severity="major",
            category="infra",
            summary="Jira webhook delivery not received within 30s polling window",
            evidence_path="vps-test-evidence/14-jira-webhook-fail.log",
            recommended_action="manual_fix",
        )

        # Still emit evidence with fail verdict (R14.6)
        evidence = {
            "provider": "jira",
            "verdict": "fail",
            "subscription_id": "operator-configured",
            "test_issue_key": "JOH-xxx (operator-created)",
            "audit_row_id": None,
            "public_endpoint": public_endpoint,
            "payload_excerpt": None,
            "poll_duration_s": POLL_TIMEOUT_S,
            "failure_reason": "No webhook.jira.received audit event within timeout",
            "captured_at_utc": _now_utc(),
        }
        _write_evidence("14-jira-webhook.json", evidence)
        print("\n[FAIL] Jira webhook probe FAILED")
        return 1


# ---------------------------------------------------------------------------
# Bitbucket Webhook Probe (R15)
# ---------------------------------------------------------------------------


def probe_bitbucket_webhook(public_endpoint: str, secret: str) -> int:
    """
    Probe Bitbucket webhook delivery and audit_events recording.

    Workflow:
    1. Operator subscribes webhook in example_workspace/smoke-test repo settings
       with events repo:push, pullrequest:created (R15.1)
    2. Operator pushes branch ai/test-branch-vps-e2e-webhook-{epoch} (R15.2)
    3. This script polls audit_events for webhook.bitbucket.received
       with payload->>'repository.full_name' == 'example_workspace/smoke-test' (R15.2, R15.3)
    4. On failure: capture automation-service logs (R15.4)
    5. Emit evidence file (R15.5)

    Returns exit code: 0 = pass, 1 = fail.
    """
    epoch = int(time.time())
    branch_name = f"ai/test-branch-vps-e2e-webhook-{epoch}"

    print("=" * 60)
    print("BITBUCKET WEBHOOK PROBE (R15)")
    print("=" * 60)
    print()
    print(f"Public Endpoint: {public_endpoint}")
    print(f"Secret configured: {'yes' if secret else 'no'}")
    print(f"Expected branch: {branch_name}")
    print()

    # Record webhook config evidence (R15.1)
    config_evidence = {
        "provider": "bitbucket",
        "public_endpoint": public_endpoint,
        "webhook_url": f"{public_endpoint}/api/v1/webhooks/bitbucket",
        "repository": "example_workspace/smoke-test",
        "events": ["repo:push", "pullrequest:created"],
        "secret_configured": bool(secret),
        "expected_branch": branch_name,
        "recorded_at_utc": _now_utc(),
    }
    _write_evidence("15-bitbucket-webhook-config.json", config_evidence)
    print("[INFO] Webhook config recorded to 15-bitbucket-webhook-config.json")

    # R15.1: Prompt operator to subscribe webhook in Bitbucket repo settings
    print()
    print("=" * 60)
    print("STEP 1 - SUBSCRIBE BITBUCKET WEBHOOK (R15.1)")
    print("=" * 60)
    print()
    print("  Operator action: Subscribe a webhook in example_workspace/smoke-test repo settings.")
    print()
    print("  1. Open: https://bitbucket.org/example_workspace/smoke-test/admin/webhooks")
    print("  2. Click 'Add webhook' and configure:")
    print(f"     • Title:    VPS-E2E Bitbucket Webhook")
    print(f"     • URL:      {public_endpoint}/api/v1/webhooks/bitbucket")
    print(f"     • Secret:   {secret if secret else '(leave empty or set HMAC)'}")
    print("     • Triggers: ☑ Repository → Push")
    print("                  ☑ Pull Request → Created")
    print("  3. Save and note the subscription UUID.")
    print()
    print("-" * 60)
    print("  HTTPS FALLBACK (R14.5):")
    print()
    print("  If Bitbucket requires HTTPS for webhook URLs, provision a")
    print("  cloudflared tunnel on VPS:")
    print("    $ cloudflared tunnel --url http://localhost:80")
    print()
    print("  Use the generated HTTPS URL as the webhook base URL.")
    print("  Re-run this script with: --public-endpoint https://<tunnel-url>")
    print("-" * 60)
    print()

    # R15.2: Prompt operator to push a branch
    print("=" * 60)
    print("STEP 2 - PUSH A BRANCH TO TRIGGER WEBHOOK (R15.2)")
    print("=" * 60)
    print()
    print("  Operator action: Push a single-commit branch to example_workspace/smoke-test.")
    print()
    print(f"     Branch name: {branch_name}")
    print()
    print("     Example commands (from a local clone of smoke-test):")
    print(f"       git checkout -b {branch_name}")
    print(f"       echo 'webhook test {epoch}' > webhook-test.md")
    print("       git add webhook-test.md")
    print(f"       git commit -m '[VPS-E2E] webhook test push {epoch}'")
    print(f"       git push origin {branch_name}")
    print()
    print("  Then press ENTER to start polling for webhook delivery...")
    print("=" * 60)

    try:
        input()
    except EOFError:
        pass

    # Poll audit_events for webhook.bitbucket.received (R15.2, R15.3)
    print(f"\n[INFO] Polling audit_events for webhook.bitbucket.received (timeout={POLL_TIMEOUT_S}s)...")
    print("  Expected: event_type='webhook.bitbucket.received'")
    print("  Expected: payload->>'repository.full_name' == 'example_workspace/smoke-test'")

    # Query for Bitbucket webhook events with repository filter
    sql = (
        "SELECT id, event_type, payload::text "
        "FROM automation.audit_events "
        "WHERE event_type = 'webhook.bitbucket.received' "
        "ORDER BY id DESC LIMIT 10;"
    )

    # More specific query checking repository.full_name in payload
    sql_specific = (
        "SELECT id, event_type, payload::text "
        "FROM automation.audit_events "
        "WHERE event_type = 'webhook.bitbucket.received' "
        "AND (payload->>'repository.full_name' = 'example_workspace/smoke-test' "
        "     OR payload->'repository'->>'full_name' = 'example_workspace/smoke-test') "
        "ORDER BY id DESC LIMIT 5;"
    )

    start_time = time.time()
    audit_row_id: str | None = None
    audit_payload: str | None = None
    matched_via: str = ""

    while time.time() - start_time < POLL_TIMEOUT_S:
        # Try the specific query first (nested JSON path)
        try:
            result = _run_psql(sql_specific)
            if result and result.strip():
                lines = [l for l in result.strip().splitlines() if l.strip()]
                if lines:
                    parts = lines[0].split("|")
                    audit_row_id = parts[0].strip() if len(parts) > 0 else None
                    audit_payload = parts[2].strip() if len(parts) > 2 else None
                    matched_via = "specific_query"
                    print(f"[PASS] Bitbucket webhook event found (specific match)! audit_row_id={audit_row_id}")
                    break
        except RuntimeError:
            pass

        # Fallback: try the general query and check payload manually
        try:
            result = _run_psql(sql)
            if result and "webhook.bitbucket.received" in result:
                lines = [l for l in result.strip().splitlines() if l.strip()]
                for line in lines:
                    if "example_workspace/smoke-test" in line:
                        parts = line.split("|")
                        audit_row_id = parts[0].strip() if len(parts) > 0 else None
                        audit_payload = parts[2].strip() if len(parts) > 2 else None
                        matched_via = "general_query_payload_scan"
                        print(f"[PASS] Bitbucket webhook event found (payload scan)! audit_row_id={audit_row_id}")
                        break
                if audit_row_id:
                    break
        except RuntimeError as e:
            print(f"[WARN] psql query error: {e}")

        time.sleep(POLL_INTERVAL_S)
        elapsed = int(time.time() - start_time)
        print(f"  ... polling ({elapsed}s / {POLL_TIMEOUT_S}s)")

    # Evaluate result
    if audit_row_id:
        # Success - emit evidence (R15.5)
        evidence = {
            "provider": "bitbucket",
            "verdict": "pass",
            "subscription_uuid": "operator-configured",
            "branch_name": branch_name,
            "repository": "example_workspace/smoke-test",
            "audit_row_id": audit_row_id,
            "public_endpoint": public_endpoint,
            "payload_excerpt": (audit_payload or "")[:256],
            "matched_via": matched_via,
            "poll_duration_s": int(time.time() - start_time),
            "captured_at_utc": _now_utc(),
        }
        _write_evidence("15-bitbucket-webhook.json", evidence)
        print("\n[PASS] Bitbucket webhook probe PASSED")
        print(f"  Evidence: vps-test-evidence/15-bitbucket-webhook.json")
        return 0
    else:
        # Failure - capture logs (R15.4)
        print(f"\n[FAIL] No webhook.bitbucket.received event found within {POLL_TIMEOUT_S}s")
        logs = _capture_automation_logs(tail=500, grep_pattern="webhook")
        fail_log_path = _write_text_evidence("15-bitbucket-webhook-fail.log", logs)
        print(f"  Failure logs: {fail_log_path}")

        # Log Open Issue (R15.4)
        _log_open_issue(
            requirement_id="R15",
            scenario_id=None,
            severity="major",
            category="infra",
            summary="Bitbucket webhook delivery not received within 30s polling window",
            evidence_path="vps-test-evidence/15-bitbucket-webhook-fail.log",
            recommended_action="manual_fix",
        )

        # Still emit evidence with fail verdict (R15.5)
        evidence = {
            "provider": "bitbucket",
            "verdict": "fail",
            "subscription_uuid": "operator-configured",
            "branch_name": branch_name,
            "repository": "example_workspace/smoke-test",
            "audit_row_id": None,
            "public_endpoint": public_endpoint,
            "payload_excerpt": None,
            "poll_duration_s": POLL_TIMEOUT_S,
            "failure_reason": "No webhook.bitbucket.received audit event with repository.full_name='example_workspace/smoke-test' within timeout",
            "captured_at_utc": _now_utc(),
        }
        _write_evidence("15-bitbucket-webhook.json", evidence)
        print("\n[FAIL] Bitbucket webhook probe FAILED")
        return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vps_webhook_probe",
        description=(
            "VPS Webhook Probe - polls audit_events for webhook delivery "
            "confirmation after operator subscribes and triggers a webhook event. "
            "Supports Jira (R14) and Bitbucket (R15) providers."
        ),
    )
    parser.add_argument(
        "--provider",
        required=True,
        choices=["jira", "bitbucket"],
        help="Webhook provider to probe: 'jira' (R14) or 'bitbucket' (R15)",
    )
    parser.add_argument(
        "--public-endpoint",
        required=True,
        help=(
            "The public HTTPS endpoint URL where webhooks are delivered. "
            "Example: https://xyz.trycloudflare.com or http://91.99.149.163"
        ),
    )
    parser.add_argument(
        "--secret",
        default="",
        help="HMAC secret configured for the webhook subscription (optional)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry-point. Returns exit code 0 on success, 1 on failure."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Normalize endpoint (strip trailing slash)
    public_endpoint = args.public_endpoint.rstrip("/")

    if args.provider == "jira":
        return probe_jira_webhook(public_endpoint, args.secret)
    elif args.provider == "bitbucket":
        return probe_bitbucket_webhook(public_endpoint, args.secret)
    else:
        print(f"[ERROR] Unknown provider: {args.provider}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
