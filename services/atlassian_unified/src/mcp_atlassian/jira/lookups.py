"""Module for Jira lookup (read-only) operations.

Exposes the static reference lookups an agent needs when supplying valid field
values to other tools: priorities, resolutions, statuses, and issue types.
Every method targets a DC REST endpoint under ``/rest/api/2/`` and returns
the raw JSON array as received from Jira.
"""

import logging
from typing import Any

from requests.exceptions import HTTPError

from ..utils.decorators import handle_auth_errors
from .client import JiraClient

logger = logging.getLogger("mcp-jira")


class LookupsMixin(JiraClient):
    """Mixin for Jira static lookup operations.

    All methods are read-only GETs and return the raw JSON array exactly as
    Jira DC returns it. The server layer is responsible for any shaping or
    redaction; this mixin performs no pre- or post-processing beyond type
    coercion for safety.

    These endpoints are not project-scoped, so the server-layer project
    filter precheck is intentionally skipped for the tools that wrap them
    (see Requirement 19).
    """

    @handle_auth_errors("Jira API")
    def list_priorities(self) -> list[dict[str, Any]]:
        """List all available issue priorities.

        Returns:
            List of priority objects as returned by
            ``GET /rest/api/2/priority``.

        Raises:
            MCPAtlassianAuthenticationError: If authentication fails (401/403).
            HTTPError: For other HTTP errors from Jira.
        """
        try:
            response = self.jira.get("rest/api/2/priority")
        except HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error listing Jira priorities: {e}", exc_info=True)
            raise
        return response if isinstance(response, list) else []

    @handle_auth_errors("Jira API")
    def list_resolutions(self) -> list[dict[str, Any]]:
        """List all available issue resolutions.

        Returns:
            List of resolution objects as returned by
            ``GET /rest/api/2/resolution``.

        Raises:
            MCPAtlassianAuthenticationError: If authentication fails (401/403).
            HTTPError: For other HTTP errors from Jira.
        """
        try:
            response = self.jira.get("rest/api/2/resolution")
        except HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error listing Jira resolutions: {e}", exc_info=True)
            raise
        return response if isinstance(response, list) else []

    @handle_auth_errors("Jira API")
    def list_statuses(self) -> list[dict[str, Any]]:
        """List all available issue statuses.

        Returns:
            List of status objects as returned by
            ``GET /rest/api/2/status``.

        Raises:
            MCPAtlassianAuthenticationError: If authentication fails (401/403).
            HTTPError: For other HTTP errors from Jira.
        """
        try:
            response = self.jira.get("rest/api/2/status")
        except HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error listing Jira statuses: {e}", exc_info=True)
            raise
        return response if isinstance(response, list) else []

    @handle_auth_errors("Jira API")
    def list_issue_types(self) -> list[dict[str, Any]]:
        """List all available issue types.

        Returns:
            List of issue type objects as returned by
            ``GET /rest/api/2/issuetype``.

        Raises:
            MCPAtlassianAuthenticationError: If authentication fails (401/403).
            HTTPError: For other HTTP errors from Jira.
        """
        try:
            response = self.jira.get("rest/api/2/issuetype")
        except HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error listing Jira issue types: {e}", exc_info=True)
            raise
        return response if isinstance(response, list) else []
