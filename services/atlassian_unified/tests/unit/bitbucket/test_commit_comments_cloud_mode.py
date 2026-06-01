"""Cloud-branch unit tests for :class:`CommitCommentsMixin`.

These tests cover the Cloud side of the Bitbucket commit-comments mixin
introduced by task 11.1 of the ``bitbucket-cloud-dc-parity`` spec
(Requirements 11.8, 11.9, 19.1, 19.2).

For each method that carries an ``if self.is_cloud:`` branch
(``list_commit_comments``, ``add_commit_comment``, ``update_commit_comment``,
``delete_commit_comment``) one happy-path test verifies that the outbound
URL prefix matches the Cloud 2.0 template
``/2.0/repositories/{workspace}/{repo_slug}/commit/{sha}/comments[/{cid}]``
(Req 11.8, 11.9). Additional tests confirm:

* ``add_commit_comment`` ships a Cloud-shaped request body with
  ``content.raw`` and an optional ``inline`` block (Req 11.9 body shape).
* ``delete_commit_comment`` on Cloud translates HTTP 401 / 403 from the
  remote into :class:`NotCommentAuthorError` — the same typed error the
  DC branch raises — so the server-tool layer renders a single
  ``not_comment_author`` envelope regardless of mode (Req 11.9 + the
  existing Req 8.5 contract from the ``atlassian-dc-tool-parity`` spec).

The mixin's DC branches are intentionally **not** touched here — they
are locked byte-for-byte by :mod:`tests.unit.bitbucket.test_commit_comments`
and by Requirement 19.2 / 23.2. The tests below stamp ``is_cloud=True``
onto a bypassed :class:`CommitCommentsMixin` instance and inspect what
the Cloud branch does.

Test pattern (mirrors :mod:`test_branches_cloud_mode`):

* Bypass :meth:`CommitCommentsMixin.__init__` via
  :meth:`CommitCommentsMixin.__new__` to avoid the live-auth / live-HTTP
  constructor (the mixin inherits from :class:`BitbucketClient`).
* Stamp ``mixin.bitbucket = MagicMock()`` so ``get`` / ``post`` / ``put``
  / ``delete`` are driven by :class:`MagicMock`.
* Stamp a :class:`SimpleNamespace` on ``mixin.config`` with
  ``is_cloud=True``, ``workspace="my-team"``, plus the minimal URL / SSL
  attributes the :attr:`BitbucketClient.is_cloud` property reads.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from requests.exceptions import HTTPError

from mcp_atlassian.bitbucket.commit_comments import (
    CommitCommentsMixin,
    NotCommentAuthorError,
)


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cloud_commit_comments_mixin() -> CommitCommentsMixin:
    """Return a :class:`CommitCommentsMixin` instance wired for Cloud mode.

    ``CommitCommentsMixin.__new__`` bypasses
    :meth:`BitbucketClient.__init__`, so no real HTTP / auth setup runs.
    The stamped ``bitbucket`` mock stands in for the
    ``atlassian.Bitbucket`` client; the stamped ``config`` namespace
    carries just enough attributes for the
    :attr:`BitbucketClient.is_cloud` property and the Cloud branches of
    the mixin methods (``config.workspace`` in particular) to work.
    """
    mixin = CommitCommentsMixin.__new__(CommitCommentsMixin)
    mixin.bitbucket = MagicMock()
    mixin.config = SimpleNamespace(
        is_cloud=True,
        workspace="my-team",
        url="https://api.bitbucket.org",
        ssl_verify=True,
    )
    return mixin


def _http_error(status: int) -> HTTPError:
    """Build an :class:`HTTPError` whose response carries ``status``.

    The mixin inspects ``error.response.status_code`` to decide whether
    to translate the failure into :class:`NotCommentAuthorError`. The
    helper mirrors the one used in the sibling
    :mod:`tests.unit.bitbucket.test_commit_comments` module so both
    the DC and Cloud delete-auth tests are driven by identical fakes.
    """
    response = MagicMock()
    response.status_code = status
    return HTTPError(response=response)


def _cloud_comment_payload(
    cid: int,
    text: str,
    *,
    inline: dict | None = None,
) -> dict:
    """Fabricate a Cloud 2.0 commit-comment dict.

    :func:`_normalize_cloud_comment` reads ``content.raw`` into ``text``
    and ``user`` into ``author`` (via :func:`normalize_user`). Returning
    the Cloud shape here lets every test assert both the outbound URL
    and the normalized payload downstream consumers see.
    """
    payload: dict = {
        "id": cid,
        "content": {"raw": text},
        "user": {
            "account_id": "{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}",
            "display_name": "Jane Doe",
        },
    }
    if inline is not None:
        payload["inline"] = inline
    return payload


# ===========================================================================
# list_commit_comments (Req 11.8)
# ===========================================================================


class TestListCommitCommentsCloud:
    """``list_commit_comments`` Cloud branch — Requirement 11.8."""

    def test_issues_cloud_commit_comments_url(
        self, cloud_commit_comments_mixin: CommitCommentsMixin
    ) -> None:
        """Happy path: single-page Cloud envelope, verify URL prefix.

        Cloud termination is ``next=None`` (Req 7.3). Each value is
        routed through :func:`_normalize_cloud_comment` so downstream
        code sees the DC-shaped ``text`` / ``author`` fields.
        """
        cloud_commit_comments_mixin.bitbucket.get.return_value = {
            "values": [
                _cloud_comment_payload(1, "Looks good"),
                _cloud_comment_payload(2, "Nit: trailing whitespace"),
            ],
            "next": None,
            "page": 1,
            "pagelen": 25,
            "size": 2,
        }

        result = cloud_commit_comments_mixin.list_commit_comments(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123def",
        )

        cloud_commit_comments_mixin.bitbucket.get.assert_called_once()
        (called_url,), _kwargs = cloud_commit_comments_mixin.bitbucket.get.call_args
        assert (
            called_url
            == "/2.0/repositories/my-team/myrepo/commit/abc123def/comments"
        )
        # Normalized DC-shaped fields are synthesized for each comment.
        assert [c["text"] for c in result] == [
            "Looks good",
            "Nit: trailing whitespace",
        ]
        # ``author`` is produced by normalize_user and exposes DC keys.
        assert result[0]["author"]["account_id"] == (
            "{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}"
        )

    def test_uses_config_workspace_when_project_key_empty(
        self, cloud_commit_comments_mixin: CommitCommentsMixin
    ) -> None:
        """Workspace fallback (Req 2.5) routes through ``config.workspace``.

        When the caller passes an empty ``project_key`` the Cloud branch
        resolves the workspace from ``config.workspace`` and still emits
        ``/2.0/repositories/my-team/...``.
        """
        cloud_commit_comments_mixin.bitbucket.get.return_value = {
            "values": [],
            "next": None,
        }

        cloud_commit_comments_mixin.list_commit_comments(
            project_key="",
            repo_slug="r",
            commit_id="sha1",
        )

        (called_url,), _ = cloud_commit_comments_mixin.bitbucket.get.call_args
        assert called_url == "/2.0/repositories/my-team/r/commit/sha1/comments"

    def test_translates_path_filter_to_cloud_query_dsl(
        self, cloud_commit_comments_mixin: CommitCommentsMixin
    ) -> None:
        """``path`` is rewritten into Cloud's ``q=inline.path="..."`` DSL.

        Cloud does not accept DC's bare ``path`` query parameter;
        Requirement 11.8 requires the Cloud branch to target the Cloud
        endpoint which implies using the Cloud-native filter syntax.
        """
        cloud_commit_comments_mixin.bitbucket.get.return_value = {
            "values": [],
            "next": None,
        }

        cloud_commit_comments_mixin.list_commit_comments(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="sha1",
            path="src/foo.py",
        )

        _args, kwargs = cloud_commit_comments_mixin.bitbucket.get.call_args
        assert kwargs["params"]["q"] == 'inline.path="src/foo.py"'
        # The DC ``path`` parameter must not be forwarded on the Cloud branch.
        assert "path" not in kwargs["params"]


# ===========================================================================
# add_commit_comment (Req 11.9 — create)
# ===========================================================================


class TestAddCommitCommentCloud:
    """``add_commit_comment`` Cloud branch — Requirement 11.9 (create)."""

    def test_posts_cloud_comments_url_with_content_raw_body(
        self, cloud_commit_comments_mixin: CommitCommentsMixin
    ) -> None:
        """General (non-inline) comment creates ``{"content": {"raw": text}}``.

        Verifies both the outbound URL (Req 11.9) and the Cloud request
        body shape. A general comment has no ``inline`` block.
        """
        cloud_commit_comments_mixin.bitbucket.post.return_value = (
            _cloud_comment_payload(42, "Nice work")
        )

        result = cloud_commit_comments_mixin.add_commit_comment(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123",
            text="Nice work",
        )

        cloud_commit_comments_mixin.bitbucket.post.assert_called_once()
        (called_url,), kwargs = cloud_commit_comments_mixin.bitbucket.post.call_args
        assert (
            called_url
            == "/2.0/repositories/my-team/myrepo/commit/abc123/comments"
        )
        # Cloud-shaped body: content.raw holds the comment text; no DC ``text``.
        assert kwargs["data"] == {"content": {"raw": "Nice work"}}
        assert "text" not in kwargs["data"]
        assert "inline" not in kwargs["data"]
        # Response passes through _normalize_cloud_comment.
        assert result["text"] == "Nice work"
        assert result["id"] == 42

    def test_inline_comment_includes_cloud_inline_block(
        self, cloud_commit_comments_mixin: CommitCommentsMixin
    ) -> None:
        """Inline comment adds a Cloud ``inline`` block with ``path`` + ``to``.

        A DC ``line_type="ADDED"`` (or default / ``CONTEXT``) maps onto
        Cloud's new-side ``to`` axis (see ``_build_cloud_inline``). The
        request body must NOT contain any of the DC ``anchor`` fields.
        """
        cloud_commit_comments_mixin.bitbucket.post.return_value = (
            _cloud_comment_payload(
                7,
                "Please add a docstring",
                inline={"path": "src/foo.py", "to": 42},
            )
        )

        cloud_commit_comments_mixin.add_commit_comment(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123",
            text="Please add a docstring",
            path="src/foo.py",
            line=42,
            line_type="ADDED",
        )

        _args, kwargs = cloud_commit_comments_mixin.bitbucket.post.call_args
        assert kwargs["data"] == {
            "content": {"raw": "Please add a docstring"},
            "inline": {"path": "src/foo.py", "to": 42},
        }
        # DC anchor envelope must not leak onto the Cloud body.
        assert "anchor" not in kwargs["data"]

    def test_inline_removed_line_maps_to_from_axis(
        self, cloud_commit_comments_mixin: CommitCommentsMixin
    ) -> None:
        """DC ``line_type="REMOVED"`` maps to Cloud's old-side ``from`` axis.

        Requirement 11.9 — the Cloud branch must express the inline
        anchor using Cloud's native ``from`` / ``to`` discriminator.
        """
        cloud_commit_comments_mixin.bitbucket.post.return_value = (
            _cloud_comment_payload(
                8,
                "Why was this removed?",
                inline={"path": "src/foo.py", "from": 10},
            )
        )

        cloud_commit_comments_mixin.add_commit_comment(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123",
            text="Why was this removed?",
            path="src/foo.py",
            line=10,
            line_type="REMOVED",
        )

        _args, kwargs = cloud_commit_comments_mixin.bitbucket.post.call_args
        assert kwargs["data"]["inline"] == {"path": "src/foo.py", "from": 10}


# ===========================================================================
# update_commit_comment (Req 11.9 — update)
# ===========================================================================


class TestUpdateCommitCommentCloud:
    """``update_commit_comment`` Cloud branch — Requirement 11.9 (update)."""

    def test_puts_cloud_comments_cid_url_with_content_raw_body(
        self, cloud_commit_comments_mixin: CommitCommentsMixin
    ) -> None:
        """``PUT /2.0/repositories/{ws}/{slug}/commit/{sha}/comments/{cid}``.

        Verifies the outbound URL suffix (Req 11.9) and that the Cloud
        update body uses ``content.raw`` (not DC ``text``/``version``).
        Cloud commit comments have no optimistic-concurrency ``version``
        field, so the DC ``version`` argument is silently dropped.
        """
        cloud_commit_comments_mixin.bitbucket.put.return_value = (
            _cloud_comment_payload(42, "Updated body")
        )

        result = cloud_commit_comments_mixin.update_commit_comment(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123",
            comment_id=42,
            text="Updated body",
            version=3,
        )

        cloud_commit_comments_mixin.bitbucket.put.assert_called_once()
        (called_url,), kwargs = cloud_commit_comments_mixin.bitbucket.put.call_args
        assert (
            called_url
            == "/2.0/repositories/my-team/myrepo/commit/abc123/comments/42"
        )
        # Cloud body shape: content.raw replaces the comment text.
        assert kwargs["data"] == {"content": {"raw": "Updated body"}}
        # DC-specific keys must not be forwarded on the Cloud branch.
        assert "text" not in kwargs["data"]
        assert "version" not in kwargs["data"]
        # Response passes through _normalize_cloud_comment.
        assert result["text"] == "Updated body"


# ===========================================================================
# delete_commit_comment (Req 11.9 — delete + auth translation)
# ===========================================================================


class TestDeleteCommitCommentCloud:
    """``delete_commit_comment`` Cloud branch — Requirement 11.9 (delete)."""

    def test_deletes_cloud_comments_cid_url(
        self, cloud_commit_comments_mixin: CommitCommentsMixin
    ) -> None:
        """``DELETE /2.0/repositories/{ws}/{slug}/commit/{sha}/comments/{cid}``.

        The Cloud DELETE is a bare URL call — no request body, no
        ``version`` parameter (Cloud has no optimistic-concurrency
        field on commit comments).
        """
        cloud_commit_comments_mixin.bitbucket.delete.return_value = None

        result = cloud_commit_comments_mixin.delete_commit_comment(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123",
            comment_id=42,
            version=1,
        )

        # Cloud delete returns None (parallels the DC 204 No Content path).
        assert result is None
        cloud_commit_comments_mixin.bitbucket.delete.assert_called_once()
        call = cloud_commit_comments_mixin.bitbucket.delete.call_args
        assert call.args == (
            "/2.0/repositories/my-team/myrepo/commit/abc123/comments/42",
        )
        # Cloud must not forward the DC ``version`` query string.
        assert "params" not in call.kwargs or not call.kwargs.get("params")

    def test_401_raises_not_comment_author(
        self, cloud_commit_comments_mixin: CommitCommentsMixin
    ) -> None:
        """HTTP 401 from Cloud → :class:`NotCommentAuthorError`.

        Both Cloud and DC enforce author/admin-only delete permissions;
        the mixin's Cloud branch must translate 401 into the same typed
        exception as the DC branch so the server-tool layer renders a
        single ``not_comment_author`` envelope regardless of mode.
        """
        cloud_commit_comments_mixin.bitbucket.delete.side_effect = _http_error(401)

        with pytest.raises(NotCommentAuthorError) as exc_info:
            cloud_commit_comments_mixin.delete_commit_comment(
                project_key="my-team",
                repo_slug="myrepo",
                commit_id="abc123",
                comment_id=42,
                version=1,
            )

        # Message mentions the HTTP status for operator diagnostics.
        assert "401" in str(exc_info.value)
        # Chained cause preserves the original HTTPError.
        assert isinstance(exc_info.value.__cause__, HTTPError)

    def test_403_raises_not_comment_author(
        self, cloud_commit_comments_mixin: CommitCommentsMixin
    ) -> None:
        """HTTP 403 from Cloud → :class:`NotCommentAuthorError`.

        Cloud returns 403 when the authenticated ``account_id`` is
        neither the comment author nor a workspace admin. Both 401 and
        403 must surface as the same typed exception (Req 11.9 +
        existing Req 8.5 contract).
        """
        cloud_commit_comments_mixin.bitbucket.delete.side_effect = _http_error(403)

        with pytest.raises(NotCommentAuthorError) as exc_info:
            cloud_commit_comments_mixin.delete_commit_comment(
                project_key="my-team",
                repo_slug="myrepo",
                commit_id="abc123",
                comment_id=42,
                version=1,
            )

        assert "403" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, HTTPError)

    @pytest.mark.parametrize("status", [404, 409, 500, 502])
    def test_other_http_errors_propagate(
        self,
        cloud_commit_comments_mixin: CommitCommentsMixin,
        status: int,
    ) -> None:
        """Non-401/403 failures surface as the original ``HTTPError``.

        The server-tool layer renders any non-``not_comment_author``
        error through its generic envelope; the Cloud branch must not
        masquerade a 404 / 500 as an auth failure.
        """
        err = _http_error(status)
        cloud_commit_comments_mixin.bitbucket.delete.side_effect = err

        with pytest.raises(HTTPError) as exc_info:
            cloud_commit_comments_mixin.delete_commit_comment(
                project_key="my-team",
                repo_slug="myrepo",
                commit_id="abc123",
                comment_id=42,
                version=1,
            )

        assert exc_info.value is err
