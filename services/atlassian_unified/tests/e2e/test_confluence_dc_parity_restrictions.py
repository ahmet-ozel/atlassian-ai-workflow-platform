"""Confluence DC parity e2e: content restrictions round-trip.

Opt-in e2e (``pytest.mark.dc_e2e``) that drives the three
``toolset:confluence_restrictions`` tools end-to-end against a real
Confluence DC instance:

* :func:`confluence_list_content_restrictions`
* :func:`confluence_set_content_restrictions`
* :func:`confluence_clear_content_restrictions`

The test exercises the reversible-receipt branch that matters for
Requirements 28.1-28.4: prior_state snapshotting on set, and the
``inverse_tool`` / ``inverse_args`` payload callers use to restore the
prior restriction principals. To hit the
``inverse_tool == "confluence_set_content_restrictions"`` branch (rather
than the empty-prior-state clear branch) the test seeds an initial
restriction via the same MCP tool before running the main flow.

Configuration is via environment variables so this file stays independent
of the docker-based conftest fixtures:

* ``CONFLUENCE_URL`` (required) - base URL of the DC instance
* ``CONFLUENCE_PERSONAL_TOKEN`` (required) - PAT for the service account
* ``CONFLUENCE_TEST_PAGE_ID`` (required) - content id to restrict
* ``CONFLUENCE_TEST_GROUP`` (required) - group name used in the
  ``read_groups`` argument of the main set call
* ``CONFLUENCE_TEST_USER`` (optional) - username used to seed the
  non-empty prior state. Falls back to an ``update_groups=[group]``
  seed when not supplied.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from fastmcp import Client
from fastmcp.client import FastMCPTransport
from mcp.types import CallToolResult, TextContent

from mcp_atlassian.servers import main_mcp

pytestmark = [pytest.mark.dc_e2e, pytest.mark.anyio]


# ---------------------------------------------------------------------------
# Environment + MCP client fixtures
# ---------------------------------------------------------------------------


REQUIRED_ENV_VARS = (
    "CONFLUENCE_URL",
    "CONFLUENCE_PERSONAL_TOKEN",
    "CONFLUENCE_TEST_PAGE_ID",
    "CONFLUENCE_TEST_GROUP",
)


@pytest.fixture(scope="module")
def restrictions_env() -> dict[str, str]:
    """Collect env vars and skip the module if any required one is missing."""
    missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        pytest.skip(
            "Skipping Confluence DC parity restrictions e2e: "
            f"missing env vars {missing}"
        )

    return {
        "CONFLUENCE_URL": os.environ["CONFLUENCE_URL"],
        "CONFLUENCE_PERSONAL_TOKEN": os.environ["CONFLUENCE_PERSONAL_TOKEN"],
        "CONFLUENCE_TEST_PAGE_ID": os.environ["CONFLUENCE_TEST_PAGE_ID"],
        "CONFLUENCE_TEST_GROUP": os.environ["CONFLUENCE_TEST_GROUP"],
        "CONFLUENCE_TEST_USER": os.environ.get("CONFLUENCE_TEST_USER", ""),
    }


@pytest.fixture
def mcp_env(restrictions_env: dict[str, str]) -> dict[str, str]:
    """Environment snapshot used to spin up the MCP server for this test."""
    return {
        "CONFLUENCE_URL": restrictions_env["CONFLUENCE_URL"],
        "CONFLUENCE_PERSONAL_TOKEN": restrictions_env["CONFLUENCE_PERSONAL_TOKEN"],
        "READ_ONLY_MODE": "false",
        "TOOLSETS": "all",
    }


@pytest.fixture
async def mcp_client(mcp_env: dict[str, str]) -> AsyncIterator[Client]:
    """MCP client connected to the server configured for Confluence DC."""
    with patch.dict(os.environ, mcp_env, clear=False):
        transport = FastMCPTransport(main_mcp)
        client = Client(transport=transport)
        async with client as connected_client:
            yield connected_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call(
    client: Client, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Invoke an MCP tool and return the parsed JSON payload."""
    result: CallToolResult = await client.call_tool(tool_name, arguments)
    assert not result.is_error, f"{tool_name} returned is_error=True: {result}"
    assert result.content, f"{tool_name} returned no content"
    first = result.content[0]
    assert isinstance(first, TextContent), (
        f"{tool_name} returned non-text content: {type(first).__name__}"
    )
    payload = json.loads(first.text)
    assert isinstance(payload, dict), (
        f"{tool_name} payload is not a dict: {payload!r}"
    )
    return payload


def _extract_principals(
    restrictions_payload: dict[str, Any], operation: str
) -> tuple[list[str], list[str]]:
    """Mirror of the server-side principal extractor.

    Walks the ``restriction/byOperation`` payload shape and returns
    ``(users, groups)`` for the requested ``operation`` ("read" or
    "update"). Tolerates missing keys the same way the server helper
    does so a quirky payload cannot break the assertion chain.
    """
    results = restrictions_payload.get("results", [])
    if not isinstance(results, list):
        return [], []

    for entry in results:
        if not isinstance(entry, dict):
            continue
        if entry.get("operation") != operation:
            continue
        restrictions = entry.get("restrictions", {})
        if not isinstance(restrictions, dict):
            return [], []

        user_block = restrictions.get("user", {})
        group_block = restrictions.get("group", {})
        user_results = (
            user_block.get("results", []) if isinstance(user_block, dict) else []
        )
        group_results = (
            group_block.get("results", []) if isinstance(group_block, dict) else []
        )

        users = [
            u.get("username")
            for u in user_results
            if isinstance(u, dict) and isinstance(u.get("username"), str)
        ]
        groups = [
            g.get("name")
            for g in group_results
            if isinstance(g, dict) and isinstance(g.get("name"), str)
        ]
        return users, groups

    return [], []


