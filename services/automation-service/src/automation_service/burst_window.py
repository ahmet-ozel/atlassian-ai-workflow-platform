"""Comment-burst debounce window — task 4.4 of ``platform-mimari-workflows``.

This module provides the ``BurstWindow`` class — the final stage of
the webhook filter chain mandated by Requirement 4.7 and the design
document's "Webhook Filter Chain" section. The window collapses
consecutive ``WebhookEvent`` deliveries that target the same Jira
``issue_key`` within a 3-second wall-clock window into a single
dispatched signal; subsequent deliveries inside the open window are
acknowledged with ``coalesce_dropped`` and remembered so the audit
log can reconstruct which deliveries were merged.

NOT replay-safe — webhook handler scope only
--------------------------------------------

This stage is intentionally **NOT replay-safe**. Every other stage in
:mod:`automation_service.webhook_filters` is a pure decision function
(given the same input it always produces the same
:class:`~automation_service.webhook_filters.FilterDecision`); the
burst debouncer is the deliberate exception because it must observe
wall-clock timing across independent webhook deliveries that arrive
out of order on different threads / event loops. The design document
therefore confines this stage to the **webhook HTTP handler scope**
(the ``automation-service`` FastAPI layer) — it must **never** be
invoked from inside a Temporal workflow body:

* If we ran this logic inside :class:`AgentRunnerWorkflow`, every
  Temporal replay would have to reproduce the original timer firing
  order — which is non-deterministic by definition. The Temporal SDK
  forbids such patterns in workflow code; ``temporalio`` would refuse
  to replay the resulting history.
* By living in the webhook handler, the debouncer's effect is simply
  "fewer signals reach Temporal". Whatever signal does reach Temporal
  is processed deterministically. The webhook handler is allowed to
  be process-local and racy because it terminates before the workflow
  ever observes the event.

Wall-clock source
-----------------

The window timer uses ``time.monotonic()`` (passed in by the caller
as the ``now`` argument), which is immune to clock skew, NTP
adjustments, daylight-saving transitions, and operator-driven date
changes. The caller supplies ``now`` explicitly so unit tests can
inject a deterministic clock; production callers pass
``time.monotonic()`` directly.

Public API
----------

* :data:`BURST_WINDOW_SECONDS` — the canonical 3.0-second window
  threshold. Exposed as a module constant so other parts of the
  chain can derive the same value without a circular import.
* :class:`CoalesceDecision` (type alias) — ``Literal[
  "coalesce_dropped", "coalesce_emit"]``. ``"coalesce_emit"`` means
  the caller should immediately dispatch the event (its delivery_id
  has opened a fresh window). ``"coalesce_dropped"`` means the event
  was merged into an open window and the caller must reply ``200 OK``
  with the running ``coalesced_with`` list attached.
* :class:`BurstWindow` — the in-memory coordinator. Construction is
  parameterless; the caller injects ``now`` on every
  :meth:`register` invocation so timing is testable.

Single-process scope
--------------------

The window stores its state in a plain ``dict`` and is correct for a
**single** ``automation-service`` worker process. Horizontal scaling
— multiple replicas behind a load balancer — would race because each
replica owns an independent buffer. The design earmarks Postgres
advisory locks (or Redis) as the cross-process backend; that wiring
is out of scope for task 4.4 and is tracked separately. Until then,
the deployment topology must ensure webhook deliveries for the same
issue land on the same replica (typical Atlassian Connect / app
sticky-session arrangement already provides this).

Determinism contract — what *is* preserved
------------------------------------------

While the window is intentionally non-replay-safe, its **observable**
semantics are still strict:

* Two events with **different** ``issue_key`` values never coalesce
  (independent windows).
* The latest event's payload is preserved verbatim (each
  :meth:`register` call replaces the buffered payload — design
  mandate "son event'in payload'ı korunur").
* The ``coalesced_with`` list returned by :meth:`flush_window`
  contains the dropped delivery_ids in observation order, **not**
  including the anchor delivery (the one that opened the window —
  that delivery was dispatched immediately as ``coalesce_emit`` and
  carries its own delivery_id).
* :meth:`flush_window` returns ``None`` for issues that have no open
  window — callers can rely on that to detect "nothing to flush".

These invariants are exercised by the property tests in
``tests/property/test_burst_debounce.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Literal, Mapping

__all__ = [
    "BURST_WINDOW_SECONDS",
    "CoalesceDecision",
    "BurstWindow",
]


# ---------------------------------------------------------------------------
# Window threshold
# ---------------------------------------------------------------------------

#: Comment-burst debounce window in seconds — Requirement 4.7 mandates
#: 3 seconds. Stored as a float so :meth:`BurstWindow.register` can
#: compare against ``time.monotonic()`` deltas directly without a
#: ``timedelta`` round trip.
BURST_WINDOW_SECONDS: Final[float] = 3.0


# ---------------------------------------------------------------------------
# CoalesceDecision — the verdict returned by ``register``
# ---------------------------------------------------------------------------

#: Outcome of :meth:`BurstWindow.register`.
#:
#: * ``"coalesce_emit"`` — the delivery has opened a fresh window;
#:   the caller dispatches the event to ``signalWithStart``
#:   immediately. The window remains open for the configured
#:   :data:`BURST_WINDOW_SECONDS` so subsequent deliveries for the
#:   same issue can be merged.
#: * ``"coalesce_dropped"`` — the delivery landed inside an open
#:   window; the caller does **not** dispatch and the
#:   :class:`~automation_service.webhook_filters.WebhookFilterChain`
#:   wraps the verdict as ``FilterDecision(action="drop",
#:   reason="burst_coalesced", coalesced_with=...)``. The buffered
#:   payload is updated to the latest event so when the window
#:   eventually closes the dispatched signal carries the most recent
#:   information.
CoalesceDecision = Literal["coalesce_dropped", "coalesce_emit"]


# ---------------------------------------------------------------------------
# Internal buffer
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _WindowBuffer:
    """Per-issue buffer state held inside :class:`BurstWindow`.

    The buffer captures everything the caller needs to reconstruct
    the dispatched signal when the window flushes:

    * :attr:`window_start` — monotonic ``time.monotonic()`` value at
      which the window opened. ``register`` compares incoming ``now``
      values against this so the window stays anchored to the
      **first** delivery rather than sliding with every incoming
      event.
    * :attr:`anchor_delivery_id` — delivery_id of the event that
      opened the window (returned ``"coalesce_emit"``). Stored so the
      audit log can correlate the dispatched signal with the burst,
      but **not** included in :attr:`dropped_delivery_ids` because it
      was dispatched, not dropped.
    * :attr:`dropped_delivery_ids` — running list of delivery_ids
      that landed inside the open window and were therefore dropped.
      Order is preserved (observation order) so the audit log can
      reconstruct the sequence of duplicate deliveries.
    * :attr:`latest_payload` — the most recent event's payload. The
      design mandates "son event'in payload'ı korunur"; we preserve
      it as a shallow ``dict`` copy so the caller cannot accidentally
      mutate the buffered state.
    """

    window_start: float
    anchor_delivery_id: str
    dropped_delivery_ids: list[str] = field(default_factory=list)
    latest_payload: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# BurstWindow — coordinator
# ---------------------------------------------------------------------------


class BurstWindow:
    """In-memory 3-second debounce coordinator (Requirement 4.7).

    The coordinator is deliberately simple: it owns a plain ``dict``
    keyed by ``issue_key`` and exposes two synchronous methods,
    :meth:`register` and :meth:`flush_window`. There is no background
    timer — the caller is responsible for invoking
    :meth:`flush_window` when it determines the window has elapsed
    (typically from a separate sweeper task or after handling a
    request that observed the elapsed timestamp).

    Why no built-in timer?
    ----------------------

    A built-in ``loop.call_later`` timer would tie the window's
    lifecycle to a specific asyncio event loop. The chain may run
    inside FastAPI (async) **or** inside a synchronous test harness
    (the property tests in this spec); both call sites need to
    observe the same window semantics. By exposing :meth:`register`
    and :meth:`flush_window` as pure synchronous methods that take
    ``now`` explicitly, the window stays trivial to test with
    Hypothesis and stays portable across runtime contexts.

    Thread safety
    -------------

    The coordinator is **not** thread-safe — concurrent
    :meth:`register` calls on the same issue_key would race on the
    internal buffer dict. FastAPI's default Uvicorn worker model
    naturally serialises request handlers on a single asyncio loop,
    so this constraint is satisfied in production. Tests that
    parallelise across loops would need their own synchronisation.

    Lifetime
    --------

    The coordinator is constructed once per ``automation-service``
    process — typically during FastAPI app startup — and stored on
    application state alongside the
    :class:`~automation_service.webhook_filters.WebhookFilterChain`.
    No explicit cleanup is required: the buffers are pure-Python
    dicts and are GC'd when the process exits.
    """

    __slots__ = ("_window_seconds", "_buffers")

    def __init__(self, *, window_seconds: float = BURST_WINDOW_SECONDS) -> None:
        """Construct a fresh coordinator.

        Parameters
        ----------
        window_seconds:
            Window threshold in seconds. Defaults to
            :data:`BURST_WINDOW_SECONDS` (3.0). Tests may override
            with smaller values to keep test runtimes short, but
            production callers should leave this at the default so
            the behaviour matches Requirement 4.7.
        """

        if window_seconds < 0:
            # Defensive: a negative window would silently disable the
            # debounce stage. Reject at construction time so
            # misconfiguration surfaces immediately.
            raise ValueError(
                "window_seconds must be non-negative; "
                f"got {window_seconds!r}"
            )

        self._window_seconds: float = window_seconds
        self._buffers: dict[str, _WindowBuffer] = {}

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def window_seconds(self) -> float:
        """Configured window threshold in seconds."""

        return self._window_seconds

    @property
    def open_windows(self) -> int:
        """Number of currently-open buffers (one per active issue)."""

        return len(self._buffers)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(
        self,
        *,
        issue_key: str,
        delivery_id: str,
        payload: Mapping[str, Any],
        now: float,
    ) -> CoalesceDecision:
        """Route a delivery through the debounce window.

        The decision rules are:

        * No open window for ``issue_key`` → open one anchored at
          ``now``, store ``payload``, and return
          ``"coalesce_emit"``. The caller dispatches immediately.
        * Open window exists AND ``now - window_start <
          window_seconds`` → drop the delivery, append
          ``delivery_id`` to the buffer's ``dropped_delivery_ids``,
          replace the buffered payload with ``payload``, and return
          ``"coalesce_dropped"``. The caller does **not** dispatch.
        * Open window exists BUT it has already expired (``now -
          window_start >= window_seconds``) → close the stale window
          (its contents are discarded — the caller is expected to
          have flushed it via :meth:`flush_window` before this point;
          if not, the design accepts the trade-off that an unflushed
          stale window is dropped because its payload is older than
          the new event), open a fresh window with this delivery as
          the anchor, and return ``"coalesce_emit"``.

        Parameters
        ----------
        issue_key:
            The Jira issue key the event targets. Different issue_keys
            never coalesce (independent windows).
        delivery_id:
            The platform's idempotency key for this specific
            webhook delivery. The :meth:`flush_window` return value
            includes every dropped ``delivery_id`` in observation
            order so audit logs can reconstruct the burst.
        payload:
            The event's payload — this is what the caller would have
            forwarded to ``signalWithStart``. Stored as a shallow
            ``dict`` copy so subsequent caller mutations cannot
            corrupt the buffered state. The latest payload wins on
            every ``register`` call (design mandate).
        now:
            Wall-clock value from ``time.monotonic()`` (or a test
            shim). Passed explicitly so the coordinator never reads
            the system clock itself; this keeps unit tests
            deterministic and lets Hypothesis explore arbitrary
            timing sequences.

        Returns
        -------
        CoalesceDecision
            ``"coalesce_emit"`` (caller dispatches) or
            ``"coalesce_dropped"`` (caller does not dispatch).
        """

        if not isinstance(issue_key, str) or not issue_key:
            raise ValueError(
                "register requires a non-empty issue_key; "
                f"got {issue_key!r}"
            )
        if not isinstance(delivery_id, str) or not delivery_id:
            raise ValueError(
                "register requires a non-empty delivery_id; "
                f"got {delivery_id!r}"
            )

        buffer = self._buffers.get(issue_key)

        # ``now - window_start`` is the elapsed time since the window
        # opened. We compare strictly less than ``window_seconds`` so
        # an event arriving exactly at the boundary opens a fresh
        # window — this matches the spec's "3 saniyelik pencerede"
        # phrasing (events strictly within the window coalesce).
        if buffer is not None and (now - buffer.window_start) < self._window_seconds:
            buffer.dropped_delivery_ids.append(delivery_id)
            # Shallow copy so the caller cannot mutate the buffered
            # payload after handing it over. We accept the trade-off
            # that nested mutable values are still shared — a deep
            # copy would be expensive and the caller never holds a
            # reference to the inner structure once :meth:`register`
            # returns.
            buffer.latest_payload = dict(payload)
            return "coalesce_dropped"

        # Open (or replace) the window. ``dict(payload)`` again
        # provides the shallow-copy isolation described above.
        self._buffers[issue_key] = _WindowBuffer(
            window_start=now,
            anchor_delivery_id=delivery_id,
            dropped_delivery_ids=[],
            latest_payload=dict(payload),
        )
        return "coalesce_emit"

    def flush_window(
        self, issue_key: str
    ) -> tuple[list[str], dict[str, Any]] | None:
        """Close the open window for *issue_key* and return its contents.

        The caller invokes this when it has determined that the
        window has elapsed (typically from a sweeper task that runs
        every ``BURST_WINDOW_SECONDS`` and inspects each issue's
        ``window_start``). The return value carries everything the
        caller needs to dispatch the **terminal** signal:

        * The list of dropped delivery_ids (so the audit log /
          ``FilterDecision.coalesced_with`` can record them).
        * The latest buffered payload (the design's "son event'in
          payload'ı korunur" mandate).

        After flushing, the window is removed from internal state;
        the next :meth:`register` for the same ``issue_key`` will
        open a fresh window.

        Parameters
        ----------
        issue_key:
            The Jira issue key whose window should be flushed.

        Returns
        -------
        tuple[list[str], dict[str, Any]] | None
            ``(dropped_delivery_ids, latest_payload)`` when an open
            window existed; ``None`` when no buffer was found
            (either the window was never opened, or it has already
            been flushed). Callers can use the ``None`` return as a
            signal that "nothing to dispatch".
        """

        buffer = self._buffers.pop(issue_key, None)
        if buffer is None:
            return None

        # The dropped list is already a fresh ``list`` owned by the
        # buffer; we hand it over directly. The payload is also a
        # shallow copy from ``register``, so we hand that over too.
        # Both are safe to mutate by the caller because the buffer
        # has been removed from internal state.
        return buffer.dropped_delivery_ids, buffer.latest_payload

    # ------------------------------------------------------------------
    # Convenience methods — useful for tests and for graceful shutdown
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Drop every open window without flushing.

        Provided for test cleanup and graceful shutdown. Calling
        :meth:`clear` immediately after :meth:`register` is a hard
        cancel — the dropped delivery_ids and the buffered payload
        are discarded.
        """

        self._buffers.clear()

    def has_open_window(self, issue_key: str) -> bool:
        """Return ``True`` iff *issue_key* has an open window."""

        return issue_key in self._buffers
