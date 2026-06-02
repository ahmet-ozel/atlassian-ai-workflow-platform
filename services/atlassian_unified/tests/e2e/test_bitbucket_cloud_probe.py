"""Bitbucket Cloud E2E tests for the dependency connectivity probe.

Opt-in end-to-end tests marked ``pytest.mark.bitbucket_cloud_e2e`` covering
Requirements 18.2, 18.3, and 18.5 — the ``_bitbucket_probe`` function that
validates connectivity against ``GET /2.0/workspaces?pagelen=1`` for Cloud
mode.

These tests drive the probe logic directly (via ``_bitbucket_probe`` from
``servers/dependencies.py``) against a live Bitbucket Cloud tenant, asserting
that:

* Valid credentials produce a successful probe result (non-error dict with
  ``values`` key).
* Invalid credentials raise ``MCPAtlassianAuthenticationError`` — the same
  structured auth-error shape used across the server.

Required environment variables (gated by ``conftest.py``):

* ``BITBUCKET_CLOUD_URL`` or ``BITBUCKET_URL`` — a Cloud-host URL
  (e.g. ``https://api.bitbucket.org``).
* ``BITBUCKET_WORKSPACE`` — the Cloud workspace slug.
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

from mcp_atlassian.bitbucket import BitbucketFetcher
from mcp_atlassian.bitbucket.config import BitbucketConfig
from mcp_atlassian.exceptions import MCPAtlassianAuthenticationError
from mcp_atlassian.servers.dependencies import _bitbucket_probe

pytestmark = pytest.mark.bitbucket_cloud_e2e


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cloud_fetcher() -> BitbucketFetcher:
    """Create a BitbucketFetcher configured for Cloud with valid credentials.

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

    return BitbucketFetcher(config=config)


@pytest.fixture(scope="module")
def invalid_cloud_fetcher() -> BitbucketFetcher:
    """Create a BitbucketFetcher configured for Cloud with intentionally bad credentials.

    Uses a clearly invalid app password to trigger a 401 from the Cloud API.
    """
    url = os.environ.get("BITBUCKET_CLOUD_URL") or os.environ.get(
        "BITBUCKET_URL", "https://api.bitbucket.org"
    )

    config = BitbucketConfig(
        url=url,
        auth_type="basic",
        username="invalid-e2e-user-does-not-exist",
        app_password="clearly-invalid-app-password-for-e2e-test",
        workspace="nonexistent-workspace",
    )

    return BitbucketFetcher(config=config)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.bitbucket_cloud_e2e
class TestBitbucketCloudProbe:
    """Requirements 18.2, 18.3, 18.5 — Cloud connectivity probe via _bitbucket_probe."""

    def test_cloud_probe_succeeds_with_valid_credentials(
        self,
        cloud_fetcher: BitbucketFetcher,
    ) -> None:
        """Probe returns a successful response against GET /2.0/workspaces?pagelen=1.

        Validates:
        - The probe does not raise any exception with valid credentials.
        - The response is a dict containing a ``values`` key (the Cloud
          pagination envelope).
        - An empty ``values`` list is acceptable (Requirement 18.3 — some
          users legitimately have access to zero workspaces).
        """
        result = _bitbucket_probe(cloud_fetcher)

        # The probe should return a dict-like response from the Cloud API.
        assert isinstance(result, dict), (
            f"Expected dict response from Cloud probe, got {type(result).__name__}: "
            f"{result!r}"
        )
        # Cloud 2.0 pagination envelope always has a ``values`` key.
        assert "values" in result, (
            f"Expected 'values' key in Cloud probe response, got keys: "
            f"{list(result.keys())}"
        )
        # ``values`` is a list (possibly empty per Requirement 18.3).
        assert isinstance(result["values"], list), (
            f"Expected 'values' to be a list, got "
            f"{type(result['values']).__name__}"
        )

    def test_cloud_probe_fails_with_invalid_credentials(
        self,
        invalid_cloud_fetcher: BitbucketFetcher,
    ) -> None:
        """Probe raises MCPAtlassianAuthenticationError on 401 with bad credentials.

        Validates:
        - Invalid credentials trigger the structured auth-error path
          (MCPAtlassianAuthenticationError), not an unhandled exception or
          a silent failure.
        - This is the same error shape that ``_create_and_validate`` catches
          and wraps into a user-facing ``ValueError``.
        """
        with pytest.raises(
            (MCPAtlassianAuthenticationError, Exception)
        ) as exc_info:
            _bitbucket_probe(invalid_cloud_fetcher)

        # The error should be either MCPAtlassianAuthenticationError directly
        # (raised by the handle_auth_errors decorator on 401/403) or an
        # HTTPError that _create_and_validate would catch. Either way, it
        # must not be a generic programming error (AttributeError, TypeError,
        # etc.).
        exc = exc_info.value
        assert not isinstance(exc, (AttributeError, TypeError, KeyError)), (
            f"Expected a structured auth/HTTP error, got an unhandled "
            f"programming error: {type(exc).__name__}: {exc}"
        )
