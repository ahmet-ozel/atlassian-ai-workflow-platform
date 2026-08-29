"""Unit tests for :mod:`src.chat.handler`.

These tests exercise :class:`src.chat.handler.ChatHandler` end-to-end
using fake collaborators that conform to the protocols declared on
:class:`src.chat.handler.ChatHandlerDeps`. They cover the deterministic
six-step pipeline:

1. PII mask is applied **before** any LLM invocation.
2. Sliding window receives the masked-and-appended history.
3. System prompt is rendered with the dept-scoped template variables
   with template variables.
4. Banned-tool list and capability gate are applied to the
   catalogue before it reaches the LLM.
5. Write-action intent triggers ``redirect_to_task_creator`` and the
   tool dispatch is **never invoked**.
6. Audit ``chat_message`` row carries the mandatory
   ``prompt_version``, ``token_in``, ``token_out``, ``cost_usd``
   payload fields.

Property-style enumerations live in
``platform/tests/property/test_write_action_intercept.py`` and
``platform/tests/property/test_sliding_window.py``; this file focuses
on the **integration shape** of the handler - that
the steps run in the right order and the right collaborator is called
for each step.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Sequence

import pytest

# ``conftest.py`` adds ``libs/*/src`` to ``sys.path`` for repo-level
# tests, but the assistant-service test suite is its own collection
# and needs the same wiring.
import sys

_REPO_ROOT = Path(__file__).resolve().parents[4]
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

# Service src path so ``from src.chat.handler import ChatHandler`` works.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


from audit_logger import AuditEvent  # noqa: E402
from messages import ChatRequest, Message, SseEvent  # noqa: E402

from src.chat.handler import (  # noqa: E402
    ChatHandler,
    ChatHandlerDeps,
    DEFAULT_SLIDING_WINDOW_N,
    DeptContext,
)
from src.chat.write_action import ToolCall  # noqa: E402


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class _RecordingPromptLoader:
    """Minimal stand-in for :class:`prompts.PromptLoader`.

    The real loader needs a filesystem and a git binary. Tests inject
    this fake so the handler's ``render`` / ``version`` calls can be
    asserted on without spinning up a tmp repo.
    """

    def __init__(self, body: str = "system: {bot_username} {department_id}") -> None:
        self.body = body
        self.last_render_args: tuple[str, Any] | None = None
        self.version_calls: list[str] = []

    def load(self, name: str) -> str:  # pragma: no cover - unused by handler
        return self.body

    def render(self, name: str, *, vars: Any) -> str:
        self.last_render_args = (name, vars)
        # Mimic ``str.format`` behaviour just enough for assertions.
        return self.body.format(
            department_id=vars.department_id,
            bot_username=vars.bot_username,
            department_repos=", ".join(vars.department_repos),
            capabilities=", ".join(sorted(vars.capabilities)),
            default_language=vars.default_language,
        )

    def version(self, name: str) -> str:
        self.version_calls.append(name)
        return "abc1234"


@dataclass
class _RecordingAudit:
    """Captures every audit event written through ``write``."""

    events: list[AuditEvent] = field(default_factory=list)

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _RecordingDispatch:
    """Tracks whether ``invoke`` was ever called."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def invoke(self, tool_call: Any) -> Any:
        self.calls.append(tool_call)
        return {"ok": True, "tool_name": getattr(tool_call, "tool_name", None)}


class _ScriptedOrchestrator:
    """Yields a pre-baked sequence of SSE events.

    Captures the arguments it was called with so tests can verify
    that the system prompt, gated tool catalogue and token cap reach
    the orchestrator unchanged.
    """

    def __init__(self, events: Sequence[SseEvent]) -> None:
        self.events = list(events)
        self.last_kwargs: dict[str, Any] | None = None
        self.on_tool_call_invocations: list[Any] = []

    def stream_with_tool_loop(
        self,
        *,
        system: str,
        history: Sequence[Message],
        tools: Sequence[Any],
        on_tool_call: Callable[[Any], Awaitable[Any]],
        token_cap: int,
    ) -> AsyncIterator[SseEvent]:
        self.last_kwargs = {
            "system": system,
            "history": tuple(history),
            "tools": tuple(tools),
            "on_tool_call": on_tool_call,
            "token_cap": token_cap,
        }
        on_tool_call_ref = on_tool_call

        async def _gen() -> AsyncIterator[SseEvent]:
            for ev in self.events:
                yield ev

        # Surface the bound callback so tests can assert dispatch is
        # *only* invoked via this path (the handler must never call
        # ``tool_dispatch.invoke`` directly).
        self.on_tool_call_invocations = []  # cleared on every stream call
        self._on_tool_call = on_tool_call_ref  # type: ignore[attr-defined]
        return _gen()


@dataclass
class _ActorFake:
    actor_id: str = "user-123"
    actor_role: str = "lead"


