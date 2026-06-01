"""Module for Jira dashboard read-only operations (DC).

This mixin implements Requirement 16 from the atlassian-dc-tool-parity
feature: read-only listing, retrieval, and search of Jira dashboards
against the Data Center REST endpoints. No write methods are exposed.
"""

import logging
from typing import Any

from .client import JiraClient

logger = logging.getLogger("mcp-jira")


class DashboardsMixin(JiraClient):
    """Mixin for Jira dashboard read-only operations.

    Provides methods for listing, retrieving, and searching Jira
    dashboards via the Data Center REST API under
    ``/rest/api/2/dashboard``.
    """

    def list_dashboards(self, *, limit: int = 25) -> list[dict[str, Any]]:
        """List dashboards visible to the current user.

        Calls ``GET /rest/api/2/dashboard``. The response envelope shapes
        like ``{"startAt", "maxResults", "total", "dashboards": [...]}``
        on both Jira DC and Cloud; this method unwraps the
        ``dashboards`` list.

        Args:
            limit: Maximum number of dashboards to return
                (forwarded as ``maxResults``). Defaults to 25.

        Returns:
            List of dashboard data dictionaries. Empty list on error or
            when the response has an unexpected shape.
        """
        params: dict[str, Any] = {"maxResults": limit}

        try:
            response = self.jira.get("rest/api/2/dashboard", params=params)
        except Exception as e:
            logger.error(f"Error listing dashboards: {str(e)}")
            return []

        if not isinstance(response, dict):
            logger.error(
                f"Unexpected response type from `GET /rest/api/2/dashboard`: "
                f"{type(response).__name__}"
            )
            return []

        dashboards = response.get("dashboards", [])
        return dashboards if isinstance(dashboards, list) else []

    def get_dashboard(self, dashboard_id: str) -> dict[str, Any]:
        """Get a single dashboard by id.

        Calls ``GET /rest/api/2/dashboard/{id}``.

        Args:
            dashboard_id: The dashboard identifier.

        Returns:
            Dashboard data dictionary as returned by Jira.

        Raises:
            ValueError: If the response is not a JSON object.
        """
        try:
            response = self.jira.get(f"rest/api/2/dashboard/{dashboard_id}")
        except Exception as e:
            logger.error(f"Error getting dashboard {dashboard_id}: {str(e)}")
            raise

        if not isinstance(response, dict):
            msg = (
                f"Unexpected response type from "
                f"`GET /rest/api/2/dashboard/{dashboard_id}`: "
                f"{type(response).__name__}"
            )
            logger.error(msg)
            raise ValueError(msg)

        return response

    def search_dashboards(
        self,
        *,
        dashboard_name: str | None = None,
        account_id: str | None = None,
        owner: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Search dashboards by name and/or owner.

        Calls ``GET /rest/api/2/dashboard/search``. The Jira DC variant
        of this endpoint accepts:

        - ``dashboardName`` — substring match on the dashboard name (DC).
        - ``accountId`` — Cloud-style owner identifier.
        - ``owner`` — Server/DC username of the dashboard owner.
        - ``maxResults`` — page size.

        The response is paginated under ``values`` (not ``dashboards``);
        this method unwraps it to a plain list.

        Args:
            dashboard_name: Substring to match on the dashboard name
                (forwarded as ``dashboardName`` per DC naming).
            account_id: Cloud account id of the owner, if known.
            owner: Server/DC username of the owner, if known.
            limit: Maximum number of dashboards to return
                (forwarded as ``maxResults``). Defaults to 25.

        Returns:
            List of dashboard data dictionaries. Empty list on error or
            when the response has an unexpected shape.
        """
        params: dict[str, Any] = {"maxResults": limit}
        if dashboard_name is not None:
            params["dashboardName"] = dashboard_name
        if account_id is not None:
            params["accountId"] = account_id
        if owner is not None:
            params["owner"] = owner

        try:
            response = self.jira.get("rest/api/2/dashboard/search", params=params)
        except Exception as e:
            logger.error(f"Error searching dashboards: {str(e)}")
            return []

        if not isinstance(response, dict):
            logger.error(
                f"Unexpected response type from "
                f"`GET /rest/api/2/dashboard/search`: {type(response).__name__}"
            )
            return []

        values = response.get("values", [])
        return values if isinstance(values, list) else []
