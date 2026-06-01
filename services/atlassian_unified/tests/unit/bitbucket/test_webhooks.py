"""Tests for WebhooksMixin and the Bitbucket webhook server tools.

These tests cover Requirement 2 from the ``atlassian-dc-tool-parity``
spec:

* ``WebhooksMixin`` methods wrap the DC 5.4+ endpoints under
  ``/rest/api/latest/projects/{k}/repos/{r}/webhooks`` (Req 2.1).
* Server tools apply the ``check_read_only`` → ``check_project_filter``
  → ``check_dc_version(required="5.4")`` prelude with zero HTTP on
  reject, matching the design document's Bitbucket guard pattern.
* ``secret`` is forwarded to Bitbucket in the request body but never
  echoed in the response of ``bitbucket_create_webhook`` (Req 2.3).
* ``configuration.secret`` is replaced by the literal string
  ``"[REDACTED]"`` in the payloads returned by ``bitbucket_list_webhooks``
  and ``bitbucket_get_webhook`` (Req 2.4).
* ``bitbucket_create_webhook`` returns a reversible receipt referencing
  ``bitbucket_delete_webhook`` (Req 2.5).
* ``dc_version_too_old`` is surfaced when the instance is pre-5.4 (Req
  2.6).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcp_atlassian.bitbucket.webhooks import WebhooksMixin


# ---------------------------------------------------------------------------
# Mixin-level tests
# ---------------------------------------------------------------------------


@pytest.fixture
def webhooks_mixin():
    """Create a ``WebhooksMixin`` instance with mocked Bitbucket transport.

    The mixin normally inherits from :class:`BitbucketClient`, whose
    constructor requires a live config and auth. For unit tests we bypass
    the constructor entirely and stamp a bare ``bitbucket`` attribute (the
    underlying ``atlassian.Bitbucket`` client) onto the instance so the
    HTTP primitives ``get``/``post``/``put``/``delete`` can be driven by
    ``MagicMock``.

    A minimal ``config`` namespace with ``is_cloud=False`` is also stamped
    on so that the dual-mode pagination dispatcher in
    :meth:`BitbucketClient._get_paged_results` can evaluate
    ``self.is_cloud`` without raising ``AttributeError``. The DC shape is
    what these tests exercise.
    """
    mixin = WebhooksMixin.__new__(WebhooksMixin)
    mixin.bitbucket = MagicMock()
    mixin.config = SimpleNamespace(is_cloud=False)
    return mixin


class TestWebhooksMixinList:
    """Unit tests for ``WebhooksMixin.list_webhooks``."""

    def test_calls_paginated_endpoint(self, webhooks_mixin):
        # ``_get_paged_results`` drives ``bitbucket.get`` in a loop; one
        # page with ``isLastPage=True`` mirrors how DC returns small
        # result sets.
        webhooks_mixin.bitbucket.get.return_value = {
            "values": [
                {
                    "id": 7,
                    "name": "hook-a",
                    "configuration": {"secret": "top-secret"},
                },
            ],
            "isLastPage": True,
        }

        result = webhooks_mixin.list_webhooks("PROJ", "repo", limit=25)

        assert result == [
            {
                "id": 7,
                "name": "hook-a",
                "configuration": {"secret": "top-secret"},
            }
        ]
        webhooks_mixin.bitbucket.get.assert_called_once()
        (called_url,), called_kwargs = webhooks_mixin.bitbucket.get.call_args
        assert called_url == "/rest/api/latest/projects/PROJ/repos/repo/webhooks"
        assert called_kwargs["params"]["start"] == 0
        assert called_kwargs["params"]["limit"] == 25


class TestWebhooksMixinGet:
    """Unit tests for ``WebhooksMixin.get_webhook``."""

    def test_returns_dict(self, webhooks_mixin):
        webhooks_mixin.bitbucket.get.return_value = {
            "id": 42,
            "name": "hook-b",
            "configuration": {"secret": "hmac-value"},
        }

        result = webhooks_mixin.get_webhook("PROJ", "repo", 42)

        webhooks_mixin.bitbucket.get.assert_called_once_with(
            "/rest/api/latest/projects/PROJ/repos/repo/webhooks/42"
        )
        assert result["id"] == 42
        assert result["configuration"]["secret"] == "hmac-value"

    def test_rejects_non_dict_response(self, webhooks_mixin):
        webhooks_mixin.bitbucket.get.return_value = ["unexpected"]

        with pytest.raises(ValueError, match="Unexpected response"):
            webhooks_mixin.get_webhook("PROJ", "repo", 1)


class TestWebhooksMixinCreate:
    """Unit tests for ``WebhooksMixin.create_webhook``."""

    def test_forwards_secret_in_request_body(self, webhooks_mixin):
        # The mixin's job is purely to forward the caller-supplied secret
        # to Bitbucket verbatim. Redaction happens in the server layer.
        webhooks_mixin.bitbucket.post.return_value = {
            "id": 99,
            "name": "hook-c",
            "configuration": {"secret": "top-secret"},
        }

        result = webhooks_mixin.create_webhook(
            "PROJ",
            "repo",
            name="hook-c",
            url="https://ci.example.com/hook",
            events=["repo:refs_changed"],
            secret="top-secret",
            active=True,
        )

        webhooks_mixin.bitbucket.post.assert_called_once()
        (called_endpoint,), called_kwargs = webhooks_mixin.bitbucket.post.call_args
        assert called_endpoint == "/rest/api/latest/projects/PROJ/repos/repo/webhooks"
        body = called_kwargs["data"]
        # Secret is present in the outbound body — this is the
        # "forward to Bitbucket" half of Req 2.3.
        assert body["configuration"] == {"secret": "top-secret"}
        assert body["events"] == ["repo:refs_changed"]
        assert body["active"] is True
        assert result["id"] == 99

    def test_omits_secret_when_not_provided(self, webhooks_mixin):
        webhooks_mixin.bitbucket.post.return_value = {
            "id": 100,
            "name": "hook-d",
            "configuration": {},
        }

        webhooks_mixin.create_webhook(
            "PROJ",
            "repo",
            name="hook-d",
            url="https://ci.example.com/hook",
            events=["pr:opened"],
        )

        body = webhooks_mixin.bitbucket.post.call_args.kwargs["data"]
        # No ``secret`` key should leak into the outbound body when the
        # caller did not supply one.
        assert body["configuration"] == {}


class TestWebhooksMixinUpdate:
    """Unit tests for ``WebhooksMixin.update_webhook``."""

    def test_puts_supplied_fields(self, webhooks_mixin):
        webhooks_mixin.bitbucket.put.return_value = {
            "id": 42,
            "name": "new-name",
            "configuration": {"secret": "rotated"},
        }

        result = webhooks_mixin.update_webhook(
            "PROJ",
            "repo",
            42,
            name="new-name",
            configuration={"secret": "rotated"},
        )

        webhooks_mixin.bitbucket.put.assert_called_once()
        (called_endpoint,), called_kwargs = webhooks_mixin.bitbucket.put.call_args
        assert called_endpoint == "/rest/api/latest/projects/PROJ/repos/repo/webhooks/42"
        assert called_kwargs["data"] == {
            "name": "new-name",
            "configuration": {"secret": "rotated"},
        }
        assert result["id"] == 42


class TestWebhooksMixinDelete:
    """Unit tests for ``WebhooksMixin.delete_webhook``."""

    def test_calls_delete_endpoint(self, webhooks_mixin):
        webhooks_mixin.bitbucket.delete.return_value = None

        webhooks_mixin.delete_webhook("PROJ", "repo", 42)

        webhooks_mixin.bitbucket.delete.assert_called_once_with(
            "/rest/api/latest/projects/PROJ/repos/repo/webhooks/42"
        )


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
    """Fetcher stub exposing ``config.projects_filter`` and webhook methods.

    ``check_dc_version`` prefers a callable ``get_dc_version`` over the
    ``_dc_version`` attribute when present, so we explicitly set both so
    tests can drive either path. Defaults are tuned to a modern 5.4+
    instance so happy-path calls pass the gate; individual tests
    override to simulate pre-5.4 instances.
    """
    fetcher = MagicMock()
    fetcher.config = SimpleNamespace(projects_filter=None)
    fetcher.get_dc_version.return_value = "5.4.0"
    fetcher._dc_version = "5.4.0"
    fetcher.list_webhooks.return_value = []
    fetcher.get_webhook.return_value = {}
    fetcher.create_webhook.return_value = {"id": 99}
    fetcher.update_webhook.return_value = {"id": 99}
    fetcher.delete_webhook.return_value = None
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


# ---- bitbucket_list_webhooks ------------------------------------------------


@pytest.mark.anyio
async def test_list_webhooks_redacts_secret(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Req 2.4: ``configuration.secret`` must be ``"[REDACTED]"`` in list."""
    from mcp_atlassian.servers.bitbucket import list_webhooks

    patch_get_fetcher.list_webhooks.return_value = [
        {"id": 1, "name": "hook-1", "configuration": {"secret": "top-secret"}},
        {"id": 2, "name": "hook-2", "configuration": {"secret": "also-secret"}},
    ]

    result_json = await list_webhooks.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo"
    )
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["count"] == 2
    # Both secrets must be replaced by the literal redaction placeholder.
    for hook in payload["webhooks"]:
        assert hook["configuration"]["secret"] == "[REDACTED]"
    # The plaintext secrets must not appear anywhere in the serialized
    # response — this is the hard negative assertion for Req 2.4.
    assert "top-secret" not in result_json
    assert "also-secret" not in result_json