def _principal_summary(
    restrictions_payload: dict[str, Any],
) -> dict[str, list[str]]:
    """Compact view of a restrictions payload for equality checks."""
    read_users, read_groups = _extract_principals(restrictions_payload, "read")
    update_users, update_groups = _extract_principals(restrictions_payload, "update")
    return {
        "read_users": sorted(read_users),
        "read_groups": sorted(read_groups),
        "update_users": sorted(update_users),
        "update_groups": sorted(update_groups),
    }


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestConfluenceDCParityRestrictions:
    """End-to-end receipt round-trip for Req 28.1-28.4."""

    @pytest.mark.anyio
    async def test_set_list_clear_roundtrip(
        self,
        mcp_client: Client,
        restrictions_env: dict[str, str],
    ) -> None:
        page_id = restrictions_env["CONFLUENCE_TEST_PAGE_ID"]
        test_group = restrictions_env["CONFLUENCE_TEST_GROUP"]
        test_user = restrictions_env["CONFLUENCE_TEST_USER"] or None

        try:
            # Reset to a known clean slate before seeding.
            await _call(
                mcp_client,
                "confluence_clear_content_restrictions",
                {"page_id": page_id},
            )

            # Seed a non-empty prior state so the main set call exercises
            # the ``inverse_tool == confluence_set_content_restrictions``
            # branch of the receipt (rather than the empty-prior clear
            # branch). Prefer a user seed when CONFLUENCE_TEST_USER is
            # provided; otherwise fall back to an update_groups seed so
            # the seed and main-set arguments do not overlap.
            if test_user:
                seed_args: dict[str, Any] = {
                    "page_id": page_id,
                    "read_users": [test_user],
                }
            else:
                seed_args = {
                    "page_id": page_id,
                    "update_groups": [test_group],
                }
            seed_response = await _call(
                mcp_client,
                "confluence_set_content_restrictions",
                seed_args,
            )
            assert seed_response.get("success") is True

            # (a) Note initial restrictions -> prior_before.
            list_before = await _call(
                mcp_client,
                "confluence_list_content_restrictions",
                {"page_id": page_id},
            )
            assert list_before.get("success") is True
            prior_before = list_before["restrictions"]
            prior_summary = _principal_summary(prior_before)

            # Sanity: the seed must have landed, otherwise the inverse_tool
            # assertion below would fall into the clear branch.
            assert prior_summary != {
                "read_users": [],
                "read_groups": [],
                "update_users": [],
                "update_groups": [],
            }, "seed step did not produce a non-empty prior state"

            # (b) Main call under test: replace restrictions with
            #     read_groups=[test_group].
            set_response = await _call(
                mcp_client,
                "confluence_set_content_restrictions",
                {"page_id": page_id, "read_groups": [test_group]},
            )
            assert set_response.get("success") is True
            assert set_response.get("page_id") == page_id

            # (c) Receipt carries the prior state snapshot.
            response_prior_state = set_response["prior_state"]
            assert _principal_summary(response_prior_state) == prior_summary, (
                "response.prior_state principal summary does not match "
                "prior_before"
            )

            receipt = set_response["receipt"]
            assert receipt["object_id"] == page_id
            assert receipt["inverse_tool"] == "confluence_set_content_restrictions"

            inverse_args = receipt["inverse_args"]
            assert isinstance(inverse_args, dict)
            assert inverse_args["page_id"] == page_id
            assert sorted(inverse_args.get("read_users") or []) == prior_summary[
                "read_users"
            ]
            assert sorted(inverse_args.get("read_groups") or []) == prior_summary[
                "read_groups"
            ]
            assert sorted(inverse_args.get("update_users") or []) == prior_summary[
                "update_users"
            ]
            assert sorted(inverse_args.get("update_groups") or []) == prior_summary[
                "update_groups"
            ]

            # (d) List now reflects read_groups=[test_group].
            list_after_set = await _call(
                mcp_client,
                "confluence_list_content_restrictions",
                {"page_id": page_id},
            )
            assert list_after_set.get("success") is True
            after_set_summary = _principal_summary(list_after_set["restrictions"])
            assert test_group in after_set_summary["read_groups"], (
                "expected test_group to appear in read_groups after set: "
                f"{after_set_summary}"
            )

            # (e) Apply the receipt's inverse -> call set again with the
            #     inverse_args so the state returns to prior_before.
            inverse_response = await _call(
                mcp_client,
                "confluence_set_content_restrictions",
                inverse_args,
            )
            assert inverse_response.get("success") is True

            # (f) Verify state matches prior_before.
            list_after_inverse = await _call(
                mcp_client,
                "confluence_list_content_restrictions",
                {"page_id": page_id},
            )
            assert list_after_inverse.get("success") is True
            assert (
                _principal_summary(list_after_inverse["restrictions"])
                == prior_summary
            ), "inverse invocation did not restore the prior principal set"
        finally:
            # (g) Cleanup via confluence_clear_content_restrictions.
            # Best-effort - do not mask a test failure with a cleanup error.
            try:
                await _call(
                    mcp_client,
                    "confluence_clear_content_restrictions",
                    {"page_id": page_id},
                )
            except AssertionError:
                pass
