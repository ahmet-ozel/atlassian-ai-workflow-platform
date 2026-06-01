"""Module for Confluence page like / unlike operations.

Implements Requirement 37 (like / unlike a page when the Confluence
Likes plugin is installed) against the plugin-bundled REST endpoint
family ``/rest/likes/1.0/content/{page_id}/likes``.

Endpoint reference:
    * ``POST   /rest/likes/1.0/content/{page_id}/likes``
      — add the authenticated user's like to the page. The endpoint
      takes no request body. On success DC returns either an empty
      ``204 No Content`` body or a small ``200 OK`` JSON payload
      describing the new like; the mixin treats both as "like
      recorded" and returns ``{"already_liked": False}``.
      When the user has *already* liked the page, DC returns
      ``409 Conflict``; the mixin catches that status and returns
      ``{"already_liked": True}`` so the tool call is idempotent
      (Requirement 37.3) without exposing the 409 as an error.
    * ``DELETE /rest/likes/1.0/content/{page_id}/likes``
      — remove the authenticated user's like from the page.

The Likes REST endpoints live under ``/rest/likes/1.0`` because they
are contributed by the bundled Confluence Likes plugin rather than the
core content REST surface. On DC instances where the plugin is absent
or disabled, every request to this path returns ``404 Not Found`` with
no plugin-specific envelope, so the mixin raises
:class:`LikesPluginUnavailableError` on 404 to give the server-tool
layer a single, unambiguous signal to translate into the structured
``plugin_unavailable`` error required by Requirement 37.2.

All other non-2xx responses (for example 401/403 on authentication or
permission failures) are surfaced by re-raising the underlying
``HTTPError`` so the server-tool layer can map them through the
existing error-handling path.

DC version gating, toolset gating, read-only mode, and receipt
construction are the server-tool layer's responsibility; this mixin
assumes its inputs have already been authorized by the cross-cutting
guards and issues the minimum HTTP call needed to flip the page's
like state for the authenticated user.
"""

from __future__ import annotations

import logging
from typing import Any

from requests.exceptions import HTTPError

from .client import ConfluenceClient

logger = logging.getLogger("mcp-atlassian")


class LikesPluginUnavailableError(Exception):
    """Raised when the Confluence Likes plugin endpoint returns 404.

    The Likes REST endpoints are provided by the bundled Confluence
    Likes plugin. On DC instances where the plugin is absent or
    disabled, every request to ``/rest/likes/1.0/...`` returns
    ``404 Not Found`` without a plugin-specific error envelope. The
    mixin translates that status into this exception so the server-tool
    layer can map it onto the structured ``plugin_unavailable`` error
    (Requirement 37.2) without inspecting HTTP status codes from
    inside the tool function.

    The exception intentionally carries no extra fields beyond the
    standard message because the plugin-unavailable signal is binary:
    the endpoint either exists or it does not, and the server-tool
    layer fills in the plugin-name detail when building the structured
    error.
    """