# ---------------------------------------------------------------------------
# Fixture factories
# ---------------------------------------------------------------------------


def _identity_compress(
    messages: Sequence[Message],
    *,
    n: int,
    summarizer: Callable[[Sequence[Message]], str],
) -> Sequence[Message]:
    """No-op compressor: return the last ``n`` messages verbatim.

    Mirrors the contract of ``compress`` for the simple
    case where ``len(messages) <= n``. We keep the summariser
    parameter to match the protocol shape but never invoke it in this
    fake.
    """

    if len(messages) <= n:
        return tuple(messages)
    return tuple(messages[-n:])


def _truncating_compress(
    messages: Sequence[Message],
    *,
    n: int,
    summarizer: Callable[[Sequence[Message]], str],
) -> Sequence[Message]:
    """Compressor that exercises the summary branch.

    Used by the test that asserts the handler hands the older messages
    to the summariser.
    """

    if len(messages) <= n:
        return tuple(messages)
    older = messages[:-n]
    recent = messages[-n:]
    summary = summarizer(older)
    return (Message(role="system", text=f"[Önceki konuşma özeti] {summary}"), *recent)


def _capability_gate_passthrough(
    tools: Iterable[Any],
    *,
    capabilities: frozenset[str],
) -> Sequence[Any]:
    """Capability gate that drops every tool whose name starts with a
    capability outside ``capabilities`` (eg. ``confluence_*`` when
    ``confluence`` is not granted).

    Captures the call so the test can assert the dept's capability
    set actually reaches it.
    """

    out: list[Any] = []
    for tool in tools:
        name = tool if isinstance(tool, str) else tool.get("name")
        prefix = name.split("_", 1)[0] if isinstance(name, str) else None
        if prefix is None or prefix in capabilities:
            out.append(tool)
    return tuple(out)


def _build_dept(
    capabilities: frozenset[str] = frozenset({"jira", "bitbucket"}),
) -> DeptContext:
    return DeptContext(
        dept_id="payment",
        department_repos=("payment-api", "payment-web"),
        capabilities=capabilities,
        default_language="tr",
        bot_username="bot.payment",
    )


def _build_request(text: str, history: tuple[Message, ...] = ()) -> ChatRequest:
    return ChatRequest(
        user_message=text,
        history=history,
        dept_id="payment",
        session_id="sess-abc",
    )


def _build_handler(
    *,
    orchestrator: _ScriptedOrchestrator,
    compress=_identity_compress,
    capability_gate=_capability_gate_passthrough,
    list_tools: Callable[[], Iterable[Any]] | None = None,
    audit: _RecordingAudit | None = None,
    dispatch: _RecordingDispatch | None = None,
    prompt_loader: _RecordingPromptLoader | None = None,
    sliding_window_n: int = DEFAULT_SLIDING_WINDOW_N,
    token_cap: int = 10_000,
    prompt_name: str = "assistant_chat",
    chat_mode: str = "atlassian",
) -> tuple[ChatHandler, _RecordingAudit, _RecordingDispatch, _RecordingPromptLoader]:
    audit = audit or _RecordingAudit()
    dispatch = dispatch or _RecordingDispatch()
    prompt_loader = prompt_loader or _RecordingPromptLoader()
    list_tools = list_tools or (
        lambda: (
            "jira_get_issue",
            "bitbucket_merge_pr",  # banned upstream
            "confluence_create_page",
            "confluence_delete_page",  # banned upstream
            "bitbucket_create_pull_request_cloud",
        )
    )

    deps = ChatHandlerDeps(
        prompt_loader=prompt_loader,  # type: ignore[arg-type]
        compress=compress,  # type: ignore[arg-type]
        summariser=lambda older: f"summary({len(older)})",
        capability_gate=capability_gate,  # type: ignore[arg-type]
        llm=orchestrator,  # type: ignore[arg-type]
        tool_dispatch=dispatch,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        token_cap=token_cap,
        sliding_window_n=sliding_window_n,
        prompt_name=prompt_name,
        chat_mode=chat_mode,
        list_tools=list_tools,
    )
    handler = ChatHandler(deps)
    return handler, audit, dispatch, prompt_loader


async def _drain(handler: ChatHandler, request: ChatRequest, dept: DeptContext) -> list[SseEvent]:
    actor = _ActorFake()
    out: list[SseEvent] = []
    async for ev in handler.stream(request, actor, dept):
        out.append(ev)
    return out


