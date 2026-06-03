"""``ChatHandler.stream`` — SSE pipeline for ``POST /api/chat/stream``.

This module wires the deterministic chat tool-call loop.

The handler is the **single funnel** every chat request flows
through. It executes a fixed six-step pipeline:

1. **PII mask** — :func:`pii_shared.mask` is invoked on the
   user-provided text *before* any other processing.
2. **Sliding window** — the history + masked message is trimmed to
   the last ``CHAT_SLIDING_WINDOW_N`` (default 20) entries with a
   summary system prefix.
3. **System prompt render** — ``prompt_loader.render("assistant_chat",
   vars=...)`` injects the dept-scoped template variables into the
   git-tracked system prompt.
4. **Tool filter** — ``mcp_client.filter_tools`` strips the foundation
   banned-tool list (``bitbucket_merge_pr``,
   ``confluence_delete_page``); ``capability_gate`` then narrows the
   catalogue to the dept's capability set.
5. **LLM tool-call loop** — ``llm.stream_with_tool_loop(...)`` yields
   SSE events. The handler intercepts each ``tool_call`` event:
   * If :func:`is_write_intent` returns ``True`` it emits a
     ``redirect_to_task_creator`` event and **stops** without calling
      ``tool_dispatch.invoke``.
   * Otherwise the call is dispatched and a ``tool_result`` event is
     yielded (the orchestrator continues the loop until ``done`` /
     ``rate_limit_exhausted`` / ``token_cap_exceeded``).
6. **Audit** — a ``chat_message`` row is written with the prompt's
   git short hash, token usage and cost.

The collaborators (sliding-window compressor, LLM orchestrator, tool
dispatch, audit logger, prompt loader, summariser) are typed against
small :class:`~typing.Protocol` interfaces so the handler can be
exercised without standing up vLLM, Postgres or Vault. Concrete
the pipeline is validated against fakes
because every collaborator is reachable through these protocols.

* The PII mask call **must** be the first thing the handler does to
  the user-provided text. Tests confirm that ``pii_filter.mask`` is
  invoked *before* any reference to ``llm.stream_with_tool_loop``.
  The implementation below keeps the two calls in the same function
  body so the AST tooling can read the source-order relationship
  directly.
* The handler **does not** call ``tool_dispatch.invoke`` directly
  inside ``stream``; the LLM orchestrator owns the tool dispatch
  callback and the handler only feeds it through. The intercept for
  write actions is therefore a pre-dispatch gate the orchestrator
  consults, *not* a post-dispatch rollback.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from audit_logger import AuditEvent, AuditLogger
from mcp_client import filter_tools as _filter_banned_tools
from messages import ChatRequest, Message, SseEvent
from pii_shared import PiiMatch, mask as pii_mask
from prompts import PromptLoader, PromptVars

from .write_action import ToolCallLike, is_write_intent

__all__ = [
    "ChatHandler",
    "ChatHandlerDeps",
    "DeptContext",
    "DEFAULT_SLIDING_WINDOW_N",
    "Summariser",
    "SlidingWindowCompressor",
    "LlmOrchestratorLike",
    "ToolDispatchLike",
    "CapabilityGateLike",
    "TokenAccounting",
]


_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------


#: Default number of chat messages kept in the LLM context window.
#: The default is 20; deployments may lower
#: it via the ``CHAT_SLIDING_WINDOW_N`` env var (read by
#: ``src.config.Settings`` in a future task) and pass the value into
#: the :class:`ChatHandler` constructor.
DEFAULT_SLIDING_WINDOW_N: int = 20


# ---------------------------------------------------------------------------
# Collaborator protocols
# ---------------------------------------------------------------------------


#: Type alias for the summariser callable consumed by sliding-window
#: compression. Returns the summary text that replaces the dropped
#: older messages.
Summariser = Callable[[Sequence[Message]], str]


@runtime_checkable
class SlidingWindowCompressor(Protocol):
    """Pure compressor matching :func:`compress`.

    The concrete implementation lives under ``src/chat/sliding_window.py``;
    any callable with this shape works for tests that inject lambdas.
    """

    def __call__(
        self,
        messages: Sequence[Message],
        *,
        n: int,
        summarizer: Summariser,
    ) -> Sequence[Message]:
        ...


@runtime_checkable
class CapabilityGateLike(Protocol):
    """Foundation capability gate.

    Narrows a tool catalogue to the dept's capability subset. The concrete
    gate lives in the foundation lib and is wired in via ``ChatHandlerDeps``.
    """

    def __call__(
        self,
        tools: Iterable[Any],
        *,
        capabilities: frozenset[str],
    ) -> Sequence[Any]:
        ...


@runtime_checkable
class ToolDispatchLike(Protocol):
    """Tool dispatcher invoked by the LLM orchestrator's callback path.

    The handler does **not** call ``invoke`` directly — it forwards the
    bound coroutine to ``llm.stream_with_tool_loop(on_tool_call=...)``.
    The protocol is exposed so :class:`ChatHandlerDeps` can carry the
    dispatcher in one place and the property tests can swap a fake.
    """

    async def invoke(self, tool_call: ToolCallLike) -> Any:  # pragma: no cover - protocol
        ...


@runtime_checkable
class LlmOrchestratorLike(Protocol):
    """Mirror of :class:`llm_orchestrator.LlmOrchestrator`.

    The handler depends only on the ``stream_with_tool_loop`` async
    generator surface; the concrete retry / fallback / token-cap
    logic lives behind this protocol so pipeline tests
    suite can stand in a fake orchestrator that emits a scripted
    sequence of SSE events.
    """

    def stream_with_tool_loop(
        self,
        *,
        system: str,
        history: Sequence[Message],
        tools: Sequence[Any],
        on_tool_call: Callable[[ToolCallLike], Awaitable[Any]],
        token_cap: int,
    ) -> AsyncIterator[SseEvent]:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeptContext:
    """Snapshot of dept-scoped data needed to render the system prompt.

    The handler does not pull this from a config store on every call
    — FastAPI dependency wiring builds it once per request from
    the OIDC ``AuthContext`` plus the ``departments.json`` lookup,
    and hands the frozen value to :meth:`ChatHandler.stream`.
    """

    dept_id: str
    department_repos: tuple[str, ...]
    capabilities: frozenset[str]
    default_language: str  # Literal["tr", "en"] at the call site
    bot_username: str


@dataclass(frozen=True, slots=True)
class TokenAccounting:
    """Cumulative token / cost counters for the audit row.

    The orchestrator updates these as it streams; the handler reads
    the final values when writing the ``chat_message`` audit event.
    Kept as a separate value object so the property tests can assert
    on the exact payload without parsing the audit JSON.
    """

    token_in: int = 0
    token_out: int = 0
    cost_usd: float = 0.0


@dataclass(slots=True)
class _StreamCounters:
    """Mutable counters scoped to a single :meth:`ChatHandler.stream` call.

    The handler reads the final state to populate the audit row; the
    orchestrator's SSE payloads are the source of truth for the
    increments. Separate from :class:`TokenAccounting` because the
    public dataclass stays frozen for the property tests.
    """

    token_in: int = 0
    token_out: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    pii_matches: int = 0
    write_intent_redirected: bool = False
    intent_emitted: bool = False

    def to_accounting(self) -> TokenAccounting:
        return TokenAccounting(
            token_in=self.token_in,
            token_out=self.token_out,
            cost_usd=self.cost_usd,
        )


@dataclass(frozen=True)
class ChatHandlerDeps:
    """Bundle of collaborator references the handler needs at construction.

    Production wiring (``src/main.py`` startup) builds this once and
    reuses it across requests. Tests build a fake
    bundle inline.

    Attributes:
        prompt_loader: :class:`prompts.PromptLoader` instance whose
            ``poll_loop`` is launched at service boot.
        compress: :func:`sliding_window.compress`.
        summariser: Summariser callable invoked by ``compress`` for
            dropped older messages.
        capability_gate: Foundation capability gate.
        llm: :class:`LlmOrchestratorLike`.
        tool_dispatch: :class:`ToolDispatchLike` consumed by the
            orchestrator's ``on_tool_call`` callback.
        audit: :class:`audit_logger.AuditLogger`.
        token_cap: Activity-level token cap forwarded to the
            orchestrator.
        sliding_window_n: Number of recent messages kept by the
            compressor; defaults to :data:`DEFAULT_SLIDING_WINDOW_N`.
        prompt_name: Name of the system prompt loaded for every chat
            (defaults to ``"assistant_chat"``).
    """

    prompt_loader: PromptLoader
    compress: SlidingWindowCompressor
    summariser: Summariser
    capability_gate: CapabilityGateLike
    llm: LlmOrchestratorLike
    tool_dispatch: ToolDispatchLike
    audit: AuditLogger
    token_cap: int
    sliding_window_n: int = DEFAULT_SLIDING_WINDOW_N
    prompt_name: str = "assistant_chat"
    # The MCP catalogue accessor — typed as a callable so the handler
    # is decoupled from the concrete client. ``list_tools()`` returns
    # the full tool descriptors before banned-tool / capability-gate
    # filtering. The foundation ``mcp_client`` is wired here.
    list_tools: Callable[
        [],
        Iterable[Any] | Awaitable[Iterable[Any]],
    ] = field(default=tuple)
    # Timeout in seconds for the LLM request. When the LLM call
    # exceeds this duration the handler aborts, writes an
    # ``assistant_llm_timeout`` audit event, and emits an SSE error
    # event.
    timeout_s: int = 60
    # Maximum number of output tokens allowed from the LLM response.
    # When the cumulative token output exceeds this threshold the
    # handler closes the stream cleanly and emits a final ``done``
    # event with ``truncated: true``.
    max_tokens_output: int = 4096


# ---------------------------------------------------------------------------
# Actor context (subset of ``auth_shared.AuthContext`` we actually use)
# ---------------------------------------------------------------------------


class _ActorLike(Protocol):
    """Structural subset of :class:`auth_shared.AuthContext`.

    The handler reads three attributes for the audit row; using a
    structural type keeps the test fakes minimal.
    """

    @property
    def actor_id(self) -> str:  # pragma: no cover - protocol
        ...

    @property
    def actor_role(self) -> str:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# ChatHandler
# ---------------------------------------------------------------------------


class ChatHandler:
    """SSE chat endpoint handler.

    Handles the HTTP/SSE chat endpoint, banned-tool filtering,
    write-action redirects, system prompt injection, PII masking,
    token-cap handling, sliding-window compression, audit emission,
    LLM retry handling, and fallback banners.
    """

    def __init__(self, deps: ChatHandlerDeps) -> None:
        if deps.token_cap <= 0:
            raise ValueError(
                "ChatHandlerDeps.token_cap must be a positive integer "
                "(activity-level token cap)."
            )
        if deps.sliding_window_n <= 0:
            raise ValueError(
                "ChatHandlerDeps.sliding_window_n must be positive "
                "(stream timeout)."
            )
        self._deps = deps

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def stream(
        self,
        request: ChatRequest,
        actor: _ActorLike,
        dept: DeptContext,
    ) -> AsyncIterator[SseEvent]:
        """Run the six-step pipeline and yield SSE events.

        Args:
            request: The :class:`messages.ChatRequest` carried in the
                HTTP body.
            actor: Authenticated caller (typically
                :class:`auth_shared.AuthContext`); only ``actor_id``
                and ``actor_role`` are read.
            dept: :class:`DeptContext` snapshot for the dept the
                request is scoped to. The dept's ``capabilities`` are
                applied to the tool catalogue and its template
                variables drive prompt rendering.

        Yields:
            :class:`messages.SseEvent` instances. The terminal event
            is one of ``done``, ``rate_limit_exhausted``,
            ``token_cap_exceeded``, ``redirect_to_task_creator`` or
            ``error`` (whichever the orchestrator emits first, except
            for the write-intent intercept which is forced by this
            handler).
        """

        deps = self._deps
        counters = _StreamCounters()

        # ------------------------------------------------------------------
        # 1. PII mask — MUST be the first thing the handler does to
        #    user-provided text.
        # ------------------------------------------------------------------
        masked_text, pii_matches = pii_mask(request.user_message)
        counters.pii_matches = len(pii_matches)
        if pii_matches:
            _LOG.info(
                "chat.pii_masked",
                extra={
                    "actor_id": actor.actor_id,
                    "dept_id": dept.dept_id,
                    "matches": _summarise_pii(pii_matches),
                },
            )

        # ------------------------------------------------------------------
        # 2. Sliding window — append the masked user message to the
        #    history, then trim to ``sliding_window_n``.
        # ------------------------------------------------------------------
        history_with_user: tuple[Message, ...] = (
            *request.history,
            Message(role="user", text=masked_text),
        )
        compressed: Sequence[Message] = deps.compress(
            history_with_user,
            n=deps.sliding_window_n,
            summarizer=deps.summariser,
        )

        # ------------------------------------------------------------------
        # 3. System prompt render — load (cache hit after boot) +
        #    inject dept template variables.
        # ------------------------------------------------------------------
        prompt_vars = PromptVars(
            department_id=dept.dept_id,
            department_repos=dept.department_repos,
            capabilities=dept.capabilities,
            default_language=dept.default_language,  # type: ignore[arg-type]
            bot_username=dept.bot_username,
        )
        system_prompt = deps.prompt_loader.render(
            deps.prompt_name, vars=prompt_vars
        )
        prompt_version = deps.prompt_loader.version(deps.prompt_name)

        # ------------------------------------------------------------------
        # 4. Tool filter — banned-tool list
        #    THEN dept capability gate. Banned-tool filtering is the
        #    single source of truth in ``mcp_client.filter_tools``;
        #    no literal ``BANNED_TOOLS`` membership is hard-coded here.
        # ------------------------------------------------------------------
        raw_catalogue = deps.list_tools()
        if inspect.isawaitable(raw_catalogue):
            catalogue = await raw_catalogue
        else:
            catalogue = raw_catalogue
        de_banned = _filter_banned_tools(catalogue)
        gated_tools = tuple(
            deps.capability_gate(de_banned, capabilities=dept.capabilities)
        )

        # ------------------------------------------------------------------
        # 5. LLM tool-call loop — the orchestrator owns retry,
        #    fallback, token cap. The handler intercepts ``tool_call``
        #    events for the write-action gate and
        #    forwards everything else verbatim.
        #
        #    Timeout: The entire streaming loop is
        #    guarded by ``asyncio.timeout`` using
        #    ``deps.timeout_s``. If the LLM call exceeds the
        #    configured timeout, the handler aborts, writes an
        #    ``assistant_llm_timeout`` audit event, and emits an SSE
        #    error event.
        #
        #    Truncation: The handler tracks
        #    cumulative output tokens. When the count exceeds
        #    ``deps.max_tokens_output``, the stream is closed cleanly
        #    and a final ``done`` event with ``truncated: true`` is
        #    emitted.
        # ------------------------------------------------------------------
        try:
            async with asyncio.timeout(deps.timeout_s):
                async for event in deps.llm.stream_with_tool_loop(
                    system=system_prompt,
                    history=compressed,
                    tools=gated_tools,
                    on_tool_call=deps.tool_dispatch.invoke,
                    token_cap=deps.token_cap,
                ):
                    # 5a. Write-action intercept — the orchestrator surfaces
                    #     each tool call before dispatch via a ``tool_call``
                    #     event. We inspect the call and the LLM-supplied
                    #     intent field; when either flags a write action we
                    #     emit the redirect event and **stop** the generator
                    #     without forwarding the tool_call event. The
                    #     orchestrator's ``on_tool_call`` callback is the
                    #     contract that actually dispatches the tool, so
                    #     simply returning here also prevents
                    #     ``tool_dispatch.invoke`` from running for this
                    #     call.
                    if event.type == "tool_call" and _is_write_call(event):
                        counters.write_intent_redirected = True
                        yield SseEvent(
                            type="redirect_to_task_creator",
                            payload=_redirect_payload(event),
                        )
                        # Audit before returning so the redirect is on the
                        # ledger even when the user closes the SSE stream.
                        await self._write_audit(
                            actor=actor,
                            dept=dept,
                            counters=counters,
                            prompt_version=prompt_version,
                            result="ok",
                        )
                        return

                    # 5b. Token / cost accounting — the orchestrator emits the
                    #     token usage on every event payload; we keep a
                    #     running tally for the audit row.
                    _accumulate_tokens(event, counters)

                    # 5b-ii. Truncation check: if
                    #     cumulative output tokens exceed the configured
                    #     max, close the stream cleanly with a ``done``
                    #     event carrying ``truncated: true``.
                    if counters.token_out >= deps.max_tokens_output:
                        _LOG.info(
                            "chat.truncated",
                            extra={
                                "actor_id": actor.actor_id,
                                "dept_id": dept.dept_id,
                                "token_out": counters.token_out,
                                "max_tokens_output": deps.max_tokens_output,
                            },
                        )
                        yield SseEvent(
                            type="done",
                            payload={"truncated": True},
                        )
                        await self._write_audit(
                            actor=actor,
                            dept=dept,
                            counters=counters,
                            prompt_version=prompt_version,
                            result="ok",
                        )
                        return

                    yield event

                    # 5c. Terminal events — ``done`` /
                    #     ``rate_limit_exhausted`` / ``token_cap_exceeded`` /
                    #     ``error`` close the loop. The orchestrator already
                    #     stops the iteration after these, but we exit
                    #     defensively in case the protocol implementation
                    #     keeps yielding.
                    if event.type in _TERMINAL_EVENT_TYPES:
                        # 5d. Intent detection — when the LLM stream ends with
                        #     ``done`` and the payload carries
                        #     ``intent == "write_action_requested"``, emit an
                        #     additional ``intent`` SSE event before closing.
                        #     Emit the intent event so the assistant-service
                        #     publishes the intent + prefill payload for the
                        #     Streamlit chat page redirect.
                        if event.type == "done":
                            intent_event = _extract_intent_event(event)
                            if intent_event is not None:
                                counters.intent_emitted = True
                                yield intent_event
                        break

        except (asyncio.TimeoutError, TimeoutError):
            # Timeout handling: LLM call exceeded
            # ``deps.timeout_s`` seconds. Abort, write audit event,
            # and emit SSE error.
            _LOG.warning(
                "chat.llm_timeout",
                extra={
                    "actor_id": actor.actor_id,
                    "dept_id": dept.dept_id,
                    "timeout_s": deps.timeout_s,
                },
            )
            # Write the ``assistant_llm_timeout`` audit event.
            await self._write_timeout_audit(
                actor=actor,
                dept=dept,
                counters=counters,
                prompt_version=prompt_version,
            )
            yield SseEvent(
                type="error",
                payload={"reason": "llm_timeout"},
            )
            return

        # ------------------------------------------------------------------
        # 6. Audit ``chat_message``. The mandatory payload fields are
        #    ``prompt_version``, ``token_in``, ``token_out``,
        #    ``cost_usd``; we add ``pii_matches_count`` and
        #    ``tool_calls`` for ops visibility.
        # ------------------------------------------------------------------
        await self._write_audit(
            actor=actor,
            dept=dept,
            counters=counters,
            prompt_version=prompt_version,
            result="ok",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _write_audit(
        self,
        *,
        actor: _ActorLike,
        dept: DeptContext,
        counters: _StreamCounters,
        prompt_version: str,
        result: str,
    ) -> None:
        """Persist the ``chat_message`` audit row.

        The :class:`audit_logger.AuditLogger` enforces the mandatory
        ``actor_role`` invariant; we just hand it the populated event.
        """

        payload: dict[str, Any] = {
            "prompt_version": prompt_version,
            "token_in": counters.token_in,
            "token_out": counters.token_out,
            "cost_usd": counters.cost_usd,
            "pii_matches_count": counters.pii_matches,
            "tool_calls": counters.tool_calls,
            "write_intent_redirected": counters.write_intent_redirected,
            "intent_emitted": counters.intent_emitted,
        }
        event = AuditEvent(
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,  # type: ignore[arg-type]
            dept_id=dept.dept_id,
            action="chat_message",
            resource=f"department:{dept.dept_id}",
            result=result,  # type: ignore[arg-type]
            timestamp=datetime.now(tz=timezone.utc),
            payload=payload,
        )
        try:
            await self._deps.audit.write(event)
        except Exception as exc:  # noqa: BLE001 — fail-soft per ops policy
            # Audit write failure must not break the SSE response;
            # ``audit_logger`` itself raises only on contract
            # violations (eg. missing actor_role), which would be a
            # programmer error worth surfacing in logs but never
            # masking the LLM output from the user.
            _LOG.warning(
                "chat_message audit write failed; SSE stream completed",
                extra={
                    "actor_id": actor.actor_id,
                    "dept_id": dept.dept_id,
                    "error": str(exc),
                },
            )

    async def _write_timeout_audit(
        self,
        *,
        actor: _ActorLike,
        dept: DeptContext,
        counters: _StreamCounters,
        prompt_version: str,
    ) -> None:
        """Persist the ``assistant_llm_timeout`` audit event.

        Written when the LLM call exceeds ``deps.timeout_s`` seconds.
        Separate from :meth:`_write_audit` because the action and
        payload differ from the normal ``chat_message`` row.
        """

        payload: dict[str, Any] = {
            "prompt_version": prompt_version,
            "timeout_s": self._deps.timeout_s,
            "token_in": counters.token_in,
            "token_out": counters.token_out,
        }
        event = AuditEvent(
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,  # type: ignore[arg-type]
            dept_id=dept.dept_id,
            action="assistant_llm_timeout",
            resource=f"department:{dept.dept_id}",
            result="error",  # type: ignore[arg-type]
            timestamp=datetime.now(tz=timezone.utc),
            payload=payload,
        )
        try:
            await self._deps.audit.write(event)
        except Exception as exc:  # noqa: BLE001 — fail-soft per ops policy
            _LOG.warning(
                "assistant_llm_timeout audit write failed",
                extra={
                    "actor_id": actor.actor_id,
                    "dept_id": dept.dept_id,
                    "error": str(exc),
                },
            )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


_TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "done",
        "rate_limit_exhausted",
        "token_cap_exceeded",
        "error",
        # ``redirect_to_task_creator`` is handled inline above.
    }
)


def _summarise_pii(matches: list[PiiMatch]) -> Mapping[str, int]:
    """Return ``{kind: count}`` for the PII matches.

    Used only for structured logging — never written to the audit
    payload (the audit row carries the *count* only, never the kinds,
    so a redacted value cannot be inferred from the log + audit row
    correlation).
    """

    summary: dict[str, int] = {}
    for match in matches:
        summary[match.kind] = summary.get(match.kind, 0) + 1
    return summary


def _is_write_call(event: SseEvent) -> bool:
    """Return ``True`` iff the ``tool_call`` event represents a write.

    The orchestrator embeds the structured tool call and the
    LLM-supplied ``intent`` field in the SSE payload. We re-package
    them into the predicate :func:`is_write_intent` exposes —
    delegating the decision keeps the rule in one place
    (``src/chat/write_action.py``) and keeps the redirect event easy to
    parameterise.
    """

    payload = event.payload or {}
    call = payload.get("call")
    intent = payload.get("intent")
    if call is None:
        return False
    if not isinstance(intent, str) and intent is not None:
        # Defensive: a non-string intent field is a contract bug;
        # treat as "no explicit intent" and fall through to the
        # tool-name based branch.
        intent = None
    return is_write_intent(call, llm_intent_field=intent)


def _redirect_payload(event: SseEvent) -> Mapping[str, Any]:
    """Build the payload for ``redirect_to_task_creator``.

    Carries enough context for the Streamlit UI to
    pre-populate the Task Creator form with the user's message and
    the would-be tool. The ``intent`` field surfaces the LLM's
    classification so the UI can show a different copy for
    explicit-intent vs implicit-tool-name redirects.
    """

    payload = event.payload or {}
    call = payload.get("call")
    if isinstance(call, Mapping):
        tool_name = call.get("tool_name") or call.get("name")
    else:
        tool_name = getattr(call, "tool_name", None) if call is not None else None
    return {
        "reason": "write_action_requested",
        "tool_name": tool_name,
        "intent": payload.get("intent"),
        "message": (
            "Bunun için task açalım, Task Creator'a yönlendireyim mi?"
        ),
    }


def _extract_intent_event(done_event: SseEvent) -> SseEvent | None:
    """Extract an ``intent`` SSE event from the ``done`` event payload.

    When the LLM's structured response includes
    ``intent == "write_action_requested"``, this helper builds the
    ``event: intent`` SSE event that the Streamlit chat page uses to
    redirect the user to Task Creator.

    The payload carries:
        * ``intent`` — always ``"write_action_requested"``.
        * ``suggested_workflow_type`` — LLM-suggested workflow type
          (e.g. ``"code_change_with_test"``).
        * ``context_summary`` — human-readable summary of the
          conversation context.
        * ``prefill`` — structured fields for Task Creator form
          pre-population: ``{title, description, repo, branch}``.

    Returns ``None`` if the ``done`` event does not carry a
    ``write_action_requested`` intent.
    """

    payload = done_event.payload or {}
    intent = payload.get("intent")
    if intent != "write_action_requested":
        return None

    # Build the intent event payload for Chat → Task Creator wiring.
    intent_payload: dict[str, Any] = {
        "intent": "write_action_requested",
        "suggested_workflow_type": payload.get("suggested_workflow_type", ""),
        "context_summary": payload.get("context_summary", ""),
        "prefill": payload.get("prefill", {}),
    }
    return SseEvent(type="intent", payload=intent_payload)


def _accumulate_tokens(event: SseEvent, counters: _StreamCounters) -> None:
    """Update the per-stream counters from an SSE event payload.

    The orchestrator emits one of the following payload shapes that
    carry token / cost telemetry; this helper is tolerant of missing
    keys so a fake orchestrator (used in property tests) does not
    have to populate every field.

    Keys recognised:
        * ``token_in``, ``token_out`` — additive counters.
        * ``cost_usd`` — additive counter.
        * The ``tool_call`` event type increments ``tool_calls``.
    """

    payload = event.payload or {}
    if event.type == "tool_call":
        counters.tool_calls += 1
    token_in = payload.get("token_in")
    token_out = payload.get("token_out")
    cost_usd = payload.get("cost_usd")
    if isinstance(token_in, int):
        counters.token_in += token_in
    if isinstance(token_out, int):
        counters.token_out += token_out
    if isinstance(cost_usd, (int, float)):
        counters.cost_usd += float(cost_usd)
