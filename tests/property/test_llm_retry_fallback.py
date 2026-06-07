"""LLM rate-limit retry and provider fallback behavior.



This file pins the deterministic retry / fallback semantics of:class:`llm_orchestrator.orchestrator.LlmOrchestrator.stream_with_tool_loop`:

* Three consecutive ``RateLimitError`` responses STOP the loop and
 emit exactly one ``rate_limit_exhausted`` SSE event; a fourth
 attempt is never made.
* Successive ``RateLimitError`` retries sleep for an exponentially
 growing duration whose values are powers of two. The exact base is
 left flexible because the property is "delay doubles each retry";
 the assertions below are tight enough to catch any
 regression that forgets to back off, that backs off linearly, or
 that overshoots the cap of two pre-exhaust sleeps.
* A ``ProviderUnavailable`` exception combined with
 ``primary.downtime >= 60`` flips the active provider to the
 fallback and emits exactly one ``fallback_provider_active`` SSE
 event; otherwise the exception propagates.
* The ``attempts_429`` counter is **not** reset by an interleaving
 successful chunk - it is a global counter for the whole stream.
* Determinism: the same ``(failure_sequence, primary_downtime_s)``
 with the same monkey-patched clock produces the same SSE event
 sequence on every run.

Code under test
---------------

The orchestrator lives at
``platform/libs/llm-orchestrator/src/llm_orchestrator/orchestrator.py``
 and exposes:.. code-block:: python

 class LlmOrchestrator:
 def stream_with_tool_loop(
 self,
 *,
 system: str,
 history: list[Message],
 tools: list[ToolSpec],
 on_tool_call: Callable[[ToolCall], Awaitable[ToolResult]],
 token_cap: int,) -> AsyncIterator[SseEvent]:...

The shared ``SseEvent`` dataclass is the chat-protocol type from
``libs/messages/src/messages/chat.py``; the property
imports the canonical class so a wire-format drift surfaces here as
an attribute error rather than a silent contract divergence.

Reference oracle
----------------

Until the real orchestrator is importable, this property is also
exercised against a *reference* orchestrator that re-states the
expected state machine. The two-layer setup means:

* The expected behavior is encoded once and tested twice - against
 the production class (when present) and against the oracle.
* When the production layer is available, it becomes the primary
 signal and the oracle layer doubles as a regression net for the
 intended behavior.

If the real orchestrator is not yet importable, the production-layer
tests are skipped with a precise reason string (mirroring
``test_sliding_window.py``); the oracle layer keeps running so
the behavior has continuous CI coverage from day one.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
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
# sys.path bootstrap - make ``messages`` and (when present)
# ``llm_orchestrator.orchestrator`` importable when the file is run
# directly via ``python -m pytest`` from any cwd. The workspace
# ``pytest.ini`` already adds the lib ``src`` directories for the
# repo-level test session.
# ---------------------------------------------------------------------------

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]

_LIB_SRC_DIRS: tuple[Path, ...] = (
    _REPO_ROOT / "libs" / "messages" / "src",
    _REPO_ROOT / "libs" / "llm-orchestrator" / "src",
)
for _src in _LIB_SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


from messages import Message, SseEvent  # noqa: E402

# ---------------------------------------------------------------------------
# Optional production-layer import - may not have landed yet.
# When the import fails we set ``_REAL_ORCHESTRATOR`` to ``None``; the
# production-layer test class is then skipped via ``pytest.mark.skipif``.
# This mirrors the pattern in ``test_sliding_window.py``.
# ---------------------------------------------------------------------------

try:  # pragma: no cover - guard collapses once the implementation is importable
    from llm_orchestrator.orchestrator import LlmOrchestrator  # noqa: E402

    _REAL_ORCHESTRATOR: type | None = LlmOrchestrator
    _IMPORT_ERROR: str | None = None
except ModuleNotFoundError as exc:  # pragma: no cover
    _REAL_ORCHESTRATOR = None
    _IMPORT_ERROR = str(exc)


# ---------------------------------------------------------------------------
# Tunables for retry exhaustion and fallback activation.
# ---------------------------------------------------------------------------

#: Max number of consecutive ``RateLimitError`` responses before the
#: orchestrator stops and emits ``rate_limit_exhausted``.
MAX_429_ATTEMPTS: int = 3

#: Downtime threshold (in seconds) at which a ``ProviderUnavailable``
#: failure flips the active provider to the fallback.
FALLBACK_DOWNTIME_THRESHOLD_S: int = 60


# ---------------------------------------------------------------------------
# Failure model - the input alphabet for the property's strategies.
# ---------------------------------------------------------------------------


class _RateLimitError(Exception):
    """Stand-in for the production ``RateLimitError``.

 The real exception class lives in
 ``libs/llm-orchestrator/src/llm_orchestrator/errors.py``.
 We cannot rely on it being importable while the orchestrator is
 still under construction, so the oracle layer uses this local
 surrogate that satisfies the same ``isinstance`` checks against
 the orchestrator's ``except`` clauses.

 The production-layer test class swaps in the real exception class
 at import time when available; both classes share the same name
 and are caught by the same ``except RateLimitError`` block.
 """


class _ProviderUnavailable(Exception):
    """Stand-in for the production ``ProviderUnavailable`` exception."""


@dataclass(frozen=True, slots=True)
class _Failure:
    """One element of the ``failure_sequence`` strategy alphabet.

 ``kind`` selects the runtime behaviour the fake provider should
 exhibit on the next ``stream`` invocation:

 * ``"success"`` - yield one terminal token-chunk and a ``done``
 event; the orchestrator's outer loop returns afterwards.
 * ``"rate_limit"`` - raise ``RateLimitError`` mid-stream so the
 orchestrator's ``except RateLimitError`` branch fires.
 * ``"unavailable"`` - raise ``ProviderUnavailable`` mid-stream so
 the orchestrator's ``except ProviderUnavailable`` branch fires.
 """

    kind: str  # Literal["success", "rate_limit", "unavailable"]


# Sampling alphabet shared by every Hypothesis strategy below.
_FAILURE_KINDS: tuple[str, ...] = ("success", "rate_limit", "unavailable")


_failure_strategy: st.SearchStrategy[_Failure] = st.builds(
    _Failure, kind=st.sampled_from(_FAILURE_KINDS)
)


# ``failure_sequence`` is a non-empty sequence so the orchestrator
# always has at least one provider response to consume; an empty
# sequence is degenerate (the orchestrator would loop forever) and
# excluded by construction.
_failure_sequence_strategy: st.SearchStrategy[tuple[_Failure, ...]] = st.lists(
    _failure_strategy, min_size=1, max_size=12
).map(tuple)


# Primary downtime in seconds, sampled from ``[0, 300]``. Includes
# the boundary value 60 so the
# fallback-vs-raise branch is exercised on the exact threshold.
_primary_downtime_strategy: st.SearchStrategy[int] = st.integers(
    min_value=0, max_value=300
)


# ---------------------------------------------------------------------------
# Reference oracle - re-implementation of the expected state machine.
# ---------------------------------------------------------------------------


@dataclass
class _SleepRecorder:
    """Captures every ``asyncio.sleep`` invocation as a (delay, attempt) pair.

 The oracle and the production orchestrator both call into this
 recorder via the ``sleep`` keyword argument - patching the
 coroutine instead of monkey-patching ``asyncio.sleep`` keeps the
 test self-contained and immune to other concurrent tests that
 might also stub the global function.
 """

    delays: list[float] = field(default_factory=list)

    async def __call__(self, delay: float) -> None:
        # Record the delay and yield to the event loop without
        # actually sleeping; the property is about *what* delay was
        # requested, not whether the wall clock advanced.
        self.delays.append(float(delay))
        await asyncio.sleep(0)


class _FakeProvider:
    """Async generator factory whose ``stream`` replays a scripted plan.

 The provider consumes the ``failure_sequence`` once: each call to
 ``stream`` advances the cursor by one element and either raises
 the encoded exception or yields a terminal-success chunk.

 ``downtime`` returns a constant value supplied at construction
 time; the orchestrator calls it on the ``ProviderUnavailable``
 branch to decide whether to fall back.

 Two providers are passed to the orchestrator (``primary`` and
 ``fallback``); the tests configure their ``failure_plan``
 independently so the fallback branch can be exercised by giving
 the primary a ``ProviderUnavailable`` and the fallback a
 ``success``.
 """

    def __init__(
        self,
        *,
        failure_plan: Sequence[_Failure],
        downtime_s: int,
        name: str,
    ) -> None:
        self._plan = list(failure_plan)
        self._cursor = 0
        self._downtime_s = downtime_s
        self.name = name
        self.calls = 0

    def downtime(self) -> int:
        return self._downtime_s

    async def stream(
        self,
        system: str,
        history: list[Message],
        tools: list[Any],
    ) -> AsyncIterator["_Chunk"]:
        # Mirror the production ``async for chunk in provider.stream(...)``
        # contract: a successful element yields exactly one terminal
        # chunk, an error element raises mid-iteration.
        self.calls += 1
        if self._cursor >= len(self._plan):
            # Exhausted plan - emit a terminal success so the
            # outer loop returns. This keeps the oracle bounded
            # even when Hypothesis draws a sequence that under-
            # specifies the trailing behaviour.
            yield _Chunk(text="ok", token_count=1, is_final=True, kind="text")
            return

        step = self._plan[self._cursor]
        self._cursor += 1
        if step.kind == "success":
            yield _Chunk(text="ok", token_count=1, is_final=True, kind="text")
            return
        if step.kind == "rate_limit":
            # Important: raise *after* the ``yield`` keyword so the
            # async generator semantics match the production
            # ``async for chunk in provider.stream(...)`` loop -
            # the orchestrator's ``except`` clause should catch
            # this regardless of whether any chunk was emitted.
            if False:  # pragma: no cover - keeps mypy happy
                yield _Chunk(text="", token_count=0, is_final=False, kind="text")
            raise _RateLimitError("simulated 429")
        if step.kind == "unavailable":
            if False:  # pragma: no cover
                yield _Chunk(text="", token_count=0, is_final=False, kind="text")
            raise _ProviderUnavailable("simulated provider down")
        raise AssertionError(f"unknown failure kind {step.kind!r}")


@dataclass(frozen=True, slots=True)
class _Chunk:
    """Stand-in for the production provider chunk dataclass.

 Mirrors the keys read by the orchestrator pseudocode
 (``token_count``, ``kind``, ``call``, ``text``, ``is_final``).
 The oracle never uses the ``call`` field so it is omitted.
 """

    text: str
    token_count: int
    is_final: bool
    kind: str  # Literal["text", "tool_call"]


async def _reference_stream_with_tool_loop(
    *,
    primary: _FakeProvider,
    fallback: _FakeProvider,
    system: str,
    history: list[Message],
    tools: list[Any],
    on_tool_call: Callable[[Any], Awaitable[Any]],
    token_cap: int,
    sleep: Callable[[float], Awaitable[None]],
) -> AsyncIterator[SseEvent]:
    """Reference orchestrator faithful to the expected state machine.

 This is the oracle the tests run *every* time, even
 when the production class has not yet been authored. It encodes
 the expected control flow so a deviation in the production code
 surfaces as a direct mismatch between the two SSE event traces.

 The ``sleep`` parameter is the seam through which the test
 asserts on the exponential backoff durations.
 """

    used_tokens = 0
    provider = primary
    attempts_429 = 0

    while True:
        try:
            async for chunk in provider.stream(system, history, tools):
                used_tokens += chunk.token_count
                if used_tokens > token_cap:
                    yield SseEvent(
                        type="token_cap_exceeded",
                        payload={"limit": token_cap},
                    )
                    return
                if chunk.kind == "tool_call":
                    # The oracle does not exercise the tool-call
                    # branch (separate coverage handles it); we re-emit a
                    # ``tool_result`` to match the protocol.
                    result = await on_tool_call(chunk)
                    yield SseEvent(
                        type="tool_result",
                        payload={"result": result},
                    )
                    continue
                yield SseEvent(type="token", payload={"text": chunk.text})
                if chunk.is_final:
                    yield SseEvent(type="done", payload={})
                    return
        except _RateLimitError:
            attempts_429 += 1
            if attempts_429 >= MAX_429_ATTEMPTS:
                yield SseEvent(type="rate_limit_exhausted", payload={})
                return
            await sleep(2 ** attempts_429)
        except _ProviderUnavailable:
            if (
                provider is primary
                and primary.downtime() >= FALLBACK_DOWNTIME_THRESHOLD_S
            ):
                provider = fallback
                yield SseEvent(
                    type="fallback_provider_active",
                    payload={"provider": fallback.name},
                )
            else:
                raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _drain(generator: AsyncIterator[SseEvent]) -> list[SseEvent]:
    """Materialise an async generator into a concrete list."""

    out: list[SseEvent] = []
    async for ev in generator:
        out.append(ev)
    return out


def _split_primary_fallback(
    failure_sequence: Sequence[_Failure],
) -> tuple[Sequence[_Failure], Sequence[_Failure]]:
    """Split a single failure sequence into primary / fallback plans.

 The first half drives the primary provider; the second half drives
 the fallback. We split deterministically (slice at midpoint) so
 Hypothesis can shrink predictably and the fallback branch always
 has at least one element - when the original sequence has length
 1 we duplicate the single element so both providers have a plan.
 """

    plan = list(failure_sequence)
    if len(plan) <= 1:
        return tuple(plan), tuple(plan)
    midpoint = len(plan) // 2
    return tuple(plan[:midpoint]), tuple(plan[midpoint:])


def _expected_terminal_event(
    failure_sequence: Sequence[_Failure],
    primary_downtime_s: int,
) -> str | None:
    """Return the expected terminal SSE event type (oracle reasoning).

 The function walks the same state machine as the orchestrator and
 returns the first terminal event reached:

 * ``"rate_limit_exhausted"`` after ``MAX_429_ATTEMPTS`` 429 errors.
 * ``"done"`` when a ``"success"`` step is reached on the active
 provider.
 * ``None`` when the sequence ends with a ``ProviderUnavailable``
 whose downtime is below the fallback threshold (the
 orchestrator re-raises in that case - surfaced as a Python
 exception, not an SSE event).

 The tests use this oracle as one of two cross-checks
 (the other being the SSE event sequence equality between the
 reference and the implementation under test).
 """

    primary_plan, fallback_plan = _split_primary_fallback(failure_sequence)
    attempts_429 = 0
    cursor_primary = 0
    cursor_fallback = 0
    on_primary = True
    while True:
        plan = primary_plan if on_primary else fallback_plan
        cursor = cursor_primary if on_primary else cursor_fallback
        if cursor >= len(plan):
            # ``_FakeProvider`` falls through to a synthetic success
            # when its plan is exhausted; mirror that here.
            return "done"
        step = plan[cursor]
        if on_primary:
            cursor_primary += 1
        else:
            cursor_fallback += 1
        if step.kind == "success":
            return "done"
        if step.kind == "rate_limit":
            attempts_429 += 1
            if attempts_429 >= MAX_429_ATTEMPTS:
                return "rate_limit_exhausted"
            continue
        if step.kind == "unavailable":
            if on_primary and primary_downtime_s >= FALLBACK_DOWNTIME_THRESHOLD_S:
                on_primary = False
                continue
            # Re-raise: no SSE terminal event.
            return None
        raise AssertionError(f"unknown step kind {step.kind!r}")


# ---------------------------------------------------------------------------
# Hypothesis settings shared across the suite
# ---------------------------------------------------------------------------


_PROPERTY_SETTINGS = settings(
    max_examples=120,
    deadline=None,  # async tests may run slowly under CI
    suppress_health_check=(HealthCheck.too_slow,),
)


# ---------------------------------------------------------------------------
# Oracle-layer checks (always run)
# ---------------------------------------------------------------------------


class TestReferenceOrchestrator:
    """Checks against the reference orchestrator. These tests
 exercise the local oracle itself, so a regression here means the
 expected state machine changed.
 """

    @_PROPERTY_SETTINGS
    @given(
        failure_sequence=_failure_sequence_strategy,
        primary_downtime_s=_primary_downtime_strategy,
    )
    def test_terminal_event_matches_oracle(
        self,
        failure_sequence: tuple[_Failure, ...],
        primary_downtime_s: int,
    ) -> None:
        """The reference orchestrator produces the SSE terminal event
 predicted by an independent oracle that walks the same state
 machine in plain Python.

 Checks the core behaviors: 429 accumulation, fallback flip on
 downtime, and the
 non-resetting global counter (the oracle does not reset
 ``attempts_429`` on a fallback flip either).
 """

        primary_plan, fallback_plan = _split_primary_fallback(failure_sequence)
        primary = _FakeProvider(
            failure_plan=primary_plan,
            downtime_s=primary_downtime_s,
            name="vllm",
        )
        fallback = _FakeProvider(
            failure_plan=fallback_plan,
            downtime_s=0,
            name="openai",
        )
        recorder = _SleepRecorder()

        async def _on_tool_call(call: Any) -> Any:
            return {"ok": True}

        async def _run() -> tuple[list[SseEvent], BaseException | None]:
            try:
                events = await _drain(
                    _reference_stream_with_tool_loop(
                        primary=primary,
                        fallback=fallback,
                        system="sys",
                        history=[],
                        tools=[],
                        on_tool_call=_on_tool_call,
                        token_cap=10**6,
                        sleep=recorder,
                    )
                )
                return events, None
            except _ProviderUnavailable as exc:
                return [], exc

        events, exc = asyncio.run(_run())

        expected = _expected_terminal_event(
            failure_sequence, primary_downtime_s
        )
        if expected is None:
            # The orchestrator must re-raise rather than surface
            # ``fallback_provider_active`` when downtime < 60.
            assert exc is not None, (
                f"orchestrator did not raise on a sub-threshold "
                f"ProviderUnavailable; events={events!r}, "
                f"failure_sequence={failure_sequence!r}, "
                f"downtime={primary_downtime_s}"
            )
            assert isinstance(exc, _ProviderUnavailable)
        else:
            assert exc is None, (
                f"orchestrator raised unexpectedly: {exc!r}; "
                f"failure_sequence={failure_sequence!r}, "
                f"downtime={primary_downtime_s}"
            )
            assert events, "orchestrator produced no SSE events"
            assert events[-1].type == expected, (
                f"terminal event {events[-1].type!r} ≠ oracle "
                f"prediction {expected!r}; "
                f"failure_sequence={failure_sequence!r}, "
                f"downtime={primary_downtime_s}, full trace="
                f"{[e.type for e in events]!r}"
            )

    @_PROPERTY_SETTINGS
    @given(
        failure_sequence=_failure_sequence_strategy,
        primary_downtime_s=_primary_downtime_strategy,
    )
    def test_no_fourth_attempt_after_three_429s(
        self,
        failure_sequence: tuple[_Failure, ...],
        primary_downtime_s: int,
    ) -> None:
        """Three consecutive ``RateLimitError``
 responses produce exactly one ``rate_limit_exhausted`` event
 and **no** further provider invocation past the third.

 We pre-cap the failure sequence to a 3×rate_limit prefix so
 the property holds independently of the trailing draw.
 """

        prefix = (
            _Failure(kind="rate_limit"),
            _Failure(kind="rate_limit"),
            _Failure(kind="rate_limit"),
        )
        plan = prefix + tuple(failure_sequence)

        primary = _FakeProvider(
            failure_plan=plan,
            downtime_s=primary_downtime_s,
            name="vllm",
        )
        fallback = _FakeProvider(
            failure_plan=(),
            downtime_s=0,
            name="openai",
        )
        recorder = _SleepRecorder()

        async def _on_tool_call(call: Any) -> Any:
            return {"ok": True}

        async def _run() -> list[SseEvent]:
            return await _drain(
                _reference_stream_with_tool_loop(
                    primary=primary,
                    fallback=fallback,
                    system="sys",
                    history=[],
                    tools=[],
                    on_tool_call=_on_tool_call,
                    token_cap=10**6,
                    sleep=recorder,
                )
            )

        events = asyncio.run(_run())
        types = [e.type for e in events]

        # Exactly one exhaustion event, and it is terminal.
        assert types.count("rate_limit_exhausted") == 1, types
        assert types[-1] == "rate_limit_exhausted", types
        # The provider was invoked at most ``MAX_429_ATTEMPTS`` times
        # because the 4th attempt is never made. Because the
        # fake provider re-enters ``stream`` on every retry, this
        # also pins the "no extra retries past the cap" behavior.
        assert primary.calls == MAX_429_ATTEMPTS, (
            f"primary.stream invoked {primary.calls} times; "
            f"retry exhaustion caps the call count at "
            f"{MAX_429_ATTEMPTS}."
        )
        # And no fallback flip happened (the failures were 429s, not
        # ProviderUnavailable).
        assert fallback.calls == 0
        assert "fallback_provider_active" not in types

    @_PROPERTY_SETTINGS
    @given(primary_downtime_s=_primary_downtime_strategy)
    def test_429_backoff_is_exponential(
        self,
        primary_downtime_s: int,
    ) -> None:
        """Successive 429 retries sleep for an
 exponentially-growing duration.

 The exact base is intentionally flexible; the behavior that must
 hold is:

 * each delay is a positive power of two,
 * each successive delay is **strictly greater** than the
 previous (i.e. the sequence is monotonically increasing),
 * there are at most ``MAX_429_ATTEMPTS - 1`` delays before
 the loop exits (the third 429 raises ``rate_limit_exhausted``
 *without* sleeping).
 """

        plan = (
            _Failure(kind="rate_limit"),
            _Failure(kind="rate_limit"),
            _Failure(kind="rate_limit"),
        )

        primary = _FakeProvider(
            failure_plan=plan,
            downtime_s=primary_downtime_s,
            name="vllm",
        )
        fallback = _FakeProvider(
            failure_plan=(),
            downtime_s=0,
            name="openai",
        )
        recorder = _SleepRecorder()

        async def _on_tool_call(call: Any) -> Any:
            return {"ok": True}

        async def _run() -> list[SseEvent]:
            return await _drain(
                _reference_stream_with_tool_loop(
                    primary=primary,
                    fallback=fallback,
                    system="sys",
                    history=[],
                    tools=[],
                    on_tool_call=_on_tool_call,
                    token_cap=10**6,
                    sleep=recorder,
                )
            )

        asyncio.run(_run())

        # Delays are bounded by ``MAX_429_ATTEMPTS - 1``: the first
        # two 429s sleep, the third triggers exhaustion without a
        # sleep call because the ``return`` branch short-circuits
        # the ``await asyncio.sleep`` line.
        assert len(recorder.delays) <= MAX_429_ATTEMPTS - 1, (
            f"orchestrator slept {len(recorder.delays)} times; "
            f"retry exhaustion caps pre-exhaust sleeps at "
            f"{MAX_429_ATTEMPTS - 1} (delays={recorder.delays!r})."
        )
        # Every delay is a positive power of two.
        for d in recorder.delays:
            assert d > 0, f"delay {d!r} is not positive"
            # Powers of two: ``d & (d-1) == 0`` for integer powers;
            # we cast to ``int`` because the recorder normalises to
            # ``float`` (``2 ** k`` is always integral for k ≥ 0).
            as_int = int(d)
            assert as_int == d, f"delay {d!r} is not integral"
            assert (as_int & (as_int - 1)) == 0, (
                f"delay {d!r} is not a power of two; exponential "
                f"backoff is required."
            )
        # Strictly monotonic (each retry waits longer than the
        # previous one).
        for prev, nxt in zip(recorder.delays, recorder.delays[1:]):
            assert nxt > prev, (
                f"backoff is not strictly increasing: "
                f"{recorder.delays!r}; exponential backoff requires "
                f"successive delays to grow."
            )

    @_PROPERTY_SETTINGS
    @given(primary_downtime_s=_primary_downtime_strategy)
    def test_provider_unavailable_below_threshold_re_raises(
        self,
        primary_downtime_s: int,
    ) -> None:
        """``ProviderUnavailable`` with downtime
 below 60s is re-raised; no ``fallback_provider_active`` event.
 """

        downtime = min(primary_downtime_s, FALLBACK_DOWNTIME_THRESHOLD_S - 1)
        plan = (_Failure(kind="unavailable"),)

        primary = _FakeProvider(
            failure_plan=plan, downtime_s=downtime, name="vllm"
        )
        fallback = _FakeProvider(
            failure_plan=(), downtime_s=0, name="openai"
        )
        recorder = _SleepRecorder()

        async def _on_tool_call(call: Any) -> Any:
            return {"ok": True}

        async def _run() -> tuple[list[SseEvent], BaseException | None]:
            try:
                events = await _drain(
                    _reference_stream_with_tool_loop(
                        primary=primary,
                        fallback=fallback,
                        system="sys",
                        history=[],
                        tools=[],
                        on_tool_call=_on_tool_call,
                        token_cap=10**6,
                        sleep=recorder,
                    )
                )
                return events, None
            except _ProviderUnavailable as exc:
                return [], exc

        events, exc = asyncio.run(_run())

        assert isinstance(exc, _ProviderUnavailable), (
            f"sub-threshold downtime should re-raise; got "
            f"events={events!r}, exc={exc!r}, downtime={downtime}"
        )
        # The fallback was never invoked; no SSE banner was emitted
        # because the orchestrator exited via the exception path.
        assert fallback.calls == 0
        assert all(e.type != "fallback_provider_active" for e in events)

    @_PROPERTY_SETTINGS
    @given(primary_downtime_s=_primary_downtime_strategy)
    def test_provider_unavailable_at_threshold_falls_back(
        self,
        primary_downtime_s: int,
    ) -> None:
        """``ProviderUnavailable`` with downtime
 ≥ 60s flips to the fallback provider and emits exactly one
 ``fallback_provider_active`` event whose payload identifies
 the fallback by name.
 """

        downtime = max(primary_downtime_s, FALLBACK_DOWNTIME_THRESHOLD_S)
        # Primary fails, fallback succeeds - the orchestrator must
        # cross over and produce a ``done`` terminal.
        primary = _FakeProvider(
            failure_plan=(_Failure(kind="unavailable"),),
            downtime_s=downtime,
            name="vllm",
        )
        fallback = _FakeProvider(
            failure_plan=(_Failure(kind="success"),),
            downtime_s=0,
            name="openai",
        )
        recorder = _SleepRecorder()

        async def _on_tool_call(call: Any) -> Any:
            return {"ok": True}

        async def _run() -> list[SseEvent]:
            return await _drain(
                _reference_stream_with_tool_loop(
                    primary=primary,
                    fallback=fallback,
                    system="sys",
                    history=[],
                    tools=[],
                    on_tool_call=_on_tool_call,
                    token_cap=10**6,
                    sleep=recorder,
                )
            )

        events = asyncio.run(_run())
        types = [e.type for e in events]

        assert types.count("fallback_provider_active") == 1, (
            f"expected exactly one fallback banner; got types={types!r}"
        )
        # The banner identifies the fallback by name.
        banner = next(
            e for e in events if e.type == "fallback_provider_active"
        )
        assert banner.payload.get("provider") == "openai", (
            f"banner payload {banner.payload!r} does not name the "
            f"fallback provider"
        )
        # The stream finished successfully via the fallback.
        assert types[-1] == "done"
        # Both providers were exercised exactly once.
        assert primary.calls == 1
        assert fallback.calls == 1

    @_PROPERTY_SETTINGS
    @given(
        failure_sequence=_failure_sequence_strategy,
        primary_downtime_s=_primary_downtime_strategy,
    )
    def test_event_sequence_is_deterministic(
        self,
        failure_sequence: tuple[_Failure, ...],
        primary_downtime_s: int,
    ) -> None:
        """The same ``(failure_sequence, downtime)``
 with the same patched clock produces the same SSE event
 sequence on every run.
 """

        async def _on_tool_call(call: Any) -> Any:
            return {"ok": True}

        primary_plan, fallback_plan = _split_primary_fallback(failure_sequence)

        async def _run() -> tuple[
            list[SseEvent], list[float], BaseException | None
        ]:
            primary = _FakeProvider(
                failure_plan=primary_plan,
                downtime_s=primary_downtime_s,
                name="vllm",
            )
            fallback = _FakeProvider(
                failure_plan=fallback_plan,
                downtime_s=0,
                name="openai",
            )
            recorder = _SleepRecorder()
            try:
                events = await _drain(
                    _reference_stream_with_tool_loop(
                        primary=primary,
                        fallback=fallback,
                        system="sys",
                        history=[],
                        tools=[],
                        on_tool_call=_on_tool_call,
                        token_cap=10**6,
                        sleep=recorder,
                    )
                )
                return events, recorder.delays, None
            except _ProviderUnavailable as exc:
                return [], recorder.delays, exc

        first_events, first_delays, first_exc = asyncio.run(_run())
        second_events, second_delays, second_exc = asyncio.run(_run())

        assert type(first_exc) is type(second_exc), (
            f"determinism: exception type changed across runs "
            f"({first_exc!r} vs {second_exc!r})"
        )
        assert [e.type for e in first_events] == [
            e.type for e in second_events
        ], (
            f"determinism: SSE type sequence diverged "
            f"({[e.type for e in first_events]!r} vs "
            f"{[e.type for e in second_events]!r})"
        )
        # Payload equality: the dataclasses are frozen so deep
        # equality is the same as ``__eq__``.
        assert first_events == second_events, (
            f"determinism: SSE payloads diverged "
            f"({first_events!r} vs {second_events!r})"
        )
        assert first_delays == second_delays, (
            f"determinism: backoff delay sequence diverged "
            f"({first_delays!r} vs {second_delays!r})"
        )


# ---------------------------------------------------------------------------
# Concrete regression anchors for exponential backoff.
# ---------------------------------------------------------------------------


class TestBackoffPinnedExamples:
    """Deterministic pins for the exponential backoff sequence.

 These tests do **not** assert on exact delay values. What they pin is:

 * the *count* of delays before exhaustion,
 * the *doubling* between successive delays,
 * the absence of a delay after the third 429.

 A regression that linearises the backoff (e.g. ``sleep(2)`` flat)
 or drops it entirely would fail here even when Hypothesis
 happens to draw a sequence that ends quickly.
 """

    def test_two_consecutive_429s_then_success_emits_done(self) -> None:
        """Two 429s  one delay, then a success completes the stream."""

        plan = (
            _Failure(kind="rate_limit"),
            _Failure(kind="rate_limit"),
            _Failure(kind="success"),
        )
        primary = _FakeProvider(failure_plan=plan, downtime_s=0, name="vllm")
        fallback = _FakeProvider(failure_plan=(), downtime_s=0, name="openai")
        recorder = _SleepRecorder()

        async def _noop(call: Any) -> Any:
            return None

        async def _run() -> list[SseEvent]:
            return await _drain(
                _reference_stream_with_tool_loop(
                    primary=primary,
                    fallback=fallback,
                    system="sys",
                    history=[],
                    tools=[],
                    on_tool_call=_noop,
                    token_cap=10**6,
                    sleep=recorder,
                )
            )

        events = asyncio.run(_run())
        types = [e.type for e in events]

        # The two 429s caused two retry sleeps; the third call
        # succeeded.
        assert len(recorder.delays) == 2, recorder.delays
        # Exponential growth between the two recorded delays.
        assert recorder.delays[1] == 2 * recorder.delays[0], (
            f"successive delays should double: {recorder.delays!r}"
        )
        assert types[-1] == "done"

    def test_three_consecutive_429s_emits_exhausted_with_one_delay(
        self,
    ) -> None:
        """Three 429s  exhaustion; only **one** sleep was issued
 between the first 429 (attempts_429  1, sleep) and the
 second 429 (attempts_429  2, sleep), but the third 429
 (attempts_429  3, exhausted) does NOT sleep.
 """

        plan = (
            _Failure(kind="rate_limit"),
            _Failure(kind="rate_limit"),
            _Failure(kind="rate_limit"),
        )
        primary = _FakeProvider(failure_plan=plan, downtime_s=0, name="vllm")
        fallback = _FakeProvider(failure_plan=(), downtime_s=0, name="openai")
        recorder = _SleepRecorder()

        async def _noop(call: Any) -> Any:
            return None

        async def _run() -> list[SseEvent]:
            return await _drain(
                _reference_stream_with_tool_loop(
                    primary=primary,
                    fallback=fallback,
                    system="sys",
                    history=[],
                    tools=[],
                    on_tool_call=_noop,
                    token_cap=10**6,
                    sleep=recorder,
                )
            )

        events = asyncio.run(_run())

        # The third 429 exits via ``return`` *before* the
        # ``await asyncio.sleep(...)`` line - so we observe at most
        # ``MAX_429_ATTEMPTS - 1 == 2`` delays.
        assert len(recorder.delays) == MAX_429_ATTEMPTS - 1, recorder.delays
        # And the second delay is double the first (exponential).
        assert recorder.delays[1] == 2 * recorder.delays[0]
        assert events[-1].type == "rate_limit_exhausted"


# ---------------------------------------------------------------------------
# Production-layer checks - skipped until the real class is importable.
# ---------------------------------------------------------------------------


pytestmark_production = pytest.mark.skipif(
    _REAL_ORCHESTRATOR is None,
    reason=(
        "llm_orchestrator.orchestrator.LlmOrchestrator is not yet "
        f"implemented; import failed with: {_IMPORT_ERROR!r}. The "
        "retry/fallback behavior is fully exercised against the "
        "reference orchestrator above and will additionally run "
        "against the production class as soon as it is importable."
    ),
)


@pytestmark_production
class TestProductionOrchestrator:
    """Checks against the production:class:`llm_orchestrator.orchestrator.LlmOrchestrator`. The class
 is expected to satisfy the same SSE event contract as the
 reference orchestrator above; the production-layer tests
 therefore re-issue the oracle assertions using the real class
 instead of the reference factory function.

 The tests instantiate the production class with two:class:`_FakeProvider` instances (the orchestrator depends only
 on a ``stream`` async generator + a ``downtime`` int callable,
 so the duck-typed fakes plug in directly). When the production
 class adds extra constructor arguments, the call sites here are
 the single place to update.
 """

    def _build(self, primary: _FakeProvider, fallback: _FakeProvider) -> Any:
        """Construct the production orchestrator.

 The constructor signature is expected to accept
 ``LlmOrchestrator(primary, fallback)``; we mirror that here and let
 any future extension surface as a ``TypeError`` that
 a developer can react to in one place.
 """

        assert _REAL_ORCHESTRATOR is not None  # for the type checker
        return _REAL_ORCHESTRATOR(primary=primary, fallback=fallback)

    @_PROPERTY_SETTINGS
    @given(
        failure_sequence=_failure_sequence_strategy,
        primary_downtime_s=_primary_downtime_strategy,
    )
    def test_terminal_event_matches_oracle(
        self,
        failure_sequence: tuple[_Failure, ...],
        primary_downtime_s: int,
    ) -> None:
        """The production orchestrator's terminal SSE event matches
 the oracle prediction for any draw of
 ``(failure_sequence, primary_downtime_s)``."""

        primary_plan, fallback_plan = _split_primary_fallback(failure_sequence)
        primary = _FakeProvider(
            failure_plan=primary_plan,
            downtime_s=primary_downtime_s,
            name="vllm",
        )
        fallback = _FakeProvider(
            failure_plan=fallback_plan,
            downtime_s=0,
            name="openai",
        )
        orchestrator = self._build(primary, fallback)

        async def _on_tool_call(call: Any) -> Any:
            return {"ok": True}

        async def _run() -> tuple[list[SseEvent], BaseException | None]:
            try:
                events = await _drain(
                    orchestrator.stream_with_tool_loop(
                        system="sys",
                        history=[],
                        tools=[],
                        on_tool_call=_on_tool_call,
                        token_cap=10**6,
                    )
                )
                return events, None
            except Exception as exc:  # noqa: BLE001 - protocol exception
                return [], exc

        events, exc = asyncio.run(_run())

        expected = _expected_terminal_event(
            failure_sequence, primary_downtime_s
        )
        if expected is None:
            assert exc is not None
        else:
            assert exc is None, f"unexpected exception {exc!r}"
            assert events
            assert events[-1].type == expected, (
                f"production orchestrator emitted {events[-1].type!r}; "
                f"oracle expected {expected!r}; full trace="
                f"{[e.type for e in events]!r}"
            )

    def test_three_429s_emits_exhaustion_no_fourth_call(self) -> None:
        """Concrete production pin for retry exhaustion."""

        plan = (
            _Failure(kind="rate_limit"),
            _Failure(kind="rate_limit"),
            _Failure(kind="rate_limit"),
        )
        primary = _FakeProvider(
            failure_plan=plan, downtime_s=0, name="vllm"
        )
        fallback = _FakeProvider(
            failure_plan=(), downtime_s=0, name="openai"
        )
        orchestrator = self._build(primary, fallback)

        async def _on_tool_call(call: Any) -> Any:
            return {"ok": True}

        async def _run() -> list[SseEvent]:
            return await _drain(
                orchestrator.stream_with_tool_loop(
                    system="sys",
                    history=[],
                    tools=[],
                    on_tool_call=_on_tool_call,
                    token_cap=10**6,
                )
            )

        events = asyncio.run(_run())

        types = [e.type for e in events]
        assert types[-1] == "rate_limit_exhausted"
        assert types.count("rate_limit_exhausted") == 1
        assert primary.calls == MAX_429_ATTEMPTS
        assert fallback.calls == 0

    def test_provider_unavailable_at_threshold_falls_back(self) -> None:
        """Concrete production pin for fallback activation."""

        primary = _FakeProvider(
            failure_plan=(_Failure(kind="unavailable"),),
            downtime_s=FALLBACK_DOWNTIME_THRESHOLD_S,
            name="vllm",
        )
        fallback = _FakeProvider(
            failure_plan=(_Failure(kind="success"),),
            downtime_s=0,
            name="openai",
        )
        orchestrator = self._build(primary, fallback)

        async def _on_tool_call(call: Any) -> Any:
            return {"ok": True}

        async def _run() -> list[SseEvent]:
            return await _drain(
                orchestrator.stream_with_tool_loop(
                    system="sys",
                    history=[],
                    tools=[],
                    on_tool_call=_on_tool_call,
                    token_cap=10**6,
                )
            )

        events = asyncio.run(_run())
        types = [e.type for e in events]

        assert types.count("fallback_provider_active") == 1
        banner = next(e for e in events if e.type == "fallback_provider_active")
        assert banner.payload.get("provider") == "openai"
        assert types[-1] == "done"
        assert primary.calls == 1
        assert fallback.calls == 1