# ---------------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_token_cap_must_be_positive(self) -> None:
        orch = _ScriptedOrchestrator([SseEvent("done", {})])
        deps = ChatHandlerDeps(
            prompt_loader=_RecordingPromptLoader(),  # type: ignore[arg-type]
            compress=_identity_compress,  # type: ignore[arg-type]
            summariser=lambda older: "x",
            capability_gate=_capability_gate_passthrough,  # type: ignore[arg-type]
            llm=orch,  # type: ignore[arg-type]
            tool_dispatch=_RecordingDispatch(),  # type: ignore[arg-type]
            audit=_RecordingAudit(),  # type: ignore[arg-type]
            token_cap=0,
        )
        with pytest.raises(ValueError, match="token_cap"):
            ChatHandler(deps)

    def test_sliding_window_must_be_positive(self) -> None:
        orch = _ScriptedOrchestrator([SseEvent("done", {})])
        deps = ChatHandlerDeps(
            prompt_loader=_RecordingPromptLoader(),  # type: ignore[arg-type]
            compress=_identity_compress,  # type: ignore[arg-type]
            summariser=lambda older: "x",
            capability_gate=_capability_gate_passthrough,  # type: ignore[arg-type]
            llm=orch,  # type: ignore[arg-type]
            tool_dispatch=_RecordingDispatch(),  # type: ignore[arg-type]
            audit=_RecordingAudit(),  # type: ignore[arg-type]
            token_cap=10,
            sliding_window_n=0,
        )
        with pytest.raises(ValueError, match="sliding_window_n"):
            ChatHandler(deps)


# ---------------------------------------------------------------------------
# Six-step pipeline
# ---------------------------------------------------------------------------


class TestPipelineWiring:
    def test_pii_masked_text_is_what_reaches_llm(self) -> None:
        """Step 1 + 2: the LLM receives the *masked* user message,
        not the raw input."""
        orch = _ScriptedOrchestrator([SseEvent("done", {})])
        handler, *_ = _build_handler(orchestrator=orch)

        # The TR phone regex matches ``5XX XXX XX XX``.
        request = _build_request("call me at 555 123 45 67 thanks")
        dept = _build_dept()

        events = asyncio.run(_drain(handler, request, dept))

        assert orch.last_kwargs is not None
        history = orch.last_kwargs["history"]
        # The newly-appended user message is the last entry.
        assert history[-1].role == "user"
        assert "555 123 45 67" not in history[-1].text
        assert "***PHONE_REDACTED***" in history[-1].text
        assert events[-1].type == "done"

    def test_sliding_window_receives_full_history_then_summarises(self) -> None:
        """Step 2: when the history exceeds ``sliding_window_n``, the
        compressor is called with the full list and the summariser
        receives the dropped older messages."""
        orch = _ScriptedOrchestrator([SseEvent("done", {})])
        history = tuple(
            Message(role="user" if i % 2 == 0 else "assistant", text=f"m{i}")
            for i in range(5)
        )
        handler, *_ = _build_handler(
            orchestrator=orch,
            compress=_truncating_compress,
            sliding_window_n=2,
        )

        events = asyncio.run(_drain(handler, _build_request("new", history), _build_dept()))

        assert orch.last_kwargs is not None
        compressed = orch.last_kwargs["history"]
        # 1 summary + last 2 of (history+new) == 3 messages.
        assert len(compressed) == 3
        assert compressed[0].role == "system"
        assert "Önceki konuşma özeti" in compressed[0].text
        assert events[-1].type == "done"

    def test_system_prompt_rendered_with_dept_vars(self) -> None:
        """Step 3: the system prompt is rendered through the
        :class:`PromptLoader` with the dept's template vars.
        """
        orch = _ScriptedOrchestrator([SseEvent("done", {})])
        loader = _RecordingPromptLoader(body="hi {bot_username} of {department_id}")
        handler, *_ = _build_handler(orchestrator=orch, prompt_loader=loader)

        asyncio.run(_drain(handler, _build_request("hello"), _build_dept()))

        assert orch.last_kwargs is not None
        assert orch.last_kwargs["system"] == "hi bot.payment of payment"
        assert loader.last_render_args is not None
        name, vars_ = loader.last_render_args
        assert name == "assistant_chat"
        assert vars_.department_id == "payment"
        assert vars_.bot_username == "bot.payment"

    def test_banned_tools_filtered_before_capability_gate(self) -> None:
        """Step 4: ``mcp_client.filter_tools`` strips banned tools
        first, then the capability gate narrows further.
        """
        orch = _ScriptedOrchestrator([SseEvent("done", {})])
        handler, *_ = _build_handler(
            orchestrator=orch,
            list_tools=lambda: (
                "jira_get_issue",
                "bitbucket_merge_pr",  # banned globally
                "confluence_create_page",
                "confluence_delete_page",  # banned globally
                "bitbucket_create_pull_request_cloud",
            ),
        )
        # Dept has only jira + bitbucket - confluence_* must drop.
        dept = _build_dept(capabilities=frozenset({"jira", "bitbucket"}))

        asyncio.run(_drain(handler, _build_request("hi"), dept))

        assert orch.last_kwargs is not None
        delivered = set(orch.last_kwargs["tools"])
        assert "bitbucket_merge_pr" not in delivered  # banned
        assert "confluence_delete_page" not in delivered  # banned
        assert "confluence_create_page" not in delivered  # capability gate
        assert "jira_get_issue" in delivered
        assert "bitbucket_create_pull_request_cloud" in delivered

    def test_token_cap_is_forwarded_to_orchestrator(self) -> None:
        """Step 5: the orchestrator receives ``token_cap`` so it can
        fail-fast when exceeded."""
        orch = _ScriptedOrchestrator([SseEvent("done", {})])
        handler, *_ = _build_handler(orchestrator=orch, token_cap=4242)

        asyncio.run(_drain(handler, _build_request("hi"), _build_dept()))

        assert orch.last_kwargs is not None
        assert orch.last_kwargs["token_cap"] == 4242


