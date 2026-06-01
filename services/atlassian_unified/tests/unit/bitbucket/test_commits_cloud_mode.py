"""Cloud-branch unit tests for :class:`CommitsMixin`.

These tests cover the Cloud side of the Bitbucket commits mixin
introduced by tasks 10.1 and 10.2 of the ``bitbucket-cloud-dc-parity``
spec (Requirements 11.1 - 11.11, 19.1, 19.2, 19.3).

For each method that carries an ``if self.is_cloud:`` branch
(``get_commits``, ``get_commit``, ``get_diff``, ``compare_commits``,
``get_commit_build_status``, ``post_commit_build_status``,
``search_code``) one happy-path test verifies that the outbound URL
prefix matches the Cloud 2.0 template documented in Requirement 11
(``/2.0/repositories/{workspace}/<slug>/...`` for per-repo endpoints;
``/2.0/workspaces/{workspace}/search/code`` for code search).

The ``compare_commits`` pre-HTTP validation (Req 11.5) has its own test
class that exhaustively exercises the characters the spec rejects:
empty strings, ``/``, ``?``, ``#`` and any whitespace. Every rejection
case asserts:

1. A :class:`ValueError` is raised whose message carries the
   ``invalid_target:`` prefix (the server-tool layer uses this prefix
   to map the mixin error onto a :class:`StructuredError` with
   ``error_code="invalid_target"``).
2. Zero outbound HTTP calls are issued. The Cloud compare endpoint
   uses ``self.bitbucket._session.get`` so the test checks that the
   session mock was never invoked.

The mixin's DC branches are intentionally **not** touched here — those
paths are locked byte-for-byte by Requirement 19.2 / 19.3 and covered
by any pre-existing DC tests. The tests below stamp ``is_cloud=True``
onto a bypassed :class:`CommitsMixin` instance and only inspect what
the Cloud branch does.

Test pattern (mirrors :mod:`test_branches_cloud_mode` and
:mod:`test_commit_comments_cloud_mode`):

* Bypass :meth:`CommitsMixin.__init__` via :meth:`CommitsMixin.__new__`
  to avoid the live-auth / live-HTTP constructor (the mixin inherits
  from :class:`BitbucketClient`).
* Stamp ``mixin.bitbucket = MagicMock()`` so ``get`` / ``post`` and
  ``bitbucket._session.get`` (used by ``get_diff`` and
  ``compare_commits`` for unified-diff text) are driven by MagicMocks.
* Stamp a :class:`SimpleNamespace` on ``mixin.config`` with
  ``is_cloud=True``, ``workspace="my-team"``, plus the minimal URL /
  SSL attributes the :attr:`BitbucketClient.is_cloud` property reads.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from mcp_atlassian.bitbucket.commits import CommitsMixin


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


WORKSPACE = "my-team"
REPO_SLUG = "myrepo"
CLOUD_BASE = f"/2.0/repositories/{WORKSPACE}/{REPO_SLUG}"


@pytest.fixture
def cloud_commits_mixin() -> CommitsMixin:
    """Return a :class:`CommitsMixin` instance wired for Cloud mode.

    ``CommitsMixin.__new__`` bypasses :meth:`BitbucketClient.__init__`,
    so no real HTTP / auth setup runs. The stamped ``bitbucket`` mock
    stands in for the ``atlassian.Bitbucket`` client; the stamped
    ``config`` namespace provides just enough attributes for the
    :attr:`BitbucketClient.is_cloud` property and the Cloud branches of
    the mixin methods (``config.workspace`` in particular) to work.

    The ``get_diff`` and ``compare_commits`` Cloud paths reach directly
    into ``self.bitbucket._session.get`` to retrieve unified-diff text
    (the ``atlassian-python-api`` wrapper only returns JSON); the
    :class:`MagicMock` auto-creates ``_session`` as another MagicMock
    so those call sites are exercised without any extra setup.
    """
    mixin = CommitsMixin.__new__(CommitsMixin)
    mixin.bitbucket = MagicMock()
    mixin.config = SimpleNamespace(
        is_cloud=True,
        workspace=WORKSPACE,
        url="https://api.bitbucket.org",
        ssl_verify=True,
    )
    return mixin


def _cloud_commit_payload(sha: str, message: str = "wip") -> dict[str, Any]:
    """Fabricate a minimal Cloud 2.0 commit dict.

    The shape carries just enough structure to exercise
    :func:`normalize_commit` without tripping its defensive guards: a
    Cloud-only ``hash`` key (so the shape-detector classifies it as
    Cloud) and a Cloud-style ``author.user`` subobject so the
    ``normalize_user`` path inside :func:`normalize_commit` fires.
    """
    return {
        "hash": sha,
        "message": message,
        "author": {
            "raw": "Jane Doe <jane@example.com>",
            "user": {
                "account_id": "{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}",
                "display_name": "Jane Doe",
            },
        },
    }


def _diff_response(text: str = "diff --git a/x b/x\n") -> MagicMock:
    """Build a fake :class:`requests.Response` for the Cloud diff endpoints.

    The Cloud branches of :meth:`get_diff` and :meth:`compare_commits`
    call ``self.bitbucket._session.get(...).raise_for_status()`` then
    return ``response.text``. Only those two surfaces need mocking.
    """
    resp = MagicMock()
    resp.text = text
    resp.raise_for_status.return_value = None
    return resp


# ===========================================================================
# get_commits (Req 11.1)
# ===========================================================================


class TestGetCommitsCloud:
    """``get_commits`` Cloud branch — Requirement 11.1."""

    def test_issues_cloud_commits_url(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """Happy path: single-page Cloud envelope, verify URL prefix.

        Cloud termination is ``next=None`` (Req 7.3). The helper routes
        each value through :func:`normalize_commit` (passed via the
        ``normalizer`` kwarg) so downstream code sees the DC-shaped
        ``id`` / ``displayId`` fields.
        """
        cloud_commits_mixin.bitbucket.get.return_value = {
            "values": [
                _cloud_commit_payload("abc1234def5678"),
                _cloud_commit_payload("999aaa888bbb111"),
            ],
            "next": None,
            "page": 1,
            "pagelen": 25,
            "size": 2,
        }

        result = cloud_commits_mixin.get_commits(
            project_key=WORKSPACE, repo_slug=REPO_SLUG
        )

        cloud_commits_mixin.bitbucket.get.assert_called_once()
        (called_url,), _kwargs = cloud_commits_mixin.bitbucket.get.call_args
        assert called_url == f"{CLOUD_BASE}/commits"
        # Normalized DC-shaped fields are synthesized for each commit.
        assert [c["id"] for c in result] == [
            "abc1234def5678",
            "999aaa888bbb111",
        ]
        assert result[0]["displayId"] == "abc1234"

    def test_uses_config_workspace_when_project_key_empty(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """Workspace fallback (Req 2.5) routes through ``config.workspace``.

        When the caller passes an empty ``project_key`` the Cloud branch
        resolves the workspace from ``config.workspace`` and still emits
        ``/2.0/repositories/my-team/...``.
        """
        cloud_commits_mixin.bitbucket.get.return_value = {
            "values": [],
            "next": None,
        }

        cloud_commits_mixin.get_commits(project_key="", repo_slug="r")

        (called_url,), _ = cloud_commits_mixin.bitbucket.get.call_args
        assert called_url == f"/2.0/repositories/{WORKSPACE}/r/commits"

    def test_translates_until_since_to_cloud_include_exclude(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """``until`` / ``since`` map to Cloud's ``include`` / ``exclude`` params.

        Cloud does not accept DC's bare ``until`` / ``since`` query
        parameters; Requirement 11.1 requires the Cloud branch to target
        the Cloud endpoint, which implies using its native ref-range
        filter syntax.
        """
        cloud_commits_mixin.bitbucket.get.return_value = {
            "values": [],
            "next": None,
        }

        cloud_commits_mixin.get_commits(
            project_key=WORKSPACE,
            repo_slug=REPO_SLUG,
            until="main",
            since="develop",
        )

        _args, kwargs = cloud_commits_mixin.bitbucket.get.call_args
        assert kwargs["params"]["include"] == "main"
        assert kwargs["params"]["exclude"] == "develop"
        # The DC ``until`` / ``since`` names must not be forwarded on Cloud.
        assert "until" not in kwargs["params"]
        assert "since" not in kwargs["params"]


# ===========================================================================
# get_commit (Req 11.2)
# ===========================================================================


class TestGetCommitCloud:
    """``get_commit`` Cloud branch — Requirement 11.2."""

    def test_issues_cloud_commit_url(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """``GET /2.0/repositories/{ws}/{slug}/commit/{sha}``.

        Verifies both the outbound URL (Req 11.2) and that the Cloud
        payload passes through :func:`normalize_commit` so downstream
        code sees the DC-shaped ``id`` / ``displayId`` fields.
        """
        cloud_commits_mixin.bitbucket.get.return_value = _cloud_commit_payload(
            "abc1234def5678"
        )

        result = cloud_commits_mixin.get_commit(
            project_key=WORKSPACE,
            repo_slug=REPO_SLUG,
            commit_id="abc1234def5678",
        )

        cloud_commits_mixin.bitbucket.get.assert_called_once()
        (called_url,), _kwargs = cloud_commits_mixin.bitbucket.get.call_args
        assert called_url == f"{CLOUD_BASE}/commit/abc1234def5678"
        assert result["id"] == "abc1234def5678"
        assert result["displayId"] == "abc1234"

    def test_non_dict_response_raises(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """Unexpected non-dict response surfaces as :class:`ValueError`.

        Matches the DC branch's contract and keeps the two modes
        indistinguishable from the caller's point of view.
        """
        cloud_commits_mixin.bitbucket.get.return_value = "not a dict"

        with pytest.raises(ValueError, match="Unexpected response for commit"):
            cloud_commits_mixin.get_commit(
                project_key=WORKSPACE,
                repo_slug=REPO_SLUG,
                commit_id="abc",
            )


# ===========================================================================
# get_diff (Req 11.3)
# ===========================================================================


class TestGetDiffCloud:
    """``get_diff`` Cloud branch — Requirement 11.3."""

    def test_commit_diff_uses_cloud_diff_sha_url(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """``GET /2.0/repositories/{ws}/{slug}/diff/{sha}`` for single commits.

        The Cloud diff endpoint returns unified-diff text, not JSON, so
        the mixin bypasses the wrapper and hits
        ``self.bitbucket._session.get`` directly. The full URL must
        include the configured base URL prefix so ``requests`` sends the
        request to ``api.bitbucket.org``.
        """
        cloud_commits_mixin.bitbucket._session.get.return_value = _diff_response(
            "diff --git a/x b/x\n@@ -1 +1 @@\n"
        )

        result = cloud_commits_mixin.get_diff(
            project_key=WORKSPACE,
            repo_slug=REPO_SLUG,
            commit_id="abc1234",
        )

        cloud_commits_mixin.bitbucket._session.get.assert_called_once()
        (called_url,), kwargs = (
            cloud_commits_mixin.bitbucket._session.get.call_args
        )
        assert called_url == (
            f"https://api.bitbucket.org{CLOUD_BASE}/diff/abc1234"
        )
        # SSL verification setting from config is forwarded to requests.
        assert kwargs["verify"] is True
        # ``context`` query param carries the context-lines count.
        assert kwargs["params"]["context"] == 3
        assert result == "diff --git a/x b/x\n@@ -1 +1 @@\n"

    def test_ref_range_diff_uses_to_dot_dot_from_spec(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """``/diff/{to}..{from}`` spec for ``since``/``until`` ref-range diffs.

        Requirement 11.3 + 11.4 — Cloud expresses a ref-range diff
        using a single ``{to}..{from}`` spec in the URL path, not DC's
        ``since=...&until=...`` query parameters.
        """
        cloud_commits_mixin.bitbucket._session.get.return_value = _diff_response()

        cloud_commits_mixin.get_diff(
            project_key=WORKSPACE,
            repo_slug=REPO_SLUG,
            since="feature/x",
            until="main",
        )

        (called_url,), _kwargs = (
            cloud_commits_mixin.bitbucket._session.get.call_args
        )
        # ``until`` maps to the ``to`` (left) side of the spec; ``since``
        # maps to the ``from`` (right) side. See the design doc table.
        assert called_url == (
            f"https://api.bitbucket.org{CLOUD_BASE}/diff/main..feature/x"
        )

    def test_missing_refs_raises_value_error(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """Calling without ``commit_id`` or a complete ref-range raises."""
        with pytest.raises(ValueError, match="requires either commit_id"):
            cloud_commits_mixin.get_diff(
                project_key=WORKSPACE,
                repo_slug=REPO_SLUG,
            )
        # Verify no outbound HTTP was issued.
        cloud_commits_mixin.bitbucket._session.get.assert_not_called()


# ===========================================================================
# compare_commits — happy path (Req 11.4)
# ===========================================================================


class TestCompareCommitsCloudHappyPath:
    """``compare_commits`` Cloud branch — Requirement 11.4."""

    def test_issues_cloud_diff_spec_url(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """``GET /2.0/repositories/{ws}/{slug}/diff/{to}..{from}``.

        Verifies the outbound URL (Req 11.4). ``compare_commits`` on
        Cloud returns the unified-diff text wrapped in a single-element
        list under the ``diff`` key, since Cloud has no native
        commit-list compare endpoint (see the docstring on the mixin
        method).
        """
        cloud_commits_mixin.bitbucket._session.get.return_value = _diff_response(
            "diff --git a/x b/x\n"
        )

        result = cloud_commits_mixin.compare_commits(
            project_key=WORKSPACE,
            repo_slug=REPO_SLUG,
            from_ref="feature",
            to_ref="main",
        )

        cloud_commits_mixin.bitbucket._session.get.assert_called_once()
        (called_url,), _kwargs = (
            cloud_commits_mixin.bitbucket._session.get.call_args
        )
        assert called_url == (
            f"https://api.bitbucket.org{CLOUD_BASE}/diff/main..feature"
        )
        assert result == [{"diff": "diff --git a/x b/x\n"}]

    def test_empty_diff_returns_empty_list(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """An empty Cloud diff body yields an empty commit list.

        ``[]`` is the sentinel for "no divergence"; callers that map
        compare results onto "commits ahead" counts treat this as zero.
        """
        cloud_commits_mixin.bitbucket._session.get.return_value = _diff_response("")

        result = cloud_commits_mixin.compare_commits(
            project_key=WORKSPACE,
            repo_slug=REPO_SLUG,
            from_ref="feature",
            to_ref="main",
        )

        assert result == []


# ===========================================================================
# compare_commits — invalid_target pre-check (Req 11.5)
# ===========================================================================


class TestCompareCommitsInvalidTarget:
    """Pre-HTTP ``invalid_target`` guard on ``compare_commits`` — Req 11.5.

    Every rejection case asserts both the :class:`ValueError` shape
    (``invalid_target:`` prefix) and the absence of any outbound HTTP
    call on ``self.bitbucket._session.get``. Covering both halves is
    what turns this into a genuine "pre-HTTP" guarantee.
    """

    @pytest.mark.parametrize(
        "from_ref,to_ref",
        [
            # Empty on either side is rejected because Cloud cannot form
            # a ``{to}..{from}`` spec without both refs.
            ("", "main"),
            ("feature", ""),
            ("", ""),
        ],
    )
    def test_empty_ref_raises_invalid_target_pre_http(
        self,
        cloud_commits_mixin: CommitsMixin,
        from_ref: str,
        to_ref: str,
    ) -> None:
        """Empty ``from`` or ``to`` refs are rejected before any HTTP."""
        with pytest.raises(ValueError, match=r"^invalid_target:"):
            cloud_commits_mixin.compare_commits(
                project_key=WORKSPACE,
                repo_slug=REPO_SLUG,
                from_ref=from_ref,
                to_ref=to_ref,
            )
        cloud_commits_mixin.bitbucket._session.get.assert_not_called()
        cloud_commits_mixin.bitbucket.get.assert_not_called()

    @pytest.mark.parametrize(
        "bad_char",
        [
            "/",  # path separator would change URL routing
            "?",  # would be interpreted as query-string start
            "#",  # would be interpreted as URL fragment
            " ",  # whitespace: space
            "\t",  # whitespace: tab
            "\n",  # whitespace: newline
        ],
    )
    def test_illegal_char_in_from_ref_raises_invalid_target_pre_http(
        self,
        cloud_commits_mixin: CommitsMixin,
        bad_char: str,
    ) -> None:
        """``from_ref`` containing ``/``, ``?``, ``#`` or whitespace is rejected."""
        bad_ref = f"feature{bad_char}x"
        with pytest.raises(ValueError, match=r"^invalid_target:"):
            cloud_commits_mixin.compare_commits(
                project_key=WORKSPACE,
                repo_slug=REPO_SLUG,
                from_ref=bad_ref,
                to_ref="main",
            )
        cloud_commits_mixin.bitbucket._session.get.assert_not_called()
        cloud_commits_mixin.bitbucket.get.assert_not_called()

    @pytest.mark.parametrize(
        "bad_char",
        [
            "/",
            "?",
            "#",
            " ",
            "\t",
            "\n",
        ],
    )
    def test_illegal_char_in_to_ref_raises_invalid_target_pre_http(
        self,
        cloud_commits_mixin: CommitsMixin,
        bad_char: str,
    ) -> None:
        """``to_ref`` containing ``/``, ``?``, ``#`` or whitespace is rejected."""
        bad_ref = f"main{bad_char}x"
        with pytest.raises(ValueError, match=r"^invalid_target:"):
            cloud_commits_mixin.compare_commits(
                project_key=WORKSPACE,
                repo_slug=REPO_SLUG,
                from_ref="feature",
                to_ref=bad_ref,
            )
        cloud_commits_mixin.bitbucket._session.get.assert_not_called()
        cloud_commits_mixin.bitbucket.get.assert_not_called()


