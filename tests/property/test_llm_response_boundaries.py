# Feature: platform-quick-fixes, Property 3: LLM Response Boundary Handling
"""Property test 3 — LLM Response Boundary Handling.

**Property 3: LLM Response Boundary Handling**

*For any* LLM response that exceeds ``LLM_MAX_TOKENS_OUTPUT`` tokens,
the SSE stream SHALL terminate with a final event containing
``truncated: true``. *For any* LLM call exceeding
``LLM_REQUEST_TIMEOUT_S`` seconds, the stream SHALL emit
``{"error": "llm_timeout"}`` and write an ``assistant_llm_timeout``
audit event.

**Validates: Requirements 1.7, 1.8**

This file exercises the ChatHandler's truncation and timeout logic
by injecting scripted orchestrator fakes that emit token events with
configurable ``token_out`` counts and configurable delays.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Sequence

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]

_LIB_SRC_DIRS = (
    _REPO_ROOT / "libs" / "audit_logger" / "src",
    _REPO_ROOT / "libs" / "messages" / "src",
    _REPO_ROOT / "libs" / "mcp_client" / "src",
    _REPO_ROOT / "libs" / "pii-shared" / "src",
    _REPO_ROOT / "libs" / "prompts" / "src",
)
for _src in _LIB_SRC_DIRS:
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

_ASSISTANT_SERVICE_ROOT = _REPO_ROOT / "services" / "assistant-service"
if str(_ASSISTANT_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ASSISTANT_SERVICE_ROOT))


# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

from audit_logger import AuditEvent  # noqa: E402
from messages import ChatRequest, Message, SseEvent  # noqa: E402

from src.chat.handler import (  # noqa: E402
    ChatHandler,
    ChatHandlerDeps,
    DEFAULT_SLIDING_WINDOW_N,
    DeptContext,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Strategy for max_tokens_output — small values to keep tests fast.
#: We use values between 1 and 200 to represent the configured cap.
_max_tokens_output = st.integers(min_value=1, max_value=200)

#: Strategy for token counts that EXCEED the max_tokens_output.
#: Given a max, we generate a total token output that exceeds it.
#: The excess is between 1 and 500 tokens above the cap.
_excess_tokens = st.integers(min_value=1, max_value=500)

#: Strategy for timeout_s — small values (1-5 seconds) for testing.
_timeout_s = st.integers(min_value=1, max_value=3)

#: Strategy for the number of token events before exceeding the cap.
#: Each event carries some portion of the total tokens.
_num_events_before_cap = st.integers(min_value=1, max_value=10)


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class _StubPromptLoader:
    """Minimal PromptLoader stand-in."""

    def render(self, name: str, *, vars: Any) -> str:
        return "stub-system-prompt"

    def version(self, name: str) -> str:
        return "stub0001"


@dataclass
class _RecordingAudit:
    """In-memory audit sink — captures every event written."""

    events: list[AuditEvent] = field(default_factory=list)

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _RecordingDispatch:
    """Tracks tool dispatch calls."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def invoke(self, tool_call: Any) -> Any:
        self.calls.append(tool_call)
        return {"ok": True}


class _TokenEmittingOrchestrator:
    """Orchestrator that emits token events with configurable token_out counts.

    Used to test the truncation boundary: the handler should detect when
    cumulative token_out exceeds max_tokens_output and emit a done event
    with truncated: true.
    """

    def __init__(self, token_events: list[SseEvent]) -> None:
        self._events = token_events

    def stream_with_tool_loop(
        self,
        *,
        system: str,
        history: Sequence[Message],
        tools: Sequence[Any],
        on_tool_call: Callable[[Any], Awaitable[Any]],
        token_cap: int,
    ) -> AsyncIterator[SseEvent]:
        async def _gen() -> AsyncIterator[SseEvent]:
            for ev in self._events:
                yield ev

        return _gen()


class _SlowOrchestrator:
    """Orchestrator that introduces a delay before yielding events.

    Used to test the timeout boundary: the handler should detect when
    the LLM call exceeds timeout_s and emit an error event.
    """

    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s

    def stream_with_tool_loop(
        self,
        *,
        system: str,
        history: Sequence[Message],
        tools: Sequence[Any],
        on_tool_call: Callable[[Any], Awaitable[Any]],
        token_cap: int,
    ) -> AsyncIterator[SseEvent]:
        delay = self._delay_s

        async def _gen() -> AsyncIterator[SseEvent]:
            # Simulate a slow LLM response that exceeds the timeout
            await asyncio.sleep(delay)
            yield SseEvent(type="token", payload={"text": "hello", "token_out": 1})
            yield SseEvent(type="done", payload={})

        return _gen()


@dataclass
class _ActorFake:
    actor_id: str = "user-prop"
    actor_role: str = "lead"


def _identity_compress(
    messages: Sequence[Message],
    *,
    n: int,
    summarizer: Callable[[Sequence[Message]], str],
) -> Sequence[Message]:
    """Compressor that returns the input verbatim when it fits."""
    if len(messages) <= n:
        return tuple(messages)
    return tuple(messages[-n:])


