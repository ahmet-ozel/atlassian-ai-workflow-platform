"""Module for Confluence page properties (content-property) operations.

Implements Requirement 33 (page properties) against the Confluence Data
Center REST API. Page properties are arbitrary key/value records stored
against a Confluence content object so automation integrations can persist
small pieces of state against a page without modifying the page body.

Endpoint reference:
    * ``GET    /rest/api/content/{page_id}/property``
      — list all properties currently defined on a page. The DC response
      envelope carries a ``results`` array; the mixin unwraps it so the
      caller receives a plain ``list[dict]``.
    * ``GET    /rest/api/content/{page_id}/property/{key}``
      — fetch a single property by key. DC returns ``404`` when the
      property has not been set, which the mixin translates into
      ``None`` so callers can distinguish "absent" from "error".
    * ``POST   /rest/api/content/{page_id}/property``
      — create a new property with a ``{"key", "value"}`` body. No
      version field is required on create.
    * ``PUT    /rest/api/content/{page_id}/property/{key}``
      — update an existing property. The body must carry the next
      ``version.number`` (current + 1) alongside the new ``value`` and
      the ``key`` so DC can validate the version bump.
    * ``DELETE /rest/api/content/{page_id}/property/{key}``
      — remove a property.

``set_page_property`` is idempotent: it first reads the existing property
(via :meth:`get_page_property`) so it can read the current
``version.number`` and PUT with an incremented version. When the property
does not yet exist the call falls through to a POST. Callers therefore do
not need to track versioning themselves — invoking the method twice with
the same ``(key, value)`` leaves the server-side state the same as a
single invocation (per Requirement 33.4).

The mixin is intentionally narrow: it issues the minimum DC REST calls
needed to manage page properties and returns plain dicts so the
server-tool layer can JSON-encode the results directly. Cross-cutting
guards (``check_read_only``, ``check_project_filter``) and DC version
gating are the server-tool layer's responsibility; this mixin assumes its
inputs have already been authorized.
"""

from __future__ import annotations

import logging
from typing import Any

from requests.exceptions import HTTPError

from .client import ConfluenceClient

logger = logging.getLogger("mcp-atlassian")


