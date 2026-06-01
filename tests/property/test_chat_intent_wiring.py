"""Property test 4 — Chat intent wiring (Streamlit → Task Creator).

Spec: ``platform-real-usage-gaps`` — Property 4.

**Validates: Requirements 4.2, 4.4, 4.7**

Background
----------

When the ``assistant-service`` SSE stream emits an ``intent`` event
with ``payload.intent == "write_action_requested"``, the Streamlit
chat page (``pages/1_chat.py``) stores the payload on
``st.session_state["_pending_task_creator_redirect"]`` so the sibling
Task Creator page (``pages/2_task_creator.py``) can prefill its form
fields on mount.

Conversely, when the stream contains only read-intent events (tokens,
tool_calls with read-only tools, or a ``done`` terminal) the session
state key MUST NOT be set — otherwise the Task Creator would open with
stale / irrelevant prefill data.

Strategy
--------

We use Hypothesis to generate random SSE event sequences that fall
into two categories:

* **Write-intent streams** — contain at least one ``intent`` event
  with ``payload.intent == "write_action_requested"`` and a valid
  ``prefill`` dict.
* **Read-intent streams** — contain only ``token`` and ``done``
  events; no ``intent`` event with ``write_action_requested``.

For each generated stream we simulate the chat page's event-processing
loop (extracted from ``pages/1_chat.py``) against a fake session state
dict and assert the invariant.

The test does NOT import Streamlit itself (which requires a running
server context). Instead it extracts the **pure logic** of the event
loop into a testable helper that mirrors the production code's
branching exactly. This is the standard pattern used by the existing
``test_write_action_intercept.py`` and ``test_streamlit_chat_proxy.py``
tests in this suite.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping, Sequence

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap — expose the messages library so we can import
# SseEvent without pip-installing the package.
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_LIB_SRC_DIRS: Final[tuple[Path, ...]] = (
    _PLATFORM_ROOT / "libs" / "messages" / "src",
)
for _src in _LIB_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)

from messages import SseEvent  # noqa: E402


# ---------------------------------------------------------------------------
# Fake assistant-service client
# ---------------------------------------------------------------------------


@dataclass
class FakeAssistantClient:
    """A list-backed fake that yields pre-scripted SSE events.

    Mirrors the interface of the real assistant-service client injected
    on ``st.session_state["_assistant_client"]`` in production. The
    ``stream`` method returns an iterable of dicts matching the event
    shape consumed by ``pages/1_chat.py``'s event loop.
    """

    events: Sequence[dict[str, Any]] = field(default_factory=list)

    def stream(
        self,
        *,
        dept_id: str,
        session_id: str,
        text: str,
        history: Any = None,
    ) -> list[dict[str, Any]]:
        return list(self.events)


# ---------------------------------------------------------------------------
# Chat page event-processing logic (extracted mirror)
# ---------------------------------------------------------------------------


def process_chat_events(
    events: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Simulate the event-processing loop from ``pages/1_chat.py``.

    This function mirrors the branching logic of the production chat
    page's ``for event in _stream_assistant(user_message)`` loop. It
    returns a dict representing the resulting session state mutations.

    The key invariant under test:
    - If an ``intent`` event with ``write_action_requested`` is seen,
      ``session_state["_pending_task_creator_redirect"]`` is set to
      the event payload.
    - Otherwise the key is absent from the returned state.
    """

    session_state: dict[str, Any] = {}
    redirect_payload: dict[str, Any] | None = None

    for event in events:
        ev_type = event.get("type")
        payload = event.get("payload") or {}

        if ev_type == "token":
            # Accumulate rendered text — no state mutation.
            pass
        elif ev_type == "tool_call":
            # Tool call rendering — no redirect logic here for intent
            # wiring (the write_action_intercept is a separate property).
            pass
        elif (
            ev_type == "intent"
            and payload.get("intent") == "write_action_requested"
        ):
            redirect_payload = payload
        elif ev_type == "redirect_to_task_creator":
            redirect_payload = payload
        elif ev_type in (
            "rate_limit_exhausted",
            "token_cap_exceeded",
            "error",
        ):
            break
        elif ev_type == "done":
            break

    if redirect_payload is not None:
        session_state["_pending_task_creator_redirect"] = redirect_payload

    return session_state


