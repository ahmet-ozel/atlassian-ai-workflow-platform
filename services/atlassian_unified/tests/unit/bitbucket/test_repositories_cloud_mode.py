"""Unit tests for the Cloud branch of :class:`RepositoriesMixin`.

These tests cover task 7.2 of the ``bitbucket-cloud-dc-parity`` spec and
the following acceptance-criteria slices:

* Requirement 2.6 — missing workspace raises a ``filtered_out`` error
  before any outbound HTTP call.
* Requirements 8.1 through 8.7 — every repository/project method targets
  the mode-appropriate Cloud 2.0 URL prefix and the response normalizer
  round-trips ``slug`` plus synthesizes the DC-shaped ``project`` wrapper
  (with ``project.key == workspace``).
* Requirement 19.1 / 19.2 — Cloud-branch tests live alongside the
  existing DC tests without modifying them; the DC call shape is not
  changed as a side effect of these tests.

Test strategy
-------------

These tests bypass :meth:`BitbucketClient.__init__` entirely by
constructing the mixin with ``RepositoriesMixin.__new__`` and stamping
the two attributes the Cloud branches actually read — ``mixin.bitbucket``
(the atlassian-python-api transport) and ``mixin.config``. Using a
:class:`~types.SimpleNamespace` for ``config`` keeps the fixture
side-effect-free: the ``is_cloud`` attribute is a plain boolean, not the
URL-derived property, so the test does not accidentally exercise the
config-layer classifier. The real classifier already has its own test
suite (``test_config_is_cloud.py``).

The ``mixin.bitbucket.get`` / ``.post`` / ``.put`` / ``.delete`` methods
are driven via :attr:`MagicMock.return_value` or :attr:`side_effect`.
For methods that bypass the ``atlassian.Bitbucket.get`` helper and call
``session.get`` directly (``get_file_content`` / ``get_raw_file_content``),
we configure ``mixin.bitbucket._session.get`` instead.

Every test asserts:

1. The outbound URL prefix matches the Cloud 2.0 template documented in
   the design (e.g. ``/2.0/repositories/my-team/...``).
2. The returned payload is normalized to the DC shape — repository
   responses carry a synthesized ``project`` dict with ``key`` equal to
   the active workspace.
3. Missing-workspace scenarios raise ``ValueError`` with a
   ``filtered_out:`` prefix *and* issue zero outbound HTTP calls
   (``mixin.bitbucket.get.call_count == 0``).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from mcp_atlassian.bitbucket.repositories import RepositoriesMixin


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_cloud_mixin(
    *,
    workspace: str | None = "my-team",
    url: str = "https://api.bitbucket.org",
    projects_filter: str | None = None,
) -> RepositoriesMixin:
    """Create a ``RepositoriesMixin`` wired for CloudMode without HTTP.

    ``RepositoriesMixin.__new__(RepositoriesMixin)`` skips the
    :class:`BitbucketClient` constructor chain (and therefore avoids
    constructing an :class:`atlassian.Bitbucket` transport). We then
    stamp the two attributes the Cloud branches read at runtime:

    * ``config`` — a :class:`SimpleNamespace` exposing the exact subset
      of :class:`BitbucketConfig` fields the mixin touches in Cloud
      mode: ``is_cloud``, ``workspace``, ``url``, ``ssl_verify``,
      ``projects_filter``.
    * ``bitbucket`` — a :class:`MagicMock` standing in for the
      ``atlassian.Bitbucket`` transport; tests drive ``.get`` / ``.post``
      / ``.put`` / ``.delete`` / ``._session.get`` via
      :attr:`MagicMock.return_value` or :attr:`side_effect`.
    """
    mixin = RepositoriesMixin.__new__(RepositoriesMixin)
    mixin.config = SimpleNamespace(  # type: ignore[attr-defined]
        is_cloud=True,
        workspace=workspace,
        url=url,
        ssl_verify=True,
        projects_filter=projects_filter,
        timeout=75,
    )
    mixin.bitbucket = MagicMock()  # type: ignore[attr-defined]
    return mixin


def _cloud_envelope(values: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a single-page Cloud 2.0 pagination envelope."""
    return {
        "values": values,
        "page": 1,
        "pagelen": max(len(values), 10),
        "size": len(values),
        # No ``next`` key → terminates after one page per Requirement 7.3.
    }


