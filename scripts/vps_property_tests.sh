#!/usr/bin/env bash
# =============================================================================
# platform/scripts/vps_property_tests.sh — Property test runner (R21)
# =============================================================================
# Runs on VPS_Host at /opt/atlassian-ai-workflow-platform.
# Executes the four property tests in a single pytest invocation and produces
# evidence at /tmp/21-property-tests.txt.
#
# Implements:
# R21.1 — Single pytest invocation with all four test files
# R21.2 — Exit code 0 and "failed: 0" assertion
# R21.3 — Per-failure Open_Issue (category=code, severity=major) with
# Hypothesis falsifying example
# R21.4 — Missing test files detected via pytest --collect-only; verdict=n/a
# + Open_Issue (category=code, recommended_action=code_change_required)
# R21.5 — Sub-table verdicts emitted for TEST_REPORT.md integration
#
# Usage:
# ./vps_property_tests.sh
#
# Exit codes:
# 0 = all tests pass (or missing tests handled gracefully)
# 1 = one or more test failures detected (Open_Issues logged)
# =============================================================================
set -uo pipefail
# NOTE: We do NOT use `set -e` because pytest exit code 1 (test failures) is
# handled gracefully — it is not a script error but a test result.

PLATFORM_DIR="/opt/atlassian-ai-workflow-platform"
EVIDENCE_FILE="/tmp/21-property-tests.txt"
SCRIPTS_DIR="$PLATFORM_DIR/scripts"
TESTS_DIR="$PLATFORM_DIR/tests/property"

# The four property test files required by R21
TEST_FILES=(
    "tests/property/test_env_coverage.py"
    "tests/property/test_sensitive_key_parity.py"
    "tests/property/test_compose_healthcheck_shape.py"
    "tests/property/test_log_redaction.py"
)

# --- Initialize evidence file -------------------------------------------------
: > "$EVIDENCE_FILE"
echo "=== VPS Property Tests (R21) ===" >> "$EVIDENCE_FILE"
echo "Timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$EVIDENCE_FILE"
echo "Working directory: $PLATFORM_DIR" >> "$EVIDENCE_FILE"
echo "" >> "$EVIDENCE_FILE"

cd "$PLATFORM_DIR"

# --- R21.4: Detect missing test files -----------------------------------------
echo "[R21.4] Checking test file availability..."
echo "--- Test File Availability ---" >> "$EVIDENCE_FILE"

EXISTING_FILES=()
MISSING_FILES=()

for TEST_FILE in "${TEST_FILES[@]}"; do
    if [ -f "$PLATFORM_DIR/$TEST_FILE" ]; then
        echo "  PRESENT: $TEST_FILE"
        echo "PRESENT: $TEST_FILE" >> "$EVIDENCE_FILE"
        EXISTING_FILES+=("$TEST_FILE")
    else
        echo "  MISSING: $TEST_FILE"
        echo "MISSING: $TEST_FILE" >> "$EVIDENCE_FILE"
        MISSING_FILES+=("$TEST_FILE")
    fi
done
echo "" >> "$EVIDENCE_FILE"

# Log Open_Issues for missing test files (R21.4)
for MISSING in "${MISSING_FILES[@]}"; do
    TEST_NAME=$(basename "$MISSING" .py)
    echo "[R21.4] Missing test file: $MISSING — logging Open_Issue (verdict=n/a)"
    python3 "$SCRIPTS_DIR/vps_open_issue_logger.py" \
        --requirement R21 \
        --scenario "$TEST_NAME" \
        --severity major \
        --category code \
        --summary "Property test file missing: $MISSING — must be authored" \
        --evidence-path "vps-test-evidence/21-property-tests.txt" \
        --recommended-action code_change_required \
        2>&1 || true
done

# --- R21.1: Run pytest on existing test files ---------------------------------
OVERALL_EXIT=0

