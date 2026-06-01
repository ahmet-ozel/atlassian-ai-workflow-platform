"""Pure sliding-window compressor used by ``ChatHandler.stream``.

Implements the design contract from
``platform-mimari-ops`` design.md §"SlidingWindow":

    def compress(messages, *, n, summarizer):
        if len(messages) <= n:
            return messages
        older = messages[:-n]
        recent = messages[-n:]
        summary = summarizer(older)
        return [Message("system", f"[Önceki konuşma özeti] {summary}")] + recent

This is a deterministic, side-effect-free transformation: the sole
external interaction is the call to ``summarizer`` on the dropped
older messages, which is the seam through which a real LLM-backed
summariser is injected at runtime. Property 14
(``platform/tests/property/test_sliding_window.py``) pins the
following invariants for the deterministic-summariser case:

(a) ``len(messages) <= n`` ⇒ output ``== messages`` (no-op, no
    summariser call).
(b) ``len(messages) > n`` ⇒ ``len(output) == n + 1`` (exactly one
    summary message followed by the ``n`` most recent entries).
(c) The trailing ``n`` elements of the output equal ``messages[-n:]``
    verbatim — original ordering preserved.
(d) The leading element has ``role == "system"`` and its ``text``
    contains the substring ``"[Önceki konuşma özeti]"``.
(e) Determinism: identical inputs (with a deterministic summariser)
    yield identical outputs across invocations.
(f) Empty input ⇒ empty output.

The compressor is intentionally minimal: env wiring (``CHAT_SLIDING
_WINDOW_N=20``), the LLM-backed summariser default, the audit
``sliding_window_summary_failed`` fallback (design Testing Strategy
table) and the boot-time ``poll_loop`` are layered on top by
``src/main.py`` and ``ChatHandler`` (tasks 3.1, 4.4). Keeping
``compress`` pure means the property test can exercise the contract
without any I/O.
"""

from __future__ import annotations

from typing import Callable, Sequence

from messages import Message

__all__ = ["compress", "Summariser", "SUMMARY_PREFIX"]


#: Type alias for the summariser callable consumed by :func:`compress`.
#: Defined locally so this module has no dependency on
#: ``src.chat.handler`` (which already declares an identical alias);
#: the two definitions are intentionally redundant so a future
#: refactor can split either side without breaking the other.
Summariser = Callable[[Sequence[Message]], str]


#: Substring stamped into the leading summary message. Property 14
#: clause (d) asserts membership of this substring in the output's
#: first element. The Turkish phrase mirrors the user-facing prompt
#: in ``platform/prompts/assistant_chat.md`` (task 4.5) and the
#: design pseudocode verbatim.
SUMMARY_PREFIX: str = "[Önceki konuşma özeti]"


def compress(
    messages: Sequence[Message],
    *,
    n: int,
    summarizer: Summariser,
) -> list[Message]:
    """Trim a chat history to the last ``n`` messages plus a summary.

    Args:
        messages: The full chat history in chronological order.
            ``messages[0]`` is the oldest entry.
        n: Number of recent messages to preserve verbatim. MUST be a
            positive integer; the handler validates this at
            construction (``ChatHandlerDeps.sliding_window_n > 0``).
        summarizer: Callable that maps the *older* slice
            (``messages[:-n]``) to a single summary string. Invoked
            only when the history overflows the window, i.e. when
            ``len(messages) > n``. Production wiring uses an
            LLM-backed summariser; the property test injects
            deterministic stand-ins.

    Returns:
        Either ``list(messages)`` unchanged (when ``len(messages) <=
        n``) or a list of length ``n + 1`` whose first element is a
        ``role="system"`` summary message and whose remaining ``n``
        elements equal ``messages[-n:]`` verbatim.

    Validates:
        Requirement 1.7.
    """

    if len(messages) <= n:
        # No-op branch — no summariser call, no allocation surprises.
        # Returning a fresh list keeps the contract stable: callers
        # observe a ``list[Message]`` regardless of the input
        # container (tuple, list, custom Sequence). The element
        # objects are themselves frozen dataclasses so sharing
        # references is safe.
        return list(messages)

    older = list(messages[:-n])
    recent = list(messages[-n:])
    summary = summarizer(older)
    head = Message(role="system", text=f"{SUMMARY_PREFIX} {summary}")
    return [head, *recent]