# ---------------------------------------------------------------------------
# Write-action intercept
# ---------------------------------------------------------------------------


class TestWriteActionIntercept:
    def test_explicit_intent_redirects_and_skips_dispatch(self) -> None:
        """LLM returns an explicit ``write_action_requested`` intent.
        Handler emits ``redirect_to_task_creator`` and **does not**
        call ``tool_dispatch.invoke``."""
        write_call_event = SseEvent(
            "tool_call",
            {"call": ToolCall(tool_name="jira_search"), "intent": "write_action_requested"},
        )
        # ``done`` should never be reached because the intercept
        # returns from the generator early.
        orch = _ScriptedOrchestrator(
            [
                SseEvent("token", {"text": "let me", "token_out": 2}),
                write_call_event,
                SseEvent("done", {}),
            ]
        )
        handler, audit, dispatch, _ = _build_handler(orchestrator=orch)

        events = asyncio.run(_drain(handler, _build_request("merge that PR"), _build_dept()))

        types = [e.type for e in events]
        assert "redirect_to_task_creator" in types
        assert "done" not in types  # generator returned early
        assert dispatch.calls == []  # tool dispatch never invoked
        # Audit is still written (the redirect is a deterministic
        # outcome that needs to be on the ledger).
        assert len(audit.events) == 1
        assert audit.events[0].action == "chat_message"
        assert audit.events[0].payload["write_intent_redirected"] is True

    def test_implicit_write_tool_name_redirects(self) -> None:
        """No explicit intent but a tool name in
        :data:`WRITE_ACTION_TOOLS` still triggers the redirect."""
        write_call_event = SseEvent(
            "tool_call",
            {"call": ToolCall(tool_name="bitbucket_create_pull_request_cloud"), "intent": None},
        )
        orch = _ScriptedOrchestrator([write_call_event, SseEvent("done", {})])
        handler, _, dispatch, _ = _build_handler(orchestrator=orch)

        events = asyncio.run(_drain(handler, _build_request("open a PR for me"), _build_dept()))

        types = [e.type for e in events]
        assert types[-1] == "redirect_to_task_creator"
        assert dispatch.calls == []

    def test_read_only_tool_call_is_forwarded(self) -> None:
        """A non-write tool call is propagated to the SSE stream as a
        ``tool_call`` event and the orchestrator continues to
        ``done``."""
        read_call_event = SseEvent(
            "tool_call",
            {"call": ToolCall(tool_name="jira_get_issue"), "intent": None, "token_out": 5},
        )
        result_event = SseEvent("tool_result", {"data": {"issue": "PR-123"}})
        orch = _ScriptedOrchestrator(
            [read_call_event, result_event, SseEvent("done", {"token_out": 1})]
        )
        handler, audit, dispatch, _ = _build_handler(orchestrator=orch)

        events = asyncio.run(_drain(handler, _build_request("show me PR-123"), _build_dept()))

        types = [e.type for e in events]
        assert types == ["tool_call", "tool_result", "done"]
        # The handler does not call ``invoke`` itself; the orchestrator
        # would, but our scripted fake never does. The contract under
        # test is "handler did not bypass the orchestrator".
        assert dispatch.calls == []
        # Audit accumulates token counts from the events.
        assert len(audit.events) == 1
        assert audit.events[0].payload["tool_calls"] == 1
        assert audit.events[0].payload["token_out"] == 6  # 5 + 1


# ---------------------------------------------------------------------------
# Audit chat_message row
# ---------------------------------------------------------------------------


