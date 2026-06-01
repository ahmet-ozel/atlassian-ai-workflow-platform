"""Module for Confluence content-restriction operations.

Implements Requirement 28 (content restrictions) against the DC REST
endpoint family ``/rest/api/content/{content_id}/restriction``. Content
restrictions govern who may read or update a page or blog post and are
expressed as two operations (``read`` and ``update``), each carrying a
list of user principals and a list of group principals.

Endpoint reference:
    * ``GET    /rest/api/content/{content_id}/restriction/byOperation``
      — list current restrictions grouped by operation.
    * ``PUT    /rest/api/content/{content_id}/restriction``
      — replace the restriction set for the content. Body shape is
      ``{"results": [{"operation": "read", "restrictions": {...}},
      {"operation": "update", "restrictions": {...}}]}`` where each
      ``restrictions`` payload contains ``user`` and ``group`` result
      collections of ``{"type": "known", "username": ...}`` and
      ``{"type": "group", "name": ...}`` entries respectively.
    * ``DELETE /rest/api/content/{content_id}/restriction``
      — clear every restriction on the content.

``set_content_restrictions`` captures the prior state (by calling
``list_content_restrictions``) before writing so the server-tool layer can
build a Reversible Receipt (Requirement 28.4) and callers can roll back.
Return shape is ``{"prior_state": prior, "new_state": new}``.
"""

from __future__ import annotations

import logging
from typing import Any

from .client import ConfluenceClient

logger = logging.getLogger("mcp-atlassian")


class RestrictionsMixin(ConfluenceClient):
    """Mixin exposing list / set / clear over Confluence content restrictions."""

    def _restriction_base_url(self, content_id: str) -> str:
        """Return the base restriction URL for a content item."""
        base_url = (self.config.url or "").rstrip("/")
        return f"{base_url}/rest/api/content/{content_id}/restriction"

    def list_content_restrictions(self, content_id: str) -> dict[str, Any]:
        """Return the current restriction set for a page or blog post.

        Wraps ``GET /rest/api/content/{content_id}/restriction/byOperation``.

        Args:
            content_id: Confluence content id of the target page or blog
                post.

        Returns:
            The raw Confluence response payload describing the read and
            update restrictions currently in effect, including their
            ``user`` and ``group`` principal lists. When the content has
            no restrictions Confluence still returns the operation entries
            with empty ``results`` collections, so callers can treat the
            return value as a stable shape.

        Raises:
            HTTPError: Propagated from the underlying session when
                Confluence returns a non-2xx response (for example 404 when
                the content does not exist).
        """
        url = f"{self._restriction_base_url(content_id)}/byOperation"
        logger.debug(
            "Listing content restrictions for content_id=%s", content_id
        )
        response = self.confluence._session.get(url)
        response.raise_for_status()
        payload = response.json() or {}
        if not isinstance(payload, dict):
            return {}
        return payload

    def set_content_restrictions(
        self,
        content_id: str,
        *,
        read_users: list[str] | None = None,
        read_groups: list[str] | None = None,
        update_users: list[str] | None = None,
        update_groups: list[str] | None = None,
    ) -> dict[str, Any]:
        """Replace the restriction set on a page or blog post.

        Wraps ``PUT /rest/api/content/{content_id}/restriction``. The method
        first GETs the current restrictions so it can return the prior
        state to the caller (Requirement 28.4 — the server-tool layer uses
        the prior state to build a Reversible Receipt).

        Each principal list is optional and defaults to an empty list. The
        request body always carries both the ``read`` and ``update``
        operations so the PUT replaces the full restriction state (rather
        than merging with whatever is currently in place).

        Args:
            content_id: Confluence content id of the target page or blog
                post.
            read_users: Usernames permitted to read the content. Empty or
                ``None`` means no user-level read restriction is applied
                (group-level read restrictions, if any, still apply).
            read_groups: Group names permitted to read the content.
            update_users: Usernames permitted to update the content.
            update_groups: Group names permitted to update the content.

        Returns:
            A dict with two keys:

            * ``prior_state``: the response from
              :meth:`list_content_restrictions` captured immediately before
              the PUT. Callers persist this so they can restore the prior
              restrictions by calling
              :meth:`set_content_restrictions` again or
              :meth:`clear_content_restrictions`.
            * ``new_state``: the response body returned by the PUT, which
              mirrors Confluence's representation of the now-current
              restrictions.

        Raises:
            HTTPError: Propagated from the underlying session on non-2xx
                responses from either the GET (prior-state capture) or the
                PUT (restriction update).
        """
        prior_state = self.list_content_restrictions(content_id)

        read_users = list(read_users or [])
        read_groups = list(read_groups or [])
        update_users = list(update_users or [])
        update_groups = list(update_groups or [])

        def _build_operation(
            operation: str,
            users: list[str],
            groups: list[str],
        ) -> dict[str, Any]:
            return {
                "operation": operation,
                "restrictions": {
                    "user": {
                        "results": [
                            {"type": "known", "username": u} for u in users
                        ]
                    },
                    "group": {
                        "results": [
                            {"type": "group", "name": g} for g in groups
                        ]
                    },
                },
            }

        body: dict[str, Any] = {
            "results": [
                _build_operation("read", read_users, read_groups),
                _build_operation("update", update_users, update_groups),
            ]
        }

        url = self._restriction_base_url(content_id)
        logger.debug(
            "Setting content restrictions for content_id=%s "
            "(read_users=%d, read_groups=%d, update_users=%d, update_groups=%d)",
            content_id,
            len(read_users),
            len(read_groups),
            len(update_users),
            len(update_groups),
        )
        response = self.confluence._session.put(url, json=body)
        response.raise_for_status()
        new_state = response.json() if response.content else {}
        if not isinstance(new_state, dict):
            new_state = {}

        return {"prior_state": prior_state, "new_state": new_state}

    def clear_content_restrictions(self, content_id: str) -> None:
        """Remove every restriction from a page or blog post.

        Wraps ``DELETE /rest/api/content/{content_id}/restriction``. After
        a successful call any user with Confluence's normal space-level
        permissions may read and update the content.

        Args:
            content_id: Confluence content id of the target page or blog
                post.

        Raises:
            HTTPError: Propagated from the underlying session when
                Confluence returns a non-2xx response. A 404 is surfaced
                to the caller so the server-tool layer can translate it
                into a structured response.
        """
        url = self._restriction_base_url(content_id)
        logger.debug(
            "Clearing content restrictions for content_id=%s", content_id
        )
        response = self.confluence._session.delete(url)
        response.raise_for_status()
        return None
