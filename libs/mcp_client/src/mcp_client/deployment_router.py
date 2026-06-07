"""Bitbucket deployment router.

Bitbucket Cloud and Bitbucket Data Center expose distinct MCP tools
for the same logical operation (eg. opening a pull request). Each
department's ``departments.json`` configuration carries a single
``bot.bitbucket.deployment`` field with one of two literal values -
``"cloud"`` or ``"server"`` - and this module is the **single source
of truth** for translating that field into the concrete MCP tool
name.

Keeping the mapping in one place means:

- The ``AgentRunnerWorkflow`` ``code_change_*`` flows can pick the
  right tool deterministically without scattering ``if deployment ==
  "cloud"`` branches across activities.
- Tests can exhaustively verify the mapping in a single place.
- A future deployment variant (``"datacenter-12"``, etc.) is added
  here and only here; the foundation ``BotEntry`` schema and this
  router stay in lock-step.

The deployment value is sourced from the foundation
``departments.json.bot.bitbucket.deployment`` field.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Literal, Mapping

# ---------------------------------------------------------------------------
# Tool name constants
# ---------------------------------------------------------------------------

#: MCP tool name for opening a pull request against Bitbucket Cloud.
BITBUCKET_CREATE_PR_CLOUD: Final[str] = "bitbucket_create_pull_request_cloud"

#: MCP tool name for opening a pull request against Bitbucket Data
#: Center (a.k.a. Bitbucket Server).
BITBUCKET_CREATE_PR_DC: Final[str] = "bitbucket_create_pull_request_dc"


#: Mapping from the foundation ``BotEntry.deployment`` literal to the
#: concrete MCP tool name. ``MappingProxyType`` makes the constant
#: read-only at runtime so callers cannot mutate the routing table by
#: accident.
_PR_CREATE_TOOL_BY_DEPLOYMENT: Final[Mapping[str, str]] = MappingProxyType(
    {
        "cloud": BITBUCKET_CREATE_PR_CLOUD,
        "server": BITBUCKET_CREATE_PR_DC,
    }
)


# ---------------------------------------------------------------------------
# select_pr_create_tool - deployment  MCP tool name
# ---------------------------------------------------------------------------


def select_pr_create_tool(deployment: Literal["cloud", "server"]) -> str:
    """Return the MCP tool name to use when opening a Bitbucket PR.

    The workflow layer must abstract Bitbucket Cloud vs Data Center
    behind a single capability. This
    function is the deterministic, replay-safe pure mapping the
    workflow uses to pick the right tool.

    Args:
        deployment: The foundation ``BotEntry.deployment`` literal.
            Must be either ``"cloud"`` or ``"server"``. Any other
            value - including ``None``, an empty string, or a
            misspelled variant such as ``"Cloud"`` - raises
            :class:`KeyError`. Callers that need to tolerate a missing
            field should default it to a known value *before* invoking
            this function so the mapping stays exhaustive.

    Returns:
        ``"bitbucket_create_pull_request_cloud"`` for ``"cloud"`` and
        ``"bitbucket_create_pull_request_dc"`` for ``"server"``.

    Raises:
        KeyError: If ``deployment`` is not one of the two supported
            literals. The exception message contains the offending
            value so the audit trail (``audit_events`` table) can
            point operators at the misconfigured department.

    Example::

        >>> select_pr_create_tool("cloud")
        'bitbucket_create_pull_request_cloud'
        >>> select_pr_create_tool("server")
        'bitbucket_create_pull_request_dc'

    Notes:
        The function intentionally has **no** default branch - silently
        falling back to ``"cloud"`` would mask a misconfigured
        ``departments.json`` and let a workflow open a Cloud-style PR
        against a Data Center instance. Failing fast surfaces the
        problem at signal-dispatch time where it is cheapest to fix.
    """

    return _PR_CREATE_TOOL_BY_DEPLOYMENT[deployment]