# ---------------------------------------------------------------------------
# Task Creator prefill logic (extracted mirror)
# ---------------------------------------------------------------------------


def task_creator_read_prefill(
    session_state: dict[str, Any],
) -> tuple[dict[str, str], bool]:
    """Simulate the Task Creator page's mount-time prefill read.

    Returns a tuple of (prefill_fields, was_prefilled) where
    ``prefill_fields`` is the dict of form field values extracted from
    the redirect payload, and ``was_prefilled`` indicates whether the
    key was present (and thus consumed/deleted from session state).

    Mirrors the logic in ``pages/2_task_creator.py`` lines that do:
        redirect_payload = st.session_state.pop(
            "_pending_task_creator_redirect", None
        ) or {}
    """

    redirect_payload: dict[str, Any] = (
        session_state.pop("_pending_task_creator_redirect", None) or {}
    )

    prefill: dict[str, str] = redirect_payload.get("prefill") or {}
    was_prefilled = bool(redirect_payload)

    return prefill, was_prefilled


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Strategy for a valid ``prefill`` dict as defined by R4.1 schema.
_prefill_strategy = st.fixed_dictionaries(
    {
        "title": st.text(min_size=1, max_size=80),
        "description": st.text(min_size=1, max_size=200),
        "repo": st.text(
            alphabet=st.characters(whitelist_categories=("Ll", "Nd", "Pd")),
            min_size=1,
            max_size=30,
        ),
        "branch": st.text(
            alphabet=st.characters(whitelist_categories=("Ll", "Nd", "Pd")),
            min_size=1,
            max_size=30,
        ),
    }
)

#: Strategy for a valid workflow type.
_workflow_type_strategy = st.sampled_from(
    [
        "code_change_with_test",
        "pr_review",
        "research",
        "doc_generation",
        "po_review_request",
        "multi_step",
    ]
)

#: Strategy for a write-intent SSE event payload.
_write_intent_payload = st.builds(
    lambda wf, summary, prefill: {
        "intent": "write_action_requested",
        "suggested_workflow_type": wf,
        "context_summary": summary,
        "prefill": prefill,
    },
    wf=_workflow_type_strategy,
    summary=st.text(min_size=5, max_size=100),
    prefill=_prefill_strategy,
)

#: Strategy for token events (read-only, no state mutation).
_token_event = st.builds(
    lambda text: {"type": "token", "payload": {"text": text}},
    text=st.text(min_size=1, max_size=50),
)

#: Strategy for a done event (terminal, no redirect).
_done_event = st.just({"type": "done", "payload": {}})

#: Strategy for a read-intent stream: sequence of tokens followed by done.
_read_intent_stream = st.builds(
    lambda tokens: tokens + [{"type": "done", "payload": {}}],
    tokens=st.lists(_token_event, min_size=1, max_size=10),
)

#: Strategy for a write-intent stream: tokens + intent event + done.
_write_intent_stream = st.builds(
    lambda tokens_before, intent_payload, tokens_after: (
        tokens_before
        + [{"type": "intent", "payload": intent_payload}]
        + tokens_after
        + [{"type": "done", "payload": {}}]
    ),
    tokens_before=st.lists(_token_event, min_size=0, max_size=5),
    intent_payload=_write_intent_payload,
    tokens_after=st.lists(_token_event, min_size=0, max_size=3),
)

#: Strategy for non-write intents (read_action or arbitrary).
_non_write_intent_payload = st.one_of(
    st.just({"intent": "read_action", "context_summary": "reading data"}),
    st.builds(
        lambda intent: {"intent": intent, "context_summary": "misc"},
        intent=st.text(min_size=1, max_size=20).filter(
            lambda s: s != "write_action_requested"
        ),
    ),
)

#: Strategy for a stream with a non-write intent event (should NOT trigger redirect).
_non_write_intent_stream = st.builds(
    lambda tokens, intent_payload: (
        tokens
        + [{"type": "intent", "payload": intent_payload}]
        + [{"type": "done", "payload": {}}]
    ),
    tokens=st.lists(_token_event, min_size=1, max_size=5),
    intent_payload=_non_write_intent_payload,
)


