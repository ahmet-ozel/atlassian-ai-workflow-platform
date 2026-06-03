#!/usr/bin/env bash
# =============================================================================
# platform/scripts/vps_boot_probe.sh — Boot bundle start & health verification
# =============================================================================
# Runs on VPS_Host. Boot probe checks:
#
#   1. Run `make boot` to start the four boot-bundle services       (R7.1)
#   2. Poll docker compose ps every 5s (max 180s) until all four
#      services report State=running, Health=healthy                 (R7.2)
#   3. Probe three healthcheck endpoints (200 OK expected)          (R7.3)
#   4. On timeout or exited service, capture logs and fail          (R7.4)
#   5. Assert no profile-gated services are running                 (R7.5)
#   6. Emit evidence to /tmp/07-boot.txt                            (R7.6)
#
# Exit codes:
#   0 = all assertions pass
#   1 = boot or health assertion failure
#
# Usage:
#   ./vps_boot_probe.sh
# =============================================================================
set -euo pipefail

PLATFORM_DIR="/opt/atlassian-ai-workflow-platform"
COMPOSE_FILE="$PLATFORM_DIR/infra/docker-compose.yml"
COMPOSE_DEV="$PLATFORM_DIR/infra/docker-compose.dev.yml"
EVIDENCE_FILE="/tmp/07-boot.txt"
EVIDENCE_DIR="/tmp"
POLL_INTERVAL=5
POLL_TIMEOUT=180

# Boot bundle expected services
BOOT_SERVICES=("postgres" "vault" "admin-dashboard-api" "admin-dashboard-ui")

# Forbidden substrings — profile-gated services that must NOT be running
FORBIDDEN_SUBSTRINGS="automation|assistant|streamlit|worker|temporal|mcp|firecrawl"

# --- Helpers ------------------------------------------------------------------

fail_boot() {
    echo "[FAIL] $1" >&2
    echo "RESULT: R7 FAIL — $1" >> "$EVIDENCE_FILE"
    exit 1
}

compose_cmd() {
    docker compose -f "$COMPOSE_FILE" -f "$COMPOSE_DEV" "$@"
}

# --- Initialize evidence file -------------------------------------------------
: > "$EVIDENCE_FILE"
echo "=== VPS Boot Probe ===" >> "$EVIDENCE_FILE"
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

# =============================================================================
# R7.1: Run make boot
# =============================================================================
echo "[R7.1] Running 'make boot' in $PLATFORM_DIR..."
echo "--- make boot ---" >> "$EVIDENCE_FILE"

BOOT_EXIT=0
BOOT_OUTPUT=$(make -C "$PLATFORM_DIR" boot 2>&1) || BOOT_EXIT=$?

echo "$BOOT_OUTPUT" >> "$EVIDENCE_FILE"
echo "make_boot_exit_code=$BOOT_EXIT" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

if [[ $BOOT_EXIT -ne 0 ]]; then
    fail_boot "make boot exited with code $BOOT_EXIT"
fi
echo "[PASS] make boot completed (exit 0)"

# =============================================================================
# R7.2: Poll docker compose ps until all 4 services are running + healthy
# =============================================================================
echo "[R7.2] Polling service health (interval=${POLL_INTERVAL}s, timeout=${POLL_TIMEOUT}s)..."
echo "--- health polling ---" >> "$EVIDENCE_FILE"

ELAPSED=0
ALL_HEALTHY=0

while [[ $ELAPSED -lt $POLL_TIMEOUT ]]; do
    ALL_HEALTHY=1

    for SVC in "${BOOT_SERVICES[@]}"; do
        # Get JSON status for this service
        SVC_JSON=$(compose_cmd ps --format json "$SVC" 2>/dev/null || echo "")

        if [[ -z "$SVC_JSON" ]]; then
            ALL_HEALTHY=0
            break
        fi

        # docker compose ps --format json may output one JSON object per line
        # Parse State and Health from the JSON output
        SVC_STATE=$(echo "$SVC_JSON" | grep -oP '"State"\s*:\s*"\K[^"]+' | head -1 || echo "unknown")
        SVC_HEALTH=$(echo "$SVC_JSON" | grep -oP '"Health"\s*:\s*"\K[^"]+' | head -1 || echo "unknown")

        # R7.4: If service has exited, capture logs and fail immediately
        if [[ "$SVC_STATE" == "exited" || "$SVC_STATE" == "dead" ]]; then
            echo "[R7.4] Service '$SVC' has State=$SVC_STATE — capturing logs..."
            FAIL_LOG="$EVIDENCE_DIR/07-boot-failure-${SVC}.log"
            compose_cmd logs "$SVC" > "$FAIL_LOG" 2>&1 || true
            echo "Failure log written to: $FAIL_LOG" >> "$EVIDENCE_FILE"
            fail_boot "Service '$SVC' exited (State=$SVC_STATE). Logs: $FAIL_LOG"
        fi

        if [[ "$SVC_STATE" != "running" || "$SVC_HEALTH" != "healthy" ]]; then
            ALL_HEALTHY=0
            break
        fi
    done

    if [[ $ALL_HEALTHY -eq 1 ]]; then
        echo "[PASS] All boot bundle services are running + healthy after ${ELAPSED}s"
        echo "health_poll_duration_seconds=$ELAPSED" >> "$EVIDENCE_FILE"
        break
    fi

    sleep "$POLL_INTERVAL"
    ELAPSED=$((ELAPSED + POLL_INTERVAL))
