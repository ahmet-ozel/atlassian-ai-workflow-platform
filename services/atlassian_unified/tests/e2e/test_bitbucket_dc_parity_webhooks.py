"""Bitbucket DC parity tests for repository webhook CRUD and secret redaction.

Opt-in end-to-end tests marked ``pytest.mark.dc_e2e`` covering
Requirements 2.1-2.5 (webhook create/list/get/delete + HMAC secret
hygiene).

These tests drive the MCP tools (``bitbucket_create_webhook``,
``bitbucket_list_webhooks``, ``bitbucket_get_webhook``,
``bitbucket_delete_webhook``) through a FastMCP in-process client,
asserting the structured JSON contract that agents observe. Tests are
configured entirely from environment variables so they can run against
any reachable Bitbucket DC 5.4+ instance:

* ``BITBUCKET_URL`` -- base URL of the Bitbucket DC instance.
* ``BITBUCKET_PERSONAL_TOKEN`` -- PAT for the authenticated test user.
  Must grant REPO_ADMIN on the target repository (required to manage
  webhooks).
* ``BITBUCKET_PROJECT_TEST_KEY`` -- project key containing the test
  repository.
* ``BITBUCKET_REPO_TEST_SLUG`` -- slug of a repository the PAT has
  ``REPO_ADMIN`` on. Webhooks will be created against this repository
  and cleaned up at test end.

All tests short-circuit with ``pytest.skip`` when the required env vars
are missing so the suite is safe to collect unconditionally. The
``dc_e2e`` marker additionally gates execution behind the ``--dc-e2e``
pytest CLI flag registered in ``tests/e2e/conftest.py``.
"""

from __future__ import annotations

import json
import os
import secrets as _secrets_module
import uuid
from typing import Any
from unittest.mock import patch

import pytest
from fastmcp import Client
from fastmcp.client import FastMCPTransport
from mcp.types import CallToolResult, TextContent

from mcp_atlassian.servers import main_mcp

pytestmark = [pytest.mark.dc_e2e, pytest.mark.anyio]


# ---------------------------------------------------------------------------
# Env-var driven configuration (independent of the docker-compose fixture)
# ---------------------------------------------------------------------------


_REQUIRED_ENV = (
    "BITBUCKET_URL",
    "BITBUCKET_PERSONAL_TOKEN",
    "BITBUCKET_PROJECT_TEST_KEY",
    "BITBUCKET_REPO_TEST_SLUG",
)


def _require_env() -> dict[str, str]:
    """Return the required env vars or skip the test module."""
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(
            "Missing required env vars for Bitbucket DC parity webhook "
            "tests: " + ", ".join(missing)
        )
    return {name: os.environ[name] for name in _REQUIRED_ENV}


@pytest.fixture
def bitbucket_env() -> dict[str, str]:
    """Environment variables used to configure the MCP server.

    Authenticates with PAT (``BITBUCKET_PERSONAL_TOKEN``) since that is
    the expected DC auth for webhook CRUD. ``READ_ONLY_MODE`` is forced
    off to allow create/delete and ``TOOLSETS=all`` enables the
    ``bitbucket_webhooks`` toolset which is opt-in by default.
    """
    required = _require_env()
    return {
        "BITBUCKET_URL": required["BITBUCKET_URL"],
        "BITBUCKET_PERSONAL_TOKEN": required["BITBUCKET_PERSONAL_TOKEN"],
        "READ_ONLY_MODE": "false",
        "TOOLSETS": "all",
    }


@pytest.fixture
def project_key() -> str:
    """Project key containing the test repository."""
    return _require_env()["BITBUCKET_PROJECT_TEST_KEY"]


@pytest.fixture
def repo_slug() -> str:
    """Repository slug that webhooks will be created against."""
    return _require_env()["BITBUCKET_REPO_TEST_SLUG"]


@pytest.fixture
async def mcp_client(bitbucket_env: dict[str, str]) -> Any:
    """In-process FastMCP client connected to ``main_mcp`` against DC."""
    with patch.dict(os.environ, bitbucket_env, clear=False):
        transport = FastMCPTransport(main_mcp)
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


