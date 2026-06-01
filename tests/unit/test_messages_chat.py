"""Unit tests for ``messages.chat`` chat protocol dataclasses.

Validates: Requirement 1.1 — task 4.6 of
``.kiro/specs/platform-mimari-ops/tasks.md``.

The tests cover:

1. The three dataclasses (:class:`Message`, :class:`ChatRequest`,
   :class:`SseEvent`) are frozen, slotted and exposed through the
   ``messages`` package root.
2. :data:`SSE_EVENT_TYPES` enumerates exactly the 10 fixed event types
   declared by the design (``token``, ``tool_call``, ``tool_result``,
   ``redirect_to_task_creator``, ``intent``,
   ``fallback_provider_active``,
   ``rate_limit_exhausted``, ``token_cap_exceeded``, ``error``,
   ``done``) and stays in sync with the :data:`SseEventType` literal.
3. ``Message.tool_call_id`` defaults to ``None`` while ``role`` and
   ``text`` remain required positional fields.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass
from typing import get_args

import pytest

from messages import (
    SSE_EVENT_TYPES,
    ChatRequest,
    Message,
    SseEvent,
)
from messages.chat import SseEventType


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


def test_message_is_frozen_dataclass_with_slots() -> None:
    msg = Message(role="user", text="hello")

    assert is_dataclass(msg)
    assert hasattr(Message, "__slots__")
    with pytest.raises(FrozenInstanceError):
        msg.text = "mutated"  # type: ignore[misc]


def test_message_tool_call_id_defaults_to_none() -> None:
    msg = Message(role="user", text="hi")

    assert msg.tool_call_id is None


def test_message_supports_tool_role_with_call_id() -> None:
    msg = Message(role="tool", text='{"ok": true}', tool_call_id="call-42")

    assert msg.role == "tool"
    assert msg.tool_call_id == "call-42"


def test_chat_request_is_frozen_dataclass_with_slots() -> None:
    req = ChatRequest(
        user_message="hello",
        history=(),
        dept_id="platform",
        session_id="s-1",
    )

    assert is_dataclass(req)
    assert hasattr(ChatRequest, "__slots__")
    with pytest.raises(FrozenInstanceError):
        req.dept_id = "other"  # type: ignore[misc]


def test_chat_request_history_field_is_tuple_typed() -> None:
    history_field = next(f for f in fields(ChatRequest) if f.name == "history")

    # ``tuple[Message, ...]`` round-trips through ``str()`` as either
    # ``tuple[messages.chat.Message, ...]`` or
    # ``Tuple[messages.chat.Message, ...]`` depending on resolver; we
    # only assert the structural prefix to stay tolerant of Python
    # version cosmetics.
    assert "tuple" in str(history_field.type).lower()
    assert "Message" in str(history_field.type)


def test_sse_event_is_frozen_dataclass_with_slots() -> None:
    evt = SseEvent(type="done", payload={})

    assert is_dataclass(evt)
    assert hasattr(SseEvent, "__slots__")
    with pytest.raises(FrozenInstanceError):
        evt.type = "error"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SSE event type catalog
# ---------------------------------------------------------------------------


_EXPECTED_SSE_TYPES = (
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


def test_sse_event_types_constant_lists_ten_fixed_types() -> None:
    assert len(SSE_EVENT_TYPES) == 10
    assert SSE_EVENT_TYPES == _EXPECTED_SSE_TYPES


def test_sse_event_type_literal_matches_runtime_tuple() -> None:
    literal_args = get_args(SseEventType)

    assert tuple(literal_args) == SSE_EVENT_TYPES


@pytest.mark.parametrize("event_type", _EXPECTED_SSE_TYPES)
def test_sse_event_accepts_each_fixed_type(event_type: str) -> None:
    evt = SseEvent(type=event_type, payload={"k": "v"})  # type: ignore[arg-type]

    assert evt.type == event_type
    assert evt.payload == {"k": "v"}


# ---------------------------------------------------------------------------
# Package re-exports
# ---------------------------------------------------------------------------


def test_chat_protocol_types_are_re_exported_from_messages() -> None:
    import messages

    for name in ("Message", "ChatRequest", "SseEvent", "SSE_EVENT_TYPES"):
        assert name in messages.__all__
        assert hasattr(messages, name)
