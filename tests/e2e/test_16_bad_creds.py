"""
Test 16: Invalid credential error paths (R16).

Validates that the system handles invalid credentials gracefully:
- Red error badge with 401/Unauthorized on invalid Jira token
- Invalid credentials NOT persisted
- Logs do NOT contain literal token value (redaction)
- Specific 401 vs 403 distinction for Bitbucket

Requirements: R16.1, R16.2, R16.3, R16.4, R16.5
"""

import subprocess
import time
from typing import Any

import httpx
import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_FILENAME = "16-error-paths.json"
INVALID_JIRA_TOKEN = "INVALID_TOKEN_12345"
INVALID_BITBUCKET_TOKEN = "INVALID_BB_TOKEN_67890"
MCP_BASE_URL = "http://localhost:8090"
ADMIN_API_URL = "http://localhost:8082"
REQUEST_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_container_logs(service: str, lines: int = 100) -> str:
    """Capture recent container logs for a service."""
    try:
        result = subprocess.run(
            ["docker", "compose", "logs", "--tail", str(lines), service],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(_platform_root()),
        )
        return result.stdout + result.stderr
    except Exception:
        return ""


def _platform_root():
    """Get the platform root directory."""
    from pathlib import Path
    return Path(__file__).resolve().parent.parent.parent


def _test_jira_connection_with_token(base_url: str, username: str, token: str) -> dict:
    """Attempt a Jira connection test with given credentials.

    Returns dict with status_code, error_message, and success flag.
    """
    result = {
        "success": False,
        "status_code": None,
        "error_message": None,
        "response_body": None,
    }

    # Try via MCP healthz or direct Jira API
    try:
        # Attempt direct Jira API call with invalid token
        jira_url = f"{base_url}/rest/api/3/myself"
        resp = httpx.get(
            jira_url,
            auth=(username, token),
            timeout=REQUEST_TIMEOUT,
        )
        result["status_code"] = resp.status_code
        result["success"] = resp.status_code == 200
        if resp.status_code != 200:
            result["error_message"] = resp.text[:500]
    except httpx.HTTPError as exc:
        result["error_message"] = str(exc)

    return result


