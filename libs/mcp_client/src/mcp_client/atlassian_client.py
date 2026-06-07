"""Atlassian MCP client - skeleton import point.

This module is intentionally a thin client import point. The real
Jira / Bitbucket / Confluence HTTP wiring lives behind the Atlassian
MCP gateway. What exists here is the **single import point** that the
tests for the banned tool list and PR draft enforcement can attach to
without dragging an HTTP client into this package.

Design context
--------------

Every outbound Atlassian call goes through the
``atlassian_mcp_bitbucket`` MCP service - never directly
from a Python module to ``api.atlassian.com``. This client is the
**caller-side wrapper** around that MCP. By owning a single class
here we keep the banned tool filter and PR draft enforcement at one
chokepoint instead of sprinkling them across every caller.

The skeleton exposes the public shape so:

- callers (``automation-service``, ``agent-runner-worker``,
  ``assistant-service``) can already type-import :class:`AtlassianClient`
  while the HTTP wiring is implemented; and
- the focused tests (``platform/tests/property/test_tool_filter.py``,
  ``test_pr_draft_enforcement.py``) have a stable import path even
  before the HTTP wiring lands.

Calling any of the placeholder methods raises
:class:`NotImplementedError` so accidental use in production code
fails loudly and points at the missing HTTP implementation.

``client_source`` header
------------------------

``client_source`` is a **mandatory** constructor argument. The MCP
traffic dashboard groups inbound calls by the ``X-Client-Source``
header so operators can answer "which component sent that 5xx-rate
burst" without guessing. The header is auto-injected into every
outbound MCP call by the future HTTP layer and is enforced at the MCP
boundary (HTTP 400 on miss). Making the constructor argument required
is the second line of defence: a worker that forgets to identify
itself fails at import time, not at first network call.
"""

from __future__ import annotations

import re
from typing import Any, Final, Mapping

from .pr_draft import enforce_pr_draft
from .tool_filter import filter_tools

#: Pointer to the component that delivers the real HTTP wiring. Surfaced
#: in every :class:`NotImplementedError` so the failure trail is short.
_FOLLOW_UP_SPEC: Final[str] = (
    "the Atlassian MCP HTTP transport implementation."
)

#: Header name the MCP boundary inspects. Mirrors the canonical
#: contract documented in ``platform/docs/api-contracts/mcp-credential-headers.md``
#: §1 (Generic / cross-service headers).
CLIENT_SOURCE_HEADER: Final[str] = "X-Client-Source"

#: Permitted shape: ``<component>`` or ``<component>:<sub-context>``.
#: Lowercase letters, digits, hyphens, and a single optional colon
#: separating the component name from a free-form sub-context string.
#: The sub-context is intentionally permissive (``[a-zA-Z0-9._@-]+``)
#: so callers can encode user-scoped sources like
#: ``streamlit-ui:user@payment``.
_CLIENT_SOURCE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9-]*(?::[a-zA-Z0-9._@-]+)?$"
)