def _passthrough_capability_gate(
    tools: Iterable[Any],
    *,
    capabilities: frozenset[str],
) -> Sequence[Any]:
    """Capability gate that allows every tool through."""
    return tuple(tools)


def _build_dept() -> DeptContext:
    return DeptContext(
        dept_id="payment",
        department_repos=("payment-api",),
        capabilities=frozenset({"jira", "bitbucket", "confluence"}),
        default_language="tr",
        bot_username="bot.payment",
    )


def _build_request() -> ChatRequest:
    return ChatRequest(
        user_message="hi",
        history=(),
        dept_id="payment",
        session_id="sess-prop",
    )


def _build_handler(
    orchestrator: Any,
    *,
    max_tokens_output: int = 4096,
    timeout_s: int = 60,
) -> tuple[ChatHandler, _RecordingAudit]:
    """Build a ChatHandler with the given orchestrator and boundary settings."""
    audit = _RecordingAudit()
    dispatch = _RecordingDispatch()
    deps = ChatHandlerDeps(
        prompt_loader=_StubPromptLoader(),  # type: ignore[arg-type]
        compress=_identity_compress,  # type: ignore[arg-type]
        summariser=lambda older: f"summary({len(older)})",
        capability_gate=_passthrough_capability_gate,  # type: ignore[arg-type]
        llm=orchestrator,  # type: ignore[arg-type]
        tool_dispatch=dispatch,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        token_cap=10_000,
        sliding_window_n=DEFAULT_SLIDING_WINDOW_N,
        list_tools=lambda: (),
        max_tokens_output=max_tokens_output,
        timeout_s=timeout_s,
    )
    return ChatHandler(deps), audit


async def _drain(handler: ChatHandler) -> list[SseEvent]:
    """Drain all SSE events from the handler stream."""
    actor = _ActorFake()
    out: list[SseEvent] = []
    async for ev in handler.stream(_build_request(), actor, _build_dept()):
        out.append(ev)
    return out


# ---------------------------------------------------------------------------
# Property 3 — Truncation: max_tokens_output exceeded
# ---------------------------------------------------------------------------