# ---------------------------------------------------------------------------
# Requirement 8.1 / 8.7 — list projects (list workspaces on Cloud)
# ---------------------------------------------------------------------------


class TestGetProjectsCloud:
    """``get_projects`` on Cloud raises pre-HTTP because Atlassian removed
    the ``GET /2.0/workspaces`` global listing endpoint in CHANGE-2770
    (September 2025). There is no replacement API for "list every
    workspace I can see"; callers must pass a workspace slug explicitly
    to workspace-scoped tools instead.
    """

    def test_raises_not_supported_on_cloud_without_http(self) -> None:
        """The Cloud branch of ``get_projects`` raises a
        ``not_supported_on_cloud``-tagged ``ValueError`` before issuing
        any HTTP request, so callers see an actionable message instead
        of a 410 Gone from the removed endpoint.
        """
        mixin = _make_cloud_mixin()

        with pytest.raises(ValueError, match=r"^not_supported_on_cloud:"):
            mixin.get_projects(limit=10)

        # Zero outbound HTTP was issued — the guard fires before any
        # bitbucket.get call. This matches the DC-only-tool guard shape
        # used elsewhere in the server layer (Requirement 14.10).
        assert mixin.bitbucket.get.call_count == 0

    def test_projects_filter_not_touched_on_cloud(self) -> None:
        """Even with a configured ``BITBUCKET_PROJECTS_FILTER``, the
        Cloud branch still short-circuits before touching the filter —
        the tool is unavailable on Cloud regardless of server config.
        """
        mixin = _make_cloud_mixin(projects_filter="my-team")

        with pytest.raises(ValueError, match=r"^not_supported_on_cloud:"):
            mixin.get_projects(limit=10)

        assert mixin.bitbucket.get.call_count == 0


# ---------------------------------------------------------------------------
# Requirement 8.7 — get a single project (get workspace on Cloud)
# ---------------------------------------------------------------------------


class TestGetProjectCloud:
    """``get_project`` on Cloud fetches a single workspace and projects
    it into the DC-shaped project envelope.
    """

    def test_targets_workspace_endpoint_with_resolved_workspace(self) -> None:
        """The ``project_key`` arg wins over ``config.workspace`` per the
        design's workspace-resolution precedence rules.
        """
        mixin = _make_cloud_mixin(workspace="default-team")
        mixin.bitbucket.get.return_value = {
            "slug": "explicit-team",
            "name": "Explicit",
            "type": "workspace",
        }

        result = mixin.get_project("explicit-team")

        assert mixin.bitbucket.get.call_count == 1
        # ``project_key`` wins (Requirement 2.4) — ``default-team`` must
        # not appear in the URL when an explicit value is supplied.
        assert mixin.bitbucket.get.call_args.args[0] == (
            "/2.0/workspaces/explicit-team"
        )
        assert result["key"] == "explicit-team"
        assert result["name"] == "Explicit"

    def test_falls_through_to_config_workspace(self) -> None:
        """When ``project_key`` is empty, the mixin falls through to
        ``config.workspace`` (Requirement 2.5).
        """
        mixin = _make_cloud_mixin(workspace="my-team")
        mixin.bitbucket.get.return_value = {"slug": "my-team", "name": "My Team"}

        result = mixin.get_project("")

        assert mixin.bitbucket.get.call_args.args[0] == "/2.0/workspaces/my-team"
        assert result["key"] == "my-team"

    def test_filtered_out_when_workspace_missing(self) -> None:
        """Empty ``project_key`` and no ``config.workspace`` raises
        pre-HTTP ``filtered_out`` (Requirement 2.6).
        """
        mixin = _make_cloud_mixin(workspace=None)

        with pytest.raises(ValueError, match=r"^filtered_out:"):
            mixin.get_project("")

        # Zero outbound HTTP was issued.
        assert mixin.bitbucket.get.call_count == 0


# ---------------------------------------------------------------------------
# Requirement 8.1 / 8.2 — list repositories
# ---------------------------------------------------------------------------


