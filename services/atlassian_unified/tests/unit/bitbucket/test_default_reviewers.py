"""Tests for DefaultReviewersMixin and the Bitbucket default-reviewer server tools.

These tests cover Requirement 1 (1.1-1.6) from the ``atlassian-dc-tool-parity``
spec:

* ``DefaultReviewersMixin`` methods wrap the Default Reviewers plugin
  endpoints under ``/rest/default-reviewers/1.0/projects/{k}/repos/{r}/
  conditions`` (Req 1.1-1.5).
* Server tools apply the ``check_read_only → check_project_filter``
  prelude with zero HTTP on reject; when the project key falls outside
  ``BITBUCKET_PROJECTS_FILTER`` a structured ``filtered_out`` error is
  surfaced (Req 1.6).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mcp_atlassian.bitbucket.config import BitbucketConfig
from mcp_atlassian.bitbucket.default_reviewers import DefaultReviewersMixin


# ---------------------------------------------------------------------------
# Mixin fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bitbucket_config() -> BitbucketConfig:
    """Minimal ``BitbucketConfig`` suitable for instantiating the mixin."""
    return BitbucketConfig(
        url="https://bb.example.com",
        auth_type="pat",
        personal_token="test-pat",
    )


@pytest.fixture
def default_reviewers_mixin(bitbucket_config):
    """Create a ``DefaultReviewersMixin`` with mocked Bitbucket transport.

    The mixin inherits from :class:`BitbucketClient`, whose constructor
    builds an ``atlassian.Bitbucket`` session. We patch that constructor
    so no real HTTP session is created, then replace
    ``mixin.bitbucket`` with a fresh ``MagicMock`` the tests drive —
    mirroring the pattern in ``test_cherry_pick.py``.
    """
    with patch("mcp_atlassian.bitbucket.client.Bitbucket") as mock_bitbucket_class:
        mock_bitbucket_class.return_value = MagicMock()
        mixin = DefaultReviewersMixin(config=bitbucket_config)
        mixin.bitbucket = MagicMock()
        return mixin


# ---------------------------------------------------------------------------
# Mixin-level tests — Requirement 1.1-1.5
# ---------------------------------------------------------------------------


class TestListDefaultReviewers:
    """Unit tests for ``DefaultReviewersMixin.list_default_reviewers``."""

    def test_returns_flat_list_from_api(self, default_reviewers_mixin):
        # The conditions endpoint returns a bare JSON array rather than
        # the paged envelope used elsewhere in Bitbucket DC, so the
        # mixin must pass it through unchanged.
        rules = [
            {
                "id": 1,
                "sourceRefMatcher": {"id": "refs/heads/feature/*"},
                "targetRefMatcher": {"id": "refs/heads/main"},
                "reviewers": [{"id": 42, "name": "jdoe"}],
                "requiredApprovals": 1,
            },
            {
                "id": 2,
                "sourceRefMatcher": {"id": "ANY_REF_MATCHER_ID"},
                "targetRefMatcher": {"id": "refs/heads/release/*"},
                "reviewers": [{"id": 7, "name": "asmith"}],
                "requiredApprovals": 2,
            },
        ]
        default_reviewers_mixin.bitbucket.get.return_value = rules

        result = default_reviewers_mixin.list_default_reviewers("PROJ", "repo")

        default_reviewers_mixin.bitbucket.get.assert_called_once_with(
            "/rest/default-reviewers/1.0/projects/PROJ/repos/repo/conditions"
        )
        assert result == rules

    def test_rejects_non_list_response(self, default_reviewers_mixin):
        # Anything other than a JSON array is a transport-layer anomaly
        # the mixin surfaces explicitly so callers never have to guard
        # against dict/None responses.
        default_reviewers_mixin.bitbucket.get.return_value = {"unexpected": "shape"}

        with pytest.raises(ValueError, match="Unexpected response"):
            default_reviewers_mixin.list_default_reviewers("PROJ", "repo")


class TestGetDefaultReviewerRule:
    """Unit tests for ``DefaultReviewersMixin.get_default_reviewer_rule``."""

    def test_returns_condition_dict(self, default_reviewers_mixin):
        rule = {
            "id": 42,
            "sourceRefMatcher": {"id": "refs/heads/feature/*"},
            "targetRefMatcher": {"id": "refs/heads/main"},
            "reviewers": [{"id": 1, "name": "jdoe"}],
            "requiredApprovals": 1,
        }
        default_reviewers_mixin.bitbucket.get.return_value = rule

        result = default_reviewers_mixin.get_default_reviewer_rule(
            "PROJ", "repo", 42
        )

        default_reviewers_mixin.bitbucket.get.assert_called_once_with(
            "/rest/default-reviewers/1.0/projects/PROJ/repos/repo/conditions/42"
        )
        assert result == rule

    def test_rejects_non_dict_response(self, default_reviewers_mixin):
        default_reviewers_mixin.bitbucket.get.return_value = ["not-a-dict"]

        with pytest.raises(ValueError, match="Unexpected response"):
            default_reviewers_mixin.get_default_reviewer_rule("PROJ", "repo", 42)


class TestCreateDefaultReviewerRule:
    """Unit tests for ``DefaultReviewersMixin.create_default_reviewer_rule``."""

    def test_posts_expected_body(self, default_reviewers_mixin):
        source_matcher = {
            "id": "refs/heads/feature/*",
            "type": {"id": "PATTERN"},
        }
        target_matcher = {
            "id": "refs/heads/main",
            "type": {"id": "BRANCH"},
        }
        reviewers = [{"name": "jdoe"}, {"id": 42}]
        created = {
            "id": 101,
            "sourceRefMatcher": source_matcher,
            "targetRefMatcher": target_matcher,
            "reviewers": reviewers,
            "requiredApprovals": 2,
        }
        default_reviewers_mixin.bitbucket.post.return_value = created

        result = default_reviewers_mixin.create_default_reviewer_rule(
            "PROJ",
            "repo",
            source_matcher=source_matcher,
            target_matcher=target_matcher,
            reviewers=reviewers,
            required_approvals=2,
        )

        default_reviewers_mixin.bitbucket.post.assert_called_once()
        (called_url,), called_kwargs = default_reviewers_mixin.bitbucket.post.call_args
        assert called_url == (
            "/rest/default-reviewers/1.0/projects/PROJ/repos/repo/conditions"
        )
        body = called_kwargs["data"]
        # The mixin translates the Pythonic kwargs to the DC-documented
        # camelCase body keys.
        assert body == {
            "sourceMatcher": source_matcher,
            "targetMatcher": target_matcher,
            "reviewers": reviewers,
            "requiredApprovals": 2,
        }
        assert result == created

    def test_rejects_non_dict_response(self, default_reviewers_mixin):
        default_reviewers_mixin.bitbucket.post.return_value = "not-a-dict"

        with pytest.raises(ValueError, match="Unexpected response"):
            default_reviewers_mixin.create_default_reviewer_rule(
                "PROJ",
                "repo",
                source_matcher={"id": "x"},
                target_matcher={"id": "y"},
                reviewers=[{"name": "jdoe"}],
                required_approvals=1,
            )


class TestUpdateDefaultReviewerRule:
    """Unit tests for ``DefaultReviewersMixin.update_default_reviewer_rule``."""

    def test_puts_supplied_fields(self, default_reviewers_mixin):
        updated = {
            "id": 42,
            "sourceRefMatcher": {"id": "refs/heads/feature/*"},
            "targetRefMatcher": {"id": "refs/heads/main"},
            "reviewers": [{"name": "jdoe"}],
            "requiredApprovals": 2,
        }
        default_reviewers_mixin.bitbucket.put.return_value = updated

        result = default_reviewers_mixin.update_default_reviewer_rule(
            "PROJ",
            "repo",
            42,
            reviewers=[{"name": "jdoe"}],
            requiredApprovals=2,
        )

        default_reviewers_mixin.bitbucket.put.assert_called_once()
        (called_url,), called_kwargs = default_reviewers_mixin.bitbucket.put.call_args
        assert called_url == (
            "/rest/default-reviewers/1.0/projects/PROJ/repos/repo/conditions/42"
        )
        # Only the supplied fields are forwarded; the caller owns the
        # camelCase spelling of condition attributes per the DC schema.
        assert called_kwargs["data"] == {
            "reviewers": [{"name": "jdoe"}],
            "requiredApprovals": 2,
        }
        assert result == updated

    def test_rejects_non_dict_response(self, default_reviewers_mixin):
        default_reviewers_mixin.bitbucket.put.return_value = None

        with pytest.raises(ValueError, match="Unexpected response"):
            default_reviewers_mixin.update_default_reviewer_rule(
                "PROJ", "repo", 42, requiredApprovals=1
            )


class TestDeleteDefaultReviewerRule:
    """Unit tests for ``DefaultReviewersMixin.delete_default_reviewer_rule``."""

    def test_calls_delete_endpoint(self, default_reviewers_mixin):
        default_reviewers_mixin.bitbucket.delete.return_value = None

        result = default_reviewers_mixin.delete_default_reviewer_rule(
            "PROJ", "repo", 42
        )

        default_reviewers_mixin.bitbucket.delete.assert_called_once_with(
            "/rest/default-reviewers/1.0/projects/PROJ/repos/repo/conditions/42"
        )
        # ``delete_default_reviewer_rule`` is declared to return None —
        # 204 responses from DC surface as ``None`` from the atlassian
        # client and the mixin must propagate that unchanged.
        assert result is None


# ---------------------------------------------------------------------------
# Server-tool tests — Requirement 1.6
# ---------------------------------------------------------------------------


class _FakeContext:
    """Minimal stand-in for :class:`fastmcp.Context` used by tool funcs."""


@pytest.fixture
def fake_ctx() -> _FakeContext:
    return _FakeContext()


@pytest.fixture
def fake_fetcher():
    """Fetcher stub exposing ``config.projects_filter`` and default-reviewer methods."""
    fetcher = MagicMock()
    fetcher.is_cloud = False
    fetcher.config = SimpleNamespace(is_cloud=False, projects_filter=None)
    fetcher.list_default_reviewers.return_value = []
    fetcher.get_default_reviewer_rule.return_value = {}
    fetcher.create_default_reviewer_rule.return_value = {"id": 1}
    fetcher.update_default_reviewer_rule.return_value = {"id": 1}
    fetcher.delete_default_reviewer_rule.return_value = None
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


# ---- Happy-path server-tool sanity check ------------------------------------


@pytest.mark.anyio
async def test_list_default_reviewers_happy_path(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """List tool returns ``{"success": True, "count": n, "rules": [...]}``."""
    from mcp_atlassian.servers.bitbucket import list_default_reviewers

    patch_get_fetcher.list_default_reviewers.return_value = [
        {"id": 1, "reviewers": [], "requiredApprovals": 1},
        {"id": 2, "reviewers": [], "requiredApprovals": 2},
    ]

    result_json = await list_default_reviewers.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo"
    )
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["count"] == 2
    assert len(payload["rules"]) == 2
    # Happy path must NOT emit the structured error envelope.
    assert "error_code" not in payload


# ---- Req 1.6: ``filtered_out`` on project outside ``BITBUCKET_PROJECTS_FILTER`` ----


@pytest.mark.anyio
async def test_create_default_reviewer_rule_filtered_out(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Req 1.6: ``bitbucket_create_default_reviewer_rule`` returns
    ``filtered_out`` when the project key is not in
    ``BITBUCKET_PROJECTS_FILTER``.

    The guard runs before the outbound HTTP request, so the fetcher's
    ``create_default_reviewer_rule`` must not be called. This pins the
    observable contract for the entire default-reviewer toolset: the
    server layer rejects out-of-scope keys before any Bitbucket write
    can occur.
    """
    from mcp_atlassian.servers.bitbucket import create_default_reviewer_rule

    # Simulate ``BITBUCKET_PROJECTS_FILTER=ALLOWED`` — the allow-list
    # does not include "PROJ" so the call must short-circuit with a
    # structured ``filtered_out`` error.
    patch_get_fetcher.config.projects_filter = "ALLOWED"

    result_json = await create_default_reviewer_rule.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        source_matcher=json.dumps(
            {"id": "refs/heads/feature/*", "type": {"id": "PATTERN"}}
        ),
        target_matcher=json.dumps(
            {"id": "refs/heads/main", "type": {"id": "BRANCH"}}
        ),
        reviewers=json.dumps([{"name": "jdoe"}]),
        required_approvals=1,
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "filtered_out"
    # Guard must fire before any HTTP request — the mixin method is
    # never invoked.
    patch_get_fetcher.create_default_reviewer_rule.assert_not_called()


# ---- Registration / tagging parity ------------------------------------------


@pytest.mark.anyio
async def test_default_reviewer_tools_have_expected_tags():
    """Ensure tool tags match Requirement 1.1-1.5."""
    from mcp_atlassian.servers.bitbucket import (
        create_default_reviewer_rule,
        delete_default_reviewer_rule,
        get_default_reviewer_rule,
        list_default_reviewers,
        update_default_reviewer_rule,
    )

    read_tags = {"bitbucket", "read", "toolset:bitbucket_default_reviewers"}
    write_tags = {"bitbucket", "write", "toolset:bitbucket_default_reviewers"}

    assert set(list_default_reviewers.tags) == read_tags
    assert set(get_default_reviewer_rule.tags) == read_tags
    assert set(create_default_reviewer_rule.tags) == write_tags
    assert set(update_default_reviewer_rule.tags) == write_tags
    assert set(delete_default_reviewer_rule.tags) == write_tags
