"""Bitbucket DC parity tests for cherry-pick clean and conflict cases.

Opt-in end-to-end tests marked ``pytest.mark.dc_e2e`` covering
Requirements 13.1-13.3 (cherry-pick success with resulting commit hash
surfaced in the Reversible Receipt, and structured
``cherry_pick_conflict`` error on a 409 conflict response).

These tests drive the MCP tool ``bitbucket_cherry_pick_commit`` through
a FastMCP in-process client, asserting the structured JSON contract
that agents observe. Tests are configured entirely from environment
variables so they can run against any reachable Bitbucket DC 5.4+
instance:

* ``BITBUCKET_URL`` -- base URL of the Bitbucket DC instance.
* ``BITBUCKET_PERSONAL_TOKEN`` -- PAT for the authenticated test user.
  Must grant write permission on the target repository (required to
  create the resulting commit on the target branch).
* ``BITBUCKET_PROJECT_TEST_KEY`` -- project key containing the test
  repository.
* ``BITBUCKET_REPO_TEST_SLUG`` -- slug of the repository the PAT can
  cherry-pick into.

Optional, per-test configuration:

* ``BITBUCKET_TEST_SOURCE_COMMIT`` / ``BITBUCKET_TEST_TARGET_BRANCH`` --
  a commit hash and a target branch that are known to apply cleanly
  (no overlapping edits). When unset, sensible fallbacks of ``HEAD``
  and ``main`` are used. If the cherry-pick fails for any reason
  (conflict, missing branch, non-fast-forward), the clean-case test
  is skipped rather than failed so the parity check does not become
  a lottery on shared DC instances.
* ``BITBUCKET_TEST_CONFLICTING_COMMIT`` /
  ``BITBUCKET_TEST_CONFLICTING_TARGET_BRANCH`` -- a commit hash and a
  target branch that are known to conflict. When either is unset the
  conflict-case test is skipped.

All tests short-circuit with ``pytest.skip`` when the required env vars
are missing so the suite is safe to collect unconditionally. The
``dc_e2e`` marker additionally gates execution behind the ``--dc-e2e``
pytest CLI flag registered in ``tests/e2e/conftest.py``.
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
            "Missing required env vars for Bitbucket DC parity cherry-pick "
            "tests: " + ", ".join(missing)
        )
    return {name: os.environ[name] for name in _REQUIRED_ENV}


@pytest.fixture
def bitbucket_env() -> dict[str, str]:
    """Environment variables used to configure the MCP server.

    Authenticates with PAT (``BITBUCKET_PERSONAL_TOKEN``) since that is
    the expected DC auth for cherry-pick. ``READ_ONLY_MODE`` is forced
    off to allow the write call, and ``TOOLSETS=all`` ensures
    ``bitbucket_cherry_pick_commit`` (tagged
    ``toolset:bitbucket_commits``) is registered.
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
    """Repository slug that cherry-picks will be applied to."""
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


# ---------------------------------------------------------------------------
# Test 1 -- clean cherry-pick surfaces the resulting commit in the receipt
# ---------------------------------------------------------------------------


class TestBitbucketCherryPickClean:
    """Req 13.1, 13.3 -- clean cherry-pick happy path + receipt object_id."""

    @pytest.mark.anyio
    async def test_clean_cherry_pick_returns_new_commit_hash_in_receipt(
        self,
        mcp_client: Client,
        project_key: str,
        repo_slug: str,
    ) -> None:
        """Clean cherry-pick yields ``success=True`` and a receipt with the
        new commit's hash as ``object_id``.

        Verifies that:

        * ``bitbucket_cherry_pick_commit`` returns ``success=True`` when
          the source commit applies cleanly onto the target branch
          (Req 13.1).
        * The response carries a Reversible Receipt whose ``object_id``
          is a non-empty string containing the resulting commit hash on
          the target branch (Req 13.3).
        * Because cherry-pick rewrites history and is not retractable in
          a single call, ``inverse_tool`` and ``inverse_args`` are
          ``None`` and the ``note`` explains the non-retractable nature.

        The source commit and target branch are driven by
        ``BITBUCKET_TEST_SOURCE_COMMIT`` and
        ``BITBUCKET_TEST_TARGET_BRANCH``; when unset, ``HEAD`` and
        ``main`` are used as conservative fallbacks. If the DC responds
        with a conflict (for example because the commit is already on
        the target branch) the test is skipped rather than failed so
        the parity check does not become a lottery on shared DC
        instances.
        """
        source_commit = os.environ.get(
            "BITBUCKET_TEST_SOURCE_COMMIT", "HEAD"
        )
        target_branch = os.environ.get(
            "BITBUCKET_TEST_TARGET_BRANCH", "main"
        )

        result = await _call_tool(
            mcp_client,
            "bitbucket_cherry_pick_commit",
            {
                "project_key": project_key,
                "repo_slug": repo_slug,
                "source_commit": source_commit,
                "target_branch": target_branch,
            },
        )
        assert not result.is_error, (
            "bitbucket_cherry_pick_commit raised an MCP-level error"
        )
        payload = _payload(result)

        # A conflict response here means the configured (source, target)
        # pair is not actually clean on this DC; skip rather than fail.
        if (
            payload.get("success") is False
            and payload.get("error_code") == "cherry_pick_conflict"
        ):
            pytest.skip(
                "Configured BITBUCKET_TEST_SOURCE_COMMIT / "
                "BITBUCKET_TEST_TARGET_BRANCH produced a conflict on this "
                "DC instance; pick a known-clean pair for the clean-case "
                "test."
            )

        assert payload.get("success") is True, (
            f"expected success=True for a clean cherry-pick; got: {payload}"
        )

        commit = payload.get("commit") or {}
        assert isinstance(commit, dict), (
            f"response must include a 'commit' object; got: {payload!r}"
        )
        new_commit_id = commit.get("id")
        assert isinstance(new_commit_id, str) and new_commit_id, (
            f"commit.id must be a non-empty string with the new commit "
            f"hash; got: {new_commit_id!r}"
        )

        receipt = payload.get("receipt")
        assert isinstance(receipt, dict), (
            f"clean cherry-pick response must carry a 'receipt' dict; "
            f"got: {payload!r}"
        )
        object_id = receipt.get("object_id")
        assert isinstance(object_id, str) and object_id, (
            f"receipt.object_id must be a non-empty string containing the "
            f"resulting commit hash; got: {object_id!r}"
        )
        assert object_id == new_commit_id, (
            f"receipt.object_id must echo the commit.id of the new commit; "
            f"got receipt.object_id={object_id!r}, commit.id={new_commit_id!r}"
        )

        # Cherry-pick is not one-call-reversible; the receipt must make
        # that explicit and carry a human-readable note.
        assert receipt.get("inverse_tool") is None, (
            f"cherry-pick receipt.inverse_tool must be None (not "
            f"retractable); got: {receipt.get('inverse_tool')!r}"
        )
        assert receipt.get("inverse_args") is None, (
            f"cherry-pick receipt.inverse_args must be None (not "
            f"retractable); got: {receipt.get('inverse_args')!r}"
        )
        note = receipt.get("note") or ""
        assert isinstance(note, str) and note, (
            f"cherry-pick receipt.note must be a non-empty string "
            f"explaining the non-retractable nature; got: {note!r}"
        )