class TestGetRepositoriesCloud:
    """``get_repositories`` on Cloud targets
    ``/2.0/repositories/{workspace}`` and normalizes each value.
    """

    def test_targets_workspace_repositories_endpoint(self) -> None:
        mixin = _make_cloud_mixin()
        mixin.bitbucket.get.return_value = _cloud_envelope(
            [
                {
                    "slug": "repo-a",
                    "name": "Repo A",
                    "full_name": "my-team/repo-a",
                    "uuid": "{uuid-a}",
                    "workspace": {"slug": "my-team", "name": "My Team"},
                },
                {
                    "slug": "repo-b",
                    "name": "Repo B",
                    "full_name": "my-team/repo-b",
                    "uuid": "{uuid-b}",
                    "workspace": {"slug": "my-team", "name": "My Team"},
                },
            ]
        )

        result = mixin.get_repositories("my-team", limit=25)

        assert mixin.bitbucket.get.call_count == 1
        assert mixin.bitbucket.get.call_args.args[0] == "/2.0/repositories/my-team"

        # Req 21.3 — slug round-trips through the normalizer.
        assert [r["slug"] for r in result] == ["repo-a", "repo-b"]
        # Req 8.7 — synthetic ``project`` wrapper with ``key == workspace``.
        assert all(r["project"]["key"] == "my-team" for r in result)

    def test_filtered_out_when_workspace_missing(self) -> None:
        mixin = _make_cloud_mixin(workspace=None)

        with pytest.raises(ValueError, match=r"^filtered_out:"):
            mixin.get_repositories("")

        assert mixin.bitbucket.get.call_count == 0


# ---------------------------------------------------------------------------
# Requirement 8.4 — get a single repository
# ---------------------------------------------------------------------------


class TestGetRepositoryCloud:
    """``get_repository`` on Cloud targets
    ``/2.0/repositories/{workspace}/{slug}`` and normalizes the payload.
    """

    def test_targets_repository_endpoint_and_synthesizes_project(self) -> None:
        """The normalized payload exposes ``slug`` (identity round-trip)
        and a DC-shaped ``project`` dict with ``key`` equal to the
        active workspace (Requirements 8.4, 8.7, 21.3).
        """
        mixin = _make_cloud_mixin()
        mixin.bitbucket.get.return_value = {
            "slug": "repo-a",
            "name": "Repo A",
            "full_name": "my-team/repo-a",
            "uuid": "{uuid-a}",
            "workspace": {"slug": "my-team", "name": "My Team"},
        }

        result = mixin.get_repository("my-team", "repo-a")

        assert mixin.bitbucket.get.call_count == 1
        assert (
            mixin.bitbucket.get.call_args.args[0]
            == "/2.0/repositories/my-team/repo-a"
        )
        # ``slug`` round-trips identity.
        assert result["slug"] == "repo-a"
        # Synthesized DC-shaped project.
        assert result["project"] == {"key": "my-team", "name": "my-team"}
        # Cloud-native fields are preserved alongside the DC additions.
        assert result["uuid"] == "{uuid-a}"
        assert result["full_name"] == "my-team/repo-a"

    def test_explicit_project_key_wins_over_config_workspace(self) -> None:
        """Per Requirement 2.4, a non-empty ``project_key`` arg is
        interpreted as the workspace slug in Cloud mode and overrides
        ``config.workspace``.
        """
        mixin = _make_cloud_mixin(workspace="default-team")
        mixin.bitbucket.get.return_value = {
            "slug": "repo-a",
            "name": "Repo A",
            "workspace": {"slug": "explicit-team", "name": "Explicit"},
        }

        result = mixin.get_repository("explicit-team", "repo-a")

        assert (
            mixin.bitbucket.get.call_args.args[0]
            == "/2.0/repositories/explicit-team/repo-a"
        )
        assert result["project"]["key"] == "explicit-team"

    def test_filtered_out_when_workspace_missing(self) -> None:
        mixin = _make_cloud_mixin(workspace=None)

        with pytest.raises(ValueError, match=r"^filtered_out:"):
            mixin.get_repository("", "repo-a")

        assert mixin.bitbucket.get.call_count == 0


# ---------------------------------------------------------------------------
# Requirement 8.2 — search repositories (workspace-scoped BBQL query)
# ---------------------------------------------------------------------------


