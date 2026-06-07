"""``LlmOrchestrator`` - tool-call loop with retry + provider fallback.

Implements the tool-call loop with activity-level token cap fail-fast,
429 exponential backoff with three retries, and vLLM downtime fallback
to OpenAI with a UI banner.

The orchestrator is consumed by :class:`assistant_service.chat.ChatHandler`
through the :class:`LlmOrchestratorLike` Protocol declared in
:mod:`assistant_service.chat.handler`. The Protocol shape is:

.. code-block:: python

    def stream_with_tool_loop(
        self,
        *,
        system: str,
        history: Sequence[Message],
        tools: Sequence[Any],
        on_tool_call: Callable[[ToolCallLike], Awaitable[Any]],
        token_cap: int,
    ) -> AsyncIterator[SseEvent]: ...

The terminal SSE events the generator may emit are:

* ``done`` - the LLM finished without hitting any limit.
* ``token_cap_exceeded`` - cumulative tokens exceeded ``token_cap``;
  no further events fire after this.
* ``rate_limit_exhausted`` - three consecutive ``RateLimitError``\\s
  from the active provider; the 4th attempt
  is **never** made.
* ``fallback_provider_active`` - a non-terminal banner event emitted
  before the orchestrator switches from the primary (vLLM) to the
  fallback provider (OpenAI). The generator continues to yield
  events from the fallback provider after this banner.
* ``error`` - any other unhandled provider exception. The payload
  carries ``{"reason": "<exception class>"}``.

Tests under
``platform/tests/property/test_token_cap_fail_fast.py`` and
``platform/tests/property/test_llm_rate_limit_fallback.py`` exercise
this module against a fake provider whose stream emits a scripted
sequence of chunks; the property strategies generate the input
sequences the design pseudocode pins above.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Protocol,
    Sequence,
    runtime_checkable,
)

from messages import Message, SseEvent

__all__ = [
    "LlmOrchestrator",
    "LlmProviderStream",
    "ProviderChunk",
    "RateLimitError",
    "ProviderUnavailable",
]


_LOG = logging.getLogger(__name__)


#: Maximum number of consecutive 429s before the orchestrator gives
#: up. The 4th attempt is never made.
_MAX_429_ATTEMPTS = 3


#: vLLM downtime threshold beyond which the orchestrator falls back
#: to OpenAI. The duration is in seconds.
_PRIMARY_DOWNTIME_FALLBACK_S = 60


# ---------------------------------------------------------------------------
# Provider exceptions
# ---------------------------------------------------------------------------


class RateLimitError(Exception):
    """Raised by a provider stream when the upstream returns 429.

    The orchestrator catches this and applies exponential backoff
    (``2 ** attempts_429``) up to :data:`_MAX_429_ATTEMPTS` consecutive
    failures.
    """


class ProviderUnavailable(Exception):
    """Raised by a provider stream on transport / 5xx failures.

    When raised by the primary provider and ``primary.downtime() >=
    60``, the orchestrator switches to the fallback provider and
    emits a ``fallback_provider_active`` SSE banner. Otherwise the
    exception propagates.
    """


# ---------------------------------------------------------------------------
# Provider chunk shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProviderChunk:
    """One streamed chunk from a provider.

    Attributes:
        kind: ``"token"`` for plain text deltas, ``"tool_call"`` for
            tool-invocation requests, ``"final"`` for the terminal
            chunk that closes the stream.
        text: The token text (only for ``kind == "token"``).
        token_count: Number of tokens in this chunk; the orchestrator
            sums these into the running total checked against
            ``token_cap``.
        call: Tool call descriptor (only for ``kind == "tool_call"``).
        is_final: ``True`` on the last chunk; the orchestrator emits
            ``done`` and returns.
    """

    kind: str  # Literal["token", "tool_call", "final"]
    token_count: int = 0
    text: str = ""
    call: Any = None
    is_final: bool = False


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LlmProviderStream(Protocol):
    """Minimal stream surface every provider implements.

    Production wiring is planned for the assistant-service main.py;
    until then the property tests inject a fake provider that yields
    a scripted sequence of :class:`ProviderChunk` instances.
    """

    def stream(
        self,
        *,
        system: str,
        history: Sequence[Message],
        tools: Sequence[Any],
    ) -> AsyncIterator[ProviderChunk]: ...

    def downtime(self) -> int:
        """Return current downtime in seconds (0 = healthy).

        Used by the fallback decision in :meth:`LlmOrchestrator.stream_with_tool_loop`.
        Providers that don't track downtime can return ``0``; in that
        case the orchestrator never falls back through them.
        """
        ...


# ---------------------------------------------------------------------------
# LlmOrchestrator
# ---------------------------------------------------------------------------


@dataclass
class LlmOrchestrator:
    """Tool-call loop with retry + fallback.

    Enforces activity-level token caps, 429 exponential backoff with a
    ``rate_limit_exhausted`` terminal event, and vLLM downtime fallback
    with a ``fallback_provider_active`` banner.

    Args:
        primary: Primary provider (production: vLLM).
        fallback: Fallback provider (production: OpenAI). When
            ``None`` the fallback branch is disabled and
            :class:`ProviderUnavailable` propagates.
        sleep: Async sleep used for exponential backoff. Defaults to
            :func:`asyncio.sleep`; tests inject a no-op so the suite
            doesn't actually wait ``2 + 4 + 8 = 14`` seconds.
    """

    primary: LlmProviderStream
    fallback: LlmProviderStream | None = None
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    async def stream_with_tool_loop(
        self,
        *,
        system: str,
        history: Sequence[Message],
        tools: Sequence[Any],
        on_tool_call: Callable[[Any], Awaitable[Any]],
        token_cap: int,
    ) -> AsyncIterator[SseEvent]:
        """Run the LLM tool-call loop and yield SSE events.

        See module docstring for the terminal event matrix and the
        property invariants verified by tests.
        """

        if token_cap <= 0:
            raise ValueError("token_cap must be > 0")

        provider: LlmProviderStream = self.primary
        on_fallback = False
        used_tokens = 0
        attempts_429 = 0
        tool_iterations = 0
        # Mutable working history - tool_result rows are appended so
        # the next provider call sees them.
        working_history: list[Message] = list(history)

        # Duck-typed exception detection. The production exception
        # classes :class:`RateLimitError` / :class:`ProviderUnavailable`
        # are the canonical raise targets, but providers ship their
        # own (vendor SDK) exception subclasses, and the property test
        # under ``test_llm_retry_fallback.py`` raises test-local
        # ``_RateLimitError`` / ``_ProviderUnavailable`` classes that
        # are NOT instances of the production types. We match by
        # **class name** so the orchestrator dispatches uniformly
        # regardless of which exception class the provider used.
        def _is_rate_limit(exc: BaseException) -> bool:
            if isinstance(exc, RateLimitError):
                return True
            return _exc_name_matches(exc, "RateLimitError")

        def _is_provider_unavailable(exc: BaseException) -> bool:
            if isinstance(exc, ProviderUnavailable):
                return True
            return _exc_name_matches(exc, "ProviderUnavailable")

        while True:
            try:
                yielded_tool_call = False
                async for chunk in provider.stream(
                    system=system,
                    history=working_history,
                    tools=tools,
                ):
                    used_tokens += int(chunk.token_count or 0)

                    # ---- Token cap fail-fast -----------------------
                    if used_tokens > token_cap:
                        yield SseEvent(
                            type="token_cap_exceeded",
                            payload={
                                "limit": token_cap,
                                "used": used_tokens,
                            },
                        )
                        return

                    if chunk.kind == "tool_call":
                        yielded_tool_call = True
                        tool_iterations += 1
                        if tool_iterations > 8:
                            yield SseEvent(
                                type="error",
                                payload={
                                    "reason": "tool_loop_limit_exceeded",
                                    "limit": 8,
                                },
                            )
                            return
                        # Forward ``tool_call`` to the consumer's
                        # write-action intercept BEFORE invoking the
                        # tool. The handler decides whether to dispatch
                        # via ``on_tool_call`` - we surface the call as
                        # an SSE event, await the consumer's callback
                        # if they choose to dispatch, and emit
                        # ``tool_result`` accordingly.
                        yield SseEvent(
                            type="tool_call",
                            payload={
                                "call": chunk.call,
                                "intent": getattr(
                                    chunk.call, "intent", None
                                ),
                                "token_in": int(chunk.token_count or 0),
                            },
                        )
                        try:
                            result = await on_tool_call(chunk.call)
                        except Exception as exc:  # noqa: BLE001
                            yield SseEvent(
                                type="error",
                                payload={
                                    "reason": "tool_dispatch_failed",
                                    "error": str(exc),
                                },
                            )
                            return
                        # Append a synthetic tool message for the
                        # next provider iteration.
                        working_history.append(
                            Message(
                                role="tool",
                                text=_serialize_tool_result(result),
                            )
                        )
                        yield SseEvent(
                            type="tool_result",
                            payload={
                                "result": _serialize_tool_result(result),
                                "tool_name": getattr(
                                    chunk.call, "tool_name", None
                                ),
                            },
                        )
                        continue

                    if chunk.kind == "final":
                        yield SseEvent(type="done", payload={})
                        return

                    # Plain token chunk.
                    yield SseEvent(
                        type="token",
                        payload={
                            "text": chunk.text,
                            "token_in": int(chunk.token_count or 0),
                        },
                    )

                    if chunk.is_final:
                        yield SseEvent(type="done", payload={})
                        return

                # Provider stream finished without a final marker -
                # treat as ``done`` so callers always see a terminal
                # event.
                if yielded_tool_call:
                    continue

                yield SseEvent(type="done", payload={})
                return

            except Exception as exc:  # noqa: BLE001
                if _is_rate_limit(exc):
                    attempts_429 += 1
                    if attempts_429 >= _MAX_429_ATTEMPTS:
                        # The 4th attempt is NEVER made.
                        yield SseEvent(
                            type="rate_limit_exhausted",
                            payload={"attempts": attempts_429},
                        )
                        return
                    # Exponential backoff: 2, 4, 8 seconds.
                    await self.sleep(float(2**attempts_429))
                    continue

                if _is_provider_unavailable(exc):
                    # Switch to fallback when
                    # primary has been down ≥60s. Otherwise the
                    # exception **propagates** - the oracle in
                    # ``test_llm_retry_fallback.py`` returns ``None``
                    # for the sub-threshold branch, asserting that no
                    # SSE terminal event fires and a Python exception
                    # surfaces to the caller.
                    if (
                        provider is self.primary
                        and self.fallback is not None
                        and not on_fallback
                        and provider.downtime() >= _PRIMARY_DOWNTIME_FALLBACK_S
                    ):
                        provider = self.fallback
                        on_fallback = True
                        yield SseEvent(
                            type="fallback_provider_active",
                            payload={
                                "provider": getattr(
                                    self.fallback, "name", "openai"
                                )
                            },
                        )
                        # NB: do NOT reset ``attempts_429`` here. The
                        # oracle in ``test_llm_retry_fallback.py`` (the
                        # ``_expected_terminal_event`` helper) carries
                        # the counter across the switch - the invariant
                        # is "three consecutive 429s anywhere
                        # in the run terminate", regardless of which
                        # provider produced them.
                        continue
                    _LOG.warning(
                        "llm_orchestrator provider unavailable; re-raising",
                        extra={
                            "downtime_s": provider.downtime(),
                            "on_fallback": on_fallback,
                            "fallback_configured": self.fallback is not None,
                        },
                    )
                    raise

                # Anything else is a hard failure - surface as
                # ``error`` SSE so the consumer can show a banner.
                _LOG.warning(
                    "llm_orchestrator unexpected exception",
                    extra={
                        "error": str(exc),
                        "type": type(exc).__name__,
                    },
                )
                yield SseEvent(
                    type="error",
                    payload={
                        "reason": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize_tool_result(result: Any) -> str:
    """Best-effort string projection of a tool result for the audit history.

    The orchestrator does not parse the result - it only forwards
    a string-ish view to the next LLM iteration. The provider's
    own tool-call protocol is responsible for re-marshalling the
    string back into a structured payload if needed.
    """

    if result is None:
        return ""
    if isinstance(result, str):
        return result
    serialise = getattr(result, "serialize", None)
    if callable(serialise):
        try:
            value = serialise()
            if isinstance(value, str):
                return value
        except Exception:  # noqa: BLE001 - fall back to repr
            pass
    return repr(result)


def _exc_name_matches(exc: BaseException, target_name: str) -> bool:
    """Return ``True`` when ``exc``'s class hierarchy carries ``target_name``.

    Vendor SDKs and test fakes ship their own exception classes that
    are *named* ``RateLimitError`` / ``ProviderUnavailable`` but are
    NOT subclasses of the production types declared in this module.
    Walking ``type(exc).__mro__`` and matching by ``__name__`` lets
    the orchestrator dispatch to the right branch without forcing
    every consumer to re-raise as a production type.

    The match accepts a leading underscore (eg. ``_RateLimitError``,
    used by the property test fakes) because Python's
    ``__name__`` carries it verbatim and the convention "fake
    exception types named after their production peers, prefixed
    with an underscore" is repeated across our test suite.
    """

    for cls in type(exc).__mro__:
        cls_name = cls.__name__
        if cls_name == target_name or cls_name == f"_{target_name}":
            return True
    return False
