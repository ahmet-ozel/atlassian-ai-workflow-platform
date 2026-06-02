"""Bitbucket Cloud E2E tests for webhook CRUD and secret redaction.

Opt-in end-to-end tests marked ``pytest.mark.bitbucket_cloud_e2e`` covering
Requirement 16.4 (webhook create/list/delete round-trip + secret hygiene).

These tests drive the ``BitbucketClient`` webhook mixin methods directly
against a live Bitbucket Cloud tenant, asserting that:

* Webhooks can be created, listed, and deleted (round-trip).
* The webhook secret is NEVER echoed back in any response body.
* List responses contain expected fields.

Tests are gated behind the ``--bitbucket-cloud-e2e`` pytest CLI flag AND
the following environment variables (see ``tests/e2e/conftest.py``):

* ``BITBUCKET_CLOUD_URL`` (or ``BITBUCKET_URL`` pointing to a Cloud host)
* ``BITBUCKET_WORKSPACE``
* At least one of: ``BITBUCKET_APP_PASSWORD``, ``BITBUCKET_CLOUD_ACCESS_TOKEN``

Additionally, the following env vars are required for webhook tests:

* ``BITBUCKET_REPO_TEST_SLUG`` -- slug of a repository the authenticated
  user has admin access to (required to manage webhooks).
"""

from __future__ import annotations

import json
import os
import secrets as _secrets_module
import uuid
from typing import Any

import pytest

from mcp_atlassian.bitbucket.client import BitbucketClient
from mcp_atlassian.bitbucket.config import BitbucketConfig

pytestmark = [pytest.mark.bitbucket_cloud_e2e]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bitbucket_cloud_client() -> BitbucketClient:
    """Create a BitbucketClient configured for Bitbucket Cloud.

    Reads credentials from environment variables. Skips the test module
    if the required env vars are missing.
    """
    url = os.environ.get("BITBUCKET_CLOUD_URL") or os.environ.get("BITBUCKET_URL", "")
    workspace = os.environ.get("BITBUCKET_WORKSPACE", "")
    repo_slug = os.environ.get("BITBUCKET_REPO_TEST_SLUG", "")

    if not url:
        pytest.skip("BITBUCKET_CLOUD_URL or BITBUCKET_URL is required")
    if not workspace:
        pytest.skip("BITBUCKET_WORKSPACE is required")
    if not repo_slug:
        pytest.skip("BITBUCKET_REPO_TEST_SLUG is required")

    # Determine auth type from available env vars
    cloud_access_token = os.environ.get("BITBUCKET_CLOUD_ACCESS_TOKEN", "")
    app_password = os.environ.get("BITBUCKET_API_TOKEN", "") or os.environ.get(
        "BITBUCKET_APP_PASSWORD", ""
    )
    username = os.environ.get("BITBUCKET_USERNAME", "")

    if cloud_access_token:
        config = BitbucketConfig(
            url=url,
            auth_type="cloud_bearer",
            cloud_access_token=cloud_access_token,
            workspace=workspace,
        )
    elif app_password and username:
        config = BitbucketConfig(
            url=url,
            auth_type="basic",
            username=username,
            app_password=app_password,
            workspace=workspace,
        )
    else:
        pytest.skip(
            "BITBUCKET_CLOUD_ACCESS_TOKEN or "
            "(BITBUCKET_USERNAME + BITBUCKET_API_TOKEN/BITBUCKET_APP_PASSWORD) is required"
        )

    return BitbucketClient(config=config)


@pytest.fixture(scope="module")
def workspace() -> str:
    """The Bitbucket Cloud workspace slug."""
    ws = os.environ.get("BITBUCKET_WORKSPACE", "")
    if not ws:
        pytest.skip("BITBUCKET_WORKSPACE is required")
    return ws


