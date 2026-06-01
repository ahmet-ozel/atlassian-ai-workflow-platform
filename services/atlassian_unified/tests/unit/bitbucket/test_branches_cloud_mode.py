"""Cloud-branch unit tests for :class:`BranchesMixin`.

These tests cover the Cloud side of the Bitbucket branches and tags
mixin introduced by task 9.1 of the ``bitbucket-cloud-dc-parity`` spec
(Requirements 10.1 - 10.5, 19.1, 19.2).

For each method that carries an ``if self.is_cloud:`` branch
(``get_branches``, ``create_branch``, ``delete_branch``, ``get_tags``,
``create_tag``, ``delete_tag``), one happy-path test verifies that the
outbound URL prefix matches the Cloud 2.0 template
``/2.0/repositories/{workspace}/{repo_slug}/refs/branches[/{name}]`` or
``.../refs/tags[/{name}]`` and that the Cloud response body is routed
through :func:`normalize_branch` / :func:`normalize_tag` so downstream
code keeps seeing DC-shaped dicts (``displayId``, ``id``, ``latestCommit``).

The mixin's DC branches are intentionally **not** touched here — those
paths are exercised by the pre-existing DC tests and by Requirement 19.2
which locks DC behavior byte-for-byte. The tests below therefore only
stamp ``is_cloud=True`` onto a bypassed ``BranchesMixin`` instance and
inspect what the Cloud branch does.

Test pattern:

* Bypass ``BranchesMixin.__init__`` with :py:meth:`BranchesMixin.__new__`
  to avoid the live-auth / live-HTTP constructor (the mixin inherits
  from :class:`BitbucketClient`).
* Stamp ``mixin.bitbucket = MagicMock()`` so HTTP primitives
  (``get``/``post``/``delete``) are driven by :class:`MagicMock`.
* Stamp a :class:`SimpleNamespace` onto ``mixin.config`` that exposes the
  attributes :attr:`BitbucketClient.is_cloud` reads (``is_cloud``,
  ``workspace``, ``url``, ``ssl_verify``). No real :class:`BitbucketConfig`
  instance is needed because the property on :class:`BitbucketClient`
  delegates straight to ``self.config.is_cloud``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcp_atlassian.bitbucket.branches import BranchesMixin


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cloud_branches_mixin() -> BranchesMixin:
    """Return a :class:`BranchesMixin` instance wired for Cloud mode.

    ``BranchesMixin.__new__`` bypasses :meth:`BitbucketClient.__init__`,
    so no real HTTP or auth setup runs. The stamped ``bitbucket`` mock
    stands in for the ``atlassian.Bitbucket`` client; the stamped
    ``config`` namespace provides just enough attributes for the
    :attr:`BitbucketClient.is_cloud` property and the Cloud branches of
    the mixin methods (``config.workspace`` in particular) to work.
    """
    mixin = BranchesMixin.__new__(BranchesMixin)
    mixin.bitbucket = MagicMock()
    mixin.config = SimpleNamespace(
        is_cloud=True,
        workspace="my-team",
        url="https://api.bitbucket.org",
        ssl_verify=True,
    )
    return mixin


def _cloud_branch_payload(name: str, sha: str) -> dict:
    """Fabricate a Cloud 2.0 branch dict that :func:`normalize_branch` recognizes.

    The ``target.hash`` + absence of DC ``displayId`` / ``latestCommit``
    keys cause :func:`normalize_branch` to add the synthetic DC fields we
    assert on.
    """
    return {
        "name": name,
        "target": {"hash": sha},
    }


def _cloud_tag_payload(name: str, sha: str) -> dict:
    """Fabricate a Cloud 2.0 tag dict suitable for :func:`normalize_tag`."""
    return {
        "name": name,
        "target": {"hash": sha},
    }


# ===========================================================================
# get_branches (Req 10.2)
# ===========================================================================


class TestGetBranchesCloud:
    """``get_branches`` Cloud branch — Requirement 10.2."""

    def test_issues_cloud_refs_branches_url(
        self, cloud_branches_mixin: BranchesMixin
    ) -> None:
        """Happy path: single-page Cloud envelope, verify URL prefix.

        Cloud termination is ``next=None`` (Req 7.3). The helper strips
        the ``next`` key from the returned list and routes each value
        through :func:`normalize_branch` so downstream code sees
        ``displayId`` / ``id`` / ``latestCommit`` (Req 10.2).
        """
        cloud_branches_mixin.bitbucket.get.return_value = {
            "values": [
                _cloud_branch_payload("main", "abc1234def5678"),
                _cloud_branch_payload("feature/x", "999aaa888bbb"),
            ],
            "next": None,
            "page": 1,
            "pagelen": 25,
            "size": 2,
        }

        result = cloud_branches_mixin.get_branches(
            project_key="my-team", repo_slug="myrepo"
        )

        # The helper issues exactly one outbound GET on the Cloud path.
        cloud_branches_mixin.bitbucket.get.assert_called_once()
        (called_url,), _kwargs = cloud_branches_mixin.bitbucket.get.call_args
        assert (
            called_url
            == "/2.0/repositories/my-team/myrepo/refs/branches"
        )
        # Normalized DC-shaped fields are synthesized for each branch.
        assert [b["displayId"] for b in result] == ["main", "feature/x"]
        assert result[0]["id"] == "refs/heads/main"
        assert result[0]["latestCommit"] == "abc1234def5678"

    def test_uses_config_workspace_when_project_key_empty(
        self, cloud_branches_mixin: BranchesMixin
    ) -> None:
        """Workspace fallback (Req 2.5) routes through ``config.workspace``.

        When the caller passes an empty ``project_key`` the Cloud branch
        resolves the workspace from ``config.workspace`` and still emits
        ``/2.0/repositories/my-team/...``.
        """
        cloud_branches_mixin.bitbucket.get.return_value = {
            "values": [],
            "next": None,
        }

        cloud_branches_mixin.get_branches(project_key="", repo_slug="r")

        (called_url,), _ = cloud_branches_mixin.bitbucket.get.call_args
        assert called_url.startswith("/2.0/repositories/my-team/r/refs/branches")

    def test_translates_filter_text_to_cloud_query_dsl(
        self, cloud_branches_mixin: BranchesMixin
    ) -> None:
        """``filter_text`` is rewritten into Cloud's ``q=name~"..."`` DSL.

        Cloud does not accept the DC ``filterText`` query parameter;
        Requirement 10.2 requires the Cloud branch to target the Cloud
        endpoint which implies using the Cloud-native filter syntax.
        """
        cloud_branches_mixin.bitbucket.get.return_value = {
            "values": [],
            "next": None,
        }

        cloud_branches_mixin.get_branches(
            project_key="my-team",
            repo_slug="myrepo",
            filter_text="release",
        )

        _args, kwargs = cloud_branches_mixin.bitbucket.get.call_args
        assert kwargs["params"]["q"] == 'name~"release"'
        assert "filterText" not in kwargs["params"]


# ===========================================================================
# create_branch (Req 10.3)
# ===========================================================================


class TestCreateBranchCloud:
    """``create_branch`` Cloud branch — Requirement 10.3."""

    def test_posts_cloud_refs_branches_url_with_target_hash_body(
        self, cloud_branches_mixin: BranchesMixin
    ) -> None:
        """``POST /2.0/repositories/{workspace}/{slug}/refs/branches`` with
        ``{"name": ..., "target": {"hash": ...}}`` (Req 10.3).
        """
        cloud_branches_mixin.bitbucket.post.return_value = _cloud_branch_payload(
            "release/1.0", "deadbeef0000"
        )

        result = cloud_branches_mixin.create_branch(
            project_key="my-team",
            repo_slug="myrepo",
            branch_name="release/1.0",
            start_point="deadbeef0000",
        )

        cloud_branches_mixin.bitbucket.post.assert_called_once()
        (called_url,), kwargs = cloud_branches_mixin.bitbucket.post.call_args
        assert (
            called_url
            == "/2.0/repositories/my-team/myrepo/refs/branches"
        )
        # Body is Cloud-shaped: ``{"name": ..., "target": {"hash": ...}}``
        assert kwargs["data"] == {
            "name": "release/1.0",
            "target": {"hash": "deadbeef0000"},
        }
        # DC-side safety argument is not present on the Cloud path.
        assert "startPoint" not in kwargs["data"]
        # Response passes through normalize_branch → synthesized DC fields.
        assert result["displayId"] == "release/1.0"
        assert result["id"] == "refs/heads/release/1.0"
        assert result["latestCommit"] == "deadbeef0000"


# ===========================================================================
# delete_branch (Req 10.4)
# ===========================================================================


class TestDeleteBranchCloud:
    """``delete_branch`` Cloud branch — Requirement 10.4."""

    def test_deletes_cloud_refs_branches_name_url(
        self, cloud_branches_mixin: BranchesMixin
    ) -> None:
        """``DELETE /2.0/repositories/{workspace}/{slug}/refs/branches/{name}``.

        The Cloud endpoint does not accept DC's ``end_point`` safety
        parameter, so the Cloud branch must drop it silently rather than
        forwarding it as a query/body parameter (Req 10.4).
        """
        cloud_branches_mixin.bitbucket.delete.return_value = None

        ok = cloud_branches_mixin.delete_branch(
            project_key="my-team",
            repo_slug="myrepo",
            branch_name="feature/x",
            end_point="abc123",
        )

        assert ok is True
        cloud_branches_mixin.bitbucket.delete.assert_called_once()
        call = cloud_branches_mixin.bitbucket.delete.call_args
        # ``delete`` is called positionally with exactly one URL argument —
        # no DC-style ``data={"name": "refs/heads/..."}`` body.
        assert call.args == (
            "/2.0/repositories/my-team/myrepo/refs/branches/feature/x",
        )
        # Cloud DELETE must not forward the DC ``end_point`` safety field.
        assert "data" not in call.kwargs


# ===========================================================================
# get_tags (Req 10.5 — list)
# ===========================================================================


class TestGetTagsCloud:
    """``get_tags`` Cloud branch — Requirement 10.5 (list)."""

    def test_issues_cloud_refs_tags_url(
        self, cloud_branches_mixin: BranchesMixin
    ) -> None:
        """``GET /2.0/repositories/{workspace}/{slug}/refs/tags``.

        Cloud termination is ``next=None``; each value is routed through
        :func:`normalize_tag` so the DC ``id`` prefix is ``refs/tags/``
        (not ``refs/heads/``).
        """
        cloud_branches_mixin.bitbucket.get.return_value = {
            "values": [
                _cloud_tag_payload("v1.0.0", "tag1sha"),
                _cloud_tag_payload("v1.1.0", "tag2sha"),
            ],
            "next": None,
        }

        result = cloud_branches_mixin.get_tags(
            project_key="my-team", repo_slug="myrepo"
        )

        cloud_branches_mixin.bitbucket.get.assert_called_once()
        (called_url,), _kwargs = cloud_branches_mixin.bitbucket.get.call_args
        assert called_url == "/2.0/repositories/my-team/myrepo/refs/tags"
        # Normalized DC-shaped fields: tags use ``refs/tags/`` prefix.
        assert [t["displayId"] for t in result] == ["v1.0.0", "v1.1.0"]
        assert result[0]["id"] == "refs/tags/v1.0.0"
        assert result[0]["latestCommit"] == "tag1sha"

    def test_translates_filter_text_to_cloud_query_dsl(
        self, cloud_branches_mixin: BranchesMixin
    ) -> None:
        """``filter_text`` on the tag listing uses the same Cloud DSL as
        branches: ``q=name~"..."``.
        """
        cloud_branches_mixin.bitbucket.get.return_value = {
            "values": [],
            "next": None,
        }

        cloud_branches_mixin.get_tags(
            project_key="my-team",
            repo_slug="myrepo",
            filter_text="v1",
        )

        _args, kwargs = cloud_branches_mixin.bitbucket.get.call_args
        assert kwargs["params"]["q"] == 'name~"v1"'
        assert "filterText" not in kwargs["params"]


# ===========================================================================
# create_tag (Req 10.5 — create)
# ===========================================================================


class TestCreateTagCloud:
    """``create_tag`` Cloud branch — Requirement 10.5 (create)."""

    def test_posts_cloud_refs_tags_url_with_target_hash_body(
        self, cloud_branches_mixin: BranchesMixin
    ) -> None:
        """``POST /2.0/repositories/{workspace}/{slug}/refs/tags`` with
        ``{"name": ..., "target": {"hash": ...}}``.
        """
        cloud_branches_mixin.bitbucket.post.return_value = _cloud_tag_payload(
            "v2.0.0", "ffeeddcc"
        )

        result = cloud_branches_mixin.create_tag(
            project_key="my-team",
            repo_slug="myrepo",
            tag_name="v2.0.0",
            start_point="ffeeddcc",
        )

        cloud_branches_mixin.bitbucket.post.assert_called_once()
        (called_url,), kwargs = cloud_branches_mixin.bitbucket.post.call_args
        assert called_url == "/2.0/repositories/my-team/myrepo/refs/tags"
        assert kwargs["data"] == {
            "name": "v2.0.0",
            "target": {"hash": "ffeeddcc"},
        }
        # DC ``startPoint`` must not appear on the Cloud body.
        assert "startPoint" not in kwargs["data"]
        # ``message`` is opt-in and absent by default.
        assert "message" not in kwargs["data"]
        # Normalized DC-shaped fields: ``refs/tags/`` prefix.
        assert result["displayId"] == "v2.0.0"
        assert result["id"] == "refs/tags/v2.0.0"
        assert result["latestCommit"] == "ffeeddcc"

    def test_forwards_annotation_message_when_provided(
        self, cloud_branches_mixin: BranchesMixin
    ) -> None:
        """An annotated Cloud tag forwards ``message`` in the POST body."""
        cloud_branches_mixin.bitbucket.post.return_value = _cloud_tag_payload(
            "v2.0.0", "ffeeddcc"
        )

        cloud_branches_mixin.create_tag(
            project_key="my-team",
            repo_slug="myrepo",
            tag_name="v2.0.0",
            start_point="ffeeddcc",
            message="release v2.0.0",
        )

        _args, kwargs = cloud_branches_mixin.bitbucket.post.call_args
        assert kwargs["data"]["message"] == "release v2.0.0"


# ===========================================================================
# delete_tag (Req 10.5 — delete)
# ===========================================================================


class TestDeleteTagCloud:
    """``delete_tag`` Cloud branch — Requirement 10.5 (delete)."""

    def test_deletes_cloud_refs_tags_name_url(
        self, cloud_branches_mixin: BranchesMixin
    ) -> None:
        """``DELETE /2.0/repositories/{workspace}/{slug}/refs/tags/{name}``.

        The Cloud DELETE is a bare URL call — no request body, no
        ``end_point`` safety parameter (tags have no DC equivalent of
        that parameter anyway).
        """
        cloud_branches_mixin.bitbucket.delete.return_value = None

        ok = cloud_branches_mixin.delete_tag(
            project_key="my-team",
            repo_slug="myrepo",
            tag_name="v1.0.0",
        )

        assert ok is True
        cloud_branches_mixin.bitbucket.delete.assert_called_once()
        call = cloud_branches_mixin.bitbucket.delete.call_args
        assert call.args == (
            "/2.0/repositories/my-team/myrepo/refs/tags/v1.0.0",
        )
        assert "data" not in call.kwargs
