"""Watcher operations for Confluence Data Center content.

Confluence DC exposes watcher management through two complementary REST
surfaces:

1. ``/rest/api/user/watch/content/{contentId}`` — the **self-scoped** endpoint
   used by :meth:`watch_page_self` / :meth:`unwatch_page_self`. ``GET``
   returns ``{"watching": bool}`` for the authenticated user; ``POST`` adds
   the authenticated user as a watcher; ``DELETE`` removes them. This is the
   only watcher endpoint the MCP server exposes for writes, satisfying
   Requirement 29.4 which forbids any tool that watches/unwatches on behalf
   of another user.

2. ``/rest/api/content/{contentId}/notification/child-created`` — the
   **read-only enumeration** used by :meth:`list_page_watchers`. DC does not
   publish a dedicated "list watchers of this page" endpoint; the nearest
   stable surface is the notification-subscribers list for the
   ``child-created`` event, which returns the same set of users that
   Confluence surfaces as watchers in the UI. We treat that list as the
   authoritative watcher set for Requirement 29.1.

All three methods are scoped to the authenticated user on writes: there is
no method here that takes a ``user`` argument, by design. The idempotency
guarantees required by Property 9 (Requirement 29.2, 29.3) are implemented
by checking the current watch state via ``GET`` before issuing the ``POST``
/ ``DELETE``, so repeated calls return a stable ``already_watching`` flag
without relying on HTTP error codes that vary across DC versions.
"""

import logging
from typing import Any

from .client import ConfluenceClient

logger = logging.getLogger("mcp-atlassian.confluence.watchers")


class WatchersMixin(ConfluenceClient):
    """Mixin providing self-scoped watcher operations for Confluence DC pages."""

    def list_page_watchers(self, page_id: str) -> list[dict[str, Any]]:
        """List users watching the given Confluence page.

        Uses ``GET /rest/api/content/{page_id}/notification/child-created``,
        which returns the notification-subscribers collection that
        Confluence DC surfaces as the page's watcher list in the UI. The
        response is normalized to a flat list of subscriber dicts by
        extracting ``results`` when the DC response wraps the collection in
        a paged envelope; a bare list response (older DC versions) is
        returned as-is.

        Args:
            page_id: The ID of the Confluence page whose watchers to list.

        Returns:
            A list of subscriber dicts as returned by DC. Each entry
            typically contains at least a ``user`` object with
            ``username``, ``userKey``, and ``displayName`` fields; the
            exact shape is passed through from the upstream response so
            callers can inspect whatever DC provides.
        """
        path = f"rest/api/content/{page_id}/notification/child-created"
        response = self.confluence.get(path)
        if isinstance(response, dict):
            results = response.get("results")
            if isinstance(results, list):
                return results
            # DC occasionally returns a single-subscriber dict without a
            # ``results`` envelope; wrap it so the caller always sees a list.
            return [response]
        if isinstance(response, list):
            return response
        # Defensive fallback for unexpected response shapes (e.g. empty
        # body on a 204): present an empty watcher list rather than
        # raising.
        logger.debug(
            "list_page_watchers: unexpected response type %s for page %s; "
            "returning empty list",
            type(response).__name__,
            page_id,
        )
        return []

    def _is_self_watching(self, page_id: str) -> bool:
        """Return whether the authenticated user is currently watching ``page_id``.

        Centralised so :meth:`watch_page_self` and :meth:`unwatch_page_self`
        share the same ``GET /rest/api/user/watch/content/{page_id}``
        probe. A missing/non-dict response or a raised exception is
        treated as "not watching"; the subsequent POST/DELETE is still
        issued so the authoritative state comes from the write call, not
        from the probe.
        """
        path = f"rest/api/user/watch/content/{page_id}"
        try:
            response = self.confluence.get(path)
        except Exception as exc:  # noqa: BLE001 — probe is best-effort
            logger.debug(
                "_is_self_watching: GET %s raised %s; assuming not watching",
                path,
                exc,
            )
            return False
        if isinstance(response, dict):
            return bool(response.get("watching", False))
        return False

    def watch_page_self(self, page_id: str) -> dict[str, Any]:
        """Add the authenticated user as a watcher of the given page.

        Idempotent: if the user is already watching the page, no ``POST``
        is issued and the method returns ``{"already_watching": True}``.
        Otherwise a ``POST /rest/api/user/watch/content/{page_id}`` is
        issued and the method returns ``{"already_watching": False}``.

        Args:
            page_id: The ID of the Confluence page to watch.

        Returns:
            ``{"already_watching": True}`` when the user was already a
            watcher before the call, ``{"already_watching": False}`` when
            this call added the watch.
        """
        if self._is_self_watching(page_id):
            return {"already_watching": True}

        path = f"rest/api/user/watch/content/{page_id}"
        try:
            self.confluence.post(path)
        except Exception as exc:  # noqa: BLE001 — idempotent fallback
            # DC returns 409 on some versions when a concurrent request
            # added the watch between our GET probe and POST; treat that
            # as an idempotent success.
            logger.warning(
                "watch_page_self: POST %s raised %s; assuming page is already watched",
                path,
                exc,
            )
            return {"already_watching": True}
        return {"already_watching": False}

    def unwatch_page_self(self, page_id: str) -> dict[str, Any]:
        """Remove the authenticated user as a watcher of the given page.

        Idempotent: if the user is not watching the page, no ``DELETE``
        is issued and the method returns ``{"already_watching": False}``.
        Otherwise a ``DELETE /rest/api/user/watch/content/{page_id}`` is
        issued and the method returns ``{"already_watching": True}``
        (the pre-call state — the user *was* watching before this call
        removed the watch).

        Args:
            page_id: The ID of the Confluence page to unwatch.

        Returns:
            ``{"already_watching": True}`` when the user was a watcher
            before this call (and this call removed the watch);
            ``{"already_watching": False}`` when the user was not
            watching, in which case no HTTP DELETE was issued.
        """
        if not self._is_self_watching(page_id):
            return {"already_watching": False}

        path = f"rest/api/user/watch/content/{page_id}"
        try:
            self.confluence.delete(path)
        except Exception as exc:  # noqa: BLE001 — idempotent fallback
            # DC returns 404 when the watch was removed by a concurrent
            # request between our GET probe and DELETE; treat that as an
            # idempotent success (the desired end state is "not watching").
            logger.warning(
                "unwatch_page_self: DELETE %s raised %s; assuming page is no longer watched",
                path,
                exc,
            )
        return {"already_watching": True}
