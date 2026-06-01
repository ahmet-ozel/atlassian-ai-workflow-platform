"""Required-builds merge-check operations for Bitbucket Data Center.

Required-builds conditions are exposed by the bundled Bitbucket
required-builds plugin under ``/rest/required-builds/latest``. Each
condition names one or more *build parent keys* (the Bamboo / Jenkins
plan keys whose builds must succeed) and a *ref matcher* describing
the branches the rule applies to. An optional *exemption matcher*
lets release managers exempt specific users or groups from the gate.

Plugin availability (Requirement 3.4): when the required-builds plugin
is absent or disabled, every request to ``/rest/required-builds/...``
returns ``404 Not Found`` without a plugin-specific envelope. The
mixin translates that status into
:class:`RequiredBuildsPluginUnavailableError` so the server-tool layer
can map it onto a structured ``plugin_unavailable`` error without
inspecting HTTP status codes from inside the tool function.

All other non-2xx responses (for example 401/403 on authentication or
permission failures) are surfaced by re-raising the underlying
``HTTPError`` so the server-tool layer can map them through the
existing error-handling path.
"""

from __future__ import annotations

import logging
from typing import Any

from requests.exceptions import HTTPError

from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket.required_builds")


class RequiredBuildsPluginUnavailableError(Exception):
    """Raised when the Bitbucket required-builds plugin endpoint returns 404.

    The required-builds REST endpoints are provided by a bundled Bitbucket
    plugin. On DC instances where the plugin is absent or disabled, every
    request to ``/rest/required-builds/latest/...`` returns ``404 Not Found``
    without a plugin-specific error envelope. The mixin translates that
    status into this exception so the server-tool layer can map it onto a
    structured ``plugin_unavailable`` error (Requirement 3.4) without
    inspecting HTTP status codes from inside the tool function.
    """


class RequiredBuildsMixin(BitbucketClient):
    """Mixin providing required-builds merge-check operations."""

    def _required_builds_condition_url(
        self, project_key: str, repo_slug: str
    ) -> str:
        """Return the collection endpoint URL for a repo's conditions."""
        return (
            f"/rest/required-builds/latest/projects/{project_key}"
            f"/repos/{repo_slug}/condition"
        )

    def list_required_builds(
        self,
        project_key: str,
        repo_slug: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List required-build conditions configured on a repository.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            limit: Page size

        Returns:
            List of required-build condition objects, each shaped like
            ``{"id": int, "buildParentKeys": [...], "refMatcher": {...},
            "exemptionMatcher": {...} | None}``.

        Raises:
            RequiredBuildsPluginUnavailableError: When the required-builds
                plugin endpoint returns HTTP 404 (plugin absent/disabled).
        """
        url = self._required_builds_condition_url(project_key, repo_slug)
        try:
            return self._get_paged_results(url, limit=limit)
        except HTTPError as exc:
            if _is_plugin_unavailable(exc):
                logger.debug(
                    "Bitbucket required-builds plugin endpoint returned 404 "
                    "for %s/%s; treating as plugin_unavailable",
                    project_key,
                    repo_slug,
                )
                raise RequiredBuildsPluginUnavailableError(
                    "Bitbucket required-builds plugin endpoint is "
                    f"unavailable (HTTP 404 from {url})."
                ) from exc
            raise

    def create_required_build(
        self,
        project_key: str,
        repo_slug: str,
        *,
        build_parent_keys: list[str],
        ref_matcher: dict[str, Any],
        exemption_matcher: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a required-build condition on a repository.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            build_parent_keys: One or more build-parent keys (e.g. Bamboo
                plan keys) that must all succeed before a PR can merge
            ref_matcher: Ref matcher describing which branches the
                condition applies to — shaped like
                ``{"id": "refs/heads/main", "type": {"id": "BRANCH"}}``
            exemption_matcher: Optional matcher naming users or groups
                permitted to bypass the gate

        Returns:
            Created condition object.

        Raises:
            RequiredBuildsPluginUnavailableError: When the required-builds
                plugin endpoint returns HTTP 404 (plugin absent/disabled).
        """
        url = self._required_builds_condition_url(project_key, repo_slug)
        data: dict[str, Any] = {
            "buildParentKeys": list(build_parent_keys),
            "refMatcher": ref_matcher,
        }
        if exemption_matcher is not None:
            data["exemptionMatcher"] = exemption_matcher

        try:
            result = self.bitbucket.post(url, data=data)
        except HTTPError as exc:
            if _is_plugin_unavailable(exc):
                logger.debug(
                    "Bitbucket required-builds plugin endpoint returned 404 "
                    "for %s/%s; treating as plugin_unavailable",
                    project_key,
                    repo_slug,
                )
                raise RequiredBuildsPluginUnavailableError(
                    "Bitbucket required-builds plugin endpoint is "
                    f"unavailable (HTTP 404 from {url})."
                ) from exc
            raise

        if not isinstance(result, dict):
            raise ValueError(
                f"Unexpected response creating required-build condition: {result}"
            )
        return result

    def delete_required_build(
        self,
        project_key: str,
        repo_slug: str,
        condition_id: int,
    ) -> None:
        """Delete a required-build condition by id.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            condition_id: Condition id returned by
                :meth:`list_required_builds` or :meth:`create_required_build`

        Raises:
            RequiredBuildsPluginUnavailableError: When the required-builds
                plugin endpoint returns HTTP 404 (plugin absent/disabled).
        """
        url = (
            f"/rest/required-builds/latest/projects/{project_key}"
            f"/repos/{repo_slug}/condition/{condition_id}"
        )
        try:
            self.bitbucket.delete(url)
        except HTTPError as exc:
            if _is_plugin_unavailable(exc):
                logger.debug(
                    "Bitbucket required-builds plugin endpoint returned 404 "
                    "for %s/%s condition %s; treating as plugin_unavailable",
                    project_key,
                    repo_slug,
                    condition_id,
                )
                raise RequiredBuildsPluginUnavailableError(
                    "Bitbucket required-builds plugin endpoint is "
                    f"unavailable (HTTP 404 from {url})."
                ) from exc
            raise


def _is_plugin_unavailable(error: HTTPError) -> bool:
    """Return True when ``error`` represents a 404 from the plugin endpoint.

    The required-builds plugin endpoint returns ``404 Not Found`` with no
    plugin-specific envelope when the plugin is absent or disabled. We
    distinguish that from a 404 on a specific condition id purely by the
    HTTP status: callers that delete an id they've just listed will
    almost always get 2xx or 409 rather than 404, and treating "plugin
    missing" vs. "id missing" identically is acceptable per Requirement
    3.4 since both produce the same user-facing guidance (install /
    enable the plugin, or confirm the condition still exists).
    """
    status = getattr(getattr(error, "response", None), "status_code", None)
    return status == 404