class TestAuditWrite:
    def test_audit_payload_carries_mandatory_fields(self) -> None:
        """``prompt_version``, ``token_in``, ``token_out``,
        ``cost_usd`` are mandatory per design.md §"ChatHandler"."""
        orch = _ScriptedOrchestrator(
            [
                SseEvent("token", {"text": "hi", "token_in": 3, "token_out": 1, "cost_usd": 0.0001}),
                SseEvent("token", {"text": "!", "token_out": 1, "cost_usd": 0.00005}),
                SseEvent("done", {"token_out": 0}),
            ]
        )
        handler, audit, _, _ = _build_handler(orchestrator=orch)

        asyncio.run(_drain(handler, _build_request("hi"), _build_dept()))

        assert len(audit.events) == 1
        event = audit.events[0]
        assert event.action == "chat_message"
        assert event.actor_id == "user-123"
        assert event.actor_role == "lead"
        assert event.dept_id == "payment"
        assert event.result == "ok"
        # Mandatory payload fields.
        assert event.payload["prompt_version"] == "abc1234"
        assert event.payload["token_in"] == 3
        assert event.payload["token_out"] == 2
        assert event.payload["cost_usd"] == pytest.approx(0.00015)
        # Ops bonus fields.
        assert event.payload["chat_mode"] == "atlassian"
        assert event.payload["prompt_name"] == "assistant_chat"
        assert event.payload["pii_matches_count"] == 0
        assert event.payload["tool_calls"] == 0
        assert event.payload["write_intent_redirected"] is False

    def test_mail_mode_audit_payload_identifies_mail_prompt(self) -> None:
        orch = _ScriptedOrchestrator([SseEvent("done", {})])
        handler, audit, _, loader = _build_handler(
            orchestrator=orch,
            prompt_name="mail_assistant_chat",
            chat_mode="mail",
            list_tools=lambda: ({"name": "gmail_list_messages"},),
        )

        asyncio.run(_drain(handler, _build_request("son mailleri getir"), _build_dept()))

        assert loader.version_calls == ["mail_assistant_chat"]
        assert audit.events[0].payload["chat_mode"] == "mail"
        assert audit.events[0].payload["prompt_name"] == "mail_assistant_chat"

    def test_pii_match_count_is_recorded(self) -> None:
        """The audit row carries the *count* of PII matches but never
        the matched substrings - they are masked out before audit
        time."""
        orch = _ScriptedOrchestrator([SseEvent("done", {})])
        handler, audit, _, _ = _build_handler(orchestrator=orch)

        # Two distinct PII patterns: TR phone + email.
        asyncio.run(
            _drain(
                handler,
                _build_request("phone 555 111 22 33 mail user@example.com"),
                _build_dept(),
            )
        )

        event = audit.events[0]
        assert event.payload["pii_matches_count"] == 2

    def test_audit_failure_does_not_break_stream(self) -> None:
        """The handler must keep streaming the LLM output even if the
        audit write blows up - audit_logger violations are
        programmer errors, not user-facing failures."""

        class _ExplodingAudit:
            async def write(self, event: AuditEvent) -> None:
                raise RuntimeError("postgres down")

        orch = _ScriptedOrchestrator(
            [SseEvent("token", {"text": "hi"}), SseEvent("done", {})]
        )
        handler, _, _, _ = _build_handler(orchestrator=orch, audit=_ExplodingAudit())  # type: ignore[arg-type]

        events = asyncio.run(_drain(handler, _build_request("hello"), _build_dept()))

        assert [e.type for e in events] == ["token", "done"]


# ---------------------------------------------------------------------------
# Pass-through SSE events
# ---------------------------------------------------------------------------


class TestTerminalEventsArePropagated:
    @pytest.mark.parametrize(
        "terminal",
        ["rate_limit_exhausted", "token_cap_exceeded", "fallback_provider_active", "error"],
    )
    def test_orchestrator_terminal_events_propagate(self, terminal: str) -> None:
        """SSE events emitted by the orchestrator pass through the
        handler verbatim."""
        orch = _ScriptedOrchestrator(
            [
                SseEvent("token", {"text": "x"}),
                SseEvent(terminal, {"reason": "test"}),  # type: ignore[arg-type]
            ]
        )
        handler, _, _, _ = _build_handler(orchestrator=orch)

        events = asyncio.run(_drain(handler, _build_request("hi"), _build_dept()))

        types = [e.type for e in events]
        assert terminal in types


# ---------------------------------------------------------------------------
# Intent SSE event emission
# ---------------------------------------------------------------------------


