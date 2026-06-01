#!/usr/bin/env bash
# =============================================================================
# platform/scripts/vps_shutdown_probe.sh — Graceful shutdown & volume retention
# =============================================================================
# Runs on VPS_Host. Implements Requirement 18 (R18.1–R18.5):
#
#   1. `make down` within 60 seconds, exit 0            (R18.1)
#   2. Assert `docker compose ps -q` returns empty      (R18.2)
#   3. Assert named volumes pg_data, minio_data,
#      agent_workspace still exist                      (R18.3)
#   4. Optional --destructive: wipe volumes + log       (R18.4)
#   5. Emit evidence to /tmp/18-shutdown.txt            (R18.5)
#
# Usage:
#   ./vps_shutdown_probe.sh               # graceful shutdown only
#   ./vps_shutdown_probe.sh --destructive # + volume removal
# =============================================================================
set -euo pipefail

PLATFORM_DIR="/opt/yeni_atlassian/platform"
COMPOSE_FILE="$PLATFORM_DIR/infra/docker-compose.yml"
EVIDENCE_FILE="/tmp/18-shutdown.txt"
DESTRUCTIVE=0

# --- Parse arguments ----------------------------------------------------------
for arg in "$@"; do
    case "$arg" in
        --destructive)
            DESTRUCTIVE=1
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            echo "Usage: $0 [--destructive]" >&2
            exit 2
            ;;
    esac
done

# --- Initialize evidence file -------------------------------------------------
: > "$EVIDENCE_FILE"
echo "=== VPS Shutdown Probe ===" >> "$EVIDENCE_FILE"
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

# --- R18.1: make down (max 60s, exit 0) --------------------------------------
echo "[R18.1] Running 'make down' with 60s timeout..."
echo "--- make down ---" >> "$EVIDENCE_FILE"

MAKE_DOWN_EXIT=0
MAKE_DOWN_OUTPUT=$(timeout 60 make -C "$PLATFORM_DIR" down 2>&1) || MAKE_DOWN_EXIT=$?

echo "$MAKE_DOWN_OUTPUT" >> "$EVIDENCE_FILE"
echo "make_down_exit_code=$MAKE_DOWN_EXIT" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

if [[ $MAKE_DOWN_EXIT -ne 0 ]]; then
    echo "[FAIL] make down exited with code $MAKE_DOWN_EXIT (expected 0)" >&2
    echo "RESULT: R18.1 FAIL — exit code $MAKE_DOWN_EXIT" >> "$EVIDENCE_FILE"
    exit 1
fi
echo "[PASS] make down completed successfully (exit 0)"

# --- R18.2: docker compose ps -q must be empty -------------------------------
echo "[R18.2] Asserting no running containers..."
echo "--- docker compose ps -q ---" >> "$EVIDENCE_FILE"

PS_OUTPUT=$(docker compose -f "$COMPOSE_FILE" ps -q 2>/dev/null || true)
echo "$PS_OUTPUT" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

if [[ -n "$PS_OUTPUT" ]]; then
    echo "[FAIL] Containers still running after make down:" >&2
    echo "$PS_OUTPUT" >&2
    echo "RESULT: R18.2 FAIL — containers still present" >> "$EVIDENCE_FILE"
    exit 1
fi
echo "[PASS] No running containers (ps -q is empty)"

# --- R18.3: Named volumes still exist ----------------------------------------
echo "[R18.3] Asserting named volumes are retained..."
echo "--- docker volume ls --filter 'name=infra_' ---" >> "$EVIDENCE_FILE"

VOLUME_OUTPUT=$(docker volume ls --filter 'name=infra_' 2>&1)
echo "$VOLUME_OUTPUT" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

REQUIRED_VOLUMES=("pg_data" "minio_data" "agent_workspace")
MISSING_VOLUMES=()

for vol in "${REQUIRED_VOLUMES[@]}"; do
    if ! echo "$VOLUME_OUTPUT" | grep -q "$vol"; then
        MISSING_VOLUMES+=("$vol")
    fi
done

if [[ ${#MISSING_VOLUMES[@]} -gt 0 ]]; then
    echo "[FAIL] Missing volumes: ${MISSING_VOLUMES[*]}" >&2
    echo "RESULT: R18.3 FAIL — missing volumes: ${MISSING_VOLUMES[*]}" >> "$EVIDENCE_FILE"
    exit 1
fi
echo "[PASS] All required volumes retained: ${REQUIRED_VOLUMES[*]}"

# --- R18.4: Optional destructive teardown -------------------------------------
if [[ $DESTRUCTIVE -eq 1 ]]; then
    echo "[R18.4] Destructive teardown requested — removing volumes..."
    echo "--- destructive teardown ---" >> "$EVIDENCE_FILE"

    docker compose -f "$COMPOSE_FILE" down -v --remove-orphans 2>&1 | tee -a "$EVIDENCE_FILE"
    echo "[DESTRUCTIVE] volumes removed" | tee -a "$EVIDENCE_FILE"
    echo "" >> "$EVIDENCE_FILE"
else
    echo "[INFO] Graceful shutdown only (no --destructive flag). Volumes preserved."
    echo "destructive_teardown=skipped" >> "$EVIDENCE_FILE"
    echo "" >> "$EVIDENCE_FILE"
fi

# --- Summary ------------------------------------------------------------------
echo "--- Summary ---" >> "$EVIDENCE_FILE"
echo "make_down_exit_code=0" >> "$EVIDENCE_FILE"
echo "containers_remaining=0" >> "$EVIDENCE_FILE"
echo "volumes_retained=${REQUIRED_VOLUMES[*]}" >> "$EVIDENCE_FILE"
echo "destructive=$DESTRUCTIVE" >> "$EVIDENCE_FILE"
echo "RESULT: R18 PASS" >> "$EVIDENCE_FILE"

echo ""
echo "=== Shutdown probe complete ==="
echo "Evidence written to: $EVIDENCE_FILE"