@pytest.mark.anyio
async def test_list_webhooks_blocked_by_dc_version(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Req 2.6: pre-5.4 instances return ``dc_version_too_old``."""
    from mcp_atlassian.servers.bitbucket import list_webhooks

    patch_get_fetcher.get_dc_version.return_value = "5.3.9"
    patch_get_fetcher._dc_version = "5.3.9"

    result_json = await list_webhooks.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo"
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "dc_version_too_old"
    assert payload["details"]["required_version"] == "5.4"
    patch_get_fetcher.list_webhooks.assert_not_called()


@pytest.mark.anyio
async def test_list_webhooks_blocked_by_project_filter(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.bitbucket import list_webhooks

    patch_get_fetcher.config.projects_filter = "ALLOWED"

    result_json = await list_webhooks.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo"
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "filtered_out"
    patch_get_fetcher.list_webhooks.assert_not_called()


# ---- bitbucket_get_webhook --------------------------------------------------


@pytest.mark.anyio
async def test_get_webhook_redacts_secret(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Req 2.4: ``configuration.secret`` must be ``"[REDACTED]"`` in get."""
    from mcp_atlassian.servers.bitbucket import get_webhook

    patch_get_fetcher.get_webhook.return_value = {
        "id": 42,
        "name": "hook-b",
        "configuration": {"secret": "hmac-value"},
    }

    result_json = await get_webhook.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo", webhook_id=42
    )
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["webhook"]["id"] == 42
    assert payload["webhook"]["configuration"]["secret"] == "[REDACTED]"
    # The plaintext secret must not appear anywhere in the serialized
    # response.
    assert "hmac-value" not in result_json


