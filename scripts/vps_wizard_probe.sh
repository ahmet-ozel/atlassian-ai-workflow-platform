#!/usr/bin/env bash
# =============================================================================
# platform/scripts/vps_wizard_probe.sh — Setup Wizard 7-step state polling
# =============================================================================
# Runs on VPS_Host. Setup wizard probe checks:
#
#   1. Prompt operator to open admin-dashboard and trigger wizard steps  (R8.1)
#   2. Poll setup_wizard_state every 30s until all steps completed       (R8.2)
#   3. Assert mcp_server healthz returns 200 when step completes         (R8.3)
#   4. Prompt operator for add_first_department bot probe                (R8.4)
#   5. Assert all 7 rows have status='completed'                         (R8.5)
#   6. Capture service logs on failure/timeout, emit Open_Issue           (R8.6)
#   7. Emit evidence to /tmp/08-wizard.txt                               (R8.7)
#
# Usage:
#   ./vps_wizard_probe.sh
#
# Prerequisites:
#   - Boot_Bundle healthy (R7 passed)
#   - Operator has SSH tunnel: ssh -L 3000:localhost:3000 root@91.99.149.163
# =============================================================================
set -euo pipefail

PLATFORM_DIR="/opt/atlassian-ai-workflow-platform"
COMPOSE_FILE="$PLATFORM_DIR/infra/docker-compose.yml"
EVIDENCE_FILE="/tmp/08-wizard.txt"
POLL_INTERVAL=30
STEP_TIMEOUT=300
EXPECTED_STEPS=("vault" "postgresql" "temporal" "mcp_server" "workers" "services" "add_first_department")
EXPECTED_STEP_COUNT=7

# --- Helpers ------------------------------------------------------------------

psql_cmd() {
    docker compose -f "$COMPOSE_FILE" exec -T postgres \
        psql -U ai -d ai -t -A -c "$1" 2>/dev/null
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
            --severity critical
            --category infra
            --summary "$summary"
            --evidence-path "vps-test-evidence/08-wizard.txt"
            --recommended-action manual_fix
        )
        if [ -n "$scenario" ]; then
            args+=(--scenario "$scenario")
        fi
        python3 "$PLATFORM_DIR/scripts/vps_open_issue_logger.py" "${args[@]}" || true
    fi
}

capture_service_logs() {
    local step_name="$1"
    local log_file="/tmp/08-wizard-failure-${step_name}.log"

    log_info "Capturing logs for step '$step_name'..."

    # Map step names to likely compose service names
    local service=""
    case "$step_name" in
        vault)                  service="vault" ;;
        postgresql)             service="postgres" ;;
        temporal)               service="temporal" ;;
        mcp_server)             service="atlassian-mcp" ;;
        workers)                service="automation-worker" ;;
        services)               service="automation-service" ;;
        add_first_department)   service="admin-dashboard-api" ;;
        *)                      service="$step_name" ;;
    esac

    docker compose -f "$COMPOSE_FILE" logs --tail=200 --no-color "$service" > "$log_file" 2>&1 || true
    echo "Service logs captured: $log_file"
    echo "" >> "$EVIDENCE_FILE"
    echo "--- Failure logs for step: $step_name (service: $service) ---" >> "$EVIDENCE_FILE"
    tail -50 "$log_file" >> "$EVIDENCE_FILE" 2>/dev/null || true
}

get_wizard_state() {
    psql_cmd "SELECT step_name, status, started_at, completed_at FROM automation.setup_wizard_state ORDER BY step_name;"
}

get_step_status() {
    local step_name="$1"
    psql_cmd "SELECT status FROM automation.setup_wizard_state WHERE step_name='$step_name';" | tr -d '[:space:]'
}

# --- Initialize evidence file -------------------------------------------------
: > "$EVIDENCE_FILE"
echo "=== VPS Wizard Probe (R8) ===" >> "$EVIDENCE_FILE"
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

# =============================================================================
# R8.1: Operator prompt — open admin-dashboard and trigger wizard steps
# =============================================================================
echo ""
echo "============================================================================="
echo "  SETUP WIZARD PROBE"
echo "============================================================================="
echo ""
echo "  Operator action required:"
echo ""
echo "  1. Ensure SSH tunnel is active:"
echo "     ssh -L 3000:localhost:3000 root@91.99.149.163"
echo ""
echo "  2. Open in browser: http://localhost:3000"
echo ""
echo "  3. Trigger the 7 Setup Wizard steps IN ORDER:"
echo "     vault → postgresql → temporal → mcp_server → workers → services → add_first_department"
echo ""
echo "  4. For 'add_first_department' step, use these values:"
echo "     - dept_id:    johni-test"
echo "     - Project key: JOH"
echo "     - Workspace:  example_workspace"
echo "     - Repository: smoke-test"
echo "     - Token:      (use the selected token from R5 token selection)"
echo ""
echo "  This script will poll the database every ${POLL_INTERVAL}s to track progress."
echo "  Each step has a ${STEP_TIMEOUT}s timeout while in 'running' state."
echo ""
echo "  Press ENTER when you are ready to begin polling..."
read -r

