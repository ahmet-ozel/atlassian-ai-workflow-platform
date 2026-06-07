"""Chat protocol dataclasses shared between assistant-service and clients.

This module defines the deterministic on-the-wire shapes used by the
``assistant-service`` chat tool-call loop and any client (Streamlit
``pages/1_chat.py``, admin-dashboard prompt sandbox, integration tests)
that consumes ``POST /api/chat/stream``.

The three primary types are:

* :class:`Message` - single chat history entry (role + text + optional
  tool call id).
* :class:`ChatRequest` - request body sent to the SSE chat endpoint.
* :class:`SseEvent` - a single event yielded over the SSE stream. The
  ``type`` field is constrained to the **10 fixed event types** defined
  by the chat event catalog:

  ``token``, ``tool_call``, ``tool_result``,
  ``redirect_to_task_creator``, ``intent``,
  ``fallback_provider_active``,
  ``rate_limit_exhausted``, ``token_cap_exceeded``, ``error``, ``done``.

All dataclasses are :func:`~dataclasses.dataclass`-decorated with
``frozen=True`` and ``slots=True`` so instances are hashable, immutable
and cheap to allocate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping

__all__ = [
    "Message",
    "ChatRequest",
    "SseEvent",
    "MessageRole",
    "SseEventType",
    "SSE_EVENT_TYPES",
]


# Public type aliases for the literal sets. Re-exported so callers can
# annotate their own functions without restating the union.
MessageRole = Literal["system", "user", "assistant", "tool"]

SseEventType = Literal[
    "token",
    "tool_call",
    "tool_result",
    "redirect_to_task_creator",
    "intent",
    "fallback_provider_active",
    "rate_limit_exhausted",
    "token_cap_exceeded",
    "error",
    "done",
]

#: Tuple of the 10 fixed SSE event types. Mirrors :data:`SseEventType`
#: at runtime so call-sites and tests can iterate or membership-check
#: without re-typing the literal.
SSE_EVENT_TYPES: tuple[SseEventType, ...] = (
    "token",
    "tool_call",
    "tool_result",
    "redirect_to_task_creator",
    "intent",
    "fallback_provider_active",
    "rate_limit_exhausted",
    "token_cap_exceeded",
    "error",
    "done",
)


@dataclass(frozen=True, slots=True)
class Message:
    """A single entry in a chat history.

    Attributes:
        role: One of ``"system"``, ``"user"``, ``"assistant"``,
            ``"tool"``. The ``"tool"`` role is used to feed tool call
            results back into the LLM context.
        text: The message body. PII filtering is applied **before** a
            user-authored message is wrapped in a :class:`Message` and
            sent to the LLM (see ``assistant_service.chat.handler``).
        tool_call_id: When ``role == "tool"``, the id of the tool call
            this message is replying to. ``None`` for other roles.
    """

    role: MessageRole
    text: str
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """Request body for ``POST /api/chat/stream``.

    Attributes:
        user_message: The newly-submitted user message text. PII
            filtering is applied to this field server-side **before**
            it is appended to ``history`` and forwarded to the LLM.
        history: Prior chat turns in chronological order. The
            assistant-service applies a sliding window (default 20) to
            this list before invoking the LLM, so callers may send the
            full session history without worrying about token caps.
        dept_id: Department id whose capability set, prompt template
            variables and budget caps apply to this request.
        session_id: Streamlit session id. Used to scope per-user
            credentials in Vault under
            ``atlassian/_user_session/<session_id>/<service>``.
    """

    user_message: str
    history: tuple[Message, ...]
    dept_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class SseEvent:
    """A single Server-Sent Event emitted by the chat stream.

    The wire format is ``data: <json>\\n\\n`` where ``<json>`` is the
    JSON-encoded form of ``{"type": <type>, "payload": <payload>}``.

    Attributes:
        type: One of the 10 fixed event types. See module docstring for
            the catalog and :data:`SSE_EVENT_TYPES` for the runtime
            tuple. The chat handler emits at most one terminal event
            per stream (``done``, ``rate_limit_exhausted``,
            ``token_cap_exceeded``, ``redirect_to_task_creator`` or
            ``error``). The ``intent`` event is emitted as a
            non-terminal event when the LLM detects a write action
            intent in the user's message.
        payload: Event-specific data. Kept as an arbitrary
            :class:`~typing.Mapping` so callers can serialise without
            an intermediate model. Concrete payload shapes are
            documented per event in the design doc.
    """

    type: SseEventType
    payload: Mapping[str, Any]
