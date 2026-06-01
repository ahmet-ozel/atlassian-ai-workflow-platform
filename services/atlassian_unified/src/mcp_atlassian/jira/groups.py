"""Module for Jira group read-only operations (DC).

This mixin implements Requirement 22 from the atlassian-dc-tool-parity
feature: read-only lookup of a user's groups and of groups on the
Data Center instance. No add/remove/grant methods are exposed, per
Requirement 22.2 and 22.3.
"""

import logging
from typing import Any

from .client import JiraClient

logger = logging.getLogger("mcp-jira")


class GroupsMixin(JiraClient):
    """Mixin for Jira group read-only operations.

    Provides methods for looking up the groups a user belongs to and for
    searching groups on the instance via the Data Center REST endpoints
    under ``/rest/api/2/user/groups`` and ``/rest/api/2/groups/picker``.

    Intentionally read-only — there are no ``add_user_to_group``,
    ``remove_user_from_group``, or permission-grant methods here.
    """

    def get_user_groups(
        self,
        *,
        username: str | None = None,
        account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return the groups a user belongs to.

        Calls ``GET /rest/api/2/user/groups``. On Jira Data Center the
        lookup key is ``username`` (DC username, case-insensitive); on
        Jira Cloud the same endpoint accepts ``accountId`` instead.
        Callers should supply whichever identifier is available for
        their deployment; when both are given, both are forwarded and
        the server picks the one it understands.

        The response is a plain JSON array of group objects shaped like
        ``[{"name": "jira-users", "self": "..."}, ...]`` on both Cloud
        and Server/DC; this method returns it verbatim.

        Args:
            username: DC username of the user (Server/DC lookup key).
            account_id: Cloud account id of the user (Cloud lookup key).

        Returns:
            List of group data dictionaries. Empty list on error or
            when the response has an unexpected shape.
        """
        params: dict[str, Any] = {}
        if username is not None:
            params["username"] = username
        if account_id is not None:
            params["accountId"] = account_id

        try:
            response = self.jira.get("rest/api/2/user/groups", params=params)
        except Exception as e:
            logger.error(f"Error getting user groups: {str(e)}")
            return []

        if not isinstance(response, list):
            logger.error(
                f"Unexpected response type from "
                f"`GET /rest/api/2/user/groups`: {type(response).__name__}"
            )
            return []

        return response

    def list_groups(
        self,
        *,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Search groups on the instance.

        Calls ``GET /rest/api/2/groups/picker``. The endpoint accepts a
        substring ``query`` on the group name and a ``maxResults`` page
        size. The response envelope shapes like
        ``{"header": "...", "total": N, "groups": [...]}`` on both
        Jira DC and Cloud; this method unwraps the ``groups`` list.

        Args:
            query: Substring to match on the group name (forwarded as
                ``query``). When ``None``, omitted from the request so
                the server returns the default picker listing.
            limit: Maximum number of groups to return (forwarded as
                ``maxResults``). Defaults to 50.

        Returns:
            List of group data dictionaries. Empty list on error or
            when the response has an unexpected shape.
        """
        params: dict[str, Any] = {"maxResults": limit}
        if query is not None:
            params["query"] = query

        try:
            response = self.jira.get("rest/api/2/groups/picker", params=params)
        except Exception as e:
            logger.error(f"Error listing groups: {str(e)}")
            return []

        if not isinstance(response, dict):
            logger.error(
                f"Unexpected response type from "
                f"`GET /rest/api/2/groups/picker`: {type(response).__name__}"
            )
            return []

        groups = response.get("groups", [])
        return groups if isinstance(groups, list) else []
