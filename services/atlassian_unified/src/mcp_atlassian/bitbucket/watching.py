"""Watch / unwatch operations for Bitbucket Cloud and Data Center.

Watch endpoints on Bitbucket are intentionally idempotent at the tool
surface — watching an object you already watch, or unwatching an object
you are not watching, is a no-op from the user's point of view, even
though the underlying REST API may respond with a 409 / 404 depending on
the host and version. To keep the MCP tool surface stable we normalise
those responses into structured ``already_watched`` / ``not_watched``
flags so callers can retry safely without needing to reason about the
specific HTTP error.

DC paths target ``/rest/api/latest/projects/{key}/repos/{slug}/watch``
(and the analogous PR path). Cloud paths target
``/2.0/repositories/{workspace}/{slug}/watchers`` (and the analogous
``/pullrequests/{id}/watchers`` path). The agent-facing method
signatures, parameter names, and return shape do not change between
modes (Requirement 16.5).
"""

import logging
from typing import Any

from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket.watching")


def _resolve_workspace(
    project_key: str | None,
    config_workspace: str | None,
) -> str:
    """Resolve the Cloud workspace for a Bitbucket tool call.

    Precedence rules from Requirements 2.4 / 2.5 / 2.6:

    1. A non-empty ``project_key`` argument wins — it is interpreted as the
       workspace slug in Cloud mode.
    2. Otherwise ``config_workspace`` (populated from ``BITBUCKET_WORKSPACE``
       or the URL path by :meth:`BitbucketConfig.from_env`) is used.
    3. When both are empty/``None``, the mixin raises ``ValueError`` with a
       ``filtered_out:`` prefix so the server layer can map it onto a
       :class:`StructuredError` with ``error_code="filtered_out"`` before
       any outbound HTTP call.
    """
    if project_key:
        return project_key
    if config_workspace:
        return config_workspace
    raise ValueError(
        "filtered_out: Bitbucket Cloud workspace is required. "
        "Pass a non-empty project_key or set BITBUCKET_WORKSPACE."
    )


