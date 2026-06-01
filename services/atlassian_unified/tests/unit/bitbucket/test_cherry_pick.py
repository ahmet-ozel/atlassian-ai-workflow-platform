"""Tests for CherryPickMixin and the Bitbucket cherry-pick server tool.

These tests cover Requirement 13.2 from the ``atlassian-dc-tool-parity``
spec:

* ``CherryPickMixin.cherry_pick_commit`` wraps
  ``POST /rest/api/latest/projects/{k}/repos/{r}/cherry-pick`` and
  translates a 409 response carrying ``errors[].conflicts`` into
  :class:`CherryPickConflictError` so the server-tool layer can surface
  a structured ``cherry_pick_conflict`` error.
* ``bitbucket.cherry_pick_commit`` (the server tool) returns the
  structured ``{"success": False, "error_code": "cherry_pick_conflict",
  "details": {"conflicts": [...]}}`` envelope when the mixin raises
  ``CherryPickConflictError``.
* The happy path — a 200 from Bitbucket — returns
  ``{"success": True, "commit": {...}, "receipt": {...}}`` where the
  receipt's ``object_id`` carries the resulting commit hash on the
  target branch (Requirement 13.3 surfaces the hash that Req 13.2
  testing incidentally exercises on the success path).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from requests.exceptions import HTTPError

from mcp_atlassian.bitbucket.cherry_pick import (
    CherryPickConflictError,
    CherryPickMixin,
    _extract_conflicts,
)
from mcp_atlassian.bitbucket.config import BitbucketConfig
from mcp_atlassian.utils.dc_guards import ERROR_CODES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _http_error(status: int, body: object | None = ...) -> HTTPError:
    """Build an ``HTTPError`` whose ``response.status_code == status``.

    When ``body`` is provided, the mock response's ``json()`` method
    returns it; when omitted, ``json()`` raises ``ValueError`` (the real
    ``requests.Response.json`` contract for non-JSON bodies). The mixin
    walks ``error.response.status_code`` and ``error.response.json()`` to
    decide whether a 409 carries cherry-pick conflicts, so modelling the
    two failure modes separately lets the tests exercise both
    discrimination branches of ``_extract_conflicts``.
    """
    response = MagicMock()
    response.status_code = status
    if body is ...:
        response.json.side_effect = ValueError("no json body")
    else:
        response.json.return_value = body
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
def cherry_pick_mixin(bitbucket_config):
    """Create a ``CherryPickMixin`` with mocked Bitbucket transport.

    The mixin inherits from :class:`BitbucketClient`, whose constructor
    builds an ``atlassian.Bitbucket`` session. We patch that constructor
    so no real HTTP session is created, then replace ``mixin.bitbucket``
    with a fresh ``MagicMock`` the tests drive.
    """
    with patch("mcp_atlassian.bitbucket.client.Bitbucket") as mock_bitbucket_class:
        mock_bitbucket_class.return_value = MagicMock()
        mixin = CherryPickMixin(config=bitbucket_config)
        mixin.bitbucket = MagicMock()
        return mixin


# ---------------------------------------------------------------------------
# _extract_conflicts predicate
# ---------------------------------------------------------------------------


class TestExtractConflicts:
    """Unit tests for the :func:`_extract_conflicts` body walker."""

    def test_returns_conflict_entries_when_present(self):
        # Bitbucket's documented 409 body shape for cherry-pick failures.
        conflicts = [
            {"ourChange": {"path": {"toString": "a.txt"}}, "theirChange": {}},
            {"ourChange": {"path": {"toString": "b.txt"}}, "theirChange": {}},
        ]
        err = _http_error(
            409,
            body={"errors": [{"message": "conflict", "conflicts": conflicts}]},
        )

        assert _extract_conflicts(err) == conflicts

    def test_flattens_conflicts_across_multiple_error_entries(self):
        err = _http_error(
            409,
            body={
                "errors": [
                    {"message": "c1", "conflicts": [{"path": "a.txt"}]},
                    {"message": "c2", "conflicts": [{"path": "b.txt"}]},
                ]
            },
        )
        assert _extract_conflicts(err) == [{"path": "a.txt"}, {"path": "b.txt"}]

    def test_returns_empty_when_body_has_no_conflicts_key(self):
        # A 409 with a plain-error body (no ``conflicts`` entries) must
        # collapse to an empty list rather than misclassifying the
        # payload — callers rely on this to decide whether conflict
        # details are attached to the raised exception.
        err = _http_error(
            409,
            body={"errors": [{"message": "Something else broke"}]},
        )
        assert _extract_conflicts(err) == []

    def test_returns_empty_when_body_has_no_errors_key(self):
        err = _http_error(409, body={"message": "nope"})
        assert _extract_conflicts(err) == []

    def test_returns_empty_when_body_is_not_dict(self):
        err = _http_error(409, body=["not", "a", "dict"])
        assert _extract_conflicts(err) == []

    def test_returns_empty_when_body_is_not_json(self):
        # ``response.json()`` raising ValueError is the real-world
        # signal for a non-JSON body (e.g. HTML error page).
        err = _http_error(409)  # body omitted → json() raises.
        assert _extract_conflicts(err) == []

    def test_returns_empty_when_response_is_missing(self):
        # HTTPError without an attached response — transport-layer
        # failure — must not pretend to carry conflict data.
        err = HTTPError()
        assert _extract_conflicts(err) == []


# ---------------------------------------------------------------------------
# Mixin-level tests — Requirement 13.2
# ---------------------------------------------------------------------------


class TestCherryPickMixinHappyPath:
    """Happy-path coverage for ``CherryPickMixin.cherry_pick_commit``."""

    def test_posts_to_expected_url_and_body(self, cherry_pick_mixin):
        # Arrange — Bitbucket returns the new commit object on success.
        cherry_pick_mixin.bitbucket.post.return_value = {
            "id": "newsha1",
            "displayId": "newsha1"[:7],
            "message": "Apply fix",
        }

        # Act
        result = cherry_pick_mixin.cherry_pick_commit(
            "PROJ",
            "repo",
            source_commit="abc123",
            target_branch="main",
        )

        # Assert — URL, body, and returned payload.
        cherry_pick_mixin.bitbucket.post.assert_called_once()
        called_url = cherry_pick_mixin.bitbucket.post.call_args[0][0]
        assert called_url == "/rest/api/latest/projects/PROJ/repos/repo/cherry-pick"
        data = cherry_pick_mixin.bitbucket.post.call_args[1]["data"]
        assert data == {"commitId": "abc123", "destinationBranch": "main"}
        # Resulting commit hash surfaces on the ``id`` field.
        assert result["id"] == "newsha1"

    def test_forwards_optional_message_override(self, cherry_pick_mixin):
        cherry_pick_mixin.bitbucket.post.return_value = {"id": "newsha2"}

        cherry_pick_mixin.cherry_pick_commit(
            "PROJ",
            "repo",
            source_commit="abc123",
            target_branch="refs/heads/main",
            message="Custom cherry-pick message",
        )

        data = cherry_pick_mixin.bitbucket.post.call_args[1]["data"]
        assert data["message"] == "Custom cherry-pick message"
        # Full-ref target branch is forwarded verbatim; Bitbucket DC
        # accepts either short name or full ref.
        assert data["destinationBranch"] == "refs/heads/main"

    def test_omits_message_when_not_supplied(self, cherry_pick_mixin):
        # The caller did not supply ``message``; the mixin must NOT send
        # a ``message`` key at all so Bitbucket falls back to the source
        # commit's message.
        cherry_pick_mixin.bitbucket.post.return_value = {"id": "newsha3"}

        cherry_pick_mixin.cherry_pick_commit(
            "PROJ",
            "repo",
            source_commit="abc123",
            target_branch="main",
        )

        data = cherry_pick_mixin.bitbucket.post.call_args[1]["data"]
        assert "message" not in data

    def test_non_dict_response_raises_value_error(self, cherry_pick_mixin):
        # Bitbucket is expected to return a dict; anything else is a
        # transport-layer anomaly the mixin surfaces explicitly so the
        # server-tool layer never has to reason about non-dict shapes.
        cherry_pick_mixin.bitbucket.post.return_value = "not-a-dict"

        with pytest.raises(ValueError):
            cherry_pick_mixin.cherry_pick_commit(
                "PROJ",
                "repo",
                source_commit="abc123",
                target_branch="main",
            )


class TestCherryPickMixinConflict:
    """409 → ``CherryPickConflictError`` translation (Requirement 13.2)."""

    def test_409_with_conflicts_raises_cherry_pick_conflict(self, cherry_pick_mixin):
        # Arrange — representative Bitbucket 409 body with conflicts.
        conflicts = [
            {
                "ourChange": {"path": {"toString": "src/a.py"}},
                "theirChange": {"path": {"toString": "src/a.py"}},
            },
            {
                "ourChange": {"path": {"toString": "README.md"}},
                "theirChange": {"path": {"toString": "README.md"}},
            },
        ]
        cherry_pick_mixin.bitbucket.post.side_effect = _http_error(
            409,
            body={
                "errors": [
                    {
                        "message": "Cherry-pick caused conflicts",
                        "conflicts": conflicts,
                    }
                ]
            },
        )

        # Act / Assert — the mixin raises CherryPickConflictError with
        # the conflict list attached.
        with pytest.raises(CherryPickConflictError) as exc_info:
            cherry_pick_mixin.cherry_pick_commit(
                "PROJ",
                "repo",
                source_commit="abc123",
                target_branch="main",
            )

        assert exc_info.value.conflicts == conflicts
        # Message identifies what was being applied where.
        assert "abc123" in str(exc_info.value)
        assert "main" in str(exc_info.value)
        # Chained cause preserves the original HTTPError for diagnosis.
        assert isinstance(exc_info.value.__cause__, HTTPError)
        # The structured error code must be part of the documented allowlist.
        assert "cherry_pick_conflict" in ERROR_CODES

    def test_409_without_conflicts_key_still_raises_but_with_empty_list(
        self, cherry_pick_mixin
    ):
        # Per the mixin's implementation, ANY 409 is mapped to
        # CherryPickConflictError — but the ``conflicts`` attribute
        # reflects only what the body actually carried. A 409 with no
        # ``errors[].conflicts`` key degrades gracefully to an empty
        # list rather than misrepresenting the upstream payload; the
        # server-tool layer echoes this list under ``details.conflicts``
        # so operators can see the discrimination downstream.
        cherry_pick_mixin.bitbucket.post.side_effect = _http_error(
            409,
            body={"errors": [{"message": "409 with no conflict details"}]},
        )

        with pytest.raises(CherryPickConflictError) as exc_info:
            cherry_pick_mixin.cherry_pick_commit(
                "PROJ",
                "repo",
                source_commit="abc123",
                target_branch="main",
            )

        # No conflicts echoed — the body carried none.
        assert exc_info.value.conflicts == []

    def test_409_with_non_json_body_yields_empty_conflicts(self, cherry_pick_mixin):
        # Real-world failure: an infra error page returned as HTML with
        # a 409 status. The mixin must not crash walking the body; the
        # conflicts list degrades to empty.
        cherry_pick_mixin.bitbucket.post.side_effect = _http_error(409)  # no body

        with pytest.raises(CherryPickConflictError) as exc_info:
            cherry_pick_mixin.cherry_pick_commit(
                "PROJ",
                "repo",
                source_commit="abc123",
                target_branch="main",
            )

        assert exc_info.value.conflicts == []

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 502])
    def test_non_409_http_errors_propagate_unchanged(
        self, cherry_pick_mixin, status
    ):
        # Non-409 failures must surface as the original HTTPError so the
        # server-tool layer renders them via its generic error path
        # rather than masquerading as ``cherry_pick_conflict``.
        err = _http_error(status)
        cherry_pick_mixin.bitbucket.post.side_effect = err

        with pytest.raises(HTTPError) as exc_info:
            cherry_pick_mixin.cherry_pick_commit(
                "PROJ",
                "repo",
                source_commit="abc123",
                target_branch="main",
            )

        assert exc_info.value is err

    def test_http_error_without_response_propagates(self, cherry_pick_mixin):
        # HTTPError without an attached response (transport-layer
        # failure) must not be coerced into cherry_pick_conflict.
        err = HTTPError("connection dropped")
        err.response = None
        cherry_pick_mixin.bitbucket.post.side_effect = err

        with pytest.raises(HTTPError, match="connection dropped"):
            cherry_pick_mixin.cherry_pick_commit(
                "PROJ",
                "repo",
                source_commit="abc123",
                target_branch="main",
            )


# ---------------------------------------------------------------------------
# Server-tool tests — Requirement 13.2 / 13.3
# ---------------------------------------------------------------------------


class _FakeContext:
    """Minimal stand-in for :class:`fastmcp.Context` used by tool funcs."""


@pytest.fixture
def fake_ctx() -> _FakeContext:
    return _FakeContext()


@pytest.fixture
def fake_fetcher():
    """Fetcher stub exposing ``config.projects_filter`` and cherry_pick_commit."""
    fetcher = MagicMock()
    fetcher.is_cloud = False
    fetcher.config = SimpleNamespace(is_cloud=False, projects_filter=None)
    fetcher.cherry_pick_commit.return_value = {
        "id": "newsha1",
        "displayId": "newsha1"[:7],
        "message": "Apply fix",
    }
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
async def test_cherry_pick_commit_happy_path(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """200 from Bitbucket → ``success=True`` with receipt carrying the new hash."""
    from mcp_atlassian.servers.bitbucket import cherry_pick_commit

    result_json = await cherry_pick_commit.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        source_commit="abc123",
        target_branch="main",
    )
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["commit"]["id"] == "newsha1"
    # Req 13.3 — receipt surfaces the resulting commit hash on the
    # target branch as its ``object_id`` field.
    assert payload["receipt"]["object_id"] == "newsha1"
    # Cherry-pick is not one-call-reversible; the receipt reflects that.
    assert payload["receipt"]["inverse_tool"] is None
    assert payload["receipt"]["inverse_args"] is None
    assert payload["receipt"]["note"]  # non-empty explanation
    assert payload["receipt"]["recipient_scope"] == {
        "source_commit": "abc123",
        "target_branch": "main",
    }
    # Happy path must NOT emit the structured error envelope.
    assert "error_code" not in payload
    patch_get_fetcher.cherry_pick_commit.assert_called_once_with(
        "PROJ",
        "repo",
        source_commit="abc123",
        target_branch="main",
        message=None,
    )


@pytest.mark.anyio
async def test_cherry_pick_commit_returns_cherry_pick_conflict_on_409(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Mixin-raised ``CherryPickConflictError`` → structured envelope (Req 13.2)."""
    from mcp_atlassian.servers.bitbucket import cherry_pick_commit

    conflicts = [
        {"ourChange": {"path": {"toString": "src/a.py"}}, "theirChange": {}},
        {"ourChange": {"path": {"toString": "README.md"}}, "theirChange": {}},
    ]
    patch_get_fetcher.cherry_pick_commit.side_effect = CherryPickConflictError(
        "Cherry-pick conflict applying abc123 onto main",
        conflicts=conflicts,
    )

    result_json = await cherry_pick_commit.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        source_commit="abc123",
        target_branch="main",
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "cherry_pick_conflict"
    # The conflicting paths are echoed under details so the agent can
    # explain which files need manual resolution.
    assert payload["details"]["conflicts"] == conflicts
    assert payload["details"]["source_commit"] == "abc123"
    assert payload["details"]["target_branch"] == "main"
    assert payload["message"]
    # Happy-path keys must not leak into the error envelope.
    assert "receipt" not in payload
    assert "commit" not in payload