# ---------------------------------------------------------------------------
# Test 2 -- conflicting cherry-pick returns structured conflict error
# ---------------------------------------------------------------------------


class TestBitbucketCherryPickConflict:
    """Req 13.2 -- ``cherry_pick_conflict`` error with conflicting paths."""

    @pytest.mark.anyio
    async def test_conflicting_cherry_pick_returns_structured_conflict(
        self,
        mcp_client: Client,
        project_key: str,
        repo_slug: str,
    ) -> None:
        """Conflict response yields a structured ``cherry_pick_conflict``.

        Verifies that when Bitbucket responds with HTTP 409 and a body
        whose ``errors[].conflicts`` list is non-empty, the tool:

        * Returns ``success=False`` (Req 13.2).
        * Sets ``error_code`` to ``"cherry_pick_conflict"`` (Req 13.2).
        * Surfaces the conflicting paths as a non-empty list under
          ``details.conflicts`` (Req 13.2).

        The source commit and target branch are driven by
        ``BITBUCKET_TEST_CONFLICTING_COMMIT`` and
        ``BITBUCKET_TEST_CONFLICTING_TARGET_BRANCH``. Without them this
        test is skipped -- conflicts depend on repository history and
        cannot be synthesised from generic fallbacks.
        """
        conflicting_commit = os.environ.get(
            "BITBUCKET_TEST_CONFLICTING_COMMIT"
        )
        conflicting_target_branch = os.environ.get(
            "BITBUCKET_TEST_CONFLICTING_TARGET_BRANCH"
        )
        if not conflicting_commit or not conflicting_target_branch:
            pytest.skip(
                "Missing env vars for Bitbucket DC cherry-pick conflict "
                "test: BITBUCKET_TEST_CONFLICTING_COMMIT and "
                "BITBUCKET_TEST_CONFLICTING_TARGET_BRANCH must both be "
                "set to a commit hash and target branch that are known "
                "to conflict on this DC instance."
            )

        result = await _call_tool(
            mcp_client,
            "bitbucket_cherry_pick_commit",
            {
                "project_key": project_key,
                "repo_slug": repo_slug,
                "source_commit": conflicting_commit,
                "target_branch": conflicting_target_branch,
            },
        )
        assert not result.is_error, (
            "bitbucket_cherry_pick_commit raised an MCP-level error; the "
            "tool must translate 409 conflicts into a structured payload"
        )
        payload = _payload(result)

        assert payload.get("success") is False, (
            f"expected success=False for a conflicting cherry-pick; "
            f"got: {payload}"
        )
        assert payload.get("error_code") == "cherry_pick_conflict", (
            f"expected error_code='cherry_pick_conflict'; got: "
            f"{payload.get('error_code')!r}"
        )

        details = payload.get("details") or {}
        assert isinstance(details, dict), (
            f"conflict response must carry a 'details' dict; got: {payload!r}"
        )
        conflicts = details.get("conflicts")
        assert isinstance(conflicts, list) and conflicts, (
            f"details.conflicts must be a non-empty list of the paths "
            f"reported by Bitbucket; got: {conflicts!r}"
        )
