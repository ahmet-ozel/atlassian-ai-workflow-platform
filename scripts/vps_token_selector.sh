#!/usr/bin/env bash
# =============================================================================
# platform/scripts/vps_token_selector.sh — Bitbucket Token Selector (VPS-side)
# =============================================================================
# Runs on VPS_Host AFTER MCP container is started with Token_B (Basic Auth).
# Called by vps_credential_loader.ps1 post-MCP-up.
#
# Flow:
#   1. Wait for atlassian-mcp container to become healthy (max 120s)
#   2. Call bitbucket_get_repository via MCP JSON-RPC with Token_B (Basic)
#   3. If HTTP non-2xx: switch to Token_A (Bearer), restart MCP, retry
#   4. Determine selection_verdict: token_b_only | token_a_only | both_succeed | both_fail
#   5. Cross-token check: if selected token works, try alternate for parity matrix
#   6. Emit evidence to /tmp/05-token-selection.json (D2 schema)
#   7. Set MCP env to final state based on selected_token_label
#
# Exit codes:
#   0 = token selected successfully (token_b_only, token_a_only, or both_succeed)
#   1 = both_fail — critical Open_Issue logged, halt
#   2 = MCP never became healthy
#
# Token selection must finish with a usable MCP credential mode.
# =============================================================================
set -euo pipefail

# --- Configuration -----------------------------------------------------------

PLATFORM_DIR="/opt/atlassian-ai-workflow-platform"
COMPOSE_FILE="$PLATFORM_DIR/infra/docker-compose.yml"
MCP_ENV_FILE="$PLATFORM_DIR/services/atlassian_mcp_bitbucket/.env"
MCP_ENDPOINT="http://localhost:8090/mcp"
EVIDENCE_FILE="/tmp/05-token-selection.json"

# Healthcheck polling
HEALTH_MAX_WAIT=120
HEALTH_INTERVAL=5

# JSON-RPC request body for bitbucket_get_repository
JSONRPC_BODY='{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"bitbucket_get_repository","arguments":{"workspace":"example_workspace","repo_slug":"smoke-test"}}}'

# --- Helpers -----------------------------------------------------------------

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
}

# Wait for atlassian-mcp to report healthy
# Returns 0 if healthy, 1 if timeout
wait_mcp_healthy() {
  local elapsed=0
  log "Waiting for atlassian-mcp to become healthy (max ${HEALTH_MAX_WAIT}s)..."

  while [ $elapsed -lt $HEALTH_MAX_WAIT ]; do
    local health
    health=$(docker compose -f "$COMPOSE_FILE" ps atlassian-mcp --format '{{.Health}}' 2>/dev/null || echo "unknown")

    if [ "$health" = "healthy" ]; then
      log "atlassian-mcp is healthy (after ${elapsed}s)"
      return 0
    fi

    sleep $HEALTH_INTERVAL
    elapsed=$((elapsed + HEALTH_INTERVAL))
  done

  log "ERROR: atlassian-mcp did not become healthy within ${HEALTH_MAX_WAIT}s (last status: $health)"
  return 1
}

# Call MCP bitbucket_get_repository and capture results
# Sets global vars: CALL_HTTP_STATUS, CALL_LATENCY_MS, CALL_RESPONSE_EXCERPT
call_mcp_bitbucket() {
  local start_ms end_ms response http_code body

  # Capture timing + response + HTTP status code
  start_ms=$(date +%s%3N 2>/dev/null || python3 -c "import time; print(int(time.time()*1000))")

  # Use curl with -w to get HTTP status, -o for body
  local tmpfile
  tmpfile=$(mktemp)

  http_code=$(curl -s -o "$tmpfile" -w '%{http_code}' \
    -X POST "$MCP_ENDPOINT" \
    -H "Content-Type: application/json" \
    -d "$JSONRPC_BODY" \
    --max-time 30 2>/dev/null || echo "000")

  end_ms=$(date +%s%3N 2>/dev/null || python3 -c "import time; print(int(time.time()*1000))")

  body=$(cat "$tmpfile" 2>/dev/null || echo "")
  rm -f "$tmpfile"

  # Calculate latency
  CALL_LATENCY_MS=$((end_ms - start_ms))
  CALL_HTTP_STATUS="$http_code"

  # Response excerpt: first 256 chars, redact sensitive tokens
  CALL_RESPONSE_EXCERPT=$(echo "$body" | head -c 256 | \
    sed -E 's/ATCTT3x[A-Za-z0-9_-]+/[REDACTED_TOKEN_A]/g' | \
    sed -E 's/ATATT3x[A-Za-z0-9_-]+/[REDACTED_TOKEN_B]/g' | \
    sed -E 's/sk-proj-[A-Za-z0-9_-]+/[REDACTED_OPENAI]/g' || echo "")

  log "MCP call result: HTTP $CALL_HTTP_STATUS, latency ${CALL_LATENCY_MS}ms"
}

