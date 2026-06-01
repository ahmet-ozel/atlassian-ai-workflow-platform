"""Tests for CommitCommentsMixin and the Bitbucket commit-comment server tools.

These tests cover Requirement 8.5 from the ``atlassian-dc-tool-parity``
spec:

* ``CommitCommentsMixin.delete_commit_comment`` translates an HTTP 401
  or 403 response from Bitbucket into :class:`NotCommentAuthorError`
  so the server-tool layer can surface a structured
  ``not_comment_author`` error without inspecting HTTP status codes
  from inside the tool function.
* ``bitbucket_delete_commit_comment`` (the server tool) returns the
  structured ``{"success": False, "error_code": "not_comment_author",
  ...}`` envelope when the mixin raises ``NotCommentAuthorError``.
* The happy path — a 204/200 from Bitbucket — returns
  ``{"success": True, ..., "deleted": True}`` with no error envelope.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from requests.exceptions import HTTPError

from mcp_atlassian.bitbucket.commit_comments import (
    CommitCommentsMixin,
    NotCommentAuthorError,
)
from mcp_atlassian.bitbucket.config import BitbucketConfig
from mcp_atlassian.utils.dc_guards import ERROR_CODES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _http_error(status: int) -> HTTPError:
    """Build an ``HTTPError`` carrying a response with ``status`` status code.

    The mixin inspects ``error.response.status_code`` to decide whether
    to translate the failure into ``NotCommentAuthorError``. The helper
    keeps the test setup compact and mirrors the pattern used by the
    sibling ``test_required_builds.py`` module.
    """
    response = MagicMock()
    response.status_code = status
    return HTTPError(response=response)


# ---------------------------------------------------------------------------
# Mixin fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bitbucket_config() -> BitbucketConfig:
    """Minimal BitbucketConfig suitable for instantiating the mixin."""
    return BitbucketConfig(
        url="https://bb.example.com",
        auth_type="pat",
        personal_token="test-pat",
    )


@pytest.fixture
def commit_comments_mixin(bitbucket_config):
    """Create a ``CommitCommentsMixin`` with mocked Bitbucket transport.

    The mixin inherits from :class:`BitbucketClient`, whose constructor
    builds an ``atlassian.Bitbucket`` session. We patch that constructor
    so no real HTTP session is created, then replace
    ``mixin.bitbucket`` with a fresh ``MagicMock`` the tests drive.
    """
    with patch("mcp_atlassian.bitbucket.client.Bitbucket") as mock_bitbucket_class:
        mock_bitbucket_class.return_value = MagicMock()
        mixin = CommitCommentsMixin(config=bitbucket_config)
        mixin.bitbucket = MagicMock()
        return mixin


# ---------------------------------------------------------------------------
# Mixin-level tests — Requirement 8.5
# ---------------------------------------------------------------------------


class TestDeleteCommitCommentNotCommentAuthor:
    """Unit tests for ``CommitCommentsMixin.delete_commit_comment`` (Req 8.5)."""

    def test_not_comment_author_is_in_error_codes_allowlist(self):
        # The structured error code emitted by the tool layer when
        # NotCommentAuthorError bubbles up MUST be a documented member of
        # the shared ERROR_CODES allowlist. Guarding it here catches
        # accidental removal from the allowlist during refactors.
        assert "not_comment_author" in ERROR_CODES

    def test_happy_path_returns_none_on_204(self, commit_comments_mixin):
        # Bitbucket returns 204 No Content on a successful delete, which
        # the atlassian-python client surfaces as ``None``. The mixin
        # must not raise in that case.
        commit_comments_mixin.bitbucket.delete.return_value = None

        result = commit_comments_mixin.delete_commit_comment(
            "PROJ", "repo", "abc123", 42, version=1
        )

        assert result is None
        commit_comments_mixin.bitbucket.delete.assert_called_once_with(
            "/rest/api/latest/projects/PROJ/repos/repo/commits/abc123/comments/42",
            params={"version": 1},
        )

    def test_happy_path_accepts_200_body(self, commit_comments_mixin):
        # Some Bitbucket DC versions return a 200 with an empty body on
        # delete; the mixin is agnostic to the return value because
        # ``delete_commit_comment`` is declared to return ``None``.
        commit_comments_mixin.bitbucket.delete.return_value = {}

        # Should not raise.
        commit_comments_mixin.delete_commit_comment(
            "PROJ", "repo", "abc123", 42, version=1
        )

        commit_comments_mixin.bitbucket.delete.assert_called_once()

    def test_401_raises_not_comment_author(self, commit_comments_mixin):
        # Bitbucket returns 401 when the authenticated user is not the
        # comment author and the instance is configured to treat the
        # permission failure as unauthenticated.
        commit_comments_mixin.bitbucket.delete.side_effect = _http_error(401)

        with pytest.raises(NotCommentAuthorError) as exc_info:
            commit_comments_mixin.delete_commit_comment(
                "PROJ", "repo", "abc123", 42, version=1
            )

        # The message mentions the HTTP status so operators can see why
        # the delete was blocked without needing raw transport access.
        assert "401" in str(exc_info.value)
        # Chained cause preserves the original HTTPError for diagnosis.
        assert isinstance(exc_info.value.__cause__, HTTPError)

    def test_403_raises_not_comment_author(self, commit_comments_mixin):
        # Bitbucket returns 403 when the authenticated user is neither
        # the comment author nor an admin. Both 401 and 403 must be
        # mapped to the same typed exception so the tool layer renders
        # them identically (Requirement 8.5).
        commit_comments_mixin.bitbucket.delete.side_effect = _http_error(403)

        with pytest.raises(NotCommentAuthorError) as exc_info:
            commit_comments_mixin.delete_commit_comment(
                "PROJ", "repo", "abc123", 42, version=1
            )

        assert "403" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, HTTPError)

    @pytest.mark.parametrize("status", [404, 409, 500, 502])
    def test_other_http_errors_propagate(self, commit_comments_mixin, status):
        # Non-401/403 failures must surface as the original HTTPError so
        # the server-tool layer can render them via its generic error
        # path instead of masquerading as ``not_comment_author``.
        err = _http_error(status)
        commit_comments_mixin.bitbucket.delete.side_effect = err

        with pytest.raises(HTTPError) as exc_info:
            commit_comments_mixin.delete_commit_comment(
                "PROJ", "repo", "abc123", 42, version=1
            )

        assert exc_info.value is err


# ---------------------------------------------------------------------------
# Server-tool tests — Requirement 8.5
# ---------------------------------------------------------------------------


class _FakeContext:
    """Minimal stand-in for :class:`fastmcp.Context` used by tool funcs."""


@pytest.fixture
def fake_ctx() -> _FakeContext:
    return _FakeContext()


@pytest.fixture
def fake_fetcher():
    """Fetcher stub exposing ``config.projects_filter`` and commit-comment methods."""
    fetcher = MagicMock()
    fetcher.is_cloud = False
    fetcher.config = SimpleNamespace(is_cloud=False, projects_filter=None)
    fetcher.delete_commit_comment.return_value = None
    return fetcher


@pytest.fixture
def patch_get_fetcher(monkeypatch, fake_fetcher):
    """Patch ``get_bitbucket_fetcher`` so tool functions return ``fake_fetcher``."""
    from mcp_atlassian.servers import bitbucket as bb_server

    async def _aget(_ctx):
        return fake_fetcher

    monkeypatch.setattr(bb_server, "get_bitbucket_fetcher", _aget)
    return fake_fetcher


@pytest.fixture
def disable_read_only(monkeypatch):
    """Ensure ``READ_ONLY_MODE`` is unset for happy-path tests."""
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)


@pytest.mark.anyio
async def test_delete_commit_comment_happy_path(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """204/200 from Bitbucket results in ``{"success": True, "deleted": True}``."""
    from mcp_atlassian.servers.bitbucket import delete_commit_comment

    # Mixin returns None on a successful delete (Bitbucket 204).
    patch_get_fetcher.delete_commit_comment.return_value = None

    result_json = await delete_commit_comment.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        commit_id="abc123",
        comment_id=42,
        version=1,
    )
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["deleted"] is True
    assert payload["comment_id"] == 42
    assert payload["commit_id"] == "abc123"
    # Happy path must NOT emit the structured error envelope.
    assert "error_code" not in payload
    patch_get_fetcher.delete_commit_comment.assert_called_once_with(
        "PROJ", "repo", "abc123", 42, version=1
    )


@pytest.mark.anyio
async def test_delete_commit_comment_401_returns_not_comment_author(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Mixin-raised ``NotCommentAuthorError`` (from HTTP 401) → structured envelope."""
    from mcp_atlassian.servers.bitbucket import delete_commit_comment

    # Simulate the mixin's 401→NotCommentAuthorError translation so this
    # test asserts the server-tool's structured-error rendering.
    patch_get_fetcher.delete_commit_comment.side_effect = NotCommentAuthorError(
        "Authenticated user is not permitted to delete commit comment 42 "
        "on abc123 (Bitbucket returned HTTP 401)."
    )

    result_json = await delete_commit_comment.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        commit_id="abc123",
        comment_id=42,
        version=1,
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "not_comment_author"
    # The raw HTTP status must not leak into the top-level error fields;
    # it is preserved only as diagnostic text inside ``details.reason``.
    assert "401" in payload["details"]["reason"]
    assert "message" in payload


