"""Repository label operations for Bitbucket Data Center.

Bitbucket DC exposes repository labels (categorization tags attached
to a repo) under ``/rest/api/latest/projects/{k}/repos/{r}/labels``.
This mixin provides list/add/remove helpers used by the
``toolset:bitbucket_repositories`` tool layer.

The add operation is idempotent from the caller's perspective:
Bitbucket DC returns ``409 Conflict`` when the label is already
attached; the mixin maps that outcome to ``{"already_labeled": True}``
rather than propagating the HTTP error.
"""

import logging
from typing import Any

from requests.exceptions import HTTPError

from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket.repo_labels")


class RepoLabelsMixin(BitbucketClient):
    """Mixin providing repository label operations for Bitbucket DC."""

    def list_repo_labels(
        self,
        project_key: str,
        repo_slug: str,
        limit: int = 100,
    ) -> list[str]:
        """List labels attached to a repository.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            limit: Page size

        Returns:
            List of label names (plain strings). Bitbucket returns
            label objects shaped like ``{"name": "team-a"}``; this
            method flattens them to just the ``name`` field.
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/labels"
        )
        values = self._get_paged_results(url, limit=limit)
        return [
            v["name"]
            for v in values
            if isinstance(v, dict) and isinstance(v.get("name"), str)
        ]

    def add_repo_label(
        self,
        project_key: str,
        repo_slug: str,
        label: str,
    ) -> dict[str, Any]:
        """Attach a label to a repository (idempotent).

        Args:
            project_key: The project key
            repo_slug: The repository slug
            label: Label name to attach

        Returns:
            A dict with an ``already_labeled`` boolean. When Bitbucket
            reports the label is already attached (HTTP 409), the
            method returns ``{"already_labeled": True}``. Otherwise it
            returns ``{"already_labeled": False, "label": label}``.
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}/labels"
        )
        try:
            self.bitbucket.post(url, data={"name": label})
        except HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 409:
                logger.debug(
                    f"Label '{label}' already attached to "
                    f"{project_key}/{repo_slug}; treating as idempotent."
                )
                return {"already_labeled": True}
            raise

        return {"already_labeled": False, "label": label}

    def remove_repo_label(
        self,
        project_key: str,
        repo_slug: str,
        label: str,
    ) -> None:
        """Remove a label from a repository.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            label: Label name to detach
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/labels/{label}"
        )
        self.bitbucket.delete(url)