# ---------------------------------------------------------------------------
# Property 4: Chat Intent Wiring
# ---------------------------------------------------------------------------


class TestChatIntentWiringWriteIntent:
    """**Validates: Requirements 4.2, 4.4, 4.7**

    When the assistant-service SSE stream contains an ``intent`` event
    with ``payload.intent == "write_action_requested"``, the chat page
    MUST set ``session_state["_pending_task_creator_redirect"]`` to the
    event payload.
    """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(stream=_write_intent_stream)
    def test_write_intent_sets_redirect_key(
        self, stream: list[dict[str, Any]]
    ) -> None:
        """R4.2: SSE ``intent`` event with ``write_action_requested``
        causes ``_pending_task_creator_redirect`` to be set in session
        state."""

        session_state = process_chat_events(stream)

        assert "_pending_task_creator_redirect" in session_state, (
            "Write-intent stream did not set "
            "'_pending_task_creator_redirect' in session state"
        )

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(stream=_write_intent_stream)
    def test_redirect_payload_contains_intent_field(
        self, stream: list[dict[str, Any]]
    ) -> None:
        """R4.2: The stored payload carries the ``intent`` field set to
        ``write_action_requested``."""

        session_state = process_chat_events(stream)
        payload = session_state["_pending_task_creator_redirect"]

        assert payload["intent"] == "write_action_requested"

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(stream=_write_intent_stream)
    def test_redirect_payload_contains_prefill(
        self, stream: list[dict[str, Any]]
    ) -> None:
        """R4.4: The stored payload carries a ``prefill`` dict with
        ``title``, ``description``, ``repo``, ``branch`` keys so the
        Task Creator can prefill its form."""

        session_state = process_chat_events(stream)
        payload = session_state["_pending_task_creator_redirect"]
        prefill = payload.get("prefill")

        assert prefill is not None, "Redirect payload missing 'prefill' dict"
        assert "title" in prefill
        assert "description" in prefill
        assert "repo" in prefill
        assert "branch" in prefill

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(stream=_write_intent_stream)
    def test_task_creator_consumes_and_deletes_key(
        self, stream: list[dict[str, Any]]
    ) -> None:
        """R4.4: Task Creator page reads the redirect payload and
        removes the key from session state (pop semantics) so it
        doesn't persist across page navigations."""

        session_state = process_chat_events(stream)
        assert "_pending_task_creator_redirect" in session_state

        prefill, was_prefilled = task_creator_read_prefill(session_state)
        assert was_prefilled is True
        assert "_pending_task_creator_redirect" not in session_state

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(stream=_write_intent_stream)
    def test_task_creator_extracts_prefill_fields(
        self, stream: list[dict[str, Any]]
    ) -> None:
        """R4.4: Task Creator extracts prefill fields from the payload
        and they are non-empty strings (generated by the LLM)."""

        session_state = process_chat_events(stream)
        prefill, was_prefilled = task_creator_read_prefill(session_state)

        assert was_prefilled is True
        assert prefill.get("title")
        assert prefill.get("description")
        assert prefill.get("repo")
        assert prefill.get("branch")


class TestChatIntentWiringReadIntent:
    """**Validates: Requirements 4.2, 4.4, 4.7**

    When the assistant-service SSE stream does NOT contain a
    ``write_action_requested`` intent event, the chat page MUST NOT
    set ``session_state["_pending_task_creator_redirect"]``.
    """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(stream=_read_intent_stream)
    def test_read_only_stream_does_not_set_redirect_key(
        self, stream: list[dict[str, Any]]
    ) -> None:
        """R4.7: A stream with only token + done events (pure read
        interaction) does NOT set the redirect key."""

        session_state = process_chat_events(stream)

        assert "_pending_task_creator_redirect" not in session_state, (
            "Read-only stream incorrectly set "
            "'_pending_task_creator_redirect' in session state"
        )

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(stream=_non_write_intent_stream)
    def test_non_write_intent_does_not_set_redirect_key(
        self, stream: list[dict[str, Any]]
    ) -> None:
        """R4.7: A stream with an ``intent`` event whose ``intent``
        field is NOT ``write_action_requested`` (e.g. ``read_action``)
        does NOT set the redirect key."""

        session_state = process_chat_events(stream)

        assert "_pending_task_creator_redirect" not in session_state, (
            "Non-write intent stream incorrectly set "
            "'_pending_task_creator_redirect' in session state"
        )

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(stream=_read_intent_stream)
    def test_task_creator_reports_no_prefill_for_read_stream(
        self, stream: list[dict[str, Any]]
    ) -> None:
        """R4.7: When no redirect payload exists, Task Creator's
        prefill read returns empty and ``was_prefilled=False``."""

        session_state = process_chat_events(stream)
        prefill, was_prefilled = task_creator_read_prefill(session_state)

        assert was_prefilled is False
        assert prefill == {}