class LikesMixin(ConfluenceClient):
    """Mixin exposing like / unlike over Confluence Data Center pages.

    Both methods target the plugin-bundled REST path
    ``/rest/likes/1.0/content/{page_id}/likes``. The mixin uses the
    underlying ``requests`` session directly (rather than the
    ``atlassian-python-api`` wrapper's high-level helpers) so it can
    inspect the exact HTTP status code on failure — specifically to
    distinguish 404 (plugin unavailable) from 409 (already liked) from
    other errors.
    """

    def _likes_url(self, page_id: str) -> str:
        """Return the absolute likes endpoint URL for a page."""
        base_url = (self.config.url or "").rstrip("/")
        return f"{base_url}/rest/likes/1.0/content/{page_id}/likes"

    def like_page(self, page_id: str) -> dict[str, Any]:
        """Add the authenticated user's like to a Confluence page.

        Wraps ``POST /rest/likes/1.0/content/{page_id}/likes``. The
        endpoint takes no request body.

        Idempotency (Requirement 37.3): when the user has already liked
        the page, DC returns ``409 Conflict``. The mixin catches that
        status and returns ``{"already_liked": True}`` so the tool call
        succeeds without exposing the 409 to the caller.

        Plugin availability (Requirement 37.2): when the Likes plugin
        is absent or disabled, the endpoint returns ``404 Not Found``.
        The mixin translates that status into
        :class:`LikesPluginUnavailableError` so the server-tool layer
        can map it onto a structured ``plugin_unavailable`` error.

        Args:
            page_id: Confluence content id of the target page.

        Returns:
            A dict with a single ``already_liked`` key:

            * ``{"already_liked": False}`` when this call added the
              like (HTTP 2xx).
            * ``{"already_liked": True}`` when the user had already
              liked the page (HTTP 409) — no state change was made
              on this call, but the call is reported as a success
              because the end state matches the caller's intent.

        Raises:
            LikesPluginUnavailableError: When the Likes plugin is not
                available on the target DC instance (HTTP 404).
            HTTPError: Propagated from the underlying session on any
                non-2xx response other than 404 or 409 (for example
                401/403 on authentication or permission failures).
        """
        url = self._likes_url(page_id)
        logger.debug("Liking Confluence page page_id=%s", page_id)
        response = self.confluence._session.post(url)
        try:
            response.raise_for_status()
        except HTTPError as exc:
            status = getattr(response, "status_code", None)
            if status == 404:
                logger.debug(
                    "Confluence Likes plugin endpoint returned 404 for "
                    "page_id=%s; treating as plugin_unavailable",
                    page_id,
                )
                raise LikesPluginUnavailableError(
                    "Confluence Likes plugin endpoint is unavailable "
                    f"(HTTP 404 from {url})."
                ) from exc
            if status == 409:
                logger.debug(
                    "Confluence page page_id=%s already liked by the "
                    "authenticated user (HTTP 409); reporting idempotent success",
                    page_id,
                )
                return {"already_liked": True}
            raise

        return {"already_liked": False}

    def unlike_page(self, page_id: str) -> None:
        """Remove the authenticated user's like from a Confluence page.

        Wraps ``DELETE /rest/likes/1.0/content/{page_id}/likes``.

        Plugin availability (Requirement 37.2): when the Likes plugin
        is absent or disabled, the endpoint returns ``404 Not Found``.
        The mixin translates that status into
        :class:`LikesPluginUnavailableError` so the server-tool layer
        can map it onto a structured ``plugin_unavailable`` error.

        This method does not attempt to distinguish "not currently
        liked" from "plugin unavailable" on a 404 response because DC
        reports the same status for both cases on the Likes endpoint
        family. Callers that need an idempotent "ensure unliked"
        semantic should wrap the call in their own try/except.

        Args:
            page_id: Confluence content id of the target page.

        Returns:
            None. A successful call is signalled by the absence of a
            raised exception.

        Raises:
            LikesPluginUnavailableError: When the Likes plugin is not
                available on the target DC instance (HTTP 404).
            HTTPError: Propagated from the underlying session on any
                non-2xx response other than 404 (for example 401/403
                on authentication or permission failures).
        """
        url = self._likes_url(page_id)
        logger.debug("Unliking Confluence page page_id=%s", page_id)
        response = self.confluence._session.delete(url)
        try:
            response.raise_for_status()
        except HTTPError as exc:
            status = getattr(response, "status_code", None)
            if status == 404:
                logger.debug(
                    "Confluence Likes plugin endpoint returned 404 for "
                    "page_id=%s; treating as plugin_unavailable",
                    page_id,
                )
                raise LikesPluginUnavailableError(
                    "Confluence Likes plugin endpoint is unavailable "
                    f"(HTTP 404 from {url})."
                ) from exc
            raise

        return None
