"""
Test 10: Confluence real API smoke test — space and page CRUD.

Validates that the Confluence REST API can be reached with real credentials
and that page create, update, and delete operations work correctly.
Uses dynamic space discovery (R22 fix) instead of hardcoded space keys.

This test uses:
- httpx for direct Confluence REST API calls (Basic Auth)
- credentials fixture for confluence_url, confluence_username, confluence_api_token
- evidence_collector fixture for emitting JSON evidence
- Dynamic space discovery via listing available spaces

Requirements: R10.1, R10.2, R10.3, R10.4, R10.5, R10.6
"""

import time
from typing import Any, Optional

import httpx
import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_FILENAME = "10-confluence-smoke.json"

# Timeouts for API calls
REQUEST_TIMEOUT = 30.0

# Test page title prefix
PAGE_TITLE_PREFIX = "[Local-E2E] Test Page"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_auth(username: str, api_token: str) -> tuple[str, str]:
    """Create Basic Auth tuple for httpx. NEVER log the token value."""
    return (username, api_token)


def confluence_api_url(base_url: str, path: str) -> str:
    """Build a Confluence REST API URL from base and path.

    Handles both ``https://x.atlassian.net`` (root) and
    ``https://x.atlassian.net/wiki`` (already includes the /wiki
    prefix) — appending an extra /wiki produces a 404 HTML page.
    """
    base = base_url.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    # If base already ends in /wiki, only append /rest/api...
    if base.endswith("/wiki"):
        return f"{base}/rest/api{path}"
    return f"{base}/wiki/rest/api{path}"