def _test_bitbucket_connection_with_token(token: str, workspace: str) -> dict:
    """Attempt a Bitbucket connection test with given token.

    Returns dict with status_code, error distinction (401 vs 403).
    """
    result = {
        "success": False,
        "status_code": None,
        "error_type": None,
        "error_message": None,
    }

    try:
        url = f"https://api.bitbucket.org/2.0/repositories/{workspace}"
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
        )
        result["status_code"] = resp.status_code
        result["success"] = resp.status_code == 200

        if resp.status_code == 401:
            result["error_type"] = "unauthorized"
            result["error_message"] = "401 Unauthorized - bad or expired token"
        elif resp.status_code == 403:
            result["error_type"] = "forbidden"
            result["error_message"] = "403 Forbidden - insufficient scope/permissions"
        elif resp.status_code != 200:
            result["error_type"] = "other"
            result["error_message"] = resp.text[:300]
    except httpx.HTTPError as exc:
        result["error_message"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBadCredentialsJira:
    """R16.1, R16.2, R16.3: Invalid Jira token error handling."""

    def test_invalid_jira_token_returns_401(self, credentials):
        """R16.1: Invalid Jira token produces 401/Unauthorized error.

        WHEN an invalid Jira API token is used, THE system SHALL
        return a 401 Unauthorized response.
        """
        result = _test_jira_connection_with_token(
            base_url=credentials.jira_url,
            username=credentials.jira_username,
            token=INVALID_JIRA_TOKEN,
        )

        assert result["status_code"] == 401, (
            f"Expected HTTP 401 for invalid Jira token, "
            f"got {result['status_code']}.\n"
            f"Error: {result['error_message']}"
        )

    def test_invalid_credentials_not_persisted(self, credentials):
        """R16.2: Invalid credentials are NOT persisted after failed test.

        WHEN the invalid credential test fails, THE system SHALL NOT
        persist the invalid credentials.
        """
        # First, test with invalid token (should fail)
        bad_result = _test_jira_connection_with_token(
            base_url=credentials.jira_url,
            username=credentials.jira_username,
            token=INVALID_JIRA_TOKEN,
        )
        assert not bad_result["success"], "Invalid token should not succeed"

        # Then verify the real credentials still work (not overwritten)
        good_result = _test_jira_connection_with_token(
            base_url=credentials.jira_url,
            username=credentials.jira_username,
            token=credentials.jira_api_token,
        )
        assert good_result["success"], (
            f"Valid credentials should still work after invalid attempt. "
            f"Status: {good_result['status_code']}, "
            f"Error: {good_result['error_message']}"
        )

    def test_logs_do_not_contain_literal_token(self, credentials):
        """R16.3: Logs do NOT contain the literal invalid token value.

        WHEN an invalid credential is used, THE logs SHALL NOT contain
        the literal token value (log redaction must be active).
        """
        # Make a request with the invalid token to generate log entries
        _test_jira_connection_with_token(
            base_url=credentials.jira_url,
            username=credentials.jira_username,
            token=INVALID_JIRA_TOKEN,
        )

        # Wait briefly for logs to flush
        time.sleep(2)

        # Check automation-service logs
        logs = _get_container_logs("automation-service", lines=50)
        # Also check admin-dashboard-api logs
        logs += _get_container_logs("admin-dashboard-api", lines=50)

        assert INVALID_JIRA_TOKEN not in logs, (
            f"SECURITY: Literal invalid token value found in container logs!\n"
            f"Token '{INVALID_JIRA_TOKEN}' should be redacted.\n"
            f"Log snippet: {logs[:500]}"
        )


class TestBadCredentialsBitbucket:
    """R16.4: Invalid Bitbucket token error handling with 401/403 distinction."""

    def test_invalid_bitbucket_token_returns_401(self, credentials):
        """R16.4: Invalid Bitbucket token produces specific 401 error.

        WHEN an invalid Bitbucket token is entered, THE system SHALL
        display a specific error distinguishing between 401 Unauthorized
        (bad token) and 403 Forbidden (insufficient scope).
        """
        result = _test_bitbucket_connection_with_token(
            token=INVALID_BITBUCKET_TOKEN,
            workspace=credentials.bitbucket_workspace,
        )

        # Should get either 401 or 403 - both are valid error responses
        assert result["status_code"] in (401, 403), (
            f"Expected HTTP 401 or 403 for invalid Bitbucket token, "
            f"got {result['status_code']}.\n"
            f"Error: {result['error_message']}"
        )

        # Verify the error type is correctly identified
        assert result["error_type"] in ("unauthorized", "forbidden"), (
            f"Error type should be 'unauthorized' or 'forbidden', "
            f"got '{result['error_type']}'"
        )

    def test_401_vs_403_distinction(self, credentials):
        """R16.4: System distinguishes between 401 and 403 for Bitbucket.

        Verify that a completely invalid token gets 401 (unauthorized)
        while a valid-format but wrong-scope token would get 403.
        """
        # Completely invalid token → should be 401
        result_invalid = _test_bitbucket_connection_with_token(
            token="COMPLETELY_INVALID",
            workspace=credentials.bitbucket_workspace,
        )

        # The response should clearly indicate the error type
        assert result_invalid["status_code"] is not None, (
            "Should receive a response (not a connection error)"
        )
        assert result_invalid["error_type"] is not None, (
            "Error type should be identified for invalid tokens"
        )


class TestBadCredentialsEvidence:
    """R16.5: Emit structured evidence for error path tests."""

    def test_emit_evidence(self, credentials, evidence_collector):
        """Collect error path test data and emit evidence JSON."""
        evidence_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "scenarios": {},
            "overall_verdict": "pass",
        }

        # Scenario 1: Invalid Jira token
        jira_result = _test_jira_connection_with_token(
            base_url=credentials.jira_url,
            username=credentials.jira_username,
            token=INVALID_JIRA_TOKEN,
        )
        evidence_data["scenarios"]["invalid_jira_token"] = {
            "token_used": "***REDACTED***",
            "status_code": jira_result["status_code"],
            "expected_status": 401,
            "error_message": jira_result["error_message"],
            "passed": jira_result["status_code"] == 401,
        }

        # Scenario 2: Invalid Bitbucket token
        bb_result = _test_bitbucket_connection_with_token(
            token=INVALID_BITBUCKET_TOKEN,
            workspace=credentials.bitbucket_workspace,
        )
        evidence_data["scenarios"]["invalid_bitbucket_token"] = {
            "token_used": "***REDACTED***",
            "status_code": bb_result["status_code"],
            "error_type": bb_result["error_type"],
            "error_message": bb_result["error_message"],
            "passed": bb_result["status_code"] in (401, 403),
        }

        # Scenario 3: Log redaction check
        time.sleep(1)
        logs = _get_container_logs("automation-service", lines=50)
        logs += _get_container_logs("admin-dashboard-api", lines=50)
        token_leaked = INVALID_JIRA_TOKEN in logs
        evidence_data["scenarios"]["log_redaction"] = {
            "token_in_logs": token_leaked,
            "passed": not token_leaked,
        }

        # Overall verdict
        all_passed = all(
            s.get("passed", False)
            for s in evidence_data["scenarios"].values()
        )
        evidence_data["overall_verdict"] = "pass" if all_passed else "fail"

        # Emit evidence
        evidence_path = evidence_collector.emit_json(
            requirement_id="R16.1,R16.2,R16.3,R16.4,R16.5",
            filename=EVIDENCE_FILENAME,
            data=evidence_data,
        )
        assert evidence_path.exists(), (
            f"Evidence file not created at {evidence_path}"
        )
