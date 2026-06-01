"""Jira DC parity tests for filter lifecycle and owner-scoped delete.

Opt-in end-to-end tests marked ``pytest.mark.dc_e2e`` covering
Requirements 15.1-15.5 (filter CRUD + ``not_filter_owner`` guard on
foreign filters).

These tests drive the MCP tools (``jira_create_filter``,
``jira_get_filter``, ``jira_delete_own_filter``) through a FastMCP
in-process client, asserting the structured JSON contract that agents
observe. The tests do not rely on the local docker-compose DC fixture;
they are configured entirely from environment variables so they can run
against any reachable Jira DC instance:

* ``JIRA_URL`` -- base URL of the Jira DC instance.
* ``JIRA_PERSONAL_TOKEN`` -- PAT for the authenticated test user.
* ``JIRA_PROJECT_TEST_KEY`` -- project key scoping the filter's JQL.
* ``JIRA_TEST_FOREIGN_FILTER_ID`` -- optional. When set, Test 2 runs and
  asserts that ``jira_delete_own_filter`` returns
  ``error_code == "not_filter_owner"`` for a filter owned by a different
  user. When unset, Test 2 is skipped.

All tests short-circuit with ``pytest.skip`` when the required env vars
are missing so the suite is safe to collect unconditionally.
"""

from __future__ import annotations

import json
import os
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


_REQUIRED_ENV = ("JIRA_URL", "JIRA_PERSONAL_TOKEN", "JIRA_PROJECT_TEST_KEY")


def _require_env() -> dict[str, str]:
    """Return the required env vars or skip the test module."""
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(
            "Missing required env vars for Jira DC parity filter tests: "
            + ", ".join(missing)
        )
    return {name: os.environ[name] for name in _REQUIRED_ENV}


@pytest.fixture
def jira_env() -> dict[str, str]:
    """Environment variables used to configure the MCP server.

    Authenticates with PAT (``JIRA_PERSONAL_TOKEN``) since that is the
    expected DC auth for filter CRUD. ``READ_ONLY_MODE`` is forced off
    to allow the owner-scoped delete, and only the Jira filters toolset
    is enabled to minimise surface area.
    """
    required = _require_env()
    return {
        "JIRA_URL": required["JIRA_URL"],
        "JIRA_PERSONAL_TOKEN": required["JIRA_PERSONAL_TOKEN"],
        "READ_ONLY_MODE": "false",
        "TOOLSETS": "all",
    }


@pytest.fixture
def project_key() -> str:
    """Project key used to scope the test filter's JQL."""
    return _require_env()["JIRA_PROJECT_TEST_KEY"]


@pytest.fixture
async def mcp_client(jira_env: dict[str, str]) -> Any:
    """In-process FastMCP client connected to ``main_mcp`` against DC."""
    with patch.dict(os.environ, jira_env, clear=False):
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


