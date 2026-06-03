"""messages: i18n message catalog loader stub + chat protocol dataclasses.

The i18n side reads JSON catalogs from the package's ``tr/`` and
``en/`` resource directories (and, eventually, a DB override layer)
and returns the merged mapping for the requested ``locale``. This
module exposes only the function signature so downstream services
can import the symbol.

The :mod:`messages.chat` submodule defines the chat protocol
dataclasses (``Message``, ``ChatRequest``, ``SseEvent``) shared
between ``assistant-service`` and its clients. They are re-exported
here for ergonomic ``from messages import SseEvent`` style imports.
"""

from __future__ import annotations

from .chat import (
    SSE_EVENT_TYPES,
    ChatRequest,
    Message,
    MessageRole,
    SseEvent,
    SseEventType,
)

__all__ = [
    "load",
    "Message",
    "MessageRole",
    "ChatRequest",
    "SseEvent",
    "SseEventType",
    "SSE_EVENT_TYPES",
]


def load(locale: str) -> dict:
    """Load the i18n message catalog for ``locale``.

    Args:
        locale: BCP-47-ish locale identifier. The project supports the
            two locales shipped with the repository: ``"tr"`` and
            ``"en"``.

    Returns:
        A dictionary mapping message keys to localized strings. The
        current implementation returns an empty dict to signal "no messages
        registered yet" without raising.
    """

    # TODO: read libs/messages/src/messages/<locale>/messages.json
    # from the package data and merge any DB-backed overrides on top.
    # Returning an empty dict keeps callers safe until messages are loaded.
    _ = locale  # placeholder: silence unused-arg lint until implemented
    return {}
