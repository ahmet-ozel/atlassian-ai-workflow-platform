"""Module for Jira my-permissions read-only operations (DC).

This mixin implements Requirement 20 from the atlassian-dc-tool-parity
feature: a read-only lookup of the permissions the authenticated user
holds for a given issue against the Jira Data Center REST endpoint
``/rest/api/2/mypermissions``. No write methods are exposed.
"""

import logging
from typing import Any

from .client import JiraClient

logger = logging.getLogger("mcp-jira")


class PermissionsMixin(JiraClient):
    """Mixin for Jira my-permissions read-only operations.

    Provides a single method for retrieving the set of permissions the
    authenticated user has for a specific issue via
    ``GET /rest/api/2/mypermissions``.
    """

    def get_my_issue_permissions(
        self,
        issue_key: str,
        *,
        permission_keys: list[str] | None = None,
    ) -> dict[str, bool]:
        """Get the authenticated user's permissions for a specific issue.

        Calls ``GET /rest/api/2/mypermissions?issueKey={issue_key}`` and
        flattens the response into a plain ``{permission_key: bool}``
        mapping. The raw Jira payload is shaped like::

            {
                "permissions": {
                    "BROWSE_PROJECTS": {
                        "id": "10",
                        "key": "BROWSE_PROJECTS",
                        "name": "Browse Projects",
                        "type": "PROJECT",
                        "havePermission": true,
                        ...
                    },
                    "CREATE_ISSUES": {"havePermission": false, ...},
                    ...
                }
            }

        and is condensed to::

            {"BROWSE_PROJECTS": true, "CREATE_ISSUES": false, ...}

        Args:
            issue_key: The Jira issue key to check permissions against
                (for example ``"PROJ-123"``). Forwarded as the
                ``issueKey`` query parameter.
            permission_keys: Optional list of permission keys to
                restrict the query to. When provided, the values are
                joined with commas and forwarded as the ``permissions``
                query parameter so Jira only returns those entries.
                When ``None`` or empty, Jira returns the full permission
                set visible to the user.

        Returns:
            Mapping of permission key to a boolean indicating whether
            the authenticated user has that permission for the given
            issue. Empty dict on error or when the response has an
            unexpected shape.
        """
        params: dict[str, Any] = {"issueKey": issue_key}
        if permission_keys:
            params["permissions"] = ",".join(permission_keys)

        try:
            response = self.jira.get("rest/api/2/mypermissions", params=params)
        except Exception as e:
            logger.error(
                f"Error getting my-permissions for issue {issue_key}: {str(e)}"
            )
            return {}

        if not isinstance(response, dict):
            logger.error(
                f"Unexpected response type from "
                f"`GET /rest/api/2/mypermissions`: {type(response).__name__}"
            )
            return {}

        permissions = response.get("permissions", {})
        if not isinstance(permissions, dict):
            logger.error(
                f"Unexpected 'permissions' field type in my-permissions "
                f"response: {type(permissions).__name__}"
            )
            return {}

        flattened: dict[str, bool] = {}
        for key, entry in permissions.items():
            if isinstance(entry, dict):
                flattened[key] = bool(entry.get("havePermission", False))
            else:
                logger.debug(
                    f"Skipping non-dict permission entry for key '{key}' "
                    f"(got {type(entry).__name__})"
                )
                flattened[key] = False

        return flattened
