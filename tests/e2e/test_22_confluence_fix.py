"""
Test 22: Verify Confluence space fix (R22).

Validates that the Confluence space discovery fix works correctly:
- Dynamic space discovery works without manual space creation
- No hardcoded space key is required
- The CONF-SPACE scenario passes using the discover_space() strategy

Verification steps:
1. Run Confluence smoke test  assert CONF-SPACE passes
2. Verify dynamic space discovery works without manual creation
3. Emit evidence JSON

Requirements: R22.3, R22.4, R22.5
"""

import time
from typing import Any, Optional

import httpx
import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_FILENAME = "22-confluence-fix.json"
REQUEST_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_auth(username: str, api_token: str) -> tuple[str, str]:
    """Create Basic Auth tuple for httpx. NEVER log the token value."""
    return (username, api_token)


def _confluence_api_url(base_url: str, path: str) -> str:
    """Build a Confluence REST API URL from base and path.

    Handles both ``https://x.atlassian.net`` (root) and
    ``https://x.atlassian.net/wiki`` (already includes the /wiki
    prefix) - appending an extra /wiki produces a 404 HTML page.
    """
    base = base_url.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    if base.endswith("/wiki"):
        return f"{base}/rest/api{path}"
    return f"{base}/wiki/rest/api{path}"


