"""Module for Jira project roles read-only operations (DC).

This mixin implements Requirement 24 from the atlassian-dc-tool-parity
feature: read-only listing of project roles and retrieval of role actors
against the Data Center REST endpoints. No write methods are exposed,
per Requirement 24.2.
"""

import logging
from typing import Any

from .client import JiraClient

logger = logging.getLogger("mcp-jira")


class ProjectRolesMixin(JiraClient):
    """Mixin for Jira project role read-only operations.

    Provides methods for listing project roles and retrieving role
    actors via the Data Center REST endpoints under
    ``/rest/api/2/project/{projectIdOrKey}/role`` and
    ``/rest/api/2/project/{projectIdOrKey}/role/{roleId}``.

    Intentionally read-only — there are no methods that add or remove
    actors from a role, or that create or delete roles themselves.
    """

    def list_project_roles(self, project_key: str) -> dict[str, str]:
        """List all roles defined for a project.

        Calls ``GET /rest/api/2/project/{projectIdOrKey}/role``. Jira
        returns a JSON object mapping each role name to the self URL
        for that role, shaped like
        ``{"Administrators": "https://.../role/10002", ...}`` on both
        Jira DC and Cloud; this method returns it verbatim.

        Args:
            project_key: The project key (e.g. ``"PROJ"``) or numeric
                project id.

        Returns:
            Dictionary mapping role name to role self URL. Empty dict
            on error or when the response has an unexpected shape.
        """
        try:
            response = self.jira.get(f"rest/api/2/project/{project_key}/role")
        except Exception as e:
            logger.error(
                f"Error listing project roles for {project_key}: {str(e)}"
            )
            return {}

        if not isinstance(response, dict):
            logger.error(
                f"Unexpected response type from "
                f"`GET /rest/api/2/project/{project_key}/role`: "
                f"{type(response).__name__}"
            )
            return {}

        # Coerce non-string values defensively; Jira only returns strings
        # here, but we guard against malformed payloads rather than
        # propagating them.
        return {
            name: url
            for name, url in response.items()
            if isinstance(name, str) and isinstance(url, str)
        }

    def get_project_role_actors(
        self, project_key: str, role_id: str
    ) -> dict[str, Any]:
        """Get a single project role with its assigned actors.

        Calls ``GET /rest/api/2/project/{projectIdOrKey}/role/{roleId}``.
        Jira returns a role object containing the role ``id``, ``name``,
        ``description``, ``self`` URL, and an ``actors`` array of user
        and group assignments. This method returns the payload verbatim.

        Args:
            project_key: The project key (e.g. ``"PROJ"``) or numeric
                project id.
            role_id: The role identifier (as returned by
                :meth:`list_project_roles` parsed from the self URL).

        Returns:
            Role data dictionary including the ``actors`` array. Empty
            dict on error or when the response has an unexpected shape.
        """
        try:
            response = self.jira.get(
                f"rest/api/2/project/{project_key}/role/{role_id}"
            )
        except Exception as e:
            logger.error(
                f"Error getting project role actors for "
                f"{project_key}/{role_id}: {str(e)}"
            )
            return {}

        if not isinstance(response, dict):
            logger.error(
                f"Unexpected response type from "
                f"`GET /rest/api/2/project/{project_key}/role/{role_id}`: "
                f"{type(response).__name__}"
            )
            return {}

        return response