@pytest.mark.anyio
async def test_cherry_pick_commit_409_without_conflicts_still_emits_error_code(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """A 409 with no ``errors[].conflicts`` still surfaces as cherry_pick_conflict,
    but ``details.conflicts`` is empty rather than a fabricated list.

    This pins the observable contract: the mixin classifies any 409 as a
    cherry-pick conflict (that is the upstream semantic of the endpoint),
    but the response faithfully reports an empty conflict list when the
    body did not carry one, so the agent can distinguish "409 with known
    conflicts" from "409 with opaque body".
    """
    from mcp_atlassian.servers.bitbucket import cherry_pick_commit

    patch_get_fetcher.cherry_pick_commit.side_effect = CherryPickConflictError(
        "Cherry-pick conflict applying abc123 onto main",
        conflicts=[],
    )

    result_json = await cherry_pick_commit.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        source_commit="abc123",
        target_branch="main",
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "cherry_pick_conflict"
    assert payload["details"]["conflicts"] == []


@pytest.mark.anyio
async def test_cherry_pick_commit_surfaces_non_409_errors_generically(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Non-409 HTTP failures must NOT be mapped to cherry_pick_conflict."""
    from mcp_atlassian.servers.bitbucket import cherry_pick_commit

    patch_get_fetcher.cherry_pick_commit.side_effect = _http_error(500)

    result_json = await cherry_pick_commit.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        source_commit="abc123",
        target_branch="main",
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    # The generic error path carries an "error" string rather than the
    # structured cherry_pick_conflict envelope.
    assert "error" in payload
    assert payload.get("error_code") != "cherry_pick_conflict"


@pytest.mark.anyio
async def test_cherry_pick_commit_blocked_in_read_only(
    fake_ctx, patch_get_fetcher, monkeypatch
):
    """``READ_ONLY_MODE=true`` short-circuits before any HTTP call."""
    from mcp_atlassian.servers.bitbucket import cherry_pick_commit

    monkeypatch.setenv("READ_ONLY_MODE", "true")

    result_json = await cherry_pick_commit.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        source_commit="abc123",
        target_branch="main",
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "read_only_mode"
    patch_get_fetcher.cherry_pick_commit.assert_not_called()


@pytest.mark.anyio
async def test_cherry_pick_commit_blocked_by_project_filter(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """``BITBUCKET_PROJECTS_FILTER`` blocks out-of-scope project keys."""
    from mcp_atlassian.servers.bitbucket import cherry_pick_commit

    patch_get_fetcher.config.projects_filter = "OTHER"

    result_json = await cherry_pick_commit.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        source_commit="abc123",
        target_branch="main",
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "filtered_out"
    patch_get_fetcher.cherry_pick_commit.assert_not_called()


@pytest.mark.anyio
async def test_cherry_pick_commit_has_expected_tags():
    """Registration parity with Requirement 13.1."""
    from mcp_atlassian.servers.bitbucket import cherry_pick_commit

    assert set(cherry_pick_commit.tags) == {
        "bitbucket",
        "write",
        "toolset:bitbucket_commits",
    }


@pytest.mark.anyio
async def test_cherry_pick_commit_end_to_end_409_from_mixin(
    fake_ctx, disable_read_only, monkeypatch
):
    """End-to-end: an underlying HTTP 409 from Bitbucket surfaces as
    ``cherry_pick_conflict`` without any intermediate patching of the mixin.

    This exercises the full translation chain — ``bitbucket.post``
    raises an ``HTTPError(status=409)`` with conflicts in the body, the
    mixin converts it to ``CherryPickConflictError``, and the server
    tool renders the structured envelope.
    """
    from mcp_atlassian.servers import bitbucket as bb_server
    from mcp_atlassian.servers.bitbucket import cherry_pick_commit

    conflicts = [{"ourChange": {"path": {"toString": "src/a.py"}}}]

    with patch("mcp_atlassian.bitbucket.client.Bitbucket") as mock_bitbucket_class:
        mock_bitbucket_class.return_value = MagicMock()
        real_mixin = CherryPickMixin(
            config=BitbucketConfig(
                url="https://bb.example.com",
                auth_type="pat",
                personal_token="test-pat",
            )
        )
        real_mixin.bitbucket = MagicMock()
        real_mixin.bitbucket.post.side_effect = _http_error(
            409,
            body={
                "errors": [
                    {
                        "message": "Cherry-pick caused conflicts",
                        "conflicts": conflicts,
                    }
                ]
            },
        )
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

    result_json = await cherry_pick_commit.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        source_commit="abc123",
        target_branch="main",
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "cherry_pick_conflict"
    assert payload["details"]["conflicts"] == conflicts
    assert payload["details"]["source_commit"] == "abc123"
    assert payload["details"]["target_branch"] == "main"
    assert payload["message"]
