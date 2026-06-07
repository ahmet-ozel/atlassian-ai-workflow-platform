"""Property-based tests for write-action intent intercept.

This file owns the write-action intent intercept behavior. It pins
the deterministic decision table of
:func:`assistant_service.chat.write_action.is_write_intent` and the
two call-site invariants that follow from it inside
:meth:`assistant_service.chat.handler.ChatHandler.stream`:

* When the predicate returns ``True`` for an LLM-issued tool call,
  the handler emits exactly one ``redirect_to_task_creator`` SSE
  event and stops the stream **without** invoking
  ``tool_dispatch.invoke``.

* When the predicate returns ``False``, the handler propagates the
  ``tool_call`` event verbatim and continues the orchestrator loop;
  the read-only branch therefore never produces a redirect event.

Universal property
------------------

For any draw of ``(tool_name, intent)`` from

* ``tool_name`` - drawn from a finite catalogue that mixes the seven
  members of :data:`WRITE_ACTION_TOOLS` with read-only / unknown
  names;
* ``intent`` - drawn from ``{"write_action_requested", "read_action",
  None, <arbitrary string>}``;

the predicate satisfies the three-row decision table:

.. code-block:: text

    intent == "write_action_requested"           ⇒ True
    intent ≠  "write_action_requested" ∧ name ∈  ⇒ True
        WRITE_ACTION_TOOLS
    intent ≠  "write_action_requested" ∧ name ∉  ⇒ False
        WRITE_ACTION_TOOLS

and the handler-level invariant holds:

.. code-block:: text

    is_write_intent(call, intent) ⇒
        redirect_to_task_creator emitted exactly once,
        tool_dispatch.invoke never called, and
        the stream terminates after the redirect event.

    ¬is_write_intent(call, intent) ⇒
        no redirect_to_task_creator event emitted, and
        the orchestrator's terminal event (``done``) is reached.

Cross-references
----------------

* Tabular spot checks of the same predicate live in
  ``platform/services/assistant-service/tests/unit/test_write_action.py``.
* Handler-level integration assertions on the redirect path live in
  ``platform/services/assistant-service/tests/unit/test_handler.py``.
* The companion sliding-window checks live in ``test_sliding_window.py``
  and the LLM retry / fallback checks live in
  ``test_llm_retry_fallback.py``.
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
# sys.path bootstrap - the assistant-service is *not* a shared library so
# it is not on ``pytest.ini``'s ``pythonpath``. We insert its source
# root manually so ``from src.chat.write_action import ...`` resolves.
# The shared libs (``messages``, ``audit_logger``, ``pii-shared``,
# ``mcp_client``, ``prompts``) are already on path via ``conftest.py``
# but we add them defensively for direct ``python -m pytest`` runs from
# unusual working directories.
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
from src.chat.write_action import (  # noqa: E402
    WRITE_ACTION_TOOLS,
    ToolCall,
    is_write_intent,
)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Read-only / unknown tool names that are guaranteed *not* to belong
#: to :data:`WRITE_ACTION_TOOLS`. Mixing real read-only MCP tools
#: (``jira_search``, ``confluence_search``, ``bitbucket_get_*``) with
#: synthetic names ensures the false branch of the decision table is
#: exercised against both a realistic and a degenerate input space.
_READ_ONLY_NAMES: tuple[str, ...] = (
    "jira_search",
    "jira_get_issue",
    "jira_list_projects",
    "confluence_search",
    "confluence_get_page",
    "bitbucket_get_pull_request",
    "bitbucket_list_branches",
    "bitbucket_get_commit",
    "",  # empty string - degenerate, but still ∉ WRITE_ACTION_TOOLS
    "totally_unknown_tool",
)

# Sanity check: the read-only catalogue must be disjoint from the
# write set; the property's "False" branch only proves anything if
# this is true at strategy-construction time.
assert set(_READ_ONLY_NAMES).isdisjoint(WRITE_ACTION_TOOLS), (
    "Test strategy invariant broken: _READ_ONLY_NAMES overlaps "
    "WRITE_ACTION_TOOLS - fix the catalogue before the property "
    "loses its discriminating power."
)


#: Strategy that draws any tool name from the *full* catalogue
#: (write + read-only). Used for the predicate-only properties so
#: every row of the decision table sees coverage.
_any_tool_name = st.one_of(
    st.sampled_from(sorted(WRITE_ACTION_TOOLS)),
    st.sampled_from(_READ_ONLY_NAMES),
)


#: Strategy that draws *only* write tool names. Used by the
#: handler-level redirect property to ensure the tool-name branch is
#: exercised independently of the explicit-intent branch.
_write_tool_name = st.sampled_from(sorted(WRITE_ACTION_TOOLS))


#: Strategy that draws *only* read-only tool names. Used by the
#: handler-level "no redirect" property.
_read_only_tool_name = st.sampled_from(_READ_ONLY_NAMES)


#: Strategy for the LLM-supplied ``intent`` field. Includes the two
#: design-mandated literals (``"write_action_requested"``,
#: ``"read_action"``), ``None`` and arbitrary strings so any future
#: LLM output drift cannot quietly bypass the predicate.
_intent_field = st.one_of(
    st.just("write_action_requested"),
    st.just("read_action"),
    st.none(),
    st.text(min_size=0, max_size=24),
)


#: Strategy for an ``intent`` that is **not** the explicit write
#: signal. Used when we want to isolate the tool-name branch.
_non_write_intent = st.one_of(
    st.just("read_action"),
    st.none(),
    # Arbitrary text that is not the explicit write literal.
    st.text(min_size=0, max_size=24).filter(
        lambda s: s != "write_action_requested"
    ),
)


# ---------------------------------------------------------------------------
# Test fakes - minimal collaborators for ``ChatHandler``
# ---------------------------------------------------------------------------


class _StubPromptLoader:
    """Tiny ``PromptLoader`` stand-in.

    The handler only calls ``render`` and ``version`` once per stream;
    we return constant strings so the audit row payload is
    deterministic and the property can focus on the redirect branch.
    """

    def render(self, name: str, *, vars: Any) -> str:
        return "stub-system-prompt"

    def version(self, name: str) -> str:
        return "stub0001"


@dataclass
class _RecordingAudit:
    """In-memory audit sink - captures every event written."""

    events: list[AuditEvent] = field(default_factory=list)

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _RecordingDispatch:
    """Tracks every ``invoke`` call so the property can assert on it.

    The redirect branch must produce ``calls == []`` after a stream
    completes; the read-only branch is also expected to hit ``[]``
    here because the *handler* does not invoke dispatch directly -
    the orchestrator's ``on_tool_call`` callback does. The scripted
    fake orchestrator below never calls the callback, which keeps
    this property self-contained.
    """

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def invoke(self, tool_call: Any) -> Any:
        self.calls.append(tool_call)
        return {"ok": True}


class _ScriptedOrchestrator:
    """Async generator that yields a pre-baked SSE event sequence."""

    def __init__(self, events: Sequence[SseEvent]) -> None:
        self.events = list(events)
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
            for ev in self.events:
                yield ev

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
    """Compressor that returns the input verbatim when it fits in n."""

    if len(messages) <= n:
        return tuple(messages)
    return tuple(messages[-n:])


def _passthrough_capability_gate(
    tools: Iterable[Any],
    *,
    capabilities: frozenset[str],
) -> Sequence[Any]:
    """Capability gate that allows every tool through.

    The property under test concerns the *redirect* gate (post-LLM),
    not the capability gate (pre-LLM). Letting every tool through
    keeps the test focused on the predicate-driven invariants.
    """

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


def _build_handler(orchestrator: _ScriptedOrchestrator) -> tuple[
    ChatHandler,
    _RecordingAudit,
    _RecordingDispatch,
]:
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
        list_tools=lambda: (),  # banned-tool / capability gate are not
                                # under test here; an empty catalogue
                                # is fine because the orchestrator
                                # fake yields scripted events.
    )
    return ChatHandler(deps), audit, dispatch


async def _drain(handler: ChatHandler) -> list[SseEvent]:
    actor = _ActorFake()
    out: list[SseEvent] = []
    async for ev in handler.stream(_build_request(), actor, _build_dept()):
        out.append(ev)
    return out


# ---------------------------------------------------------------------------
# Predicate decision table
# ---------------------------------------------------------------------------


class TestIsWriteIntentDecisionTable:
    """The three-row write-action decision table expressed as a universal property over
    ``(tool_name, intent)``.
    """

    @settings(
        max_examples=300,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_any_tool_name, intent=_intent_field)
    def test_explicit_intent_takes_priority(
        self, tool_name: str, intent: str | None
    ) -> None:
        """Row 1: ``intent == "write_action_requested"`` ⇒ ``True``
        regardless of ``tool_name``.

        The explicit LLM signal overrides the implicit catalogue
        check; this is the design-mandated priority order.
        """

        call = ToolCall(tool_name=tool_name)
        if intent == "write_action_requested":
            assert is_write_intent(call, llm_intent_field=intent) is True
        # The complementary direction (``intent ≠ explicit ⇒ ?``) is
        # covered by the next two tests, which split the cases by the
        # tool-name branch.

    @settings(
        max_examples=300,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_write_tool_name, intent=_non_write_intent)
    def test_write_tool_name_triggers_intercept_without_explicit_intent(
        self, tool_name: str, intent: str | None
    ) -> None:
        """Row 2: every ``tool_name ∈ WRITE_ACTION_TOOLS`` triggers an
        intercept even when the explicit intent is missing or
        unrelated.

        This is the safety net for an LLM that forgot to populate the
        ``intent`` field - the catalogue is the source of truth.
        """

        call = ToolCall(tool_name=tool_name)
        assert is_write_intent(call, llm_intent_field=intent) is True

    @settings(
        max_examples=300,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_read_only_tool_name, intent=_non_write_intent)
    def test_read_only_tool_with_non_write_intent_is_safe(
        self, tool_name: str, intent: str | None
    ) -> None:
        """Row 3: ``tool_name ∉ WRITE_ACTION_TOOLS`` paired with any
        non-write intent (including ``None``) is *not* an intercept.

        This pins the "no false positive" branch - the predicate must
        let read-only calls through so the chat assistant remains
        useful.
        """

        call = ToolCall(tool_name=tool_name)
        assert is_write_intent(call, llm_intent_field=intent) is False

    @settings(
        max_examples=300,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_any_tool_name, intent=_intent_field)
    def test_predicate_is_deterministic(
        self, tool_name: str, intent: str | None
    ) -> None:
        """The predicate is a pure function: two evaluations with the
        same inputs produce the same output, and it never mutates
        the inputs.

        Determinism is what makes the redirect SSE event safe to
        replay across SSE reconnects (``Last-Event-Id``) - the same
        call always lands the same intercept decision.
        """

        call = ToolCall(tool_name=tool_name)
        first = is_write_intent(call, llm_intent_field=intent)
        second = is_write_intent(call, llm_intent_field=intent)
        assert first == second
        # ``ToolCall`` is frozen so this is doubly guaranteed; assert
        # the read still returns the same name as a smoke check.
        assert call.tool_name == tool_name

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_any_tool_name, intent=_intent_field)
    def test_result_matches_oracle(
        self, tool_name: str, intent: str | None
    ) -> None:
        """Cross-check the predicate against an independent oracle
        that re-states the decision table directly.

        Two independent implementations agreeing on the same input
        space is a strong signal the function is what the design
        document says - and a typo in either implementation surfaces
        immediately.
        """

        call = ToolCall(tool_name=tool_name)
        oracle = (
            intent == "write_action_requested"
            or tool_name in WRITE_ACTION_TOOLS
        )
        assert is_write_intent(call, llm_intent_field=intent) is oracle


class TestWriteActionToolsCatalogueInvariants:
    """Structural pins on the canonical write-tool catalogue.


    Strict typing and cardinality guard against the most common
    silent regression - an entry being added or removed without the
    accompanying spec / design update.
    """

    def test_is_a_frozenset(self) -> None:
        """``WRITE_ACTION_TOOLS`` is immutable at runtime."""

        assert isinstance(WRITE_ACTION_TOOLS, frozenset)

    def test_cardinality_matches_design(self) -> None:
        """The seven entries enumerated in design.md
        §"WriteActionIntercept" are exactly the seven members."""

        assert len(WRITE_ACTION_TOOLS) == 7

    def test_every_member_is_a_non_empty_string(self) -> None:
        """Catch any future drift toward typed tool ids without
        breaking the property's reliance on ``str`` membership."""

        for name in WRITE_ACTION_TOOLS:
            assert isinstance(name, str)
            assert name  # non-empty


