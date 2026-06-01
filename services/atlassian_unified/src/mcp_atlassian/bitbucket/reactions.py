"""Pull-request comment reactions for Bitbucket Data Center (DC 8.8+)."""

import logging
from typing import Any

from .client import BitbucketClient

logger = logging.getLogger("mcp-atlassian.bitbucket.reactions")


class ReactionsMixin(BitbucketClient):
    """Mixin providing pull-request comment reaction operations.

    Requires Bitbucket Data Center 8.8 or newer. Version gating is enforced
    at the server layer via :func:`mcp_atlassian.utils.dc_guards.check_dc_version`
    before these methods are invoked.
    """

    def add_pr_comment_reaction(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        comment_id: int,
        emoji: str,
    ) -> dict[str, Any]:
        """Add an emoji reaction to a pull-request comment.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            pr_id: The pull request id
            comment_id: The target comment id
            emoji: The emoji shortcode (for example ``"+1"``, ``"smile"``)

        Returns:
            Reaction object as returned by Bitbucket DC.
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/comments/{comment_id}/reactions/{emoji}"
        )

        result = self.bitbucket.post(url)
        if not isinstance(result, dict):
            # Some DC versions return an empty body on success; normalize.
            return {"emoji": emoji, "comment_id": comment_id}
        return result

    def remove_pr_comment_reaction(
        self,
        project_key: str,
        repo_slug: str,
        pr_id: int,
        comment_id: int,
        emoji: str,
    ) -> None:
        """Remove the authenticated user's emoji reaction from a PR comment.

        Args:
            project_key: The project key
            repo_slug: The repository slug
            pr_id: The pull request id
            comment_id: The target comment id
            emoji: The emoji shortcode to remove
        """
        url = (
            f"/rest/api/latest/projects/{project_key}/repos/{repo_slug}"
            f"/pull-requests/{pr_id}/comments/{comment_id}/reactions/{emoji}"
        )
        self.bitbucket.delete(url)