class TestSearchRepositoriesCloud:
    """``search_repositories`` on Cloud issues a BBQL-like query against
    the workspace-scoped ``/2.0/repositories/{workspace}`` endpoint.
    """

    def test_targets_workspace_endpoint_with_bbql_query(self) -> None:
        mixin = _make_cloud_mixin()
        mixin.bitbucket.get.return_value = _cloud_envelope(
            [
                {
                    "slug": "payments-api",
                    "name": "payments-api",
                    "full_name": "my-team/payments-api",
                    "workspace": {"slug": "my-team"},
                }
            ]
        )

        result = mixin.search_repositories("payments", limit=25)

        assert mixin.bitbucket.get.call_count == 1
        call = mixin.bitbucket.get.call_args
        assert call.args[0] == "/2.0/repositories/my-team"
        # BBQL-like substring query — ``name~"<text>"``. The Cloud
        # pagination helper additionally threads ``pagelen`` into the
        # first-page params, so assert the query key is present without
        # pinning the exact dict.
        params = call.kwargs.get("params") or {}
        assert params.get("q") == 'name~"payments"'

        assert [r["slug"] for r in result] == ["payments-api"]
        # Normalized synthetic project wrapper present.
        assert result[0]["project"]["key"] == "my-team"

    def test_filtered_out_when_workspace_unset(self) -> None:
        """``search_repositories`` always passes ``project_key=None``
        to :func:`_resolve_workspace`; when ``config.workspace`` is also
        ``None`` the mixin raises ``filtered_out`` before any HTTP call.
        """
        mixin = _make_cloud_mixin(workspace=None)

        with pytest.raises(ValueError, match=r"^filtered_out:"):
            mixin.search_repositories("payments", limit=25)

        assert mixin.bitbucket.get.call_count == 0


# ---------------------------------------------------------------------------
# Requirement 8.5 — file browse (text file content)
# ---------------------------------------------------------------------------


def _fake_response(body: bytes, *, status: int = 200) -> MagicMock:
    """Build a ``requests.Response``-like MagicMock.

    The Cloud branches that use the session directly (``get_file_content``,
    ``get_raw_file_content``, ``browse_directory``) need both:

    * ``.content`` (bytes) + ``.raise_for_status()`` for raw body callers, and
    * ``.json()`` for structured callers such as the browse_directory helper.

    When the supplied ``body`` is valid JSON, ``.json()`` returns the parsed
    dict/list; otherwise it raises ``ValueError`` to match ``requests``.
    """
    response = MagicMock()
    response.content = body
    response.status_code = status
    response.raise_for_status = MagicMock()

    def _json() -> Any:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Response body is not valid UTF-8")
        return json.loads(text)

    response.json = _json
    response.text = body.decode("utf-8", errors="replace") if body else ""
    return response


class TestGetFileContentCloud:
    """``get_file_content`` on Cloud targets the ``/src/{ref}/{path}``
    endpoint and decodes the response body as UTF-8 with replacement.
    """

    def test_targets_src_endpoint_with_head_default_ref(self) -> None:
        """When ``at`` is omitted, the Cloud branch uses ``HEAD`` as the
        ref segment (Requirement 8.5).
        """
        mixin = _make_cloud_mixin()
        mixin.bitbucket._session.get.return_value = _fake_response(
            b"line1\nline2\n"
        )

        result = mixin.get_file_content("my-team", "repo-a", "README.md")

        mixin.bitbucket._session.get.assert_called_once()
        url = mixin.bitbucket._session.get.call_args.args[0]
        assert url == (
            "https://api.bitbucket.org"
            "/2.0/repositories/my-team/repo-a/src/HEAD/README.md"
        )
        assert result == "line1\nline2\n"

    def test_targets_src_endpoint_with_explicit_ref(self) -> None:
        mixin = _make_cloud_mixin()
        mixin.bitbucket._session.get.return_value = _fake_response(
            b"hello\n"
        )

        mixin.get_file_content(
            "my-team", "repo-a", "src/app.py", at="feature/x"
        )

        url = mixin.bitbucket._session.get.call_args.args[0]
        assert url == (
            "https://api.bitbucket.org"
            "/2.0/repositories/my-team/repo-a/src/feature/x/src/app.py"
        )

    def test_filtered_out_when_workspace_missing(self) -> None:
        mixin = _make_cloud_mixin(workspace=None)

        with pytest.raises(ValueError, match=r"^filtered_out:"):
            mixin.get_file_content("", "repo-a", "README.md")

        # Neither the high-level ``.get`` nor the low-level session GET
        # was exercised — the precheck terminates before any outbound
        # HTTP call.
        assert mixin.bitbucket.get.call_count == 0
        assert mixin.bitbucket._session.get.call_count == 0


