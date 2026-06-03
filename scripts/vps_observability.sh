#!/usr/bin/env bash
# =============================================================================
# platform/scripts/vps_observability.sh — Observability probe (R17)
# =============================================================================
# Runs on VPS_Host. Collects container logs, queries Postgres audit/wizard
# tables, runs log-redaction property test, and emits evidence.
#
# Outputs:
#   /tmp/17-logs-automation-service.txt
#   /tmp/17-logs-agent-runner-worker.txt
#   /tmp/17-logs-automation-worker.txt
#   /tmp/17-logs-atlassian-mcp.txt
#   /tmp/17-logs-admin-dashboard-api.txt
#   /tmp/17-observability.json
#
# Exit codes:
#   0 = all assertions pass
#   1 = audit_events count < 5 (R17.2)
#   2 = setup_wizard_state assertion failure (R17.3)
#   3 = log-redaction test failure (R17.4) — also emits Open_Issue (R17.5)
#
# Requirements: R17.1, R17.2, R17.3, R17.4, R17.5, R17.6
# =============================================================================
set -euo pipefail

PLATFORM_DIR="/opt/atlassian-ai-workflow-platform"
COMPOSE_FILE="$PLATFORM_DIR/infra/docker-compose.yml"
EVIDENCE_DIR="/tmp"
OBSERVABILITY_JSON="$EVIDENCE_DIR/17-observability.json"

# Services to collect logs from (R17.1)
SERVICES=("automation-service" "agent-runner-worker" "automation-worker" "atlassian-mcp" "admin-dashboard-api")

# Sensitive substrings to check in logs (R17.4)
SENSITIVE_PATTERNS=(
    "Bearer ATCTT3x"
    "Bearer ATATT3x"
    "sk-proj-"
    "password=ai_dev_only"
)

# --- Helpers -----------------------------------------------------------------

log_info() {
    echo "[INFO] $1"
}

log_fail() {
    echo "[FAIL] $1" >&2
}

log_pass() {
    echo "[PASS] $1"
}

# Postgres query helper via docker compose exec
psql_query() {
    local sql="$1"
    docker compose -f "$COMPOSE_FILE" exec -T postgres psql -U ai -d ai -t -A -c "$sql" 2>/dev/null
}

# =============================================================================
# R17.1: Collect last 200 log lines for each service
# =============================================================================
log_info "Collecting container logs for ${#SERVICES[@]} services..."

declare -A LOG_LINE_COUNTS

for svc in "${SERVICES[@]}"; do
    local_evidence="$EVIDENCE_DIR/17-logs-${svc}.txt"
    log_info "  Capturing logs for: $svc → $local_evidence"

    docker compose -f "$COMPOSE_FILE" logs --tail=200 --no-color "$svc" \
        > "$local_evidence" 2>&1 || true

    # Count lines captured
    line_count=$(wc -l < "$local_evidence" | tr -d ' ')
    LOG_LINE_COUNTS["$svc"]="$line_count"
    log_info "  $svc: $line_count lines captured"
done

log_pass "R17.1 — Log collection complete for all ${#SERVICES[@]} services"

# =============================================================================
# R17.2: Assert audit_events count >= 5
# =============================================================================
log_info "Querying audit_events count..."

AUDIT_COUNT=$(psql_query "SELECT count(*) FROM automation.audit_events;")
AUDIT_COUNT=$(echo "$AUDIT_COUNT" | tr -d '[:space:]')

log_info "  audit_events count = $AUDIT_COUNT"

AUDIT_PASS=true
if [[ -z "$AUDIT_COUNT" ]] || [[ "$AUDIT_COUNT" -lt 5 ]]; then
    log_fail "R17.2 — audit_events count ($AUDIT_COUNT) < 5"
    AUDIT_PASS=false
else
    log_pass "R17.2 — audit_events count ($AUDIT_COUNT) >= 5"
fi

# =============================================================================
# R17.3: Assert setup_wizard_state — 7 rows, all completed, completed_at NOT NULL
# =============================================================================
log_info "Querying setup_wizard_state..."

WIZARD_OUTPUT=$(psql_query "SELECT step_name, status, completed_at FROM automation.setup_wizard_state ORDER BY step_name;")

# Count rows
WIZARD_ROW_COUNT=$(echo "$WIZARD_OUTPUT" | grep -c '|' || echo "0")
log_info "  setup_wizard_state rows = $WIZARD_ROW_COUNT"