# Check if HTTP status is 2xx
is_2xx() {
  local code="$1"
  [[ "$code" =~ ^2[0-9]{2}$ ]]
}

# Switch MCP env to Token_A (Bearer) — remove Basic Auth, add PAT
switch_to_token_a() {
  log "Switching MCP env to Token_A (Bearer)..."

  # Read Token_A from env file comment or known location
  # The credential_loader.ps1 stores Token_A value as a comment for fallback
  # We need to: remove BITBUCKET_USERNAME + BITBUCKET_PASSWORD, add BITBUCKET_PERSONAL_TOKEN

  # Remove Basic Auth lines
  sed -i '/^BITBUCKET_USERNAME=/d' "$MCP_ENV_FILE"
  sed -i '/^BITBUCKET_PASSWORD=/d' "$MCP_ENV_FILE"

  # Check if TOKEN_A_VALUE is passed as env var or stored in a sidecar file
  local token_a_value=""
  if [ -n "${BITBUCKET_TOKEN_A:-}" ]; then
    token_a_value="$BITBUCKET_TOKEN_A"
  elif [ -f "$PLATFORM_DIR/.token_a_value" ]; then
    token_a_value=$(cat "$PLATFORM_DIR/.token_a_value")
  else
    log "ERROR: Cannot find Token_A value. Set BITBUCKET_TOKEN_A env var or place value in $PLATFORM_DIR/.token_a_value"
    return 1
  fi

  # Add Bearer token
  echo "BITBUCKET_PERSONAL_TOKEN=$token_a_value" >> "$MCP_ENV_FILE"

  log "MCP env switched to Token_A (Bearer)"
}

# Switch MCP env back to Token_B (Basic Auth) — remove PAT, add Basic Auth
switch_to_token_b() {
  log "Switching MCP env to Token_B (Basic Auth)..."

  # Remove Bearer token line
  sed -i '/^BITBUCKET_PERSONAL_TOKEN=/d' "$MCP_ENV_FILE"

  # Restore Basic Auth lines
  local token_b_username=""
  local token_b_password=""

  if [ -n "${BITBUCKET_TOKEN_B_USERNAME:-}" ] && [ -n "${BITBUCKET_TOKEN_B_PASSWORD:-}" ]; then
    token_b_username="$BITBUCKET_TOKEN_B_USERNAME"
    token_b_password="$BITBUCKET_TOKEN_B_PASSWORD"
  elif [ -f "$PLATFORM_DIR/.token_b_credentials" ]; then
    token_b_username=$(sed -n '1p' "$PLATFORM_DIR/.token_b_credentials")
    token_b_password=$(sed -n '2p' "$PLATFORM_DIR/.token_b_credentials")
  else
    log "ERROR: Cannot find Token_B credentials. Set BITBUCKET_TOKEN_B_USERNAME/PASSWORD or place in $PLATFORM_DIR/.token_b_credentials"
    return 1
  fi

  echo "BITBUCKET_USERNAME=$token_b_username" >> "$MCP_ENV_FILE"
  echo "BITBUCKET_PASSWORD=$token_b_password" >> "$MCP_ENV_FILE"

  log "MCP env switched to Token_B (Basic Auth)"
}

# Restart atlassian-mcp and wait for healthy
restart_mcp() {
  log "Restarting atlassian-mcp container..."
  docker compose -f "$COMPOSE_FILE" restart atlassian-mcp
  wait_mcp_healthy
}

