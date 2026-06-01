"""Module for Confluence inline tasks read-only operations.

Implements Requirement 36 (Confluence Inline Tasks Read) for the Atlassian
DC tool parity feature. This mixin is intentionally read-only: no
create/update/complete/delete methods are exposed here, and Requirement
36.2 explicitly forbids any Write_Tool in the ``confluence_tasks``
toolset in this feature.

Endpoint reference:
    * ``GET /rest/mywork/latest/task?pageId={page_id}``
      — Confluence Data Center exposes inline tasks through the
      ``mywork`` plugin, which ships with DC and backs the UI's "Tasks"
      surface. The endpoint accepts a ``pageId`` query parameter and
      returns a JSON array of task descriptors (or a paged envelope on
      newer DC builds), where each entry carries the task id,
      completion status, assignee, due date, source page, and task
      body. Returning this shape unchanged keeps the server-tool layer
      a thin mapper and preserves forward-compatibility with whatever
      additional fields DC emits in future releases.

Older DC instances and instances running with the ``mywork`` plugin
disabled return ``404`` (or otherwise raise) for this endpoint; per
Requirement 36.1 the goal is to *list* inline tasks rather than fail a
page inspection, so the mixin treats such errors as "no visible inline
tasks" and returns an empty list. That keeps downstream tools stable
when inline-task data simply is not available, while still letting the
server-tool layer rely on the standard read-only envelope.
"""

from __future__ import annotations

import logging
from typing import Any

from .client import ConfluenceClient

logger = logging.getLogger("mcp-atlassian")


class InlineTasksMixin(ConfluenceClient):
    """Mixin exposing read-only listing of Confluence inline tasks.

    The single method targets the DC ``mywork`` REST surface and returns
    a plain list of task dicts so the server-tool layer can JSON-encode
    the result directly. DC version gating and toolset registration are
    the server layer's responsibility; this mixin assumes its inputs
    have already been authorized by the cross-cutting guards.
    """

    def list_inline_tasks(self, page_id: str) -> list[dict[str, Any]]:
        """List inline tasks attached to a Confluence page.

        Wraps ``GET /rest/mywork/latest/task?pageId={page_id}``. The DC
        ``mywork`` endpoint returns a collection of inline-task entries
        for the given page; each entry typically carries fields such as
        the task id, completion status, assignee, due date, and task
        body. The response shape is passed through to the caller so the
        server-tool layer can surface assignee and due-date information
        required by Requirement 36.1 without a second round-trip.

        The method normalizes two response shapes:

        * a bare JSON array (older DC builds) — returned as-is;
        * a paged envelope of the form ``{"results": [...]}`` (newer DC
          builds) — the ``results`` list is extracted.

        Any other response shape, or any error raised by the underlying
        HTTP client (including ``404`` from instances without the
        ``mywork`` plugin, and connection or decode errors), is treated
        as "no visible inline tasks" and yields an empty list. This
        keeps Requirement 36.1 a best-effort read rather than a hard
        failure on DC instances where the plugin is unavailable.

        Args:
            page_id: Confluence content id of the page whose inline
                tasks to list.

        Returns:
            A list of inline-task dicts as returned by DC, or an empty
            list when the page has no inline tasks, the ``mywork``
            endpoint is unavailable, or the response cannot be
            interpreted.
        """
        logger.debug("Listing Confluence inline tasks page_id=%s", page_id)
        try:
            response = self.confluence.get(
                "rest/mywork/latest/task",
                params={"pageId": str(page_id)},
            )
        except Exception as exc:  # noqa: BLE001 — read is best-effort
            logger.debug(
                "list_inline_tasks: GET rest/mywork/latest/task raised %s "
                "for page %s; returning empty list",
                exc,
                page_id,
            )
            return []

        if isinstance(response, list):
            return response
        if isinstance(response, dict):
            results = response.get("results")
            if isinstance(results, list):
                return results

        logger.debug(
            "list_inline_tasks: unexpected response type %s for page %s; "
            "returning empty list",
            type(response).__name__,
            page_id,
        )
        return []