@pytest.mark.anyio
async def test_delete_commit_comment_403_returns_not_comment_author(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Mixin-raised ``NotCommentAuthorError`` (from HTTP 403) → structured envelope."""
    from mcp_atlassian.servers.bitbucket import delete_commit_comment

    patch_get_fetcher.delete_commit_comment.side_effect = NotCommentAuthorError(
        "Authenticated user is not permitted to delete commit comment 42 "
        "on abc123 (Bitbucket returned HTTP 403)."
    )

    result_json = await delete_commit_comment.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        commit_id="abc123",
        comment_id=42,
        version=1,
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "not_comment_author"
    assert "403" in payload["details"]["reason"]
    assert "message" in payload


@pytest.mark.anyio
async def test_delete_commit_comment_end_to_end_401_from_mixin(
    fake_ctx, disable_read_only, monkeypatch
):
    """End-to-end: an underlying HTTP 401 from Bitbucket surfaces as
    ``not_comment_author`` without any intermediate patching of the mixin.

    This exercises the full translation chain — ``bitbucket.delete``
    raises an ``HTTPError(status=401)``, the mixin converts it to
    ``NotCommentAuthorError``, and the server tool renders the
    structured envelope.
    """
    from mcp_atlassian.servers import bitbucket as bb_server
    from mcp_atlassian.servers.bitbucket import delete_commit_comment

    # Build a real mixin instance with a mocked transport so the 401 →
    # NotCommentAuthorError translation exercises actual mixin code.
    with patch("mcp_atlassian.bitbucket.client.Bitbucket") as mock_bitbucket_class:
        mock_bitbucket_class.return_value = MagicMock()
        real_mixin = CommitCommentsMixin(
            config=BitbucketConfig(
                url="https://bb.example.com",
                auth_type="pat",
                personal_token="test-pat",
            )
        )
        real_mixin.bitbucket = MagicMock()
        real_mixin.bitbucket.delete.side_effect = _http_error(401)
        # Provide the ``config.projects_filter`` attribute the tool reads.
        real_mixin.config = SimpleNamespace(
            url="https://bb.example.com",
            auth_type="pat",
            personal_token="test-pat",
            projects_filter=None,
            is_cloud=False,
            workspace=None,
        )

    async def _aget(_ctx):
        return real_mixin

    monkeypatch.setattr(bb_server, "get_bitbucket_fetcher", _aget)

    result_json = await delete_commit_comment.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        commit_id="abc123",
        comment_id=42,
        version=1,
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "not_comment_author"
    assert "401" in payload["details"]["reason"]
    # The structured envelope must include a human-readable message so
    # the agent can explain the constraint to the operator.
    assert payload["message"]


@pytest.mark.anyio
async def test_delete_commit_comment_end_to_end_403_from_mixin(
    fake_ctx, disable_read_only, monkeypatch
):
    """End-to-end: underlying HTTP 403 → ``not_comment_author``."""
    from mcp_atlassian.servers import bitbucket as bb_server
    from mcp_atlassian.servers.bitbucket import delete_commit_comment

    with patch("mcp_atlassian.bitbucket.client.Bitbucket") as mock_bitbucket_class:
        mock_bitbucket_class.return_value = MagicMock()
        real_mixin = CommitCommentsMixin(
            config=BitbucketConfig(
                url="https://bb.example.com",
                auth_type="pat",
                personal_token="test-pat",
            )
        )
        real_mixin.bitbucket = MagicMock()
        real_mixin.bitbucket.delete.side_effect = _http_error(403)
        real_mixin.config = SimpleNamespace(
            url="https://bb.example.com",
            auth_type="pat",
            personal_token="test-pat",
            projects_filter=None,
            is_cloud=False,
            workspace=None,
        )

    async def _aget(_ctx):
        return real_mixin

    monkeypatch.setattr(bb_server, "get_bitbucket_fetcher", _aget)

    result_json = await delete_commit_comment.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        commit_id="abc123",
        comment_id=42,
        version=1,
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "not_comment_author"
    assert "403" in payload["details"]["reason"]
