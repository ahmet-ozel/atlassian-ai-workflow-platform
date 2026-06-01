"""Branch and tag operations for Bitbucket Data Center and Cloud.

DC paths target ``/rest/api/latest/projects/{key}/repos/{slug}/branches``
and ``/tags`` (and ``/rest/branch-utils/latest/...`` for the legacy
branch-utils delete). Cloud paths target
``/2.0/repositories/{workspace}/{repo_slug}/refs/branches`` and
``/refs/tags`` (Requirements 10.1 - 10.5). The agent-facing method
signatures, parameter names, and return types do not change between
modes; Cloud payloads are passed through :func:`normalize_branch` /
:func:`normalize_tag` so downstream code keeps consuming the DC shape.
"""

import logging
from typing import Any

from .client import BitbucketClient
from .response_normalizer import normalize_branch, normalize_tag

logger = logging.getLogger("mcp-atlassian.bitbucket.branches")


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


class BranchesMixin(BitbucketClient):
    """Mixin providing branch and tag operations for Bitbucket DC and Cloud."""

    def get_branches(
        self,
        project_key: str,
        repo_slug: str,
        filter_text: str | None = None,
        order_by: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """List branches in a repository.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            filter_text: Optional text to filter branches by name
            order_by: Optional ordering (ALPHABETICAL, MODIFICATION)
            limit: Maximum number of results per page

        Returns:
            List of branch objects (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/repositories/{workspace}/{repo_slug}/refs/branches"
            params: dict[str, Any] = {}
            if filter_text:
                # Cloud's filter DSL: ``q=name~"<text>"``
                params["q"] = f'name~"{filter_text}"'
            if order_by:
                # Cloud sort keys differ from DC but the agent-visible
                # parameter name is preserved; map the two DC orderings
                # onto the closest Cloud equivalents. Unknown values are
                # forwarded as-is so Cloud can respond authoritatively.
                cloud_sort = {
                    "ALPHABETICAL": "name",
                    "MODIFICATION": "-target.date",
                }.get(order_by, order_by)
                params["sort"] = cloud_sort
            return self._get_paged_results(
                url, params=params, limit=limit, normalizer=normalize_branch
            )

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/branches"
        params = {}
        if filter_text:
            params["filterText"] = filter_text
        if order_by:
            params["orderBy"] = order_by

        return self._get_paged_results(url, params=params, limit=limit)

    def create_branch(
        self,
        project_key: str,
        repo_slug: str,
        branch_name: str,
        start_point: str,
    ) -> dict[str, Any]:
        """Create a new branch.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            branch_name: Name for the new branch
            start_point: Commit hash or branch name to branch from

        Returns:
            Created branch object (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/repositories/{workspace}/{repo_slug}/refs/branches"
            data = {
                "name": branch_name,
                "target": {"hash": start_point},
            }
            result = self.bitbucket.post(url, data=data)
            if not isinstance(result, dict):
                raise ValueError(f"Unexpected response creating branch: {result}")
            normalized = normalize_branch(result)
            assert normalized is not None
            return normalized

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/branches"
        data = {
            "name": branch_name,
            "startPoint": start_point,
        }

        result = self.bitbucket.post(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response creating branch: {result}")
        return result

    def delete_branch(
        self,
        project_key: str,
        repo_slug: str,
        branch_name: str,
        end_point: str | None = None,
    ) -> bool:
        """Delete a branch.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            branch_name: Name of the branch to delete
            end_point: Optional commit hash (DC safety check). Ignored on
                Cloud because the Cloud delete endpoint does not accept it.

        Returns:
            True if deletion was successful
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/refs/branches/{branch_name}"
            )
            self.bitbucket.delete(url)
            return True

        url = f"/rest/branch-utils/latest/projects/{project_key}/repos/{repo_slug}/branches"
        data: dict[str, Any] = {"name": f"refs/heads/{branch_name}"}
        if end_point:
            data["endPoint"] = end_point

        self.bitbucket.delete(url, data=data)
        return True

    def get_branch_permissions(
        self,
        project_key: str,
        repo_slug: str,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Get branch permissions/restrictions for a repository.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            limit: Maximum number of results per page

        Returns:
            List of branch permission objects
        """
        url = f"/rest/branch-permissions/latest/projects/{project_key}/repos/{repo_slug}/restrictions"
        return self._get_paged_results(url, limit=limit)

    def get_tags(
        self,
        project_key: str,
        repo_slug: str,
        filter_text: str | None = None,
        order_by: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """List tags in a repository.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            filter_text: Optional text to filter tags by name
            order_by: Optional ordering (ALPHABETICAL, MODIFICATION)
            limit: Maximum number of results per page

        Returns:
            List of tag objects (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/repositories/{workspace}/{repo_slug}/refs/tags"
            params: dict[str, Any] = {}
            if filter_text:
                params["q"] = f'name~"{filter_text}"'
            if order_by:
                cloud_sort = {
                    "ALPHABETICAL": "name",
                    "MODIFICATION": "-target.date",
                }.get(order_by, order_by)
                params["sort"] = cloud_sort
            return self._get_paged_results(
                url, params=params, limit=limit, normalizer=normalize_tag
            )

        url = f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/tags"
        params = {}
        if filter_text:
            params["filterText"] = filter_text
        if order_by:
            params["orderBy"] = order_by

        return self._get_paged_results(url, params=params, limit=limit)

    def create_tag(
        self,
        project_key: str,
        repo_slug: str,
        tag_name: str,
        start_point: str,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Create a lightweight or annotated tag.

        When ``message`` is omitted the endpoint creates a lightweight tag.
        Providing ``message`` turns it into an annotated tag.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            tag_name: New tag name (without ``refs/tags/`` prefix)
            start_point: Commit hash, branch or tag to place the tag at
            message: Optional annotation message

        Returns:
            Created tag object (normalized to the DC shape on Cloud).
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = f"/2.0/repositories/{workspace}/{repo_slug}/refs/tags"
            data: dict[str, Any] = {
                "name": tag_name,
                "target": {"hash": start_point},
            }
            if message is not None:
                data["message"] = message
            result = self.bitbucket.post(url, data=data)
            if not isinstance(result, dict):
                raise ValueError(f"Unexpected response creating tag: {result}")
            normalized = normalize_tag(result)
            assert normalized is not None
            return normalized

        url = (
            f"/rest/git/latest/projects/{project_key}/repos/{repo_slug}/tags"
        )
        data = {
            "name": tag_name,
            "startPoint": start_point,
        }
        if message is not None:
            data["message"] = message

        result = self.bitbucket.post(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response creating tag: {result}")
        return result

    def delete_tag(
        self,
        project_key: str,
        repo_slug: str,
        tag_name: str,
    ) -> bool:
        """Delete a tag.

        Args:
            project_key: The project key (DC) or workspace slug (Cloud)
            repo_slug: The repository slug
            tag_name: Tag name to remove

        Returns:
            True on successful deletion.
        """
        if self.is_cloud:
            workspace = _resolve_workspace(project_key, self.config.workspace)
            url = (
                f"/2.0/repositories/{workspace}/{repo_slug}"
                f"/refs/tags/{tag_name}"
            )
            self.bitbucket.delete(url)
            return True

        url = (
            f"/rest/git/latest/projects/{project_key}/repos/{repo_slug}"
            f"/tags/{tag_name}"
        )
        self.bitbucket.delete(url)
        return True

    def list_branch_restrictions(
        self,
        project_key: str,
        repo_slug: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List branch permission restrictions (read-only).

        Writing/deleting restrictions is deliberately NOT exposed — it can
        lock contributors out of a repository and should be managed by
        admins through the UI.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            limit: Page size

        Returns:
            List of restriction objects (``type``, ``matcher``, ``users``,
            ``groups``, ``accessKeys``).
        """
        url = (
            f"/rest/branch-permissions/latest/projects/{project_key}"
            f"/repos/{repo_slug}/restrictions"
        )
        return self._get_paged_results(url, limit=limit)
