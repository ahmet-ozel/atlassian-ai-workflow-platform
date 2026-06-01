"""Pull request participant read operations for Bitbucket Data Center.

The DC REST surface already exposes reviewer and participant information
inline on the pull-request payload, but an agent often only needs the
participant slice (role, approval status, last-reviewed-commit) and
paying the cost of fetching the full PR body just to extract it is
wasteful. The ``/participants`` sub-resource returns exactly that slice
and supports DC pagination, which is what this mixin wraps.
"""

import logging
from typing import Any

from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket.pr_participants")


class PRParticipantsMixin(BitbucketClient):
    """Mixin providing pull request participant read operations for Bitbucket DC."""

    def list_pr_participants(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List participants of a pull request.

        Returns reviewers and other participants with their role
        (``AUTHOR``, ``REVIEWER``, ``PARTICIPANT``), approval status
        (``approved``, ``status``) and the ``lastReviewedCommit`` hash
        when the participant has reviewed at least once. Walks DC's
        paged response so callers get the full list in one call.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            pr_id: The pull request ID
            limit: Maximum number of results per page

        Returns:
            List of participant objects. Each entry mirrors the DC
            payload: ``{"user": {...}, "role": ..., "approved": bool,
            "status": ..., "lastReviewedCommit": ...}``.
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/participants"
        )
        return self._get_paged_results(url, limit=limit)
