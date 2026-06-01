"""Module for Jira filter operations (DC).

This mixin implements Requirement 15 from the atlassian-dc-tool-parity
feature: filter CRUD plus owner-resolution helper used by the
owner-scoped delete path. The mixin exposes only raw data access — the
server layer in ``servers/jira.py`` is responsible for pairing
``get_filter_owner_name`` with ``require_owner`` before issuing any
DELETE, so mixin callers never have to implement the ownership rule
themselves.

All methods target the Jira DC REST v2 filter endpoints under
``/rest/api/2/filter``. No Cloud-only surface (JQL-with-values, ARI
identifiers, sharing permissions DSL) is used here.
"""

import logging
from typing import Any

from .client import JiraClient

logger = logging.getLogger("mcp-jira")


class FiltersMixin(JiraClient):
    """Mixin for Jira filter operations (list, get, search, CRUD, owner).

    Endpoints:
        - ``GET  /rest/api/2/filter/my``
        - ``GET  /rest/api/2/filter/{filter_id}``
        - ``GET  /rest/api/2/filter/search``
        - ``POST /rest/api/2/filter``
        - ``PUT  /rest/api/2/filter/{filter_id}``
        - ``DELETE /rest/api/2/filter/{filter_id}``
    """

    def list_my_filters(
        self, *, include_favourites: bool = True
    ) -> list[dict[str, Any]]:
        """List filters owned by the authenticated user.

        Calls ``GET /rest/api/2/filter/my``. The DC endpoint returns a
        flat JSON array of filter objects. When
        ``include_favourites`` is ``True`` the caller's favourites are
        merged into the same response by Jira itself via the
        ``includeFavourites`` query flag.

        Args:
            include_favourites: Whether to include the user's favourite
                filters in the response (forwarded as
                ``includeFavourites``). Defaults to ``True``.

        Returns:
            List of filter data dictionaries. Empty list on error or
            when the response shape is unexpected.
        """
        params: dict[str, Any] = {
            "includeFavourites": "true" if include_favourites else "false",
        }

        try:
            response = self.jira.get("rest/api/2/filter/my", params=params)
        except Exception as e:
            logger.error(f"Error listing my filters: {str(e)}")
            return []

        if isinstance(response, list):
            return response

        logger.error(
            f"Unexpected response type from `GET /rest/api/2/filter/my`: "
            f"{type(response).__name__}"
        )
        return []

    def get_filter(self, filter_id: str) -> dict[str, Any]:
        """Fetch a single filter by id.

        Calls ``GET /rest/api/2/filter/{filter_id}``.

        Args:
            filter_id: The filter identifier.

        Returns:
            Filter data dictionary as returned by Jira.

        Raises:
            ValueError: If the response is not a JSON object.
        """
        try:
            response = self.jira.get(f"rest/api/2/filter/{filter_id}")
        except Exception as e:
            logger.error(f"Error getting filter {filter_id}: {str(e)}")
            raise

        if not isinstance(response, dict):
            msg = (
                f"Unexpected response type from "
                f"`GET /rest/api/2/filter/{filter_id}`: "
                f"{type(response).__name__}"
            )
            logger.error(msg)
            raise ValueError(msg)

        return response

    def search_filters(
        self,
        *,
        filter_name: str | None = None,
        account_id: str | None = None,
        owner: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Search filters by name and/or owner.

        Calls ``GET /rest/api/2/filter/search``. Parameters map to Jira
        DC query string names:

        - ``filterName`` — substring match on the filter name.
        - ``accountId`` — Cloud-style owner identifier (accepted by DC
          for forward-compat, generally unused).
        - ``owner`` — Server/DC username of the filter owner.
        - ``maxResults`` — page size.

        The response is paginated under ``values``; this method unwraps
        it to a plain list.

        Args:
            filter_name: Substring to match on the filter name
                (forwarded as ``filterName``).
            account_id: Cloud account id of the owner, if known.
            owner: Server/DC username of the owner, if known.
            limit: Maximum number of filters to return (forwarded as
                ``maxResults``). Defaults to 25.

        Returns:
            List of filter data dictionaries. Empty list on error or
            when the response shape is unexpected.
        """
        params: dict[str, Any] = {"maxResults": limit}
        if filter_name is not None:
            params["filterName"] = filter_name
        if account_id is not None:
            params["accountId"] = account_id
        if owner is not None:
            params["owner"] = owner

        try:
            response = self.jira.get("rest/api/2/filter/search", params=params)
        except Exception as e:
            logger.error(f"Error searching filters: {str(e)}")
            return []

        if not isinstance(response, dict):
            logger.error(
                f"Unexpected response type from "
                f"`GET /rest/api/2/filter/search`: {type(response).__name__}"
            )
            return []

        values = response.get("values", [])
        return values if isinstance(values, list) else []

    def create_filter(
        self,
        *,
        name: str,
        jql: str,
        description: str | None = None,
        favourite: bool = False,
    ) -> dict[str, Any]:
        """Create a new filter owned by the authenticated user.

        Calls ``POST /rest/api/2/filter``. The body follows the DC
        schema ``{"name", "jql", "description", "favourite"}``.

        Args:
            name: The filter display name.
            jql: The JQL query backing the filter.
            description: Optional filter description.
            favourite: Whether to mark the filter as a favourite of the
                authenticated user. Defaults to ``False``.

        Returns:
            Created filter data dictionary.

        Raises:
            ValueError: If the response is not a JSON object.
        """
        data: dict[str, Any] = {
            "name": name,
            "jql": jql,
            "favourite": favourite,
        }
        if description is not None:
            data["description"] = description

        response = self.jira.post("rest/api/2/filter", json=data)
        if not isinstance(response, dict):
            msg = (
                f"Unexpected response type from "
                f"`POST /rest/api/2/filter`: {type(response).__name__}"
            )
            logger.error(msg)
            raise ValueError(msg)

        return response

    def update_filter(self, filter_id: str, **fields: Any) -> dict[str, Any]:
        """Update an existing filter.

        Calls ``PUT /rest/api/2/filter/{filter_id}``. Accepted fields
        follow the DC schema (``name``, ``jql``, ``description``,
        ``favourite``) and are forwarded verbatim as the request body.

        Args:
            filter_id: The filter identifier.
            **fields: Fields to update on the filter.

        Returns:
            Updated filter data dictionary.

        Raises:
            ValueError: If the response is not a JSON object.
        """
        response = self.jira.put(
            f"rest/api/2/filter/{filter_id}", data=dict(fields)
        )
        if not isinstance(response, dict):
            msg = (
                f"Unexpected response type from "
                f"`PUT /rest/api/2/filter/{filter_id}`: "
                f"{type(response).__name__}"
            )
            logger.error(msg)
            raise ValueError(msg)

        return response

    def get_filter_owner_name(self, filter_id: str) -> str:
        """Resolve the owning user's identifier for a filter.

        Helper used by the owner-scoped delete path in
        ``servers/jira.py``: the server tool calls this first, compares
        the result to the authenticated user via ``require_owner``, and
        only issues the DELETE when ownership matches.

        Jira DC returns the owner under the ``owner`` key of the filter
        object. Server/DC populates ``owner.name`` (the username) and
        may additionally expose ``owner.key``. Atlassian Cloud instead
        surfaces ``owner.accountId`` (and omits ``name``/``key``). This
        helper prefers the DC-native ``name`` / ``key`` fields and falls
        back to ``accountId`` so the same tool works transparently on
        both deployment shapes.

        Args:
            filter_id: The filter identifier.

        Returns:
            The owner's DC username (``owner.name``) when available,
            otherwise the owner key (``owner.key``), otherwise the
            Cloud account id (``owner.accountId``). Returns an empty
            string when none of those fields is present.
        """
        filter_obj = self.get_filter(filter_id)
        owner = filter_obj.get("owner")
        if not isinstance(owner, dict):
            return ""

        name = owner.get("name")
        if isinstance(name, str) and name:
            return name

        key = owner.get("key")
        if isinstance(key, str) and key:
            return key

        account_id = owner.get("accountId")
        if isinstance(account_id, str) and account_id:
            return account_id

        return ""

    def delete_filter(self, filter_id: str) -> None:
        """Delete a filter by id.

        Calls ``DELETE /rest/api/2/filter/{filter_id}``. This mixin
        method performs no ownership check; callers (typically the
        ``jira_delete_own_filter`` server tool) MUST resolve the owner
        via :meth:`get_filter_owner_name` and confirm ownership before
        calling this.

        Args:
            filter_id: The filter identifier.
        """
        self.jira.delete(f"rest/api/2/filter/{filter_id}")