# Write evidence JSON to /tmp/05-token-selection.json (D2 schema)
# Uses Python for reliable JSON generation (avoids shell quoting issues)
write_evidence() {
  local verdict="$1"
  local primary_status="$2"
  local primary_latency="$3"
  local primary_excerpt="$4"
  local fallback_status="${5:-null}"
  local fallback_latency="${6:-0}"
  local fallback_excerpt="${7:-}"
  local selected_label="$8"
  local cross_token_status="${9:-null}"
  local cross_token_latency="${10:-0}"
  local cross_token_excerpt="${11:-}"

  python3 -c "
import json, sys
from datetime import datetime, timezone

verdict = sys.argv[1]
primary_status = int(sys.argv[2]) if sys.argv[2] != 'null' else 0
primary_latency = int(sys.argv[3])
primary_excerpt = sys.argv[4]
fallback_status_raw = sys.argv[5]
fallback_latency = int(sys.argv[6])
fallback_excerpt = sys.argv[7]
selected_label = sys.argv[8]
cross_status_raw = sys.argv[9]
cross_latency = int(sys.argv[10])
cross_excerpt = sys.argv[11]

evidence = {
    'selection_verdict': verdict,
    'primary_attempt': {
        'token_label': 'Bitbucket_Token_B',
        'auth_mode': 'Basic',
        'tool_name': 'bitbucket_get_repository',
        'http_status': primary_status,
        'latency_ms': primary_latency,
        'response_excerpt': primary_excerpt
    },
    'fallback_attempt': None,
    'cross_token_check': None,
    'selected_token_label': selected_label,
    'captured_at_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
}

if fallback_status_raw != 'null':
    evidence['fallback_attempt'] = {
        'token_label': 'Bitbucket_Token_A',
        'auth_mode': 'Bearer',
        'tool_name': 'bitbucket_get_repository',
        'http_status': int(fallback_status_raw),
        'latency_ms': fallback_latency,
        'response_excerpt': fallback_excerpt
    }

if cross_status_raw != 'null':
    alt_label = 'Bitbucket_Token_A' if selected_label == 'Bitbucket_Token_B' else 'Bitbucket_Token_B'
    alt_mode = 'Bearer' if selected_label == 'Bitbucket_Token_B' else 'Basic'
    evidence['cross_token_check'] = {
        'token_label': alt_label,
        'auth_mode': alt_mode,
        'tool_name': 'bitbucket_get_repository',
        'http_status': int(cross_status_raw),
        'latency_ms': cross_latency,
        'response_excerpt': cross_excerpt
    }

with open('$EVIDENCE_FILE', 'w') as f:
    json.dump(evidence, f, indent=2, ensure_ascii=False)
" "$verdict" "$primary_status" "$primary_latency" "$primary_excerpt" \
  "$fallback_status" "$fallback_latency" "$fallback_excerpt" \
  "$selected_label" "$cross_token_status" "$cross_token_latency" "$cross_token_excerpt"

  log "Evidence written to $EVIDENCE_FILE"
}

# Log critical Open_Issue for both_fail (R5.6)
log_critical_open_issue() {
  log "[CRITICAL OPEN ISSUE] R5 — both Bitbucket tokens failed"

  # Call the Python open issue logger if available
  if command -v python3 &>/dev/null && [ -f "$PLATFORM_DIR/scripts/vps_open_issue_logger.py" ]; then
    python3 "$PLATFORM_DIR/scripts/vps_open_issue_logger.py" \
      --requirement R5 \
      --severity critical \
      --category config \
      --summary "Both Bitbucket tokens (Token_A Bearer + Token_B Basic) failed MCP bitbucket_get_repository call" \
      --evidence-path "vps-test-evidence/05-token-selection.json" \
      --recommended-action config_change || true
  fi
}

# =============================================================================
# MAIN FLOW
# =============================================================================

log "=== Bitbucket Token Selector — Start ==="
log "MCP endpoint: $MCP_ENDPOINT"
log "MCP env file: $MCP_ENV_FILE"

# --- Step 1: Wait for MCP healthy (initial Token_B config) --------------------

if ! wait_mcp_healthy; then
  log "FATAL: MCP container never became healthy. Cannot proceed with token selection."
  exit 2
fi

# --- Step 2: Call bitbucket_get_repository with Token_B (Basic) ---------------

log "--- Primary attempt: Token_B (Basic Auth) ---"
call_mcp_bitbucket

