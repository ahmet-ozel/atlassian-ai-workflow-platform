"""Module for Confluence page descendants tree operations.

Implements Requirement 40 (descendants tree read with a bounded depth)
against the Confluence Data Center REST API.

Endpoint reference:
    * ``GET /rest/api/content/{page_id}/descendant/page``
      — returns the descendant pages of ``page_id``. Confluence DC
      accepts a ``depth`` query parameter that controls how many
      levels of the page tree are returned. The mixin forwards the
      clamped depth value unchanged and pins ``limit=25`` per the
      feature design, matching the REST default used across the rest
      of this codebase for bounded read calls.

The mixin is intentionally narrow: it issues the single DC REST call
needed to read the descendants tree and returns the raw response
payload unchanged so the server-tool layer can shape the final tool
response (for example attaching ``capped_depth`` metadata per
Requirement 40.3) without a second round-trip.

Depth bounds are enforced at the mixin boundary so every caller — the
server-tool layer today, plus any future internal callers — sees the
same contract. :meth:`DescendantsMixin.get_page_descendants` clamps
``depth`` into the supported ``[1, :data:`MAX_DESCENDANTS_DEPTH`]``
range before forwarding it upstream. The server-tool layer compares
the caller-supplied value to :data:`MAX_DESCENDANTS_DEPTH` to build
the ``capped_depth`` metadata required by Requirement 40.3.
"""

from __future__ import annotations

import logging
from typing import Any

from .client import ConfluenceClient

logger = logging.getLogger("mcp-atlassian")

# Minimum and maximum descendants-tree depth supported by the MCP
# server. The upper bound is defined by Requirement 40.3 and mirrored
# in design Property 15; the lower bound of 1 is implicit in the
# endpoint's contract (a depth of 0 returns no descendants and is not
# a useful tool invocation).
MIN_DESCENDANTS_DEPTH = 1
MAX_DESCENDANTS_DEPTH = 10

# Page size forwarded to the DC endpoint. Aligned with the rest of the
# feature's bounded read calls; the endpoint does not meaningfully
# paginate a tree response so this is effectively an upper bound on the
# number of siblings returned at any given level.
_DESCENDANTS_LIMIT = 25


class DescendantsMixin(ConfluenceClient):
    """Mixin exposing descendants-tree reads over Confluence pages.

    The single method targets a Confluence Data Center REST path and
    returns the raw response dict so the server-tool layer can
    JSON-encode the result directly. The mixin silently clamps
    ``depth`` into ``[:data:`MIN_DESCENDANTS_DEPTH`,
    :data:`MAX_DESCENDANTS_DEPTH`]`` so every caller sees the same
    bounded contract; the server-tool layer is responsible for
    surfacing ``capped_depth`` metadata in the tool response when the
    caller-supplied value exceeds the cap (Requirement 40.3).
    """

    def get_page_descendants(
        self, page_id: str, *, depth: int = 3
    ) -> dict[str, Any]:
        """Fetch the descendants tree for a Confluence page.

        Wraps ``GET /rest/api/content/{page_id}/descendant/page`` with
        a clamped ``depth`` and ``limit=25`` query parameters.
        Confluence DC returns the descendants as a tree-like payload
        (or a flat list with parent references depending on DC
        version); the mixin surfaces the response unchanged so the
        server-tool layer can shape the final tool output without a
        second round-trip.

        ``depth`` is clamped into the supported range before being
        forwarded upstream: values below
        :data:`MIN_DESCENDANTS_DEPTH` are raised to the lower bound
        and values above :data:`MAX_DESCENDANTS_DEPTH` are capped at
        the upper bound. Clamping is silent by design so internal
        callers never have to pre-validate; the server-tool layer
        inspects the caller-supplied value against the cap to build
        the ``capped_depth`` metadata required by Requirement 40.3.

        Args:
            page_id: Confluence content id of the root page whose
                descendants should be fetched.
            depth: Maximum tree depth to traverse. Defaults to 3 per
                Requirement 40.2. Silently clamped to
                ``[:data:`MIN_DESCENDANTS_DEPTH`,
                :data:`MAX_DESCENDANTS_DEPTH`]``.

        Returns:
            The raw DC response dictionary. When DC returns an empty
            body the result is an empty dict so callers can rely on a
            dict contract.

        Raises:
            HTTPError: Propagated from the underlying client when
                Confluence returns a non-2xx response (for example 404
                when the page does not exist, or 403 when the caller
                lacks view permission). The server-tool layer maps
                these to the standard structured-error envelope.
        """
        clamped_depth = max(
            MIN_DESCENDANTS_DEPTH, min(MAX_DESCENDANTS_DEPTH, depth)
        )
        if clamped_depth != depth:
            logger.debug(
                "Clamped descendants depth %r -> %d for page_id=%s",
                depth,
                clamped_depth,
                page_id,
            )

        logger.debug(
            "Fetching Confluence descendants tree page_id=%s depth=%d",
            page_id,
            clamped_depth,
        )
        params = {"depth": clamped_depth, "limit": _DESCENDANTS_LIMIT}
        response = self.confluence.get(
            f"rest/api/content/{page_id}/descendant/page", params=params
        )
        # ``self.confluence.get`` returns ``None`` on an empty body;
        # normalize to an empty dict so callers can rely on a dict
        # contract.
        if not isinstance(response, dict):
            return {}
        return response
