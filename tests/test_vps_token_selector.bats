#!/usr/bin/env bats
# =============================================================================
# test_vps_token_selector.bats — Unit tests for vps_token_selector.sh
#
# Uses bats-core to test the four verdict paths of the Bitbucket Token Selector
# by mocking curl responses (200, 401, 403) and docker compose commands.
#
# Requirements validated: R5.5 (selection_verdict determination)
#
# Verdict paths tested:
#   1. token_b_only  — Token_B HTTP 200, cross-check Token_A fails
#   2. token_a_only  — Token_B HTTP 401, fallback Token_A HTTP 200
#   3. both_succeed  — Token_B HTTP 200, cross-check Token_A HTTP 200
#   4. both_fail     — Token_B HTTP 403, fallback Token_A HTTP 401
#
# Usage:
#   bats platform/tests/test_vps_token_selector.bats
# =============================================================================

# --- Setup / Teardown --------------------------------------------------------

setup() {
  # Create temporary directories for mocks and test artifacts
  MOCK_BIN="$(mktemp -d)"
  TEST_PLATFORM_DIR="$(mktemp -d)"
  TEST_EVIDENCE_DIR="$(mktemp -d)"
  export PATH="${MOCK_BIN}:${PATH}"

  # Script under test
  SCRIPT_DIR="$(cd "$(dirname "${BATS_TEST_FILENAME}")/../scripts" && pwd)"
  SCRIPT_UNDER_TEST="${SCRIPT_DIR}/vps_token_selector.sh"

  # Create a testable copy with patched paths
  TEST_SCRIPT="$(mktemp)"
  cp "${SCRIPT_UNDER_TEST}" "${TEST_SCRIPT}"

  # Patch configuration paths in the test script
  sed -i "s|PLATFORM_DIR=\"/opt/yeni_atlassian/platform\"|PLATFORM_DIR=\"${TEST_PLATFORM_DIR}\"|g" "${TEST_SCRIPT}"
  sed -i "s|COMPOSE_FILE=\"\$PLATFORM_DIR/infra/docker-compose.yml\"|COMPOSE_FILE=\"${TEST_PLATFORM_DIR}/infra/docker-compose.yml\"|g" "${TEST_SCRIPT}"
  sed -i "s|MCP_ENV_FILE=\"\$PLATFORM_DIR/services/atlassian_unified/.env\"|MCP_ENV_FILE=\"${TEST_PLATFORM_DIR}/services/atlassian_unified/.env\"|g" "${TEST_SCRIPT}"
  sed -i "s|EVIDENCE_FILE=\"/tmp/05-token-selection.json\"|EVIDENCE_FILE=\"${TEST_EVIDENCE_DIR}/05-token-selection.json\"|g" "${TEST_SCRIPT}"

  # Reduce health wait time for faster tests
  sed -i "s|HEALTH_MAX_WAIT=120|HEALTH_MAX_WAIT=5|g" "${TEST_SCRIPT}"
  sed -i "s|HEALTH_INTERVAL=5|HEALTH_INTERVAL=1|g" "${TEST_SCRIPT}"

  chmod +x "${TEST_SCRIPT}"

  # Create required directory structure
  mkdir -p "${TEST_PLATFORM_DIR}/infra"
  mkdir -p "${TEST_PLATFORM_DIR}/services/atlassian_unified"
  mkdir -p "${TEST_PLATFORM_DIR}/scripts"

  # Create initial MCP env file with Token_B (Basic Auth)
  cat > "${TEST_PLATFORM_DIR}/services/atlassian_unified/.env" <<EOF
JIRA_URL=https://example.atlassian.net
CONFLUENCE_URL=https://example.atlassian.net/wiki
JIRA_USERNAME=user@example.com
JIRA_API_TOKEN=FAKE_JIRA_TOKEN
CONFLUENCE_USERNAME=user@example.com
CONFLUENCE_API_TOKEN=FAKE_CONFLUENCE_TOKEN
TRANSPORT=streamable-http
PORT=8090
READ_ONLY=false
MCP_VERY_VERBOSE=true
BITBUCKET_USERNAME=user@example.com
BITBUCKET_PASSWORD=FAKE_TOKEN_B_VALUE
EOF

  # Create Token_A sidecar file for fallback
  echo "FAKE_TOKEN_A_VALUE" > "${TEST_PLATFORM_DIR}/.token_a_value"

  # Create Token_B credentials sidecar file for switch-back
  printf "user@example.com\nFAKE_TOKEN_B_VALUE\n" > "${TEST_PLATFORM_DIR}/.token_b_credentials"

  # Create a dummy open issue logger script (no-op)
  cat > "${TEST_PLATFORM_DIR}/scripts/vps_open_issue_logger.py" <<'SCRIPT'
#!/usr/bin/env python3
import sys
print(f"[MOCK] Open issue logged: {' '.join(sys.argv[1:])}")
SCRIPT
  chmod +x "${TEST_PLATFORM_DIR}/scripts/vps_open_issue_logger.py"

  # Create default mocks
  _create_mock_docker_compose_healthy
  _create_mock_date
  _create_mock_sleep
  _create_mock_mktemp
}