class TestChatIntentWiringEdgeCases:
    """Edge-case properties for the intent wiring contract.

    **Validates: Requirements 4.2, 4.4, 4.7**
    """

    def test_empty_stream_does_not_set_redirect(self) -> None:
        """An empty event stream (assistant-service unreachable)
        produces no redirect."""

        session_state = process_chat_events([])
        assert "_pending_task_creator_redirect" not in session_state

    def test_error_before_intent_does_not_set_redirect(self) -> None:
        """If an error event terminates the stream before any intent
        event, no redirect is set."""

        stream = [
            {"type": "token", "payload": {"text": "processing..."}},
            {"type": "error", "payload": {"message": "LLM timeout"}},
            # Intent event after error — should not be reached.
            {
                "type": "intent",
                "payload": {
                    "intent": "write_action_requested",
                    "prefill": {"title": "x", "description": "y", "repo": "r", "branch": "b"},
                },
            },
        ]
        session_state = process_chat_events(stream)
        assert "_pending_task_creator_redirect" not in session_state

    def test_intent_event_with_none_payload_does_not_crash(self) -> None:
        """An intent event with ``None`` payload is handled gracefully
        (no redirect, no crash)."""

        stream = [
            {"type": "intent", "payload": None},
            {"type": "done", "payload": {}},
        ]
        session_state = process_chat_events(stream)
        assert "_pending_task_creator_redirect" not in session_state

    def test_multiple_write_intents_last_one_wins(self) -> None:
        """If multiple write-intent events appear (unlikely but
        possible), the last one's payload is stored."""

        stream = [
            {
                "type": "intent",
                "payload": {
                    "intent": "write_action_requested",
                    "suggested_workflow_type": "research",
                    "context_summary": "first",
                    "prefill": {"title": "A", "description": "a", "repo": "r1", "branch": "b1"},
                },
            },
            {
                "type": "intent",
                "payload": {
                    "intent": "write_action_requested",
                    "suggested_workflow_type": "code_change_with_test",
                    "context_summary": "second",
                    "prefill": {"title": "B", "description": "b", "repo": "r2", "branch": "b2"},
                },
            },
            {"type": "done", "payload": {}},
        ]
        session_state = process_chat_events(stream)
        payload = session_state["_pending_task_creator_redirect"]
        assert payload["context_summary"] == "second"
        assert payload["prefill"]["title"] == "B"

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        write_stream=_write_intent_stream,
        read_stream=_read_intent_stream,
    )
    def test_write_then_read_session_isolation(
        self,
        write_stream: list[dict[str, Any]],
        read_stream: list[dict[str, Any]],
    ) -> None:
        """Simulates a user sending a write-intent message, navigating
        to Task Creator (which pops the key), then sending a read-only
        message. The second message must NOT re-set the redirect key.

        This validates the full lifecycle: set → pop → absent."""

        # First message: write intent → key set.
        session_state = process_chat_events(write_stream)
        assert "_pending_task_creator_redirect" in session_state

        # Task Creator consumes the key.
        _, was_prefilled = task_creator_read_prefill(session_state)
        assert was_prefilled is True
        assert "_pending_task_creator_redirect" not in session_state

        # Second message: read-only → key stays absent.
        # In production the session_state persists across page renders;
        # we simulate by processing the read stream and merging into
        # the same state dict.
        read_result = process_chat_events(read_stream)
        session_state.update(read_result)
        assert "_pending_task_creator_redirect" not in session_state
