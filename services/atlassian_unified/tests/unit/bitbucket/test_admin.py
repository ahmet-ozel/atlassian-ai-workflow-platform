"""Tests for ``AdminMixin`` and the Bitbucket repository/project admin tools.

These tests cover Requirements 4.4, 4.5 and 5.3 from the
``atlassian-dc-tool-parity`` spec:

* ``AdminMixin`` exposes ``create_repository``, ``update_repository``,
  ``fork_repository``, ``create_project`` and ``update_project`` as thin
  wrappers over the Bitbucket DC
  ``/rest/api/latest/projects(/{k}/repos)`` endpoints (Req 4.1-4.3,
  5.1-5.2).
* The mixin intentionally exposes NO ``delete_repository`` or
  ``delete_project`` method (Req 4.4, 5.3). The registration-parity test
  covers tool-level absence; this module pins the same prohibition at
  the Python-module layer so adding a delete primitive to the mixin
  would fail CI even before it reaches the server layer.
* The server tools run the ``check_read_only`` →
  ``check_project_filter`` prelude before issuing any HTTP call. Two
  filter-scope cases are exercised here — the design calls out that
  ``bitbucket_fork_repository`` gates on the *destination* project, and
  ``bitbucket_create_project`` gates on the *new* project key (Req 4.5,
  5.3).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcp_atlassian.bitbucket import admin as admin_module
from mcp_atlassian.bitbucket.admin import AdminMixin


# ---------------------------------------------------------------------------
# Mixin fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_mixin():
    """Create an ``AdminMixin`` instance with mocked Bitbucket transport.

    The mixin normally inherits from :class:`BitbucketClient`, whose
    constructor requires a live config and auth. For unit tests we
    bypass the constructor entirely via ``__new__`` and stamp a bare
    ``bitbucket`` attribute (the underlying ``atlassian.Bitbucket``
    client) onto the instance so the HTTP primitives ``post``/``put``
    can be driven by ``MagicMock``.
    """
    mixin = AdminMixin.__new__(AdminMixin)
    mixin.bitbucket = MagicMock()
    return mixin


# ---------------------------------------------------------------------------
# Mixin happy-path tests — Req 4.1-4.3, 5.1-5.2
# ---------------------------------------------------------------------------


class TestCreateRepository:
    """Happy-path coverage for ``AdminMixin.create_repository``."""

    def test_posts_to_project_repos_endpoint_with_expected_body(self, admin_mixin):
        admin_mixin.bitbucket.post.return_value = {
            "slug": "new-repo",
            "name": "new-repo",
            "project": {"key": "PROJ"},
        }

        result = admin_mixin.create_repository(
            "PROJ",
            name="new-repo",
        )

        admin_mixin.bitbucket.post.assert_called_once()
        called_url = admin_mixin.bitbucket.post.call_args[0][0]
        assert called_url == "/rest/api/latest/projects/PROJ/repos"
        data = admin_mixin.bitbucket.post.call_args[1]["data"]
        # Default SCM, forkable, public flags are forwarded verbatim so
        # the response body matches what Bitbucket DC expects.
        assert data == {
            "name": "new-repo",
            "scmId": "git",
            "forkable": True,
            "public": False,
        }
        assert result["slug"] == "new-repo"

    def test_forwards_overridden_flags(self, admin_mixin):
        admin_mixin.bitbucket.post.return_value = {"slug": "r"}

        admin_mixin.create_repository(
            "PROJ",
            name="r",
            scm="git",
            forkable=False,
            public=True,
        )

        data = admin_mixin.bitbucket.post.call_args[1]["data"]
        assert data["forkable"] is False
        assert data["public"] is True

    def test_non_dict_response_raises_value_error(self, admin_mixin):
        admin_mixin.bitbucket.post.return_value = "not-a-dict"

        with pytest.raises(ValueError):
            admin_mixin.create_repository("PROJ", name="new-repo")


class TestUpdateRepository:
    """Happy-path coverage for ``AdminMixin.update_repository``."""

    def test_puts_only_supplied_fields(self, admin_mixin):
        admin_mixin.bitbucket.put.return_value = {
            "slug": "repo",
            "name": "renamed",
        }

        result = admin_mixin.update_repository(
            "PROJ",
            "repo",
            name="renamed",
            description="new desc",
        )

        admin_mixin.bitbucket.put.assert_called_once()
        called_url = admin_mixin.bitbucket.put.call_args[0][0]
        assert called_url == "/rest/api/latest/projects/PROJ/repos/repo"
        data = admin_mixin.bitbucket.put.call_args[1]["data"]
        # Partial update: only the keys the caller supplied show up in
        # the PUT body. Omitted fields must not appear — Bitbucket
        # interprets missing keys as "leave alone".
        assert data == {"name": "renamed", "description": "new desc"}
        assert "defaultBranch" not in data
        assert "public" not in data
        assert "forkable" not in data
        assert result["name"] == "renamed"

    def test_empty_fields_still_issues_put(self, admin_mixin):
        # The mixin itself does NOT reject empty updates — that's the
        # server tool's job. At the mixin layer the PUT is still issued
        # with an empty body so callers can compose higher-level policy.
        admin_mixin.bitbucket.put.return_value = {"slug": "repo"}

        admin_mixin.update_repository("PROJ", "repo")

        data = admin_mixin.bitbucket.put.call_args[1]["data"]
        assert data == {}

    def test_non_dict_response_raises_value_error(self, admin_mixin):
        admin_mixin.bitbucket.put.return_value = None

        with pytest.raises(ValueError):
            admin_mixin.update_repository("PROJ", "repo", name="x")


class TestForkRepository:
    """Happy-path coverage for ``AdminMixin.fork_repository``."""

    def test_posts_to_source_repo_with_destination_project_payload(self, admin_mixin):
        admin_mixin.bitbucket.post.return_value = {
            "slug": "repo",
            "project": {"key": "DEST"},
        }

        result = admin_mixin.fork_repository(
            "SRC",
            "repo",
            dest_project="DEST",
        )

        admin_mixin.bitbucket.post.assert_called_once()
        called_url = admin_mixin.bitbucket.post.call_args[0][0]
        # Bitbucket's fork endpoint is POST on the SOURCE repo URL with
        # a destination-project body — not a POST under the destination.
        assert called_url == "/rest/api/latest/projects/SRC/repos/repo"
        data = admin_mixin.bitbucket.post.call_args[1]["data"]
        assert data == {"project": {"key": "DEST"}}
        # ``name`` is omitted by default — Bitbucket reuses the source
        # repository's name for the fork.
        assert "name" not in data
        assert result["project"]["key"] == "DEST"

    def test_forwards_optional_name_override(self, admin_mixin):
        admin_mixin.bitbucket.post.return_value = {"slug": "fork-name"}

        admin_mixin.fork_repository(
            "SRC",
            "repo",
            dest_project="DEST",
            name="fork-name",
        )

        data = admin_mixin.bitbucket.post.call_args[1]["data"]
        assert data == {"project": {"key": "DEST"}, "name": "fork-name"}

    def test_non_dict_response_raises_value_error(self, admin_mixin):
        admin_mixin.bitbucket.post.return_value = ["not", "a", "dict"]

        with pytest.raises(ValueError):
            admin_mixin.fork_repository("SRC", "repo", dest_project="DEST")


class TestCreateProject:
    """Happy-path coverage for ``AdminMixin.create_project``."""

    def test_posts_to_projects_endpoint(self, admin_mixin):
        admin_mixin.bitbucket.post.return_value = {
            "key": "NEWPROJ",
            "name": "New Project",
        }

        result = admin_mixin.create_project(
            key="NEWPROJ",
            name="New Project",
        )

        admin_mixin.bitbucket.post.assert_called_once()
        called_url = admin_mixin.bitbucket.post.call_args[0][0]
        assert called_url == "/rest/api/latest/projects"
        data = admin_mixin.bitbucket.post.call_args[1]["data"]
        # ``description`` is omitted when unset so Bitbucket does not
        # overwrite any server-side default with an empty string.
        assert data == {"key": "NEWPROJ", "name": "New Project", "public": False}
        assert "description" not in data
        assert result["key"] == "NEWPROJ"

    def test_forwards_optional_description_and_public_flag(self, admin_mixin):
        admin_mixin.bitbucket.post.return_value = {"key": "PUBPROJ"}

        admin_mixin.create_project(
            key="PUBPROJ",
            name="Public",
            description="A public project",
            public=True,
        )

        data = admin_mixin.bitbucket.post.call_args[1]["data"]
        assert data["description"] == "A public project"
        assert data["public"] is True

    def test_non_dict_response_raises_value_error(self, admin_mixin):
        admin_mixin.bitbucket.post.return_value = 42

        with pytest.raises(ValueError):
            admin_mixin.create_project(key="K", name="N")


class TestUpdateProject:
    """Happy-path coverage for ``AdminMixin.update_project``."""

    def test_puts_only_supplied_fields(self, admin_mixin):
        admin_mixin.bitbucket.put.return_value = {
            "key": "PROJ",
            "name": "Renamed",
        }

        result = admin_mixin.update_project(
            "PROJ",
            name="Renamed",
            description="new desc",
        )

        admin_mixin.bitbucket.put.assert_called_once()
        called_url = admin_mixin.bitbucket.put.call_args[0][0]
        assert called_url == "/rest/api/latest/projects/PROJ"
        data = admin_mixin.bitbucket.put.call_args[1]["data"]
        assert data == {"name": "Renamed", "description": "new desc"}
        # Omitted optional keys must not appear in the PUT body.
        assert "avatar" not in data
        assert "public" not in data
        assert result["name"] == "Renamed"

    def test_empty_fields_still_issues_put(self, admin_mixin):
        admin_mixin.bitbucket.put.return_value = {"key": "PROJ"}

        admin_mixin.update_project("PROJ")

        data = admin_mixin.bitbucket.put.call_args[1]["data"]
        assert data == {}

    def test_non_dict_response_raises_value_error(self, admin_mixin):
        admin_mixin.bitbucket.put.return_value = "not-a-dict"

        with pytest.raises(ValueError):
            admin_mixin.update_project("PROJ", name="x")


# ---------------------------------------------------------------------------
# Forbidden-capability checks — Req 4.4, 5.3
# ---------------------------------------------------------------------------
#
# The admin mixin MUST NOT expose repository or project deletion. These
# assertions pin the prohibition at the Python-module layer: if a
# future patch adds either method to the mixin the test fails before
# the server-registration parity test runs.


class TestNoDeleteEndpointsExist:
    """Req 4.4 / 5.3: no delete primitives exist in ``admin.py``."""

    @pytest.mark.parametrize("attr", ["delete_repository", "delete_project"])
    def test_module_has_no_delete_attribute(self, attr):
        # Accessing a non-existent module-level attribute must raise
        # AttributeError. ``hasattr`` alone would silently hide a
        # non-callable stub (e.g. ``delete_repository = None``); using
        # ``getattr`` with a sentinel + assertion makes the contract
        # explicit.
        sentinel = object()
        value = getattr(admin_module, attr, sentinel)
        assert value is sentinel, (
            f"Admin module must not expose {attr!r}; delete endpoints are "
            "intentionally omitted per DC tool-parity Req 4.4 / 5.3."
        )

    @pytest.mark.parametrize("attr", ["delete_repository", "delete_project"])
    def test_mixin_has_no_callable_delete_method(self, attr):
        # Same invariant at the class level: the mixin must not carry a
        # ``delete_*`` method (callable or otherwise) even if a sibling
        # module ever re-exported one at module scope.
        sentinel = object()
        value = getattr(AdminMixin, attr, sentinel)
        assert value is sentinel or not callable(value), (
            f"AdminMixin must not expose a callable {attr!r}; delete "
            "endpoints are intentionally omitted per Req 4.4 / 5.3."
        )


# ---------------------------------------------------------------------------
# Server-tool filter-scope tests — Req 4.5, 5.3
# ---------------------------------------------------------------------------


class _FakeContext:
    """Minimal stand-in for :class:`fastmcp.Context` used by tool funcs."""


@pytest.fixture
def fake_ctx() -> _FakeContext:
    return _FakeContext()


@pytest.fixture
def fake_fetcher():
    """Fetcher stub exposing ``config.projects_filter`` and admin methods."""
    fetcher = MagicMock()
    fetcher.config = SimpleNamespace(projects_filter=None)
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
async def test_fork_repository_filtered_out_on_destination_project(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Req 4.5: filter scope is evaluated against ``dest_project``.

    The source project may be in the operator's allow-list, but the
    fork would land outside it — the tool must short-circuit with the
    structured ``filtered_out`` envelope and issue zero HTTP.
    """
    from mcp_atlassian.servers.bitbucket import fork_repository

    # Allow only the SOURCE project; the destination (``DEST``) is
    # out-of-scope so the guard must fire.
    patch_get_fetcher.config.projects_filter = "SRC"

    result_json = await fork_repository.fn(
        fake_ctx,
        source_project="SRC",
        source_slug="repo",
        dest_project="DEST",
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "filtered_out"
    # The guard reports the offending key (the destination), not the
    # source, so operators can tell which side of the fork failed.
    assert payload["details"]["key"] == "DEST"
    assert payload["details"]["product"] == "bitbucket"
    # Zero HTTP: the mixin fork method must not have been called.
    patch_get_fetcher.fork_repository.assert_not_called()


@pytest.mark.anyio
async def test_create_project_filtered_out_on_new_project_key(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Req 5.3-adjacent: a new project ``key`` outside
    ``BITBUCKET_PROJECTS_FILTER`` must be rejected before any HTTP call.

    This prevents callers from using the admin toolset to create
    projects outside the operator-configured scope.
    """
    from mcp_atlassian.servers.bitbucket import create_project

    patch_get_fetcher.config.projects_filter = "ALLOWED"

    result_json = await create_project.fn(
        fake_ctx,
        key="BLOCKED",
        name="Blocked Project",
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "filtered_out"
    assert payload["details"]["key"] == "BLOCKED"
    assert payload["details"]["product"] == "bitbucket"
    # Zero HTTP: the mixin create_project method must not have been called.
    patch_get_fetcher.create_project.assert_not_called()