class TestIntentEventEmission:
    """Tests for the ``event: intent`` SSE emission when the LLM
    response contains ``intent == "write_action_requested"``
    """

    def test_done_with_write_intent_emits_intent_event(self) -> None:
        """When the ``done`` event payload carries
        ``intent: "write_action_requested"``, the handler emits an
        additional ``intent`` SSE event after ``done``."""
        done_payload = {
            "intent": "write_action_requested",
            "suggested_workflow_type": "code_change_with_test",
            "context_summary": "User wants to commit retry logic",
            "prefill": {
                "title": "Add retry mechanism",
                "description": "## Task\nAdd retry to payment callbacks",
                "repo": "payment-callbacks",
                "branch": "develop",
            },
        }
        orch = _ScriptedOrchestrator(
            [
                SseEvent("token", {"text": "I can help with that."}),
                SseEvent("done", done_payload),
            ]
        )
        handler, audit, _, _ = _build_handler(orchestrator=orch)

        events = asyncio.run(_drain(handler, _build_request("commit this code"), _build_dept()))

        types = [e.type for e in events]
        assert types == ["token", "done", "intent"]

        # Verify intent event payload structure
        intent_event = events[2]
        assert intent_event.payload["intent"] == "write_action_requested"
        assert intent_event.payload["suggested_workflow_type"] == "code_change_with_test"
        assert intent_event.payload["context_summary"] == "User wants to commit retry logic"
        assert intent_event.payload["prefill"]["title"] == "Add retry mechanism"
        assert intent_event.payload["prefill"]["repo"] == "payment-callbacks"
        assert intent_event.payload["prefill"]["branch"] == "develop"

        # Audit records intent_emitted
        assert len(audit.events) == 1
        assert audit.events[0].payload["intent_emitted"] is True

    def test_done_without_intent_does_not_emit_intent_event(self) -> None:
        """When the ``done`` event has no intent field, no extra
        ``intent`` event is emitted."""
        orch = _ScriptedOrchestrator(
            [
                SseEvent("token", {"text": "Here is the info."}),
                SseEvent("done", {}),
            ]
        )
        handler, audit, _, _ = _build_handler(orchestrator=orch)

        events = asyncio.run(_drain(handler, _build_request("show me the issue"), _build_dept()))

        types = [e.type for e in events]
        assert types == ["token", "done"]
        assert audit.events[0].payload["intent_emitted"] is False

    def test_done_with_read_intent_does_not_emit_intent_event(self) -> None:
        """When the ``done`` event carries a non-write intent (e.g.
        ``read_action``), no ``intent`` event is emitted."""
        orch = _ScriptedOrchestrator(
            [
                SseEvent("token", {"text": "Found it."}),
                SseEvent("done", {"intent": "read_action"}),
            ]
        )
        handler, audit, _, _ = _build_handler(orchestrator=orch)

        events = asyncio.run(_drain(handler, _build_request("find the bug"), _build_dept()))

        types = [e.type for e in events]
        assert types == ["token", "done"]
        assert audit.events[0].payload["intent_emitted"] is False

    def test_intent_event_with_missing_optional_fields(self) -> None:
        """When the ``done`` event has ``write_action_requested`` but
        some optional fields are missing, the intent event still
        emits with empty defaults."""
        done_payload = {
            "intent": "write_action_requested",
            # No suggested_workflow_type, context_summary, or prefill
        }
        orch = _ScriptedOrchestrator(
            [SseEvent("done", done_payload)]
        )
        handler, audit, _, _ = _build_handler(orchestrator=orch)

        events = asyncio.run(_drain(handler, _build_request("deploy this"), _build_dept()))

        types = [e.type for e in events]
        assert types == ["done", "intent"]

        intent_event = events[1]
        assert intent_event.payload["intent"] == "write_action_requested"
        assert intent_event.payload["suggested_workflow_type"] == ""
        assert intent_event.payload["context_summary"] == ""
        assert intent_event.payload["prefill"] == {}
        assert audit.events[0].payload["intent_emitted"] is True

    def test_error_terminal_does_not_emit_intent_event(self) -> None:
        """When the stream ends with ``error`` (not ``done``), no
        intent event is emitted even if the payload has intent."""
        orch = _ScriptedOrchestrator(
            [
                SseEvent("error", {"intent": "write_action_requested", "reason": "timeout"}),
            ]
        )
        handler, audit, _, _ = _build_handler(orchestrator=orch)

        events = asyncio.run(_drain(handler, _build_request("commit"), _build_dept()))

        types = [e.type for e in events]
        assert "intent" not in types
        assert audit.events[0].payload["intent_emitted"] is False


# ---------------------------------------------------------------------------
# Timeout and Truncation Handling
# ---------------------------------------------------------------------------


class _SlowOrchestrator:
    """Orchestrator that delays before yielding events.

    Used to test the timeout handling: when the delay exceeds
    ``ChatHandlerDeps.timeout_s``, the handler should abort and emit
    an ``llm_timeout`` error event.
    """

    def __init__(self, events: Sequence[SseEvent], delay_s: float) -> None:
        self.events = list(events)
        self.delay_s = delay_s
        self.last_kwargs: dict[str, Any] | None = None

    def stream_with_tool_loop(
        self,
        *,
        system: str,
        history: Sequence[Message],
        tools: Sequence[Any],
        on_tool_call: Callable[[Any], Awaitable[Any]],
        token_cap: int,
    ) -> AsyncIterator[SseEvent]:
        self.last_kwargs = {
            "system": system,
            "history": tuple(history),
            "tools": tuple(tools),
            "on_tool_call": on_tool_call,
            "token_cap": token_cap,
        }

        async def _gen() -> AsyncIterator[SseEvent]:
            await asyncio.sleep(self.delay_s)
            for ev in self.events:
                yield ev

        return _gen()


