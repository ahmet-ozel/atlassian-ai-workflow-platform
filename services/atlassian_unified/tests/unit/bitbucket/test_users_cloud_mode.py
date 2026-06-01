"""Cloud-branch unit tests for :class:`UsersMixin`.

These tests cover the Cloud side of the Bitbucket users mixin introduced
by tasks 13.1 and 13.2 of the ``bitbucket-cloud-dc-parity`` spec
(Requirements 13.1, 13.2, 13.3, 13.5, 19.1, 19.2, 19.3).

Scope:

* Happy-path ``get_user`` with a brace-wrapped UUID shape
  (``{01234567-89ab-cdef-0123-456789abcdef}``) — Requirement 13.2 —
  verifies the outbound URL prefix matches the Cloud 2.0 template
  ``/2.0/users/{account_id}`` and that the response is normalized so
  the returned dict exposes ``account_id``, ``name`` and ``slug``
  (Requirement 13.5).
* Happy-path ``get_user`` with a modern ``account_id`` shape
  (``557058:abc-123``) — Requirement 13.2 — verifies the same URL
  template is used for the ``:``-separated identifier form.
* Pre-HTTP ``invalid_target`` rejection (Requirement 13.3) for
  DC-style usernames that contain characters outside the Cloud
  ``account_id`` alphabet (``.``, ``@``, whitespace, empty string).
  The Cloud branch must raise ``ValueError`` with an
  ``invalid_target:`` prefix *before* any outbound HTTP call
  (Requirement 19.3).

The mixin's DC branch is intentionally **not** exercised here — it is
locked byte-for-byte by Requirement 19.2 / 23.2 and covered by other
modules. The tests below stamp ``is_cloud=True`` onto a bypassed
:class:`UsersMixin` instance so only the Cloud branch runs.

Test pattern (mirrors the sibling ``test_*_cloud_mode`` modules):

* Bypass :meth:`UsersMixin.__init__` via :meth:`UsersMixin.__new__` to
  avoid the live-auth / live-HTTP constructor (the mixin inherits from
  :class:`BitbucketClient`).
* Stamp ``mixin.bitbucket = MagicMock()`` so ``get`` is driven by
  :class:`MagicMock`.
* Stamp a :class:`SimpleNamespace` on ``mixin.config`` with
  ``is_cloud=True`` plus the minimal URL / SSL attributes the
  :attr:`BitbucketClient.is_cloud` property reads.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcp_atlassian.bitbucket.users import UsersMixin


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cloud_users_mixin() -> UsersMixin:
    """Return a :class:`UsersMixin` instance wired for Cloud mode.

    ``UsersMixin.__new__`` bypasses :meth:`BitbucketClient.__init__`, so
    no real HTTP / auth setup runs. The stamped ``bitbucket`` mock
    stands in for the ``atlassian.Bitbucket`` client; the stamped
    ``config`` namespace carries just enough attributes for the
    :attr:`BitbucketClient.is_cloud` property to return ``True``.
    ``workspace`` is intentionally ``None`` because ``get_user`` does
    not resolve a workspace — Cloud users are workspace-agnostic.
    """
    mixin = UsersMixin.__new__(UsersMixin)
    mixin.bitbucket = MagicMock()
    mixin.config = SimpleNamespace(
        is_cloud=True,
        workspace=None,
        url="https://api.bitbucket.org",
        ssl_verify=True,
    )
    return mixin


def _cloud_user_payload(account_id: str) -> dict:
    """Fabricate a Cloud 2.0 user dict keyed off ``account_id``.

    :func:`normalize_user` copies ``account_id`` into the synthesized
    ``name`` / ``slug`` fields and mirrors ``display_name`` onto
    ``displayName`` / ``uuid`` onto ``id``. Returning the Cloud shape
    here lets every test assert both the outbound URL and the
    normalized payload downstream consumers see.
    """
    return {
        "account_id": account_id,
        "display_name": "Jane Doe",
        "uuid": "{aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee}",
        "nickname": "jdoe",
        "type": "user",
    }


# ===========================================================================
# get_user happy paths (Req 13.2, 13.5)
# ===========================================================================


class TestGetUserCloudHappyPath:
    """Cloud ``get_user`` — Requirements 13.1, 13.2, 13.5."""

    def test_brace_uuid_routes_to_cloud_users_endpoint(
        self, cloud_users_mixin: UsersMixin
    ) -> None:
        """Brace-wrapped UUID is accepted and routed to ``/2.0/users/{uuid}``.

        The brace-wrapped UUID is the legacy Cloud account identifier
        shape; Requirement 13.2 requires ``get_user`` to accept it and
        forward it verbatim as the path segment on Cloud.
        """
        account_id = "{01234567-89ab-cdef-0123-456789abcdef}"
        cloud_users_mixin.bitbucket.get.return_value = _cloud_user_payload(
            account_id
        )

        result = cloud_users_mixin.get_user(account_id)

        cloud_users_mixin.bitbucket.get.assert_called_once()
        (called_url,), _kwargs = cloud_users_mixin.bitbucket.get.call_args
        assert called_url == f"/2.0/users/{account_id}"
        # Requirement 13.5: the normalized payload exposes both the
        # Cloud ``account_id`` and the DC ``name`` / ``slug`` keys with
        # identical values.
        assert result["account_id"] == account_id
        assert result["name"] == account_id
        assert result["slug"] == account_id
        # Cloud-only fields pass through so callers keep full access.
        assert result["display_name"] == "Jane Doe"
        # ``display_name`` is mirrored onto ``displayName`` (DC key).
        assert result["displayName"] == "Jane Doe"

    def test_modern_account_id_routes_to_cloud_users_endpoint(
        self, cloud_users_mixin: UsersMixin
    ) -> None:
        """Modern ``account_id`` (``557058:abc-123``) is accepted.

        The ``:``-separated ``account_id`` is the canonical modern
        Cloud identifier. Requirement 13.2 requires ``get_user`` to
        accept it and forward it verbatim as the path segment.
        """
        account_id = "557058:abc-123"
        cloud_users_mixin.bitbucket.get.return_value = _cloud_user_payload(
            account_id
        )

        result = cloud_users_mixin.get_user(account_id)

        cloud_users_mixin.bitbucket.get.assert_called_once()
        (called_url,), _kwargs = cloud_users_mixin.bitbucket.get.call_args
        assert called_url == f"/2.0/users/{account_id}"
        # Normalized payload still exposes DC-shaped keys.
        assert result["account_id"] == account_id
        assert result["name"] == account_id
        assert result["slug"] == account_id


# ===========================================================================
# get_user pre-HTTP invalid_target guard (Req 13.3, 19.3)
# ===========================================================================


class TestGetUserCloudInvalidTarget:
    """Cloud ``get_user`` pre-HTTP ``invalid_target`` — Requirement 13.3.

    When ``is_cloud`` is ``True`` and the ``username`` argument does
    not match either the brace-wrapped UUID shape
    (``^\\{[0-9a-f-]{36}\\}$``) nor the modern ``account_id`` shape
    (``^[A-Za-z0-9_:\\-]+$``), the mixin SHALL raise a ``ValueError``
    whose message is prefixed with ``invalid_target:`` before issuing
    any outbound HTTP call (Requirement 19.3).

    DC-style usernames typically contain characters outside these
    allowlists — periods (``john.doe``), ``@`` (``jane@example.com``),
    whitespace (display names), or they are empty. Each of those cases
    must be rejected pre-HTTP.
    """

    @pytest.mark.parametrize(
        "dc_style_username",
        [
            # A DC username with a period — first.last is the most
            # common DC convention, disallowed on Cloud.
            "john.doe",
            # A DC username that contains ``@`` (e.g. when the slug is
            # the user's email address).
            "jane@example.com",
            # Whitespace is never valid in an ``account_id``.
            "jane doe",
            # A leading space variant — still rejected.
            " jdoe",
            # Empty string — no account id to target.
            "",
        ],
    )
    def test_dc_style_username_raises_invalid_target_pre_http(
        self,
        cloud_users_mixin: UsersMixin,
        dc_style_username: str,
    ) -> None:
        """DC-style ``username`` values are rejected with ``invalid_target:``.

        The guard runs *before* any HTTP call, so the mocked
        ``bitbucket.get`` must remain untouched — verified by
        ``call_count == 0`` (Requirement 19.3).
        """
        with pytest.raises(ValueError) as exc_info:
            cloud_users_mixin.get_user(dc_style_username)

        # Message carries the structured ``invalid_target:`` prefix
        # so the server-tool layer can render the right envelope.
        assert str(exc_info.value).startswith("invalid_target:")
        # Zero outbound HTTP calls — this is the core of Requirement 19.3.
        assert cloud_users_mixin.bitbucket.get.call_count == 0
