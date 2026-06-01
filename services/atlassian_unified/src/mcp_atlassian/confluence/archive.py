"""Module for Confluence page and space archive operations.

Implements Requirement 34 (archive, restore, and space-archive without
permanent delete) against the Confluence Data Center REST API.

Endpoint reference:
    * ``POST /rest/api/content/archive``
      — bulk archive one or more pages. The DC endpoint takes a
      ``{"pages": [{"id": "..."}]}`` body and returns a long-task / job
      descriptor (or a direct confirmation on small batches). The mixin
      submits a single-page batch so the caller-facing shape is
      predictable.
    * ``PUT  /rest/api/content/{page_id}?status=archived``
      — restore a previously archived page. The request body is the
      standard content-update envelope carrying the next version number
      and the target status ``current``. The ``status=archived`` query
      parameter is required so DC resolves the page in the archived
      collection rather than the default (current) collection.
    * ``PUT  /rest/api/space/{space_key}/archive``
      — archive a space (Confluence DC 7.0+). The endpoint takes no
      request body and sets the space status to ``archived``.

The mixin is intentionally narrow: it issues the minimum DC REST calls
needed to flip archive state and returns a deterministic confirmation
payload (including the resolved identifier) so the server-tool layer can
build a ``Reversible_Receipt`` referencing
:func:`restore_archived_page` (per Requirement 34.6) without a second
round-trip.

This module deliberately does **not** expose any permanent-delete
method. Requirements 34.4 and 34.5 explicitly forbid a space-delete or
cascading page-tree delete tool, so neither mapping is implemented
here. Callers that need to remove content permanently must do so
through the Confluence UI with appropriate administrator review.
"""

from __future__ import annotations

import logging
from typing import Any

from .client import ConfluenceClient

logger = logging.getLogger("mcp-atlassian")


class ArchiveMixin(ConfluenceClient):
    """Mixin exposing archive / restore over Confluence pages and spaces.

    All three methods target Confluence Data Center REST paths and
    return plain dicts so the server-tool layer can JSON-encode the
    result directly. DC version gating (``check_dc_version``) and
    receipt construction (``build_receipt``) are the server layer's
    responsibility; this mixin assumes its inputs have already been
    authorized by the cross-cutting guards.
    """

    def archive_page(self, page_id: str) -> dict[str, Any]:
        """Archive a single Confluence page.

        Wraps ``POST /rest/api/content/archive`` with a single-entry
        ``pages`` batch so the behavior is deterministic: one call
        archives exactly the one page identified by ``page_id``. The DC
        endpoint accepts a batch body, so submitting a list of one is
        the simplest path that avoids relying on the version-update
        variant (``PUT /rest/api/content/{id}`` with ``status=archived``)
        which would require a fresh version-number read first.

        Args:
            page_id: Confluence content id of the page to archive.

        Returns:
            A confirmation dictionary of the shape::

                {
                    "archived": True,
                    "page_id": "<page_id>",
                    "response": <raw DC response or {} when body is empty>,
                }

            The ``response`` field carries whatever the archive endpoint
            returned (typically a long-task descriptor like
            ``{"id": "...", "links": {...}}``) so the server-tool layer
            can surface it to the agent unchanged. When DC returns an
            empty body the field is an empty dict.

        Raises:
            HTTPError: Propagated from the underlying client when
                Confluence returns a non-2xx response (for example 404
                when the page does not exist, or 403 when the caller
                lacks archive permission). The server-tool layer maps
                these to the standard structured-error envelope.
        """
        logger.debug("Archiving Confluence page page_id=%s", page_id)
        body = {"pages": [{"id": str(page_id)}]}
        response = self.confluence.post("rest/api/content/archive", data=body)
        if not isinstance(response, dict):
            response = {}
        return {
            "archived": True,
            "page_id": str(page_id),
            "response": response,
        }

    def restore_archived_page(self, page_id: str) -> dict[str, Any]:
        """Restore a previously archived Confluence page.

        Wraps ``PUT /rest/api/content/{page_id}?status=archived`` with
        the standard content-update envelope. DC requires:

        * the ``status=archived`` query parameter so the page is
          resolved from the archived collection (the default resolution
          scope is ``current`` and would 404 for an archived page);
        * a request body that carries the incremented version number
          and the target status ``current``.

        The version number is read from the archived page itself via
        ``get_page_by_id(..., status="archived", expand="version")``
        immediately before the PUT so the update reflects the latest
        known version.

        Args:
            page_id: Confluence content id of the archived page to
                restore.

        Returns:
            A confirmation dictionary of the shape::

                {
                    "restored": True,
                    "page_id": "<page_id>",
                    "version": <new version number>,
                    "response": <raw DC response or {} when body is empty>,
                }

        Raises:
            HTTPError: Propagated from the underlying client when
                Confluence returns a non-2xx response on either the
                version-lookup GET or the restore PUT (for example 404
                when the page is not archived, or 403 when the caller
                lacks restore permission).
        """
        logger.debug("Restoring archived Confluence page page_id=%s", page_id)

        # Read the archived page's current version number so the PUT
        # body can increment it. Confluence's content-update contract
        # requires the new version to be strictly greater than the
        # existing one; submitting a stale version triggers a 409.
        archived = self.confluence.get_page_by_id(
            page_id=str(page_id),
            status="archived",
            expand="version",
        )
        if not isinstance(archived, dict):
            archived = {}
        current_version = int(
            (archived.get("version") or {}).get("number") or 0
        )
        new_version = current_version + 1

        body: dict[str, Any] = {
            "id": str(page_id),
            "type": archived.get("type") or "page",
            "title": archived.get("title") or "",
            "status": "current",
            "version": {"number": new_version},
        }

        response = self.confluence.put(
            f"rest/api/content/{page_id}",
            data=body,
            params={"status": "archived"},
        )
        if not isinstance(response, dict):
            response = {}

        return {
            "restored": True,
            "page_id": str(page_id),
            "version": new_version,
            "response": response,
        }

    def archive_space(self, space_key: str) -> dict[str, Any]:
        """Archive a Confluence space (DC 7.0+).

        Wraps ``PUT /rest/api/space/{space_key}/archive``. The endpoint
        takes no request body and flips the space status to
        ``archived``. It is not reversible through a dedicated REST
        unarchive path on older DC versions; space-administrator tooling
        in the Confluence UI is the supported restore mechanism, so no
        ``restore_space`` method is exposed here.

        DC version gating (``check_dc_version(required="7.0")``) is the
        server-tool layer's responsibility; this mixin simply issues
        the call and surfaces the response.

        Args:
            space_key: Key of the space to archive (for example
                ``"ENG"``).

        Returns:
            A confirmation dictionary of the shape::

                {
                    "archived": True,
                    "space_key": "<space_key>",
                    "response": <raw DC response or {} when body is empty>,
                }

        Raises:
            HTTPError: Propagated from the underlying client when
                Confluence returns a non-2xx response (for example 404
                when the space does not exist, 403 when the caller is
                not a space administrator, or 501 when the DC version
                predates the archive endpoint).
        """
        logger.debug("Archiving Confluence space space_key=%s", space_key)
        response = self.confluence.put(f"rest/api/space/{space_key}/archive")
        if not isinstance(response, dict):
            response = {}
        return {
            "archived": True,
            "space_key": str(space_key),
            "response": response,
        }