teardown() {
  rm -rf "${MOCK_BIN}"
  rm -rf "${TEST_PLATFORM_DIR}"
  rm -rf "${TEST_EVIDENCE_DIR}"
  rm -f "${TEST_SCRIPT}"
}

# --- Mock Creators -----------------------------------------------------------

_create_mock_docker_compose_healthy() {
  # Mock docker compose: always reports healthy, restart is no-op
  cat > "${MOCK_BIN}/docker" <<'SCRIPT'
#!/bin/bash
# Handle "docker compose" subcommands
if [[ "$1" == "compose" ]]; then
  shift  # remove "compose"
  # Skip -f <file> arguments
  while [[ "$1" == "-f" ]]; do
    shift; shift
  done

  case "$1" in
    ps)
      # Return healthy status for atlassian-mcp
      echo "healthy"
      ;;
    restart)
      # No-op restart
      exit 0
      ;;
  esac
fi
exit 0
SCRIPT
  chmod +x "${MOCK_BIN}/docker"
}

_create_mock_docker_compose_unhealthy() {
  # Mock docker compose: always reports unhealthy (for timeout testing)
  cat > "${MOCK_BIN}/docker" <<'SCRIPT'
#!/bin/bash
if [[ "$1" == "compose" ]]; then
  shift
  while [[ "$1" == "-f" ]]; do
    shift; shift
  done
  case "$1" in
    ps)
      echo "starting"
      ;;
    restart)
      exit 0
      ;;
  esac
fi
exit 0
SCRIPT
  chmod +x "${MOCK_BIN}/docker"
}

_create_mock_curl_sequence() {
  # Create a curl mock that returns different HTTP status codes in sequence
  # Arguments: status_code_1 status_code_2 status_code_3 ...
  # Each call consumes the next status code from a state file
  local state_file="${MOCK_BIN}/.curl_call_counter"
  echo "0" > "${state_file}"

  # Write the sequence of responses to a file
  local response_file="${MOCK_BIN}/.curl_responses"
  printf "%s\n" "$@" > "${response_file}"

  cat > "${MOCK_BIN}/curl" <<SCRIPT
#!/bin/bash
STATE_FILE="${state_file}"
RESPONSE_FILE="${response_file}"

# Read current call index
CALL_IDX=\$(cat "\${STATE_FILE}")

# Get the HTTP status for this call
HTTP_STATUS=\$(sed -n "\$((CALL_IDX + 1))p" "\${RESPONSE_FILE}")
if [ -z "\${HTTP_STATUS}" ]; then
  # Default to last status if we run out of sequence
  HTTP_STATUS=\$(tail -1 "\${RESPONSE_FILE}")
fi

# Increment counter
echo \$((CALL_IDX + 1)) > "\${STATE_FILE}"

# Parse curl arguments to find -o (output file) and -w (write-out)
OUTPUT_FILE=""
WRITE_OUT=""
while [[ \$# -gt 0 ]]; do
  case "\$1" in
    -o) OUTPUT_FILE="\$2"; shift 2 ;;
    -w) WRITE_OUT="\$2"; shift 2 ;;
    *) shift ;;
  esac
done

# Write mock response body to output file
if [ -n "\${OUTPUT_FILE}" ]; then
  if [[ "\${HTTP_STATUS}" == 2* ]]; then
    echo '{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"{\"full_name\":\"example_workspace/smoke-test\",\"mainbranch\":{\"name\":\"main\"}}"}]}}' > "\${OUTPUT_FILE}"
  elif [[ "\${HTTP_STATUS}" == "401" ]]; then
    echo '{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"Unauthorized: Invalid credentials"}}' > "\${OUTPUT_FILE}"
  elif [[ "\${HTTP_STATUS}" == "403" ]]; then
    echo '{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"Forbidden: Insufficient permissions"}}' > "\${OUTPUT_FILE}"
  else
    echo '{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"Unknown error"}}' > "\${OUTPUT_FILE}"
  fi