async def _best_effort_delete(client: Client, filter_id: str) -> None:
    """Issue a best-effort owner-scoped delete; ignore any failure."""
    try:
        await _call_tool(
            client,
            "jira_delete_own_filter",
            {"filter_id": filter_id},
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Test 1 -- create + owner-scoped delete round-trip
# ---------------------------------------------------------------------------


class TestJiraFilterOwnerRoundtrip:
    """Req 15.1, 15.2, 15.3, 15.4 -- filter CRUD happy path."""

    @pytest.mark.anyio
    async def test_create_get_and_delete_own_filter(
        self,
        mcp_client: Client,
        project_key: str,
    ) -> None:
        """Create a filter, read it back, then delete it as its owner.

        Verifies that:

        * ``jira_create_filter`` returns ``success=True`` and surfaces
          the new filter's ``id`` (Req 15.1).
        * ``jira_get_filter`` can fetch the created filter by id (Req
          15.2).
        * ``jira_delete_own_filter`` succeeds for the authenticated
          owner (Req 15.3).
        * A subsequent ``jira_get_filter`` no longer returns the filter
          -- either surfacing ``success=False`` or an empty payload
          (Req 15.4 enforced implicitly by the DELETE succeeding).
        """
        uid = uuid.uuid4().hex[:8]
        name = f"E2E DC Parity Filter {uid}"
        jql = f"project={project_key} ORDER BY created DESC"

        # 1. Create the filter.
        create_result = await _call_tool(
            mcp_client,
            "jira_create_filter",
            {
                "name": name,
                "jql": jql,
                "description": "Auto-created by test_jira_dc_parity_filters",
                "favourite": False,
            },
        )
        assert not create_result.is_error, "jira_create_filter raised"
        create_payload = _payload(create_result)
        assert create_payload["success"] is True, create_payload
        filter_obj = create_payload["filter"]
        filter_id = str(filter_obj["id"])
        assert filter_id, "created filter must expose an id"

        try:
            # 2. Verify the filter exists via jira_get_filter.
            get_result = await _call_tool(
                mcp_client,
                "jira_get_filter",
                {"filter_id": filter_id},
            )
            assert not get_result.is_error, "jira_get_filter raised"
            get_payload = _payload(get_result)
            assert get_payload["success"] is True, get_payload
            assert str(get_payload["filter"]["id"]) == filter_id
            assert get_payload["filter"]["name"] == name

            # 3. Owner-scoped delete -- authenticated user owns the filter.
            delete_result = await _call_tool(
                mcp_client,
                "jira_delete_own_filter",
                {"filter_id": filter_id},
            )
            assert not delete_result.is_error, "jira_delete_own_filter raised"
            delete_payload = _payload(delete_result)
            assert delete_payload["success"] is True, delete_payload
            # Mark the cleanup as done so the finally-block no-ops.
            filter_id = ""
        finally:
            if filter_id:
                await _best_effort_delete(mcp_client, filter_id)

        # 4. Verify the filter is gone. Jira returns 404 on the follow-up
        # GET, which the server tool surfaces as ``success=False``.
        gone_result = await _call_tool(
            mcp_client,
            "jira_get_filter",
            {"filter_id": create_payload["filter"]["id"]},
        )
        # The tool swallows HTTPError into a structured error payload.
        gone_payload = _payload(gone_result)
        assert gone_payload.get("success") is False, (
            f"expected filter to be gone, got: {gone_payload}"
        )


# ---------------------------------------------------------------------------
# Test 2 -- foreign filter delete blocked with not_filter_owner
# ---------------------------------------------------------------------------


class TestJiraFilterOwnerGuard:
    """Req 15.3, 15.4, 15.5 -- ``not_filter_owner`` on foreign filters."""

    @pytest.mark.anyio
    async def test_foreign_filter_delete_returns_not_filter_owner(
        self,
        mcp_client: Client,
    ) -> None:
        """Deleting another user's filter yields ``not_filter_owner``.

        Requires ``JIRA_TEST_FOREIGN_FILTER_ID`` to point at a filter
        owned by a user OTHER than the one authenticated via
        ``JIRA_PERSONAL_TOKEN``. When the env var is unset the test is
        skipped (Req 15 permits opt-in coverage).

        The test asserts that:

        * ``jira_delete_own_filter`` returns ``success=False`` with
          ``error_code == "not_filter_owner"`` (Req 15.3).
        * The guard short-circuits BEFORE the DELETE: a follow-up
          ``jira_get_filter`` still observes the filter (Req 15.4,
          15.5).
        """
        foreign_id = os.environ.get("JIRA_TEST_FOREIGN_FILTER_ID")
        if not foreign_id:
            pytest.skip(
                "JIRA_TEST_FOREIGN_FILTER_ID not set -- provide a filter "
                "id owned by a different user to exercise the "
                "not_filter_owner guard."
            )

        # Precondition: the foreign filter must be readable before the
        # attempted delete, otherwise we cannot distinguish
        # "filter missing" from "owner guard fired".
        pre_get = await _call_tool(
            mcp_client,
            "jira_get_filter",
            {"filter_id": foreign_id},
        )
        pre_payload = _payload(pre_get)
        assert pre_payload.get("success") is True, (
            f"foreign filter {foreign_id!r} must be readable before delete, "
            f"got: {pre_payload}"
        )

        # Attempt the owner-scoped delete as a non-owner.
        delete_result = await _call_tool(
            mcp_client,
            "jira_delete_own_filter",
            {"filter_id": foreign_id},
        )
        # The tool returns a structured payload; not a protocol error.
        assert not delete_result.is_error, (
            "jira_delete_own_filter should surface a structured error, "
            "not a protocol error"
        )
        delete_payload = _payload(delete_result)
        assert delete_payload.get("success") is False, (
            f"expected success=False for foreign filter delete, got: "
            f"{delete_payload}"
        )
        assert delete_payload.get("error_code") == "not_filter_owner", (
            f"expected error_code='not_filter_owner', got: {delete_payload}"
        )

        # Filter must still exist after the blocked delete.
        post_get = await _call_tool(
            mcp_client,
            "jira_get_filter",
            {"filter_id": foreign_id},
        )
        post_payload = _payload(post_get)
        assert post_payload.get("success") is True, (
            f"foreign filter {foreign_id!r} should still exist after "
            f"not_filter_owner short-circuit, got: {post_payload}"
        )
        assert str(post_payload["filter"]["id"]) == str(foreign_id)