class AtlassianClient:
    """Skeleton wrapper around the ``atlassian_mcp_bitbucket`` MCP service.

    The class binds together the banned-tool and draft-PR enforcement helpers
    in this library so callers route through a single chokepoint:

    - :meth:`available_tools` filters the catalog through
      :func:`mcp_client.filter_tools` so banned tools are never
      surfaced to the LLM.
    - :meth:`open_pull_request` rewrites the payload through
      :func:`mcp_client.enforce_pr_draft` so the outbound call always
      sets ``draft=True`` and audits any flip.

    Real HTTP wiring (httpx-based requests to the MCP, retry policy,
    per-department credential resolution) is intentionally out of scope
    for this package; the :class:`NotImplementedError` placeholders are
    replaced by the concrete gateway client.
    """

    def __init__(
        self,
        *,
        client_source: str,
        mcp_base_url: str | None = None,
    ) -> None:
        """Capture the MCP base URL and the mandatory client source tag.

        Args:
            client_source: Identifier sent in the ``X-Client-Source``
                header on every outbound MCP call. Required. Format:
                ``<component>[:<sub-context>]`` where ``<component>``
                is lowercase kebab-case (e.g. ``agent-runner-worker``,
                ``automation-service``) and the optional sub-context
                is a free-form scope tag (e.g.
                ``streamlit-ui:user@payment``,
                ``automation-service:webhook-jira``).
            mcp_base_url: Optional MCP base URL. Stored for the
                forthcoming HTTP implementation; this package does not
                exercise it.

        Raises:
            ValueError: ``client_source`` is empty, whitespace-only, or
                does not match the documented format.
        """

        if not isinstance(client_source, str):
            raise ValueError(
                "client_source must be a string identifying the calling "
                "component (e.g. 'agent-runner-worker' or "
                "'streamlit-ui:user@payment')."
            )

        normalised = client_source.strip()
        if not normalised:
            raise ValueError(
                "client_source must not be empty. See "
                "platform/docs/api-contracts/mcp-credential-headers.md §1 "
                "for the X-Client-Source contract."
            )

        if not _CLIENT_SOURCE_PATTERN.fullmatch(normalised):
            raise ValueError(
                f"client_source={client_source!r} does not match the "
                "documented pattern. Expected '<component>' or "
                "'<component>:<sub-context>' where component is "
                "lowercase kebab-case (e.g. 'agent-runner-worker' or "
                "'streamlit-ui:user@payment'). See "
                "platform/docs/api-contracts/mcp-credential-headers.md §1."
            )

        self._client_source = normalised
        self._mcp_base_url = mcp_base_url

    @property
    def client_source(self) -> str:
        """Return the validated ``X-Client-Source`` tag for this client."""

        return self._client_source

    # ------------------------------------------------------------------
    # Banned tool catalog filter
    # ------------------------------------------------------------------

    def available_tools(self, raw_catalog: Any) -> list[Any]:
        """Return ``raw_catalog`` with banned tools stripped.

        Thin pass-through to :func:`mcp_client.filter_tools`. The
        method exists on the class so the property test suite can
        bind to a single object that owns both enforcement paths, and
        so downstream callers reach for the same chokepoint regardless
        of which rule they care about.

        Args:
            raw_catalog: The MCP-side tool catalog. Any iterable of
                tool descriptors is accepted - see
                :func:`mcp_client.tool_filter.filter_tools` for the
                supported shapes.

        Returns:
            A new ``list`` containing the catalog entries whose name
            is not in :data:`mcp_client.BANNED_TOOLS`.
        """

        return filter_tools(raw_catalog)

    # ------------------------------------------------------------------
    # PR draft enforcement (skeleton)
    # ------------------------------------------------------------------

    async def open_pull_request(
        self,
        payload: Mapping[str, Any],
        *,
        audit_logger: Any | None = None,
        actor_id: str = "system",
        actor_role: str = "system",
        dept_id: str | None = None,
    ) -> dict[str, Any]:
        """Skeleton - coerce ``draft=True`` and raise NotImplementedError.

        The HTTP wiring lives outside this package; here we exercise
        the enforcement helper and then raise so any caller that
        mistakenly relies on this class today fails loudly with a
        pointer to the gateway implementation.

        The :func:`mcp_client.enforce_pr_draft` call is intentional:
        even though the method ultimately raises, running the helper
        first guarantees that the caller's ``payload`` has been
        processed through the single-source enforcement point. A
        property-style smoke test could observe the audit side
        effect on a payload that needed flipping before the
        :class:`NotImplementedError` propagates.

        Args:
            payload: The PR creation payload.
            audit_logger: Optional :class:`audit_logger.AuditLogger`
                used by :func:`mcp_client.enforce_pr_draft`.
            actor_id: Audit event ``actor_id``.
            actor_role: Audit event ``actor_role``.
            dept_id: Audit event ``dept_id`` (``None`` allowed).

        Raises:
            NotImplementedError: Always - the HTTP transport is
                delivered by the next spec.
        """

        # Run the enforcement helper so the audit side effect (and
        # the ``draft=True`` invariant) is observable even from this
        # skeleton path. Result is discarded because the caller is
        # never going to reach the real HTTP call in this package.
        await enforce_pr_draft(
            payload,
            audit_logger=audit_logger,
            actor_id=actor_id,
            actor_role=actor_role,  # type: ignore[arg-type]
            dept_id=dept_id,
        )
        raise NotImplementedError(
            "AtlassianClient.open_pull_request HTTP wiring is delivered "
            f"by {_FOLLOW_UP_SPEC}"
        )
