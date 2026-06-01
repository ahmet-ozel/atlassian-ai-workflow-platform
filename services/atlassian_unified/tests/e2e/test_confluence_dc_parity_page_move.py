"""Confluence DC parity tests for asynchronous page moves.

Opt-in end-to-end test marked ``pytest.mark.dc_e2e`` covering
Requirements 31.1, 31.4, and 38.1 -- the full page-move workflow
including the ``confluence_move_page`` → ``confluence_get_long_task``
poll loop when DC dispatches the operation asynchronously.

The test drives the MCP tools through an in-process FastMCP client and
asserts the structured JSON contract that agents observe. It is
configured entirely from environment variables so it can run against
any reachable Confluence DC instance:

* ``CONFLUENCE_URL`` -- base URL of the Confluence DC instance (no
  ``/wiki`` suffix).
* ``CONFLUENCE_PERSONAL_TOKEN`` -- PAT for the authenticated test user.
* ``CONFLUENCE_SPACE_TEST_KEY`` -- space key containing the test pages.
* ``CONFLUENCE_TEST_PAGE_ID`` -- content id of the page to move.
* ``CONFLUENCE_TEST_TARGET_PARENT_ID`` -- content id of the destination
  parent page. Must live in the same space as ``CONFLUENCE_TEST_PAGE_ID``
  and must NOT be an ancestor of it.

When any required env var is missing the test short-circuits with
``pytest.skip`` so the suite stays safe to collect unconditionally.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from unittest.mock import patch

import pytest
import requests
from fastmcp import Client
from fastmcp.client import FastMCPTransport
from mcp.types import CallToolResult, TextContent

from mcp_atlassian.servers import main_mcp

pytestmark = [pytest.mark.dc_e2e, pytest.mark.anyio]


# ---------------------------------------------------------------------------
# Env-var driven configuration (independent of the docker-compose fixture)
# ---------------------------------------------------------------------------


_REQUIRED_ENV = (
    "CONFLUENCE_URL",
    "CONFLUENCE_PERSONAL_TOKEN",
    "CONFLUENCE_SPACE_TEST_KEY",
    "CONFLUENCE_TEST_PAGE_ID",
    "CONFLUENCE_TEST_TARGET_PARENT_ID",
)

# Poll parameters for ``confluence_get_long_task`` -- per task spec.
_POLL_INTERVAL_SECONDS = 2.0
_POLL_TIMEOUT_SECONDS = 60.0


def _require_env() -> dict[str, str]:
    """Return the required env vars or skip the test module."""
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(
            "Missing required env vars for Confluence DC parity page-move "
            "tests: " + ", ".join(missing)
        )
    return {name: os.environ[name] for name in _REQUIRED_ENV}


@pytest.fixture
def confluence_env() -> dict[str, str]:
    """Environment variables used to configure the MCP server.

    Authenticates with PAT (``CONFLUENCE_PERSONAL_TOKEN``) since that is
    the expected DC auth for a page move. ``READ_ONLY_MODE`` is forced
    off so the write tool is allowed, and ``TOOLSETS=all`` ensures the
    page-move and long-task tools are registered.
    """
    required = _require_env()
    return {
        "CONFLUENCE_URL": required["CONFLUENCE_URL"],
        "CONFLUENCE_PERSONAL_TOKEN": required["CONFLUENCE_PERSONAL_TOKEN"],
        "READ_ONLY_MODE": "false",
        "TOOLSETS": "all",
    }


@pytest.fixture
def test_params() -> dict[str, str]:
    """Page ids and space key used by the test."""
    required = _require_env()
    return {
        "space_key": required["CONFLUENCE_SPACE_TEST_KEY"],
        "page_id": required["CONFLUENCE_TEST_PAGE_ID"],
        "target_parent_id": required["CONFLUENCE_TEST_TARGET_PARENT_ID"],
    }


@pytest.fixture
async def mcp_client(confluence_env: dict[str, str]) -> Any:
    """In-process FastMCP client connected to ``main_mcp`` against DC."""
    with patch.dict(os.environ, confluence_env, clear=False):
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


def _lookup_parent_id(confluence_url: str, pat: str, page_id: str) -> str:
    """Return the content id of ``page_id``'s immediate parent.

    Uses ``GET /rest/api/content/{page_id}?expand=ancestors`` against the
    DC REST API directly -- this matches the style of the existing e2e
    conftest helpers (see ``_find_or_create_test_page``). The last entry
    in the ``ancestors`` list is the immediate parent.
    """
    resp = requests.get(
        f"{confluence_url.rstrip('/')}/rest/api/content/{page_id}",
        params={"expand": "ancestors"},
        headers={"Authorization": f"Bearer {pat}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    ancestors = data.get("ancestors") or []
    if not ancestors:
        pytest.skip(
            f"Page {page_id!r} has no ancestors -- cannot determine an "
            "original parent to restore after the move. Provide a page "
            "with a parent via CONFLUENCE_TEST_PAGE_ID."
        )
    return str(ancestors[-1]["id"])


async def _poll_long_task_until_finished(
    client: Client, long_task_id: str
) -> dict[str, Any]:
    """Poll ``confluence_get_long_task`` until ``finished=True``.

    Polls every ``_POLL_INTERVAL_SECONDS`` seconds up to
    ``_POLL_TIMEOUT_SECONDS`` total. Returns the final status envelope
    (Req 38.1). Fails the test if the timeout expires before the task
    finishes or the tool surfaces a structured error.
    """
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    last_payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        result = await _call_tool(
            client,
            "confluence_get_long_task",
            {"long_task_id": long_task_id},
        )
        assert not result.is_error, "confluence_get_long_task raised"
        payload = _payload(result)
        last_payload = payload
        assert payload.get("success") is True, (
            f"confluence_get_long_task returned an error envelope: {payload}"
        )
        status = payload.get("status") or {}
        if status.get("finished") is True:
            return payload
        time.sleep(_POLL_INTERVAL_SECONDS)

    pytest.fail(
        f"Long task {long_task_id!r} did not finish within "
        f"{_POLL_TIMEOUT_SECONDS}s. Last payload: {last_payload}"
    )


async def _best_effort_move_back(
    client: Client, page_id: str, original_parent_id: str
) -> None:
    """Issue a best-effort move back to the original parent.

    Any failure (including a DC-reported conflict) is swallowed so
    cleanup never masks the primary assertion failure.
    """
    try:
        result = await _call_tool(
            client,
            "confluence_move_page",
            {
                "page_id": page_id,
                "target_parent_id": original_parent_id,
                "position": "append",
            },
        )
        payload = _payload(result)
        long_task_id = payload.get("long_task_id")
        if long_task_id:
            await _poll_long_task_until_finished(client, str(long_task_id))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Test -- move + (optional) long-task poll + restore
# ---------------------------------------------------------------------------


class TestConfluencePageMoveParity:
    """Req 31.1, 31.4, 38.1 -- page move with long-task poll loop."""

    @pytest.mark.anyio
    async def test_move_page_and_poll_long_task(
        self,
        mcp_client: Client,
        test_params: dict[str, str],
    ) -> None:
        """Move a page to a new parent and, if async, poll to completion.

        Verifies that:

        * ``confluence_move_page`` returns ``success=True`` (Req 31.1).
        * When DC dispatches the move asynchronously, the tool surfaces
          a ``long_task_id`` that ``confluence_get_long_task`` can poll
          every 2 seconds until ``finished=True`` within 60 seconds
          (Req 31.4, Req 38.1). The final status must report
          ``successful=True``.
        * When DC completes the move synchronously (no ``long_task_id``
          in the response), success is asserted directly (Req 31.1).

        Cleanup: the page is moved back to its original parent with a
        best-effort follow-up call so the source space is left as found.
        """
        required = _require_env()
        confluence_url = required["CONFLUENCE_URL"]
        pat = required["CONFLUENCE_PERSONAL_TOKEN"]
        page_id = test_params["page_id"]
        target_parent_id = test_params["target_parent_id"]

        # Capture the original parent BEFORE the move so cleanup can
        # restore the page's position regardless of how the move
        # ultimately rewires the tree.
        original_parent_id = _lookup_parent_id(confluence_url, pat, page_id)
        if original_parent_id == target_parent_id:
            pytest.skip(
                "CONFLUENCE_TEST_TARGET_PARENT_ID is already the current "
                "parent of CONFLUENCE_TEST_PAGE_ID -- the move would be a "
                "no-op. Provide a different target parent."
            )

        # 1. Fire the move.
        move_result = await _call_tool(
            mcp_client,
            "confluence_move_page",
            {
                "page_id": page_id,
                "target_parent_id": target_parent_id,
                "position": "append",
            },
        )
        assert not move_result.is_error, "confluence_move_page raised"
        move_payload = _payload(move_result)
        assert move_payload.get("success") is True, (
            f"expected success=True from confluence_move_page, got: "
            f"{move_payload}"
        )
        assert str(move_payload.get("page_id")) == page_id
        assert str(move_payload.get("target_parent_id")) == target_parent_id

        long_task_id = move_payload.get("long_task_id")

        try:
            # 2. If DC dispatched async, poll the long-task endpoint
            #    (Req 31.4, Req 38.1). Otherwise the move is already
            #    complete (Req 31.1 synchronous path).
            if long_task_id:
                final_payload = await _poll_long_task_until_finished(
                    mcp_client, str(long_task_id)
                )
                status = final_payload["status"]
                assert status.get("finished") is True, status
                # ``successful`` is only meaningful once ``finished`` is
                # true, per the DC contract documented on
                # ``LongTasksMixin.get_long_task``.
                assert status.get("successful") is True, (
                    f"long task finished but was not successful: {status}"
                )
                assert str(final_payload.get("long_task_id")) == str(
                    long_task_id
                )
            else:
                # Synchronous move -- success is already asserted above.
                # Nothing else to verify on this path.
                assert move_payload.get("long_task_id") is None
        finally:
            # 3. Best-effort restore: move the page back to its original
            #    parent so the instance is left as we found it.
            await _best_effort_move_back(
                mcp_client, page_id, original_parent_id
            )