if [ ${#EXISTING_FILES[@]} -eq 0 ]; then
    echo "[R21.1] No property test files found in repository. All verdicts = n/a."
    echo "--- pytest invocation ---" >> "$EVIDENCE_FILE"
    echo "SKIPPED: No test files available to run." >> "$EVIDENCE_FILE"
    echo "" >> "$EVIDENCE_FILE"
    echo "OVERALL_VERDICT=n/a" >> "$EVIDENCE_FILE"
else
    echo "[R21.1] Running pytest on ${#EXISTING_FILES[@]} test file(s)..."
    echo "--- pytest invocation ---" >> "$EVIDENCE_FILE"
    echo "Command: pytest ${EXISTING_FILES[*]} -v --tb=short --hypothesis-show-statistics" >> "$EVIDENCE_FILE"
    echo "" >> "$EVIDENCE_FILE"

    # Run pytest; capture exit code without aborting script
    PYTEST_EXIT=0
    pytest "${EXISTING_FILES[@]}" -v --tb=short --hypothesis-show-statistics 2>&1 | tee -a "$EVIDENCE_FILE" || PYTEST_EXIT=$?

    echo "" >> "$EVIDENCE_FILE"
    echo "pytest_exit_code=$PYTEST_EXIT" >> "$EVIDENCE_FILE"
    echo "" >> "$EVIDENCE_FILE"

    # --- R21.2: Assert exit code 0 and "failed: 0" ----------------------------
    if [ $PYTEST_EXIT -eq 0 ]; then
        echo "[R21.2] pytest exit code 0 — all tests passed."
        echo "PYTEST_RESULT=PASS" >> "$EVIDENCE_FILE"
    else
        echo "[R21.2] pytest exit code $PYTEST_EXIT — test failures detected."
        echo "PYTEST_RESULT=FAIL (exit code $PYTEST_EXIT)" >> "$EVIDENCE_FILE"
        OVERALL_EXIT=1

        # --- R21.3: Extract failures and log Open_Issues ----------------------
        echo "[R21.3] Extracting failure details for Open_Issues..."
        echo "" >> "$EVIDENCE_FILE"
        echo "--- Failure Analysis ---" >> "$EVIDENCE_FILE"

        # Parse the pytest output for FAILED lines
        FAILED_TESTS=$(grep -E "^FAILED " "$EVIDENCE_FILE" 2>/dev/null || true)

        if [ -n "$FAILED_TESTS" ]; then
            while IFS= read -r FAILED_LINE; do
                # Extract test name from "FAILED tests/property/test_foo.py::test_bar - ..."
                FAILED_TEST_ID=$(echo "$FAILED_LINE" | sed 's/^FAILED //' | cut -d' ' -f1)
                FAILED_FILE=$(echo "$FAILED_TEST_ID" | cut -d':' -f1)
                FAILED_NAME=$(basename "$FAILED_FILE" .py)

                # Try to extract Hypothesis falsifying example
                FALSIFYING=""
                if grep -q "Falsifying example:" "$EVIDENCE_FILE" 2>/dev/null; then
                    # Get the falsifying example block (next line after "Falsifying example:")
                    FALSIFYING=$(grep -A2 "Falsifying example:" "$EVIDENCE_FILE" | head -3 | tr '\n' ' ' | cut -c1-120)
                fi

                SUMMARY="Property test failed: $FAILED_TEST_ID"
                if [ -n "$FALSIFYING" ]; then
                    # Truncate to fit within 160 chars
                    SUMMARY="$FAILED_TEST_ID — $FALSIFYING"
                    SUMMARY="${SUMMARY:0:160}"
                fi

                echo "  Failed: $FAILED_TEST_ID"
                echo "FAILED_TEST: $FAILED_TEST_ID" >> "$EVIDENCE_FILE"
                [ -n "$FALSIFYING" ] && echo "FALSIFYING_EXAMPLE: $FALSIFYING" >> "$EVIDENCE_FILE"

                # Log Open_Issue (R21.3: category=code, severity=major)
                python3 "$SCRIPTS_DIR/vps_open_issue_logger.py" \
                    --requirement R21 \
                    --scenario "$FAILED_NAME" \
                    --severity major \
                    --category code \
                    --summary "$SUMMARY" \
                    --evidence-path "vps-test-evidence/21-property-tests.txt" \
                    --recommended-action code_change_required \
                    2>&1 || true

            done <<< "$FAILED_TESTS"
        else
            # No explicit FAILED lines but non-zero exit — log generic issue
            echo "  No FAILED lines parsed, but pytest exited with code $PYTEST_EXIT"
            echo "GENERIC_FAILURE: pytest exit code $PYTEST_EXIT" >> "$EVIDENCE_FILE"

            python3 "$SCRIPTS_DIR/vps_open_issue_logger.py" \
                --requirement R21 \
                --severity major \
                --category code \
                --summary "Property tests failed with exit code $PYTEST_EXIT (no specific FAILED lines parsed)" \
                --evidence-path "vps-test-evidence/21-property-tests.txt" \
                --recommended-action code_change_required \
                2>&1 || true
        fi
    fi

    # Additional check: verify "failed" count in summary line
    FAILED_COUNT=$(grep -oP '\d+(?= failed)' "$EVIDENCE_FILE" | tail -1 || echo "")
    if [ -n "$FAILED_COUNT" ] && [ "$FAILED_COUNT" -gt 0 ]; then
        echo "failed_count=$FAILED_COUNT" >> "$EVIDENCE_FILE"
        if [ $OVERALL_EXIT -eq 0 ]; then
            # Edge case: exit code was 0 but summary shows failures
            echo "[WARN] pytest exit 0 but summary reports $FAILED_COUNT failed"
            OVERALL_EXIT=1
        fi
    fi
fi

# --- Summary ------------------------------------------------------------------
echo "" >> "$EVIDENCE_FILE"
echo "--- Summary ---" >> "$EVIDENCE_FILE"
echo "total_test_files=${#TEST_FILES[@]}" >> "$EVIDENCE_FILE"
echo "existing_test_files=${#EXISTING_FILES[@]}" >> "$EVIDENCE_FILE"
echo "missing_test_files=${#MISSING_FILES[@]}" >> "$EVIDENCE_FILE"
echo "overall_exit_code=$OVERALL_EXIT" >> "$EVIDENCE_FILE"

if [ ${#MISSING_FILES[@]} -gt 0 ]; then
    echo "missing_files=${MISSING_FILES[*]}" >> "$EVIDENCE_FILE"
fi

if [ $OVERALL_EXIT -eq 0 ] && [ ${#MISSING_FILES[@]} -eq 0 ]; then
    echo "OVERALL_VERDICT=PASS" >> "$EVIDENCE_FILE"
elif [ $OVERALL_EXIT -eq 0 ] && [ ${#MISSING_FILES[@]} -gt 0 ]; then
    echo "OVERALL_VERDICT=PARTIAL (missing tests  n/a)" >> "$EVIDENCE_FILE"
else
    echo "OVERALL_VERDICT=FAIL" >> "$EVIDENCE_FILE"
fi

echo "completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$EVIDENCE_FILE"

echo ""
echo "=== Property tests complete ==="
echo "Evidence written to: $EVIDENCE_FILE"
echo "  Existing tests: ${#EXISTING_FILES[@]}/${#TEST_FILES[@]}"
echo "  Missing tests:  ${#MISSING_FILES[@]}/${#TEST_FILES[@]}"
[ ${#MISSING_FILES[@]} -gt 0 ] && echo "  Missing: ${MISSING_FILES[*]}"
echo "  Overall exit:   $OVERALL_EXIT"

exit $OVERALL_EXIT