done

# R7.4: Timeout — capture logs for any unhealthy service
if [[ $ALL_HEALTHY -ne 1 ]]; then
    echo "[R7.4] Timeout after ${POLL_TIMEOUT}s — capturing logs for unhealthy services..."
    for SVC in "${BOOT_SERVICES[@]}"; do
        SVC_JSON=$(compose_cmd ps --format json "$SVC" 2>/dev/null || echo "")
        SVC_STATE=$(echo "$SVC_JSON" | grep -oP '"State"\s*:\s*"\K[^"]+' | head -1 || echo "unknown")
        SVC_HEALTH=$(echo "$SVC_JSON" | grep -oP '"Health"\s*:\s*"\K[^"]+' | head -1 || echo "unknown")

        if [[ "$SVC_STATE" != "running" || "$SVC_HEALTH" != "healthy" ]]; then
            FAIL_LOG="$EVIDENCE_DIR/07-boot-failure-${SVC}.log"
            compose_cmd logs "$SVC" > "$FAIL_LOG" 2>&1 || true
            echo "Service '$SVC' (State=$SVC_STATE, Health=$SVC_HEALTH) — log: $FAIL_LOG" >> "$EVIDENCE_FILE"
        fi
    done
    fail_boot "Timeout ${POLL_TIMEOUT}s exceeded — not all boot services healthy"
fi

# =============================================================================
# R7.3: Probe three healthcheck endpoints
# =============================================================================
echo "[R7.3] Probing healthcheck endpoints..."
echo "" >> "$EVIDENCE_FILE"
echo "--- healthcheck probes ---" >> "$EVIDENCE_FILE"

ENDPOINTS=(
    "http://localhost:8082/healthz"
    "http://localhost:3000/api/health"
    "http://localhost:8200/v1/sys/health"
)

PROBE_PASS=1
for URL in "${ENDPOINTS[@]}"; do
    echo "Probing: $URL" >> "$EVIDENCE_FILE"
    HTTP_CODE=$(curl -o /dev/null -w '%{http_code}' -fsS --max-time 10 "$URL" 2>/dev/null || echo "000")
    CURL_OUTPUT=$(curl -sS --max-time 10 "$URL" 2>&1 || echo "(curl failed)")

    echo "  HTTP_STATUS=$HTTP_CODE" >> "$EVIDENCE_FILE"
    echo "  RESPONSE=${CURL_OUTPUT:0:512}" >> "$EVIDENCE_FILE"
    echo "" >> "$EVIDENCE_FILE"

    if [[ "$HTTP_CODE" != "200" ]]; then
        echo "[FAIL] $URL returned HTTP $HTTP_CODE (expected 200)" >&2
        PROBE_PASS=0
    else
        echo "[PASS] $URL → 200 OK"
    fi
done

if [[ $PROBE_PASS -ne 1 ]]; then
    fail_boot "One or more healthcheck endpoints did not return HTTP 200"
fi

# =============================================================================
# R7.5: Boot bundle exclusivity — no profile-gated services running
# =============================================================================
echo "[R7.5] Asserting boot bundle exclusivity..."
echo "" >> "$EVIDENCE_FILE"
echo "--- boot bundle exclusivity ---" >> "$EVIDENCE_FILE"

RUNNING_NAMES=$(compose_cmd ps --format '{{.Name}}' 2>/dev/null || echo "")
echo "Running containers:" >> "$EVIDENCE_FILE"
echo "$RUNNING_NAMES" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

FORBIDDEN_FOUND=""
while IFS= read -r NAME; do
    [[ -z "$NAME" ]] && continue
    if echo "$NAME" | grep -qEi "$FORBIDDEN_SUBSTRINGS"; then
        FORBIDDEN_FOUND="${FORBIDDEN_FOUND}${NAME} "
    fi
done <<< "$RUNNING_NAMES"

if [[ -n "$FORBIDDEN_FOUND" ]]; then
    echo "EXCLUSIVITY_VIOLATION=$FORBIDDEN_FOUND" >> "$EVIDENCE_FILE"
    fail_boot "Profile-gated services found running: $FORBIDDEN_FOUND"
fi
echo "[PASS] No profile-gated services running (boot bundle exclusive)"
echo "EXCLUSIVITY=PASS" >> "$EVIDENCE_FILE"

# =============================================================================
# R7.6: Emit final evidence — ps table + healthcheck transcripts
# =============================================================================
echo "" >> "$EVIDENCE_FILE"
echo "--- docker compose ps (full table) ---" >> "$EVIDENCE_FILE"
compose_cmd ps >> "$EVIDENCE_FILE" 2>&1 || true
echo "" >> "$EVIDENCE_FILE"

echo "--- Summary ---" >> "$EVIDENCE_FILE"
echo "boot_exit_code=0" >> "$EVIDENCE_FILE"
echo "all_services_healthy=true" >> "$EVIDENCE_FILE"
echo "healthcheck_endpoints_pass=true" >> "$EVIDENCE_FILE"
echo "boot_bundle_exclusive=true" >> "$EVIDENCE_FILE"
echo "RESULT: R7 PASS" >> "$EVIDENCE_FILE"

echo ""
echo "=== Boot probe complete ==="
echo "Evidence written to: $EVIDENCE_FILE"