class PagePropertiesMixin(ConfluenceClient):
    """Mixin exposing CRUD over Confluence content properties (page properties).

    All four methods target Confluence Data Center REST paths under
    ``/rest/api/content/{page_id}/property`` and return plain dicts (or a
    plain list / ``None``) so the server-tool layer can JSON-encode the
    result directly.
    """

    def list_page_properties(self, page_id: str) -> list[dict[str, Any]]:
        """List all page properties defined on a Confluence page.

        Wraps ``GET /rest/api/content/{page_id}/property``. The DC
        response envelope is::

            {"results": [ {...}, {...} ], "size": N, ...}

        The mixin unwraps ``results`` so the caller receives a plain
        list; if DC returns an unexpected shape (non-dict body, missing
        ``results``, or ``results`` not a list) the method returns an
        empty list rather than propagating the odd shape.

        Args:
            page_id: Confluence content id of the target page.

        Returns:
            List of property dicts as returned by Confluence under the
            ``results`` key. Returns an empty list when the page has no
            properties defined or when the response shape is unexpected.

        Raises:
            HTTPError: Propagated from the underlying client when
                Confluence returns a non-2xx response (for example 404
                when the page does not exist, or 403 when the caller
                lacks read permission).
        """
        logger.debug(
            "Listing Confluence page properties page_id=%s", page_id
        )
        response = self.confluence.get(
            f"rest/api/content/{page_id}/property"
        )
        if not isinstance(response, dict):
            return []
        results = response.get("results")
        if not isinstance(results, list):
            return []
        return results

    def get_page_property(
        self, page_id: str, key: str
    ) -> dict[str, Any] | None:
        """Return a single page property by key, or ``None`` if absent.

        Wraps ``GET /rest/api/content/{page_id}/property/{key}``.
        Confluence DC returns ``404`` when the property has not been set
        on the page; the mixin catches that specific case and translates
        it into ``None`` so callers can distinguish "absent" from
        "error". Any other non-2xx response is re-raised.

        Args:
            page_id: Confluence content id of the target page.
            key: The property key to look up.

        Returns:
            The property dict (including ``value`` and ``version``), or
            ``None`` when Confluence responds with 404 (the property
            does not exist on this page).

        Raises:
            HTTPError: Propagated for any non-404 error response (for
                example 401/403 on auth failures, 500 for transport
                issues).
        """
        logger.debug(
            "Fetching Confluence page property page_id=%s key=%s",
            page_id,
            key,
        )
        try:
            response = self.confluence.get(
                f"rest/api/content/{page_id}/property/{key}"
            )
        except HTTPError as exc:
            if (
                exc.response is not None
                and exc.response.status_code == 404
            ):
                return None
            raise

        if not isinstance(response, dict):
            return None
        return response

    def set_page_property(
        self, page_id: str, key: str, value: Any
    ) -> dict[str, Any]:
        """Idempotently create or update a Confluence page property.

        The method first calls :meth:`get_page_property` to discover
        whether the property already exists:

        * If absent, it ``POST``s a new record to
          ``/rest/api/content/{page_id}/property`` with a body of the
          form ``{"key": key, "value": value}`` (no version field is
          required on create).
        * If present, it reads the current ``version.number`` from the
          existing record and ``PUT``s to
          ``/rest/api/content/{page_id}/property/{key}`` with a body of
          the form ``{"key": key, "value": value, "version": {"number":
          current + 1}}``. DC validates the version bump and rejects
          stale updates with a 409.

        This two-step flow is what makes the operation idempotent in
        the sense required by Requirement 33.4: invoking the method
        twice with the same ``(key, value)`` leaves the server-side
        state the same as a single invocation — the only observable
        difference is the version counter, which DC bumps on each
        write.

        Args:
            page_id: Confluence content id of the target page.
            key: The property key to create or update.
            value: The JSON-serializable value to store. May be any
                shape Confluence accepts (object, array, string, number,
                boolean).

        Returns:
            The resulting property dict returned by Confluence (the
            ``POST`` response on create, or the ``PUT`` response on
            update). Returns an empty dict when DC hands back an
            unexpected non-dict body.

        Raises:
            HTTPError: Propagated from the underlying client when
                Confluence returns a non-2xx response on the probe GET
                (non-404), the create POST, or the update PUT.
        """
        logger.debug(
            "Setting Confluence page property page_id=%s key=%s",
            page_id,
            key,
        )
        existing = self.get_page_property(page_id, key)

        if existing is None:
            # Property does not exist — create via POST. The collection
            # endpoint takes a ``{"key", "value"}`` body with no
            # version field (Confluence assigns the initial version
            # on create).
            body: dict[str, Any] = {"key": key, "value": value}
            response = self.confluence.post(
                f"rest/api/content/{page_id}/property",
                data=body,
            )
            if not isinstance(response, dict):
                return {}
            return response

        # Property exists — bump the version and PUT to update. DC's
        # content-property update contract requires the new version to
        # be strictly greater than the existing one; submitting a stale
        # version triggers a 409.
        current_version = (existing.get("version") or {}).get("number")
        try:
            next_version = int(current_version) + 1
        except (TypeError, ValueError):
            # Defensive: Confluence is expected to always return an
            # integer here. If a proxy/mocked response hands back
            # something unexpected, fall back to version 1 rather than
            # crashing mid-operation.
            next_version = 1

        body = {
            "key": key,
            "value": value,
            "version": {"number": next_version},
        }
        response = self.confluence.put(
            f"rest/api/content/{page_id}/property/{key}",
            data=body,
        )
        if not isinstance(response, dict):
            return {}
        return response

    def delete_page_property(self, page_id: str, key: str) -> None:
        """Delete a Confluence page property by key.

        Wraps ``DELETE /rest/api/content/{page_id}/property/{key}``.
        Returns ``None`` on success. A 404 from Confluence (the
        property is already absent) is surfaced to the caller as an
        ``HTTPError`` rather than silently ignored, so the server-tool
        layer can decide how to render "not found" versus "removed" to
        the agent.

        Args:
            page_id: Confluence content id of the target page.
            key: The property key to delete.

        Raises:
            HTTPError: Propagated from the underlying client when
                Confluence returns a non-2xx response (for example 404
                when the property or page does not exist, or 403 when
                the caller lacks write permission).
        """
        logger.debug(
            "Deleting Confluence page property page_id=%s key=%s",
            page_id,
            key,
        )
        self.confluence.delete(
            f"rest/api/content/{page_id}/property/{key}"
        )
        return None