class TestTimeoutHandling:
    """When the LLM call exceeds ``LLM_REQUEST_TIMEOUT_S`` seconds, the
    handler aborts the request, writes an ``assistant_llm_timeout``
    audit event, and emits an SSE error event with
    ``{"reason": "llm_timeout"}``.
    """

    def test_timeout_emits_error_event(self) -> None:
        """LLM exceeding timeout_s  SSE error with reason=llm_timeout."""
        # Orchestrator that sleeps 2 seconds before yielding
        orch = _SlowOrchestrator(
            events=[SseEvent("done", {"truncated": False})],
            delay_s=2.0,
        )
        audit = _RecordingAudit()
        deps = ChatHandlerDeps(
            prompt_loader=_RecordingPromptLoader(),  # type: ignore[arg-type]
            compress=_identity_compress,  # type: ignore[arg-type]
            summariser=lambda older: "summary",
            capability_gate=_capability_gate_passthrough,  # type: ignore[arg-type]
            llm=orch,  # type: ignore[arg-type]
            tool_dispatch=_RecordingDispatch(),  # type: ignore[arg-type]
            audit=audit,  # type: ignore[arg-type]
            token_cap=10_000,
            timeout_s=1,  # 1 second timeout - orchestrator sleeps 2s
            max_tokens_output=4096,
        )
        handler = ChatHandler(deps)

        events = asyncio.run(_drain(handler, _build_request("hello"), _build_dept()))

        # Should emit exactly one error event with reason=llm_timeout
        assert len(events) == 1
        assert events[0].type == "error"
        assert events[0].payload["reason"] == "llm_timeout"

    def test_timeout_writes_audit_event(self) -> None:
        """LLM timeout  ``assistant_llm_timeout`` audit event written."""
        orch = _SlowOrchestrator(
            events=[SseEvent("done", {"truncated": False})],
            delay_s=2.0,
        )
        audit = _RecordingAudit()
        deps = ChatHandlerDeps(
            prompt_loader=_RecordingPromptLoader(),  # type: ignore[arg-type]
            compress=_identity_compress,  # type: ignore[arg-type]
            summariser=lambda older: "summary",
            capability_gate=_capability_gate_passthrough,  # type: ignore[arg-type]
            llm=orch,  # type: ignore[arg-type]
            tool_dispatch=_RecordingDispatch(),  # type: ignore[arg-type]
            audit=audit,  # type: ignore[arg-type]
            token_cap=10_000,
            timeout_s=1,
            max_tokens_output=4096,
        )
        handler = ChatHandler(deps)

        asyncio.run(_drain(handler, _build_request("hello"), _build_dept()))

        # Audit should have exactly one event with action=assistant_llm_timeout
        assert len(audit.events) == 1
        assert audit.events[0].action == "assistant_llm_timeout"
        assert audit.events[0].payload["timeout_s"] == 1

    def test_no_timeout_when_within_limit(self) -> None:
        """LLM responding within timeout_s  normal done event."""
        orch = _SlowOrchestrator(
            events=[SseEvent("done", {"truncated": False})],
            delay_s=0.01,  # Very fast
        )
        audit = _RecordingAudit()
        deps = ChatHandlerDeps(
            prompt_loader=_RecordingPromptLoader(),  # type: ignore[arg-type]
            compress=_identity_compress,  # type: ignore[arg-type]
            summariser=lambda older: "summary",
            capability_gate=_capability_gate_passthrough,  # type: ignore[arg-type]
            llm=orch,  # type: ignore[arg-type]
            tool_dispatch=_RecordingDispatch(),  # type: ignore[arg-type]
            audit=audit,  # type: ignore[arg-type]
            token_cap=10_000,
            timeout_s=5,  # 5 second timeout - orchestrator responds in 0.01s
            max_tokens_output=4096,
        )
        handler = ChatHandler(deps)

        events = asyncio.run(_drain(handler, _build_request("hello"), _build_dept()))

        # Should get the normal done event, not a timeout error
        assert any(e.type == "done" for e in events)
        assert not any(e.type == "error" and e.payload.get("reason") == "llm_timeout" for e in events)
        # Audit should be chat_message, not assistant_llm_timeout
        assert audit.events[0].action == "chat_message"