@pytest.mark.anyio
async def test_get_webhook_blocked_by_dc_version(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.bitbucket import get_webhook

    patch_get_fetcher.get_dc_version.return_value = "5.0.0"
    patch_get_fetcher._dc_version = "5.0.0"

    result_json = await get_webhook.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo", webhook_id=1
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "dc_version_too_old"
    assert payload["details"]["required_version"] == "5.4"
    patch_get_fetcher.get_webhook.assert_not_called()


# ---- bitbucket_create_webhook ----------------------------------------------


@pytest.mark.anyio
async def test_create_webhook_happy_path(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Req 2.3, 2.5: secret must not appear in response, and the response
    must include a receipt pointing at ``bitbucket_delete_webhook``."""
    from mcp_atlassian.servers.bitbucket import create_webhook

    # Simulate Bitbucket echoing the secret back in the POST response
    # (real DC behavior) so we can prove the redactor strips it before
    # it reaches the agent.
    patch_get_fetcher.create_webhook.return_value = {
        "id": 77,
        "name": "hook-new",
        "configuration": {"secret": "hmac-super-secret"},
    }

    result_json = await create_webhook.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        name="hook-new",
        url="https://ci.example.com/hook",
        events='["repo:refs_changed"]',
        secret="hmac-super-secret",
    )
    payload = json.loads(result_json)

    # Success + redacted echo (Req 2.3/2.4).
    assert payload["success"] is True
    assert payload["webhook"]["id"] == 77
    assert payload["webhook"]["configuration"]["secret"] == "[REDACTED]"
    # The plaintext secret must not appear anywhere in the response —
    # this is the hard negative assertion for Req 2.3.
    assert "hmac-super-secret" not in result_json

    # The secret was forwarded to the mixin (part of the "forward to
    # Bitbucket" half of Req 2.3).
    patch_get_fetcher.create_webhook.assert_called_once()
    _, called_kwargs = patch_get_fetcher.create_webhook.call_args
    assert called_kwargs["secret"] == "hmac-super-secret"
    assert called_kwargs["events"] == ["repo:refs_changed"]

    # Reversible Receipt referencing the inverse delete tool (Req 2.5).
    receipt = payload["receipt"]
    assert receipt["object_id"] == "77"
    assert receipt["inverse_tool"] == "bitbucket_delete_webhook"
    assert receipt["inverse_args"] == {
        "project_key": "PROJ",
        "repo_slug": "repo",
        "webhook_id": 77,
    }
    # The receipt's recipient_scope summarizes the broadcast target but
    # MUST NOT include the HMAC secret.
    assert receipt["recipient_scope"] == {
        "url": "https://ci.example.com/hook",
        "events": ["repo:refs_changed"],
    }
    assert "secret" not in (receipt["recipient_scope"] or {})


@pytest.mark.anyio
async def test_create_webhook_blocked_in_read_only(
    fake_ctx, patch_get_fetcher, monkeypatch
):
    """Write tools are blocked when ``READ_ONLY_MODE=true`` (zero HTTP)."""
    from mcp_atlassian.servers.bitbucket import create_webhook

    monkeypatch.setenv("READ_ONLY_MODE", "true")

    result_json = await create_webhook.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        name="hook-new",
        url="https://ci.example.com/hook",
        events='["repo:refs_changed"]',
        secret="should-never-reach-bitbucket",
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "read_only_mode"
    patch_get_fetcher.create_webhook.assert_not_called()


@pytest.mark.anyio
async def test_create_webhook_blocked_by_project_filter(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.bitbucket import create_webhook

    patch_get_fetcher.config.projects_filter = "OTHER"

    result_json = await create_webhook.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        name="hook-new",
        url="https://ci.example.com/hook",
        events='["repo:refs_changed"]',
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "filtered_out"
    patch_get_fetcher.create_webhook.assert_not_called()


@pytest.mark.anyio
async def test_create_webhook_blocked_by_dc_version(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Req 2.6: pre-5.4 instances return ``dc_version_too_old`` on create."""
    from mcp_atlassian.servers.bitbucket import create_webhook

    patch_get_fetcher.get_dc_version.return_value = "5.2.0"
    patch_get_fetcher._dc_version = "5.2.0"

    result_json = await create_webhook.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        name="hook-new",
        url="https://ci.example.com/hook",
        events='["repo:refs_changed"]',
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "dc_version_too_old"
    assert payload["details"]["required_version"] == "5.4"
    patch_get_fetcher.create_webhook.assert_not_called()


# ---- bitbucket_update_webhook ----------------------------------------------


@pytest.mark.anyio
async def test_update_webhook_redacts_secret(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    """Rotated secrets must not leak through update responses."""
    from mcp_atlassian.servers.bitbucket import update_webhook

    patch_get_fetcher.update_webhook.return_value = {
        "id": 42,
        "name": "rotated-hook",
        "configuration": {"secret": "new-hmac"},
    }

    result_json = await update_webhook.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        webhook_id=42,
        name="rotated-hook",
        configuration='{"secret": "new-hmac"}',
    )
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["webhook"]["configuration"]["secret"] == "[REDACTED]"
    # Plaintext secret must not appear in the serialized response.
    assert "new-hmac" not in result_json


@pytest.mark.anyio
async def test_update_webhook_blocked_in_read_only(
    fake_ctx, patch_get_fetcher, monkeypatch
):
    from mcp_atlassian.servers.bitbucket import update_webhook

    monkeypatch.setenv("READ_ONLY_MODE", "true")

    result_json = await update_webhook.fn(
        fake_ctx,
        project_key="PROJ",
        repo_slug="repo",
        webhook_id=1,
        name="renamed",
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "read_only_mode"
    patch_get_fetcher.update_webhook.assert_not_called()


# ---- bitbucket_delete_webhook ----------------------------------------------


@pytest.mark.anyio
async def test_delete_webhook_happy_path(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.bitbucket import delete_webhook

    result_json = await delete_webhook.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo", webhook_id=77
    )
    payload = json.loads(result_json)

    assert payload["success"] is True
    assert payload["deleted"] is True
    assert payload["webhook_id"] == 77
    patch_get_fetcher.delete_webhook.assert_called_once_with("PROJ", "repo", 77)


@pytest.mark.anyio
async def test_delete_webhook_blocked_in_read_only(
    fake_ctx, patch_get_fetcher, monkeypatch
):
    from mcp_atlassian.servers.bitbucket import delete_webhook

    monkeypatch.setenv("READ_ONLY_MODE", "true")

    result_json = await delete_webhook.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo", webhook_id=77
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "read_only_mode"
    patch_get_fetcher.delete_webhook.assert_not_called()


@pytest.mark.anyio
async def test_delete_webhook_blocked_by_dc_version(
    fake_ctx, patch_get_fetcher, disable_read_only
):
    from mcp_atlassian.servers.bitbucket import delete_webhook

    patch_get_fetcher.get_dc_version.return_value = "5.0.0"
    patch_get_fetcher._dc_version = "5.0.0"

    result_json = await delete_webhook.fn(
        fake_ctx, project_key="PROJ", repo_slug="repo", webhook_id=77
    )
    payload = json.loads(result_json)

    assert payload["success"] is False
    assert payload["error_code"] == "dc_version_too_old"
    assert payload["details"]["required_version"] == "5.4"
    patch_get_fetcher.delete_webhook.assert_not_called()


# ---- Registration / tagging parity -----------------------------------------


@pytest.mark.anyio
async def test_webhook_tools_have_expected_tags():
    """Ensure tool tags match Requirement 2.1 / 2.2."""
    from mcp_atlassian.servers.bitbucket import (
        create_webhook,
        delete_webhook,
        get_webhook,
        list_webhooks,
        update_webhook,
    )

    read_tags = {"bitbucket", "read", "toolset:bitbucket_webhooks"}
    write_tags = {"bitbucket", "write", "toolset:bitbucket_webhooks"}

    assert set(list_webhooks.tags) == read_tags
    assert set(get_webhook.tags) == read_tags
    assert set(create_webhook.tags) == write_tags
    assert set(update_webhook.tags) == write_tags
    assert set(delete_webhook.tags) == write_tags