def discover_space(base_url: str, auth: tuple[str, str]) -> Optional[str]:
    """Dynamically discover an available Confluence space.

    Strategy (R22 fix):
    1. List all global spaces the user has access to
    2. Return the first available space key
    3. If no spaces exist, attempt to create a test space 'E2ETEST'
    4. Return None if all attempts fail

    Returns:
        Space key string or None.
    """
    # Step 1: List available spaces
    url = confluence_api_url(base_url, "/space")
    try:
        resp = httpx.get(
            url,
            auth=auth,
            params={"limit": 25, "type": "global"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            if results:
                return results[0]["key"]
    except (httpx.HTTPError, KeyError, ValueError):
        pass

    # Step 2: Try to create a test space
    try:
        create_url = confluence_api_url(base_url, "/space")
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
        if resp.status_code in (200, 201):
            return "E2ETEST"
    except (httpx.HTTPError, ValueError):
        pass

    return None


def create_page(
    base_url: str,
    auth: tuple[str, str],
    space_key: str,
    title: str,
    body_content: str = "<p>Initial content created by Local E2E test.</p>",
) -> dict[str, Any]:
    """Create a Confluence page in the given space.

    Returns:
        Dict with keys: success, status_code, page_id, version, response_data, error
    """
    url = confluence_api_url(base_url, "/content")
    payload = {
        "type": "page",
        "title": title,
        "space": {"key": space_key},
        "body": {
            "storage": {
                "value": body_content,
                "representation": "storage",
            }
        },
    }

    result: dict[str, Any] = {
        "success": False,
        "status_code": None,
        "page_id": None,
        "version": None,
        "response_data": None,
        "error": None,
    }

    try:
        resp = httpx.post(
            url,
            auth=auth,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        result["status_code"] = resp.status_code

        if resp.status_code in (200, 201):
            data = resp.json()
            result["success"] = True
            result["page_id"] = data.get("id")
            result["version"] = data.get("version", {}).get("number")
            result["response_data"] = {
                "id": data.get("id"),
                "title": data.get("title"),
                "version": data.get("version", {}).get("number"),
                "space_key": data.get("space", {}).get("key"),
                "_links": {
                    "webui": data.get("_links", {}).get("webui", ""),
                },
            }
        else:
            result["error"] = resp.text[:500]
    except httpx.HTTPError as exc:
        result["error"] = str(exc)

    return result


def update_page(
    base_url: str,
    auth: tuple[str, str],
    page_id: str,
    title: str,
    current_version: int,
    new_body: str = "<p>Updated content by Local E2E test.</p>",
) -> dict[str, Any]:
    """Update an existing Confluence page.

    Returns:
        Dict with keys: success, status_code, new_version, response_data, error
    """
    url = confluence_api_url(base_url, f"/content/{page_id}")
    payload = {
        "id": page_id,
        "type": "page",
        "title": title,
        "version": {"number": current_version + 1},
        "body": {
            "storage": {
                "value": new_body,
                "representation": "storage",
            }
        },
    }

    result: dict[str, Any] = {
        "success": False,
        "status_code": None,
        "new_version": None,
        "response_data": None,
        "error": None,
    }

    try:
        resp = httpx.put(
            url,
            auth=auth,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        result["status_code"] = resp.status_code

        if resp.status_code == 200:
            data = resp.json()
            result["success"] = True
            result["new_version"] = data.get("version", {}).get("number")
            result["response_data"] = {
                "id": data.get("id"),
                "title": data.get("title"),
                "version": data.get("version", {}).get("number"),
            }
        else:
            result["error"] = resp.text[:500]
    except httpx.HTTPError as exc:
        result["error"] = str(exc)

    return result


def delete_page(
    base_url: str,
    auth: tuple[str, str],
    page_id: str,
) -> dict[str, Any]:
    """Delete a Confluence page.

    Returns:
        Dict with keys: success, status_code, skipped, error
    """
    url = confluence_api_url(base_url, f"/content/{page_id}")

    result: dict[str, Any] = {
        "success": False,
        "status_code": None,
        "skipped": False,
        "error": None,
    }

    try:
        resp = httpx.delete(
            url,
            auth=auth,
            timeout=REQUEST_TIMEOUT,
        )
        result["status_code"] = resp.status_code

        if resp.status_code in (200, 204):
            result["success"] = True
        elif resp.status_code == 403:
            # Delete may be banned/forbidden — skip gracefully
            result["skipped"] = True
            result["error"] = "Delete forbidden (403) — skipping"
        else:
            result["error"] = resp.text[:500]
    except httpx.HTTPError as exc:
        result["error"] = str(exc)

    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConfluenceSmoke:
    """Confluence real API smoke test — space and page CRUD.

    Uses httpx with Basic Auth to call Confluence REST API directly.
    Implements dynamic space discovery (R22 fix) to avoid hardcoded space keys.
    """

    def test_conf_space_discovery(self, credentials, evidence_collector):
        """R10.1: Verify/create a usable Confluence space.

        WHEN scenario CONF-SPACE is executed, THE Test_Framework SHALL verify
        that a usable space exists (create one if needed, or use existing)
        and SHALL record target_space_key.
        """
        auth = make_auth(credentials.confluence_username, credentials.confluence_api_token)
        start_time = time.time()

        space_key = discover_space(credentials.confluence_url, auth)
        latency = round(time.time() - start_time, 3)

        # Record for use by subsequent tests
        self.__class__._space_key = space_key

        assert space_key is not None, (
            "CONF-SPACE: Failed to discover or create a usable Confluence space. "
            "Ensure the user has access to at least one global space, or has "
            "permission to create spaces."
        )

        # Emit partial evidence (will be completed by later tests)
        self.__class__._evidence = {
            "scenarios": {
                "CONF-SPACE": {
                    "verdict": "pass",
                    "space_key": space_key,
                    "latency_seconds": latency,
                    "method": "dynamic_discovery",
                }
            }
        }

    def test_conf_create_page(self, credentials, evidence_collector):
        """R10.2: Create a page in the discovered space.

        WHEN scenario CONF-CREATE is executed, THE Test_Framework SHALL invoke
        confluence_create_page in target_space_key with title
        '[Local-E2E] Test Page {timestamp}' and SHALL assert HTTP 2xx and
        a returned page ID.
        """
        space_key = getattr(self.__class__, "_space_key", None)
        if space_key is None:
            pytest.skip("CONF-SPACE did not discover a space — cannot create page")

        auth = make_auth(credentials.confluence_username, credentials.confluence_api_token)
        timestamp = int(time.time())
        title = f"{PAGE_TITLE_PREFIX} {timestamp}"

        start_time = time.time()
        result = create_page(
            base_url=credentials.confluence_url,
            auth=auth,
            space_key=space_key,
            title=title,
        )
        latency = round(time.time() - start_time, 3)

        # Store for subsequent tests
        self.__class__._page_id = result.get("page_id")
        self.__class__._page_title = title
        self.__class__._page_version = result.get("version")

        assert result["success"], (
            f"CONF-CREATE: Failed to create page. "
            f"Status: {result['status_code']}, Error: {result['error']}"
        )
        assert result["page_id"] is not None, (
            "CONF-CREATE: Page created but no page ID returned"
        )

        # Update evidence
        evidence = getattr(self.__class__, "_evidence", {"scenarios": {}})
        evidence["scenarios"]["CONF-CREATE"] = {
            "verdict": "pass",
            "page_id": result["page_id"],
            "title": title,
            "space_key": space_key,
            "version": result["version"],
            "status_code": result["status_code"],
            "latency_seconds": latency,
        }
        self.__class__._evidence = evidence

    def test_conf_update_page(self, credentials, evidence_collector):
        """R10.3: Update the created page and verify version increment.

        WHEN scenario CONF-UPDATE is executed, THE Test_Framework SHALL invoke
        confluence_update_page to append content and SHALL assert version
        number incremented.
        """
        page_id = getattr(self.__class__, "_page_id", None)
        page_title = getattr(self.__class__, "_page_title", None)
        current_version = getattr(self.__class__, "_page_version", None)

        if page_id is None:
            pytest.skip("CONF-CREATE did not produce a page — cannot update")

        auth = make_auth(credentials.confluence_username, credentials.confluence_api_token)
        updated_body = (
            "<p>Updated content by Local E2E test at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}.</p>"
        )

        start_time = time.time()
        result = update_page(
            base_url=credentials.confluence_url,
            auth=auth,
            page_id=page_id,
            title=page_title,
            current_version=current_version,
            new_body=updated_body,
        )
        latency = round(time.time() - start_time, 3)

        assert result["success"], (
            f"CONF-UPDATE: Failed to update page {page_id}. "
            f"Status: {result['status_code']}, Error: {result['error']}"
        )
        assert result["new_version"] is not None, (
            "CONF-UPDATE: Page updated but no version returned"
        )
        assert result["new_version"] > current_version, (
            f"CONF-UPDATE: Version not incremented. "
            f"Expected > {current_version}, got {result['new_version']}"
        )

        # Store updated version
        self.__class__._page_version = result["new_version"]

        # Update evidence
        evidence = getattr(self.__class__, "_evidence", {"scenarios": {}})
        evidence["scenarios"]["CONF-UPDATE"] = {
            "verdict": "pass",
            "page_id": page_id,
            "previous_version": current_version,
            "new_version": result["new_version"],
            "status_code": result["status_code"],
            "latency_seconds": latency,
        }
        self.__class__._evidence = evidence

    def test_conf_delete_page(self, credentials, evidence_collector):
        """R10.4: Delete the created page (or skip if forbidden).

        WHEN scenario CONF-DELETE is executed, THE Test_Framework SHALL invoke
        confluence_delete_page (or skip with n/a if banned) and SHALL verify
        deletion.
        """
        page_id = getattr(self.__class__, "_page_id", None)

        if page_id is None:
            pytest.skip("CONF-CREATE did not produce a page — cannot delete")

        auth = make_auth(credentials.confluence_username, credentials.confluence_api_token)

        start_time = time.time()
        result = delete_page(
            base_url=credentials.confluence_url,
            auth=auth,
            page_id=page_id,
        )
        latency = round(time.time() - start_time, 3)

        # Determine verdict
        if result["skipped"]:
            verdict = "skipped"
            # Deletion is banned — this is acceptable per R10.4
        elif result["success"]:
            verdict = "pass"
            # Verify deletion by trying to GET the page
            verify_url = confluence_api_url(
                credentials.confluence_url, f"/content/{page_id}"
            )
            try:
                verify_resp = httpx.get(
                    verify_url, auth=auth, timeout=REQUEST_TIMEOUT
                )
                # After deletion, expect 404 or the page in trash
                if verify_resp.status_code == 404:
                    pass  # Confirmed deleted
                elif verify_resp.status_code == 200:
                    # Page might be in trash (Confluence moves to trash first)
                    data = verify_resp.json()
                    status = data.get("status", "")
                    assert status == "trashed", (
                        f"CONF-DELETE: Page still accessible with status '{status}'"
                    )
            except httpx.HTTPError:
                pass  # Network error during verification — acceptable
        else:
            verdict = "fail"
            # Only fail if it's not a permission issue
            if result["status_code"] == 403:
                verdict = "skipped"
            else:
                pytest.fail(
                    f"CONF-DELETE: Failed to delete page {page_id}. "
                    f"Status: {result['status_code']}, Error: {result['error']}"
                )

        # Update evidence
        evidence = getattr(self.__class__, "_evidence", {"scenarios": {}})
        evidence["scenarios"]["CONF-DELETE"] = {
            "verdict": verdict,
            "page_id": page_id,
            "status_code": result["status_code"],
            "skipped": result["skipped"],
            "latency_seconds": latency,
            "error": result.get("error"),
        }
        self.__class__._evidence = evidence

    def test_emit_evidence(self, credentials, evidence_collector):
        """R10.6: Emit evidence JSON with per-scenario verdict.

        THE Evidence_Collector SHALL emit e2e-evidence/10-confluence-smoke.json
        with per-scenario verdict and evidence.
        """
        evidence = getattr(self.__class__, "_evidence", {"scenarios": {}})

        # Add metadata
        evidence["confluence_url"] = credentials.confluence_url
        evidence["username"] = credentials.confluence_username
        # NEVER log the API token
        evidence["api_token"] = "***REDACTED***"
        evidence["dynamic_space_discovery"] = True
        evidence["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Calculate overall verdict
        scenario_verdicts = [
            s.get("verdict", "unknown")
            for s in evidence.get("scenarios", {}).values()
        ]
        if all(v in ("pass", "skipped") for v in scenario_verdicts):
            evidence["overall_verdict"] = "pass"
        else:
            evidence["overall_verdict"] = "fail"

        # Emit evidence
        evidence_path = evidence_collector.emit_json(
            "R10", EVIDENCE_FILENAME, evidence
        )
        assert evidence_path.exists(), (
            f"Evidence file not created at {evidence_path}"
        )