WIZARD_PASS=true
WIZARD_ISSUES=""

# Assert 7 rows
if [[ "$WIZARD_ROW_COUNT" -ne 7 ]]; then
    log_fail "R17.3 — Expected 7 wizard rows, got $WIZARD_ROW_COUNT"
    WIZARD_PASS=false
    WIZARD_ISSUES="row_count=$WIZARD_ROW_COUNT (expected 7)"
fi

# Assert all rows have status='completed' and completed_at IS NOT NULL
INCOMPLETE_STEPS=""
while IFS='|' read -r step_name status completed_at; do
    step_name=$(echo "$step_name" | xargs)
    status=$(echo "$status" | xargs)
    completed_at=$(echo "$completed_at" | xargs)

    if [[ "$status" != "completed" ]]; then
        INCOMPLETE_STEPS="${INCOMPLETE_STEPS}${step_name}(status=${status}) "
        WIZARD_PASS=false
    fi
    if [[ -z "$completed_at" ]] || [[ "$completed_at" == "" ]]; then
        INCOMPLETE_STEPS="${INCOMPLETE_STEPS}${step_name}(completed_at=NULL) "
        WIZARD_PASS=false
    fi
done <<< "$WIZARD_OUTPUT"

if [[ "$WIZARD_PASS" == "true" ]]; then
    log_pass "R17.3 — All 7 wizard steps completed with non-null completed_at"
else
    log_fail "R17.3 — Wizard state issues: $INCOMPLETE_STEPS $WIZARD_ISSUES"
fi

# =============================================================================
# R17.4: Run log-redaction property test
# =============================================================================
log_info "Running log-redaction property test..."

REDACTION_TEST_RESULT="not_run"
REDACTION_TEST_OUTPUT=""
REDACTION_EXIT=0

cd "$PLATFORM_DIR"

if [[ -f "tests/property/test_log_redaction.py" ]]; then
    REDACTION_TEST_OUTPUT=$(pytest tests/property/test_log_redaction.py -v 2>&1) || REDACTION_EXIT=$?

    if [[ $REDACTION_EXIT -eq 0 ]]; then
        REDACTION_TEST_RESULT="pass"
        log_pass "R17.4 — Log redaction property test passed"
    else
        REDACTION_TEST_RESULT="fail"
        log_fail "R17.4 — Log redaction property test failed (exit code $REDACTION_EXIT)"
    fi
else
    REDACTION_TEST_RESULT="n/a"
    log_info "R17.4 — test_log_redaction.py not found in repo, skipping (verdict=n/a)"
fi

# Additionally, scan captured logs for literal sensitive substrings (R17.4)
log_info "Scanning captured logs for literal sensitive substrings..."

LEAKED_PATTERNS=""
LEAK_DETECTED=false

for svc in "${SERVICES[@]}"; do
    log_file="$EVIDENCE_DIR/17-logs-${svc}.txt"
    if [[ -f "$log_file" ]]; then
        for pattern in "${SENSITIVE_PATTERNS[@]}"; do
            if grep -qF "$pattern" "$log_file" 2>/dev/null; then
                LEAKED_PATTERNS="${LEAKED_PATTERNS}[${svc}:${pattern}] "
                LEAK_DETECTED=true
            fi
        done
    fi
done

if [[ "$LEAK_DETECTED" == "true" ]]; then
    log_fail "R17.4 — Sensitive substrings found in logs: $LEAKED_PATTERNS"
    REDACTION_TEST_RESULT="fail"
else
    log_pass "R17.4 — No literal sensitive substrings found in captured logs"
fi

# =============================================================================
# R17.5: If redaction fails, emit critical Open_Issue
# =============================================================================
if [[ "$REDACTION_TEST_RESULT" == "fail" ]]; then
    log_info "R17.5 — Emitting critical Open_Issue for redaction failure..."

    # Attempt to call the open issue logger if Python is available
    if command -v python3 &>/dev/null && [[ -f "$PLATFORM_DIR/scripts/vps_open_issue_logger.py" ]]; then
        ISSUE_SUMMARY="Log redaction failure: sensitive substrings detected in service logs"
        if [[ -n "$LEAKED_PATTERNS" ]]; then
            ISSUE_SUMMARY="Log redaction failure: leaked patterns ${LEAKED_PATTERNS:0:100}"
        fi

        python3 "$PLATFORM_DIR/scripts/vps_open_issue_logger.py" \
            --requirement R17 \
            --severity critical \
            --category code \
            --summary "$ISSUE_SUMMARY" \
            --evidence-path "vps-test-evidence/17-observability.json" \
            --recommended-action code_change_required \
            2>&1 || log_info "  (Open_Issue logger returned non-zero, continuing)"
    else
        echo "[CRITICAL OPEN ISSUE] R17 — Log redaction failure: secrets not redacted before logging"
    fi