PRIMARY_HTTP_STATUS="$CALL_HTTP_STATUS"
PRIMARY_LATENCY_MS="$CALL_LATENCY_MS"
PRIMARY_EXCERPT="$CALL_RESPONSE_EXCERPT"

log "Primary attempt (Token_B): HTTP $PRIMARY_HTTP_STATUS, latency ${PRIMARY_LATENCY_MS}ms"

# --- Step 3: If non-2xx, switch to Token_A and retry -------------------------

FALLBACK_HTTP_STATUS="null"
FALLBACK_LATENCY_MS=0
FALLBACK_EXCERPT=""
TOKEN_B_SUCCESS=false
TOKEN_A_SUCCESS=false

if is_2xx "$PRIMARY_HTTP_STATUS"; then
  TOKEN_B_SUCCESS=true
  log "Token_B succeeded (HTTP $PRIMARY_HTTP_STATUS)"
else
  log "Token_B failed (HTTP $PRIMARY_HTTP_STATUS). Switching to Token_A (Bearer)..."

  # R5.4: Switch to Token_A, restart MCP, retry
  if ! switch_to_token_a; then
    log "ERROR: Failed to switch to Token_A. Treating as both_fail."
    write_evidence "both_fail" "$PRIMARY_HTTP_STATUS" "$PRIMARY_LATENCY_MS" "$PRIMARY_EXCERPT" \
      "null" "0" "" "none" "null" "0" ""
    log_critical_open_issue
    exit 1
  fi

  if ! restart_mcp; then
    log "ERROR: MCP did not become healthy after Token_A switch."
    write_evidence "both_fail" "$PRIMARY_HTTP_STATUS" "$PRIMARY_LATENCY_MS" "$PRIMARY_EXCERPT" \
      "000" "0" "MCP unhealthy after restart" "none" "null" "0" ""
    log_critical_open_issue
    exit 1
  fi

  log "--- Fallback attempt: Token_A (Bearer) ---"
  call_mcp_bitbucket

  FALLBACK_HTTP_STATUS="$CALL_HTTP_STATUS"
  FALLBACK_LATENCY_MS="$CALL_LATENCY_MS"
  FALLBACK_EXCERPT="$CALL_RESPONSE_EXCERPT"

  log "Fallback attempt (Token_A): HTTP $FALLBACK_HTTP_STATUS, latency ${FALLBACK_LATENCY_MS}ms"

  if is_2xx "$FALLBACK_HTTP_STATUS"; then
    TOKEN_A_SUCCESS=true
    log "Token_A succeeded (HTTP $FALLBACK_HTTP_STATUS)"
  else
    log "Token_A also failed (HTTP $FALLBACK_HTTP_STATUS)"
  fi
fi

# --- Step 4: Determine selection_verdict (R5.5) -------------------------------

SELECTION_VERDICT=""
SELECTED_TOKEN_LABEL=""

if [ "$TOKEN_B_SUCCESS" = true ] && [ "$TOKEN_A_SUCCESS" = false ]; then
  # Token_B worked on first try; Token_A not yet tested (will be tested in cross-check)
  SELECTION_VERDICT="token_b_only"
  SELECTED_TOKEN_LABEL="Bitbucket_Token_B"
elif [ "$TOKEN_B_SUCCESS" = false ] && [ "$TOKEN_A_SUCCESS" = true ]; then
  SELECTION_VERDICT="token_a_only"
  SELECTED_TOKEN_LABEL="Bitbucket_Token_A"
elif [ "$TOKEN_B_SUCCESS" = false ] && [ "$TOKEN_A_SUCCESS" = false ]; then
  SELECTION_VERDICT="both_fail"
  SELECTED_TOKEN_LABEL="none"
fi
# Note: both_succeed is determined after cross-token check below

# --- Step 4b: Handle both_fail (R5.6) ----------------------------------------

if [ "$SELECTION_VERDICT" = "both_fail" ]; then
  log "CRITICAL: selection_verdict = both_fail"
  write_evidence "both_fail" "$PRIMARY_HTTP_STATUS" "$PRIMARY_LATENCY_MS" "$PRIMARY_EXCERPT" \
    "$FALLBACK_HTTP_STATUS" "$FALLBACK_LATENCY_MS" "$FALLBACK_EXCERPT" \
    "none" "null" "0" ""
  log_critical_open_issue
  exit 1