fi

# If -w '%{http_code}' was requested, output the HTTP status
if [[ "\${WRITE_OUT}" == *"http_code"* ]]; then
  echo "\${HTTP_STATUS}"
else
  echo "\${HTTP_STATUS}"
fi
SCRIPT
  chmod +x "${MOCK_BIN}/curl"
}

_create_mock_python3() {
  # Mock python3 for evidence writing and timing
  cat > "${MOCK_BIN}/python3" <<SCRIPT
#!/bin/bash
# Handle different python3 invocations
if [[ "\$1" == "-c" ]]; then
  PYCODE="\$2"
  # Timing helper: return milliseconds
  if echo "\${PYCODE}" | grep -q "time.time"; then
    echo "\$(date +%s)000"
  # JSON evidence writer
  elif echo "\${PYCODE}" | grep -q "json.dump"; then
    # Execute the actual python code for evidence writing
    # Create a minimal evidence JSON
    EVIDENCE_FILE="${TEST_EVIDENCE_DIR}/05-token-selection.json"
    # Extract verdict from arguments (argv[1])
    shift; shift  # skip -c and code
    VERDICT="\${1:-unknown}"
    cat > "\${EVIDENCE_FILE}" <<JSONEOF
{
  "selection_verdict": "\${VERDICT}",
  "primary_attempt": {"token_label": "Bitbucket_Token_B", "auth_mode": "Basic", "http_status": \${2:-0}},
  "fallback_attempt": null,
  "selected_token_label": "\${8:-none}",
  "captured_at_utc": "2025-01-15T10:00:00Z"
}
JSONEOF
  fi
else
  # Direct script execution (open issue logger)
  exit 0
fi
SCRIPT
  chmod +x "${MOCK_BIN}/python3"
}

_create_mock_date() {
  cat > "${MOCK_BIN}/date" <<'SCRIPT'
#!/bin/bash
if [[ "$*" == *"%s%3N"* ]]; then
  # Return millisecond timestamp (incrementing for latency calc)
  STATE_FILE="/tmp/.mock_date_counter"
  if [ -f "${STATE_FILE}" ]; then
    VAL=$(cat "${STATE_FILE}")
    echo $((VAL + 300))
    echo $((VAL + 300)) > "${STATE_FILE}"
  else
    echo "1705312800000"
    echo "1705312800000" > "${STATE_FILE}"
  fi
elif [[ "$*" == *"%Y-%m-%dT"* ]]; then
  echo "2025-01-15T10:00:00Z"
else
  echo "2025-01-15T10:00:00Z"
fi
SCRIPT
  chmod +x "${MOCK_BIN}/date"
}

_create_mock_sleep() {
  # No-op sleep for fast tests
  cat > "${MOCK_BIN}/sleep" <<'SCRIPT'
#!/bin/bash
exit 0
SCRIPT
  chmod +x "${MOCK_BIN}/sleep"
}

_create_mock_mktemp() {
  # Mock mktemp to return a predictable temp file
  local tmpfile="${MOCK_BIN}/.mock_curl_body"
  touch "${tmpfile}"
  cat > "${MOCK_BIN}/mktemp" <<SCRIPT
#!/bin/bash
if [[ "\$1" == "-d" ]]; then
  # For directory creation, use real mktemp
  /usr/bin/mktemp -d
else
  echo "${tmpfile}"
  touch "${tmpfile}"
fi
SCRIPT
  chmod +x "${MOCK_BIN}/mktemp"
}

# =============================================================================
# TEST: Verdict path — token_b_only
# Token_B (Basic) succeeds with HTTP 200, cross-check Token_A fails (HTTP 403)
# =============================================================================

@test "R5.5: verdict=token_b_only when Token_B returns 200 and cross-check Token_A returns 403" {
  # Curl sequence: 1st call (Token_B) = 200, 2nd call (cross-check Token_A) = 403
  _create_mock_curl_sequence 200 403
  _create_mock_python3

  run bash "${TEST_SCRIPT}"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Token_B succeeded"* ]]
  [[ "$output" == *"token_b_only"* ]] || [[ "$output" == *"selection_verdict: token_b_only"* ]]
}

@test "R5.5: token_b_only exits with code 0 (success)" {
  _create_mock_curl_sequence 200 401
  _create_mock_python3

  run bash "${TEST_SCRIPT}"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Bitbucket Token Selector — Complete"* ]]
}

