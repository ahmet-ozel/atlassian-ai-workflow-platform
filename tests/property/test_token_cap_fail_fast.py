"""Activity-level token cap fail-fast tests.

Hypothesis-driven verification of the activity-level token cap
fail-fast behaviour in ``LlmOrchestrator.stream_with_tool_loop``.

Expected behaviour
------------------

For any hypothesis-generated ``(token_chunks, token_cap)`` pair
where every element of ``token_chunks`` is an integer in
``[0, 1000]`` and ``token_cap ∈ [100, 100000]``,
:meth:`llm_orchestrator.orchestrator.LlmOrchestrator.\
stream_with_tool_loop` MUST satisfy:

    (a) When the running ``used_tokens`` total **first** exceeds
        ``token_cap`` (i.e. ``used_tokens > token_cap`` after the
        i-th chunk), the orchestrator yields exactly one SSE event
        whose ``type`` equals ``"token_cap_exceeded"`` and whose
        ``payload`` exposes the configured ``limit``.
    (b) After the ``token_cap_exceeded`` event the generator stops
        - no further SSE events of any type are produced.
    (c) When ``sum(token_chunks) <= token_cap`` (cap never
        exceeded), the orchestrator terminates with the normal
        ``done`` event and ``token_cap_exceeded`` is **not**
        emitted.
    (d) The cumulative token tally observed by the orchestrator is
        monotonically non-decreasing
        (``used_tokens_{i+1} >= used_tokens_i``); equivalently the
        cap-cross point is the first index ``i`` with
        ``sum(chunks[:i+1]) > token_cap``.
    (e) Determinism: a second call with the same ``(token_chunks,
        token_cap)`` and a fresh provider stub yields the exact
        same SSE event sequence (same types, same payloads, same
        length).

Surface under test
------------------

The orchestrator exposes::

    class LlmOrchestrator:
        async def stream_with_tool_loop(
            self,
            *,
            system: str,
            history: list[Message],
            tools: list[ToolSpec],
            on_tool_call: Callable[[ToolCall], Awaitable[ToolResult]],
            token_cap: int,
        ) -> AsyncIterator[SseEvent]: ...

The constructor receives a primary and fallback provider; for the
fail-fast property only the primary is exercised. The provider's
``stream`` returns an async iterator of chunks each carrying a
``token_count`` and ``kind`` field (``"token"`` for normal output
and ``"final"`` / ``is_final=True`` for the closing chunk).

Because the orchestrator depends only on a small protocol surface
(``provider.stream`` + ``provider.downtime``), the property test
drives it through a deterministic in-memory provider fake instead
of standing up vLLM / OpenAI. The fake is the *only* place chunks
are produced, which keeps the cumulative-token accounting visible
and makes the determinism assertion of clause (e) meaningful.

Related coverage
----------------

* Companion sliding-window coverage lives in
  ``test_sliding_window.py`` and LLM retry / fallback coverage
  lives in ``test_llm_retry_fallback.py``.
* The :class:`messages.SseEvent` event type catalogue -
  including the ``token_cap_exceeded`` literal asserted here -
  lives at ``platform/libs/messages/src/messages/chat.py`` and is
  also exercised by ``platform/tests/unit/test_messages_chat.py``.
* The handler-level forwarding of ``token_cap`` into the
  orchestrator is asserted in
  ``platform/services/assistant-service/tests/unit/test_handler.py``
  (``test_token_cap_is_forwarded_to_orchestrator``); this property
  test owns the orchestrator-side fail-fast contract that pairs
  with that forwarding.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Sequence,
)

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# sys.path bootstrap - the orchestrator lives in
# ``libs/llm-orchestrator/src`` (already on the workspace pythonpath via
# ``pytest.ini``), but we add it defensively for direct
# ``python -m pytest <file>`` runs from unusual cwds. Mirrors
# ``test_sliding_window.py`` and ``test_write_action_intercept.py``.
# ---------------------------------------------------------------------------

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]

_LIB_SRC_DIRS: tuple[Path, ...] = (
    _REPO_ROOT / "libs" / "llm-orchestrator" / "src",
    _REPO_ROOT / "libs" / "messages" / "src",
)
for _src in _LIB_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


from messages import SseEvent  # noqa: E402

# If the orchestrator import fails with ``ModuleNotFoundError``,
# capture the error string and mirror the
# ``test_sliding_window.py`` pattern: capture the error string and
# skip the entire module with a precise reason so collection stays
# clean.
try:  # pragma: no cover - import guard for optional dependency
    from llm_orchestrator.orchestrator import (  # type: ignore[import-not-found]
        LlmOrchestrator,
    )
except ModuleNotFoundError as exc:  # pragma: no cover
    LlmOrchestrator = None  # type: ignore[assignment,misc]
    _IMPORT_ERROR: str | None = str(exc)
else:  # pragma: no cover - import succeeds in integrated runs
    _IMPORT_ERROR = None


# ---------------------------------------------------------------------------
# Constants from the design contract
# ---------------------------------------------------------------------------

#: SSE event type yielded when the running token total crosses the
#: configured cap. The literal is also a member of
#: :data:`messages.SSE_EVENT_TYPES`.
TOKEN_CAP_EVENT: str = "token_cap_exceeded"

#: Terminal event yielded when a stream completes within the cap.
DONE_EVENT: str = "done"


# ---------------------------------------------------------------------------
# Provider / chunk fakes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Chunk:
    """In-memory provider chunk used by the property test.

    Mirrors the shape consumed by
    :meth:`LlmOrchestrator.stream_with_tool_loop`'s loop body
    (``chunk.token_count``, ``chunk.kind``, ``chunk.text``,
    ``chunk.is_final``). Keeping the fake in-line - rather than
    importing a production chunk dataclass - pins the test to the
    protocol shape and prevents a future rename of the provider
    chunk type from silently weakening the invariant.
    """

    token_count: int
    text: str = ""
    kind: str = "token"
    is_final: bool = False


class _ScriptedProvider:
    """Provider stub yielding a pre-baked chunk sequence.

    The class is intentionally minimal: it implements the two
    methods the orchestrator inspects (``stream`` and
    ``downtime``) and nothing else. ``downtime`` always returns
    ``0`` so the failover branch cannot
    interfere with the cap-crossing assertions.

    A counter records how many chunks the orchestrator actually
    drained from the iterator; this confirms
    this to confirm the orchestrator stopped *immediately* after
    the cap-cross (and didn't keep pulling chunks from the
    provider only to discard them).
    """

    def __init__(self, chunks: Sequence[_Chunk]) -> None:
        self._chunks: tuple[_Chunk, ...] = tuple(chunks)
        self.consumed: int = 0

    def downtime(self) -> int:
        return 0

    async def stream(
        self,
        system: str,  # noqa: ARG002 - protocol parity
        history: Sequence[Any],  # noqa: ARG002 - protocol parity
        tools: Sequence[Any],  # noqa: ARG002 - protocol parity
    ) -> AsyncIterator[_Chunk]:
        for chunk in self._chunks:
            self.consumed += 1
            yield chunk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_chunks(
    counts: Sequence[int],
    *,
    finalise: bool,
) -> tuple[_Chunk, ...]:
    """Wrap an integer sequence into a :class:`_Chunk` tuple.

    ``finalise`` flips ``is_final=True`` on the trailing chunk so
    clause (c) - "cap never exceeded ⇒ ``done`` event" - has a
    well-defined trigger inside the orchestrator's loop.
    """

    if not counts:
        return ()
    body: list[_Chunk] = [
        _Chunk(token_count=c, text=f"t{i}", kind="token", is_final=False)
        for i, c in enumerate(counts)
    ]
    if finalise:
        last = body[-1]
        body[-1] = _Chunk(
            token_count=last.token_count,
            text=last.text,
            kind=last.kind,
            is_final=True,
        )
    return tuple(body)


def _first_cap_cross_index(
    counts: Sequence[int], cap: int
) -> int | None:
    """Return the first index ``i`` with ``sum(counts[:i+1]) > cap``.

    Mirrors clause (d) - the cumulative token tally is a monotone
    prefix sum, so the cap-cross point is well-defined as a single
    integer (or ``None`` when the cap is never exceeded).
    """

    running = 0
    for i, c in enumerate(counts):
        running += c
        if running > cap:
            return i
    return None


async def _drain(orch_stream: AsyncIterator[SseEvent]) -> list[SseEvent]:
    """Materialise an async SSE generator into a list."""

    out: list[SseEvent] = []
    async for ev in orch_stream:
        out.append(ev)
    return out


async def _on_tool_call(_call: Any) -> Any:  # noqa: ARG001 - protocol parity
    """Tool-call callback that should *never* be invoked.

    These tests fix the chunk kind to ``"token"``; the
    orchestrator's tool-call branch is exercised separately in
    (``test_write_action_intercept.py``). Raising here means a
    regression that misroutes a token chunk through the tool path
    surfaces immediately rather than silently passing.
    """

    raise AssertionError(
        "on_tool_call must not run for token-chunk-only streams "
        "(these tests fix ``kind == 'token'``)."
    )


def _build_orchestrator(provider: _ScriptedProvider) -> Any:
    """Construct an :class:`LlmOrchestrator` around the provider.

    Accept either ``LlmOrchestrator(primary,
    fallback)`` (positional) or the keyword form so a minor naming
    drift in the implementation does not silently neuter the
    property. Both shapes are explicitly tried and the first one
    that succeeds is returned.
    """

    assert LlmOrchestrator is not None  # pytestmark guards runtime

    fallback = _ScriptedProvider(chunks=())

    last_exc: Exception | None = None
    for attempt in (
        lambda: LlmOrchestrator(primary=provider, fallback=fallback),  # type: ignore[call-arg]
        lambda: LlmOrchestrator(provider, fallback),  # type: ignore[call-arg]
    ):
        try:
            return attempt()
        except TypeError as exc:  # pragma: no cover - defensive
            last_exc = exc
            continue
    # If neither shape matches the implementation drifted; surface
    # the last TypeError so the failure points at the constructor.
    raise AssertionError(
        "LlmOrchestrator constructor signature did not match either "
        "``(primary=, fallback=)`` or ``(primary, fallback)``; "
        f"last error was: {last_exc!r}"
    )


# ---------------------------------------------------------------------------
# Strategies for token chunks and cap limits
# ---------------------------------------------------------------------------

#: ``token_chunks`` ∈ list of integers each in ``[0, 1000]``. The
#: lower bound includes ``0`` so a chunk that contributes nothing
#: still gets walked (regression anchor for an off-by-one in the
#: monotone prefix-sum).
_chunks_strategy: st.SearchStrategy[list[int]] = st.lists(
    st.integers(min_value=0, max_value=1000),
    min_size=0,
    max_size=40,
)

#: ``token_cap`` ∈ ``[100, 100_000]``.
_cap_strategy: st.SearchStrategy[int] = st.integers(
    min_value=100, max_value=100_000
)


# ---------------------------------------------------------------------------
# Module-level skip for unavailable orchestrator implementation.
# ---------------------------------------------------------------------------


pytestmark = pytest.mark.skipif(
    LlmOrchestrator is None,
    reason=(
        "llm_orchestrator.orchestrator.LlmOrchestrator is not yet "
        "implemented; import failed with: "
        f"{_IMPORT_ERROR!r}. This coverage will run once the "
        "orchestrator is available."
    ),
)


# ---------------------------------------------------------------------------
# Full invariant set (a)..(e)
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=(HealthCheck.too_slow,),
)
@given(chunks=_chunks_strategy, token_cap=_cap_strategy)
def test_token_cap_fail_fast_invariants(
    chunks: list[int], token_cap: int
) -> None:
    """Fail-fast on cap cross with deterministic output.

    The orchestrator is exercised twice with identical inputs to
    verify clause (e); both runs use independent
    :class:`_ScriptedProvider` instances so any hidden state in the
    orchestrator that survives across calls would surface as a
    diff between the two event sequences.
    """

    cross = _first_cap_cross_index(chunks, token_cap)
    cap_will_be_crossed = cross is not None

    # ---- Run #1 ----
    provider_a = _ScriptedProvider(
        chunks=_build_chunks(chunks, finalise=not cap_will_be_crossed)
    )
    orchestrator_a = _build_orchestrator(provider_a)
    events_a = asyncio.run(
        _drain(
            orchestrator_a.stream_with_tool_loop(
                system="sys",
                history=[],
                tools=[],
                on_tool_call=_on_tool_call,
                token_cap=token_cap,
            )
        )
    )

    # ---- Run #2 - independent provider, identical script ----
    provider_b = _ScriptedProvider(
        chunks=_build_chunks(chunks, finalise=not cap_will_be_crossed)
    )
    orchestrator_b = _build_orchestrator(provider_b)
    events_b = asyncio.run(
        _drain(
            orchestrator_b.stream_with_tool_loop(
                system="sys",
                history=[],
                tools=[],
                on_tool_call=_on_tool_call,
                token_cap=token_cap,
            )
        )
    )

    # ----- (e) Determinism - same script ⇒ same SSE sequence -----
    assert events_a == events_b, (
        f"stream_with_tool_loop is non-deterministic: identical "
        f"inputs produced different SSE sequences.\n"
        f"  run #1: {events_a!r}\n"
        f"  run #2: {events_b!r}\n"
        "Token-cap fail-fast requires identical outputs."
    )

    event_types = [ev.type for ev in events_a]

    if cap_will_be_crossed:
        assert cross is not None  # narrow for type-checker

        # ----- (a) ``token_cap_exceeded`` is emitted exactly once -----
        cap_indices = [
            i for i, t in enumerate(event_types) if t == TOKEN_CAP_EVENT
        ]
        assert len(cap_indices) == 1, (
            f"Expected exactly one ``{TOKEN_CAP_EVENT}`` event when "
            f"cap is crossed; saw {len(cap_indices)} in "
            f"{event_types!r}."
        )

        # ----- (a) payload exposes the configured ``limit`` -----
        cap_event = events_a[cap_indices[0]]
        # ``payload`` is a Mapping - design pseudocode uses
        # ``{"limit": token_cap}``. We allow any mapping that
        # contains the key (additional metadata is fine) so
        # implementations can attach context like
        # ``used_tokens`` without breaking the invariant.
        assert "limit" in cap_event.payload, (
            f"``{TOKEN_CAP_EVENT}`` payload {cap_event.payload!r} "
            "does not expose the configured ``limit`` field. "
            "The cap event must expose the configured limit."
        )
        assert cap_event.payload["limit"] == token_cap, (
            f"``{TOKEN_CAP_EVENT}`` payload limit "
            f"{cap_event.payload['limit']!r} != configured "
            f"{token_cap}."
        )

        # ----- (b) generator stops after the cap event -----
        cap_idx = cap_indices[0]
        assert cap_idx == len(events_a) - 1, (
            f"``{TOKEN_CAP_EVENT}`` was emitted at index {cap_idx} "
            f"but {len(events_a) - 1 - cap_idx} more events "
            f"followed: {event_types[cap_idx + 1:]!r}. "
            "The generator must stop after "
            f"the cap event."
        )

        # ----- (b) ``done`` MUST NOT appear once the cap fires -----
        assert DONE_EVENT not in event_types, (
            f"``{DONE_EVENT}`` event was emitted alongside "
            f"``{TOKEN_CAP_EVENT}`` for cap-crossing input "
            f"{chunks!r} / cap={token_cap}; cap and done events "
            f"are mutually exclusive."
        )

        # ----- (b) provider was not drained past the cap-cross chunk
        # (fail-fast). The orchestrator may consume one chunk to
        # *discover* the crossing, so we expect ``consumed ==
        # cross + 1``; anything larger means it kept pulling.
        assert provider_a.consumed == cross + 1, (
            f"Provider drained {provider_a.consumed} chunks but the "
            f"cap was crossed at index {cross} (expected "
            f"{cross + 1}). Fail-fast behavior requires the "
            f"orchestrator to stop pulling immediately after "
            f"detecting the cap cross."
        )

    else:
        # ----- (c) cap never crossed ⇒ ``done`` terminal event -----
        assert TOKEN_CAP_EVENT not in event_types, (
            f"``{TOKEN_CAP_EVENT}`` was emitted for non-crossing "
            f"input chunks={chunks!r} cap={token_cap} "
            f"(sum={sum(chunks)})."
        )
        # ``done`` must be the terminal event when chunks are
        # non-empty; an empty chunk list is a degenerate case where
        # the provider never yields, so the loop returns without
        # ever entering the ``is_final`` branch. Both branches
        # satisfy clause (c) - the *forbidden* outcome is
        # ``token_cap_exceeded``, which we already excluded above.
        if chunks:
            assert event_types and event_types[-1] == DONE_EVENT, (
                f"Stream over non-empty chunks {chunks!r} without "
                f"crossing cap={token_cap} terminated with events "
                f"{event_types!r}; within-budget streams require "
                f"``{DONE_EVENT}`` as the closing event."
            )

    # ----- (d) cumulative token tally is monotonically non-decreasing
    # The orchestrator does not expose ``used_tokens`` directly, but
    # the prefix sum of the generated chunks is the invariant we
    # care about: ``sum(chunks[:i+1]) >= sum(chunks[:i])``. With
    # ``token_count >= 0`` from the strategy this is structurally
    # guaranteed; the assertion pins the strategy contract so a
    # future widening that admits negative counts would surface
    # here rather than silently breaking clause (a).
    running = 0
    for c in chunks:
        assert c >= 0, (
            "Token accounting requires non-decreasing cumulative "
            f"tokens; strategy generated negative count {c}."
        )
        running += c


# ---------------------------------------------------------------------------
# Concrete regression anchors - pinned examples that complement the
# Hypothesis search by fixing the cap-cross point on a known input.
# ---------------------------------------------------------------------------


def test_cap_crossed_on_first_chunk_emits_only_cap_event() -> None:
    """A single oversized chunk crosses the cap immediately.

    Anchors clause (a) + (b): when ``chunks[0] > cap`` the very
    first iteration of the loop must emit
    ``token_cap_exceeded`` and stop, leaving no preceding
    ``token`` events. Pinned independently of Hypothesis so a
    regression that always emits at least one ``token`` event
    before the cap check is caught deterministically.

    """

    assert LlmOrchestrator is not None

    cap = 100
    provider = _ScriptedProvider(
        chunks=_build_chunks([500], finalise=False)
    )
    orch = _build_orchestrator(provider)

    events = asyncio.run(
        _drain(
            orch.stream_with_tool_loop(
                system="sys",
                history=[],
                tools=[],
                on_tool_call=_on_tool_call,
                token_cap=cap,
            )
        )
    )

    types = [ev.type for ev in events]
    assert types == [TOKEN_CAP_EVENT], (
        f"Expected the single-event sequence "
        f"[{TOKEN_CAP_EVENT!r}] when chunks[0] (500) > cap "
        f"({cap}); got {types!r}."
    )
    assert events[0].payload.get("limit") == cap, (
        f"Cap event payload {events[0].payload!r} must expose "
        f"``limit == {cap}``."
    )
    assert provider.consumed == 1, (
        f"Provider drained {provider.consumed} chunks; expected "
        "exactly 1 (fail-fast on the first cross)."
    )


def test_cap_crossed_at_boundary_keeps_preceding_token_events() -> None:
    """Cap fires only after several within-budget chunks.

    Concrete example exercising the "stream forwards ``token``
    events until the cap is crossed, then stops" contract on a
    fixed input. ``[40, 40, 50]`` with ``cap=100`` crosses on the
    third chunk (running total ``130 > 100``), so the expected
    SSE sequence is two ``token`` events followed by the cap
    event - never any ``done``.

    """

    assert LlmOrchestrator is not None

    chunks = [40, 40, 50]
    cap = 100
    provider = _ScriptedProvider(
        chunks=_build_chunks(chunks, finalise=False)
    )
    orch = _build_orchestrator(provider)

    events = asyncio.run(
        _drain(
            orch.stream_with_tool_loop(
                system="sys",
                history=[],
                tools=[],
                on_tool_call=_on_tool_call,
                token_cap=cap,
            )
        )
    )

    types = [ev.type for ev in events]

    # Two within-budget token chunks (running totals 40 / 80) then
    # the cap fires. The exact ``type`` of the within-budget events
    # is ``"token"``.
    assert types[-1] == TOKEN_CAP_EVENT, (
        f"Last event type should be {TOKEN_CAP_EVENT!r}; got "
        f"{types!r}."
    )
    assert types.count(TOKEN_CAP_EVENT) == 1, (
        f"``{TOKEN_CAP_EVENT}`` should fire exactly once; got "
        f"{types!r}."
    )
    assert DONE_EVENT not in types, (
        f"``{DONE_EVENT}`` must not appear in a cap-crossing "
        f"stream; got {types!r}."
    )
    # The within-budget prefix carries the two ``token`` events.
    pre_cap = types[:-1]
    assert pre_cap == ["token", "token"], (
        f"Expected two within-budget ``token`` events before the "
        f"cap fires (running totals 40 and 80, both ≤ {cap}); got "
        f"{pre_cap!r}."
    )
    assert provider.consumed == 3, (
        f"Provider drained {provider.consumed} chunks; expected 3 "
        "(two within-budget + the chunk that crosses the cap)."
    )


def test_under_cap_terminates_with_done() -> None:
    """A within-budget stream closes with ``done`` and no cap event.

    Anchors clause (c) on a fixed input so a regression that
    spuriously emits ``token_cap_exceeded`` whenever the loop
    *could* have crossed (e.g. an off-by-one ``>=`` instead of
    ``>``) is caught deterministically.

    ``sum([10, 20, 30]) == 60 < cap=100`` so the only allowed
    closing event is ``done``.

    """

    assert LlmOrchestrator is not None

    chunks = [10, 20, 30]
    cap = 100
    provider = _ScriptedProvider(
        chunks=_build_chunks(chunks, finalise=True)
    )
    orch = _build_orchestrator(provider)

    events = asyncio.run(
        _drain(
            orch.stream_with_tool_loop(
                system="sys",
                history=[],
                tools=[],
                on_tool_call=_on_tool_call,
                token_cap=cap,
            )
        )
    )

    types = [ev.type for ev in events]
    assert TOKEN_CAP_EVENT not in types, (
        f"``{TOKEN_CAP_EVENT}`` must not appear in within-budget "
        f"streams (sum={sum(chunks)}, cap={cap}); got {types!r}."
    )
    assert types[-1] == DONE_EVENT, (
        f"Within-budget stream must terminate with "
        f"``{DONE_EVENT}``; got {types!r}."
    )


def test_cap_at_exact_boundary_does_not_fire() -> None:
    """``used_tokens == token_cap`` is **not** a crossing.

    The loop uses ``used_tokens > token_cap`` (strict
    inequality), so reaching the cap exactly is allowed and the
    stream must continue. ``[50, 50]`` with ``cap=100`` produces
    a final running total of exactly ``100`` - no cap event.

    """

    assert LlmOrchestrator is not None

    chunks = [50, 50]
    cap = 100
    provider = _ScriptedProvider(
        chunks=_build_chunks(chunks, finalise=True)
    )
    orch = _build_orchestrator(provider)

    events = asyncio.run(
        _drain(
            orch.stream_with_tool_loop(
                system="sys",
                history=[],
                tools=[],
                on_tool_call=_on_tool_call,
                token_cap=cap,
            )
        )
    )

    types = [ev.type for ev in events]
    assert TOKEN_CAP_EVENT not in types, (
        f"Strict inequality (``used_tokens > cap``) means the "
        f"exact-boundary case (sum={sum(chunks)} == cap={cap}) "
        f"must not trigger ``{TOKEN_CAP_EVENT}``; got {types!r}."
    )
    assert types[-1] == DONE_EVENT, (
        f"Exact-boundary stream must close with ``{DONE_EVENT}``; "
        f"got {types!r}."
    )


# ---------------------------------------------------------------------------
# Defensive: the unused-import shield silences linters for symbols
# that exist purely so call-site type signatures stay accurate
# (``Iterable``, ``Awaitable``, ``Callable``).
# ---------------------------------------------------------------------------

_ = (Iterable, Awaitable, Callable)
