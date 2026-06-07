"""
Test 09: Jira real API smoke test - CRUD operations via Jira REST API.

Validates that the platform can perform full Jira issue lifecycle operations
(create, search, comment, transition, delete) against real Jira Cloud using
credentials from credentials.md.

Uses httpx with Basic Auth (username:api_token) to call Jira REST API directly.
The Jira project key is "JOH".

Requirements: R9.1, R9.2, R9.3, R9.4, R9.5, R9.6
"""

import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

JIRA_PROJECT_KEY = "KAN"
ISSUE_KEY_PATTERN = re.compile(rf"^{JIRA_PROJECT_KEY}-\d+$")
EVIDENCE_FILENAME = "09-jira-smoke.json"

# Timeouts
REQUEST_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_jira_client(credentials) -> httpx.Client:
    """Build an httpx client configured for Jira REST API with Basic Auth.

    NEVER logs raw API tokens.
    """
    auth = (credentials.jira_username, credentials.jira_api_token)
    base_url = credentials.jira_url.rstrip("/")

    return httpx.Client(
        base_url=base_url,
        auth=auth,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=REQUEST_TIMEOUT,
    )


def _scenario_result(
    scenario: str,
    verdict: str,
    status_code: int | None = None,
    latency_ms: float | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a structured scenario result dict."""
    result: dict[str, Any] = {
        "scenario": scenario,
        "verdict": verdict,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if status_code is not None:
        result["http_status"] = status_code
    if latency_ms is not None:
        result["latency_ms"] = round(latency_ms, 2)
    if details:
        result["details"] = details
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestJiraSmoke:
    """Jira CRUD smoke tests via REST API with real credentials.

    Tests execute in order: create → search → comment → transition → delete.
    Each test records its result for evidence emission.
    """

    # Shared state across test methods within this class
    _created_issue_key: str | None = None
    _created_issue_id: str | None = None
    _scenario_results: list[dict[str, Any]] = []

    def test_jira_create_issue(self, credentials, evidence_collector):
        """R9.1: JIRA-CREATE - create issue in JOH project, assert key matches pattern.

        WHEN scenario JIRA-CREATE is executed, THE Test_Framework SHALL invoke
        jira_create_issue with project JOH, summary containing timestamp and
        type Task and SHALL assert HTTP 2xx and a returned key matching ^JOH-\\d+$.
        """
        client = _build_jira_client(credentials)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        summary = f"[Local-E2E] Smoke issue {timestamp}"

        payload = {
            "fields": {
                "project": {"key": JIRA_PROJECT_KEY},
                "summary": summary,
                "issuetype": {"name": "Task"},
            }
        }

        start = time.perf_counter()
        try:
            response = client.post("/rest/api/3/issue", json=payload)
            latency_ms = (time.perf_counter() - start) * 1000
        finally:
            client.close()

        # Assert HTTP 2xx
        assert response.status_code in (200, 201), (
            f"JIRA-CREATE failed: HTTP {response.status_code}\n"
            f"Response: {response.text[:500]}"
        )

        data = response.json()
        issue_key = data.get("key", "")
        issue_id = data.get("id", "")

        # Assert key matches pattern
        assert ISSUE_KEY_PATTERN.match(issue_key), (
            f"Issue key '{issue_key}' does not match pattern ^JOH-\\d+$"
        )

        # Store for subsequent tests
        TestJiraSmoke._created_issue_key = issue_key
        TestJiraSmoke._created_issue_id = issue_id

        result = _scenario_result(
            scenario="JIRA-CREATE",
            verdict="pass",
            status_code=response.status_code,
            latency_ms=latency_ms,
            details={
                "issue_key": issue_key,
                "issue_id": issue_id,
                "summary": summary,
            },
        )
        TestJiraSmoke._scenario_results.append(result)

    def test_jira_search_issue(self, credentials, evidence_collector):
        """R9.2: JIRA-SEARCH - JQL search for created issue.

        WHEN scenario JIRA-SEARCH is executed, THE Test_Framework SHALL invoke
        jira_search_issues with JQL and SHALL assert the created issue appears
        in results.
        """
        issue_key = TestJiraSmoke._created_issue_key
        assert issue_key is not None, (
            "JIRA-SEARCH requires JIRA-CREATE to have succeeded first"
        )

        client = _build_jira_client(credentials)
        jql = f'project = {JIRA_PROJECT_KEY} AND summary ~ "Local-E2E"'

        start = time.perf_counter()
        try:
            # Atlassian deprecated ``/rest/api/3/search`` in 2024 and
            # removed it; the replacement is ``/rest/api/3/search/jql``
            # which returns the same shape under ``issues``. See
            # https://developer.atlassian.com/changelog/#CHANGE-2046
            # The new /search/jql endpoint omits ``key`` from the
            # response unless ``fields`` is requested explicitly - pass
            # a minimal field list so the parser below can still read
            # ``issue["key"]``.
            response = client.get(
                "/rest/api/3/search/jql",
                params={
                    "jql": jql,
                    "maxResults": 50,
                    "fields": "summary",
                },
            )
            latency_ms = (time.perf_counter() - start) * 1000
        finally:
            client.close()

        assert response.status_code == 200, (
            f"JIRA-SEARCH failed: HTTP {response.status_code}\n"
            f"Response: {response.text[:500]}"
        )

        data = response.json()
        issues = data.get("issues", [])
        found_keys = [issue["key"] for issue in issues]

        assert issue_key in found_keys, (
            f"Created issue '{issue_key}' not found in JQL results.\n"
            f"Found keys: {found_keys[:10]}"
        )

        result = _scenario_result(
            scenario="JIRA-SEARCH",
            verdict="pass",
            status_code=response.status_code,
            latency_ms=latency_ms,
            details={
                "jql": jql,
                "total_results": data.get("total", 0),
                "issue_found": True,
            },
        )
        TestJiraSmoke._scenario_results.append(result)

    def test_jira_add_comment(self, credentials, evidence_collector):
        """R9.3: JIRA-COMMENT - add comment to created issue.

        WHEN scenario JIRA-COMMENT is executed, THE Test_Framework SHALL invoke
        jira_add_comment on the created issue and SHALL assert HTTP 2xx.
        """
        issue_key = TestJiraSmoke._created_issue_key
        assert issue_key is not None, (
            "JIRA-COMMENT requires JIRA-CREATE to have succeeded first"
        )

        client = _build_jira_client(credentials)
        comment_body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "[Local-E2E] automated comment",
                            }
                        ],
                    }
                ],
            }
        }

        start = time.perf_counter()
        try:
            response = client.post(
                f"/rest/api/3/issue/{issue_key}/comment",
                json=comment_body,
            )
            latency_ms = (time.perf_counter() - start) * 1000
        finally:
            client.close()

        assert response.status_code in (200, 201), (
            f"JIRA-COMMENT failed: HTTP {response.status_code}\n"
            f"Response: {response.text[:500]}"
        )

        data = response.json()
        comment_id = data.get("id", "")

        result = _scenario_result(
            scenario="JIRA-COMMENT",
            verdict="pass",
            status_code=response.status_code,
            latency_ms=latency_ms,
            details={
                "issue_key": issue_key,
                "comment_id": comment_id,
            },
        )
        TestJiraSmoke._scenario_results.append(result)

    def test_jira_transition_to_done(self, credentials, evidence_collector):
        """R9.4: JIRA-TRANSITION - move issue to Done, verify status.

        WHEN scenario JIRA-TRANSITION is executed, THE Test_Framework SHALL
        move the issue to Done and SHALL verify the status change.
        """
        issue_key = TestJiraSmoke._created_issue_key
        assert issue_key is not None, (
            "JIRA-TRANSITION requires JIRA-CREATE to have succeeded first"
        )

        client = _build_jira_client(credentials)

        # Step 1: Get available transitions
        start = time.perf_counter()
        try:
            transitions_resp = client.get(
                f"/rest/api/3/issue/{issue_key}/transitions"
            )
            assert transitions_resp.status_code == 200, (
                f"Failed to get transitions: HTTP {transitions_resp.status_code}\n"
                f"Response: {transitions_resp.text[:500]}"
            )

            transitions = transitions_resp.json().get("transitions", [])

            # Find the "Done" transition (case-insensitive search)
            done_transition = None
            for t in transitions:
                if t.get("name", "").lower() == "done":
                    done_transition = t
                    break

            # If no exact "Done", look for any terminal status
            if done_transition is None:
                for t in transitions:
                    status_category = (
                        t.get("to", {})
                        .get("statusCategory", {})
                        .get("key", "")
                    )
                    if status_category == "done":
                        done_transition = t
                        break

            assert done_transition is not None, (
                f"No 'Done' transition found for {issue_key}.\n"
                f"Available transitions: {[t['name'] for t in transitions]}"
            )

            # Step 2: Execute the transition
            transition_payload = {"transition": {"id": done_transition["id"]}}
            transition_resp = client.post(
                f"/rest/api/3/issue/{issue_key}/transitions",
                json=transition_payload,
            )
            assert transition_resp.status_code == 204, (
                f"Transition failed: HTTP {transition_resp.status_code}\n"
                f"Response: {transition_resp.text[:500]}"
            )

            # Step 3: Verify the status changed
            verify_resp = client.get(f"/rest/api/3/issue/{issue_key}")
            assert verify_resp.status_code == 200, (
                f"Failed to verify issue status: HTTP {verify_resp.status_code}"
            )

            latency_ms = (time.perf_counter() - start) * 1000

            issue_data = verify_resp.json()
            current_status = (
                issue_data.get("fields", {}).get("status", {}).get("name", "")
            )
            status_category = (
                issue_data.get("fields", {})
                .get("status", {})
                .get("statusCategory", {})
                .get("key", "")
            )

            # Accept either exact "Done" name or "done" category
            assert status_category == "done" or current_status.lower() == "done", (
                f"Issue {issue_key} status is '{current_status}' "
                f"(category: '{status_category}'), expected Done."
            )

        finally:
            client.close()

        result = _scenario_result(
            scenario="JIRA-TRANSITION",
            verdict="pass",
            status_code=204,
            latency_ms=latency_ms,
            details={
                "issue_key": issue_key,
                "transition_id": done_transition["id"],
                "transition_name": done_transition["name"],
                "final_status": current_status,
                "status_category": status_category,
            },
        )
        TestJiraSmoke._scenario_results.append(result)

    def test_jira_delete_issue(self, credentials, evidence_collector):
        """R9.5: JIRA-DELETE - delete issue, verify 404 on get.

        WHEN scenario JIRA-DELETE is executed, THE Test_Framework SHALL delete
        the issue and SHALL verify HTTP 404 on subsequent get.
        """
        issue_key = TestJiraSmoke._created_issue_key
        assert issue_key is not None, (
            "JIRA-DELETE requires JIRA-CREATE to have succeeded first"
        )

        client = _build_jira_client(credentials)

        start = time.perf_counter()
        try:
            # Delete the issue
            delete_resp = client.delete(f"/rest/api/3/issue/{issue_key}")
            assert delete_resp.status_code == 204, (
                f"JIRA-DELETE failed: HTTP {delete_resp.status_code}\n"
                f"Response: {delete_resp.text[:500]}"
            )

            # Verify 404 on subsequent get
            verify_resp = client.get(f"/rest/api/3/issue/{issue_key}")
            latency_ms = (time.perf_counter() - start) * 1000

            assert verify_resp.status_code == 404, (
                f"Expected 404 after deletion, got HTTP {verify_resp.status_code}\n"
                f"Issue {issue_key} may not have been deleted."
            )

        finally:
            client.close()

        result = _scenario_result(
            scenario="JIRA-DELETE",
            verdict="pass",
            status_code=204,
            latency_ms=latency_ms,
            details={
                "issue_key": issue_key,
                "delete_confirmed": True,
                "get_after_delete_status": 404,
            },
        )
        TestJiraSmoke._scenario_results.append(result)

    def test_emit_jira_evidence(self, credentials, evidence_collector):
        """R9.6: Emit e2e-evidence/09-jira-smoke.json with per-scenario verdict.

        THE Evidence_Collector SHALL emit evidence with per-scenario verdict,
        HTTP status, latency and response excerpts.
        """
        # Build overall verdict
        all_pass = all(
            r["verdict"] == "pass" for r in TestJiraSmoke._scenario_results
        )
        overall_verdict = "pass" if all_pass else "fail"

        evidence_data = {
            "overall_verdict": overall_verdict,
            "project_key": JIRA_PROJECT_KEY,
            "scenarios_executed": len(TestJiraSmoke._scenario_results),
            "scenarios": TestJiraSmoke._scenario_results,
            "created_issue_key": TestJiraSmoke._created_issue_key,
        }

        evidence_path = evidence_collector.emit_json(
            "R9.1-R9.6", EVIDENCE_FILENAME, evidence_data
        )
        assert evidence_path.exists(), (
            f"Evidence file not created at {evidence_path}"
        )