# ---------------------------------------------------------------------------
# Handler-level invariants
# ---------------------------------------------------------------------------


class TestChatHandlerWriteActionIntercept:
    """The redirect-and-stop invariant of
    :meth:`ChatHandler.stream` for any ``(tool_name, intent)`` whose
    :func:`is_write_intent` is ``True``, and the no-redirect
    invariant for any pair whose predicate is ``False``.
    """

    @settings(
        max_examples=80,
        deadline=4000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_write_tool_name, intent=_non_write_intent)
    def test_write_tool_name_intercepts_without_dispatch(
        self, tool_name: str, intent: str | None
    ) -> None:
        """For any ``(write_tool, non_write_intent)`` the handler:

        * emits exactly one ``redirect_to_task_creator`` event,
        * emits no ``done`` event (the generator returned early),
        * never calls ``tool_dispatch.invoke``,
        * still writes one ``chat_message`` audit row marking the
          redirect.
        """

        call = ToolCall(tool_name=tool_name)
        # Mirror the real orchestrator's payload contract: the
        # ``tool_call`` event embeds both the structured ``call`` and
        # the LLM-supplied ``intent`` field. The handler unpacks them
        # via :func:`src.chat.handler._is_write_call`.
        write_event = SseEvent(
            type="tool_call",
            payload={"call": call, "intent": intent},
        )
        # A trailing ``done`` event the handler must *not* reach.
        orch = _ScriptedOrchestrator([write_event, SseEvent("done", {})])
        handler, audit, dispatch = _build_handler(orch)

        events = asyncio.run(_drain(handler))
        types = [e.type for e in events]

        assert types.count("redirect_to_task_creator") == 1
        assert "done" not in types
        assert dispatch.calls == []
        # Audit row carries the redirect flag.
        assert len(audit.events) == 1
        assert audit.events[0].action == "chat_message"
        assert audit.events[0].payload is not None
        assert audit.events[0].payload["write_intent_redirected"] is True

    @settings(
        max_examples=40,
        deadline=4000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_any_tool_name)
    def test_explicit_intent_intercepts_for_any_tool(
        self, tool_name: str,
    ) -> None:
        """The explicit ``write_action_requested`` intent overrides
        the catalogue check and redirects regardless of the tool
        name (including read-only tools)."""

        call = ToolCall(tool_name=tool_name)
        write_event = SseEvent(
            type="tool_call",
            payload={"call": call, "intent": "write_action_requested"},
        )
        orch = _ScriptedOrchestrator([write_event, SseEvent("done", {})])
        handler, audit, dispatch = _build_handler(orch)

        events = asyncio.run(_drain(handler))
        types = [e.type for e in events]

        assert types[-1] == "redirect_to_task_creator"
        assert "done" not in types
        assert dispatch.calls == []
        assert audit.events[0].payload is not None
        assert audit.events[0].payload["write_intent_redirected"] is True

    @settings(
        max_examples=80,
        deadline=4000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_read_only_tool_name, intent=_non_write_intent)
    def test_read_only_tool_does_not_redirect(
        self, tool_name: str, intent: str | None
    ) -> None:
        """For any ``(read_only_tool, non_write_intent)`` the handler:

        * propagates the ``tool_call`` event verbatim,
        * reaches the orchestrator's terminal ``done`` event,
        * does not emit a ``redirect_to_task_creator`` event,
        * leaves ``write_intent_redirected`` set to ``False`` in the
          audit row.

        The property does *not* assert that ``tool_dispatch.invoke``
        is called - the orchestrator owns that callback and the
        scripted fake never invokes it. The contract under test is
        the **handler did not bypass** the orchestrator on the
        read-only branch.
        """

        call = ToolCall(tool_name=tool_name)
        read_event = SseEvent(
            type="tool_call",
            payload={"call": call, "intent": intent},
        )
        orch = _ScriptedOrchestrator([read_event, SseEvent("done", {})])
        handler, audit, dispatch = _build_handler(orch)

        events = asyncio.run(_drain(handler))
        types = [e.type for e in events]

        assert "redirect_to_task_creator" not in types
        assert types == ["tool_call", "done"]
        assert dispatch.calls == []
        assert len(audit.events) == 1
        assert audit.events[0].payload is not None
        assert audit.events[0].payload["write_intent_redirected"] is False

    @settings(
        max_examples=40,
        deadline=4000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_write_tool_name, intent=_non_write_intent)
    def test_redirect_payload_carries_tool_name_and_intent(
        self, tool_name: str, intent: str | None
    ) -> None:
        """The ``redirect_to_task_creator`` payload echoes the
        intercepted tool name and the LLM-supplied intent so the
        Streamlit UI can pre-populate the Task Creator
        form. Pinning the payload shape keeps the handler's
        contract observable from outside the unit tests."""

        call = ToolCall(tool_name=tool_name)
        write_event = SseEvent(
            type="tool_call",
            payload={"call": call, "intent": intent},
        )
        orch = _ScriptedOrchestrator([write_event, SseEvent("done", {})])
        handler, _audit, _dispatch = _build_handler(orch)

        events = asyncio.run(_drain(handler))
        redirects = [e for e in events if e.type == "redirect_to_task_creator"]

        assert len(redirects) == 1
        payload = redirects[0].payload
        assert payload["reason"] == "write_action_requested"
        assert payload["tool_name"] == tool_name
        assert payload["intent"] == intent

    @settings(
        max_examples=40,
        deadline=4000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_write_tool_name, intent=_non_write_intent)
    def test_redirect_is_idempotent_under_replay(
        self, tool_name: str, intent: str | None
    ) -> None:
        """Replaying the handler with the same scripted event sequence
        produces the same redirect outcome - required for SSE
        ``Last-Event-Id`` replays to land on the same intercept."""

        call = ToolCall(tool_name=tool_name)
        write_event = SseEvent(
            type="tool_call",
            payload={"call": call, "intent": intent},
        )

        first_events: list[SseEvent] = []
        for _ in range(2):
            orch = _ScriptedOrchestrator([write_event, SseEvent("done", {})])
            handler, _audit, _dispatch = _build_handler(orch)
            events = asyncio.run(_drain(handler))
            if not first_events:
                first_events = events
            else:
                assert [e.type for e in events] == [
                    e.type for e in first_events
                ]
                # Compare redirect payloads; the audit timestamp differs
                # between runs so we restrict the equality check to
                # the SSE stream.
                redirects_a = [
                    e for e in events if e.type == "redirect_to_task_creator"
                ]
                redirects_b = [
                    e
                    for e in first_events
                    if e.type == "redirect_to_task_creator"
                ]
                assert redirects_a == redirects_b


