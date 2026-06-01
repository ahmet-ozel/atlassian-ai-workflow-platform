"""Jira DC parity tests for issue archive / restore round-trip (DC 9.4+).

Opt-in end-to-end tests marked ``pytest.mark.dc_e2e`` covering
Requirements 26.1-26.4 (``jira_archive_issue`` + ``jira_restore_issue``
write-tool pair, DC 9.4+ version gate, and the reversible receipt that
references the inverse tool).

These tests drive the MCP tools (``jira_archive_issue``,
``jira_restore_issue``, ``jira_get_issue``) through a FastMCP in-process
client, asserting the structured JSON contract that agents observe.
They are configured entirely from environment variables so they can run
against any reachable Jira DC 9.4+ instance:

* ``JIRA_URL`` -- base URL of the Jira DC instance.
* ``JIRA_PERSONAL_TOKEN`` -- PAT for the authenticated test user. Must
  grant the ``Archive Issues`` and ``Restore Issues`` permissions on the
  target project (or the equivalent per-project permissions).
* ``JIRA_TEST_ISSUE_KEY`` -- key of an issue the authenticated user can
  archive and restore (for example ``E2E-1``). The issue is left in its
  original (non-archived) state after a successful run.

All tests short-circuit with ``pytest.skip`` when the required env vars
are missing so the suite is safe to collect unconditionally. An
additional version-gate skip fires when the detected Jira DC version is
earlier than 9.4 -- the archive/restore endpoints only exist on 9.4+
per Requirement 26.3.
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

from mcp_atlassian.jira import JiraFetcher
from mcp_atlassian.jira.config import JiraConfig
from mcp_atlassian.servers import main_mcp
from mcp_atlassian.utils.dc_guards import compare_dc_versions

pytestmark = [pytest.mark.dc_e2e, pytest.mark.anyio]


# ---------------------------------------------------------------------------
# Env-var driven configuration (independent of the docker-compose fixture)
# ---------------------------------------------------------------------------


_REQUIRED_ENV = ("JIRA_URL", "JIRA_PERSONAL_TOKEN", "JIRA_TEST_ISSUE_KEY")

# Minimum Jira DC version that exposes ``PUT /rest/api/2/issue/{key}/archive``
# and its ``/restore`` counterpart (Req 26.3).
_MIN_DC_VERSION = "9.4"


def _require_env() -> dict[str, str]:
    """Return the required env vars or skip the test module."""
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.skip(
            "Missing required env vars for Jira DC parity archive tests: "
            + ", ".join(missing)
        )
    return {name: os.environ[name] for name in _REQUIRED_ENV}


@pytest.fixture
def jira_env() -> dict[str, str]:
    """Environment variables used to configure the MCP server.

    Authenticates with PAT (``JIRA_PERSONAL_TOKEN``) since that is the
    expected DC auth for the archive/restore endpoints.
    ``READ_ONLY_MODE`` is forced off so the write tools are allowed and
    ``TOOLSETS=all`` ensures the ``jira_archive`` toolset is registered.
    """
    required = _require_env()
    return {
        "JIRA_URL": required["JIRA_URL"],
        "JIRA_PERSONAL_TOKEN": required["JIRA_PERSONAL_TOKEN"],
        "READ_ONLY_MODE": "false",
        "TOOLSETS": "all",
    }


@pytest.fixture
def issue_key() -> str:
    """Jira issue key that the round-trip acts on."""
    return _require_env()["JIRA_TEST_ISSUE_KEY"]


@pytest.fixture
def dc_version_guard() -> None:
    """Skip the module when the live Jira DC instance is older than 9.4.

    Builds a direct ``JiraFetcher`` from the required env vars, probes
    the cached ``_dc_version`` via ``get_dc_version()``, and compares it
    against :data:`_MIN_DC_VERSION` with ``compare_dc_versions`` -- the
    same semver-lite helper used by the in-tool ``check_dc_version``
    guard. Any probe failure (network error, unexpected payload shape)
    leaves ``_dc_version`` as ``None``; in that case we proceed and let
    the archive tool itself emit the ``dc_version_unknown`` fall-through
    if the endpoint is actually missing.
    """
    required = _require_env()
    config = JiraConfig(
        url=required["JIRA_URL"],
        auth_type="pat",
        personal_token=required["JIRA_PERSONAL_TOKEN"],
        ssl_verify=False,
    )
    fetcher = JiraFetcher(config=config)
    detected = fetcher.get_dc_version()
    # ``compare_dc_versions`` returns ``None`` when either side is
    # unparseable; only skip when we have a definitive "too old" signal.
    cmp = compare_dc_versions(detected, _MIN_DC_VERSION)
    if cmp is not None and cmp < 0:
        pytest.skip(
            f"Jira DC {detected!r} is older than the {_MIN_DC_VERSION} "
            "minimum required by the issue archive / restore tools "
            "(Req 26.3)."
        )


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


async def _best_effort_restore(client: Client, issue_key: str) -> None:
    """Issue a best-effort restore; ignore any failure.

    Runs in the ``finally`` branch of the happy-path test so a partial
    failure does not leave the target issue archived.
    """
    try:
        await _call_tool(
            client,
            "jira_restore_issue",
            {"issue_key": issue_key},
        )
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Test -- archive + restore round-trip with receipt verification
# ---------------------------------------------------------------------------


class TestJiraArchiveRestoreRoundtrip:
    """Req 26.1-26.4 -- archive / restore happy path and receipt shape."""

    @pytest.mark.anyio
    async def test_archive_and_restore_round_trip(
        self,
        dc_version_guard: None,
        mcp_client: Client,
        issue_key: str,
    ) -> None:
        """Archive a known issue, restore it, and verify the receipt.

        Verifies that:

        * The authenticated user can read the issue before the archive
          (precondition for restore verification).
        * ``jira_archive_issue`` returns ``success=True`` with
          ``archived=True`` (Req 26.1).
        * The archive response carries a Reversible Receipt whose
          ``inverse_tool`` is ``"jira_restore_issue"`` and whose
          ``inverse_args`` is exactly ``{"issue_key": issue_key}``
          (Req 26.4).
        * ``jira_restore_issue`` returns ``success=True`` with
          ``restored=True`` (Req 26.2).
        * The issue is readable again after the restore, confirming it
          was returned to its original (non-archived) state.
        """
        # 1. Precondition: confirm the issue exists and is readable as
        # the authenticated user. This baseline makes the follow-up
        # "readable again after restore" assertion meaningful.
        pre_get = await _call_tool(
            mcp_client,
            "jira_get_issue",
            {"issue_key": issue_key},
        )
        assert not pre_get.is_error, "jira_get_issue raised before archive"
        pre_payload = _payload(pre_get)
        # ``jira_get_issue`` returns the issue model directly on success;
        # it does not carry a top-level ``success`` field. A present
        # ``key`` matching the requested id is the happy-path signal.
        assert pre_payload.get("key") == issue_key, (
            f"expected pre-archive issue key={issue_key!r}, got: "
            f"{pre_payload}"
        )

        # Track whether we still need to restore in the cleanup branch.
        # Flip to ``False`` once the explicit restore step succeeds so
        # the finally-block becomes a no-op.
        needs_restore = False
        try:
            # 2. Archive the issue.
            archive_result = await _call_tool(
                mcp_client,
                "jira_archive_issue",
                {"issue_key": issue_key},
            )
            assert not archive_result.is_error, "jira_archive_issue raised"
            archive_payload = _payload(archive_result)
            assert archive_payload.get("success") is True, archive_payload
            assert archive_payload.get("issue_key") == issue_key
            assert archive_payload.get("archived") is True, (
                f"expected archived=True in archive response, got: "
                f"{archive_payload}"
            )
            # From this point on we MUST restore before the test exits.
            needs_restore = True

            # 3. Reversible-receipt shape (Req 26.4).
            receipt = archive_payload.get("receipt")
            assert isinstance(receipt, dict), (
                f"archive response must carry a 'receipt' dict, got: "
                f"{archive_payload!r}"
            )
            assert receipt.get("object_id") == issue_key, (
                f"receipt.object_id must equal the archived issue key, "
                f"got: {receipt!r}"
            )
            assert receipt.get("inverse_tool") == "jira_restore_issue", (
                f"receipt.inverse_tool must be 'jira_restore_issue', "
                f"got: {receipt!r}"
            )
            assert receipt.get("inverse_args") == {"issue_key": issue_key}, (
                f"receipt.inverse_args must be exactly "
                f"{{'issue_key': {issue_key!r}}}, got: {receipt!r}"
            )
            # Non-broadcast, retractable effect -- ``note`` and
            # ``recipient_scope`` are always emitted with ``None``
            # values by ``build_receipt``.
            assert "note" in receipt and receipt["note"] is None
            assert (
                "recipient_scope" in receipt
                and receipt["recipient_scope"] is None
            )

            # 4. Restore the issue.
            restore_result = await _call_tool(
                mcp_client,
                "jira_restore_issue",
                {"issue_key": issue_key},
            )
            assert not restore_result.is_error, "jira_restore_issue raised"
            restore_payload = _payload(restore_result)
            assert restore_payload.get("success") is True, restore_payload
            assert restore_payload.get("issue_key") == issue_key
            assert restore_payload.get("restored") is True, (
                f"expected restored=True in restore response, got: "
                f"{restore_payload}"
            )
            # Restore succeeded -- cleanup is complete.
            needs_restore = False
        finally:
            if needs_restore:
                await _best_effort_restore(mcp_client, issue_key)

        # 5. Post-restore: confirm the issue is readable and reports the
        # same key, demonstrating the round-trip returned it to its
        # original state.
        post_get = await _call_tool(
            mcp_client,
            "jira_get_issue",
            {"issue_key": issue_key},
        )
        assert not post_get.is_error, "jira_get_issue raised after restore"
        post_payload = _payload(post_get)
        assert post_payload.get("key") == issue_key, (
            f"expected post-restore issue key={issue_key!r}, got: "
            f"{post_payload}"
        )