echo "R8.1: Operator acknowledged — beginning wizard state polling" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

# =============================================================================
# R8.2 / R8.5 / R8.6: Poll setup_wizard_state until all steps completed
# =============================================================================

WIZARD_RESULT="pass"
MCP_HEALTHZ_DONE=0
DEPT_PROBE_DONE=0

# Track per-step running time
declare -A STEP_RUNNING_SINCE

log_info "Starting wizard state polling (interval=${POLL_INTERVAL}s, step timeout=${STEP_TIMEOUT}s)..."
echo ""

while true; do
    # Query current state
    CURRENT_STATE=$(get_wizard_state)

    if [ -z "$CURRENT_STATE" ]; then
        log_info "setup_wizard_state table is empty or not yet populated. Waiting..."
        sleep "$POLL_INTERVAL"
        continue
    fi

    # Display current state
    echo "--- Wizard State @ $(date -u +%H:%M:%S) ---"
    echo "$CURRENT_STATE" | column -t -s '|' 2>/dev/null || echo "$CURRENT_STATE"
    echo ""

    # Count statuses
    COMPLETED_COUNT=0
    FAILED_COUNT=0
    RUNNING_STEPS=()
    ALL_DONE=1

    while IFS='|' read -r step_name status started_at completed_at; do
        # Trim whitespace
        step_name=$(echo "$step_name" | xargs)
        status=$(echo "$status" | xargs)

        case "$status" in
            completed)
                COMPLETED_COUNT=$((COMPLETED_COUNT + 1))
                # Clear running tracker
                unset "STEP_RUNNING_SINCE[$step_name]" 2>/dev/null || true
                ;;
            failed)
                FAILED_COUNT=$((FAILED_COUNT + 1))
                ALL_DONE=0

                log_fail "Step '$step_name' has status='failed'"
                capture_service_logs "$step_name"
                log_open_issue "R8" "Setup Wizard step '$step_name' failed (R8.6)" "$step_name"

                WIZARD_RESULT="fail"
                echo "R8.6: Step '$step_name' FAILED — Open_Issue logged" >> "$EVIDENCE_FILE"

                # Halt on wizard step failure
                echo "" >> "$EVIDENCE_FILE"
                echo "--- Final state at failure ---" >> "$EVIDENCE_FILE"
                echo "$CURRENT_STATE" >> "$EVIDENCE_FILE"
                echo "" >> "$EVIDENCE_FILE"
                echo "RESULT: R8 FAIL — step '$step_name' failed" >> "$EVIDENCE_FILE"

                echo ""
                log_fail "Wizard probe halted: step '$step_name' reported 'failed'."
                echo "Evidence written to: $EVIDENCE_FILE"
                exit 1
                ;;
            running)
                ALL_DONE=0
                RUNNING_STEPS+=("$step_name")

                # Track running duration
                NOW_EPOCH=$(date +%s)
                if [ -z "${STEP_RUNNING_SINCE[$step_name]:-}" ]; then
                    STEP_RUNNING_SINCE[$step_name]=$NOW_EPOCH
                fi

                RUNNING_ELAPSED=$((NOW_EPOCH - STEP_RUNNING_SINCE[$step_name]))

                if [ "$RUNNING_ELAPSED" -ge "$STEP_TIMEOUT" ]; then
                    log_fail "Step '$step_name' exceeded ${STEP_TIMEOUT}s in 'running' state (elapsed: ${RUNNING_ELAPSED}s)"
                    capture_service_logs "$step_name"
                    log_open_issue "R8" "Setup Wizard step '$step_name' timeout after ${STEP_TIMEOUT}s (R8.6)" "$step_name"

                    WIZARD_RESULT="fail"
                    echo "R8.6: Step '$step_name' TIMEOUT (${RUNNING_ELAPSED}s > ${STEP_TIMEOUT}s) — Open_Issue logged" >> "$EVIDENCE_FILE"

                    echo "" >> "$EVIDENCE_FILE"
                    echo "--- Final state at timeout ---" >> "$EVIDENCE_FILE"
                    echo "$CURRENT_STATE" >> "$EVIDENCE_FILE"
                    echo "" >> "$EVIDENCE_FILE"
                    echo "RESULT: R8 FAIL — step '$step_name' timeout" >> "$EVIDENCE_FILE"

                    echo ""
                    log_fail "Wizard probe halted: step '$step_name' timed out."
                    echo "Evidence written to: $EVIDENCE_FILE"
                    exit 1
                else
                    log_info "Step '$step_name' running for ${RUNNING_ELAPSED}s / ${STEP_TIMEOUT}s max"
                fi
                ;;
            pending)
                ALL_DONE=0
                ;;
            *)
                ALL_DONE=0
                log_info "Step '$step_name' has unknown status: '$status'"
                ;;
        esac
    done <<< "$CURRENT_STATE"

    # --- R8.3: mcp_server healthz check when step completes -------------------
    if [ "$MCP_HEALTHZ_DONE" -eq 0 ]; then
        MCP_STATUS=$(get_step_status "mcp_server")
        if [ "$MCP_STATUS" = "completed" ]; then
            log_info "mcp_server step completed — probing healthz..."
            echo "" >> "$EVIDENCE_FILE"
            echo "--- R8.3: MCP Server healthz probe ---" >> "$EVIDENCE_FILE"

            MCP_HTTP_CODE=$(curl -fsS -o /dev/null -w "%{http_code}" http://localhost:8090/healthz 2>/dev/null || echo "000")
            MCP_RESPONSE=$(curl -sS http://localhost:8090/healthz 2>/dev/null || echo "(connection failed)")

            echo "HTTP Status: $MCP_HTTP_CODE" >> "$EVIDENCE_FILE"
            echo "Response: $MCP_RESPONSE" >> "$EVIDENCE_FILE"
            echo "" >> "$EVIDENCE_FILE"

            if [ "$MCP_HTTP_CODE" = "200" ]; then
                log_pass "R8.3: MCP healthz returned HTTP 200 OK"
                echo "R8.3: PASS — MCP healthz HTTP 200" >> "$EVIDENCE_FILE"
            else
                log_fail "R8.3: MCP healthz returned HTTP $MCP_HTTP_CODE (expected 200)"
                echo "R8.3: FAIL — MCP healthz HTTP $MCP_HTTP_CODE" >> "$EVIDENCE_FILE"
                log_open_issue "R8" "MCP healthz returned $MCP_HTTP_CODE after mcp_server step completed (R8.3)"
                WIZARD_RESULT="fail"
            fi

            MCP_HEALTHZ_DONE=1
        fi
    fi

    # --- R8.4: add_first_department bot probe reminder -------------------------
    if [ "$DEPT_PROBE_DONE" -eq 0 ]; then
        DEPT_STATUS=$(get_step_status "add_first_department")
        if [ "$DEPT_STATUS" = "running" ] || [ "$DEPT_STATUS" = "pending" ]; then
            # Remind operator about department configuration
            if [ "$DEPT_STATUS" = "running" ]; then
                log_info "add_first_department is running — ensure you submitted:"
                echo "       dept_id=johni-test, project_key=JOH, workspace=example_workspace, repo=smoke-test"
            fi
        elif [ "$DEPT_STATUS" = "completed" ]; then
            log_pass "R8.4: add_first_department step completed"
            DEPT_PROBE_DONE=1

            # Capture bot probe transcript
            echo "" >> "$EVIDENCE_FILE"
            echo "--- R8.4: add_first_department bot probe ---" >> "$EVIDENCE_FILE"
            echo "dept_id: johni-test" >> "$EVIDENCE_FILE"
            echo "project_key: JOH" >> "$EVIDENCE_FILE"
            echo "workspace: example_workspace" >> "$EVIDENCE_FILE"
            echo "repository: smoke-test" >> "$EVIDENCE_FILE"
            echo "status: completed" >> "$EVIDENCE_FILE"

            # Attempt to capture the bot probe HTTP transcript from admin-dashboard-api logs
            BOT_PROBE_LOG=$(docker compose -f "$COMPOSE_FILE" logs --tail=100 --no-color admin-dashboard-api 2>/dev/null \
                | grep -i -E "(probe|department|bitbucket|atlassian|myself|/user|/repositories)" | tail -20 || true)

            if [ -n "$BOT_PROBE_LOG" ]; then
                echo "" >> "$EVIDENCE_FILE"
                echo "Bot probe HTTP transcript (from admin-dashboard-api logs):" >> "$EVIDENCE_FILE"
                echo "$BOT_PROBE_LOG" >> "$EVIDENCE_FILE"
            else
                echo "Bot probe HTTP transcript: (not captured from logs — operator confirmed via UI)" >> "$EVIDENCE_FILE"
            fi
            echo "" >> "$EVIDENCE_FILE"
        fi
    fi

    # --- Check if all steps are completed -------------------------------------
    if [ "$ALL_DONE" -eq 1 ] && [ "$COMPLETED_COUNT" -ge "$EXPECTED_STEP_COUNT" ]; then
        log_info "All $EXPECTED_STEP_COUNT steps show completed!"
        break
    fi

    # Status summary
    log_info "Progress: ${COMPLETED_COUNT}/${EXPECTED_STEP_COUNT} completed, ${#RUNNING_STEPS[@]} running, ${FAILED_COUNT} failed"

    sleep "$POLL_INTERVAL"
done

# =============================================================================
# R8.5: Final assertion — all 7 rows status='completed'
# =============================================================================
echo ""
log_info "Running final assertion: all 7 steps must have status='completed'..."

FINAL_SNAPSHOT=$(psql_cmd "SELECT step_name, status, started_at, completed_at FROM automation.setup_wizard_state ORDER BY step_name;")
COMPLETED_FINAL=$(psql_cmd "SELECT count(*) FROM automation.setup_wizard_state WHERE status='completed';")
COMPLETED_FINAL=$(echo "$COMPLETED_FINAL" | tr -d '[:space:]')

echo "" >> "$EVIDENCE_FILE"
echo "--- R8.5: Final setup_wizard_state snapshot ---" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

# Pretty-print the snapshot with headers
echo " step_name            | status    | started_at                  | completed_at" >> "$EVIDENCE_FILE"
echo "----------------------+-----------+-----------------------------+-----------------------------" >> "$EVIDENCE_FILE"
echo "$FINAL_SNAPSHOT" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"
echo "Total rows with status='completed': $COMPLETED_FINAL" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

if [ "$COMPLETED_FINAL" -eq "$EXPECTED_STEP_COUNT" ]; then
    log_pass "R8.5: All $EXPECTED_STEP_COUNT steps have status='completed'"
    echo "R8.5: PASS — $COMPLETED_FINAL/$EXPECTED_STEP_COUNT steps completed" >> "$EVIDENCE_FILE"
else
    log_fail "R8.5: Only $COMPLETED_FINAL/$EXPECTED_STEP_COUNT steps completed"
    echo "R8.5: FAIL — $COMPLETED_FINAL/$EXPECTED_STEP_COUNT steps completed" >> "$EVIDENCE_FILE"

    # Identify non-completed steps
    NON_COMPLETED=$(psql_cmd "SELECT step_name, status FROM automation.setup_wizard_state WHERE status != 'completed' ORDER BY step_name;")
    if [ -n "$NON_COMPLETED" ]; then
        echo "Non-completed steps:" >> "$EVIDENCE_FILE"
        echo "$NON_COMPLETED" >> "$EVIDENCE_FILE"
    fi

    log_open_issue "R8" "Only $COMPLETED_FINAL/$EXPECTED_STEP_COUNT wizard steps completed (R8.5)"
    WIZARD_RESULT="fail"
fi

# =============================================================================
# R8.7: Evidence summary
# =============================================================================
echo "" >> "$EVIDENCE_FILE"
echo "--- Summary ---" >> "$EVIDENCE_FILE"
echo "expected_steps=$EXPECTED_STEP_COUNT" >> "$EVIDENCE_FILE"
echo "completed_steps=$COMPLETED_FINAL" >> "$EVIDENCE_FILE"
echo "mcp_healthz_checked=$MCP_HEALTHZ_DONE" >> "$EVIDENCE_FILE"
echo "dept_probe_completed=$DEPT_PROBE_DONE" >> "$EVIDENCE_FILE"
echo "wizard_result=$WIZARD_RESULT" >> "$EVIDENCE_FILE"
echo "completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

if [ "$WIZARD_RESULT" = "pass" ]; then
    echo "RESULT: R8 PASS" >> "$EVIDENCE_FILE"
    echo ""
    echo "============================================================================="
    echo "  WIZARD PROBE COMPLETE — ALL STEPS PASSED"
    echo "============================================================================="
else
    echo "RESULT: R8 FAIL" >> "$EVIDENCE_FILE"
    echo ""
    echo "============================================================================="
    echo "  WIZARD PROBE COMPLETE — FAILURES DETECTED (see evidence)"
    echo "============================================================================="
fi

echo ""
echo "Evidence written to: $EVIDENCE_FILE"
