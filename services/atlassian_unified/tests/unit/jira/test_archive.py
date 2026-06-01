"""Tests for ArchiveMixin and the Jira archive/restore server tools.

These tests cover Requirement 26 from the ``atlassian-dc-tool-parity``
spec:

* ``ArchiveMixin`` methods (``archive_issue``, ``restore_issue``) wrap
  the DC 9.4+ endpoints under ``/rest/api/2/issue/{key}/archive`` and
  ``/rest/api/2/issue/{key}/restore`` and return deterministic
  confirmation payloads (Req 26.1, 26.2).
* Server tools (``jira_archive_issue``, ``jira_restore_issue``) apply
  the ``check_read_only`` → ``check_project_filter`` →
  ``check_dc_version(required="9.4")`` prelude with zero HTTP on
  reject, matching the ``jira_notify_issue`` pattern from task 24.2.
* ``jira_archive_issue`` returns a reversible receipt that references
  ``jira_restore_issue`` with the archived issue key (Req 26.4).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcp_atlassian.jira.archive import ArchiveMixin


# ---------------------------------------------------------------------------
# Mixin-level tests
# ---------------------------------------------------------------------------


@pytest.fixture
def archive_mixin(jira_client):
    """Create an ``ArchiveMixin`` instance with mocked Jira transport."""
    mixin = ArchiveMixin(config=jira_client.config)
    mixin.jira = MagicMock()
    return mixin


class TestArchiveMixinArchive:
    """Unit tests for ``ArchiveMixin.archive_issue``."""

    def test_calls_archive_endpoint(self, archive_mixin):
        archive_mixin.jira.put.return_value = None

        result = archive_mixin.archive_issue("PROJ-1")

        archive_mixin.jira.put.assert_called_once_with(
            "rest/api/2/issue/PROJ-1/archive"
        )
        assert result == {"archived": True, "issue_key": "PROJ-1"}

    def test_confirmation_shape_is_deterministic(self, archive_mixin):
        archive_mixin.jira.put.return_value = None

        for key in ("ACV2-642", "TEST-1"):
            result = archive_mixin.archive_issue(key)
            assert result == {"archived": True, "issue_key": key}


class TestArchiveMixinRestore:
    """Unit tests for ``ArchiveMixin.restore_issue``."""

    def test_calls_restore_endpoint(self, archive_mixin):
        archive_mixin.jira.put.return_value = None

        result = archive_mixin.restore_issue("PROJ-1")

        archive_mixin.jira.put.assert_called_once_with(
            "rest/api/2/issue/PROJ-1/restore"
        )
        assert result == {"restored": True, "issue_key": "PROJ-1"}

    def test_confirmation_shape_is_deterministic(self, archive_mixin):
        archive_mixin.jira.put.return_value = None

        for key in ("ACV2-642", "TEST-1"):
            result = archive_mixin.restore_issue(key)
            assert result == {"restored": True, "issue_key": key}


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
    """Fetcher stub exposing ``config.projects_filter`` and archive methods."""
    # ``check_dc_version`` prefers a callable ``get_dc_version`` over the
    # ``_dc_version`` attribute when present, so we explicitly spec the
    # mock with the method we want tests to drive. This keeps
    # ``get_dc_version`` as a controllable ``MagicMock`` while letting
    # archive_issue / restore_issue / config stay auto-mocked.
    fetcher = MagicMock()
    fetcher.config = SimpleNamespace(projects_filter=None)
    # DC version probe: defaults to a 9.4+ version so the gate lets
    # happy-path calls through. Individual tests override to simulate
    # older instances.
    fetcher.get_dc_version.return_value = "9.4.0"
    fetcher._dc_version = "9.4.0"
    fetcher.archive_issue.return_value = {
        "archived": True,
        "issue_key": "PROJ-1",
    }
    fetcher.restore_issue.return_value = {
        "restored": True,
        "issue_key": "PROJ-1",
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


# ---- jira_archive_issue ----------------------------------------------------


@pytest.mark.anyio
async def test_jira_archive_issue_happy_path(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.jira import jira_archive_issue

    result_json = await jira_archive_issue.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["issue_key"] == "PROJ-1"
    assert payload["archived"] is True
    # Reversible receipt (Req 26.4).
    receipt = payload["receipt"]
    assert receipt["object_id"] == "PROJ-1"
    assert receipt["inverse_tool"] == "jira_restore_issue"
    assert receipt["inverse_args"] == {"issue_key": "PROJ-1"}
    patch_get_fetcher.archive_issue.assert_called_once_with("PROJ-1")


@pytest.mark.anyio
async def test_jira_archive_issue_blocked_in_read_only(
    fake_ctx, patch_get_fetcher, monkeypatch
):
    from mcp_atlassian.servers.jira import jira_archive_issue

    monkeypatch.setenv("READ_ONLY_MODE", "true")

    result_json = await jira_archive_issue.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "read_only_mode"
    patch_get_fetcher.archive_issue.assert_not_called()


@pytest.mark.anyio
async def test_jira_archive_issue_blocked_by_project_filter(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.jira import jira_archive_issue

    patch_get_fetcher.config.projects_filter = "ALLOWED"

    result_json = await jira_archive_issue.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "filtered_out"
    patch_get_fetcher.archive_issue.assert_not_called()


@pytest.mark.anyio
async def test_jira_archive_issue_blocked_by_dc_version(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.jira import jira_archive_issue

    # Simulate a pre-9.4 DC instance (Req 26.3).
    patch_get_fetcher.get_dc_version.return_value = "9.2.1"
    patch_get_fetcher._dc_version = "9.2.1"

    result_json = await jira_archive_issue.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "dc_version_too_old"
    assert payload["details"]["required_version"] == "9.4"
    patch_get_fetcher.archive_issue.assert_not_called()


# ---- jira_restore_issue ----------------------------------------------------


@pytest.mark.anyio
async def test_jira_restore_issue_happy_path(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.jira import jira_restore_issue

    result_json = await jira_restore_issue.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["issue_key"] == "PROJ-1"
    assert payload["restored"] is True
    # Restore does not carry a receipt of its own; it is itself the
    # inverse of archive.
    assert "receipt" not in payload
    patch_get_fetcher.restore_issue.assert_called_once_with("PROJ-1")


@pytest.mark.anyio
async def test_jira_restore_issue_blocked_in_read_only(
    fake_ctx, patch_get_fetcher, monkeypatch
):
    from mcp_atlassian.servers.jira import jira_restore_issue

    monkeypatch.setenv("READ_ONLY_MODE", "true")

    result_json = await jira_restore_issue.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "read_only_mode"
    patch_get_fetcher.restore_issue.assert_not_called()


@pytest.mark.anyio
async def test_jira_restore_issue_blocked_by_project_filter(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.jira import jira_restore_issue

    patch_get_fetcher.config.projects_filter = "OTHER"

    result_json = await jira_restore_issue.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "filtered_out"
    patch_get_fetcher.restore_issue.assert_not_called()


@pytest.mark.anyio
async def test_jira_restore_issue_blocked_by_dc_version(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.jira import jira_restore_issue

    # Simulate a pre-9.4 DC instance (Req 26.3).
    patch_get_fetcher.get_dc_version.return_value = "9.3.9"
    patch_get_fetcher._dc_version = "9.3.9"

    result_json = await jira_restore_issue.fn(fake_ctx, issue_key="PROJ-1")
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "dc_version_too_old"
    assert payload["details"]["required_version"] == "9.4"
    patch_get_fetcher.restore_issue.assert_not_called()


# ---- Registration / tagging parity -----------------------------------------


@pytest.mark.anyio
async def test_archive_tools_have_expected_tags():
    """Ensure tool tags match Requirement 26.1 / 26.2."""
    from mcp_atlassian.servers.jira import (
        jira_archive_issue,
        jira_restore_issue,
    )

    write_tags = {"jira", "write", "toolset:jira_archive"}

    assert set(jira_archive_issue.tags) == write_tags
    assert set(jira_restore_issue.tags) == write_tags
