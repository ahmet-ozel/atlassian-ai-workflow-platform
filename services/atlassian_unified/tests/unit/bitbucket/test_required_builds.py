"""Tests for RequiredBuildsMixin and the Bitbucket required-builds server tools.

These tests cover Requirement 3 from the ``atlassian-dc-tool-parity``
spec, with emphasis on Requirement 3.4 (plugin_unavailable mapping):

* ``RequiredBuildsMixin`` methods (``list_required_builds``,
  ``create_required_build``, ``delete_required_build``) wrap the
  required-builds plugin endpoints under
  ``/rest/required-builds/latest/projects/{k}/repos/{r}/condition``
  and translate a 404 response into
  :class:`RequiredBuildsPluginUnavailableError` (Req 3.4).
* Server tools (``list_required_builds``, ``create_required_build``,
  ``delete_required_build``) catch that exception and return a
  structured ``plugin_unavailable`` error naming the plugin, with zero
  HTTP leakage beyond the underlying 404 (Req 3.4).
* Happy-path for list / create / delete returns ``success=True`` with
  the expected payload shape.
* Non-404 HTTP errors (e.g. 500) propagate through the mixin and
  surface as a generic ``success=False`` error envelope at the tool
  layer rather than being coerced into ``plugin_unavailable``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from requests.exceptions import HTTPError

from mcp_atlassian.bitbucket.config import BitbucketConfig
from mcp_atlassian.bitbucket.required_builds import (
    RequiredBuildsMixin,
    RequiredBuildsPluginUnavailableError,
    _is_plugin_unavailable,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _http_error(status: int) -> HTTPError:
    """Build an ``HTTPError`` carrying a response with ``status`` status code.

    ``_is_plugin_unavailable`` inspects ``error.response.status_code``; the
    helper keeps the test setup compact and consistent across the suite.
    """
    response = MagicMock()
    response.status_code = status
    return HTTPError(response=response)


# ---------------------------------------------------------------------------
# Mixin fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bitbucket_config() -> BitbucketConfig:
    """Minimal BitbucketConfig suitable for instantiating mixins in tests."""
    return BitbucketConfig(
        url="https://bb.example.com",
        auth_type="pat",
        personal_token="test-pat",
    )


@pytest.fixture
def required_builds_mixin(bitbucket_config):
    """Create a ``RequiredBuildsMixin`` with mocked Bitbucket transport.

    The mixin inherits from :class:`BitbucketClient`, whose constructor
    instantiates an ``atlassian.Bitbucket`` session. We patch that
    constructor so no real HTTP session is built, then replace
    ``mixin.bitbucket`` with a fresh ``MagicMock`` the tests drive.
    """
    with patch("mcp_atlassian.bitbucket.client.Bitbucket") as mock_bitbucket_class:
        mock_bitbucket_class.return_value = MagicMock()
        mixin = RequiredBuildsMixin(config=bitbucket_config)
        mixin.bitbucket = MagicMock()
        return mixin


# ---------------------------------------------------------------------------
# Mixin-level tests
# ---------------------------------------------------------------------------


class TestIsPluginUnavailable:
    """Unit tests for the :func:`_is_plugin_unavailable` predicate."""

    def test_true_when_status_is_404(self):
        assert _is_plugin_unavailable(_http_error(404)) is True

    @pytest.mark.parametrize("status", [200, 401, 403, 409, 500, 502])
    def test_false_for_non_404_statuses(self, status):
        assert _is_plugin_unavailable(_http_error(status)) is False

    def test_false_when_response_is_missing(self):
        # HTTPError without a response object (transport-layer failure)
        # must not be treated as plugin_unavailable.
        err = HTTPError()
        assert _is_plugin_unavailable(err) is False


class TestRequiredBuildsMixinList:
    """Unit tests for ``RequiredBuildsMixin.list_required_builds``."""

    def test_returns_conditions_happy_path(self, required_builds_mixin):
        required_builds_mixin.bitbucket.get.return_value = {
            "values": [
                {
                    "id": 1,
                    "buildParentKeys": ["PROJ-PLAN"],
                    "refMatcher": {
                        "id": "refs/heads/main",
                        "type": {"id": "BRANCH"},
                    },
                },
            ],
            "isLastPage": True,
        }

        result = required_builds_mixin.list_required_builds("PROJ", "repo")

        # ``_get_paged_results`` calls ``self.bitbucket.get`` exactly once for
        # a single-page response with ``isLastPage=True``.
        required_builds_mixin.bitbucket.get.assert_called_once()
        called_url = required_builds_mixin.bitbucket.get.call_args[0][0]
        assert called_url == (
            "/rest/required-builds/latest/projects/PROJ/repos/repo/condition"
        )
        assert len(result) == 1
        assert result[0]["id"] == 1
        assert result[0]["buildParentKeys"] == ["PROJ-PLAN"]

    def test_404_raises_plugin_unavailable(self, required_builds_mixin):
        required_builds_mixin.bitbucket.get.side_effect = _http_error(404)

        with pytest.raises(RequiredBuildsPluginUnavailableError) as exc_info:
            required_builds_mixin.list_required_builds("PROJ", "repo")

        # The exception message names the exact path the 404 came from so
        # operators can locate the missing plugin endpoint.
        assert "/rest/required-builds/latest/projects/PROJ/repos/repo/condition" in str(
            exc_info.value
        )
        # Chained cause preserves the original HTTPError for diagnosis.
        assert isinstance(exc_info.value.__cause__, HTTPError)

    @pytest.mark.parametrize("status", [401, 403, 500, 502])
    def test_other_http_errors_propagate(self, required_builds_mixin, status):
        err = _http_error(status)
        required_builds_mixin.bitbucket.get.side_effect = err

        # Non-404 failures must surface as the original HTTPError so the
        # server-tool layer can render them via its generic error path.
        with pytest.raises(HTTPError) as exc_info:
            required_builds_mixin.list_required_builds("PROJ", "repo")

        assert exc_info.value is err


class TestRequiredBuildsMixinCreate:
    """Unit tests for ``RequiredBuildsMixin.create_required_build``."""

    def test_posts_condition_happy_path(self, required_builds_mixin):
        required_builds_mixin.bitbucket.post.return_value = {
            "id": 42,
            "buildParentKeys": ["PROJ-PLAN"],
            "refMatcher": {"id": "refs/heads/main", "type": {"id": "BRANCH"}},
        }

        result = required_builds_mixin.create_required_build(
            "PROJ",
            "repo",
            build_parent_keys=["PROJ-PLAN"],
            ref_matcher={"id": "refs/heads/main", "type": {"id": "BRANCH"}},
        )

        required_builds_mixin.bitbucket.post.assert_called_once()
        called_args = required_builds_mixin.bitbucket.post.call_args
        assert called_args[0][0] == (
            "/rest/required-builds/latest/projects/PROJ/repos/repo/condition"
        )
        data = called_args[1]["data"]
        assert data["buildParentKeys"] == ["PROJ-PLAN"]
        assert data["refMatcher"] == {
            "id": "refs/heads/main",
            "type": {"id": "BRANCH"},
        }
        # exemption_matcher omitted from body when not provided.
        assert "exemptionMatcher" not in data
        assert result["id"] == 42

    def test_exemption_matcher_passed_when_supplied(self, required_builds_mixin):
        required_builds_mixin.bitbucket.post.return_value = {"id": 7}
        exemption = {"type": "group", "value": "release-managers"}

        required_builds_mixin.create_required_build(
            "PROJ",
            "repo",
            build_parent_keys=["PLAN"],
            ref_matcher={"id": "refs/heads/main", "type": {"id": "BRANCH"}},
            exemption_matcher=exemption,
        )

        data = required_builds_mixin.bitbucket.post.call_args[1]["data"]
        assert data["exemptionMatcher"] == exemption

    def test_404_raises_plugin_unavailable(self, required_builds_mixin):
        required_builds_mixin.bitbucket.post.side_effect = _http_error(404)

        with pytest.raises(RequiredBuildsPluginUnavailableError):
            required_builds_mixin.create_required_build(
                "PROJ",
                "repo",
                build_parent_keys=["PLAN"],
                ref_matcher={"id": "refs/heads/main", "type": {"id": "BRANCH"}},
            )

    def test_other_http_errors_propagate(self, required_builds_mixin):
        err = _http_error(500)
        required_builds_mixin.bitbucket.post.side_effect = err

        with pytest.raises(HTTPError) as exc_info:
            required_builds_mixin.create_required_build(
                "PROJ",
                "repo",
                build_parent_keys=["PLAN"],
                ref_matcher={"id": "refs/heads/main", "type": {"id": "BRANCH"}},
            )
        assert exc_info.value is err


class TestRequiredBuildsMixinDelete:
    """Unit tests for ``RequiredBuildsMixin.delete_required_build``."""

    def test_delete_calls_correct_url(self, required_builds_mixin):
        required_builds_mixin.bitbucket.delete.return_value = None

        required_builds_mixin.delete_required_build("PROJ", "repo", 99)

        required_builds_mixin.bitbucket.delete.assert_called_once_with(
            "/rest/required-builds/latest/projects/PROJ/repos/repo/condition/99"
        )

    def test_404_raises_plugin_unavailable(self, required_builds_mixin):
        required_builds_mixin.bitbucket.delete.side_effect = _http_error(404)

        with pytest.raises(RequiredBuildsPluginUnavailableError):
            required_builds_mixin.delete_required_build("PROJ", "repo", 99)

    def test_other_http_errors_propagate(self, required_builds_mixin):
        err = _http_error(403)
        required_builds_mixin.bitbucket.delete.side_effect = err

        with pytest.raises(HTTPError) as exc_info:
            required_builds_mixin.delete_required_build("PROJ", "repo", 99)
        assert exc_info.value is err


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
    """Fetcher stub exposing ``config.projects_filter`` and required-build methods."""
    fetcher = MagicMock()
    fetcher.is_cloud = False
    fetcher.config = SimpleNamespace(is_cloud=False, projects_filter=None)
    fetcher.list_required_builds.return_value = [
        {
            "id": 1,
            "buildParentKeys": ["PROJ-PLAN"],
            "refMatcher": {"id": "refs/heads/main", "type": {"id": "BRANCH"}},
        }
    ]
    fetcher.create_required_build.return_value = {
        "id": 42,
        "buildParentKeys": ["PROJ-PLAN"],
        "refMatcher": {"id": "refs/heads/main", "type": {"id": "BRANCH"}},
    }
    fetcher.delete_required_build.return_value = None
    return fetcher


@pytest.fixture
def patch_get_fetcher(monkeypatch, fake_fetcher):
    """Patch ``get_bitbucket_fetcher`` so tool functions return ``fake_fetcher``."""
    from mcp_atlassian.servers import bitbucket as bitbucket_server

    async def _aget(_ctx):
        return fake_fetcher

    monkeypatch.setattr(bitbucket_server, "get_bitbucket_fetcher", _aget)
    return fake_fetcher


@pytest.fixture
def disable_read_only(monkeypatch):
    """Ensure ``READ_ONLY_MODE`` is unset for happy-path tests."""
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)


# ---- list_required_builds --------------------------------------------------


@pytest.mark.anyio
async def test_list_required_builds_happy_path(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.bitbucket import list_required_builds

    result_json = await list_required_builds.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo"
    )
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["count"] == 1
    assert payload["conditions"][0]["id"] == 1
    patch_get_fetcher.list_required_builds.assert_called_once_with(
        "PROJ", "repo", limit=100
    )


@pytest.mark.anyio
async def test_list_required_builds_returns_plugin_unavailable_on_missing_plugin(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.bitbucket import list_required_builds

    patch_get_fetcher.list_required_builds.side_effect = (
        RequiredBuildsPluginUnavailableError(
            "Bitbucket required-builds plugin endpoint is unavailable "
            "(HTTP 404 from /rest/required-builds/latest/projects/PROJ/"
            "repos/repo/condition)."
        )
    )

    result_json = await list_required_builds.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo"
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "plugin_unavailable"
    assert payload["details"]["plugin"] == "required-builds"
    assert payload["details"]["product"] == "bitbucket"


@pytest.mark.anyio
async def test_list_required_builds_surfaces_500_generically(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Non-404 HTTP errors should NOT be mapped to plugin_unavailable."""
    from mcp_atlassian.servers.bitbucket import list_required_builds

    patch_get_fetcher.list_required_builds.side_effect = _http_error(500)

    result_json = await list_required_builds.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo"
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    # The generic error path carries an "error" string rather than the
    # structured plugin_unavailable envelope.
    assert "error" in payload
    assert payload.get("error_code") != "plugin_unavailable"


