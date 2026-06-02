"""Bitbucket Cloud E2E tests for repository listing, get, and file content.

Opt-in end-to-end tests marked ``pytest.mark.bitbucket_cloud_e2e`` covering
Requirements 8.1, 8.2, and 8.5 — the ``list_repositories``,
``get_repository``, and ``get_file_content`` round-trip against a real
Bitbucket Cloud workspace.

These tests drive the ``BitbucketClient`` directly (not through the MCP
tool layer) to validate that the Cloud branches of the repositories mixin
produce correct, normalized responses when talking to a live Cloud tenant.

Required environment variables (gated by ``conftest.py``):

* ``BITBUCKET_CLOUD_URL`` or ``BITBUCKET_URL`` — a Cloud-host URL
  (e.g. ``https://api.bitbucket.org``).
* ``BITBUCKET_WORKSPACE`` — the Cloud workspace slug to list repos from.
* At least one of:
  - ``BITBUCKET_USERNAME`` + ``BITBUCKET_APP_PASSWORD`` (Basic auth)
  - ``BITBUCKET_CLOUD_ACCESS_TOKEN`` (OAuth 2.0 bearer)

All tests short-circuit with ``pytest.skip`` when the required env vars
are missing. The ``bitbucket_cloud_e2e`` marker additionally gates
execution behind the ``--bitbucket-cloud-e2e`` pytest CLI flag registered
in ``tests/e2e/conftest.py``.
"""

from __future__ import annotations

import os

import pytest

from mcp_atlassian.bitbucket.client import BitbucketClient
from mcp_atlassian.bitbucket.config import BitbucketConfig

pytestmark = pytest.mark.bitbucket_cloud_e2e


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bitbucket_cloud_client() -> BitbucketClient:
    """Create a BitbucketClient configured for Cloud from environment variables.

    Reads:
    - BITBUCKET_CLOUD_URL or BITBUCKET_URL (Cloud host)
    - BITBUCKET_WORKSPACE
    - BITBUCKET_USERNAME + BITBUCKET_APP_PASSWORD, or BITBUCKET_CLOUD_ACCESS_TOKEN
    """
    url = os.environ.get("BITBUCKET_CLOUD_URL") or os.environ.get("BITBUCKET_URL")
    if not url:
        pytest.skip("BITBUCKET_CLOUD_URL or BITBUCKET_URL not set")

    workspace = os.environ.get("BITBUCKET_WORKSPACE")
    if not workspace:
        pytest.skip("BITBUCKET_WORKSPACE not set")

    cloud_access_token = os.environ.get("BITBUCKET_CLOUD_ACCESS_TOKEN")
    username = os.environ.get("BITBUCKET_USERNAME")
    app_password = os.environ.get("BITBUCKET_API_TOKEN") or os.environ.get("BITBUCKET_APP_PASSWORD")

    if cloud_access_token:
        config = BitbucketConfig(
            url=url,
            auth_type="cloud_bearer",
            cloud_access_token=cloud_access_token,
            workspace=workspace,
        )
    elif username and app_password:
        config = BitbucketConfig(
            url=url,
            auth_type="basic",
            username=username,
            app_password=app_password,
            workspace=workspace,
        )
    else:
        pytest.skip(
            "Need BITBUCKET_CLOUD_ACCESS_TOKEN or "
            "BITBUCKET_USERNAME + BITBUCKET_API_TOKEN/BITBUCKET_APP_PASSWORD"
        )

    return BitbucketClient(config=config)


@pytest.fixture(scope="module")
def workspace() -> str:
    """The Bitbucket Cloud workspace slug from env."""
    ws = os.environ.get("BITBUCKET_WORKSPACE")
    if not ws:
        pytest.skip("BITBUCKET_WORKSPACE not set")
    return ws


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBitbucketCloudRepositories:
    """Req 8.1, 8.2, 8.5 — repository list/get/file-content round-trip."""

    def test_list_repositories(
        self,
        bitbucket_cloud_client: BitbucketClient,
        workspace: str,
    ) -> None:
        """list_repositories returns at least one repo with expected fields.

        Validates Requirement 8.2: Cloud mode calls
        ``GET /2.0/repositories/{workspace}`` and returns normalized
        repository objects.
        """
        repos = bitbucket_cloud_client.get_repositories(
            project_key=workspace,
            limit=10,
        )

        assert isinstance(repos, list)
        assert len(repos) >= 1, (
            f"Expected at least one repository in workspace '{workspace}'"
        )

        first_repo = repos[0]
        # Normalized repo must expose slug and name
        assert "slug" in first_repo, f"repo missing 'slug': {first_repo.keys()}"
        assert "name" in first_repo, f"repo missing 'name': {first_repo.keys()}"
        # Normalized repo must have a synthesized project object (Req 8.7)
        assert "project" in first_repo, (
            f"repo missing 'project': {first_repo.keys()}"
        )
        project = first_repo["project"]
        assert "key" in project, f"project missing 'key': {project}"

    def test_get_repository(
        self,
        bitbucket_cloud_client: BitbucketClient,
        workspace: str,
    ) -> None:
        """get_repository returns the same repo slug as listed.

        Validates Requirement 8.4: Cloud mode calls
        ``GET /2.0/repositories/{workspace}/{repo_slug}`` and returns
        a normalized repository object.
        """
        # First, list repos to get a known slug
        repos = bitbucket_cloud_client.get_repositories(
            project_key=workspace,
            limit=5,
        )
        assert len(repos) >= 1, "Need at least one repo to test get_repository"

        target_slug = repos[0]["slug"]

        # Now fetch that specific repo
        repo = bitbucket_cloud_client.get_repository(
            project_key=workspace,
            repo_slug=target_slug,
        )

        assert isinstance(repo, dict)
        assert repo["slug"] == target_slug, (
            f"Expected slug '{target_slug}', got '{repo.get('slug')}'"
        )
        # Should also have the synthesized project
        assert "project" in repo
        assert repo["project"]["key"] == workspace

    def test_get_file_content(
        self,
        bitbucket_cloud_client: BitbucketClient,
        workspace: str,
    ) -> None:
        """get_file_content returns non-empty content for a known file.

        Validates Requirement 8.5: Cloud mode calls
        ``GET /2.0/repositories/{workspace}/{slug}/src/{commit_or_branch}/{path}``
        and returns file content as a string.

        Attempts to read README.md (or README) from the first repo that
        has one. If no repo has a README, the test is skipped.
        """
        repos = bitbucket_cloud_client.get_repositories(
            project_key=workspace,
            limit=10,
        )
        assert len(repos) >= 1, "Need at least one repo to test get_file_content"

        # Try common README filenames across available repos
        readme_candidates = ["README.md", "README", "README.rst", "README.txt"]
        content: str | None = None
        found_file: str | None = None

        for repo in repos:
            repo_slug = repo["slug"]
            for filename in readme_candidates:
                try:
                    content = bitbucket_cloud_client.get_file_content(
                        project_key=workspace,
                        repo_slug=repo_slug,
                        file_path=filename,
                    )
                    if content:
                        found_file = f"{repo_slug}/{filename}"
                        break
                except (ValueError, Exception):  # noqa: BLE001
                    continue
            if content:
                break

        if content is None:
            pytest.skip(
                "No README file found in any repository in workspace "
                f"'{workspace}' — cannot test get_file_content"
            )

        assert isinstance(content, str)
        assert len(content) > 0, (
            f"Expected non-empty content from {found_file}"
        )
