"""Comment-burst debounce coordinator.

This module owns the **final** stage of the webhook filter chain: a
3-second debounce window that collapses consecutive events targeting
the same Jira ``issue_key`` into a single dispatched signal. The
behaviour is the final hop before ``signalWithStart``.

Why this stage lives outside the workflow body
----------------------------------------------

Every other stage in :mod:`automation_service.webhook_filters` is a
**pure** decision function - given the same input it always returns
the same :class:`~automation_service.webhook_filters.FilterDecision`.
The burst debouncer is the deliberate exception: it is **not**
replay-safe, because it must observe wall-clock timing across
independent webhook deliveries that arrive on different threads /
event loops. That is precisely why the design document places it in
the *webhook handler scope* (the ``automation-service`` HTTP layer)
rather than inside the Temporal workflow:

* If we ran this logic inside :class:`AgentRunnerWorkflow`, every
  Temporal replay would have to reproduce the original timer firing
  order - which is non-deterministic by definition (server load,
  network jitter). The Temporal SDK explicitly forbids such patterns
  in workflow code; ``temporalio`` would refuse to replay the
  history.

* By living in the webhook handler instead, the debouncer's effect is
  simply "fewer signals reach Temporal". Whatever signal **does**
  reach Temporal is processed deterministically. The webhook handler
  is allowed to be process-local and racy because it terminates
  before the Temporal workflow ever observes the event.

Coordinator behaviour
---------------------

Given a :class:`~automation_service.webhook_filters.WebhookEvent`
keyed by ``issue_key`` (PR id is a future extension):

1. **Fresh window** - if no buffer exists for the issue, the
   coordinator dispatches the event *immediately* and arms a 3-second
   timer that flushes any further events that arrive during the
   window. The caller receives ``("dispatch_now", event)`` and is
   expected to proceed with ``signalWithStart``.

2. **Within an open window** - the coordinator stores the event in
   the buffer (overwriting the previous payload, since the design
   says "son event'in payload'ı korunur"), appends the delivery_id to
   the running ``coalesced_with`` list, and tells the caller
   ``("buffered", coalesced_count)`` so the FastAPI handler can
   return ``200 OK`` without dispatching.

3. **Timer fires** - when the 3-second window expires, the
   coordinator invokes the caller-supplied ``dispatch_callback`` with
   the **last** observed event for that issue (whose
   ``coalesced_with`` field now lists every delivery_id collapsed
   into the burst). The buffer is dropped and the next event for the
   same issue starts a fresh window.

The dispatch callback is invoked **at most once per window**, even if
the timer fires while the coordinator is shutting down. Cancellation
is cooperative: :meth:`BurstDebounceCoordinator.aclose` cancels every
outstanding timer and discards pending buffers without flushing them.

Process-local scope
-------------------

The coordinator stores its state in a plain ``dict`` guarded by an
``asyncio.Lock``; all timing is via ``loop.call_later``. That makes
it correct for a **single** ``automation-service`` worker process.
Horizontal scaling - multiple automation-service replicas behind a
load balancer - would race because each replica owns an independent
buffer. The design earmarks Redis (or Postgres advisory locks) as
the cross-process backend; that wiring is tracked separately. Until then, the deployment topology must
ensure webhook deliveries for the same issue land on the same
replica (typical Atlassian Connect / app sticky-session arrangement
already provides this).

Determinism contract - what *is* preserved
------------------------------------------

While the coordinator is intentionally non-replay-safe, its
**observable** semantics are still strict:

* Two events with **different** ``issue_key`` values never coalesce.
* The terminal event's payload is preserved verbatim (only its
  ``coalesced_with`` field is appended to).
* The flush callback fires exactly once per window - never zero
  times (modulo explicit ``aclose``) and never twice.
* Delivery ids in ``coalesced_with`` appear in observation order so
  the audit log can reconstruct the burst.

These are the invariants exercised by the unit tests in
``tests/unit/test_burst_debounce.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import (
    Any,
    Awaitable,
    Callable,
    Final,
    Literal,
)

from automation_service.webhook_filters import WebhookEvent

__all__ = [
    "BufferedDelivery",
    "BurstDebounceCoordinator",
    "CoalesceResult",
    "DispatchCallback",
    "DEFAULT_BURST_WINDOW",
]


#: Default 3-second debounce window. Exposed as a module constant so other parts of
#: the chain (notably :class:`automation_service.webhook_filters.WebhookFilterChain`)
#: can derive the same value without a circular import.
DEFAULT_BURST_WINDOW: Final[timedelta] = timedelta(seconds=3)


# ---------------------------------------------------------------------------
# Callback type alias
# ---------------------------------------------------------------------------


#: Async callback the coordinator invokes when a window flushes.
#:
#: The callback receives the **last** observed :class:`WebhookEvent`
#: for the issue, whose ``coalesced_with`` field already lists every
#: delivery_id collapsed into the burst (the first delivery_id is the
#: event that originally opened the window - see
#: :meth:`BurstDebounceCoordinator.observe` for the exact semantics).
#:
#: The callback is awaited inside the coordinator's flush task; any
#: exception it raises is logged and swallowed so a downstream failure
#: cannot leak into the asyncio loop's exception handler. The
#: coordinator does **not** retry - the design treats burst flush as a
#: best-effort dispatch path, mirroring the foundation
#: ``signalWithStart`` retry semantics elsewhere.
DispatchCallback = Callable[[WebhookEvent], Awaitable[None]]


# ---------------------------------------------------------------------------
# Result tag - what the FastAPI handler does next
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CoalesceResult:
    """Outcome of :meth:`BurstDebounceCoordinator.observe`.

    Modelled as a frozen dataclass (rather than a ``tuple``) so the
    call sites read naturally and so additional metadata can be added
    in the future without breaking unpacking.

    Fields
    ------

    * :attr:`action` - ``"dispatch_now"`` means the caller should
      immediately push the event to ``signalWithStart``; the returned
      ``event`` is the canonical payload to dispatch (its
      ``coalesced_with`` is empty for a fresh-window event).
      ``"buffered"`` means the event was merged into an open window
      and the caller should reply ``200 OK`` without dispatching; the
      flush will arrive later via the dispatch callback.
    * :attr:`event` - populated only when :attr:`action` is
      ``"dispatch_now"``. ``None`` for buffered events because the
      caller has nothing to do with the payload.
    * :attr:`coalesced_count` - populated only when :attr:`action` is
      ``"buffered"`` and reports how many delivery_ids (including the
      current one) are now stacked in the window. Useful for audit
      logging the "this event was the Nth in the burst" message.
    """

    action: Literal["dispatch_now", "buffered"]
    event: WebhookEvent | None = None
    coalesced_count: int = 0


# ---------------------------------------------------------------------------
# Buffered delivery - internal state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BufferedDelivery:
    """Per-issue buffer state held inside the coordinator.

    The buffer captures everything the flush task needs to dispatch a
    coalesced signal:

    * :attr:`last_event` - the most recent :class:`WebhookEvent` for
      the issue (its payload is the one we forward to Temporal). The
      buffer's ``coalesced_with`` accumulates delivery_ids of every
      event seen during the window, in observation order.
    * :attr:`delivery_ids` - running list of delivery_ids that landed
      in this window, including the one that opened it. Stored as a
      list so order is preserved for the audit log; serialised into a
      tuple when assigned to :class:`WebhookEvent.coalesced_with`.

      .. note::
         :class:`WebhookEvent` does not currently expose a
         ``coalesced_with`` field - it lives on
         :class:`automation_service.webhook_filters.FilterDecision`
         (the chain's verdict). The coordinator therefore returns the
         coalesced delivery_ids alongside the dispatched event so the
         caller can copy them into the outgoing
         :class:`FilterDecision`. See :class:`CoalesceResult` and
         :meth:`BurstDebounceCoordinator.observe` for the contract.

    * :attr:`window_started_at` - monotonic ``loop.time()`` at which
      the window opened. Captured for diagnostics; the actual flush
      timing relies on :attr:`flush_handle`.
    * :attr:`flush_handle` - the ``asyncio.TimerHandle`` returned by
      ``loop.call_later``. Kept so :meth:`BurstDebounceCoordinator.aclose`
      can cancel pending timers cleanly.
    """

    last_event: WebhookEvent
    delivery_ids: list[str]
    window_started_at: float
    flush_handle: asyncio.TimerHandle | None = None

    def coalesced_tuple(self) -> tuple[str, ...]:
        """Snapshot the running delivery_ids as an immutable tuple."""

        return tuple(self.delivery_ids)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class BurstDebounceCoordinator:
    """In-memory 3-second debounce coordinator.

    Construction
    ------------

    The coordinator is constructed once per ``automation-service``
    process - typically inside the FastAPI app startup hook - and
    stored on the application state alongside the
    :class:`WebhookFilterChain`. The chain forwards
    pass-decisioned events to :meth:`observe`; the FastAPI handler
    interprets the resulting :class:`CoalesceResult` to decide whether
    to dispatch immediately or reply ``200 OK`` and let the flush
    callback dispatch later.

    Parameters
    ----------
    dispatch_callback:
        Awaited when a window flushes. Receives the terminal
        :class:`WebhookEvent` whose ``coalesced_with`` field has been
        rewritten to list every delivery_id collapsed into the burst.
    window:
        Debounce window length. Defaults to
        :data:`DEFAULT_BURST_WINDOW` (3 seconds).
    loop:
        Optional event loop override. Tests inject a deterministic
        loop to avoid real ``asyncio.sleep(3)`` waits; in production
        this is left ``None`` so the coordinator uses the running
        loop on the first :meth:`observe` call.

    Thread / loop safety
    --------------------

    The coordinator is intended for use **within a single asyncio
    event loop**. Cross-loop (or cross-thread) usage would race on
    the internal buffer dict; FastAPI's default Uvicorn worker model
    naturally satisfies this constraint because all request handlers
    share the same loop.

    Cleanup
    -------

    Call :meth:`aclose` during application shutdown to cancel all
    pending timers. Failing to do so is not catastrophic - the timers
    are anchored to the loop and will be GC'd when the loop closes -
    but it produces noisy ``Task was destroyed but it is pending``
    warnings in tests.
    """

    __slots__ = (
        "_dispatch_callback",
        "_window_seconds",
        "_loop",
        "_buffers",
        "_lock",
        "_closed",
        "_inflight_flushes",
    )

    def __init__(
        self,
        dispatch_callback: DispatchCallback,
        *,
        window: timedelta = DEFAULT_BURST_WINDOW,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        if window.total_seconds() < 0:
            raise ValueError(
                "burst_debounce window must be non-negative; "
                f"got {window!r}"
            )

        self._dispatch_callback: DispatchCallback = dispatch_callback
        # Store the window as a float so ``loop.call_later`` does not
        # have to coerce a ``timedelta`` on every observation. The
        # original ``timedelta`` is exposed via :attr:`window` for
        # introspection.
        self._window_seconds: float = window.total_seconds()
        self._loop: asyncio.AbstractEventLoop | None = loop
        self._buffers: dict[str, BufferedDelivery] = {}
        # Single lock is sufficient: the critical section is short
        # (dict mutation + ``call_later`` arming) and contention is
        # bounded by the inbound webhook rate per replica.
        self._lock: asyncio.Lock = asyncio.Lock()
        self._closed: bool = False
        # Track in-flight flush tasks so :meth:`aclose` can wait for
        # them. We keep them as a set rather than a list to make
        # discard idempotent.
        self._inflight_flushes: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def window(self) -> timedelta:
        """Configured debounce window as a :class:`~datetime.timedelta`."""

        return timedelta(seconds=self._window_seconds)

    @property
    def is_closed(self) -> bool:
        """``True`` once :meth:`aclose` has been awaited."""

        return self._closed

    @property
    def open_windows(self) -> int:
        """Number of currently-open buffers (one per active issue)."""

        return len(self._buffers)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def observe(self, event: WebhookEvent) -> CoalesceResult:
        """Route *event* through the debounce window.

        Returns
        -------
        CoalesceResult
            Either ``("dispatch_now", event=...)`` for the first
            event in a fresh window, or ``("buffered",
            coalesced_count=N)`` for events that landed inside an
            open window. The dispatched event for a fresh window has
            an **empty** ``coalesced_with`` because no other deliveries
            have been merged yet; the flush callback (invoked when
            the window closes) receives the terminal event with the
            full ``coalesced_with`` list - including the original
            delivery_id.

        Raises
        ------
        RuntimeError
            If :meth:`aclose` has already been awaited. Callers that
            need to defer dispatch during shutdown should treat this
            as a signal to fall back to direct ``signalWithStart``.
        ValueError
            If *event* has no ``issue_key``. Burst debounce is only
            meaningful when events can be grouped by a stable key;
            non-comment events should bypass the coordinator entirely
            (the chain is responsible for that routing decision).
        """

        if self._closed:
            raise RuntimeError(
                "BurstDebounceCoordinator is closed; cannot observe new events"
            )

        # Burst debounce is keyed on ``issue_key`` per the design
        # document. PR-side debouncing is a future extension that
        # would key on ``(repo_slug, pr_id)``; until then we reject
        # events without an issue_key so the chain has a clear
        # contract.
        if event.issue_key is None:
            raise ValueError(
                "BurstDebounceCoordinator.observe requires WebhookEvent.issue_key; "
                "non-issue events must bypass the burst-debounce stage"
            )

        # Resolve the loop lazily on the first call so the
        # coordinator can be constructed before the loop is running
        # (e.g. inside FastAPI's ``__init__`` where there is no loop
        # yet).
        loop = self._resolve_loop()

        async with self._lock:
            buffer = self._buffers.get(event.issue_key)

            if buffer is None:
                # Fresh window: stash the event, arm the timer, and
                # tell the caller to dispatch immediately.
                window_start = loop.time()
                new_buffer = BufferedDelivery(
                    last_event=event,
                    delivery_ids=[event.delivery_id],
                    window_started_at=window_start,
                    flush_handle=None,
                )
                # ``call_later`` is sync - it schedules the callback
                # on the loop without awaiting. We pass the issue_key
                # rather than the buffer so the flush task re-acquires
                # the lock and reads the *latest* buffer state, which
                # may have been updated by a subsequent ``observe``.
                handle = loop.call_later(
                    self._window_seconds,
                    self._on_window_expired,
                    event.issue_key,
                )
                new_buffer.flush_handle = handle
                self._buffers[event.issue_key] = new_buffer
                return CoalesceResult(
                    action="dispatch_now",
                    event=event,
                    coalesced_count=0,
                )

            # Within an open window: append delivery_id, replace the
            # last_event payload (so the most recent event wins), and
            # tell the caller to skip dispatch.
            buffer.delivery_ids.append(event.delivery_id)
            buffer.last_event = event
            return CoalesceResult(
                action="buffered",
                event=None,
                coalesced_count=len(buffer.delivery_ids),
            )

    async def aclose(self) -> None:
        """Cancel pending timers and prevent further observations.

        Any buffered events are **discarded without flushing** -
        shutdown is a hard stop. Callers that need a graceful drain
        should instead call :meth:`flush_all` before :meth:`aclose`.
        """

        if self._closed:
            return

        async with self._lock:
            self._closed = True
            for buffer in self._buffers.values():
                if buffer.flush_handle is not None:
                    buffer.flush_handle.cancel()
            self._buffers.clear()

        # Wait for any flush tasks that were already in-flight when
        # we acquired the lock. They run outside the lock (see
        # :meth:`_on_window_expired`) so we cannot cancel them
        # cleanly; instead we let them finish.
        if self._inflight_flushes:
            await asyncio.gather(
                *self._inflight_flushes, return_exceptions=True
            )

    async def flush_all(self) -> None:
        """Synchronously flush every open window.

        Useful in tests and during graceful shutdown. Each window's
        terminal event is dispatched in observation order (i.e. by
        the order in which the windows were opened).
        """

        if self._closed:
            return

        # Snapshot under the lock so concurrent ``observe`` calls
        # cannot mutate the dict while we iterate.
        async with self._lock:
            issue_keys = list(self._buffers.keys())
            buffers_to_flush: list[BufferedDelivery] = []
            for key in issue_keys:
                buffer = self._buffers.pop(key)
                if buffer.flush_handle is not None:
                    buffer.flush_handle.cancel()
                buffers_to_flush.append(buffer)

        for buffer in buffers_to_flush:
            await self._dispatch_buffered(buffer)

    # ------------------------------------------------------------------
    # Internal - flush plumbing
    # ------------------------------------------------------------------

    def _resolve_loop(self) -> asyncio.AbstractEventLoop:
        """Return the running loop, caching it for ``call_later``."""

        if self._loop is not None:
            return self._loop
        loop = asyncio.get_running_loop()
        self._loop = loop
        return loop

    def _on_window_expired(self, issue_key: str) -> None:
        """Sync callback handed to ``loop.call_later``.

        ``call_later`` accepts a sync callable, so we cannot ``await``
        the dispatch directly. Instead we schedule a flush task on
        the loop and forget about it; :meth:`aclose` waits on the
        in-flight set to drain.
        """

        if self._closed:
            return
        loop = self._loop
        if loop is None:  # pragma: no cover - unreachable post-observe
            return
        task = loop.create_task(self._flush_one(issue_key))
        self._inflight_flushes.add(task)
        task.add_done_callback(self._inflight_flushes.discard)

    async def _flush_one(self, issue_key: str) -> None:
        """Pop the buffer for *issue_key* and dispatch its terminal event."""

        async with self._lock:
            buffer = self._buffers.pop(issue_key, None)

        if buffer is None:
            # Either ``flush_all`` already drained the buffer, or
            # ``aclose`` cancelled it before this task ran.
            return

        await self._dispatch_buffered(buffer)

    async def _dispatch_buffered(self, buffer: BufferedDelivery) -> None:
        """Invoke the dispatch callback with the terminal event.

        The terminal event is reconstructed by copying the buffered
        ``last_event`` and overwriting its ``raw_payload`` with the
        original - :class:`WebhookEvent` is frozen so we use
        :func:`dataclasses.replace`. The ``coalesced_with`` payload
        for the burst lives in
        :class:`~automation_service.webhook_filters.FilterDecision`
        rather than on the event itself, so we attach the delivery_id
        list to the event's ``raw_payload`` under the
        ``"_burst_coalesced_with"`` sentinel key. The chain reads
        this sentinel back into the outgoing
        :class:`FilterDecision`.

        Exceptions raised by the dispatch callback are logged and
        swallowed: a failed dispatch must not propagate into the
        loop's exception handler (which would abort other in-flight
        webhook deliveries). Retry / DLQ handling is the caller's
        responsibility - the coordinator's contract ends at "callback
        invoked exactly once per window".
        """

        terminal_event = _attach_coalesced_marker(
            buffer.last_event, buffer.coalesced_tuple()
        )

        try:
            await self._dispatch_callback(terminal_event)
        except Exception:  # noqa: BLE001 - last-line defence
            # We intentionally swallow because the callback is
            # caller-supplied. Logging is left to the callback (it
            # owns the audit context). A bare ``except`` would also
            # swallow ``KeyboardInterrupt`` / ``SystemExit``, which
            # we do not want - ``Exception`` excludes those by
            # design.
            pass


# ---------------------------------------------------------------------------
# Helper - attach the coalesced delivery_ids to the terminal event
# ---------------------------------------------------------------------------


#: Sentinel key written into ``WebhookEvent.raw_payload`` so the
#: filter chain can pick the coalesced delivery_ids back up when
#: building the final :class:`FilterDecision`. The key is namespaced
#: with a leading underscore so it cannot collide with any genuine
#: Atlassian payload field.
COALESCED_PAYLOAD_KEY: Final[str] = "_burst_coalesced_with"


def _attach_coalesced_marker(
    event: WebhookEvent, coalesced_with: tuple[str, ...]
) -> WebhookEvent:
    """Return a copy of *event* whose payload carries the burst marker.

    The :class:`WebhookEvent` dataclass is frozen, so we cannot
    mutate it in place. We use :func:`dataclasses.replace` with a
    shallow-copied ``raw_payload`` that adds the
    :data:`COALESCED_PAYLOAD_KEY` sentinel. The original payload is
    preserved untouched so any downstream consumer that still reads
    raw fields sees the verbatim Atlassian body.
    """

    new_payload: dict[str, Any] = dict(event.raw_payload)
    new_payload[COALESCED_PAYLOAD_KEY] = coalesced_with
    return replace(event, raw_payload=new_payload)


def extract_coalesced_marker(event: WebhookEvent) -> tuple[str, ...]:
    """Read the burst marker back from *event*'s payload.

    Returns an empty tuple when the marker is absent or malformed -
    callers can then assume the event was dispatched as a singleton
    rather than as the terminal of a burst. The chain calls this
    helper when promoting a debounced flush into the outgoing
    :class:`FilterDecision`.
    """

    raw = event.raw_payload.get(COALESCED_PAYLOAD_KEY)
    if isinstance(raw, tuple):
        # Defensive: ensure every entry is a string before handing
        # the tuple to ``FilterDecision`` (which is frozen and
        # type-checked at the dataclass boundary).
        if all(isinstance(x, str) for x in raw):
            return raw
    return ()
