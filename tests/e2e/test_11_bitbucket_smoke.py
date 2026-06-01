"""
Test 11: Bitbucket real API smoke test — repo, branch, PR lifecycle.

Validates the full Bitbucket Cloud lifecycle via REST API 2.0:
- BB-REPO: get repository example_workspace/smoke-test
- BB-BRANCH: create branch ai/local-e2e-{epoch} from main
- BB-COMMIT: commit test file on branch
- BB-PR: open PR from branch to main
- BB-DECLINE: decline PR, verify state
- BB-CLEANUP: delete branch

Uses httpx with Bearer token auth (Workspace Access Token).
NEVER logs raw tokens.

Requirements: R11.1, R11.2, R11.3, R11.4, R11.5, R11.6, R11.7
"""

import time
from typing import Any, Optional

import httpx
import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BITBUCKET_API_BASE = "https://api.bitbucket.org/2.0"

# Timeout for individual API calls
REQUEST_TIMEOUT = 30.0

# Evidence filename
EVIDENCE_FILENAME = "11-bitbucket-smoke.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def bb_headers(token: str) -> dict[str, str]:
    """Build authorization headers for Bitbucket API using Bearer token.

    NEVER logs the raw token value.
    """
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def bb_accept_headers(token: str) -> dict[str, str]:
    """Build Bearer auth headers for body-less Bitbucket actions."""
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }


