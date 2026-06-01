"""Cloud-branch unit tests for :class:`WebhooksMixin`.

These tests cover the Cloud side of the Bitbucket webhooks mixin
introduced by task 14.1 of the ``bitbucket-cloud-dc-parity`` spec
(Requirements 16.4, 19.1, 19.2).

For each method that carries an ``if self.is_cloud:`` branch
(``list_webhooks``, ``get_webhook``, ``create_webhook``,
``update_webhook``, ``delete_webhook``) one happy-path test verifies
that the outbound URL prefix matches the Cloud 2.0 template
``/2.0/repositories/{workspace}/{repo_slug}/hooks[/{uid}]``
(Req 16.4). Additional tests confirm:

* ``create_webhook`` forwards a Cloud-shaped request body
  (``description`` / ``url`` / ``events`` / ``active`` / top-level
  ``secret``) and ships the caller-supplied ``secret`` verbatim so
  Bitbucket can HMAC outbound payloads (Req 16.4 body shape).
* ``update_webhook`` translates the DC-shaped kwargs used by the server
  layer into the Cloud 2.0 body: ``name`` → ``description``,
  ``configuration.secret`` → top-level ``secret`` (Req 16.4 body shape).
* The mixin's Cloud branches do **not** perform secret redaction
  themselves. Redaction is the server layer's job — see
  ``redact_secrets()`` and :mod:`tests.unit.bitbucket.test_webhooks`
  which already lock the redaction contract for both modes. The mixin
  merely forwards the payload it received to downstream callers.

The mixin's DC branches are intentionally **not** touched here — they
are locked byte-for-byte by :mod:`tests.unit.bitbucket.test_webhooks`
and by Requirement 19.2 / 23.2. The tests below stamp ``is_cloud=True``
onto a bypassed :class:`WebhooksMixin` instance and inspect what the
Cloud branch does.

Test pattern (mirrors :mod:`test_branches_cloud_mode` and
:mod:`test_commit_comments_cloud_mode`):

* Bypass :meth:`WebhooksMixin.__init__` via
  :meth:`WebhooksMixin.__new__` to avoid the live-auth / live-HTTP
  constructor (the mixin inherits from :class:`BitbucketClient`).
* Stamp ``mixin.bitbucket = MagicMock()`` so ``get`` / ``post`` / ``put``
  / ``delete`` are driven by :class:`MagicMock`.
* Stamp a :class:`SimpleNamespace` on ``mixin.config`` with
  ``is_cloud=True``, ``workspace="my-team"``, plus the minimal URL / SSL
  attributes the :attr:`BitbucketClient.is_cloud` property reads.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcp_atlassian.bitbucket.webhooks import WebhooksMixin


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cloud_webhooks_mixin() -> WebhooksMixin:
    """Return a :class:`WebhooksMixin` instance wired for Cloud mode.

    ``WebhooksMixin.__new__`` bypasses :meth:`BitbucketClient.__init__`,
    so no real HTTP / auth setup runs. The stamped ``bitbucket`` mock
    stands in for the ``atlassian.Bitbucket`` client; the stamped
    ``config`` namespace carries just enough attributes for the
    :attr:`BitbucketClient.is_cloud` property and the Cloud branches of
    the mixin methods (``config.workspace`` in particular) to work.
    """
    mixin = WebhooksMixin.__new__(WebhooksMixin)
    mixin.bitbucket = MagicMock()
    mixin.config = SimpleNamespace(
        is_cloud=True,
        workspace="my-team",
        url="https://api.bitbucket.org",
        ssl_verify=True,
    )
    return mixin


def _cloud_webhook_payload(
    uid: str,
    *,
    description: str = "hook",
    url_: str = "https://ci.example.com/hook",
    events: list[str] | None = None,
    secret: str | None = None,
    active: bool = True,
) -> dict:
    """Fabricate a Cloud 2.0 webhook dict.

    Cloud returns webhooks with a ``uuid`` primary key, a top-level
    ``description`` (the DC ``name`` analog) and — when the caller set a
    secret on the create/update request — a top-level ``secret`` field.
    :func:`normalize_webhook` passes the payload through as a shallow
    copy; secret redaction is handled by the server layer.
    """
    payload: dict = {
        "uuid": uid,
        "description": description,
        "url": url_,
        "active": active,
        "events": list(events or ["repo:push"]),
    }
    if secret is not None:
        payload["secret"] = secret
    return payload


# ===========================================================================
# list_webhooks (Req 16.4 — list)
# ===========================================================================


class TestListWebhooksCloud:
    """``list_webhooks`` Cloud branch — Requirement 16.4 (list)."""

    def test_issues_cloud_hooks_url(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """Happy path: single-page Cloud envelope, verify URL prefix.

        Cloud termination is ``next=None`` (Req 7.3). Each value is
        routed through :func:`normalize_webhook` (a shallow-copy
        passthrough) so downstream code still sees the Cloud fields
        (``uuid``, ``description``) untouched. The mixin itself does
        **not** redact secrets — that's the server layer's job.
        """
        cloud_webhooks_mixin.bitbucket.get.return_value = {
            "values": [
                _cloud_webhook_payload("{uuid-1}", description="hook-a"),
                _cloud_webhook_payload("{uuid-2}", description="hook-b"),
            ],
            "next": None,
            "page": 1,
            "pagelen": 25,
            "size": 2,
        }

        result = cloud_webhooks_mixin.list_webhooks(
            project_key="my-team", repo_slug="myrepo", limit=25
        )

        cloud_webhooks_mixin.bitbucket.get.assert_called_once()
        (called_url,), _kwargs = cloud_webhooks_mixin.bitbucket.get.call_args
        assert called_url == "/2.0/repositories/my-team/myrepo/hooks"
        # Pagination helper returns a flat list of the Cloud values
        # (normalize_webhook is a shallow passthrough).
        assert [h["uuid"] for h in result] == ["{uuid-1}", "{uuid-2}"]
        assert [h["description"] for h in result] == ["hook-a", "hook-b"]

    def test_uses_config_workspace_when_project_key_empty(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """Workspace fallback (Req 2.5) routes through ``config.workspace``.

        When the caller passes an empty ``project_key`` the Cloud branch
        resolves the workspace from ``config.workspace`` and still emits
        ``/2.0/repositories/my-team/...``.
        """
        cloud_webhooks_mixin.bitbucket.get.return_value = {
            "values": [],
            "next": None,
        }

        cloud_webhooks_mixin.list_webhooks(
            project_key="", repo_slug="r", limit=10
        )

        (called_url,), _ = cloud_webhooks_mixin.bitbucket.get.call_args
        assert called_url == "/2.0/repositories/my-team/r/hooks"

    def test_missing_workspace_raises_filtered_out(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """Empty ``project_key`` AND empty ``config.workspace`` → ``filtered_out``.

        Requirement 2.6: when no workspace can be resolved, the Cloud
        branch raises a ``ValueError`` with a ``filtered_out:`` prefix
        BEFORE any HTTP call, so the server-tool layer can surface the
        structured error with zero outbound Bitbucket traffic
        (Req 19.3).
        """
        cloud_webhooks_mixin.config.workspace = None

        with pytest.raises(ValueError, match="filtered_out"):
            cloud_webhooks_mixin.list_webhooks(
                project_key="", repo_slug="r", limit=10
            )

        # Critical: no outbound HTTP was issued.
        cloud_webhooks_mixin.bitbucket.get.assert_not_called()


# ===========================================================================
# get_webhook (Req 16.4 — get)
# ===========================================================================


class TestGetWebhookCloud:
    """``get_webhook`` Cloud branch — Requirement 16.4 (get)."""

    def test_issues_cloud_hooks_uid_url(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """``GET /2.0/repositories/{ws}/{slug}/hooks/{uid}``.

        Cloud identifies webhooks by ``uuid`` (including the braces),
        while DC uses a numeric ``id``. The mixin accepts either as
        ``webhook_id`` and interpolates it verbatim into the URL path;
        this test confirms a UUID-shaped id round-trips through the
        Cloud template unchanged.
        """
        cloud_webhooks_mixin.bitbucket.get.return_value = (
            _cloud_webhook_payload("{uuid-42}", description="hook-b")
        )

        result = cloud_webhooks_mixin.get_webhook(
            project_key="my-team",
            repo_slug="myrepo",
            webhook_id="{uuid-42}",
        )

        cloud_webhooks_mixin.bitbucket.get.assert_called_once_with(
            "/2.0/repositories/my-team/myrepo/hooks/{uuid-42}"
        )
        # Shallow-copy passthrough — Cloud fields are preserved.
        assert result["uuid"] == "{uuid-42}"
        assert result["description"] == "hook-b"

    def test_rejects_non_dict_response(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """Non-dict response from Cloud raises ``ValueError``.

        Mirrors the DC branch's error handling so the server-tool layer
        renders a consistent envelope regardless of mode.
        """
        cloud_webhooks_mixin.bitbucket.get.return_value = ["unexpected"]

        with pytest.raises(ValueError, match="Unexpected response"):
            cloud_webhooks_mixin.get_webhook(
                project_key="my-team",
                repo_slug="myrepo",
                webhook_id="{uuid-1}",
            )


# ===========================================================================
# create_webhook (Req 16.4 — create)
# ===========================================================================


class TestCreateWebhookCloud:
    """``create_webhook`` Cloud branch — Requirement 16.4 (create)."""

    def test_posts_cloud_hooks_url_with_description_and_secret(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """POST body translates DC ``name`` → Cloud ``description`` and
        forwards a top-level ``secret`` (Req 16.4 body shape).

        Verifies both the outbound URL (Req 16.4) and the Cloud request
        body shape. The secret is forwarded verbatim so Bitbucket can
        sign outbound webhook payloads; the server-layer
        ``redact_secrets()`` helper strips it from the response before
        the agent sees it — that's tested in
        :mod:`tests.unit.bitbucket.test_webhooks`, not here.
        """
        cloud_webhooks_mixin.bitbucket.post.return_value = (
            _cloud_webhook_payload(
                "{uuid-77}",
                description="hook-new",
                events=["repo:push"],
                secret="hmac-super-secret",
            )
        )

        result = cloud_webhooks_mixin.create_webhook(
            project_key="my-team",
            repo_slug="myrepo",
            name="hook-new",
            url="https://ci.example.com/hook",
            events=["repo:push"],
            secret="hmac-super-secret",
            active=True,
        )

        cloud_webhooks_mixin.bitbucket.post.assert_called_once()
        (called_url,), kwargs = (
            cloud_webhooks_mixin.bitbucket.post.call_args
        )
        assert called_url == "/2.0/repositories/my-team/myrepo/hooks"
        # Cloud-shaped body: ``description`` not ``name``; top-level
        # ``secret`` not ``configuration.secret``.
        assert kwargs["data"] == {
            "description": "hook-new",
            "url": "https://ci.example.com/hook",
            "active": True,
            "events": ["repo:push"],
            "secret": "hmac-super-secret",
        }
        # DC-specific envelope must not leak onto the Cloud body.
        assert "name" not in kwargs["data"]
        assert "configuration" not in kwargs["data"]
        # Response passes through normalize_webhook (shallow copy).
        assert result["uuid"] == "{uuid-77}"
        assert result["description"] == "hook-new"

    def test_omits_secret_when_not_provided(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """Cloud body skips ``secret`` entirely when the caller omits it.

        The Cloud API treats a missing ``secret`` field as "no HMAC",
        which differs from DC where ``configuration`` is always sent as
        an (empty) object. The Cloud branch must NOT introduce a
        placeholder.
        """
        cloud_webhooks_mixin.bitbucket.post.return_value = (
            _cloud_webhook_payload("{uuid-78}", description="hook-d")
        )

        cloud_webhooks_mixin.create_webhook(
            project_key="my-team",
            repo_slug="myrepo",
            name="hook-d",
            url="https://ci.example.com/hook",
            events=["pullrequest:created"],
        )

        _args, kwargs = cloud_webhooks_mixin.bitbucket.post.call_args
        assert "secret" not in kwargs["data"]
        # ``configuration`` envelope must not be fabricated either.
        assert "configuration" not in kwargs["data"]

    def test_forwards_secret_but_does_not_redact_at_mixin_layer(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """The mixin returns whatever Bitbucket returned verbatim.

        Secret hygiene — redacting ``secret`` / ``configuration.secret``
        values and ensuring the caller-supplied string never appears in
        the final tool response — is the server layer's responsibility
        (``redact_secrets()``). The mixin's job is strictly to forward
        the payload; this test pins that contract so it can't drift
        without notice.
        """
        cloud_webhooks_mixin.bitbucket.post.return_value = (
            _cloud_webhook_payload(
                "{uuid-79}",
                description="hook-e",
                secret="echo-back-from-bitbucket",
            )
        )

        result = cloud_webhooks_mixin.create_webhook(
            project_key="my-team",
            repo_slug="myrepo",
            name="hook-e",
            url="https://ci.example.com/hook",
            events=["repo:push"],
            secret="ships-this-to-bitbucket",
        )

        # The secret was forwarded to Bitbucket (outbound body).
        _args, out_kwargs = cloud_webhooks_mixin.bitbucket.post.call_args
        assert out_kwargs["data"]["secret"] == "ships-this-to-bitbucket"

        # The mixin does not strip secrets from the returned payload —
        # that's the server-layer contract. Pinning this at the mixin
        # layer makes the boundary explicit.
        assert result.get("secret") == "echo-back-from-bitbucket"


# ===========================================================================
# update_webhook (Req 16.4 — update, body translation)
# ===========================================================================


class TestUpdateWebhookCloud:
    """``update_webhook`` Cloud branch — Requirement 16.4 (update)."""

    def test_puts_cloud_hooks_uid_url(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """``PUT /2.0/repositories/{ws}/{slug}/hooks/{uid}``.

        Verifies the outbound URL suffix (Req 16.4) with a UUID-shaped
        webhook id.
        """
        cloud_webhooks_mixin.bitbucket.put.return_value = (
            _cloud_webhook_payload("{uuid-42}", description="rotated")
        )

        result = cloud_webhooks_mixin.update_webhook(
            project_key="my-team",
            repo_slug="myrepo",
            webhook_id="{uuid-42}",
            name="rotated",
        )

        cloud_webhooks_mixin.bitbucket.put.assert_called_once()
        (called_url,), _kwargs = (
            cloud_webhooks_mixin.bitbucket.put.call_args
        )
        assert called_url == "/2.0/repositories/my-team/myrepo/hooks/{uuid-42}"
        assert result["uuid"] == "{uuid-42}"

    def test_translates_dc_name_to_cloud_description(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """DC ``name`` kwarg → Cloud ``description`` in the PUT body.

        The server-tool layer still calls the mixin with DC-shaped
        kwargs (``name``, ``configuration``, ...); the Cloud branch is
        responsible for translating them onto the Cloud 2.0 body shape.
        """
        cloud_webhooks_mixin.bitbucket.put.return_value = (
            _cloud_webhook_payload("{uuid-42}", description="new-name")
        )

        cloud_webhooks_mixin.update_webhook(
            project_key="my-team",
            repo_slug="myrepo",
            webhook_id="{uuid-42}",
            name="new-name",
            url="https://ci.example.com/rotated",
            events=["repo:push"],
            active=False,
        )

        _args, kwargs = cloud_webhooks_mixin.bitbucket.put.call_args
        assert kwargs["data"] == {
            "description": "new-name",
            "url": "https://ci.example.com/rotated",
            "events": ["repo:push"],
            "active": False,
        }
        # DC-specific key must not leak onto the Cloud body.
        assert "name" not in kwargs["data"]

    def test_translates_configuration_secret_to_top_level_secret(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """DC ``configuration.secret`` → Cloud top-level ``secret``.

        The server-tool layer accepts a DC-shaped JSON blob with the
        secret nested under ``configuration.secret``; the Cloud branch
        must lift that into the Cloud-native top-level ``secret`` field
        (Req 16.4 body shape). The caller-supplied string is forwarded
        verbatim — redaction is the server layer's job.
        """
        cloud_webhooks_mixin.bitbucket.put.return_value = (
            _cloud_webhook_payload(
                "{uuid-42}",
                description="rotated",
                secret="rotated-hmac",
            )
        )

        cloud_webhooks_mixin.update_webhook(
            project_key="my-team",
            repo_slug="myrepo",
            webhook_id="{uuid-42}",
            configuration={"secret": "rotated-hmac"},
        )

        _args, kwargs = cloud_webhooks_mixin.bitbucket.put.call_args
        assert kwargs["data"]["secret"] == "rotated-hmac"
        # DC ``configuration`` envelope must not leak onto the Cloud body.
        assert "configuration" not in kwargs["data"]

    def test_bare_secret_kwarg_wins_over_configuration_secret(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """A bare ``secret=`` kwarg overrides ``configuration.secret``.

        Callers that want to rotate the secret without rebuilding the
        entire ``configuration`` dict can pass ``secret=`` directly; the
        Cloud branch honors it as the last-write-wins value on the PUT
        body.
        """
        cloud_webhooks_mixin.bitbucket.put.return_value = (
            _cloud_webhook_payload(
                "{uuid-42}",
                description="hook",
                secret="winner",
            )
        )

        cloud_webhooks_mixin.update_webhook(
            project_key="my-team",
            repo_slug="myrepo",
            webhook_id="{uuid-42}",
            configuration={"secret": "loser"},
            secret="winner",
        )

        _args, kwargs = cloud_webhooks_mixin.bitbucket.put.call_args
        assert kwargs["data"]["secret"] == "winner"
        assert "configuration" not in kwargs["data"]


# ===========================================================================
# delete_webhook (Req 16.4 — delete)
# ===========================================================================


class TestDeleteWebhookCloud:
    """``delete_webhook`` Cloud branch — Requirement 16.4 (delete)."""

    def test_deletes_cloud_hooks_uid_url(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """``DELETE /2.0/repositories/{ws}/{slug}/hooks/{uid}``.

        The Cloud DELETE is a bare URL call — no request body, no query
        parameters. Returns ``None`` to parallel the DC 204 No Content
        path.
        """
        cloud_webhooks_mixin.bitbucket.delete.return_value = None

        result = cloud_webhooks_mixin.delete_webhook(
            project_key="my-team",
            repo_slug="myrepo",
            webhook_id="{uuid-77}",
        )

        assert result is None
        cloud_webhooks_mixin.bitbucket.delete.assert_called_once_with(
            "/2.0/repositories/my-team/myrepo/hooks/{uuid-77}"
        )


# ===========================================================================
# Cross-method: mixin layer does NOT redact supplied secret
# ===========================================================================


class TestMixinDoesNotRedactSecret:
    """The mixin forwards secrets verbatim; redaction lives in the server.

    Requirement 16.4 says webhook responses must have ``secret`` /
    ``configuration.secret`` redacted and the input secret must never
    appear in the returned JSON. That is enforced by the server-layer
    ``redact_secrets()`` helper (see :mod:`test_webhooks`), not by the
    mixin. These tests pin the boundary: the mixin is a pure forwarder.
    A regression that moved redaction into the mixin would trip here,
    prompting a review of where the redaction actually belongs.
    """

    def test_create_returns_bitbucket_payload_unredacted(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """Create response preserves whatever Bitbucket returns.

        The server layer's ``redact_secrets()`` runs after the mixin
        returns; at the mixin layer the secret is still present.
        """
        cloud_webhooks_mixin.bitbucket.post.return_value = (
            _cloud_webhook_payload(
                "{uuid-1}",
                description="hook",
                secret="echoed-back-from-bitbucket",
            )
        )

        result = cloud_webhooks_mixin.create_webhook(
            project_key="my-team",
            repo_slug="myrepo",
            name="hook",
            url="https://ci.example.com/hook",
            events=["repo:push"],
            secret="supplied-by-caller",
        )

        # Mixin returns the Bitbucket payload as-is (pre-redaction).
        assert result["secret"] == "echoed-back-from-bitbucket"

    def test_get_returns_bitbucket_payload_unredacted(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """Get response preserves whatever Bitbucket returns.

        Same boundary as create: the mixin is a forwarder; the server
        layer owns redaction.
        """
        cloud_webhooks_mixin.bitbucket.get.return_value = (
            _cloud_webhook_payload(
                "{uuid-1}",
                description="hook",
                secret="still-present-at-mixin-layer",
            )
        )

        result = cloud_webhooks_mixin.get_webhook(
            project_key="my-team",
            repo_slug="myrepo",
            webhook_id="{uuid-1}",
        )

        assert result["secret"] == "still-present-at-mixin-layer"

    def test_supplied_secret_appears_in_outbound_body_only(
        self, cloud_webhooks_mixin: WebhooksMixin
    ) -> None:
        """Caller-supplied secret reaches Bitbucket (outbound) verbatim.

        Cloud HMAC requires the secret to be transmitted to Bitbucket
        exactly once, on the create/update PUT/POST. The mixin
        guarantees that forwarding; the server layer guarantees the
        secret never travels in the opposite direction (towards the
        agent).
        """
        cloud_webhooks_mixin.bitbucket.post.return_value = (
            _cloud_webhook_payload("{uuid-1}", description="hook")
        )

        cloud_webhooks_mixin.create_webhook(
            project_key="my-team",
            repo_slug="myrepo",
            name="hook",
            url="https://ci.example.com/hook",
            events=["repo:push"],
            secret="ships-to-bitbucket",
        )

        # The secret is present in the outbound body ...
        _args, kwargs = cloud_webhooks_mixin.bitbucket.post.call_args
        assert kwargs["data"]["secret"] == "ships-to-bitbucket"
        # ... and only in the outbound body (the raw serialized form
        # contains the secret exactly once).
        serialized = json.dumps(kwargs["data"])
        assert serialized.count("ships-to-bitbucket") == 1