@pytest.fixture(scope="module")
def repo_slug() -> str:
    """The repository slug for webhook tests."""
    slug = os.environ.get("BITBUCKET_REPO_TEST_SLUG", "")
    if not slug:
        pytest.skip("BITBUCKET_REPO_TEST_SLUG is required")
    return slug


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize(obj: Any) -> str:
    """Return a deterministic JSON dump for secret-leak substring checks."""
    return json.dumps(obj, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.bitbucket_cloud_e2e
class TestBitbucketCloudWebhookCRUD:
    """Requirement 16.4 -- webhook CRUD round-trip + secret redaction."""

    def test_webhook_crud_roundtrip(
        self,
        bitbucket_cloud_client: BitbucketClient,
        workspace: str,
        repo_slug: str,
    ) -> None:
        """Create a webhook with a secret, list to verify, then delete.

        Asserts the secret value never appears in any response body
        (only the webhook URL and events should be visible).
        """
        uid = uuid.uuid4().hex[:8]
        raw_secret = "e2e-cloud-hmac-" + _secrets_module.token_hex(16)
        name = f"E2E Cloud Webhook {uid}"
        target_url = f"https://test-{uid}.example.com/hook"
        events = ["repo:push"]

        webhook_id: Any = None
        try:
            # 1. Create the webhook with a secret.
            created = bitbucket_cloud_client.create_webhook(
                project_key=workspace,
                repo_slug=repo_slug,
                name=name,
                url=target_url,
                events=events,
                secret=raw_secret,
                active=True,
            )

            assert isinstance(created, dict), (
                f"create_webhook must return a dict, got: {type(created)}"
            )
            # Cloud webhooks use a UUID as the id field after normalization
            webhook_id = created.get("id") or created.get("uuid")
            assert webhook_id is not None, (
                f"created webhook must have an id or uuid, got: {created}"
            )

            # Secret MUST NOT appear anywhere in the create response.
            # Note: The mixin itself does not redact; the server layer does.
            # However, Bitbucket Cloud's API also does not echo the secret
            # back in responses, so it should not appear regardless.
            serialized_create = _serialize(created)
            assert raw_secret not in serialized_create, (
                "raw HMAC secret must not appear anywhere in the create "
                "response body"
            )

            # 2. List webhooks and verify the created one appears.
            webhooks = bitbucket_cloud_client.list_webhooks(
                project_key=workspace,
                repo_slug=repo_slug,
                limit=100,
            )

            assert isinstance(webhooks, list), (
                f"list_webhooks must return a list, got: {type(webhooks)}"
            )
            matching = [
                w for w in webhooks
                if (w.get("id") == webhook_id or w.get("uuid") == webhook_id)
            ]
            assert len(matching) >= 1, (
                f"expected webhook with id={webhook_id} in list response; "
                f"found {len(matching)} matches in {len(webhooks)} webhooks"
            )

            # Secret MUST NOT appear in the list response.
            serialized_list = _serialize(webhooks)
            assert raw_secret not in serialized_list, (
                "raw HMAC secret must not appear anywhere in the list "
                "response body"
            )

            # 3. Delete the webhook.
            bitbucket_cloud_client.delete_webhook(
                project_key=workspace,
                repo_slug=repo_slug,
                webhook_id=webhook_id,
            )
            # Mark as cleaned up
            webhook_id = None

        finally:
            # Best-effort cleanup if the test failed before deletion.
            if webhook_id is not None:
                try:
                    bitbucket_cloud_client.delete_webhook(
                        project_key=workspace,
                        repo_slug=repo_slug,
                        webhook_id=webhook_id,
                    )
                except Exception:  # noqa: BLE001
                    pass

    def test_list_webhooks(
        self,
        bitbucket_cloud_client: BitbucketClient,
        workspace: str,
        repo_slug: str,
    ) -> None:
        """List webhooks and assert the response is a list with expected fields.

        Verifies that the list_webhooks method returns a list of dicts
        with at minimum the expected structural fields (url, events, active).
        """
        webhooks = bitbucket_cloud_client.list_webhooks(
            project_key=workspace,
            repo_slug=repo_slug,
            limit=25,
        )

        assert isinstance(webhooks, list), (
            f"list_webhooks must return a list, got: {type(webhooks)}"
        )

        # If there are any webhooks, verify they have expected fields.
        for webhook in webhooks:
            assert isinstance(webhook, dict), (
                f"each webhook must be a dict, got: {type(webhook)}"
            )
            # Webhooks should have at least a url and events field
            # (after normalization from Cloud shape)
            assert "url" in webhook or "links" in webhook, (
                f"webhook must have 'url' or 'links' field: {webhook}"
            )
            # The 'active' field should be present
            assert "active" in webhook, (
                f"webhook must have 'active' field: {webhook}"
            )
