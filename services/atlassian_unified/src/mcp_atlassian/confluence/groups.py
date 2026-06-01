"""Module for Confluence group search and user-group membership reads.

Implements Requirement 39 (search groups and list a user's group
memberships, read-only) against the Confluence Data Center REST API.

Endpoint reference:
    * ``GET /rest/api/group?prefix={query}&limit={limit}``
      — search groups whose name starts with ``prefix``. DC returns a
      paged envelope of the form
      ``{"results": [{"name": "...", "type": "..."}, ...], "start": N,
      "limit": N, "size": N}``. When ``prefix`` is omitted DC returns
      the first ``limit`` groups ordered by name, which is the natural
      "list groups" surface the agent uses when no query is supplied.
    * ``GET /rest/api/user/memberof?username={username}`` (or ``&key={key}``)
      — list the groups the given user is a member of. DC resolves the
      user by ``username`` (legacy) or by ``key`` (``userKey``); the two
      selectors are mutually exclusive but the mixin simply forwards
      whichever the caller supplied so DC can apply its own precedence
      rules.

The mixin is deliberately read-only. Requirement 39.2 forbids any tool
that creates, deletes, or modifies groups, or that adds or removes
users from groups, so no write methods are implemented here. The
server-tool layer MUST NOT register a write tool that delegates to this
module — Property 7 (forbidden-endpoint exclusion) enforces the same
boundary at registration time.

Both methods return plain ``list[dict]`` values so the server-tool
layer can JSON-encode the result directly. Response envelopes are
unwrapped (``results`` extracted) and unexpected shapes are normalized
to an empty list rather than raising, matching the read-only
conservatism used by :mod:`~mcp_atlassian.confluence.watchers`.
"""

from __future__ import annotations

import logging
from typing import Any

from .client import ConfluenceClient

logger = logging.getLogger("mcp-atlassian")


class GroupsMixin(ConfluenceClient):
    """Mixin exposing read-only group search and user-group membership reads.

    Both methods target Confluence Data Center REST paths and return
    plain lists of dicts so the server-tool layer can surface them to
    the agent without additional shaping. No write surface is exposed:
    group creation, deletion, modification, and membership changes are
    explicitly out of scope per Requirement 39.2.
    """

    def search_groups(
        self,
        *,
        query: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Search Confluence groups by name prefix.

        Wraps ``GET /rest/api/group`` with an optional ``prefix`` query
        parameter so callers can either list the first page of groups
        (when ``query`` is ``None``) or narrow to names starting with a
        given substring. DC returns a paged envelope of the form
        ``{"results": [...], "start": N, "limit": N, "size": N}``; this
        method extracts and returns the ``results`` list.

        Args:
            query: Optional name prefix to filter groups by. When
                ``None`` or empty, the ``prefix`` parameter is omitted
                and DC returns groups ordered by name.
            limit: Maximum number of groups to return in a single page.
                Forwarded to DC as the ``limit`` query parameter.
                Defaults to 50.

        Returns:
            A list of group dicts as returned by DC. Each entry
            typically contains at least ``name`` and ``type`` fields;
            the exact shape is passed through from the upstream
            response so callers can inspect whatever DC provides. When
            DC returns an unexpected body shape (non-dict, missing
            ``results``), an empty list is returned.

        Raises:
            HTTPError: Propagated from the underlying client when
                Confluence returns a non-2xx response (for example 401
                or 403 when the caller lacks permission to enumerate
                groups). The server-tool layer maps these to the
                standard structured-error envelope.
        """
        params: dict[str, Any] = {"limit": int(limit)}
        if query:
            params["prefix"] = query

        logger.debug(
            "Searching Confluence groups query=%r limit=%s",
            query,
            params["limit"],
        )
        response = self.confluence.get("rest/api/group", params=params)
        if isinstance(response, dict):
            results = response.get("results")
            if isinstance(results, list):
                return results
            logger.debug(
                "search_groups: missing/non-list 'results' in response; "
                "returning empty list",
            )
            return []
        if isinstance(response, list):
            return response
        logger.debug(
            "search_groups: unexpected response type %s; returning empty list",
            type(response).__name__,
        )
        return []

    def get_user_groups_confluence(
        self,
        *,
        username: str | None = None,
        key: str | None = None,
    ) -> list[dict[str, Any]]:
        """List the Confluence groups a given user is a member of.

        Wraps ``GET /rest/api/user/memberof`` with either the legacy
        ``username`` selector or the ``key`` (user key) selector. DC
        resolves the user from whichever selector is provided; callers
        should supply exactly one, though the mixin forwards both when
        given and lets DC apply its own precedence rules (typically
        ``key`` wins when both are present).

        DC returns a paged envelope of the form
        ``{"results": [{"name": "...", "type": "..."}, ...], "start": N,
        "limit": N, "size": N}``; this method extracts and returns the
        ``results`` list.

        Args:
            username: Optional username selector. Forwarded as the
                ``username`` query parameter when provided.
            key: Optional user-key selector. Forwarded as the ``key``
                query parameter when provided.

        Returns:
            A list of group dicts as returned by DC. Each entry
            typically contains at least ``name`` and ``type`` fields;
            the exact shape is passed through from the upstream
            response so callers can inspect whatever DC provides. When
            DC returns an unexpected body shape (non-dict, missing
            ``results``), an empty list is returned.

        Raises:
            HTTPError: Propagated from the underlying client when
                Confluence returns a non-2xx response (for example 404
                when the user cannot be resolved, or 401/403 when the
                caller lacks permission to read the user's memberships).
        """
        params: dict[str, Any] = {}
        if username is not None:
            params["username"] = username
        if key is not None:
            params["key"] = key

        logger.debug(
            "Listing Confluence group memberships username=%r key=%r",
            username,
            key,
        )
        response = self.confluence.get("rest/api/user/memberof", params=params)
        if isinstance(response, dict):
            results = response.get("results")
            if isinstance(results, list):
                return results
            logger.debug(
                "get_user_groups_confluence: missing/non-list 'results' in "
                "response; returning empty list",
            )
            return []
        if isinstance(response, list):
            return response
        logger.debug(
            "get_user_groups_confluence: unexpected response type %s; "
            "returning empty list",
            type(response).__name__,
        )
        return []