fi

# --- Step 5: Cross-token check (R12.9 preparation) ---------------------------
# If selected token succeeded, try the alternate token once for parity matrix

CROSS_HTTP_STATUS="null"
CROSS_LATENCY_MS=0
CROSS_EXCERPT=""

if [ "$TOKEN_B_SUCCESS" = true ]; then
  # Token_B worked; now try Token_A for cross-check
  log "--- Cross-token check: trying Token_A (Bearer) ---"

  if switch_to_token_a; then
    if restart_mcp; then
      call_mcp_bitbucket
      CROSS_HTTP_STATUS="$CALL_HTTP_STATUS"
      CROSS_LATENCY_MS="$CALL_LATENCY_MS"
      CROSS_EXCERPT="$CALL_RESPONSE_EXCERPT"

      if is_2xx "$CROSS_HTTP_STATUS"; then
        TOKEN_A_SUCCESS=true
        SELECTION_VERDICT="both_succeed"
        log "Cross-token check: Token_A also succeeded → both_succeed"
      else
        log "Cross-token check: Token_A failed (HTTP $CROSS_HTTP_STATUS) → token_b_only confirmed"
      fi

      # Switch back to Token_B (the selected token)
      switch_to_token_b
      restart_mcp
    else
      log "WARN: MCP unhealthy after Token_A cross-check switch. Reverting to Token_B."
      switch_to_token_b
      restart_mcp
    fi
  else
    log "WARN: Could not switch to Token_A for cross-check. Skipping."
  fi

elif [ "$TOKEN_A_SUCCESS" = true ]; then
  # Token_A worked (after Token_B failed); now try Token_B again for cross-check
  # Token_B already failed in primary attempt, so we record that as the cross-check
  # The cross-token result is the primary attempt result (Token_B failed)
  CROSS_HTTP_STATUS="$PRIMARY_HTTP_STATUS"
  CROSS_LATENCY_MS="$PRIMARY_LATENCY_MS"
  CROSS_EXCERPT="$PRIMARY_EXCERPT"
  log "Cross-token check: Token_B already failed in primary attempt (HTTP $PRIMARY_HTTP_STATUS) → token_a_only confirmed"
  # MCP is already on Token_A from the fallback, which is correct
fi

# --- Step 6: Write evidence (D2 schema) --------------------------------------

write_evidence "$SELECTION_VERDICT" \
  "$PRIMARY_HTTP_STATUS" "$PRIMARY_LATENCY_MS" "$PRIMARY_EXCERPT" \
  "$FALLBACK_HTTP_STATUS" "$FALLBACK_LATENCY_MS" "$FALLBACK_EXCERPT" \
  "$SELECTED_TOKEN_LABEL" \
  "$CROSS_HTTP_STATUS" "$CROSS_LATENCY_MS" "$CROSS_EXCERPT"

# --- Step 7: Ensure MCP env is in final state for selected token --------------

log "--- Final MCP env state: $SELECTED_TOKEN_LABEL ---"

if [ "$SELECTED_TOKEN_LABEL" = "Bitbucket_Token_B" ]; then
  # Ensure Token_B (Basic Auth) is active
  if grep -q "^BITBUCKET_PERSONAL_TOKEN=" "$MCP_ENV_FILE" 2>/dev/null; then
    switch_to_token_b
    restart_mcp
  fi
  log "MCP env confirmed: Token_B (Basic Auth) active"

elif [ "$SELECTED_TOKEN_LABEL" = "Bitbucket_Token_A" ]; then
  # Ensure Token_A (Bearer) is active
  if ! grep -q "^BITBUCKET_PERSONAL_TOKEN=" "$MCP_ENV_FILE" 2>/dev/null; then
    switch_to_token_a
    restart_mcp
  fi
  log "MCP env confirmed: Token_A (Bearer) active"
fi

# --- Summary ------------------------------------------------------------------

log "=== Bitbucket Token Selector — Complete ==="
log "selection_verdict: $SELECTION_VERDICT"
log "selected_token_label: $SELECTED_TOKEN_LABEL"
log "Evidence: $EVIDENCE_FILE"

exit 0