class TestLlmResponseTruncation:
    """**Validates: Requirements 1.8**

    For any LLM response that exceeds ``LLM_MAX_TOKENS_OUTPUT`` tokens,
    the SSE stream SHALL terminate with a final event containing
    ``truncated: true``.
    """

    @settings(
        max_examples=100,
        deadline=4000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        max_tokens=_max_tokens_output,
        excess=_excess_tokens,
        num_events=_num_events_before_cap,
    )
    def test_truncation_emitted_when_tokens_exceed_max(
        self, max_tokens: int, excess: int, num_events: int
    ) -> None:
        """When cumulative token_out exceeds max_tokens_output, the handler
        emits a final ``done`` event with ``truncated: true`` and stops
        the stream.

        We distribute the total tokens (max_tokens + excess) across
        num_events token events. The handler should detect the boundary
        crossing and terminate with truncation.
        """
        total_tokens = max_tokens + excess

        # Distribute tokens across events. Each event carries a portion.
        tokens_per_event = total_tokens // num_events
        remainder = total_tokens % num_events

        events: list[SseEvent] = []
        for i in range(num_events):
            tok = tokens_per_event + (1 if i < remainder else 0)
            events.append(
                SseEvent(
                    type="token",
                    payload={"text": "x", "token_out": tok},
                )
            )
        # Add a trailing done event that the handler should NOT reach
        # (it should truncate before getting here).
        events.append(SseEvent(type="done", payload={}))

        orch = _TokenEmittingOrchestrator(events)
        handler, audit = _build_handler(orch, max_tokens_output=max_tokens)

        result = asyncio.run(_drain(handler))

        # The final event must be a "done" with truncated: true
        assert len(result) > 0, "Stream should emit at least one event"
        final_event = result[-1]
        assert final_event.type == "done", (
            f"Expected final event type 'done', got '{final_event.type}'"
        )
        assert final_event.payload.get("truncated") is True, (
            f"Expected truncated: true in final event payload, "
            f"got {final_event.payload}"
        )

    @settings(
        max_examples=100,
        deadline=4000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        max_tokens=_max_tokens_output,
        excess=_excess_tokens,
    )
    def test_no_events_after_truncation(
        self, max_tokens: int, excess: int
    ) -> None:
        """After the truncation ``done`` event, no further events are
        emitted. The stream terminates cleanly.
        """
        total_tokens = max_tokens + excess

        # Single event that exceeds the cap in one shot
        events: list[SseEvent] = [
            SseEvent(
                type="token",
                payload={"text": "big chunk", "token_out": total_tokens},
            ),
            # These should never be reached
            SseEvent(type="token", payload={"text": "extra", "token_out": 10}),
            SseEvent(type="done", payload={}),
        ]

        orch = _TokenEmittingOrchestrator(events)
        handler, audit = _build_handler(orch, max_tokens_output=max_tokens)

        result = asyncio.run(_drain(handler))

        # Only the first token event + the truncation done event
        types = [e.type for e in result]
        # After the truncation done event, nothing else should appear
        done_indices = [i for i, e in enumerate(result) if e.type == "done"]
        assert len(done_indices) == 1, (
            f"Expected exactly one 'done' event, got {len(done_indices)}"
        )
        done_idx = done_indices[0]
        assert done_idx == len(result) - 1, (
            "The 'done' event should be the last event in the stream"
        )
        assert result[done_idx].payload.get("truncated") is True

    @settings(
        max_examples=100,
        deadline=4000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(max_tokens=st.integers(min_value=2, max_value=200))
    def test_below_cap_no_truncation(self, max_tokens: int) -> None:
        """When cumulative token_out stays strictly below max_tokens_output,
        the stream completes normally without truncation.

        We use max_tokens >= 2 so that (max_tokens - 1) >= 1, ensuring
        at least one token is emitted while staying below the cap.
        The handler's condition is ``token_out >= max_tokens_output``,
        so ``max_tokens - 1`` is guaranteed to be below the threshold.
        """
        # Emit tokens that stay strictly below the cap
        below_cap_tokens = max_tokens - 1

        events: list[SseEvent] = [
            SseEvent(
                type="token",
                payload={"text": "normal", "token_out": below_cap_tokens},
            ),
            SseEvent(type="done", payload={}),
        ]

        orch = _TokenEmittingOrchestrator(events)
        handler, audit = _build_handler(orch, max_tokens_output=max_tokens)

        result = asyncio.run(_drain(handler))

        # The stream should complete with the normal done event
        final_event = result[-1]
        assert final_event.type == "done"
        # No truncation flag (or explicitly false/absent)
        assert final_event.payload.get("truncated") is not True


# ---------------------------------------------------------------------------
# Property 3 — Timeout: LLM_REQUEST_TIMEOUT_S exceeded
# ---------------------------------------------------------------------------


class TestLlmResponseTimeout:
    """**Validates: Requirements 1.7**

    For any LLM call exceeding ``LLM_REQUEST_TIMEOUT_S`` seconds, the
    stream SHALL emit ``{"error": "llm_timeout"}`` and write an
    ``assistant_llm_timeout`` audit event.
    """

    @settings(
        max_examples=100,
        deadline=None,  # Timeout tests involve real sleeps
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(timeout_s=st.just(1))
    def test_timeout_emits_error_event(self, timeout_s: int) -> None:
        """When the LLM call exceeds timeout_s, the handler emits an
        SSE error event with reason 'llm_timeout'.
        """
        # The orchestrator delays longer than the timeout
        delay = timeout_s + 1.0

        orch = _SlowOrchestrator(delay_s=delay)
        handler, audit = _build_handler(orch, timeout_s=timeout_s)

        result = asyncio.run(_drain(handler))

        # The stream should contain exactly one error event
        assert len(result) == 1, (
            f"Expected exactly 1 event (error), got {len(result)}: "
            f"{[e.type for e in result]}"
        )
        error_event = result[0]
        assert error_event.type == "error"
        assert error_event.payload.get("reason") == "llm_timeout"

    @settings(
        max_examples=100,
        deadline=None,  # Timeout tests involve real sleeps
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(timeout_s=st.just(1))
    def test_timeout_writes_audit_event(self, timeout_s: int) -> None:
        """When the LLM call times out, the handler writes an
        ``assistant_llm_timeout`` audit event with the correct payload.
        """
        delay = timeout_s + 1.0

        orch = _SlowOrchestrator(delay_s=delay)
        handler, audit = _build_handler(orch, timeout_s=timeout_s)

        asyncio.run(_drain(handler))

        # Exactly one audit event should be written
        assert len(audit.events) == 1, (
            f"Expected 1 audit event, got {len(audit.events)}"
        )
        audit_event = audit.events[0]
        assert audit_event.action == "assistant_llm_timeout"
        assert audit_event.result == "error"
        assert audit_event.dept_id == "payment"
        assert audit_event.payload is not None
        assert audit_event.payload["timeout_s"] == timeout_s

    @settings(
        max_examples=100,
        deadline=None,  # Timeout tests involve real sleeps
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(timeout_s=st.just(1))
    def test_no_timeout_when_within_limit(self, timeout_s: int) -> None:
        """When the LLM responds within timeout_s, no timeout error
        is emitted and the stream completes normally.
        """
        # Use a fast orchestrator that responds immediately
        events: list[SseEvent] = [
            SseEvent(type="token", payload={"text": "fast", "token_out": 5}),
            SseEvent(type="done", payload={}),
        ]
        orch = _TokenEmittingOrchestrator(events)
        handler, audit = _build_handler(orch, timeout_s=timeout_s)

        result = asyncio.run(_drain(handler))

        # No error event
        types = [e.type for e in result]
        assert "error" not in types
        # Normal completion
        assert result[-1].type == "done"
        # No timeout audit event
        timeout_audits = [
            e for e in audit.events if e.action == "assistant_llm_timeout"
        ]
        assert len(timeout_audits) == 0
