"""Deployment read operations for Bitbucket Data Center.

Bitbucket Data Center 7.10+ records deployments against a repository
under ``/rest/api/latest/projects/{k}/repos/{r}/deployments``. Each
deployment carries its environment, state, deployment sequence number,
and the commit it deployed. This mixin exposes list and get helpers
used by the ``toolset:bitbucket_deployments`` tool layer.

Only read endpoints are wired here — creating, updating or deleting
deployments is intentionally out of scope per Requirement 11.3.
DC version gating (minimum 7.10) is enforced at the server-tool layer
via ``check_dc_version``.
"""

import logging
from typing import Any

from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket.deployments")


class DeploymentsMixin(BitbucketClient):
    """Mixin providing read-only deployment operations for Bitbucket DC."""

    def list_deployments(
        self,
        project_key: str,
        repo_slug: str,
        *,
        environment: str | None = None,
        state: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """List deployments recorded against a repository.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            environment: Optional environment name to filter by
                (e.g. ``"production"``, ``"staging"``)
            state: Optional deployment state to filter by
                (e.g. ``"SUCCESSFUL"``, ``"FAILED"``, ``"IN_PROGRESS"``,
                ``"PENDING"``, ``"CANCELLED"``, ``"ROLLED_BACK"``,
                ``"UNKNOWN"``)
            limit: Page size

        Returns:
            List of deployment objects, each shaped like
            ``{"key": str, "state": str, "environment": {...},
            "deploymentSequenceNumber": int, "displayName": str,
            "url": str, "fromCommit": {...}, "toCommit": {...},
            "lastUpdated": int}``.
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/deployments"
        )
        params: dict[str, Any] = {}
        if environment:
            params["environment"] = environment
        if state:
            params["state"] = state

        return self._get_paged_results(url, params=params, limit=limit)

    def get_deployment(
        self,
        project_key: str,
        repo_slug: str,
        deployment_id: str,
    ) -> dict[str, Any]:
        """Retrieve a single deployment by id.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            deployment_id: Deployment id (the ``key`` field returned by
                :meth:`list_deployments`)

        Returns:
            Deployment object with the same shape as entries returned by
            :meth:`list_deployments`.
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/deployments/{deployment_id}"
        )
        result = self.bitbucket.get(url)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response fetching deployment: {result}")
        return result
