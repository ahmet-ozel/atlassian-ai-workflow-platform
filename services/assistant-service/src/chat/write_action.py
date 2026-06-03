"""Write-action intent intercept.

The chat tool-call loop in :mod:`src.chat.handler` consults
:func:`is_write_intent` *before* dispatching any LLM-issued tool call.
When the predicate returns ``True`` the handler emits a
``redirect_to_task_creator`` SSE event and stops the iteration --- the
underlying tool is **never invoked**. This is the deterministic mechanism
that enforces the write-action guard:

    THE Assistant_Service SHALL LLM çıktısında
    ``intent: write_action_requested`` algıladığında doğrudan yazma
    aksiyonu çalıştırmaz; UI'a ``redirect_to_task_creator`` SSE event'i
    gönderir [...]

The module is intentionally tiny and dependency-free so the property
test can validate the
decision table by table-driven enumeration without standing up the
chat handler.

The ``ToolCall`` dataclass declared in this module is a *minimal*
local placeholder used only by :func:`is_write_intent`. The richer
SSE/messages contract lives in ``libs/messages``; when that
module lands :func:`is_write_intent` will continue to accept any
object whose ``tool_name`` attribute is a string thanks to the
``Protocol`` typing below, so the property test will keep working
against either definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


__all__ = [
    "WRITE_ACTION_TOOLS",
    "ToolCall",
    "ToolCallLike",
    "is_write_intent",
]


# ---------------------------------------------------------------------------
# Static catalogue of "write" MCP tools
# ---------------------------------------------------------------------------

#: Tool names that mutate Atlassian resources.
#:
#: This set is the source of truth used by :func:`is_write_intent` for the
#: *implicit* (tool-name based) branch of the decision table; the
#: *explicit* branch is driven by the LLM's structured ``intent`` field.
#: A ``frozenset`` is used so the catalogue is immutable at runtime --- a
#: bug that mutates this set would silently widen what the chat handler
#: lets through.
#:
#: The seven entries cover Bitbucket (PR creation Cloud + DC, raw commit), Confluence
#: (create + update page), and Jira (create issue, transition issue).
#: Bitbucket *merge* and Confluence *delete* are not listed here because
#: they are blocked even earlier by the foundation banned-tool list
#: by the shared MCP tool filter; reaching this predicate
#: with one of those names would already be a bug elsewhere.
WRITE_ACTION_TOOLS: frozenset[str] = frozenset(
    {
        "bitbucket_create_pull_request_cloud",
        "bitbucket_create_pull_request_dc",
        "bitbucket_commit",
        "confluence_create_page",
        "confluence_update_page",
        "jira_create_issue",
        "jira_transition_issue",
    }
)


# ---------------------------------------------------------------------------
# Minimal ToolCall placeholder
# ---------------------------------------------------------------------------


class ToolCallLike(Protocol):
    """Structural type for any object exposing a ``tool_name`` string.

    The chat handler will eventually receive ``ToolCall`` instances from
    ``libs/messages``. Until that module is wired this
    protocol keeps :func:`is_write_intent` decoupled from the concrete
    type so callers --- including the property test --- can pass either
    the local :class:`ToolCall` placeholder or the future shared
    dataclass.
    """

    @property
    def tool_name(self) -> str:  # pragma: no cover - structural typing only
        ...


@dataclass(frozen=True, slots=True)
class ToolCall:
    """Minimal frozen tool-call record used by :func:`is_write_intent`.

    Only the ``tool_name`` field is consumed by the predicate; richer
    fields (arguments, call id, MCP server) belong on the eventual
    shared ``libs/messages.ToolCall`` dataclass and are intentionally
    omitted here to keep the surface area --- and the property test
    enumeration --- small.

    The dataclass is ``frozen`` and ``slots``-enabled so equality is
    structural and the predicate has no way to mutate the call object.
    """

    tool_name: str


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


def is_write_intent(
    tool_call: ToolCallLike,
    *,
    llm_intent_field: str | None,
) -> bool:
    """Return ``True`` iff the upcoming tool call is a write action.

    The decision table is:

    +---------------------------------------+--------------------------------+--------+
    | ``llm_intent_field``                  | ``tool_call.tool_name``        | result |
    +=======================================+================================+========+
    | ``"write_action_requested"``          | *anything*                     | True   |
    +---------------------------------------+--------------------------------+--------+
    | anything else (incl. ``None``,        | ``∈ WRITE_ACTION_TOOLS``       | True   |
    | ``"read_action"``, arbitrary string)  |                                |        |
    +---------------------------------------+--------------------------------+--------+
    | anything else                         | ``∉ WRITE_ACTION_TOOLS``       | False  |
    +---------------------------------------+--------------------------------+--------+

    The explicit branch (LLM-supplied intent) takes priority over the
    implicit one so the LLM can flag a *would-be-write* even when the
    structured tool name is missing or generic. The implicit branch is
    the safety net for cases where the LLM forgot to populate the
    intent field.

    Parameters
    ----------
    tool_call:
        Any object exposing ``tool_name: str`` --- typically the local
        :class:`ToolCall` placeholder or the future
        ``libs/messages.ToolCall`` dataclass.
    llm_intent_field:
        Value of the LLM's structured ``intent`` field for this tool
        call. ``None`` is allowed and means "the LLM did not classify
        this call".

    Returns
    -------
    bool
        ``True`` when the chat handler must intercept and emit
        ``redirect_to_task_creator``; ``False`` when the call may be
        dispatched to the MCP tool layer (after the foundation banned
        tool list and capability gate).
    """

    if llm_intent_field == "write_action_requested":
        return True
    tool_name = getattr(tool_call, "tool_name", None)
    if tool_name is None and isinstance(tool_call, Mapping):
        tool_name = tool_call.get("tool_name") or tool_call.get("name")
    return str(tool_name or "") in WRITE_ACTION_TOOLS
