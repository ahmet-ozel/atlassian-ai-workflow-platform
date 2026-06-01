"""Module for Jira @mention suggestion lookups (DC, read-only).

This mixin implements Requirement 23 from the atlassian-dc-tool-parity
feature: resolve a free-text fragment into a list of user-suggestion
candidates suitable for an ``@mention`` in a comment. The operation is a
pure GET against the DC user picker endpoint and performs no writes.

Design notes:

* The upstream endpoint is ``/rest/api/2/user/picker`` — the simplest DC
  user suggestion endpoint. The picker accepts an optional ``issueKey``
  that Jira DC uses to bias the suggestion ordering toward users who
  have interacted with the referenced issue.
* Per Requirement 23.2, an empty or whitespace-only ``query`` MUST
  short-circuit to ``[]`` *before* any outbound HTTP call. This avoids
  the DC picker returning a large default list and, more importantly,
  makes the empty-query contract verifiable by the property test
  (``tests/unit/properties/test_mention_empty_query_property.py``).
"""

import logging
from typing import Any

from requests.exceptions import HTTPError

from ..utils.decorators import handle_auth_errors
from .client import JiraClient

logger = logging.getLogger("mcp-jira")


class MentionsMixin(JiraClient):
    """Mixin for Jira @mention suggestion lookups (read-only).

    Backed by ``GET /rest/api/2/user/picker``. The response envelope
    shapes like ``{"users": [...], "total": N, "header": "..."}`` on
    both Jira DC and Cloud; this method unwraps the ``users`` list.

    Intentionally read-only — no mention-creation or user-write methods
    are exposed here (those live on :class:`CommentsMixin`).
    """

    @handle_auth_errors("Jira API")
    def get_mention_suggestions(
        self,
        query: str,
        *,
        issue_key: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return @mention suggestions matching ``query``.

        Short-circuits to an empty list when ``query`` is empty or
        whitespace-only so no HTTP call is issued (Requirement 23.2).

        Args:
            query: Free-text fragment to match against user name /
                display-name / email. An empty or whitespace-only value
                causes this method to return ``[]`` immediately without
                contacting Jira.
            issue_key: Optional issue key used by Jira DC to bias the
                suggestion ordering toward users who have interacted
                with the referenced issue. Forwarded as ``issueKey``
                when provided.
            limit: Maximum number of suggestions to return. Forwarded
                as ``maxResults``. Defaults to 10.

        Returns:
            List of user-suggestion dictionaries as returned by the
            picker (``name``, ``displayName``, ``avatarUrl``, ``html``
            on DC; ``accountId``, ``displayName``, ``avatarUrls`` on
            Cloud). Returns ``[]`` on empty query, on error, or when
            the response has an unexpected shape.
        """
        # Requirement 23.2: empty / whitespace-only query short-circuits
        # BEFORE any outbound HTTP call.
        if not query or not query.strip():
            return []

        params: dict[str, Any] = {
            "query": query,
            "maxResults": limit,
        }
        if issue_key is not None:
            params["issueKey"] = issue_key

        try:
            response = self.jira.get("rest/api/2/user/picker", params=params)
        except HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error fetching mention suggestions: {e}", exc_info=True)
            return []

        if not isinstance(response, dict):
            logger.error(
                f"Unexpected response type from "
                f"`GET /rest/api/2/user/picker`: {type(response).__name__}"
            )
            return []

        users = response.get("users", [])
        return users if isinstance(users, list) else []
