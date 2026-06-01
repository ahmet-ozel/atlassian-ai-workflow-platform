"""Cloud-branch unit tests for :class:`WatchingMixin`.

These tests cover the Cloud side of the Bitbucket watch/unwatch mixin
introduced by task 15 of the ``bitbucket-cloud-dc-parity`` spec
(Requirements 16.5, 19.1, 19.2).

For each method that carries an ``if self.is_cloud:`` branch
(``watch_pr``, ``unwatch_pr``, ``watch_repo``, ``unwatch_repo``) three
behaviors are exercised:

1. Happy path — on a 2xx status the Cloud branch returns
   ``{"already_watched": False}`` (watch) or ``{"not_watched": False}``
   (unwatch) and the outbound URL matches the Cloud 2.0 watchers
   template ``/2.0/repositories/{workspace}/{slug}/watchers`` or
   ``.../pullrequests/{id}/watchers``.
2. Double-watch — a PUT that returns HTTP 409 (the remote already has
   an active subscription for the authenticated user) is normalized
   into ``{"already_watched": True}``. Only the single PUT is issued;
   the mixin MUST NOT retry or emit any extra HTTP calls.
3. Unwatch-while-not-watching — a DELETE that returns HTTP 404 (no
   existing subscription) is normalized into ``{"not_watched": True}``,
   again with a single DELETE call.

These three cases together lock the Cloud idempotence contract
described in :mod:`mcp_atlassian.bitbucket.watching` and keep the
agent-facing return shape identical to the DC branch.

The mixin's DC branches are intentionally **not** touched here — those
paths are locked by Requirement 19.2 and by the existing DC tests. The
tests below only stamp ``is_cloud=True`` onto a bypassed
``WatchingMixin`` instance and inspect what the Cloud branch does.

Test pattern (mirrors :mod:`test_branches_cloud_mode` and
:mod:`test_commit_comments_cloud_mode`):

* Bypass :meth:`WatchingMixin.__init__` via
  :meth:`WatchingMixin.__new__` to avoid the live-auth / live-HTTP
  constructor (the mixin inherits from :class:`BitbucketClient`).
* Stamp ``mixin.bitbucket = MagicMock()`` so ``bitbucket._session.put``
  / ``bitbucket._session.delete`` are driven by :class:`MagicMock`.
  The Cloud branch calls the low-level session directly (rather than
  ``bitbucket.put`` / ``bitbucket.delete``) so the status-code-driven
  idempotence mapping is testable without monkey-patching the
  higher-level HTTP client.
* Stamp a :class:`SimpleNamespace` onto ``mixin.config`` that exposes
  just the attributes the :attr:`BitbucketClient.is_cloud` property
  and the Cloud branches read: ``is_cloud``, ``workspace``, ``url``,
  ``ssl_verify``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from mcp_atlassian.bitbucket.watching import WatchingMixin


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cloud_watching_mixin() -> WatchingMixin:
    """Return a :class:`WatchingMixin` instance wired for Cloud mode.

    ``WatchingMixin.__new__`` bypasses :meth:`BitbucketClient.__init__`,
    so no real HTTP or auth setup runs. The stamped ``bitbucket`` mock
    stands in for the ``atlassian.Bitbucket`` client; the stamped
    ``config`` namespace provides just enough attributes for
    :attr:`BitbucketClient.is_cloud`, ``_resolve_workspace``, and the
    Cloud ``_cloud_watch`` / ``_cloud_unwatch`` helpers (which read
    ``config.url`` and ``config.ssl_verify``) to work.
    """
    mixin = WatchingMixin.__new__(WatchingMixin)
    mixin.bitbucket = MagicMock()
    mixin.config = SimpleNamespace(
        is_cloud=True,
        workspace="my-team",
        url="https://api.bitbucket.org",
        ssl_verify=True,
    )
    return mixin


def _response(status: int) -> MagicMock:
    """Fabricate a minimal ``requests.Response``-shaped mock.

    Only ``status_code`` is consulted by :meth:`WatchingMixin._cloud_watch`
    / :meth:`WatchingMixin._cloud_unwatch`, so the helper deliberately
    stops there to keep the fakes shallow and obvious at the call site.
    """
    response = MagicMock()
    response.status_code = status
    return response


# ===========================================================================
# watch_pr (Req 16.5, 19.1)
# ===========================================================================


class TestWatchPrCloud:
    """``watch_pr`` Cloud branch — Requirements 16.5, 19.1."""

    def test_happy_path_returns_already_watched_false_on_2xx(
        self, cloud_watching_mixin: WatchingMixin
    ) -> None:
        """2xx from PUT → ``{"already_watched": False}`` at the expected URL.

        Verifies the outbound URL uses the Cloud PR-watchers template
        ``/2.0/repositories/{workspace}/{slug}/pullrequests/{id}/watchers``
        (prefixed with ``config.url``) and that the first-watch response
        shape is synthesized from the 2xx status alone — the Cloud API
        does not explicitly signal "newly subscribed" vs "already
        subscribed" in the 2xx case, so 2xx is always treated as a
        fresh watch (Req 16.5 idempotent surface).
        """
        cloud_watching_mixin.bitbucket._session.put.return_value = _response(204)

        result = cloud_watching_mixin.watch_pr(
            project_key="my-team",
            repo_slug="myrepo",
            pr_id=42,
        )

        assert result == {"already_watched": False}
        cloud_watching_mixin.bitbucket._session.put.assert_called_once()
        (called_url,), kwargs = cloud_watching_mixin.bitbucket._session.put.call_args
        assert called_url == (
            "https://api.bitbucket.org"
            "/2.0/repositories/my-team/myrepo/pullrequests/42/watchers"
        )
        # SSL verification flag is plumbed through from config.
        assert kwargs["verify"] is True
        # The DELETE primitive is untouched on a watch call.
        cloud_watching_mixin.bitbucket._session.delete.assert_not_called()

    def test_double_watch_409_returns_already_watched_true_without_retry(
        self, cloud_watching_mixin: WatchingMixin
    ) -> None:
        """409 from PUT → ``{"already_watched": True}`` with a single HTTP call.

        The Cloud idempotence contract collapses "already subscribed"
        into a non-raising ``already_watched=True`` result. The test
        also asserts that no retry / fallback call is issued — the
        mixin must map the status code directly without touching the
        DELETE primitive or issuing a second PUT.
        """
        cloud_watching_mixin.bitbucket._session.put.return_value = _response(409)

        result = cloud_watching_mixin.watch_pr(
            project_key="my-team",
            repo_slug="myrepo",
            pr_id=42,
        )

        assert result == {"already_watched": True}
        assert cloud_watching_mixin.bitbucket._session.put.call_count == 1
        cloud_watching_mixin.bitbucket._session.delete.assert_not_called()

    def test_uses_config_workspace_when_project_key_empty(
        self, cloud_watching_mixin: WatchingMixin
    ) -> None:
        """Workspace fallback (Req 2.5) routes through ``config.workspace``.

        When the caller passes an empty ``project_key`` the Cloud
        branch resolves the workspace from ``config.workspace`` and
        still emits ``/2.0/repositories/my-team/...`` — the same URL
        prefix the tool surface advertises.
        """
        cloud_watching_mixin.bitbucket._session.put.return_value = _response(200)

        cloud_watching_mixin.watch_pr(
            project_key="",
            repo_slug="r",
            pr_id=7,
        )

        (called_url,), _ = cloud_watching_mixin.bitbucket._session.put.call_args
        assert called_url == (
            "https://api.bitbucket.org"
            "/2.0/repositories/my-team/r/pullrequests/7/watchers"
        )


# ===========================================================================
# unwatch_pr (Req 16.5, 19.1)
# ===========================================================================


class TestUnwatchPrCloud:
    """``unwatch_pr`` Cloud branch — Requirements 16.5, 19.1."""

    def test_happy_path_returns_not_watched_false_on_2xx(
        self, cloud_watching_mixin: WatchingMixin
    ) -> None:
        """2xx from DELETE → ``{"not_watched": False}`` at the expected URL.

        Verifies the DELETE targets the same PR-watchers URL the PUT
        targets (Req 16.5 URL parity) and that a successful unsubscribe
        is surfaced as ``not_watched=False`` — the caller had an active
        subscription before the call.
        """
        cloud_watching_mixin.bitbucket._session.delete.return_value = _response(204)

        result = cloud_watching_mixin.unwatch_pr(
            project_key="my-team",
            repo_slug="myrepo",
            pr_id=42,
        )

        assert result == {"not_watched": False}
        cloud_watching_mixin.bitbucket._session.delete.assert_called_once()
        (called_url,), kwargs = cloud_watching_mixin.bitbucket._session.delete.call_args
        assert called_url == (
            "https://api.bitbucket.org"
            "/2.0/repositories/my-team/myrepo/pullrequests/42/watchers"
        )
        assert kwargs["verify"] is True
        # The PUT primitive is untouched on an unwatch call.
        cloud_watching_mixin.bitbucket._session.put.assert_not_called()

    def test_unwatch_while_not_watching_404_returns_not_watched_true(
        self, cloud_watching_mixin: WatchingMixin
    ) -> None:
        """404 from DELETE → ``{"not_watched": True}`` with a single HTTP call.

        The Cloud idempotence contract collapses "was not subscribed"
        into a non-raising ``not_watched=True`` result. A single DELETE
        is issued; no compensating PUT or retry is emitted.
        """
        cloud_watching_mixin.bitbucket._session.delete.return_value = _response(404)

        result = cloud_watching_mixin.unwatch_pr(
            project_key="my-team",
            repo_slug="myrepo",
            pr_id=42,
        )

        assert result == {"not_watched": True}
        assert cloud_watching_mixin.bitbucket._session.delete.call_count == 1
        cloud_watching_mixin.bitbucket._session.put.assert_not_called()


# ===========================================================================
# watch_repo (Req 16.5, 19.1)
# ===========================================================================


class TestWatchRepoCloud:
    """``watch_repo`` Cloud branch — Requirements 16.5, 19.1."""

    def test_happy_path_returns_already_watched_false_on_2xx(
        self, cloud_watching_mixin: WatchingMixin
    ) -> None:
        """2xx from PUT → ``{"already_watched": False}`` at the expected URL.

        The repository watchers URL omits the ``/pullrequests/{id}``
        segment and targets ``/2.0/repositories/{ws}/{slug}/watchers``
        directly.
        """
        cloud_watching_mixin.bitbucket._session.put.return_value = _response(204)

        result = cloud_watching_mixin.watch_repo(
            project_key="my-team",
            repo_slug="myrepo",
        )

        assert result == {"already_watched": False}
        cloud_watching_mixin.bitbucket._session.put.assert_called_once()
        (called_url,), kwargs = cloud_watching_mixin.bitbucket._session.put.call_args
        assert called_url == (
            "https://api.bitbucket.org"
            "/2.0/repositories/my-team/myrepo/watchers"
        )
        assert kwargs["verify"] is True
        cloud_watching_mixin.bitbucket._session.delete.assert_not_called()

    def test_double_watch_409_returns_already_watched_true_without_retry(
        self, cloud_watching_mixin: WatchingMixin
    ) -> None:
        """409 from PUT → ``{"already_watched": True}`` with a single HTTP call.

        Mirrors the PR-level double-watch contract at the repository
        scope. The single PUT must suffice; no retry / compensating
        DELETE is issued when the remote reports an existing
        subscription.
        """
        cloud_watching_mixin.bitbucket._session.put.return_value = _response(409)

        result = cloud_watching_mixin.watch_repo(
            project_key="my-team",
            repo_slug="myrepo",
        )

        assert result == {"already_watched": True}
        assert cloud_watching_mixin.bitbucket._session.put.call_count == 1
        cloud_watching_mixin.bitbucket._session.delete.assert_not_called()


# ===========================================================================
# unwatch_repo (Req 16.5, 19.1)
# ===========================================================================


class TestUnwatchRepoCloud:
    """``unwatch_repo`` Cloud branch — Requirements 16.5, 19.1."""

    def test_happy_path_returns_not_watched_false_on_2xx(
        self, cloud_watching_mixin: WatchingMixin
    ) -> None:
        """2xx from DELETE → ``{"not_watched": False}`` at the expected URL.

        The DELETE shares the PUT's repository-watchers URL, locking
        the DC/Cloud URL parity at the repo scope as well as the
        return-shape parity required by Req 16.5.
        """
        cloud_watching_mixin.bitbucket._session.delete.return_value = _response(204)

        result = cloud_watching_mixin.unwatch_repo(
            project_key="my-team",
            repo_slug="myrepo",
        )

        assert result == {"not_watched": False}
        cloud_watching_mixin.bitbucket._session.delete.assert_called_once()
        (called_url,), kwargs = cloud_watching_mixin.bitbucket._session.delete.call_args
        assert called_url == (
            "https://api.bitbucket.org"
            "/2.0/repositories/my-team/myrepo/watchers"
        )
        assert kwargs["verify"] is True
        cloud_watching_mixin.bitbucket._session.put.assert_not_called()

    def test_unwatch_while_not_watching_404_returns_not_watched_true(
        self, cloud_watching_mixin: WatchingMixin
    ) -> None:
        """404 from DELETE → ``{"not_watched": True}`` with a single HTTP call.

        A DELETE against a repository the caller never watched must
        collapse into ``not_watched=True`` — not a raised exception —
        so downstream agent flows stay idempotent.
        """
        cloud_watching_mixin.bitbucket._session.delete.return_value = _response(404)

        result = cloud_watching_mixin.unwatch_repo(
            project_key="my-team",
            repo_slug="myrepo",
        )

        assert result == {"not_watched": True}
        assert cloud_watching_mixin.bitbucket._session.delete.call_count == 1
        cloud_watching_mixin.bitbucket._session.put.assert_not_called()