# ===========================================================================
# get_commit_build_status (Req 11.6)
# ===========================================================================


class TestGetCommitBuildStatusCloud:
    """``get_commit_build_status`` Cloud branch — Requirement 11.6."""

    def test_issues_cloud_commit_statuses_url(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """``GET /2.0/repositories/{ws}/{slug}/commit/{sha}/statuses``.

        Unlike DC's global ``/rest/build-status`` plugin, Cloud scopes
        build statuses under the per-repository commit resource, so the
        workspace + repo slug have to be threaded through.
        """
        cloud_commits_mixin.bitbucket.get.return_value = {
            "values": [
                {"state": "SUCCESSFUL", "key": "ci-lint", "name": "Lint"},
                {"state": "FAILED", "key": "ci-test", "name": "Tests"},
            ],
            "next": None,
        }

        result = cloud_commits_mixin.get_commit_build_status(
            commit_id="abc123",
            project_key=WORKSPACE,
            repo_slug=REPO_SLUG,
        )

        cloud_commits_mixin.bitbucket.get.assert_called_once()
        (called_url,), _kwargs = cloud_commits_mixin.bitbucket.get.call_args
        assert called_url == f"{CLOUD_BASE}/commit/abc123/statuses"
        assert [s["key"] for s in result] == ["ci-lint", "ci-test"]

    def test_missing_repo_slug_raises_value_error(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """Cloud requires ``repo_slug``; DC looks up by SHA alone.

        The Cloud URL template ``.../repositories/{ws}/{slug}/...``
        cannot be formed without a repo slug, so the mixin fails loud
        before any HTTP call rather than issuing a malformed request.
        """
        with pytest.raises(ValueError, match="Cloud requires repo_slug"):
            cloud_commits_mixin.get_commit_build_status(
                commit_id="abc123",
                project_key=WORKSPACE,
            )
        cloud_commits_mixin.bitbucket.get.assert_not_called()


# ===========================================================================
# post_commit_build_status (Req 11.6, 11.7)
# ===========================================================================


class TestPostCommitBuildStatusCloud:
    """``post_commit_build_status`` Cloud branch — Requirement 11.6 / 11.7."""

    def test_posts_cloud_statuses_build_url_with_dc_shaped_body(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """``POST /2.0/repositories/{ws}/{slug}/commit/{sha}/statuses/build``.

        Requirement 11.7 documents that the DC fields
        ``key``/``url``/``state``/``name``/``description`` map 1:1 to the
        Cloud body shape; ``state`` values ``SUCCESSFUL``/``FAILED``/
        ``INPROGRESS`` are identical on both sides. The test forwards
        every optional field so the body-shape assertion is total.
        """
        cloud_commits_mixin.bitbucket.post.return_value = {}

        ok = cloud_commits_mixin.post_commit_build_status(
            commit_id="abc123",
            state="SUCCESSFUL",
            key="ci-lint",
            name="Lint",
            url="https://ci.example.com/job/42",
            description="All checks passed",
            project_key=WORKSPACE,
            repo_slug=REPO_SLUG,
        )

        assert ok is True
        cloud_commits_mixin.bitbucket.post.assert_called_once()
        (called_url,), kwargs = cloud_commits_mixin.bitbucket.post.call_args
        assert called_url == f"{CLOUD_BASE}/commit/abc123/statuses/build"
        assert kwargs["data"] == {
            "state": "SUCCESSFUL",
            "key": "ci-lint",
            "name": "Lint",
            "url": "https://ci.example.com/job/42",
            "description": "All checks passed",
        }

    def test_missing_repo_slug_raises_value_error(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """Cloud requires ``repo_slug`` to form the per-repo URL template."""
        with pytest.raises(ValueError, match="Cloud requires repo_slug"):
            cloud_commits_mixin.post_commit_build_status(
                commit_id="abc123",
                state="SUCCESSFUL",
                key="ci-lint",
                project_key=WORKSPACE,
            )
        cloud_commits_mixin.bitbucket.post.assert_not_called()


# ===========================================================================
# search_code (Req 11.11)
# ===========================================================================


class TestSearchCodeCloud:
    """``search_code`` Cloud branch — Requirement 11.11."""

    def test_issues_cloud_workspace_search_code_url(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """``GET /2.0/workspaces/{workspace}/search/code``.

        Note this endpoint is under ``/2.0/workspaces/...``, not
        ``/2.0/repositories/...`` — the workspace-level search is the
        only code-search surface Cloud exposes (Requirement 11.11).
        """
        cloud_commits_mixin.bitbucket.get.return_value = {
            "values": [{"content_matches": [], "file": {"path": "foo.py"}}],
            "next": None,
        }

        result = cloud_commits_mixin.search_code(
            query="hello",
            project_key=WORKSPACE,
        )

        cloud_commits_mixin.bitbucket.get.assert_called_once()
        (called_url,), kwargs = cloud_commits_mixin.bitbucket.get.call_args
        assert called_url == f"/2.0/workspaces/{WORKSPACE}/search/code"
        # Cloud's free-text query rides under ``search_query``.
        assert kwargs["params"]["search_query"] == "hello"
        assert result == [{"content_matches": [], "file": {"path": "foo.py"}}]

    def test_repo_slug_is_encoded_inline_in_search_query(
        self, cloud_commits_mixin: CommitsMixin
    ) -> None:
        """Repo-scoping uses Cloud's inline ``repo:{slug}`` DSL.

        Cloud's code-search DSL scopes a query to a single repository
        via an inline ``repo:<slug>`` term appended to the query. The
        outbound URL stays workspace-scoped.
        """
        cloud_commits_mixin.bitbucket.get.return_value = {"values": [], "next": None}

        cloud_commits_mixin.search_code(
            query="hello",
            project_key=WORKSPACE,
            repo_slug=REPO_SLUG,
        )

        (called_url,), kwargs = cloud_commits_mixin.bitbucket.get.call_args
        assert called_url == f"/2.0/workspaces/{WORKSPACE}/search/code"
        assert kwargs["params"]["search_query"] == f"hello repo:{REPO_SLUG}"
