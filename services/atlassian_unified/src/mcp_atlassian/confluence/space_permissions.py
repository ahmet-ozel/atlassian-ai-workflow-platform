"""Module for Confluence space permissions read-only operations.

This mixin implements the space-permissions listing half of Requirement 30
(Confluence Space Permissions Read) for the Atlassian DC tool parity feature.
It is intentionally read-only: no grant, revoke, or modification methods are
exposed here, and none are registered under the ``confluence_space_admin``
toolset. Write behavior is explicitly excluded per Requirement 30.2.
"""

import logging
from typing import Any

from .client import ConfluenceClient

logger = logging.getLogger("mcp-atlassian")


class SpacePermissionsMixin(ConfluenceClient):
    """Mixin for Confluence space permissions read operations.

    Adds a single read-only method that inspects the permissions list attached
    to a Confluence space. The upstream endpoint used is the v1 REST
    ``/rest/api/space/{spaceKey}?expand=permissions`` resource, which DC
    instances have supported across all versions targeted by this feature.
    """

    def list_space_permissions(self, space_key: str) -> list[dict[str, Any]]:
        """List permissions configured on a Confluence space.

        Fetches the space resource with the ``permissions`` expansion and
        returns the raw list of permission entries. Each entry typically
        contains the operation (for example ``read``, ``create``, ``delete``),
        the target type (``user``, ``group``, or anonymous), and the
        associated principal. Callers (server tool functions) are expected to
        shape the payload for the agent; this mixin is intentionally thin and
        preserves the DC structure so downstream mappers stay simple.

        Args:
            space_key: The Confluence space key (for example ``"DOCS"``).

        Returns:
            The list of permission entries attached to the space, or an empty
            list when the space has no ``permissions`` field.

        Raises:
            Exception: Propagated from the HTTP layer when the request fails
                (network error, 4xx/5xx response, or JSON decoding error).
                The server layer converts these to structured errors.
        """
        base_url = (self.config.url or "").rstrip("/")
        url = f"{base_url}/rest/api/space/{space_key}"
        params = {"expand": "permissions"}

        try:
            response = self.confluence._session.get(
                url, params=params, timeout=self.config.timeout
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.error(
                "Failed to list permissions for space '%s': %s", space_key, exc
            )
            raise Exception(
                f"Failed to list permissions for space '{space_key}': {exc}"
            ) from exc

        permissions_container = payload.get("permissions")
        if isinstance(permissions_container, dict):
            # Some DC builds wrap the list in ``{"results": [...]}``; unwrap
            # when present so callers always see a plain list.
            results = permissions_container.get("results")
            if isinstance(results, list):
                return results
            return []
        if isinstance(permissions_container, list):
            return permissions_container
        return []