# =============================================================================
# TEST: Verdict path — token_a_only
# Token_B (Basic) fails with HTTP 401, fallback Token_A (Bearer) succeeds 200
# =============================================================================

@test "R5.5: verdict=token_a_only when Token_B returns 401 and Token_A returns 200" {
  # Curl sequence: 1st call (Token_B) = 401, 2nd call (Token_A fallback) = 200
  _create_mock_curl_sequence 401 200
  _create_mock_python3

  run bash "${TEST_SCRIPT}"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Token_B failed"* ]]
  [[ "$output" == *"Token_A succeeded"* ]]
  [[ "$output" == *"token_a_only"* ]] || [[ "$output" == *"selection_verdict: token_a_only"* ]]
}

@test "R5.5: token_a_only switches MCP env to Bearer token" {
  _create_mock_curl_sequence 401 200
  _create_mock_python3

  run bash "${TEST_SCRIPT}"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Switching MCP env to Token_A (Bearer)"* ]]
  [[ "$output" == *"selected_token_label: Bitbucket_Token_A"* ]]
}

# =============================================================================
# TEST: Verdict path — both_succeed
# Token_B (Basic) succeeds 200, cross-check Token_A also succeeds 200
# =============================================================================

@test "R5.5: verdict=both_succeed when Token_B returns 200 and cross-check Token_A returns 200" {
  # Curl sequence: 1st call (Token_B) = 200, 2nd call (cross-check Token_A) = 200
  _create_mock_curl_sequence 200 200
  _create_mock_python3

  run bash "${TEST_SCRIPT}"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Token_B succeeded"* ]]
  [[ "$output" == *"both_succeed"* ]] || [[ "$output" == *"selection_verdict: both_succeed"* ]]
}

@test "R5.5: both_succeed keeps Token_B as selected token" {
  _create_mock_curl_sequence 200 200
  _create_mock_python3

  run bash "${TEST_SCRIPT}"

  [ "$status" -eq 0 ]
  [[ "$output" == *"selected_token_label: Bitbucket_Token_B"* ]]
}

# =============================================================================
# TEST: Verdict path — both_fail
# Token_B (Basic) fails 403, fallback Token_A (Bearer) also fails 401
# =============================================================================

@test "R5.5: verdict=both_fail when Token_B returns 403 and Token_A returns 401" {
  # Curl sequence: 1st call (Token_B) = 403, 2nd call (Token_A fallback) = 401
  _create_mock_curl_sequence 403 401
  _create_mock_python3

  run bash "${TEST_SCRIPT}"

  [ "$status" -eq 1 ]
  [[ "$output" == *"Token_B failed"* ]]
  [[ "$output" == *"Token_A also failed"* ]]
  [[ "$output" == *"both_fail"* ]]
}

@test "R5.5: both_fail exits with code 1 (halt)" {
  _create_mock_curl_sequence 403 401
  _create_mock_python3

  run bash "${TEST_SCRIPT}"

  [ "$status" -eq 1 ]
  [[ "$output" == *"CRITICAL"* ]]
}

@test "R5.5: both_fail logs critical open issue (R5.6)" {
  _create_mock_curl_sequence 401 403
  _create_mock_python3

  run bash "${TEST_SCRIPT}"

  [ "$status" -eq 1 ]
  [[ "$output" == *"[CRITICAL OPEN ISSUE] R5"* ]]
  [[ "$output" == *"both Bitbucket tokens failed"* ]]
}

# =============================================================================
# TEST: MCP health timeout — exit code 2
# =============================================================================

@test "R5.3: exit code 2 when MCP never becomes healthy" {
  _create_mock_docker_compose_unhealthy
  _create_mock_curl_sequence 200
  _create_mock_python3

  run bash "${TEST_SCRIPT}"

  [ "$status" -eq 2 ]
  [[ "$output" == *"did not become healthy"* ]]
}

# =============================================================================
# TEST: Evidence file generation
# =============================================================================

@test "R5.5: evidence file is written on successful token selection" {
  _create_mock_curl_sequence 200 200
  _create_mock_python3

  run bash "${TEST_SCRIPT}"

  [ "$status" -eq 0 ]
  [[ "$output" == *"Evidence written to"* ]] || [[ "$output" == *"Evidence:"* ]]
}

@test "R5.5: evidence file is written even on both_fail" {
  _create_mock_curl_sequence 403 401
  _create_mock_python3

  run bash "${TEST_SCRIPT}"

  [ "$status" -eq 1 ]
  [[ "$output" == *"Evidence written to"* ]] || [[ "$output" == *"Evidence:"* ]]
}