def bb_url(path: str) -> str:
    """Build full Bitbucket API URL from a relative path."""
    return f"{BITBUCKET_API_BASE}/{path.lstrip('/')}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBitbucketSmoke:
    """Bitbucket Cloud lifecycle smoke test via REST API 2.0.

    Executes a full lifecycle: get repo → create branch → commit file →
    open PR → decline PR → delete branch. Uses Bearer token auth.
    """

    # Shared state across test methods (populated sequentially)
    _branch_name: Optional[str] = None
    _commit_sha: Optional[str] = None
    _pr_id: Optional[int] = None
    _evidence: dict[str, Any] = {}

    def test_bb_repo_get_repository(self, credentials, evidence_collector):
        """R11.1: BB-REPO — get repository example_workspace/smoke-test, assert HTTP 2xx.

        WHEN scenario BB-REPO is executed, THE Test_Framework SHALL invoke
        bitbucket_get_repository for example_workspace/smoke-test and SHALL assert HTTP 2xx.
        """
        workspace = credentials.bitbucket_workspace
        repo_slug = credentials.bitbucket_repo
        token = credentials.bitbucket_token_bearer

        url = bb_url(f"repositories/{workspace}/{repo_slug}")

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.get(url, headers=bb_headers(token))

        TestBitbucketSmoke._evidence["bb_repo"] = {
            "scenario": "BB-REPO",
            "url": url,
            "status_code": response.status_code,
            "workspace": workspace,
            "repo_slug": repo_slug,
            "verdict": "pass" if response.status_code < 300 else "fail",
        }

        assert response.status_code < 300, (
            f"BB-REPO failed: expected HTTP 2xx, got {response.status_code}. "
            f"Response: {response.text[:500]}"
        )

        # Verify response contains expected repo data
        data = response.json()
        assert data.get("slug") == repo_slug, (
            f"Expected repo slug '{repo_slug}', got '{data.get('slug')}'"
        )
        TestBitbucketSmoke._evidence["bb_repo"]["repo_full_name"] = data.get("full_name")

    def test_bb_branch_create(self, credentials, evidence_collector):
        """R11.2: BB-BRANCH — create branch ai/local-e2e-{epoch} from main.

        WHEN scenario BB-BRANCH is executed, THE Test_Framework SHALL create
        branch ai/local-e2e-{epoch} from main and SHALL record the branch name.
        """
        workspace = credentials.bitbucket_workspace
        repo_slug = credentials.bitbucket_repo
        token = credentials.bitbucket_token_bearer

        epoch = int(time.time())
        branch_name = f"ai/local-e2e-{epoch}"
        TestBitbucketSmoke._branch_name = branch_name

        url = bb_url(f"repositories/{workspace}/{repo_slug}/refs/branches")
        payload = {
            "name": branch_name,
            "target": {
                "hash": "main",
            },
        }

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(url, headers=bb_headers(token), json=payload)

        TestBitbucketSmoke._evidence["bb_branch"] = {
            "scenario": "BB-BRANCH",
            "branch_name": branch_name,
            "status_code": response.status_code,
            "verdict": "pass" if response.status_code < 300 else "fail",
        }

        assert response.status_code < 300, (
            f"BB-BRANCH failed: expected HTTP 2xx, got {response.status_code}. "
            f"Response: {response.text[:500]}"
        )

        data = response.json()
        TestBitbucketSmoke._evidence["bb_branch"]["target_hash"] = data.get("target", {}).get("hash")

    def test_bb_commit_file(self, credentials, evidence_collector):
        """R11.3: BB-COMMIT — commit test file on branch, record commit SHA.

        WHEN scenario BB-COMMIT is executed, THE Test_Framework SHALL commit
        a test file on the created branch and SHALL record the commit SHA.
        """
        workspace = credentials.bitbucket_workspace
        repo_slug = credentials.bitbucket_repo
        token = credentials.bitbucket_token_bearer
        branch_name = TestBitbucketSmoke._branch_name

        assert branch_name is not None, (
            "BB-COMMIT requires BB-BRANCH to have run first (branch_name is None)"
        )

        # Use the src endpoint to commit a file
        url = bb_url(f"repositories/{workspace}/{repo_slug}/src")

        # Bitbucket src endpoint uses multipart form data for file commits
        epoch = int(time.time())
        file_content = f"# E2E Smoke Test\nCreated at epoch: {epoch}\nBranch: {branch_name}\n"

        headers = {
            "Authorization": f"Bearer {token}",
        }

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(
                url,
                headers=headers,
                data={
                    "message": f"e2e: smoke test commit at {epoch}",
                    "branch": branch_name,
                },
                files={
                    "e2e-test-file.md": ("e2e-test-file.md", file_content, "text/plain"),
                },
            )

        TestBitbucketSmoke._evidence["bb_commit"] = {
            "scenario": "BB-COMMIT",
            "branch_name": branch_name,
            "status_code": response.status_code,
            "verdict": "pass" if response.status_code < 300 else "fail",
        }

        assert response.status_code < 300, (
            f"BB-COMMIT failed: expected HTTP 2xx, got {response.status_code}. "
            f"Response: {response.text[:500]}"
        )

        # Get the latest commit SHA on the branch
        branch_url = bb_url(
            f"repositories/{workspace}/{repo_slug}/refs/branches/{branch_name}"
        )
        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            branch_resp = client.get(branch_url, headers=bb_headers(token))

        if branch_resp.status_code < 300:
            branch_data = branch_resp.json()
            commit_sha = branch_data.get("target", {}).get("hash", "")
            TestBitbucketSmoke._commit_sha = commit_sha
            TestBitbucketSmoke._evidence["bb_commit"]["commit_sha"] = commit_sha

    def test_bb_pr_open(self, credentials, evidence_collector):
        """R11.4: BB-PR — open PR from branch to main, record PR ID.

        WHEN scenario BB-PR is executed, THE Test_Framework SHALL open a PR
        from the created branch to main and SHALL record the PR ID.
        """
        workspace = credentials.bitbucket_workspace
        repo_slug = credentials.bitbucket_repo
        token = credentials.bitbucket_token_bearer
        branch_name = TestBitbucketSmoke._branch_name

        assert branch_name is not None, (
            "BB-PR requires BB-BRANCH to have run first (branch_name is None)"
        )

        url = bb_url(f"repositories/{workspace}/{repo_slug}/pullrequests")
        payload = {
            "title": f"[E2E Smoke] Auto-test PR from {branch_name}",
            "description": "Automated E2E smoke test PR. Will be declined automatically.",
            "source": {
                "branch": {
                    "name": branch_name,
                },
            },
            "destination": {
                "branch": {
                    "name": "main",
                },
            },
            "close_source_branch": False,
        }

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(url, headers=bb_headers(token), json=payload)

        TestBitbucketSmoke._evidence["bb_pr"] = {
            "scenario": "BB-PR",
            "branch_name": branch_name,
            "status_code": response.status_code,
            "verdict": "pass" if response.status_code < 300 else "fail",
        }

        assert response.status_code < 300, (
            f"BB-PR failed: expected HTTP 2xx, got {response.status_code}. "
            f"Response: {response.text[:500]}"
        )

        data = response.json()
        pr_id = data.get("id")
        assert pr_id is not None, "PR creation succeeded but no PR ID in response"

        TestBitbucketSmoke._pr_id = pr_id
        TestBitbucketSmoke._evidence["bb_pr"]["pr_id"] = pr_id
        TestBitbucketSmoke._evidence["bb_pr"]["pr_state"] = data.get("state")

    def test_bb_decline_pr(self, credentials, evidence_collector):
        """R11.5: BB-DECLINE — decline PR, verify state is DECLINED.

        WHEN scenario BB-DECLINE is executed, THE Test_Framework SHALL decline
        the PR and SHALL verify state is DECLINED.
        """
        workspace = credentials.bitbucket_workspace
        repo_slug = credentials.bitbucket_repo
        token = credentials.bitbucket_token_bearer
        pr_id = TestBitbucketSmoke._pr_id
        auth_mode = "bearer"

        assert pr_id is not None, (
            "BB-DECLINE requires BB-PR to have run first (pr_id is None)"
        )

        url = bb_url(
            f"repositories/{workspace}/{repo_slug}/pullrequests/{pr_id}/decline"
        )

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.post(url, headers=bb_accept_headers(token))
            if response.status_code == 400:
                # Bitbucket Cloud documents the decline endpoint as a
                # body-less POST. Some workspace access-token calls return
                # a generic 400 here; retry with the user/app-password
                # credential from CREDENTIALS.md before failing the smoke.
                auth_mode = "basic_fallback"
                response = client.post(
                    url,
                    headers={"Accept": "application/json"},
                    auth=(
                        credentials.bitbucket_username,
                        credentials.bitbucket_token_basic,
                    ),
                )

        TestBitbucketSmoke._evidence["bb_decline"] = {
            "scenario": "BB-DECLINE",
            "pr_id": pr_id,
            "status_code": response.status_code,
            "auth_mode": auth_mode,
            "verdict": "pass" if response.status_code < 300 else "fail",
        }

        assert response.status_code < 300, (
            f"BB-DECLINE failed: expected HTTP 2xx, got {response.status_code}. "
            f"Response: {response.text[:500]}"
        )

        data = response.json()
        pr_state = data.get("state", "").upper()

        TestBitbucketSmoke._evidence["bb_decline"]["pr_state"] = pr_state

        assert pr_state == "DECLINED", (
            f"Expected PR state 'DECLINED', got '{pr_state}'"
        )

    def test_bb_cleanup_delete_branch(self, credentials, evidence_collector):
        """R11.6: BB-CLEANUP — delete the created branch.

        WHEN scenario BB-CLEANUP is executed, THE Test_Framework SHALL delete
        the created branch.
        """
        workspace = credentials.bitbucket_workspace
        repo_slug = credentials.bitbucket_repo
        token = credentials.bitbucket_token_bearer
        branch_name = TestBitbucketSmoke._branch_name

        assert branch_name is not None, (
            "BB-CLEANUP requires BB-BRANCH to have run first (branch_name is None)"
        )

        # Bitbucket API: DELETE /repositories/{workspace}/{repo_slug}/refs/branches/{name}
        # Note: branch name with slashes needs to be URL-encoded
        encoded_branch = branch_name.replace("/", "%2F")
        url = bb_url(
            f"repositories/{workspace}/{repo_slug}/refs/branches/{encoded_branch}"
        )

        with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
            response = client.delete(url, headers=bb_headers(token))

        TestBitbucketSmoke._evidence["bb_cleanup"] = {
            "scenario": "BB-CLEANUP",
            "branch_name": branch_name,
            "status_code": response.status_code,
            "verdict": "pass" if response.status_code < 300 else "fail",
        }

        # 204 No Content is the expected success response for DELETE
        assert response.status_code in (200, 204), (
            f"BB-CLEANUP failed: expected HTTP 200/204, got {response.status_code}. "
            f"Response: {response.text[:500]}"
        )

    def test_bb_emit_evidence(self, credentials, evidence_collector):
        """R11.7: Emit e2e-evidence/11-bitbucket-smoke.json with per-scenario verdict.

        THE Evidence_Collector SHALL emit e2e-evidence/11-bitbucket-smoke.json
        with per-scenario verdict and evidence.
        """
        # Build overall verdict
        scenarios = [
            "bb_repo", "bb_branch", "bb_commit", "bb_pr", "bb_decline", "bb_cleanup"
        ]
        all_pass = all(
            TestBitbucketSmoke._evidence.get(s, {}).get("verdict") == "pass"
            for s in scenarios
        )

        evidence_data = {
            "test": "test_11_bitbucket_smoke",
            "overall_verdict": "pass" if all_pass else "fail",
            "workspace": credentials.bitbucket_workspace,
            "repo": credentials.bitbucket_repo,
            "branch_name": TestBitbucketSmoke._branch_name,
            "commit_sha": TestBitbucketSmoke._commit_sha,
            "pr_id": TestBitbucketSmoke._pr_id,
            "scenarios": TestBitbucketSmoke._evidence,
        }

        evidence_path = evidence_collector.emit_json(
            "R11", EVIDENCE_FILENAME, evidence_data
        )

        assert evidence_path.exists(), (
            f"Evidence file not created at {evidence_path}"
        )