async def _best_effort_delete(
    client: Client,
    project_key: str,
    repo_slug: str,
    webhook_id: int,
) -> None:
    """Issue a best-effort webhook delete; ignore any failure."""
    try:
        await _call_tool(
            client,
            "bitbucket_delete_webhook",
            {
                "project_key": project_key,
                "repo_slug": repo_slug,
                "webhook_id": webhook_id,
            },
        )
    except Exception:  # noqa: BLE001
        pass


def _serialize(obj: Any) -> str:
    """Return a deterministic JSON dump for secret-leak substring checks."""
    return json.dumps(obj, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Test -- create + list + get + delete round-trip with secret redaction
# ---------------------------------------------------------------------------


class TestBitbucketWebhookRoundtrip:
    """Req 2.1-2.5 -- webhook CRUD happy path + HMAC secret hygiene."""

    @pytest.mark.anyio
    async def test_create_list_get_delete_webhook_with_secret_redaction(
        self,
        mcp_client: Client,
        project_key: str,
        repo_slug: str,
    ) -> None:
        """Full webhook lifecycle with secret redaction at every read hop.

        Verifies that:

        * ``bitbucket_create_webhook`` returns ``success=True`` and
          surfaces the new webhook's ``id`` (Req 2.1, 2.2).
        * The create response does NOT echo the raw ``secret`` back to
          the agent -- ``configuration.secret`` is ``"[REDACTED]"`` and
          the raw secret literal is absent from the full serialized
          payload (Req 2.3, 2.4).
        * The create response carries a Reversible Receipt whose
          ``inverse_tool`` is ``bitbucket_delete_webhook`` (Req 2.5).
        * ``bitbucket_list_webhooks`` surfaces the new webhook with its
          ``configuration.secret`` redacted (Req 2.4).
        * ``bitbucket_get_webhook`` returns the same webhook with its
          ``configuration.secret`` redacted (Req 2.4).
        * ``bitbucket_delete_webhook`` succeeds and a follow-up
          ``bitbucket_get_webhook`` no longer finds the webhook (Req
          2.2).
        """
        # Generate a unique, high-entropy secret so substring checks on
        # the serialized payload are meaningful (no chance of collision
        # with any natural text the server might echo).
        uid = uuid.uuid4().hex[:8]
        raw_secret = "e2e-parity-hmac-" + _secrets_module.token_hex(16)
        name = f"E2E DC Parity Webhook {uid}"
        target_url = f"https://example.invalid/e2e-hook/{uid}"
        events = ["repo:refs_changed"]

        # 1. Create the webhook with a secret.
        create_result = await _call_tool(
            mcp_client,
            "bitbucket_create_webhook",
            {
                "project_key": project_key,
                "repo_slug": repo_slug,
                "name": name,
                "url": target_url,
                "events": json.dumps(events),
                "secret": raw_secret,
                "active": True,
            },
        )
        assert not create_result.is_error, "bitbucket_create_webhook raised"
        create_payload = _payload(create_result)
        assert create_payload["success"] is True, create_payload
        webhook_obj = create_payload["webhook"]
        webhook_id = webhook_obj.get("id")
        assert isinstance(webhook_id, int), (
            f"created webhook must expose a numeric id, got: {webhook_obj}"
        )

        # Track cleanup: set to ``None`` once we have confirmed the
        # webhook is deleted. The finally-block issues a best-effort
        # DELETE when this is still a numeric id (i.e. the test raised
        # before reaching the explicit delete step).
        cleanup_id: int | None = webhook_id
        try:
            # 2a. Secret MUST NOT be echoed anywhere in the create
            # response. Check the full serialized payload -- this
            # catches accidental leakage into unexpected fields (for
            # example ``receipt.recipient_scope`` or an audit trail
            # sub-object) in addition to the canonical
            # ``configuration.secret`` location.
            serialized_create = _serialize(create_payload)
            assert raw_secret not in serialized_create, (
                "raw HMAC secret must not appear anywhere in the create "
                "response; redaction is a precondition for returning the "
                "payload to an agent"
            )

            # 2b. Canonical redaction location: ``configuration.secret``
            # is either absent or replaced with the sentinel string.
            configuration = webhook_obj.get("configuration") or {}
            if "secret" in configuration:
                assert configuration["secret"] == "[REDACTED]", (
                    f"configuration.secret must be '[REDACTED]' in create "
                    f"response, got: {configuration['secret']!r}"
                )

            # 2c. Reversible Receipt points at the inverse delete tool
            # so the agent can undo the creation in a single call.
            receipt = create_payload.get("receipt")
            assert isinstance(receipt, dict), (
                f"create response must carry a 'receipt' dict, got: "
                f"{create_payload!r}"
            )
            assert receipt.get("inverse_tool") == "bitbucket_delete_webhook", (
                f"receipt.inverse_tool must reference the inverse delete "
                f"tool, got: {receipt!r}"
            )
            inverse_args = receipt.get("inverse_args") or {}
            assert inverse_args.get("project_key") == project_key
            assert inverse_args.get("repo_slug") == repo_slug
            assert inverse_args.get("webhook_id") == webhook_id

            # 3. List webhooks -- new webhook must appear and its
            # ``configuration.secret`` must be redacted.
            list_result = await _call_tool(
                mcp_client,
                "bitbucket_list_webhooks",
                {
                    "project_key": project_key,
                    "repo_slug": repo_slug,
                    "limit": 100,
                },
            )
            assert not list_result.is_error, "bitbucket_list_webhooks raised"
            list_payload = _payload(list_result)
            assert list_payload["success"] is True, list_payload
            webhooks = list_payload.get("webhooks") or []
            matching = [w for w in webhooks if w.get("id") == webhook_id]
            assert len(matching) == 1, (
                f"expected exactly one webhook with id={webhook_id} in "
                f"list response; got {len(matching)}: {matching!r}"
            )
            listed = matching[0]

            # Serialized list payload must not contain the raw secret.
            serialized_list = _serialize(list_payload)
            assert raw_secret not in serialized_list, (
                "raw HMAC secret must not appear anywhere in the list "
                "response"
            )
            listed_configuration = listed.get("configuration") or {}
            if "secret" in listed_configuration:
                assert listed_configuration["secret"] == "[REDACTED]", (
                    f"configuration.secret must be '[REDACTED]' in list "
                    f"response, got: {listed_configuration['secret']!r}"
                )

            # 4. Get the single webhook by id and assert the same
            # redaction invariants.
            get_result = await _call_tool(
                mcp_client,
                "bitbucket_get_webhook",
                {
                    "project_key": project_key,
                    "repo_slug": repo_slug,
                    "webhook_id": webhook_id,
                },
            )
            assert not get_result.is_error, "bitbucket_get_webhook raised"
            get_payload = _payload(get_result)
            assert get_payload["success"] is True, get_payload
            fetched = get_payload["webhook"]
            assert fetched.get("id") == webhook_id

            serialized_get = _serialize(get_payload)
            assert raw_secret not in serialized_get, (
                "raw HMAC secret must not appear anywhere in the get "
                "response"
            )
            fetched_configuration = fetched.get("configuration") or {}
            if "secret" in fetched_configuration:
                assert fetched_configuration["secret"] == "[REDACTED]", (
                    f"configuration.secret must be '[REDACTED]' in get "
                    f"response, got: {fetched_configuration['secret']!r}"
                )

            # 5. Delete the webhook.
            delete_result = await _call_tool(
                mcp_client,
                "bitbucket_delete_webhook",
                {
                    "project_key": project_key,
                    "repo_slug": repo_slug,
                    "webhook_id": webhook_id,
                },
            )
            assert not delete_result.is_error, "bitbucket_delete_webhook raised"
            delete_payload = _payload(delete_result)
            assert delete_payload.get("success") is True, delete_payload

            # Mark cleanup as complete so the finally-block no-ops.
            cleanup_id = None
        finally:
            if cleanup_id is not None:
                await _best_effort_delete(
                    mcp_client, project_key, repo_slug, cleanup_id
                )

        # 6. Verify the webhook is gone -- follow-up get must surface
        # ``success=False`` (the server tool swallows HTTPError 404 into
        # a structured error payload).
        gone_result = await _call_tool(
            mcp_client,
            "bitbucket_get_webhook",
            {
                "project_key": project_key,
                "repo_slug": repo_slug,
                "webhook_id": webhook_id,
            },
        )
        gone_payload = _payload(gone_result)
        assert gone_payload.get("success") is False, (
            f"expected webhook {webhook_id!r} to be gone, got: "
            f"{gone_payload}"
        )
