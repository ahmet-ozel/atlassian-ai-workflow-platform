"""Module for Confluence long-task polling.

Implements Requirement 38 (read-only polling of asynchronous Confluence
operations such as page moves and page-tree copies) against the
Confluence Data Center REST API.

Endpoint reference:
    * ``GET /rest/api/longtask/{long_task_id}``
      — return the status envelope for a single long-running task. The
      response body carries the task id, progress percentage, success /
      finished flags, the originating user, elapsed and remaining time,
      and any accumulated message records. The DC contract makes no
      promise about which subset of fields is populated at any given
      poll (for example ``successful`` is only meaningful once
      ``finished`` is true), so the mixin passes the dict through
      verbatim and leaves interpretation to the caller.

The mixin is read-only by design: Requirement 38 only asks for a poll
method, and Confluence DC does not expose a cancel endpoint for the
long-task surface in the first place. The one wrinkle the mixin handles
is 404 translation. Confluence returns 404 both for task ids it never
issued and for task ids that have been garbage-collected after
completion (DC retains long-task records only for a short window). The
server-tool layer needs to distinguish that case from arbitrary transport
errors so it can return the structured ``long_task_not_found`` error
code listed in the feature's error-code allowlist; doing the translation
here keeps the server layer's error mapping small and declarative.

To that end the module defines a local :class:`LongTaskNotFoundError`
that ``get_long_task`` raises on a 404. All other HTTP failures fall
through as the underlying ``HTTPError`` so the cross-cutting auth and
transport error mappers in the server layer can handle them uniformly.
"""

from __future__ import annotations

import logging
from typing import Any

from requests.exceptions import HTTPError

from .client import ConfluenceClient

logger = logging.getLogger("mcp-atlassian")


class LongTaskNotFoundError(Exception):
    """Raised when Confluence returns 404 for a long-task id.

    The server-tool layer catches this and maps it to the structured
    ``long_task_not_found`` error envelope required by Requirement 38.2.
    The exception carries the offending id so the server layer can
    include it in the error details without re-parsing the message.
    """

    def __init__(self, long_task_id: str) -> None:
        self.long_task_id = str(long_task_id)
        super().__init__(
            f"Confluence long-task id {self.long_task_id!r} was not found"
        )


class LongTasksMixin(ConfluenceClient):
    """Mixin exposing read-only long-task polling for Confluence DC.

    Only one method is provided — :meth:`get_long_task` — matching the
    single Read_Tool registered by Requirement 38.1. Version gating is
    not needed: the long-task endpoint has been present since Confluence
    5.x and has a stable contract across supported DC releases.
    """

    def get_long_task(self, long_task_id: str) -> dict[str, Any]:
        """Return the status envelope for a Confluence long-running task.

        Wraps ``GET /rest/api/longtask/{long_task_id}``. The DC response
        is a dict containing at least the following fields (names are
        reproduced verbatim from the DC contract):

        * ``id`` — the long-task id as a string;
        * ``percentageComplete`` — integer 0-100;
        * ``successful`` — boolean, meaningful once ``finished`` is true;
        * ``finished`` — boolean indicating whether the task has stopped
          (regardless of success);
        * ``elapsedTime`` / ``remainingTime`` — millisecond integers;
        * ``messages`` — list of localized status records;
        * ``additionalDetails`` — provider-specific payload (e.g. the
          destination page id for a page-move task).

        The mixin returns the dict unchanged so the server-tool layer
        can surface every field to the agent without re-serializing.

        Args:
            long_task_id: The Confluence long-task id to poll. Accepted
                as a string so the method works transparently with ids
                returned by :meth:`PageMoveCopyMixin.move_page` and
                :meth:`PageMoveCopyMixin.copy_page_tree`.

        Returns:
            The status dict as returned by Confluence.

        Raises:
            LongTaskNotFoundError: Confluence responded with 404, which
                DC uses both for ids it never issued and for completed
                tasks whose records have aged out of the long-task
                registry. The server-tool layer translates this to the
                ``long_task_not_found`` structured error.
            HTTPError: Propagated from the underlying client for any
                other non-2xx response (for example 401/403 on auth
                failures, or 500 for transport issues). The server layer
                routes these through the standard structured-error
                envelope.
        """
        logger.debug(
            "Polling Confluence long task long_task_id=%s", long_task_id
        )
        path = f"rest/api/longtask/{long_task_id}"
        try:
            response = self.confluence.get(path)
        except HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                raise LongTaskNotFoundError(str(long_task_id)) from exc
            raise

        if not isinstance(response, dict):
            # DC is expected to always return a JSON object for this
            # endpoint; if the client helper hands back anything else
            # (for example an empty body normalized to ``None``) we
            # surface an empty dict rather than propagating the
            # unexpected shape. This keeps the server layer's JSON
            # serialization deterministic.
            logger.debug(
                "get_long_task: unexpected response type %s for "
                "long_task_id=%s; returning empty status",
                type(response).__name__,
                long_task_id,
            )
            return {}
        return response
