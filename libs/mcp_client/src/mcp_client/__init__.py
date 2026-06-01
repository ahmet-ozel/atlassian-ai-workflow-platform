"""mcp_client — MCP outbound wrapper with single-source enforcement.

Re-exports the public API so callers can simply do::

    from mcp_client import BANNED_TOOLS, filter_tools, enforce_pr_draft

The library owns the canonical implementations of three MIMARI §1
critical rules:

- **Rule 9 / R1.8** — banned MCP tool list
  (:data:`BANNED_TOOLS`, :func:`filter_tools` in :mod:`tool_filter`).
- **Rule 10 / R1.9** — PR draft enforcement
  (:func:`enforce_pr_draft` in :mod:`pr_draft`).
- **Rule §16.10.3 Y3 / Workflows R9.1-R9.6** — firecrawl egress
  allowlist with dept overrides and graceful 403 / overflow handling
  (:class:`FirecrawlClient`, :func:`effective_allowlist` and the
  :class:`EgressBlocked` / :class:`PayloadOverflow` outcome value
  objects in :mod:`firecrawl`).

The :mod:`atlassian_client` skeleton is exported as well so the
single-source enforcement tests have a stable import point (the real
HTTP wiring is delivered by later specs — see
``.kiro/specs/platform-mimari-foundation/design.md`` §`libs/mcp_client`).
"""

from .atlassian_client import AtlassianClient, CLIENT_SOURCE_HEADER
from .deployment_router import (  # re-export for downstream callers
    BITBUCKET_CREATE_PR_CLOUD,
    BITBUCKET_CREATE_PR_DC,
    select_pr_create_tool,
)
from .firecrawl import (
    EgressBlocked,
    FirecrawlClient,
    FirecrawlResult,
    FirecrawlSuccess,
    FirecrawlTransportError,
    PayloadOverflow,
    effective_allowlist,
)
from .pr_draft import PR_DRAFT_AUDIT_ACTION, enforce_pr_draft
from .tool_filter import BANNED_TOOLS, filter_tools

__all__ = [
    "AtlassianClient",
    "BANNED_TOOLS",
    "CLIENT_SOURCE_HEADER",
    "EgressBlocked",
    "FirecrawlClient",
    "FirecrawlResult",
    "FirecrawlSuccess",
    "FirecrawlTransportError",
    "PR_DRAFT_AUDIT_ACTION",
    "PayloadOverflow",
    "effective_allowlist",
    "enforce_pr_draft",
    "filter_tools",
    "BITBUCKET_CREATE_PR_CLOUD",
    "BITBUCKET_CREATE_PR_DC",
    "select_pr_create_tool",
]
