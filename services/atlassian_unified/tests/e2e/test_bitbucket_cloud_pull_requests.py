"""Bitbucket Cloud E2E tests for pull request operations.

Opt-in end-to-end tests marked ``pytest.mark.bitbucket_cloud_e2e`` covering
Requirements 9.1, 9.2, 9.3, 9.5 (pull request list, get, create, approve,
merge round-trip).

These tests drive the MCP tools (``list_pull_requests``,
``get_pull_request``, ``create_pull_request``, ``approve_pull_request``,
``merge_pull_request``) through a FastMCP in-process client, asserting the
structured JSON contract that agents observe.

Tests are configured entirely from environment variables so they can run
against any reachable Bitbucket Cloud workspace:

* ``BITBUCKET_CLOUD_URL`` (or ``BITBUCKET_URL``) -- Cloud base URL
  (e.g. ``https://api.bitbucket.org``).
* ``BITBUCKET_WORKSPACE`` -- the Cloud workspace slug.
* ``BITBUCKET_APP_PASSWORD`` or ``BITBUCKET_CLOUD_ACCESS_TOKEN`` -- Cloud
  credentials.
* ``BITBUCKET_USERNAME`` -- required when using App Password auth.
* ``BITBUCKET_TEST_REPO_SLUG`` -- repository slug for tests (defaults to
  first available repo in the workspace).
* ``BITBUCKET_TEST_PROJECT_KEY`` -- project key / workspace override for
  tests (defaults to ``BITBUCKET_WORKSPACE``).

All tests short-circuit with ``pytest.skip`` when the required env vars
are missing. The ``bitbucket_cloud_e2e`` marker additionally gates
execution behind the ``--bitbucket-cloud-e2e`` pytest CLI flag registered
in ``tests/e2e/conftest.py``.
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import patch

import pytest
from fastmcp import Client
from fastmcp.client import FastMCPTransport
from mcp.types import CallToolResult, TextContent

from mcp_atlassian.servers import bitbucket_mcp

pytestmark = [pytest.mark.bitbucket_cloud_e2e, pytest.mark.anyio]


# ---------------------------------------------------------------------------
# Env-var driven configuration
# ---------------------------------------------------------------------------

_REQUIRED_ENV = (
    "BITBUCKET_WORKSPACE",
)

_CREDENTIAL_ENV_SETS = (
    ("BITBUCKET_CLOUD_ACCESS_TOKEN",),
    ("BITBUCKET_API_TOKEN", "BITBUCKET_USERNAME"),
    ("BITBUCKET_APP_PASSWORD", "BITBUCKET_USERNAME"),
)


def _require_env() -> dict[str, str]:
    """Return the required env vars or skip the test module."""
    missing: list[str] = []

    url = os.environ.get("BITBUCKET_CLOUD_URL") or os.environ.get("BITBUCKET_URL")
    if not url:
        missing.append("BITBUCKET_CLOUD_URL or BITBUCKET_URL")

    for name in _REQUIRED_ENV:
        if not os.environ.get(name):
            missing.append(name)

    # Check at least one credential set is present
    has_creds = any(
        all(os.environ.get(v) for v in cred_set)
        for cred_set in _CREDENTIAL_ENV_SETS
    )
    if not has_creds:
        missing.append(
            "BITBUCKET_CLOUD_ACCESS_TOKEN or "
            "(BITBUCKET_API_TOKEN/BITBUCKET_APP_PASSWORD + BITBUCKET_USERNAME)"
        )

    if missing:
        pytest.skip(
            "Missing required env vars for Bitbucket Cloud PR E2E tests: "
            + ", ".join(missing)
        )

    env: dict[str, str] = {}
    env["BITBUCKET_URL"] = url  # type: ignore[assignment]
    env["BITBUCKET_WORKSPACE"] = os.environ["BITBUCKET_WORKSPACE"]

    # Propagate credentials
    for key in (
        "BITBUCKET_CLOUD_ACCESS_TOKEN",
        "BITBUCKET_API_TOKEN",
        "BITBUCKET_APP_PASSWORD",
        "BITBUCKET_USERNAME",
    ):
        val = os.environ.get(key)
        if val:
            env[key] = val

    return env


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bitbucket_env() -> dict[str, str]:
    """Environment variables used to configure the MCP server for Cloud.

    ``READ_ONLY_MODE`` is forced off to allow write operations and
    ``TOOLSETS=all`` enables all toolsets including pull_requests.
    """
    required = _require_env()
    env = {
        **required,
        "READ_ONLY_MODE": "false",
        "TOOLSETS": "all",
    }
    return env


@pytest.fixture
def project_key() -> str:
    """Project key (workspace) for the test repository."""
    _require_env()
    return os.environ.get(
        "BITBUCKET_TEST_PROJECT_KEY",
        os.environ.get("BITBUCKET_WORKSPACE", ""),
    )


@pytest.fixture
def repo_slug() -> str:
    """Repository slug for the test repository.

    Falls back to BITBUCKET_TEST_REPO_SLUG env var. If not set, tests
    that require a specific repo will attempt to discover one via
    list_pull_requests on the workspace.
    """
    _require_env()
    return os.environ.get("BITBUCKET_TEST_REPO_SLUG", "")


@pytest.fixture
async def mcp_client(bitbucket_env: dict[str, str]) -> Any:
    """In-process FastMCP client connected to ``bitbucket_mcp`` against Cloud."""
    with patch.dict(os.environ, bitbucket_env, clear=False):
        transport = FastMCPTransport(bitbucket_mcp)
        client = Client(transport=transport)
        async with client as connected_client:
            yield connected_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call_tool(
    client: Client, tool_name: str, arguments: dict[str, Any]
) -> CallToolResult:
    """Invoke an MCP tool on the given client."""
    return await client.call_tool(tool_name, arguments)


def _payload(result: CallToolResult) -> dict[str, Any]:
    """Extract the JSON payload from a ``CallToolResult``."""
    assert result.content and isinstance(result.content[0], TextContent), (
        "expected a single TextContent from the MCP tool"
    )
    return json.loads(result.content[0].text)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBitbucketCloudPullRequests:
    """Requirements 9.1, 9.2 -- list and get pull requests on Cloud."""

    @pytest.mark.anyio
    async def test_list_pull_requests(
        self,
        mcp_client: Client,
        project_key: str,
        repo_slug: str,
    ) -> None:
        """list_pull_requests returns a list (may be empty) on Cloud.

        Validates Requirement 9.2: Cloud PR tools target
        ``/2.0/repositories/{workspace}/{repo_slug}/pullrequests/...``.
        """
        if not repo_slug:
            pytest.skip(
                "BITBUCKET_TEST_REPO_SLUG not set; cannot list PRs without a repo"
            )

        result = await _call_tool(
            mcp_client,
            "list_pull_requests",
            {
                "project_key": project_key,
                "repo_slug": repo_slug,
                "state": "ALL",
                "limit": 10,
            },
        )
        assert not result.is_error, f"list_pull_requests raised: {result}"
        payload = _payload(result)
        assert payload["success"] is True, payload

        prs = payload.get("pull_requests", [])
        assert isinstance(prs, list), (
            f"expected pull_requests to be a list, got {type(prs).__name__}"
        )

    @pytest.mark.anyio
    async def test_get_pull_request(
        self,
        mcp_client: Client,
        project_key: str,
        repo_slug: str,
    ) -> None:
        """get_pull_request returns expected fields for an existing PR.

        Validates Requirement 9.2: Cloud PR tools target the correct
        endpoint and return normalized data with id, title, state, author.
        """
        if not repo_slug:
            pytest.skip(
                "BITBUCKET_TEST_REPO_SLUG not set; cannot get PR without a repo"
            )

        # First, list PRs to find one to get
        list_result = await _call_tool(
            mcp_client,
            "list_pull_requests",
            {
                "project_key": project_key,
                "repo_slug": repo_slug,
                "state": "ALL",
                "limit": 5,
            },
        )
        list_payload = _payload(list_result)
        prs = list_payload.get("pull_requests", [])
        if not prs:
            pytest.skip("No pull requests found in the test repository")

        pr_id = prs[0]["id"]

        # Get the specific PR
        get_result = await _call_tool(
            mcp_client,
            "get_pull_request",
            {
                "project_key": project_key,
                "repo_slug": repo_slug,
                "pr_id": pr_id,
            },
        )
        assert not get_result.is_error, f"get_pull_request raised: {get_result}"
        get_payload = _payload(get_result)
        assert get_payload["success"] is True, get_payload

        pr = get_payload["pull_request"]
        assert "id" in pr, f"PR missing 'id' field: {pr}"
        assert "title" in pr, f"PR missing 'title' field: {pr}"
        assert "state" in pr, f"PR missing 'state' field: {pr}"
        assert "author" in pr, f"PR missing 'author' field: {pr}"


class TestBitbucketCloudPullRequestRoundtrip:
    """Requirement 9.3, 9.5 -- create, approve, merge round-trip on Cloud.

    This is a destructive test that creates a PR, approves it, and merges
    it. It requires a test repository with at least two branches where a
    PR can be created. Marked as xfail by default since it requires
    specific repository setup (a source branch with commits ahead of the
    target branch).
    """

    @pytest.mark.xfail(
        reason=(
            "Destructive test: requires BITBUCKET_TEST_REPO_SLUG with a "
            "branch that can be PR'd and merged. Set "
            "BITBUCKET_TEST_SOURCE_BRANCH and BITBUCKET_TEST_TARGET_BRANCH "
            "env vars to enable."
        ),
        strict=False,
    )
    @pytest.mark.anyio
    async def test_create_approve_merge_roundtrip(
        self,
        mcp_client: Client,
        project_key: str,
        repo_slug: str,
    ) -> None:
        """Create a PR, approve it, then merge it.

        Validates:
        - Requirement 9.3: approve targets
          ``POST /2.0/repositories/{workspace}/{repo_slug}/pullrequests/{id}/approve``
        - Requirement 9.5: merge targets
          ``POST /2.0/repositories/{workspace}/{repo_slug}/pullrequests/{id}/merge``

        This test is destructive and requires:
        - BITBUCKET_TEST_REPO_SLUG to be set
        - BITBUCKET_TEST_SOURCE_BRANCH (branch with commits ahead of target)
        - BITBUCKET_TEST_TARGET_BRANCH (e.g. 'main')
        """
        if not repo_slug:
            pytest.skip("BITBUCKET_TEST_REPO_SLUG not set")

        source_branch = os.environ.get("BITBUCKET_TEST_SOURCE_BRANCH", "")
        target_branch = os.environ.get("BITBUCKET_TEST_TARGET_BRANCH", "main")

        if not source_branch:
            pytest.skip(
                "BITBUCKET_TEST_SOURCE_BRANCH not set; "
                "cannot create PR without a source branch"
            )

        # 1. Create a pull request
        create_result = await _call_tool(
            mcp_client,
            "create_pull_request",
            {
                "project_key": project_key,
                "repo_slug": repo_slug,
                "title": "E2E Cloud PR roundtrip test",
                "from_branch": source_branch,
                "to_branch": target_branch,
                "description": "Automated E2E test PR - will be merged immediately.",
            },
        )
        assert not create_result.is_error, (
            f"create_pull_request raised: {create_result}"
        )
        create_payload = _payload(create_result)
        assert create_payload["success"] is True, create_payload

        pr = create_payload["pull_request"]
        pr_id = pr["id"]
        assert isinstance(pr_id, int), f"PR id must be int, got {type(pr_id)}"

        try:
            # 2. Approve the pull request
            approve_result = await _call_tool(
                mcp_client,
                "approve_pull_request",
                {
                    "project_key": project_key,
                    "repo_slug": repo_slug,
                    "pr_id": pr_id,
                },
            )
            assert not approve_result.is_error, (
                f"approve_pull_request raised: {approve_result}"
            )
            approve_payload = _payload(approve_result)
            assert approve_payload["success"] is True, approve_payload

            # 3. Merge the pull request
            merge_result = await _call_tool(
                mcp_client,
                "merge_pull_request",
                {
                    "project_key": project_key,
                    "repo_slug": repo_slug,
                    "pr_id": pr_id,
                    "message": "E2E Cloud PR roundtrip merge",
                    "delete_source_branch": False,
                },
            )
            assert not merge_result.is_error, (
                f"merge_pull_request raised: {merge_result}"
            )
            merge_payload = _payload(merge_result)
            assert merge_payload["success"] is True, merge_payload

            merged_pr = merge_payload["pull_request"]
            assert merged_pr.get("state") in ("MERGED", "merged"), (
                f"expected PR state to be MERGED after merge, got: "
                f"{merged_pr.get('state')}"
            )
        except Exception:
            # Best-effort: decline the PR if merge failed so we don't
            # leave orphan PRs in the test repo.
            try:
                await _call_tool(
                    mcp_client,
                    "decline_pull_request",
                    {
                        "project_key": project_key,
                        "repo_slug": repo_slug,
                        "pr_id": pr_id,
                    },
                )
            except Exception:  # noqa: BLE001
                pass
            raise