class WatchingMixin(BitbucketClient):
    """Mixin providing watch/unwatch operations for Bitbucket Cloud and DC."""

    # ------------------------------------------------------------------
    # Cloud HTTP helpers — thin wrappers around ``_session`` that expose
    # the raw status code so we can map 200/204/409/404 onto the
    # ``already_watched`` / ``not_watched`` idempotence contract without
    # raising. Using the low-level session (instead of ``bitbucket.put``
    # / ``bitbucket.delete``) keeps the mapping explicit and avoids any
    # behavior that treats a 409 as a fatal HTTPError.
    # ------------------------------------------------------------------

    def _cloud_watch(self, path: str) -> dict[str, Any]:
        """PUT a Cloud watchers endpoint and map the response to idempotent flags.

        Args:
            path: Cloud API path beginning with ``/2.0/...`` (no base URL).

        Returns:
            ``{"already_watched": False}`` when the server returned a
            2xx status (fresh watch success), or
            ``{"already_watched": True}`` when the server returned 409
            (already subscribed) or any other non-2xx response /
            exception (conservative idempotent fallback).
        """
        full_url = f"{self.config.url}{path}"
        try:
            response = self.bitbucket._session.put(
                full_url,
                verify=self.config.ssl_verify,
            )
        except Exception as e:  # noqa: BLE001 — idempotent fallback
            logger.warning(
                "watch (Cloud): PUT %s raised %s; assuming target is already watched",
                path,
                e,
            )
            return {"already_watched": True}

        status = response.status_code
        # 200 (OK, body returned) and 204 (No Content) both indicate the
        # current user is now subscribed; Cloud does not explicitly
        # distinguish "newly subscribed" from "already subscribed" in the
        # 2xx case, so we report a fresh watch on 2xx and rely on 409 to
        # signal an existing subscription.
        if 200 <= status < 300:
            return {"already_watched": False}
        if status == 409:
            return {"already_watched": True}
        logger.warning(
            "watch (Cloud): PUT %s returned HTTP %s; assuming target is already watched",
            path,
            status,
        )
        return {"already_watched": True}

    def _cloud_unwatch(self, path: str) -> dict[str, Any]:
        """DELETE a Cloud watchers endpoint and map the response to idempotent flags.

        Args:
            path: Cloud API path beginning with ``/2.0/...`` (no base URL).

        Returns:
            ``{"not_watched": False}`` when the server returned 204
            (successful unsubscribe) or any other 2xx, or
            ``{"not_watched": True}`` when the server returned 404
            (the user was not subscribed) or any other non-2xx response
            / exception (conservative idempotent fallback).
        """
        full_url = f"{self.config.url}{path}"
        try:
            response = self.bitbucket._session.delete(
                full_url,
                verify=self.config.ssl_verify,
            )
        except Exception as e:  # noqa: BLE001 — idempotent fallback
            logger.warning(
                "unwatch (Cloud): DELETE %s raised %s; assuming target was not watched",
                path,
                e,
            )
            return {"not_watched": True}

        status = response.status_code
        if 200 <= status < 300:
            return {"not_watched": False}
        if status == 404:
            return {"not_watched": True}
        logger.warning(
            "unwatch (Cloud): DELETE %s returned HTTP %s; assuming target was not watched",
            path,
            status,
        )
        return {"not_watched": True}

    # ------------------------------------------------------------------
    # Pull request watch / unwatch
    # ------------------------------------------------------------------

    def watch_pr(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
    ) -> dict[str, Any]:
        """Start watching a pull request for the authenticated user.

        Idempotent: a second call (or a call against an already-watched PR)
        returns ``{"already_watched": True}`` instead of raising. On DC,
        any HTTP error raised by the underlying client is treated as
        "already watching" — the most common cause by far — and logged at
        WARNING so operators can investigate genuine authz failures. On
        Cloud, the PUT response status is inspected directly: 2xx →
        fresh watch; 409 → already watching; any other status or
        exception → conservative idempotent fallback.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID

        Returns:
            ``{"already_watched": False}`` on a fresh watch, or
            ``{"already_watched": True}`` when the endpoint indicated the
            PR was already being watched.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            path = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/watchers"
            )
            return self._cloud_watch(path)

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/watch"
        )
        try:
            self.bitbucket.post(url)
        except Exception as e:  # noqa: BLE001 — idempotent fallback
            logger.warning(
                "watch_pr: POST %s raised %s; assuming PR is already watched",
                url,
                e,
            )
            return {"already_watched": True}
        return {"already_watched": False}

    def unwatch_pr(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
    ) -> dict[str, Any]:
        """Stop watching a pull request for the authenticated user.

        Idempotent: if the user was not watching the PR (DC typically
        returns 404, Cloud returns 404), the method returns
        ``{"not_watched": True}`` rather than raising.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            pr_id: The pull request ID

        Returns:
            ``{"not_watched": False}`` when an existing watch was removed,
            or ``{"not_watched": True}`` when no watch existed.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            path = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/pullrequests/{pr_id}/watchers"
            )
            return self._cloud_unwatch(path)

        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/watch"
        )
        try:
            self.bitbucket.delete(url)
        except Exception as e:  # noqa: BLE001 — idempotent fallback
            logger.warning(
                "unwatch_pr: DELETE %s raised %s; assuming PR was not watched",
                url,
                e,
            )
            return {"not_watched": True}
        return {"not_watched": False}

    # ------------------------------------------------------------------
    # Repository watch / unwatch
    # ------------------------------------------------------------------

    def watch_repo(
        self,
        project_key: str,
        repo_slug: str,
    ) -> dict[str, Any]:
        """Start watching a repository for the authenticated user.

        Idempotent: repeated calls (or calls against an already-watched
        repository) return ``{"already_watched": True}`` instead of
        raising. Cloud status-code mapping mirrors :meth:`watch_pr`.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug

        Returns:
            ``{"already_watched": False}`` on a fresh watch, or
            ``{"already_watched": True}`` when the repository was already
            being watched.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            path = f"/2.0/repositories/{workspace}/{repo_slug}/watchers"
            return self._cloud_watch(path)

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/watch"
        try:
            self.bitbucket.post(url)
        except Exception as e:  # noqa: BLE001 — idempotent fallback
            logger.warning(
                "watch_repo: POST %s raised %s; assuming repo is already watched",
                url,
                e,
            )
            return {"already_watched": True}
        return {"already_watched": False}

    def unwatch_repo(
        self,
        project_key: str,
        repo_slug: str,
    ) -> dict[str, Any]:
        """Stop watching a repository for the authenticated user.

        Idempotent: if the user was not watching the repository (DC
        typically returns 404, Cloud returns 404), the method returns
        ``{"not_watched": True}`` rather than raising.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug

        Returns:
            ``{"not_watched": False}`` when an existing watch was removed,
            or ``{"not_watched": True}`` when no watch existed.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            path = f"/2.0/repositories/{workspace}/{repo_slug}/watchers"
            return self._cloud_unwatch(path)

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/watch"
        try:
            self.bitbucket.delete(url)
        except Exception as e:  # noqa: BLE001 — idempotent fallback
            logger.warning(
                "unwatch_repo: DELETE %s raised %s; assuming repo was not watched",
                url,
                e,
            )
            return {"not_watched": True}
        return {"not_watched": False}
