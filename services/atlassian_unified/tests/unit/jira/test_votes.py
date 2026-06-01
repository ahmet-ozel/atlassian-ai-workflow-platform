"""Tests for VotesMixin and the Jira vote server tools.

These tests cover Requirement 18 from the ``atlassian-dc-tool-parity``
spec:

* ``VotesMixin`` methods (``get_issue_votes``, ``add_issue_vote``,
  ``remove_issue_vote``) produce the expected payloads and surface
  idempotent ``already_voted`` / ``not_voted`` flags.
* Server tools (``jira_get_issue_votes``, ``jira_add_issue_vote``,
  ``jira_remove_issue_vote``) apply the ``check_read_only`` →
  ``check_project_filter`` prelude with zero HTTP on reject, matching
  the ``jira_notify_issue`` pattern from task 24.2.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from requests.exceptions import HTTPError

from mcp_atlassian.jira.votes import VotesMixin


# ---------------------------------------------------------------------------
# Mixin-level tests
# ---------------------------------------------------------------------------


@pytest.fixture
def votes_mixin(jira_client):
    """Create a ``VotesMixin`` instance with mocked Jira transport."""
    mixin = VotesMixin(config=jira_client.config)
    mixin.jira = MagicMock()
    return mixin


class TestVotesMixinGet:
    """Unit tests for ``VotesMixin.get_issue_votes``."""

    def test_returns_normalized_payload(self, votes_mixin):
        votes_mixin.jira.get.return_value = {
            "votes": 3,
            "hasVoted": True,
            "voters": [{"name": "alice"}, {"name": "bob"}],
        }

        result = votes_mixin.get_issue_votes("PROJ-1")

        votes_mixin.jira.get.assert_called_once_with("rest/api/2/issue/PROJ-1/votes")
        assert result == {
            "issue_key": "PROJ-1",
            "votes": 3,
            "has_voted": True,
            "voters": [{"name": "alice"}, {"name": "bob"}],
        }

    def test_tolerates_missing_voters_list(self, votes_mixin):
        votes_mixin.jira.get.return_value = {"votes": 0, "hasVoted": False}

        result = votes_mixin.get_issue_votes("PROJ-2")

        assert result["voters"] == []
        assert result["votes"] == 0
        assert result["has_voted"] is False

    def test_rejects_non_dict_response(self, votes_mixin):
        votes_mixin.jira.get.return_value = ["unexpected"]

        with pytest.raises(ValueError, match="Unexpected response type"):
            votes_mixin.get_issue_votes("PROJ-3")


class TestVotesMixinAdd:
    """Unit tests for ``VotesMixin.add_issue_vote``."""

    def test_first_vote_returns_already_voted_false(self, votes_mixin):
        votes_mixin.jira.get.side_effect = [
            {"votes": 0, "hasVoted": False},  # pre-state
            {"votes": 1, "hasVoted": True},  # post-state
        ]
        votes_mixin.jira.post.return_value = None

        result = votes_mixin.add_issue_vote("PROJ-1")

        votes_mixin.jira.post.assert_called_once_with(
            "rest/api/2/issue/PROJ-1/votes"
        )
        assert result == {
            "issue_key": "PROJ-1",
            "already_voted": False,
            "votes": 1,
        }

    def test_repeat_vote_returns_already_voted_true(self, votes_mixin):
        votes_mixin.jira.get.side_effect = [
            {"votes": 2, "hasVoted": True},  # pre-state
            {"votes": 2, "hasVoted": True},  # post-state
        ]
        votes_mixin.jira.post.return_value = None

        result = votes_mixin.add_issue_vote("PROJ-1")

        # The POST is still issued so Jira is authoritative, but the
        # pre-state flag tells the caller the call is a no-op (Req 18.3).
        votes_mixin.jira.post.assert_called_once()
        assert result["already_voted"] is True
        assert result["votes"] == 2


class TestVotesMixinRemove:
    """Unit tests for ``VotesMixin.remove_issue_vote``."""

    def test_removes_existing_vote(self, votes_mixin):
        votes_mixin.jira.get.side_effect = [
            {"votes": 1, "hasVoted": True},  # pre-state
            {"votes": 0, "hasVoted": False},  # post-state
        ]
        votes_mixin.jira.delete.return_value = None

        result = votes_mixin.remove_issue_vote("PROJ-1")

        votes_mixin.jira.delete.assert_called_once_with(
            "rest/api/2/issue/PROJ-1/votes"
        )
        assert result == {
            "issue_key": "PROJ-1",
            "not_voted": False,
            "votes": 0,
        }

    def test_returns_not_voted_when_no_prior_vote(self, votes_mixin):
        votes_mixin.jira.get.side_effect = [
            {"votes": 0, "hasVoted": False},  # pre-state
            {"votes": 0, "hasVoted": False},  # post-state
        ]
        votes_mixin.jira.delete.return_value = None

        result = votes_mixin.remove_issue_vote("PROJ-1")

        assert result["not_voted"] is True
        assert result["votes"] == 0

    def test_tolerates_404_when_user_had_not_voted(self, votes_mixin):
        response = MagicMock()
        response.status_code = 404
        http_err = HTTPError(response=response)

        votes_mixin.jira.get.side_effect = [
            {"votes": 0, "hasVoted": False},  # pre-state
            {"votes": 0, "hasVoted": False},  # post-state
        ]
        votes_mixin.jira.delete.side_effect = http_err

        # 404 + not_voted=True is a benign no-op; should not raise.
        result = votes_mixin.remove_issue_vote("PROJ-1")

        assert result["not_voted"] is True
        assert result["votes"] == 0


# ---------------------------------------------------------------------------
# Server-tool tests
# ---------------------------------------------------------------------------


class _FakeContext:
    """Minimal stand-in for :class:`fastmcp.Context` used by tool funcs."""


@pytest.fixture
def fake_ctx() -> _FakeContext:
    return _FakeContext()


@pytest.fixture
def fake_fetcher():
    """Fetcher stub exposing ``config.projects_filter`` and vote methods."""
    fetcher = MagicMock()
    fetcher.config = SimpleNamespace(projects_filter=None)
    fetcher.get_issue_votes.return_value = {
        "issue_key": "PROJ-1",
        "votes": 0,
        "has_voted": False,
        "voters": [],
    }
    fetcher.add_issue_vote.return_value = {
        "issue_key": "PROJ-1",
        "already_voted": False,
        "votes": 1,
    }
    fetcher.remove_issue_vote.return_value = {
        "issue_key": "PROJ-1",
        "not_voted": False,
        "votes": 0,
    }
    return fetcher


@pytest.fixture
def patch_get_fetcher(monkeypatch, fake_fetcher):
    """Patch ``get_jira_fetcher`` so tool functions return ``fake_fetcher``."""
    from mcp_atlassian.servers import jira as jira_server

    async def _aget(_ctx):
        return fake_fetcher

    monkeypatch.setattr(jira_server, "get_jira_fetcher", _aget)
    return fake_fetcher


@pytest.fixture
def disable_read_only(monkeypatch):
    """Ensure ``READ_ONLY_MODE`` is unset for happy-path tests."""
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)


@pytest.mark.anyio
async def test_jira_get_issue_votes_returns_payload(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.jira import jira_get_issue_votes

    patch_get_fetcher.get_issue_votes.return_value = {
        "issue_key": "PROJ-1",
        "votes": 2,
        "has_voted": True,
        "voters": [{"name": "alice"}, {"name": "bob"}],
    }

    result_json = await jira_get_issue_votes.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["issue_key"] == "PROJ-1"
    assert payload["votes"] == 2
    assert payload["has_voted"] is True
    patch_get_fetcher.get_issue_votes.assert_called_once_with("PROJ-1")


@pytest.mark.anyio
async def test_jira_get_issue_votes_blocked_by_project_filter(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.jira import jira_get_issue_votes

    patch_get_fetcher.config.projects_filter = "ALLOWED"

    result_json = await jira_get_issue_votes.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "filtered_out"
    patch_get_fetcher.get_issue_votes.assert_not_called()


@pytest.mark.anyio
async def test_jira_add_issue_vote_happy_path(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.jira import jira_add_issue_vote

    patch_get_fetcher.add_issue_vote.return_value = {
        "issue_key": "PROJ-1",
        "already_voted": False,
        "votes": 1,
    }

    result_json = await jira_add_issue_vote.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["already_voted"] is False
    assert payload["votes"] == 1


@pytest.mark.anyio
async def test_jira_add_issue_vote_returns_already_voted(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.jira import jira_add_issue_vote

    patch_get_fetcher.add_issue_vote.return_value = {
        "issue_key": "PROJ-1",
        "already_voted": True,
        "votes": 4,
    }

    result_json = await jira_add_issue_vote.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["already_voted"] is True


@pytest.mark.anyio
async def test_jira_add_issue_vote_blocked_in_read_only(
    fake_ctx, patch_get_fetcher, monkeypatch
):
    from mcp_atlassian.servers.jira import jira_add_issue_vote

    monkeypatch.setenv("READ_ONLY_MODE", "true")

    result_json = await jira_add_issue_vote.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "read_only_mode"
    patch_get_fetcher.add_issue_vote.assert_not_called()


@pytest.mark.anyio
async def test_jira_add_issue_vote_blocked_by_project_filter(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.jira import jira_add_issue_vote

    patch_get_fetcher.config.projects_filter = "OTHER"

    result_json = await jira_add_issue_vote.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "filtered_out"
    patch_get_fetcher.add_issue_vote.assert_not_called()


@pytest.mark.anyio
async def test_jira_remove_issue_vote_happy_path(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.jira import jira_remove_issue_vote

    patch_get_fetcher.remove_issue_vote.return_value = {
        "issue_key": "PROJ-1",
        "not_voted": False,
        "votes": 2,
    }

    result_json = await jira_remove_issue_vote.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["not_voted"] is False
    assert payload["votes"] == 2


@pytest.mark.anyio
async def test_jira_remove_issue_vote_returns_not_voted(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.jira import jira_remove_issue_vote

    patch_get_fetcher.remove_issue_vote.return_value = {
        "issue_key": "PROJ-1",
        "not_voted": True,
        "votes": 0,
    }

    result_json = await jira_remove_issue_vote.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["not_voted"] is True


@pytest.mark.anyio
async def test_jira_remove_issue_vote_blocked_in_read_only(
    fake_ctx, patch_get_fetcher, monkeypatch
):
    from mcp_atlassian.servers.jira import jira_remove_issue_vote

    monkeypatch.setenv("READ_ONLY_MODE", "true")

    result_json = await jira_remove_issue_vote.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "read_only_mode"
    patch_get_fetcher.remove_issue_vote.assert_not_called()


@pytest.mark.anyio
async def test_vote_tools_have_expected_tags():
    """Ensure tool tags match Requirement 18.1 / 18.2."""
    from mcp_atlassian.servers.jira import (
        jira_add_issue_vote,
        jira_get_issue_votes,
        jira_remove_issue_vote,
    )

    read_tags = {"jira", "read", "toolset:jira_issues"}
    write_tags = {"jira", "write", "toolset:jira_issues"}

    assert set(jira_get_issue_votes.tags) == read_tags
    assert set(jira_add_issue_vote.tags) == write_tags
    assert set(jira_remove_issue_vote.tags) == write_tags