@pytest.mark.anyio
async def test_list_required_builds_blocked_by_project_filter(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.bitbucket import list_required_builds

    patch_get_fetcher.config.projects_filter = "OTHER"

    result_json = await list_required_builds.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo"
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "filtered_out"
    patch_get_fetcher.list_required_builds.assert_not_called()


# ---- create_required_build -------------------------------------------------


@pytest.mark.anyio
async def test_create_required_build_happy_path(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.bitbucket import create_required_build

    build_parent_keys = json.dumps(["PROJ-PLAN"])
    ref_matcher = json.dumps(
        {"id": "refs/heads/main", "type": {"id": "BRANCH"}}
    )

    result_json = await create_required_build.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        build_parent_keys=build_parent_keys,
        ref_matcher=ref_matcher,
    )
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["condition"]["id"] == 42
    patch_get_fetcher.create_required_build.assert_called_once()
    kwargs = patch_get_fetcher.create_required_build.call_args.kwargs
    assert kwargs["build_parent_keys"] == ["PROJ-PLAN"]
    assert kwargs["ref_matcher"] == {
        "id": "refs/heads/main",
        "type": {"id": "BRANCH"},
    }
    assert kwargs["exemption_matcher"] is None


@pytest.mark.anyio
async def test_create_required_build_returns_plugin_unavailable_on_missing_plugin(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.bitbucket import create_required_build

    patch_get_fetcher.create_required_build.side_effect = (
        RequiredBuildsPluginUnavailableError(
            "Bitbucket required-builds plugin endpoint is unavailable."
        )
    )

    result_json = await create_required_build.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        build_parent_keys=json.dumps(["PROJ-PLAN"]),
        ref_matcher=json.dumps(
            {"id": "refs/heads/main", "type": {"id": "BRANCH"}}
        ),
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "plugin_unavailable"
    assert payload["details"]["plugin"] == "required-builds"


@pytest.mark.anyio
async def test_create_required_build_blocked_in_read_only(
    fake_ctx, patch_get_fetcher, monkeypatch
):
    from mcp_atlassian.servers.bitbucket import create_required_build

    monkeypatch.setenv("READ_ONLY_MODE", "true")

    result_json = await create_required_build.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        build_parent_keys=json.dumps(["PROJ-PLAN"]),
        ref_matcher=json.dumps(
            {"id": "refs/heads/main", "type": {"id": "BRANCH"}}
        ),
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "read_only_mode"
    patch_get_fetcher.create_required_build.assert_not_called()


@pytest.mark.anyio
async def test_create_required_build_blocked_by_project_filter(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.bitbucket import create_required_build

    patch_get_fetcher.config.projects_filter = "OTHER"

    result_json = await create_required_build.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        build_parent_keys=json.dumps(["PROJ-PLAN"]),
        ref_matcher=json.dumps(
            {"id": "refs/heads/main", "type": {"id": "BRANCH"}}
        ),
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "filtered_out"
    patch_get_fetcher.create_required_build.assert_not_called()


# ---- delete_required_build -------------------------------------------------


@pytest.mark.anyio
async def test_delete_required_build_happy_path(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.bitbucket import delete_required_build

    result_json = await delete_required_build.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo", condition_id=99
    )
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["condition_id"] == 99
    assert payload["deleted"] is True
    patch_get_fetcher.delete_required_build.assert_called_once_with(
        "PROJ", "repo", 99
    )


@pytest.mark.anyio
async def test_delete_required_build_returns_plugin_unavailable_on_missing_plugin(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.bitbucket import delete_required_build

    patch_get_fetcher.delete_required_build.side_effect = (
        RequiredBuildsPluginUnavailableError(
            "Bitbucket required-builds plugin endpoint is unavailable."
        )
    )

    result_json = await delete_required_build.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo", condition_id=99
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "plugin_unavailable"
    assert payload["details"]["plugin"] == "required-builds"


@pytest.mark.anyio
async def test_delete_required_build_surfaces_500_generically(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Non-404 HTTP errors should NOT be mapped to plugin_unavailable."""
    from mcp_atlassian.servers.bitbucket import delete_required_build

    patch_get_fetcher.delete_required_build.side_effect = _http_error(500)

    result_json = await delete_required_build.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo", condition_id=99
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert "error" in payload
    assert payload.get("error_code") != "plugin_unavailable"


@pytest.mark.anyio
async def test_delete_required_build_blocked_in_read_only(
    fake_ctx, patch_get_fetcher, monkeypatch
):
    from mcp_atlassian.servers.bitbucket import delete_required_build

    monkeypatch.setenv("READ_ONLY_MODE", "true")

    result_json = await delete_required_build.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo", condition_id=99
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "read_only_mode"
    patch_get_fetcher.delete_required_build.assert_not_called()


# ---- Registration / tagging parity -----------------------------------------


@pytest.mark.anyio
async def test_required_build_tools_have_expected_tags():
    """Ensure tool tags match Requirement 3.1 / 3.2 / 3.3."""
    from mcp_atlassian.servers.bitbucket import (
        create_required_build,
        delete_required_build,
        list_required_builds,
    )

    read_tags = {"bitbucket", "read", "toolset:bitbucket_required_builds"}
    write_tags = {"bitbucket", "write", "toolset:bitbucket_required_builds"}

    assert set(list_required_builds.tags) == read_tags
    assert set(create_required_build.tags) == write_tags
    assert set(delete_required_build.tags) == write_tags


# ---- Structured-error-code allowlist parity (Req 3.4) -----------------------


def test_plugin_unavailable_is_in_dc_guards_error_codes():
    """``plugin_unavailable`` must be an allowed StructuredError code.

    Requirement 3.4 states that the required-builds tools surface a
    ``plugin_unavailable`` error when the plugin endpoint is missing.
    The ``dc_guards.ERROR_CODES`` frozenset is the single source of
    truth for allowed error codes; any code used in a tool response
    must be a member, otherwise ``StructuredError`` construction would
    raise at runtime. This test pins that guarantee at the unit level
    so a drift in either file is caught before integration.
    """
    from mcp_atlassian.utils.dc_guards import ERROR_CODES

    assert "plugin_unavailable" in ERROR_CODES