class TestTruncationHandling:
    """When the LLM response exceeds ``LLM_MAX_TOKENS_OUTPUT`` tokens,
    the handler closes the stream cleanly and emits a final ``done``
    event with ``truncated: true``.
    """

    def test_truncation_emits_done_with_truncated_true(self) -> None:
        """Output tokens exceeding max  done event with truncated=True."""
        # Orchestrator that emits token events with high token_out counts
        orch = _ScriptedOrchestrator(
            [
                SseEvent("token", {"text": "hello ", "token_out": 50}),
                SseEvent("token", {"text": "world ", "token_out": 60}),  # cumulative: 110 > 100
                SseEvent("token", {"text": "more ", "token_out": 30}),
                SseEvent("done", {"truncated": False}),
            ]
        )
        audit = _RecordingAudit()
        deps = ChatHandlerDeps(
            prompt_loader=_RecordingPromptLoader(),  # type: ignore[arg-type]
            compress=_identity_compress,  # type: ignore[arg-type]
            summariser=lambda older: "summary",
            capability_gate=_capability_gate_passthrough,  # type: ignore[arg-type]
            llm=orch,  # type: ignore[arg-type]
            tool_dispatch=_RecordingDispatch(),  # type: ignore[arg-type]
            audit=audit,  # type: ignore[arg-type]
            token_cap=10_000,
            timeout_s=60,
            max_tokens_output=100,  # Low limit - will be exceeded
        )
        handler = ChatHandler(deps)

        events = asyncio.run(_drain(handler, _build_request("hello"), _build_dept()))

        # The first token event (50 tokens) should pass through
        assert events[0].type == "token"
        # After the second token event (cumulative 110 > 100), truncation kicks in
        # The handler emits the done event with truncated=True
        done_events = [e for e in events if e.type == "done"]
        assert len(done_events) == 1
        assert done_events[0].payload["truncated"] is True

    def test_truncation_stops_further_events(self) -> None:
        """After truncation, no more events from the orchestrator are yielded."""
        orch = _ScriptedOrchestrator(
            [
                SseEvent("token", {"text": "a", "token_out": 200}),  # Exceeds immediately
                SseEvent("token", {"text": "b", "token_out": 100}),
                SseEvent("done", {"truncated": False}),
            ]
        )
        audit = _RecordingAudit()
        deps = ChatHandlerDeps(
            prompt_loader=_RecordingPromptLoader(),  # type: ignore[arg-type]
            compress=_identity_compress,  # type: ignore[arg-type]
            summariser=lambda older: "summary",
            capability_gate=_capability_gate_passthrough,  # type: ignore[arg-type]
            llm=orch,  # type: ignore[arg-type]
            tool_dispatch=_RecordingDispatch(),  # type: ignore[arg-type]
            audit=audit,  # type: ignore[arg-type]
            token_cap=10_000,
            timeout_s=60,
            max_tokens_output=100,
        )
        handler = ChatHandler(deps)

        events = asyncio.run(_drain(handler, _build_request("hello"), _build_dept()))

        # Only the done event with truncated=True should be the terminal event
        # The second token and original done should NOT appear
        types = [e.type for e in events]
        assert types.count("done") == 1
        assert events[-1].type == "done"
        assert events[-1].payload["truncated"] is True

    def test_truncation_writes_chat_message_audit(self) -> None:
        """Truncation still writes a ``chat_message`` audit event."""
        orch = _ScriptedOrchestrator(
            [
                SseEvent("token", {"text": "a", "token_out": 200}),
                SseEvent("done", {"truncated": False}),
            ]
        )
        audit = _RecordingAudit()
        deps = ChatHandlerDeps(
            prompt_loader=_RecordingPromptLoader(),  # type: ignore[arg-type]
            compress=_identity_compress,  # type: ignore[arg-type]
            summariser=lambda older: "summary",
            capability_gate=_capability_gate_passthrough,  # type: ignore[arg-type]
            llm=orch,  # type: ignore[arg-type]
            tool_dispatch=_RecordingDispatch(),  # type: ignore[arg-type]
            audit=audit,  # type: ignore[arg-type]
            token_cap=10_000,
            timeout_s=60,
            max_tokens_output=100,
        )
        handler = ChatHandler(deps)

        asyncio.run(_drain(handler, _build_request("hello"), _build_dept()))

        # Audit should have a chat_message event (not assistant_llm_timeout)
        assert len(audit.events) == 1
        assert audit.events[0].action == "chat_message"

    def test_no_truncation_when_within_limit(self) -> None:
        """Output tokens within max  normal done event without truncated flag."""
        orch = _ScriptedOrchestrator(
            [
                SseEvent("token", {"text": "hello", "token_out": 10}),
                SseEvent("done", {"truncated": False}),
            ]
        )
        audit = _RecordingAudit()
        deps = ChatHandlerDeps(
            prompt_loader=_RecordingPromptLoader(),  # type: ignore[arg-type]
            compress=_identity_compress,  # type: ignore[arg-type]
            summariser=lambda older: "summary",
            capability_gate=_capability_gate_passthrough,  # type: ignore[arg-type]
            llm=orch,  # type: ignore[arg-type]
            tool_dispatch=_RecordingDispatch(),  # type: ignore[arg-type]
            audit=audit,  # type: ignore[arg-type]
            token_cap=10_000,
            timeout_s=60,
            max_tokens_output=4096,  # High limit - won't be exceeded
        )
        handler = ChatHandler(deps)

        events = asyncio.run(_drain(handler, _build_request("hello"), _build_dept()))

        # Normal done event without truncated=True
        done_events = [e for e in events if e.type == "done"]
        assert len(done_events) == 1
        assert done_events[0].payload.get("truncated") is not True
