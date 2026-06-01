"""Pull-request task operations for Bitbucket Data Center.

Tasks in DC 7.x+ are implemented as a flavour of blocker comments on
a pull request: each task is a comment whose ``severity`` is
``BLOCKER`` and whose ``state`` is ``OPEN`` or ``RESOLVED``. The
dedicated ``/blocker-comments`` endpoint exposes convenient CRUD and
state-transition operations so reviewers can track action items on
a PR.
"""

import logging
from typing import Any

from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket.pr_tasks")


class PullRequestTasksMixin(BitbucketClient):
    """Mixin providing PR task (blocker comment) operations."""

    def list_pr_tasks(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        state: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List tasks (blocker comments) on a pull request.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            pr_id: The pull request ID
            state: Optional filter — ``OPEN`` or ``RESOLVED``
            limit: Page size

        Returns:
            List of task objects.
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/blocker-comments"
        )
        params: dict[str, Any] = {}
        if state:
            params["state"] = state
        return self._get_paged_results(url, params=params, limit=limit)

    def get_pr_task(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        task_id: int,
    ) -> dict[str, Any]:
        """Fetch a single task by ID.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            pr_id: The pull request ID
            task_id: Task (blocker comment) ID

        Returns:
            Task object.
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/blocker-comments/{task_id}"
        )
        result = self.bitbucket.get(url)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response for task {task_id}: {result}")
        return result

    def create_pr_task(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        text: str,
        anchor: dict[str, Any] | None = None,
        parent_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a PR task (blocker comment).

        When ``anchor`` is provided the task becomes an inline task on the
        referenced file/line. Otherwise it is a top-level PR task.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            pr_id: The pull request ID
            text: Task description
            anchor: Optional inline anchor (``path``, ``line``,
                ``lineType``, ``fileType``)
            parent_id: Optional parent comment ID to create the task as a
                reply under an existing discussion

        Returns:
            Created task object.
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/blocker-comments"
        )
        data: dict[str, Any] = {
            "text": text,
            "severity": "BLOCKER",
            "state": "OPEN",
        }
        if parent_id is not None:
            data["parent"] = {"id": parent_id}
        if anchor:
            data["anchor"] = anchor

        result = self.bitbucket.post(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response creating task: {result}")
        return result

    def update_pr_task(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        task_id: int,
        version: int,
        text: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        """Edit a task's text or transition its state.

        Use ``state="RESOLVED"`` to mark a task done, ``state="OPEN"`` to
        reopen it. Version is required for optimistic locking.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            pr_id: The pull request ID
            task_id: Task ID
            version: Current task version
            text: Optional new text (omit to keep)
            state: Optional new state — ``OPEN`` or ``RESOLVED``

        Returns:
            Updated task object.
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/blocker-comments/{task_id}"
        )
        data: dict[str, Any] = {"version": version}
        if text is not None:
            data["text"] = text
        if state is not None:
            data["state"] = state

        result = self.bitbucket.put(url, data=data)
        if not isinstance(result, dict):
            raise ValueError(f"Unexpected response updating task {task_id}: {result}")
        return result

    def delete_pr_task(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        task_id: int,
        version: int,
    ) -> bool:
        """Delete a PR task.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            pr_id: The pull request ID
            task_id: Task ID
            version: Current task version

        Returns:
            True on successful deletion.
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/blocker-comments/{task_id}"
        )
        self.bitbucket.delete(url, params={"version": version})
        return True

    def resolve_pr_task(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        task_id: int,
        version: int,
    ) -> dict[str, Any]:
        """Convenience wrapper: mark a task ``RESOLVED``."""
        return self.update_pr_task(
            project_key,
            repo_slug,
            pr_id,
            task_id,
            version=version,
            state="RESOLVED",
        )

    def reopen_pr_task(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        task_id: int,
        version: int,
    ) -> dict[str, Any]:
        """Convenience wrapper: move a task back to ``OPEN``."""
        return self.update_pr_task(
            project_key,
            repo_slug,
            pr_id,
            task_id,
            version=version,
            state="OPEN",
        )
