"""Tests for ProjectRolesMixin and the Jira project roles server tools.

These tests cover Requirement 24 from the ``atlassian-dc-tool-parity``
spec:

* ``ProjectRolesMixin`` methods (``list_project_roles``,
  ``get_project_role_actors``) wrap the read-only DC endpoints under
  ``/rest/api/2/project/{projectIdOrKey}/role`` and return the raw
  payloads with defensive type guards.
* Server tools (``jira_list_project_roles``,
  ``jira_get_project_role_actors``) apply the ``check_project_filter``
  prelude with zero HTTP on reject, carry the
  ``toolset:jira_project_roles`` tag, and register NO write tools in
  that toolset (Req 24.2).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcp_atlassian.jira.project_roles import ProjectRolesMixin


# ---------------------------------------------------------------------------
# Mixin-level tests
# ---------------------------------------------------------------------------


@pytest.fixture
def project_roles_mixin(jira_client):
    """Create a ``ProjectRolesMixin`` instance with mocked Jira transport."""
    mixin = ProjectRolesMixin(config=jira_client.config)
    mixin.jira = MagicMock()
    return mixin


class TestProjectRolesMixinList:
    """Unit tests for ``ProjectRolesMixin.list_project_roles``."""

    def test_returns_role_map(self, project_roles_mixin):
        project_roles_mixin.jira.get.return_value = {
            "Administrators": "https://jira/rest/api/2/project/10000/role/10002",
            "Developers": "https://jira/rest/api/2/project/10000/role/10001",
        }

        result = project_roles_mixin.list_project_roles("PROJ")

        project_roles_mixin.jira.get.assert_called_once_with(
            "rest/api/2/project/PROJ/role"
        )
        assert result == {
            "Administrators": "https://jira/rest/api/2/project/10000/role/10002",
            "Developers": "https://jira/rest/api/2/project/10000/role/10001",
        }

    def test_returns_empty_dict_on_unexpected_shape(self, project_roles_mixin):
        project_roles_mixin.jira.get.return_value = ["not", "a", "dict"]

        result = project_roles_mixin.list_project_roles("PROJ")

        assert result == {}

    def test_returns_empty_dict_on_exception(self, project_roles_mixin):
        project_roles_mixin.jira.get.side_effect = RuntimeError("boom")

        result = project_roles_mixin.list_project_roles("PROJ")

        assert result == {}


class TestProjectRolesMixinGetActors:
    """Unit tests for ``ProjectRolesMixin.get_project_role_actors``."""

    def test_returns_role_payload(self, project_roles_mixin):
        payload = {
            "self": "https://jira/rest/api/2/project/10000/role/10002",
            "id": 10002,
            "name": "Administrators",
            "description": "Project admins",
            "actors": [
                {"id": 1, "displayName": "Alice", "type": "atlassian-user-role-actor"},
                {"id": 2, "displayName": "jira-devs", "type": "atlassian-group-role-actor"},
            ],
        }
        project_roles_mixin.jira.get.return_value = payload

        result = project_roles_mixin.get_project_role_actors("PROJ", "10002")

        project_roles_mixin.jira.get.assert_called_once_with(
            "rest/api/2/project/PROJ/role/10002"
        )
        assert result == payload

    def test_returns_empty_dict_on_unexpected_shape(self, project_roles_mixin):
        project_roles_mixin.jira.get.return_value = []

        result = project_roles_mixin.get_project_role_actors("PROJ", "10002")

        assert result == {}

    def test_returns_empty_dict_on_exception(self, project_roles_mixin):
        project_roles_mixin.jira.get.side_effect = RuntimeError("boom")

        result = project_roles_mixin.get_project_role_actors("PROJ", "10002")

        assert result == {}


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
    """Fetcher stub exposing ``config.projects_filter`` and role methods."""
    fetcher = MagicMock()
    fetcher.config = SimpleNamespace(projects_filter=None)
    fetcher.list_project_roles.return_value = {
        "Administrators": "https://jira/rest/api/2/project/10000/role/10002",
    }
    fetcher.get_project_role_actors.return_value = {
        "id": 10002,
        "name": "Administrators",
        "actors": [],
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


@pytest.mark.anyio
async def test_jira_list_project_roles_returns_payload(fake_ctx, patch_get_fetcher):
    from mcp_atlassian.servers.jira import jira_list_project_roles

    patch_get_fetcher.list_project_roles.return_value = {
        "Administrators": "https://jira/rest/api/2/project/10000/role/10002",
        "Developers": "https://jira/rest/api/2/project/10000/role/10001",
    }

    result_json = await jira_list_project_roles.fn(fake_ctx, project_key="PROJ")
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["project_key"] == "PROJ"
    assert payload["count"] == 2
    assert payload["roles"]["Administrators"].endswith("/role/10002")
    patch_get_fetcher.list_project_roles.assert_called_once_with("PROJ")


@pytest.mark.anyio
async def test_jira_list_project_roles_blocked_by_project_filter(
    fake_ctx, patch_get_fetcher
):
    from mcp_atlassian.servers.jira import jira_list_project_roles

    patch_get_fetcher.config.projects_filter = "ALLOWED"

    result_json = await jira_list_project_roles.fn(fake_ctx, project_key="PROJ")
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "filtered_out"
    patch_get_fetcher.list_project_roles.assert_not_called()


@pytest.mark.anyio
async def test_jira_list_project_roles_surfaces_exception(
    fake_ctx, patch_get_fetcher
):
    from mcp_atlassian.servers.jira import jira_list_project_roles

    patch_get_fetcher.list_project_roles.side_effect = RuntimeError("upstream 500")

    result_json = await jira_list_project_roles.fn(fake_ctx, project_key="PROJ")
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert "upstream 500" in payload["error"]


@pytest.mark.anyio
async def test_jira_get_project_role_actors_returns_payload(
    fake_ctx, patch_get_fetcher
):
    from mcp_atlassian.servers.jira import jira_get_project_role_actors

    patch_get_fetcher.get_project_role_actors.return_value = {
        "id": 10002,
        "name": "Administrators",
        "actors": [
            {"id": 1, "displayName": "Alice"},
        ],
    }

    result_json = await jira_get_project_role_actors.fn(
        fake_ctx, project_key="PROJ", role_id="10002"
    )
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["project_key"] == "PROJ"
    assert payload["role_id"] == "10002"
    assert payload["role"]["name"] == "Administrators"
    assert payload["role"]["actors"][0]["displayName"] == "Alice"
    patch_get_fetcher.get_project_role_actors.assert_called_once_with(
        "PROJ", "10002"
    )


@pytest.mark.anyio
async def test_jira_get_project_role_actors_blocked_by_project_filter(
    fake_ctx, patch_get_fetcher
):
    from mcp_atlassian.servers.jira import jira_get_project_role_actors

    patch_get_fetcher.config.projects_filter = "OTHER"

    result_json = await jira_get_project_role_actors.fn(
        fake_ctx, project_key="PROJ", role_id="10002"
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "filtered_out"
    patch_get_fetcher.get_project_role_actors.assert_not_called()


@pytest.mark.anyio
async def test_jira_get_project_role_actors_surfaces_exception(
    fake_ctx, patch_get_fetcher
):
    from mcp_atlassian.servers.jira import jira_get_project_role_actors

    patch_get_fetcher.get_project_role_actors.side_effect = RuntimeError("boom")

    result_json = await jira_get_project_role_actors.fn(
        fake_ctx, project_key="PROJ", role_id="10002"
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert "boom" in payload["error"]


@pytest.mark.anyio
async def test_project_roles_tools_have_expected_tags():
    """Ensure tool tags match Requirement 24.1 and no write tool is registered.

    Validates: Requirement 24.1 (read-tool tagging) and Requirement 24.2
    (no write tool in ``toolset:jira_project_roles``).
    """
    from mcp_atlassian.servers import jira as jira_server
    from mcp_atlassian.servers.jira import (
        jira_get_project_role_actors,
        jira_list_project_roles,
    )

    read_tags = {"jira", "read", "toolset:jira_project_roles"}

    assert set(jira_list_project_roles.tags) == read_tags
    assert set(jira_get_project_role_actors.tags) == read_tags

    # Req 24.2: no write tool may carry the jira_project_roles toolset tag.
    # Walk every registered tool and assert none are both tagged with this
    # toolset and the "write" sentinel.
    for attr in dir(jira_server):
        obj = getattr(jira_server, attr)
        tags = getattr(obj, "tags", None)
        if not isinstance(tags, (set, frozenset, list, tuple)):
            continue
        tagset = set(tags)
        if "toolset:jira_project_roles" in tagset:
            assert "write" not in tagset, (
                f"{attr} must not carry 'write' tag in toolset:jira_project_roles "
                "per Requirement 24.2"
            )