# ---------------------------------------------------------------------------
# Write-Action Intent Detection
# ---------------------------------------------------------------------------


class TestWriteActionIntentDetection:
    """For any user message containing a write-action intent (commit,
    deploy, push, create PR vb.), the chat handler SHALL produce
    ``intent: write_action_requested`` metadata and emit a
    ``redirect_to_task_creator`` SSE event.

    This class tests the ``is_write_intent`` predicate with various
    tool names from ``WRITE_ACTION_TOOLS`` and verifies that:
    - Write-action tools always return True
    - Non-write tools return False (when no explicit intent)
    - The explicit intent field ``"write_action_requested"`` always
      returns True regardless of tool name
    - The handler emits ``redirect_to_task_creator`` SSE event with
      ``intent: write_action_requested`` metadata for write actions
    """

    # ------------------------------------------------------------------
    # Predicate-level: write-action tools always detected
    # ------------------------------------------------------------------

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_write_tool_name, intent=_intent_field)
    def test_write_action_tools_always_detected(
        self, tool_name: str, intent: str | None
    ) -> None:
        """For any tool name drawn from WRITE_ACTION_TOOLS and any
        intent value (including None, arbitrary strings), the
        predicate SHALL return True.

        This validates that the implicit branch (tool-name catalogue)
        is a sufficient condition for write-action detection,
        independent of the LLM intent field.
        """

        call = ToolCall(tool_name=tool_name)
        assert is_write_intent(call, llm_intent_field=intent) is True

    # ------------------------------------------------------------------
    # Predicate-level: non-write tools not falsely detected
    # ------------------------------------------------------------------

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_read_only_tool_name, intent=_non_write_intent)
    def test_non_write_tools_not_detected_without_explicit_intent(
        self, tool_name: str, intent: str | None
    ) -> None:
        """For any tool name NOT in WRITE_ACTION_TOOLS paired with
        any non-write intent, the predicate SHALL return False.

        This ensures no false positives - read-only operations are
        never intercepted unless the LLM explicitly signals write
        intent.
        """

        call = ToolCall(tool_name=tool_name)
        assert is_write_intent(call, llm_intent_field=intent) is False

    # ------------------------------------------------------------------
    # Predicate-level: explicit intent always triggers detection
    # ------------------------------------------------------------------

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_any_tool_name)
    def test_explicit_write_intent_field_always_true(
        self, tool_name: str,
    ) -> None:
        """For any tool name (write or read-only), when
        ``llm_intent_field="write_action_requested"`` the predicate
        SHALL always return True.

        This validates that the explicit LLM intent signal is the
        highest-priority branch in the decision table - it overrides
        the tool-name catalogue check entirely.
        """

        call = ToolCall(tool_name=tool_name)
        assert is_write_intent(
            call, llm_intent_field="write_action_requested"
        ) is True

    # ------------------------------------------------------------------
    # Handler-level: redirect_to_task_creator SSE event emitted
    # ------------------------------------------------------------------

    @settings(
        max_examples=100,
        deadline=4000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_write_tool_name, intent=_non_write_intent)
    def test_write_action_produces_redirect_sse_event(
        self, tool_name: str, intent: str | None
    ) -> None:
        """For any write-action tool call, the chat handler SHALL
        emit a ``redirect_to_task_creator`` SSE event with
        ``intent: write_action_requested`` metadata (via the
        ``reason`` field in the payload).

        This is the end-to-end validation of the redirect behavior: the
        user's write-action request triggers the Task Creator
        redirect mechanism.
        """

        call = ToolCall(tool_name=tool_name)
        write_event = SseEvent(
            type="tool_call",
            payload={"call": call, "intent": intent},
        )
        orch = _ScriptedOrchestrator([write_event, SseEvent("done", {})])
        handler, audit, dispatch = _build_handler(orch)

        events = asyncio.run(_drain(handler))
        redirects = [
            e for e in events if e.type == "redirect_to_task_creator"
        ]

        # Exactly one redirect event emitted
        assert len(redirects) == 1
        # Payload carries write_action_requested metadata
        payload = redirects[0].payload
        assert payload["reason"] == "write_action_requested"
        assert payload["tool_name"] == tool_name
        # The underlying tool is never invoked
        assert dispatch.calls == []

    @settings(
        max_examples=100,
        deadline=4000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_any_tool_name)
    def test_explicit_intent_produces_redirect_with_metadata(
        self, tool_name: str,
    ) -> None:
        """For any tool name with explicit
        ``intent="write_action_requested"``, the handler SHALL emit
        ``redirect_to_task_creator`` with the intent metadata.

        This validates the explicit-intent branch at the handler
        level - even read-only tools get redirected when the LLM
        explicitly classifies the call as a write action.
        """

        call = ToolCall(tool_name=tool_name)
        write_event = SseEvent(
            type="tool_call",
            payload={"call": call, "intent": "write_action_requested"},
        )
        orch = _ScriptedOrchestrator([write_event, SseEvent("done", {})])
        handler, audit, dispatch = _build_handler(orch)

        events = asyncio.run(_drain(handler))
        redirects = [
            e for e in events if e.type == "redirect_to_task_creator"
        ]

        # Redirect emitted with correct metadata
        assert len(redirects) == 1
        payload = redirects[0].payload
        assert payload["reason"] == "write_action_requested"
        assert payload["intent"] == "write_action_requested"
        # Audit records the redirect
        assert len(audit.events) == 1
        assert audit.events[0].payload is not None
        assert audit.events[0].payload["write_intent_redirected"] is True
        # Tool never dispatched
        assert dispatch.calls == []

    # ------------------------------------------------------------------
    # Handler-level: no redirect for non-write actions
    # ------------------------------------------------------------------

    @settings(
        max_examples=100,
        deadline=4000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(tool_name=_read_only_tool_name, intent=_non_write_intent)
    def test_non_write_action_does_not_produce_redirect(
        self, tool_name: str, intent: str | None
    ) -> None:
        """For any non-write tool call without explicit write intent,
        the handler SHALL NOT emit ``redirect_to_task_creator`` and
        SHALL reach the terminal ``done`` event normally.

        This validates the complement: only
        actual write-action intents trigger the redirect mechanism.
        """

        call = ToolCall(tool_name=tool_name)
        read_event = SseEvent(
            type="tool_call",
            payload={"call": call, "intent": intent},
        )
        orch = _ScriptedOrchestrator([read_event, SseEvent("done", {})])
        handler, audit, dispatch = _build_handler(orch)

        events = asyncio.run(_drain(handler))
        types = [e.type for e in events]

        # No redirect event
        assert "redirect_to_task_creator" not in types
        # Stream completes normally with done event
        assert "done" in types
        # Audit records no redirect
        assert audit.events[0].payload is not None
        assert audit.events[0].payload["write_intent_redirected"] is False