# ---------------------------------------------------------------------------
# Requirement 8.6 — raw file browse
# ---------------------------------------------------------------------------


class TestGetRawFileContentCloud:
    """``get_raw_file_content`` on Cloud uses ``format=raw`` on the
    ``/src/{ref}/{path}`` endpoint (Requirement 8.6).
    """

    def test_targets_src_endpoint_with_format_raw(self) -> None:
        mixin = _make_cloud_mixin()
        mixin.bitbucket._session.get.return_value = _fake_response(
            b"binary-ish"
        )

        result = mixin.get_raw_file_content(
            "my-team", "repo-a", "README.md", at="main"
        )

        mixin.bitbucket._session.get.assert_called_once()
        call = mixin.bitbucket._session.get.call_args
        assert call.args[0] == (
            "https://api.bitbucket.org"
            "/2.0/repositories/my-team/repo-a/src/main/README.md"
        )
        assert call.kwargs.get("params") == {"format": "raw"}
        assert result == "binary-ish"

    def test_filtered_out_when_workspace_missing(self) -> None:
        mixin = _make_cloud_mixin(workspace=None)

        with pytest.raises(ValueError, match=r"^filtered_out:"):
            mixin.get_raw_file_content("", "repo-a", "README.md")

        assert mixin.bitbucket._session.get.call_count == 0


# ---------------------------------------------------------------------------
# Requirement 8.5 — directory browse
# ---------------------------------------------------------------------------


class TestBrowseDirectoryCloud:
    """``browse_directory`` on Cloud walks ``/src/{ref}/{path}`` entries
    and maps ``commit_directory`` / ``commit_file`` onto the DC-shaped
    ``DIR`` / ``FILE`` markers.
    """

    def test_targets_src_endpoint_and_normalizes_types(self) -> None:
        mixin = _make_cloud_mixin()
        mixin.bitbucket._session.get.return_value = _fake_response(
            json.dumps(
                _cloud_envelope(
                    [
                        {"path": "src", "type": "commit_directory"},
                        {"path": "README.md", "type": "commit_file"},
                    ]
                )
            ).encode("utf-8")
        )

        result = mixin.browse_directory(
            "my-team", "repo-a", path="", at="main", limit=100
        )

        # Cloud browse_directory bypasses the atlassian-lib wrapper and
        # issues the request through the underlying session so trailing
        # slashes are preserved.
        assert mixin.bitbucket.get.call_count == 0
        mixin.bitbucket._session.get.assert_called_once()
        url = mixin.bitbucket._session.get.call_args.args[0]
        # Root listing uses a trailing slash on /src/{ref}/ — without it
        # Cloud responds with HTTP 404 "Resource not found".
        assert url == (
            "https://api.bitbucket.org"
            "/2.0/repositories/my-team/repo-a/src/main/"
        )

        assert result[0] == {
            "path": "src",
            "type": "DIR",
        }
        assert result[1] == {
            "path": "README.md",
            "type": "FILE",
        }

    def test_dot_path_is_treated_as_root(self) -> None:
        """``"."`` (and ``"./"``) are common agent idioms for "repository
        root". Cloud's ``/src/{ref}/.`` returns HTTP 500, so the mixin
        normalises them into the same URL shape used by the empty-path
        case.
        """
        mixin = _make_cloud_mixin()
        mixin.bitbucket._session.get.return_value = _fake_response(
            json.dumps(_cloud_envelope([])).encode("utf-8")
        )

        mixin.browse_directory(
            "my-team", "repo-a", path=".", at="main", limit=50
        )

        mixin.bitbucket._session.get.assert_called_once()
        assert (
            mixin.bitbucket._session.get.call_args.args[0]
            == "https://api.bitbucket.org"
            "/2.0/repositories/my-team/repo-a/src/main/"
        )

    def test_filtered_out_when_workspace_missing(self) -> None:
        mixin = _make_cloud_mixin(workspace=None)

        with pytest.raises(ValueError, match=r"^filtered_out:"):
            mixin.browse_directory("", "repo-a", path="src")

        assert mixin.bitbucket.get.call_count == 0


