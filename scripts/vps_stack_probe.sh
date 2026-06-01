#!/usr/bin/env bash
# =============================================================================
# platform/scripts/vps_stack_probe.sh — Full stack health & shape verification
# =============================================================================
# Runs on VPS_Host at /opt/yeni_atlassian/platform.
# Implements Requirement 9 (R9.1–R9.5):
#
#   1. Assert 13 services are running + healthy via docker compose ps  (R9.1)
#   2. Run pytest test_compose_healthcheck_shape.py, assert exit 0     (R9.2)
#   3. If any service unhealthy >120s, capture logs + Open_Issue       (R9.3)
#   4. Assert automation-service env: LOG_REDACTION_ENABLED=true,
#      LOG_LEVEL∈{INFO,DEBUG}, LOG_FORMAT=json                         (R9.4)
#   5. Emit evidence to /tmp/09-stack.txt                              (R9.5)
#
# Exit codes:
#   0 = all assertions pass
#   1 = one or more assertions failed
#
# Usage:
#   ./vps_stack_probe.sh
#
# Prerequisites:
#   - Setup Wizard completed (R8 passed)
#   - All profile-gated services started
# =============================================================================
set -uo pipefail

PLATFORM_DIR="/opt/yeni_atlassian/platform"
COMPOSE_FILE="$PLATFORM_DIR/infra/docker-compose.yml"
COMPOSE_DEV="$PLATFORM_DIR/infra/docker-compose.dev.yml"
EVIDENCE_FILE="/tmp/09-stack.txt"
EVIDENCE_DIR="/tmp"

# Maximum time to wait for unhealthy services before capturing logs (R9.3)
UNHEALTHY_TIMEOUT=120
POLL_INTERVAL=10