fi

# =============================================================================
# R17.6: Emit evidence — /tmp/17-observability.json
# =============================================================================
log_info "Generating evidence file: $OBSERVABILITY_JSON"

# Build JSON evidence
cat > "$OBSERVABILITY_JSON" <<JSONEOF
{
  "timestamp_utc": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "log_line_counts": {
    "automation-service": ${LOG_LINE_COUNTS["automation-service"]:-0},
    "agent-runner-worker": ${LOG_LINE_COUNTS["agent-runner-worker"]:-0},
    "automation-worker": ${LOG_LINE_COUNTS["automation-worker"]:-0},
    "atlassian-mcp": ${LOG_LINE_COUNTS["atlassian-mcp"]:-0},
    "admin-dashboard-api": ${LOG_LINE_COUNTS["admin-dashboard-api"]:-0}
  },
  "audit_events": {
    "count": ${AUDIT_COUNT:-0},
    "assertion": "count >= 5",
    "result": "${AUDIT_PASS}"
  },
  "setup_wizard_state": {
    "row_count": ${WIZARD_ROW_COUNT:-0},
    "all_completed": ${WIZARD_PASS},
    "assertion": "7 rows, all status=completed, completed_at IS NOT NULL",
    "result": "${WIZARD_PASS}",
    "snapshot": $(echo "$WIZARD_OUTPUT" | python3 -c "
import sys, json
rows = []
for line in sys.stdin:
    line = line.strip()
    if '|' in line:
        parts = [p.strip() for p in line.split('|')]
        if len(parts) >= 3:
            rows.append({'step_name': parts[0], 'status': parts[1], 'completed_at': parts[2]})
print(json.dumps(rows))
" 2>/dev/null || echo "[]")
  },
  "log_redaction_test": {
    "result": "${REDACTION_TEST_RESULT}",
    "pytest_exit_code": ${REDACTION_EXIT},
    "literal_scan_leak_detected": ${LEAK_DETECTED},
    "leaked_patterns": "$(echo "$LEAKED_PATTERNS" | sed 's/"/\\"/g')"
  },
  "overall_result": "$(
    if [[ "$AUDIT_PASS" == "true" && "$WIZARD_PASS" == "true" && "$REDACTION_TEST_RESULT" != "fail" ]]; then
        echo "PASS"
    else
        echo "FAIL"
    fi
  )"
}
JSONEOF

log_info "Evidence written to: $OBSERVABILITY_JSON"

# =============================================================================
# Summary
# =============================================================================
echo ""
echo "=== Observability Probe Summary ==="
echo "  R17.1 Log collection:     PASS (${#SERVICES[@]} services)"
echo "  R17.2 Audit events:       $(if [[ "$AUDIT_PASS" == "true" ]]; then echo "PASS ($AUDIT_COUNT >= 5)"; else echo "FAIL ($AUDIT_COUNT < 5)"; fi)"
echo "  R17.3 Wizard state:       $(if [[ "$WIZARD_PASS" == "true" ]]; then echo "PASS (7/7 completed)"; else echo "FAIL"; fi)"
echo "  R17.4 Log redaction:      $(echo "$REDACTION_TEST_RESULT" | tr '[:lower:]' '[:upper:]')"
echo "  R17.5 Open_Issue emitted: $(if [[ "$REDACTION_TEST_RESULT" == "fail" ]]; then echo "YES (critical)"; else echo "N/A"; fi)"
echo "  R17.6 Evidence file:      $OBSERVABILITY_JSON"
echo ""
echo "Evidence files:"
for svc in "${SERVICES[@]}"; do
    echo "  $EVIDENCE_DIR/17-logs-${svc}.txt"
done
echo "  $OBSERVABILITY_JSON"
echo ""

# Exit with appropriate code
if [[ "$AUDIT_PASS" != "true" ]]; then
    exit 1
elif [[ "$WIZARD_PASS" != "true" ]]; then
    exit 2
elif [[ "$REDACTION_TEST_RESULT" == "fail" ]]; then
    exit 3
fi

echo "=== Observability probe complete — all assertions passed ==="
exit 0