# ---------------------------------------------------------------------------
# Requirement 8.4 — default branch (projected from ``mainbranch``)
# ---------------------------------------------------------------------------


class TestGetDefaultBranchCloud:
    """``get_default_branch`` on Cloud fetches the repo and projects its
    ``mainbranch`` onto the DC-shaped branch envelope.
    """

    def test_targets_repository_endpoint_and_projects_mainbranch(self) -> None:
        mixin = _make_cloud_mixin()
        mixin.bitbucket.get.return_value = {
            "slug": "repo-a",
            "name": "Repo A",
            "mainbranch": {"name": "main", "type": "branch"},
            "workspace": {"slug": "my-team"},
        }

        result = mixin.get_default_branch("my-team", "repo-a")

        assert mixin.bitbucket.get.call_count == 1
        assert (
            mixin.bitbucket.get.call_args.args[0]
            == "/2.0/repositories/my-team/repo-a"
        )
        assert result == {
            "id": "refs/heads/main",
            "displayId": "main",
            "type": "branch",
        }

    def test_filtered_out_when_workspace_missing(self) -> None:
        mixin = _make_cloud_mixin(workspace=None)

        with pytest.raises(ValueError, match=r"^filtered_out:"):
            mixin.get_default_branch("", "repo-a")

        assert mixin.bitbucket.get.call_count == 0


# ---------------------------------------------------------------------------
# Cross-method invariant — URL prefix never leaks DC paths on Cloud
# ---------------------------------------------------------------------------


class TestCloudUrlPrefixInvariant:
    """No Cloud branch issues a DC-shaped URL. This guards against a
    future refactor that forgets to branch one method on ``self.is_cloud``.
    """

    def test_none_of_the_cloud_branches_issue_rest_api_latest_urls(
        self,
    ) -> None:
        mixin = _make_cloud_mixin()
        mixin.bitbucket.get.return_value = _cloud_envelope([])
        mixin.bitbucket._session.get.return_value = _fake_response(b"")

        # Exercise every Cloud branch that routes through either
        # ``.get`` or ``._session.get``. The repository-returning
        # methods use ``.get``; the file-content methods use
        # ``._session.get``.
        #
        # Note: ``get_projects`` is deliberately NOT exercised here — on
        # Cloud it short-circuits to ``not_supported_on_cloud`` before
        # issuing any HTTP, because Atlassian removed the underlying
        # ``/2.0/workspaces`` listing endpoint in CHANGE-2770.
        mixin.bitbucket.get.return_value = {"slug": "my-team"}
        mixin.get_project("my-team")
        mixin.bitbucket.get.return_value = _cloud_envelope([])
        mixin.get_repositories("my-team")
        mixin.bitbucket.get.return_value = {
            "slug": "repo-a",
            "workspace": {"slug": "my-team"},
        }
        mixin.get_repository("my-team", "repo-a")
        mixin.bitbucket.get.return_value = _cloud_envelope([])
        mixin.search_repositories("q")
        mixin.browse_directory("my-team", "repo-a", path="src", at="main")
        mixin.bitbucket.get.return_value = {
            "mainbranch": {"name": "main", "type": "branch"}
        }
        mixin.get_default_branch("my-team", "repo-a")
        mixin.get_file_content("my-team", "repo-a", "README.md")
        mixin.get_raw_file_content("my-team", "repo-a", "README.md")

        # Collect every URL we emitted across both transports.
        all_urls: list[str] = []
        for call in mixin.bitbucket.get.call_args_list:
            all_urls.append(call.args[0])
        for call in mixin.bitbucket._session.get.call_args_list:
            all_urls.append(call.args[0])

        assert all_urls, (
            "Test-setup sanity check — expected at least one Cloud URL "
            "to have been emitted across the exercised methods."
        )
        for url in all_urls:
            assert "/rest/api/latest/" not in url, (
                f"Cloud branch leaked a DC URL: {url!r}"
            )
            assert "/2.0/" in url, (
                f"Cloud branch emitted an unexpected URL: {url!r}"
            )