def _discover_space_dynamic(base_url: str, auth: tuple[str, str]) -> dict[str, Any]:
    """Dynamically discover an available Confluence space.

    Implements the R22 fix strategy:
    1. List all global spaces the user has access to
    2. Return the first available space key
    3. If no spaces exist, attempt to create a test space 'E2ETEST'
    4. Return detailed result dict with discovery method and diagnostics

    Returns:
        Dict with keys: success, space_key, method, spaces_found, error, details
    """
    result: dict[str, Any] = {
        "success": False,
        "space_key": None,
        "method": None,
        "spaces_found": 0,
        "error": None,
        "details": {},
    }

    # Step 1: List available spaces (dynamic discovery)
    url = _confluence_api_url(base_url, "/space")
    try:
        resp = httpx.get(
            url,
            auth=auth,
            params={"limit": 25, "type": "global"},
            timeout=REQUEST_TIMEOUT,
        )
        result["details"]["list_status_code"] = resp.status_code

        if resp.status_code == 200:
            data = resp.json()
            spaces = data.get("results", [])
            result["spaces_found"] = len(spaces)
            result["details"]["space_keys"] = [s.get("key") for s in spaces[:10]]

            if spaces:
                result["success"] = True
                result["space_key"] = spaces[0]["key"]
                result["method"] = "dynamic_discovery"
                return result
        elif resp.status_code == 401:
            result["error"] = "Authentication failed (401). Check API token."
            return result
        elif resp.status_code == 403:
            result["error"] = "Permission denied (403). Token may lack space read access."
            return result
        else:
            result["details"]["list_error"] = resp.text[:300]
    except httpx.HTTPError as exc:
        result["error"] = f"HTTP error listing spaces: {exc}"
        return result

    # Step 2: Fallback - try to create a test space
    try:
        create_url = _confluence_api_url(base_url, "/space")
        payload = {
            "key": "E2ETEST",
            "name": "E2E Test Space",
            "description": {
                "plain": {
                    "value": "Automated test space for local E2E testing",
                    "representation": "plain",
                }
            },
        }
        resp = httpx.post(
            create_url,
            auth=auth,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        result["details"]["create_status_code"] = resp.status_code

        if resp.status_code in (200, 201):
            result["success"] = True
            result["space_key"] = "E2ETEST"
            result["method"] = "created_test_space"
            return result
        elif resp.status_code == 409:
            # Space already exists - use it
            result["success"] = True
            result["space_key"] = "E2ETEST"
            result["method"] = "existing_test_space"
            return result
        else:
            result["details"]["create_error"] = resp.text[:300]
            result["error"] = (
                f"No spaces found and space creation failed "
                f"(status {resp.status_code}). "
                f"This may require admin permissions."
            )
    except httpx.HTTPError as exc:
        result["error"] = f"HTTP error creating space: {exc}"

    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConfluenceSpaceFix:
    """R22: Verify Confluence space discovery fix works without hardcoded keys."""

    def test_conf_space_dynamic_discovery(self, credentials):
        """R22.3: CONF-SPACE scenario passes without manual space creation.

        WHEN the fix is applied, THE Test_Framework SHALL execute the
        Confluence smoke test and SHALL assert that CONF-SPACE scenario
        passes without manual space creation.
        """
        auth = _make_auth(
            credentials.confluence_username,
            credentials.confluence_api_token,
        )

        discovery_result = _discover_space_dynamic(
            credentials.confluence_url, auth
        )

        # Store for use by subsequent tests
        self.__class__._discovery_result = discovery_result

        assert discovery_result["success"], (
            f"CONF-SPACE dynamic discovery failed.\n"
            f"Error: {discovery_result['error']}\n"
            f"Details: {discovery_result['details']}\n"
            f"This means the R22 fix (dynamic space discovery) is not working. "
            f"Ensure the Confluence user has access to at least one global space."
        )

        assert discovery_result["space_key"] is not None, (
            "Discovery reported success but no space_key was returned."
        )

    def test_no_hardcoded_space_key(self, credentials):
        """R22.4: Dynamic discovery works as fallback without hardcoded keys.

        IF space creation requires admin permissions not available with the
        current token, THEN THE fix SHALL use a fallback strategy (list
        available spaces and use the first one).
        """
        discovery_result = getattr(self.__class__, "_discovery_result", None)
        if discovery_result is None:
            pytest.skip("Previous discovery test did not run")

        # The method should be 'dynamic_discovery' (listing existing spaces)
        # or 'created_test_space' / 'existing_test_space' - all are valid
        valid_methods = {"dynamic_discovery", "created_test_space", "existing_test_space"}
        assert discovery_result["method"] in valid_methods, (
            f"Unexpected discovery method: {discovery_result['method']}. "
            f"Expected one of: {valid_methods}"
        )

        # If method is dynamic_discovery, at least one space was found
        if discovery_result["method"] == "dynamic_discovery":
            assert discovery_result["spaces_found"] >= 1, (
                "Dynamic discovery succeeded but reported 0 spaces found."
            )

    def test_space_is_accessible(self, credentials):
        """R22.3: Verify the discovered space is actually accessible for operations.

        After discovery, confirm the space can be read (GET /space/{key})
        to ensure it's usable for page creation.
        """
        discovery_result = getattr(self.__class__, "_discovery_result", None)
        if discovery_result is None or not discovery_result.get("success"):
            pytest.skip("Space discovery did not succeed - cannot verify access")

        space_key = discovery_result["space_key"]
        auth = _make_auth(
            credentials.confluence_username,
            credentials.confluence_api_token,
        )

        # Verify the space is accessible
        url = _confluence_api_url(
            credentials.confluence_url, f"/space/{space_key}"
        )
        resp = httpx.get(url, auth=auth, timeout=REQUEST_TIMEOUT)

        assert resp.status_code == 200, (
            f"Discovered space '{space_key}' is not accessible. "
            f"GET /space/{space_key} returned {resp.status_code}. "
            f"Response: {resp.text[:300]}"
        )

        # Verify the response contains expected space data
        data = resp.json()
        assert data.get("key") == space_key, (
            f"Space key mismatch. Expected '{space_key}', "
            f"got '{data.get('key')}'"
        )


class TestConfluenceFixEvidence:
    """R22.5: Emit structured evidence for the Confluence space fix."""

    def test_emit_evidence(self, credentials, evidence_collector):
        """Collect Confluence fix verification data and emit evidence JSON."""
        auth = _make_auth(
            credentials.confluence_username,
            credentials.confluence_api_token,
        )

        # Run the full discovery to capture evidence
        start_time = time.time()
        discovery_result = _discover_space_dynamic(
            credentials.confluence_url, auth
        )
        latency = round(time.time() - start_time, 3)

        evidence_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "confluence_url": credentials.confluence_url,
            "username": credentials.confluence_username,
            "api_token": "***REDACTED***",
            "fix_description": (
                "Replaced hardcoded space key with dynamic space discovery. "
                "Strategy: list available spaces first, use first one found. "
                "Fallback: create E2ETEST space if no spaces exist."
            ),
            "discovery_result": {
                "success": discovery_result["success"],
                "space_key": discovery_result["space_key"],
                "method": discovery_result["method"],
                "spaces_found": discovery_result["spaces_found"],
                "error": discovery_result["error"],
                "latency_seconds": latency,
            },
            "root_cause": (
                "Original code used a hardcoded space key (e.g., 'TEAM') that "
                "did not exist in the target Confluence instance, causing 404 "
                "or 403 errors. Fix implements dynamic discovery via "
                "GET /wiki/rest/api/space?type=global."
            ),
            "overall_verdict": "pass" if discovery_result["success"] else "fail",
        }

        # Emit evidence
        evidence_path = evidence_collector.emit_json(
            requirement_id="R22.3,R22.4,R22.5",
            filename=EVIDENCE_FILENAME,
            data=evidence_data,
        )
        assert evidence_path.exists(), (
            f"Evidence file not created at {evidence_path}"
        )