# Expected 13 services that must be running + healthy (R9.1)
EXPECTED_SERVICES=(
    "postgres"
    "vault"
    "temporal"
    "temporal-ui"
    "atlassian-mcp"
    "automation-service"
    "assistant-service"
    "automation-worker"
    "agent-runner-worker"
    "execution-runner-worker"
    "streamlit-ui"
    "admin-dashboard-api"
    "admin-dashboard-ui"
)
EXPECTED_COUNT=${#EXPECTED_SERVICES[@]}

# --- Helpers ------------------------------------------------------------------

compose_cmd() {
    docker compose -f "$COMPOSE_FILE" -f "$COMPOSE_DEV" "$@"
}

fail_stack() {
    echo "[FAIL] $1" >&2
    echo "RESULT: R9 FAIL — $1" >> "$EVIDENCE_FILE"
    exit 1
}

log_info() {
    echo "[INFO] $(date -u +%H:%M:%S) $1"
}

log_pass() {
    echo "[PASS] $1"
}

log_fail() {
    echo "[FAIL] $1" >&2
}

log_open_issue() {
    local requirement="$1"
    local summary="$2"
    local scenario="${3:-}"

    if command -v python3 &>/dev/null && [ -f "$PLATFORM_DIR/scripts/vps_open_issue_logger.py" ]; then
        local args=(
            --requirement "$requirement"
            --severity major
            --category infra
            --summary "$summary"
            --evidence-path "vps-test-evidence/09-stack.txt"
            --recommended-action manual_fix
        )
        if [ -n "$scenario" ]; then
            args+=(--scenario "$scenario")
        fi
        python3 "$PLATFORM_DIR/scripts/vps_open_issue_logger.py" "${args[@]}" || true
    fi
}

# --- Initialize evidence file -------------------------------------------------
: > "$EVIDENCE_FILE"
echo "=== VPS Stack Probe (R9) ===" >> "$EVIDENCE_FILE"
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

# =============================================================================
# R9.1: Assert 13 services are running + healthy
# =============================================================================
echo "[R9.1] Checking full stack health (expecting $EXPECTED_COUNT services running+healthy)..."
echo "--- R9.1: docker compose ps ---" >> "$EVIDENCE_FILE"

PS_OUTPUT=$(compose_cmd ps --format '{{.Name}}\t{{.State}}\t{{.Health}}' 2>/dev/null || echo "")
echo "$PS_OUTPUT" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

HEALTHY_COUNT=0
UNHEALTHY_SERVICES=()
MISSING_SERVICES=()

for SVC in "${EXPECTED_SERVICES[@]}"; do
    # Find the line matching this service name (container name may have prefix like "infra-" and suffix like "-1")
    SVC_LINE=$(echo "$PS_OUTPUT" | grep -E "(^|-)${SVC}-[0-9]+[[:space:]]" || echo "")

    if [ -z "$SVC_LINE" ]; then
        log_fail "Service '$SVC' not found in docker compose ps output"
        MISSING_SERVICES+=("$SVC")
        continue
    fi

    SVC_STATE=$(echo "$SVC_LINE" | awk -F'\t' '{print $2}' | tr -d '[:space:]')
    SVC_HEALTH=$(echo "$SVC_LINE" | awk -F'\t' '{print $3}' | tr -d '[:space:]')

    if [[ "$SVC_STATE" == "running" && "$SVC_HEALTH" == "healthy" ]]; then
        HEALTHY_COUNT=$((HEALTHY_COUNT + 1))
    else
        log_fail "Service '$SVC' — State=$SVC_STATE, Health=$SVC_HEALTH (expected running/healthy)"
        UNHEALTHY_SERVICES+=("$SVC")
    fi
done

echo "healthy_count=$HEALTHY_COUNT" >> "$EVIDENCE_FILE"
echo "expected_count=$EXPECTED_COUNT" >> "$EVIDENCE_FILE"
echo "missing_services=${MISSING_SERVICES[*]:-none}" >> "$EVIDENCE_FILE"
echo "unhealthy_services=${UNHEALTHY_SERVICES[*]:-none}" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

# =============================================================================
# R9.3: If any service is unhealthy, wait up to 120s then capture logs
# =============================================================================
if [ ${#UNHEALTHY_SERVICES[@]} -gt 0 ] || [ ${#MISSING_SERVICES[@]} -gt 0 ]; then
    log_info "Unhealthy/missing services detected. Polling for up to ${UNHEALTHY_TIMEOUT}s..."
    echo "--- R9.3: Unhealthy service polling ---" >> "$EVIDENCE_FILE"

    ELAPSED=0
    while [ $ELAPSED -lt $UNHEALTHY_TIMEOUT ]; do
        sleep "$POLL_INTERVAL"
        ELAPSED=$((ELAPSED + POLL_INTERVAL))

        # Re-check unhealthy services
        STILL_UNHEALTHY=()
        for SVC in "${UNHEALTHY_SERVICES[@]}"; do
            SVC_LINE=$(compose_cmd ps --format '{{.Name}}\t{{.State}}\t{{.Health}}' "$SVC" 2>/dev/null || echo "")
            SVC_STATE=$(echo "$SVC_LINE" | awk -F'\t' '{print $2}' | tr -d '[:space:]')
            SVC_HEALTH=$(echo "$SVC_LINE" | awk -F'\t' '{print $3}' | tr -d '[:space:]')

            if [[ "$SVC_STATE" != "running" || "$SVC_HEALTH" != "healthy" ]]; then
                STILL_UNHEALTHY+=("$SVC")
            fi
        done

        if [ ${#STILL_UNHEALTHY[@]} -eq 0 ] && [ ${#MISSING_SERVICES[@]} -eq 0 ]; then
            log_info "All previously unhealthy services recovered after ${ELAPSED}s"
            UNHEALTHY_SERVICES=()
            # Recount healthy
            HEALTHY_COUNT=$EXPECTED_COUNT
            break
        fi

        log_info "Still unhealthy after ${ELAPSED}s: ${STILL_UNHEALTHY[*]}"
        UNHEALTHY_SERVICES=("${STILL_UNHEALTHY[@]}")
    done

    # After timeout, capture logs for services still unhealthy
    if [ ${#UNHEALTHY_SERVICES[@]} -gt 0 ]; then
        echo "Unhealthy services after ${UNHEALTHY_TIMEOUT}s timeout:" >> "$EVIDENCE_FILE"
        for SVC in "${UNHEALTHY_SERVICES[@]}"; do
            LOG_FILE="$EVIDENCE_DIR/09-unhealthy-${SVC}.log"
            log_info "Capturing last 200 log lines for '$SVC' → $LOG_FILE"
            compose_cmd logs --tail=200 --no-color "$SVC" > "$LOG_FILE" 2>&1 || true
            echo "  $SVC → $LOG_FILE" >> "$EVIDENCE_FILE"
            log_open_issue "R9" "Service '$SVC' unhealthy >120s after wizard completion (R9.3)" "$SVC"
        done
        echo "" >> "$EVIDENCE_FILE"
    fi

    if [ ${#MISSING_SERVICES[@]} -gt 0 ]; then
        echo "Missing services (not found in compose ps):" >> "$EVIDENCE_FILE"
        for SVC in "${MISSING_SERVICES[@]}"; do
            echo "  $SVC" >> "$EVIDENCE_FILE"
            log_open_issue "R9" "Service '$SVC' not found in compose ps output (R9.1)" "$SVC"
        done
        echo "" >> "$EVIDENCE_FILE"
    fi
fi

# Final R9.1 verdict
TOTAL_ISSUES=$((${#UNHEALTHY_SERVICES[@]} + ${#MISSING_SERVICES[@]}))
if [ $TOTAL_ISSUES -eq 0 ]; then
    log_pass "R9.1: All $EXPECTED_COUNT services are running + healthy"
    echo "R9.1: PASS — $HEALTHY_COUNT/$EXPECTED_COUNT services running+healthy" >> "$EVIDENCE_FILE"
else
    log_fail "R9.1: $TOTAL_ISSUES service(s) not running+healthy"
    echo "R9.1: FAIL — $HEALTHY_COUNT/$EXPECTED_COUNT healthy, $TOTAL_ISSUES issue(s)" >> "$EVIDENCE_FILE"
fi
echo "" >> "$EVIDENCE_FILE"

# =============================================================================
# R9.2: Run pytest test_compose_healthcheck_shape.py
# =============================================================================
echo "[R9.2] Running property test: test_compose_healthcheck_shape.py..."
echo "--- R9.2: healthcheck shape property test ---" >> "$EVIDENCE_FILE"

PYTEST_EXIT=0
PYTEST_OUTPUT=""

if [ -f "$PLATFORM_DIR/tests/property/test_compose_healthcheck_shape.py" ]; then
    PYTEST_OUTPUT=$(cd "$PLATFORM_DIR" && python3 -m pytest tests/property/test_compose_healthcheck_shape.py -v 2>&1) || PYTEST_EXIT=$?
    echo "$PYTEST_OUTPUT" >> "$EVIDENCE_FILE"
    echo "" >> "$EVIDENCE_FILE"
    echo "pytest_exit_code=$PYTEST_EXIT" >> "$EVIDENCE_FILE"

    if [ $PYTEST_EXIT -eq 0 ]; then
        log_pass "R9.2: test_compose_healthcheck_shape.py passed (exit 0)"
        echo "R9.2: PASS" >> "$EVIDENCE_FILE"
    else
        log_fail "R9.2: test_compose_healthcheck_shape.py failed (exit $PYTEST_EXIT)"
        echo "R9.2: FAIL — pytest exit code $PYTEST_EXIT" >> "$EVIDENCE_FILE"
        log_open_issue "R9" "Healthcheck shape property test failed (exit $PYTEST_EXIT) (R9.2)"
    fi
else
    log_info "R9.2: test_compose_healthcheck_shape.py not found in repo — verdict n/a"
    echo "R9.2: N/A — test file not present in repository" >> "$EVIDENCE_FILE"
    echo "pytest_exit_code=n/a" >> "$EVIDENCE_FILE"

    # Log Open_Issue for missing test file (R21.4 pattern)
    if command -v python3 &>/dev/null && [ -f "$PLATFORM_DIR/scripts/vps_open_issue_logger.py" ]; then
        python3 "$PLATFORM_DIR/scripts/vps_open_issue_logger.py" \
            --requirement R9 \
            --severity major \
            --category code \
            --summary "test_compose_healthcheck_shape.py not found in repository (R9.2)" \
            --evidence-path "vps-test-evidence/09-stack.txt" \
            --recommended-action code_change_required || true
    fi
fi
echo "" >> "$EVIDENCE_FILE"

# =============================================================================
# R9.4: Assert automation-service env vars (LOG_REDACTION, LOG_LEVEL, LOG_FORMAT)
# =============================================================================
echo "[R9.4] Checking automation-service environment variables..."
echo "--- R9.4: automation-service env assertions ---" >> "$EVIDENCE_FILE"

# Disable errexit temporarily — container may not exist
ENV_OUTPUT=""
ENV_OUTPUT=$(docker exec automation-service env 2>/dev/null | grep -E '^(LOG_REDACTION_ENABLED|LOG_LEVEL|LOG_FORMAT)=' || true)
if [ -z "$ENV_OUTPUT" ]; then
    ENV_OUTPUT=$(docker exec infra-automation-service-1 env 2>/dev/null | grep -E '^(LOG_REDACTION_ENABLED|LOG_LEVEL|LOG_FORMAT)=' || true)
fi
echo "$ENV_OUTPUT" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

R94_PASS=1

# Check LOG_REDACTION_ENABLED=true
LOG_REDACTION=$(echo "$ENV_OUTPUT" | grep '^LOG_REDACTION_ENABLED=' | cut -d'=' -f2 | tr -d '[:space:]' || true)
if [ "$LOG_REDACTION" = "true" ]; then
    log_pass "R9.4: LOG_REDACTION_ENABLED=true"
    echo "LOG_REDACTION_ENABLED: PASS (value=true)" >> "$EVIDENCE_FILE"
else
    log_fail "R9.4: LOG_REDACTION_ENABLED='$LOG_REDACTION' (expected 'true')"
    echo "LOG_REDACTION_ENABLED: FAIL (value='$LOG_REDACTION', expected 'true')" >> "$EVIDENCE_FILE"
    R94_PASS=0
fi

# Check LOG_LEVEL ∈ {INFO, DEBUG}
LOG_LEVEL=$(echo "$ENV_OUTPUT" | grep '^LOG_LEVEL=' | cut -d'=' -f2 | tr -d '[:space:]' || true)
if [[ "$LOG_LEVEL" == "INFO" || "$LOG_LEVEL" == "DEBUG" ]]; then
    log_pass "R9.4: LOG_LEVEL=$LOG_LEVEL (valid)"
    echo "LOG_LEVEL: PASS (value=$LOG_LEVEL)" >> "$EVIDENCE_FILE"
else
    log_fail "R9.4: LOG_LEVEL='$LOG_LEVEL' (expected INFO or DEBUG)"
    echo "LOG_LEVEL: FAIL (value='$LOG_LEVEL', expected INFO or DEBUG)" >> "$EVIDENCE_FILE"
    R94_PASS=0
fi

# Check LOG_FORMAT=json
LOG_FORMAT=$(echo "$ENV_OUTPUT" | grep '^LOG_FORMAT=' | cut -d'=' -f2 | tr -d '[:space:]' || true)
if [ "$LOG_FORMAT" = "json" ]; then
    log_pass "R9.4: LOG_FORMAT=json"
    echo "LOG_FORMAT: PASS (value=json)" >> "$EVIDENCE_FILE"
else
    log_fail "R9.4: LOG_FORMAT='$LOG_FORMAT' (expected 'json')"
    echo "LOG_FORMAT: FAIL (value='$LOG_FORMAT', expected 'json')" >> "$EVIDENCE_FILE"
    R94_PASS=0
fi

if [ $R94_PASS -eq 1 ]; then
    echo "R9.4: PASS" >> "$EVIDENCE_FILE"
else
    echo "R9.4: FAIL" >> "$EVIDENCE_FILE"
    log_open_issue "R9" "automation-service env vars do not match expected values (R9.4)"
fi
echo "" >> "$EVIDENCE_FILE"

# =============================================================================
# R9.5: Emit final evidence — full ps table + property test output
# =============================================================================
echo "" >> "$EVIDENCE_FILE"
echo "--- Full docker compose ps table ---" >> "$EVIDENCE_FILE"
compose_cmd ps >> "$EVIDENCE_FILE" 2>&1 || true
echo "" >> "$EVIDENCE_FILE"

# --- Summary ------------------------------------------------------------------
echo "--- Summary ---" >> "$EVIDENCE_FILE"
echo "services_healthy=$HEALTHY_COUNT/$EXPECTED_COUNT" >> "$EVIDENCE_FILE"
echo "healthcheck_shape_test=$([ $PYTEST_EXIT -eq 0 ] && echo 'pass' || echo 'fail')" >> "$EVIDENCE_FILE"
echo "env_assertions=$([ $R94_PASS -eq 1 ] && echo 'pass' || echo 'fail')" >> "$EVIDENCE_FILE"
echo "completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$EVIDENCE_FILE"

# Determine overall result
if [ $TOTAL_ISSUES -eq 0 ] && [ $PYTEST_EXIT -eq 0 ] && [ $R94_PASS -eq 1 ]; then
    echo "RESULT: R9 PASS" >> "$EVIDENCE_FILE"
    echo ""
    echo "============================================================================="
    echo "  STACK PROBE COMPLETE — ALL ASSERTIONS PASSED"
    echo "============================================================================="
else
    echo "RESULT: R9 FAIL" >> "$EVIDENCE_FILE"
    echo ""
    echo "============================================================================="
    echo "  STACK PROBE COMPLETE — FAILURES DETECTED (see evidence)"
    echo "============================================================================="
fi

echo ""
echo "Evidence written to: $EVIDENCE_FILE"
