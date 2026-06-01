"""Module for Jira issue vote operations (DC).

Implements Requirement 18 from the ``atlassian-dc-tool-parity`` feature:
read, add, and remove the authenticated user's vote on a Jira issue.

All three methods target the DC REST endpoint
``/rest/api/2/issue/{issueIdOrKey}/votes``. To give callers deterministic
``already_voted`` / ``not_voted`` flags even though the Jira POST/DELETE
responses are empty, the mutators first issue a GET to capture
``hasVoted`` and then perform the mutation. The flag returned reflects the
state observed *before* the mutation so the caller can react
idempotently.
"""

import logging
from typing import Any

from requests.exceptions import HTTPError

from ..utils.decorators import handle_auth_errors
from .client import JiraClient

logger = logging.getLogger("mcp-jira")


class VotesMixin(JiraClient):
    """Mixin for Jira issue vote operations (read + idempotent write).

    Provides read access to vote metadata plus idempotent ``add`` and
    ``remove`` operations that expose a pre-state flag so the server
    layer can satisfy Requirement 18.3 (``already_voted``) and
    Requirement 18.4 (``not_voted``) without raising on a no-op.
    """

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _get_votes_raw(self, issue_key: str) -> dict[str, Any]:
        """GET ``/rest/api/2/issue/{key}/votes`` and return the raw payload.

        Args:
            issue_key: The issue key or id (for example ``PROJ-123``).

        Returns:
            The raw Jira response dictionary.

        Raises:
            HTTPError: Propagated from the upstream call (for example 404
                on an unknown issue key).
            ValueError: If the response shape is not a JSON object.
        """
        response = self.jira.get(f"rest/api/2/issue/{issue_key}/votes")
        if not isinstance(response, dict):
            msg = (
                f"Unexpected response type from "
                f"`GET /rest/api/2/issue/{issue_key}/votes`: "
                f"{type(response).__name__}"
            )
            logger.error(msg)
            raise ValueError(msg)
        return response

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------
    @handle_auth_errors("Jira API")
    def get_issue_votes(self, issue_key: str) -> dict[str, Any]:
        """Return vote metadata for an issue.

        Calls ``GET /rest/api/2/issue/{issueIdOrKey}/votes``. The Jira
        response includes at minimum ``votes`` (count) and ``hasVoted``
        (whether the authenticated user has voted).

        Args:
            issue_key: The issue key or id (for example ``PROJ-123``).

        Returns:
            Dictionary with ``issue_key``, ``votes`` (int), ``has_voted``
            (bool), and ``voters`` (list) when the upstream response
            includes voter details.

        Raises:
            MCPAtlassianAuthenticationError: If authentication fails
                (401/403).
            HTTPError: For other HTTP errors from Jira.
        """
        try:
            raw = self._get_votes_raw(issue_key)
        except HTTPError:
            raise
        except Exception as e:
            logger.error(
                f"Error fetching votes for issue {issue_key}: {e}",
                exc_info=True,
            )
            raise

        voters_raw = raw.get("voters", [])
        voters = voters_raw if isinstance(voters_raw, list) else []

        return {
            "issue_key": issue_key,
            "votes": int(raw.get("votes", 0) or 0),
            "has_voted": bool(raw.get("hasVoted", False)),
            "voters": voters,
        }

    @handle_auth_errors("Jira API")
    def add_issue_vote(self, issue_key: str) -> dict[str, Any]:
        """Cast the authenticated user's vote on an issue (idempotent).

        Calls ``POST /rest/api/2/issue/{issueIdOrKey}/votes``. Before
        posting, issues a GET to capture the current ``hasVoted`` state;
        the returned ``already_voted`` flag reflects that pre-state so a
        repeated invocation is a no-op from the caller's perspective
        (Requirement 18.3).

        Args:
            issue_key: The issue key or id (for example ``PROJ-123``).

        Returns:
            Dictionary with ``issue_key``, ``already_voted`` (bool), and
            ``votes`` (the post-operation vote count).

        Raises:
            MCPAtlassianAuthenticationError: If authentication fails
                (401/403).
            HTTPError: For other HTTP errors from Jira (for example 404
                when the issue does not exist, or 204 downgraded upstream
                errors).
        """
        # 1. Capture pre-state so ``already_voted`` is deterministic.
        pre = self._get_votes_raw(issue_key)
        already_voted = bool(pre.get("hasVoted", False))

        # 2. POST is idempotent on Jira DC: if the user has already voted,
        # the server still returns 204 No Content without error. We still
        # issue the call so the upstream is the authoritative source of
        # success/failure semantics.
        try:
            self.jira.post(f"rest/api/2/issue/{issue_key}/votes")
        except HTTPError:
            raise
        except Exception as e:
            logger.error(
                f"Error adding vote for issue {issue_key}: {e}",
                exc_info=True,
            )
            raise

        # 3. Return the post-state vote count for convenience.
        post = self._get_votes_raw(issue_key)
        return {
            "issue_key": issue_key,
            "already_voted": already_voted,
            "votes": int(post.get("votes", 0) or 0),
        }

    @handle_auth_errors("Jira API")
    def remove_issue_vote(self, issue_key: str) -> dict[str, Any]:
        """Retract the authenticated user's vote on an issue (idempotent).

        Calls ``DELETE /rest/api/2/issue/{issueIdOrKey}/votes``. Before
        deleting, issues a GET to capture the current ``hasVoted`` state;
        the returned ``not_voted`` flag is ``True`` when the user had
        not voted before the call (Requirement 18.4) so a repeated
        invocation is a no-op from the caller's perspective.

        Args:
            issue_key: The issue key or id (for example ``PROJ-123``).

        Returns:
            Dictionary with ``issue_key``, ``not_voted`` (bool), and
            ``votes`` (the post-operation vote count).

        Raises:
            MCPAtlassianAuthenticationError: If authentication fails
                (401/403).
            HTTPError: For other HTTP errors from Jira.
        """
        # 1. Capture pre-state so ``not_voted`` is deterministic.
        pre = self._get_votes_raw(issue_key)
        was_voted_before = bool(pre.get("hasVoted", False))
        not_voted = not was_voted_before

        # 2. DELETE is idempotent on Jira DC: if the user has not voted,
        # the server returns 404 on some versions and 204 on others. We
        # tolerate 404 as a no-op so the tool stays idempotent for the
        # caller; every other HTTP error is re-raised unchanged.
        try:
            self.jira.delete(f"rest/api/2/issue/{issue_key}/votes")
        except HTTPError as http_err:
            status = getattr(getattr(http_err, "response", None), "status_code", None)
            if status == 404 and not_voted:
                # Expected: user had no vote to remove. Treat as no-op.
                logger.debug(
                    "DELETE votes returned 404 for %s; treating as no-op "
                    "(user had not voted).",
                    issue_key,
                )
            else:
                raise
        except Exception as e:
            logger.error(
                f"Error removing vote for issue {issue_key}: {e}",
                exc_info=True,
            )
            raise

        # 3. Return the post-state vote count for convenience.
        post = self._get_votes_raw(issue_key)
        return {
            "issue_key": issue_key,
            "not_voted": not_voted,
            "votes": int(post.get("votes", 0) or 0),
        }
