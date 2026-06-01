"""Property tests for the comment-burst debounce window — task 4.4.

**Validates: Requirements 4.7**

Property statement (design.md §"Property 3", requirement 4.7)
-------------------------------------------------------------

The :class:`automation_service.burst_window.BurstWindow` coordinator
collapses consecutive webhook deliveries that target the same Jira
``issue_key`` within a 3-second wall-clock window into a single
dispatched signal. Specifically:

* Two events with the **same** ``issue_key`` arriving within
  ``BURST_WINDOW_SECONDS`` (3.0s) → the first dispatches
  (``coalesce_emit``); subsequent deliveries inside the window drop
  (``coalesce_dropped``) and are recorded in ``coalesced_with``.
  The latest event's payload is preserved.
* Two events with the same ``issue_key`` arriving **at or after** the
  window threshold (≥3.0s apart) → both dispatch independently
  (``coalesce_emit`` each), because the first event's window has
  closed before the second arrives.
* Events with **different** ``issue_key`` values never coalesce —
  they live in independent windows.
* The ``coalesced_with`` list returned by
  :meth:`BurstWindow.flush_window` contains exactly the dropped
  delivery_ids (the anchor delivery's id is **not** included because
  it was dispatched, not dropped).

Wired-chain invariant
---------------------

When the burst-debounce stage is wired into
:class:`automation_service.webhook_filters.WebhookFilterChain` (task
4.4 wiring), a ``coalesce_dropped`` decision surfaces as
``FilterDecision(action="drop", reason="burst_coalesced",
coalesced_with=...)`` per the design's decision table.

NOT replay-safe
---------------

The :class:`BurstWindow` is intentionally non-replay-safe — it uses
wall-clock timing (``time.monotonic()`` semantics, injected by the
caller as ``now``) and lives in the webhook handler scope rather than
inside any Temporal workflow. The property tests therefore inject
``now`` explicitly so they are deterministic without needing
``time.sleep``.
"""

from __future__ import annotations

import importlib.util as _importlib_util
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Module loading — avoid the heavy ``automation_service`` package __init__
#
# ``automation_service/__init__.py`` imports ``app.py`` which in turn
# imports ``src.config`` from the legacy ``multi-service-scaffold``
# skeleton. Pulling that chain into a property-test process is both
# slow (FastAPI + Vault clients) and fragile (the legacy ``src.*``
# import chain only resolves under the Docker entrypoint). We bypass
# the package init by loading ``burst_window.py`` and
# ``webhook_filters.py`` as standalone modules via
# ``importlib.util.spec_from_file_location``. This mirrors the pattern
# used by ``test_replay_dedup.py`` for ``processed_events.py``.
# ---------------------------------------------------------------------------

_AUTOMATION_PKG_DIR = (
    Path(__file__).resolve().parents[1].parent
    / "services"
    / "automation-service"
    / "src"
    / "automation_service"
)


def _load_module(name: str, file_path: Path) -> Any:
    """Load *file_path* as a top-level module under *name*.

    ``importlib.util.spec_from_file_location`` registers the module
    under a synthetic name so the system import cache cannot pull in
    the real ``automation_service.<name>`` (which would re-trigger
    the package init). The returned object exposes the module's
    public API exactly as a regular import would.
    """

    spec = _importlib_util.spec_from_file_location(name, file_path)
    assert spec is not None and spec.loader is not None, (
        f"Failed to build import spec for {file_path!s}"
    )
    module = _importlib_util.module_from_spec(spec)
    # Register before exec so cross-module imports inside the file
    # (``from automation_service.webhook_filters import ...`` inside
    # ``burst_window.py``, if any) resolve to our synthetic module.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# webhook_filters has no dependencies on burst_window, so load it first.
_webhook_filters_mod = _load_module(
    "_burst_test_webhook_filters",
    _AUTOMATION_PKG_DIR / "webhook_filters.py",
)
# burst_window only uses ``typing`` and the standard library, so it
# loads cleanly without further wiring.
_burst_window_mod = _load_module(
    "_burst_test_burst_window",
    _AUTOMATION_PKG_DIR / "burst_window.py",
)

BURST_WINDOW_SECONDS: float = _burst_window_mod.BURST_WINDOW_SECONDS
BurstWindow = _burst_window_mod.BurstWindow

REASON_BURST_COALESCED: str = _webhook_filters_mod.REASON_BURST_COALESCED
REASON_FILTER_CHAIN_PASS: str = _webhook_filters_mod.REASON_FILTER_CHAIN_PASS
BurstRegisterResult = _webhook_filters_mod.BurstRegisterResult
FilterDecision = _webhook_filters_mod.FilterDecision
WebhookEvent = _webhook_filters_mod.WebhookEvent
WebhookFilterChain = _webhook_filters_mod.WebhookFilterChain


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Issue keys are uppercase project + dash + integer. Bounded to a
# small set of distinct keys per example so the burst-window state
# machine actually exercises the "same key vs different key" branch
# without sprawling into thousands of unique keys per run.
_issue_keys = st.sampled_from(
    [f"PAY-{i}" for i in range(1, 6)]
    + [f"OPS-{i}" for i in range(1, 6)]
    + [f"WEB-{i}" for i in range(1, 6)]
)

# Delivery ids are unique-per-event; we use UUID-like text but bound
# size for shrinking speed.
_delivery_ids = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="-_",
    ),
    min_size=8,
    max_size=24,
)

# Payloads are small JSON-shaped dicts. We don't need recursion here
# because the burst window only cares about *which* payload is
# preserved, not its structure.
_payload_values = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1_000, max_value=1_000),
    st.text(min_size=0, max_size=20),
)
_payloads = st.dictionaries(
    keys=st.text(min_size=1, max_size=8),
    values=_payload_values,
    max_size=5,
)

# Window-relative timestamps — non-negative floats up to 10 seconds
# so we cover both within-window (<3s) and outside-window (>=3s)
# branches generously.
_offsets = st.floats(
    min_value=0.0,
    max_value=10.0,
    allow_nan=False,
    allow_infinity=False,
)


# ---------------------------------------------------------------------------
# Test 1: two events same issue_key within 3s → second is dropped,
# latest payload preserved
# ---------------------------------------------------------------------------


class TestSameKeyWithinWindow:
    """Two same-key events inside the 3-second window coalesce."""

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        issue_key=_issue_keys,
        first_delivery=_delivery_ids,
        second_delivery=_delivery_ids,
        first_payload=_payloads,
        second_payload=_payloads,
        # Constrain second event to land STRICTLY inside the window.
        # ``BURST_WINDOW_SECONDS - 1e-3`` keeps us safely below the
        # threshold even after float quirks.
        gap=st.floats(
            min_value=0.0,
            max_value=BURST_WINDOW_SECONDS - 1e-3,
            allow_nan=False,
            allow_infinity=False,
        ),
        start=st.floats(
            min_value=0.0,
            max_value=10_000.0,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    def test_second_event_within_3s_is_dropped(
        self,
        issue_key: str,
        first_delivery: str,
        second_delivery: str,
        first_payload: dict[str, Any],
        second_payload: dict[str, Any],
        gap: float,
        start: float,
    ) -> None:
        """Inside the window the second event drops; payload is the latest."""

        # Distinct delivery ids are required by the spec — two events
        # sharing the same delivery id would collide on idempotency.
        assume(first_delivery != second_delivery)

        window = BurstWindow()

        first = window.register(
            issue_key=issue_key,
            delivery_id=first_delivery,
            payload=first_payload,
            now=start,
        )
        # The first event always opens a fresh window.
        assert first == "coalesce_emit"

        second = window.register(
            issue_key=issue_key,
            delivery_id=second_delivery,
            payload=second_payload,
            now=start + gap,
        )
        # Strictly within the window: the second event drops.
        assert second == "coalesce_dropped"

        # Flushing the window now must surface the dropped delivery
        # id and the **latest** payload — the design mandates "son
        # event'in payload'ı korunur".
        flush = window.flush_window(issue_key)
        assert flush is not None
        coalesced_with, latest_payload = flush

        # The dropped delivery is exactly the second one. The first
        # delivery is the anchor (it was dispatched immediately) and
        # therefore not in the dropped list.
        assert coalesced_with == [second_delivery]
        # Shallow-equal because :meth:`register` shallow-copies the
        # payload dict but does not deep-copy values.
        assert latest_payload == second_payload

        # After flushing, no buffer remains.
        assert window.flush_window(issue_key) is None
        assert not window.has_open_window(issue_key)


# ---------------------------------------------------------------------------
# Test 2: two events 4s apart (or any gap >= window) → both pass through
# ---------------------------------------------------------------------------


class TestSameKeyOutsideWindow:
    """Two same-key events outside the window are independent."""

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        issue_key=_issue_keys,
        first_delivery=_delivery_ids,
        second_delivery=_delivery_ids,
        first_payload=_payloads,
        second_payload=_payloads,
        # Strictly outside the window — at-or-after the threshold.
        # Add a small epsilon to guard against IEEE-754 rounding when
        # ``start`` has many fractional bits: ``(start + 3.0) - start``
        # may evaluate to ``2.9999999999999996`` for some ``start``
        # values. ``1e-3`` is more than enough headroom for any
        # ``start`` we generate (≤ 10_000.0).
        gap=st.floats(
            min_value=BURST_WINDOW_SECONDS + 1e-3,
            max_value=BURST_WINDOW_SECONDS + 10.0,
            allow_nan=False,
            allow_infinity=False,
        ),
        start=st.floats(
            min_value=0.0,
            max_value=10_000.0,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    def test_second_event_after_window_dispatches_again(
        self,
        issue_key: str,
        first_delivery: str,
        second_delivery: str,
        first_payload: dict[str, Any],
        second_payload: dict[str, Any],
        gap: float,
        start: float,
    ) -> None:
        """At or after 3s gap, the second event opens a fresh window."""

        assume(first_delivery != second_delivery)

        window = BurstWindow()

        first = window.register(
            issue_key=issue_key,
            delivery_id=first_delivery,
            payload=first_payload,
            now=start,
        )
        assert first == "coalesce_emit"

        # The second event arrives at or after the window threshold.
        # The original window has expired (its anchor is older than
        # ``BURST_WINDOW_SECONDS``); the coordinator treats this as a
        # fresh window and emits.
        second = window.register(
            issue_key=issue_key,
            delivery_id=second_delivery,
            payload=second_payload,
            now=start + gap,
        )
        assert second == "coalesce_emit"

        # Flushing the freshly-opened window yields no dropped
        # deliveries (only the second event was anchored, no
        # subsequent events landed).
        flush = window.flush_window(issue_key)
        assert flush is not None
        coalesced_with, latest_payload = flush
        assert coalesced_with == []
        assert latest_payload == second_payload


# ---------------------------------------------------------------------------
# Test 3: different issue_keys never coalesce together
# ---------------------------------------------------------------------------


class TestDifferentKeysIndependent:
    """Different issue_keys live in independent windows."""

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        key_a=_issue_keys,
        key_b=_issue_keys,
        delivery_a=_delivery_ids,
        delivery_b=_delivery_ids,
        payload_a=_payloads,
        payload_b=_payloads,
        # Choose any gap — even a gap that would coalesce on a single
        # key must not coalesce across distinct keys.
        gap=st.floats(
            min_value=0.0,
            max_value=BURST_WINDOW_SECONDS - 1e-3,
            allow_nan=False,
            allow_infinity=False,
        ),
        start=st.floats(
            min_value=0.0,
            max_value=10_000.0,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    def test_different_issue_keys_do_not_coalesce(
        self,
        key_a: str,
        key_b: str,
        delivery_a: str,
        delivery_b: str,
        payload_a: dict[str, Any],
        payload_b: dict[str, Any],
        gap: float,
        start: float,
    ) -> None:
        """Same-window-time events on distinct keys both emit independently."""

        # Keys must differ — the same-key path is exercised by the
        # other tests in this module.
        assume(key_a != key_b)
        assume(delivery_a != delivery_b)

        window = BurstWindow()

        a = window.register(
            issue_key=key_a,
            delivery_id=delivery_a,
            payload=payload_a,
            now=start,
        )
        b = window.register(
            issue_key=key_b,
            delivery_id=delivery_b,
            payload=payload_b,
            now=start + gap,
        )

        # Both events open their own window — distinct issue keys
        # never share state.
        assert a == "coalesce_emit"
        assert b == "coalesce_emit"

        # Each window flushes independently, with no dropped
        # deliveries and the original payload preserved.
        flush_a = window.flush_window(key_a)
        assert flush_a is not None
        assert flush_a[0] == []
        assert flush_a[1] == payload_a

        flush_b = window.flush_window(key_b)
        assert flush_b is not None
        assert flush_b[0] == []
        assert flush_b[1] == payload_b


# ---------------------------------------------------------------------------
# Test 4: coalesced_with list contains exactly the dropped delivery_ids
# ---------------------------------------------------------------------------


class TestCoalescedListExactness:
    """``coalesced_with`` carries every dropped delivery_id, in order.

    This invariant covers the design mandate "coalesced_with listesi
    delivery_id'leri içerir" and the implicit "the anchor delivery
    is not in the dropped list" — the anchor was dispatched, not
    dropped.
    """

    @settings(
        max_examples=200,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(
        issue_key=_issue_keys,
        deliveries=st.lists(
            _delivery_ids,
            min_size=2,
            max_size=8,
            unique=True,
        ),
        payloads=st.lists(_payloads, min_size=2, max_size=8),
        # Each subsequent gap is small enough that all events stay
        # inside the same window. We pick a fixed sub-window step.
        step=st.floats(
            min_value=0.0,
            max_value=(BURST_WINDOW_SECONDS - 1e-3) / 8,
            allow_nan=False,
            allow_infinity=False,
        ),
        start=st.floats(
            min_value=0.0,
            max_value=10_000.0,
            allow_nan=False,
            allow_infinity=False,
        ),
    )
    def test_coalesced_with_lists_dropped_ids_in_order(
        self,
        issue_key: str,
        deliveries: list[str],
        payloads: list[dict[str, Any]],
        step: float,
        start: float,
    ) -> None:
        """Every delivery after the first is in coalesced_with, in order."""

        # Align the payload list with the delivery list — Hypothesis
        # generates them independently so we slice / pad as needed.
        n = min(len(deliveries), len(payloads))
        assume(n >= 2)
        deliveries = deliveries[:n]
        payloads = payloads[:n]

        window = BurstWindow()

        # Register the first event — opens the window.
        first_decision = window.register(
            issue_key=issue_key,
            delivery_id=deliveries[0],
            payload=payloads[0],
            now=start,
        )
        assert first_decision == "coalesce_emit"

        # Register the rest — all should drop because each step is
        # bounded so the cumulative offset stays under the window
        # threshold (n <= 8 and step <= window/8 => total <= window).
        for i in range(1, n):
            decision = window.register(
                issue_key=issue_key,
                delivery_id=deliveries[i],
                payload=payloads[i],
                now=start + step * i,
            )
            assert decision == "coalesce_dropped"

        flush = window.flush_window(issue_key)
        assert flush is not None
        coalesced_with, latest_payload = flush

        # Exactly the post-first deliveries, in observation order.
        assert coalesced_with == deliveries[1:]
        # The latest event wins on payload.
        assert latest_payload == payloads[-1]
        # Anchor is not in the dropped list.
        assert deliveries[0] not in coalesced_with


# ---------------------------------------------------------------------------
# Test 5: chain wiring — coalesce_dropped surfaces as
# FilterDecision(action="drop", reason="burst_coalesced", ...)
# ---------------------------------------------------------------------------


def _make_chain_with_burst(
    burst: BurstWindow, *, now_provider: list[float]
) -> WebhookFilterChain:
    """Build a chain whose burst stage is wired to *burst*.

    All other stages are configured to be effectively no-ops so the
    burst stage is the deciding factor:

    * ``verify_hmac`` always returns True (chain doesn't call it
      because task 4.2 isn't wired into evaluate yet).
    * ``resolve_dept`` returns a fixed dept id.
    * ``bot_account_ids`` returns an empty set (no loop guard).
    * ``is_processed`` returns False (no replay dedup).
    * ``mention_set_for`` returns ``frozenset()`` (no mention drops);
      paired with ``iter_count_for`` returning 1 so the first-iter
      exception applies for the reporter regardless.
    * ``iter_count_for`` returns 1 so :meth:`_stage_mention_filter`
      lets comment events through unchanged.
    * ``reporter_for`` returns a sentinel that does not match any
      actor we use in tests.

    The ``now_provider`` is a list whose first entry is consumed by
    each ``burst_register`` call — tests pop from the head so each
    invocation can set its own wall-clock value deterministically.
    """

    def _burst_register(event: WebhookEvent) -> BurstRegisterResult | None:
        if event.issue_key is None:
            return None
        # Pop the next ``now`` value the test has prepared. Using a
        # list-as-queue keeps the test code straightforward without
        # pulling in unittest.mock.
        now = now_provider.pop(0)
        decision = burst.register(
            issue_key=event.issue_key,
            delivery_id=event.delivery_id,
            payload=dict(event.raw_payload),
            now=now,
        )
        if decision == "coalesce_dropped":
            # Build the running coalesced_with by peeking at the
            # buffer non-destructively. We can do this by exploiting
            # the design contract: the buffer's dropped list is
            # ordered, and ``flush_window`` returns the same. Here
            # we cheat slightly by calling ``flush_window`` to read
            # the state, then re-registering would be wrong — so we
            # instead reach into the private ``_buffers`` dict. This
            # is acceptable in tests because the chain wiring at
            # production time will use a sweeper task that reads
            # ``flush_window`` directly.
            buffer = burst._buffers.get(event.issue_key)  # noqa: SLF001
            coalesced = (
                tuple(buffer.dropped_delivery_ids) if buffer is not None else ()
            )
            return BurstRegisterResult(
                decision="coalesce_dropped",
                coalesced_with=coalesced,
            )
        return BurstRegisterResult(decision="coalesce_emit", coalesced_with=())

    return WebhookFilterChain(
        verify_hmac=lambda _e: True,
        resolve_dept=lambda _e: "dept-test",
        bot_account_ids=lambda: frozenset(),
        is_processed=lambda _d: False,
        mention_set_for=lambda _k: frozenset(),
        iter_count_for=lambda _k: 1,
        reporter_for=lambda _k: "__no_reporter__",
        burst_register=_burst_register,
    )


def _comment_event(
    *, issue_key: str, delivery_id: str, payload: dict[str, Any]
) -> WebhookEvent:
    """Build a Jira ``issue_commented`` event with the given fields."""

    return WebhookEvent(
        provider="jira",
        # Use a non-comment event_type so the mention filter does
        # not engage; we want the burst stage to be the deciding
        # filter for these tests.
        event_type="jira:issue_updated",
        delivery_id=delivery_id,
        actor_account_id="actor-test",
        body_text=None,
        project_key=issue_key.split("-", 1)[0],
        repo_slug=None,
        issue_key=issue_key,
        pr_id=None,
        raw_payload=payload,
    )


class TestChainWiring:
    """The chain translates burst decisions into FilterDecision verdicts."""

    def test_first_event_passes_through(self) -> None:
        """Fresh window → ``filter_chain_pass``."""

        burst = BurstWindow()
        now_q: list[float] = [100.0]
        chain = _make_chain_with_burst(burst, now_provider=now_q)

        event = _comment_event(
            issue_key="PAY-1",
            delivery_id="d1",
            payload={"version": 1},
        )
        decision = chain.evaluate(event)

        assert isinstance(decision, FilterDecision)
        assert decision.action == "pass"
        assert decision.reason == REASON_FILTER_CHAIN_PASS
        assert decision.coalesced_with == ()

    def test_second_event_within_window_drops_with_burst_coalesced(self) -> None:
        """Second event inside window → drop, reason="burst_coalesced"."""

        burst = BurstWindow()
        # First call: t=100, second call: t=101 (within 3s window).
        now_q: list[float] = [100.0, 101.0]
        chain = _make_chain_with_burst(burst, now_provider=now_q)

        first = chain.evaluate(
            _comment_event(
                issue_key="PAY-1",
                delivery_id="d1",
                payload={"v": 1},
            )
        )
        second = chain.evaluate(
            _comment_event(
                issue_key="PAY-1",
                delivery_id="d2",
                payload={"v": 2},
            )
        )

        assert first.action == "pass"
        assert second.action == "drop"
        assert second.reason == REASON_BURST_COALESCED
        # The dropped delivery_id is in the coalesced_with tuple.
        assert second.coalesced_with == ("d2",)

    def test_third_event_within_window_extends_coalesced_with(self) -> None:
        """Three events same-key in window → second & third drop, both listed."""

        burst = BurstWindow()
        now_q: list[float] = [100.0, 100.5, 101.0]
        chain = _make_chain_with_burst(burst, now_provider=now_q)

        chain.evaluate(_comment_event(issue_key="PAY-1", delivery_id="d1", payload={}))
        second = chain.evaluate(
            _comment_event(issue_key="PAY-1", delivery_id="d2", payload={})
        )
        third = chain.evaluate(
            _comment_event(issue_key="PAY-1", delivery_id="d3", payload={})
        )

        assert second.action == "drop"
        assert second.reason == REASON_BURST_COALESCED
        assert second.coalesced_with == ("d2",)

        assert third.action == "drop"
        assert third.reason == REASON_BURST_COALESCED
        # The third event's coalesced_with includes BOTH dropped
        # delivery ids in observation order; the anchor "d1" is not
        # in the list.
        assert third.coalesced_with == ("d2", "d3")

    def test_event_after_window_passes_again(self) -> None:
        """≥3s after the first event opens a fresh window and passes through."""

        burst = BurstWindow()
        # First at t=100, second at t=104 (4s later, outside 3s window).
        now_q: list[float] = [100.0, 104.0]
        chain = _make_chain_with_burst(burst, now_provider=now_q)

        first = chain.evaluate(
            _comment_event(issue_key="PAY-1", delivery_id="d1", payload={})
        )
        second = chain.evaluate(
            _comment_event(issue_key="PAY-1", delivery_id="d2", payload={})
        )

        assert first.action == "pass"
        assert first.reason == REASON_FILTER_CHAIN_PASS

        # The second event is outside the window; the burst stage
        # opens a fresh window for it and the chain falls through to
        # the default pass.
        assert second.action == "pass"
        assert second.reason == REASON_FILTER_CHAIN_PASS

    def test_chain_without_burst_register_skips_stage(self) -> None:
        """A chain with ``burst_register=None`` ignores the burst stage."""

        chain = WebhookFilterChain(
            verify_hmac=lambda _e: True,
            resolve_dept=lambda _e: "dept-test",
            bot_account_ids=lambda: frozenset(),
            is_processed=lambda _d: False,
            mention_set_for=lambda _k: frozenset(),
            iter_count_for=lambda _k: 1,
            reporter_for=lambda _k: "__no_reporter__",
            # burst_register intentionally omitted (defaults to None).
        )

        event = _comment_event(
            issue_key="PAY-1", delivery_id="d1", payload={"v": 1}
        )
        decision = chain.evaluate(event)
        # Without the stage wired, every well-formed event passes
        # through with the default ``filter_chain_pass`` reason.
        assert decision.action == "pass"
        assert decision.reason == REASON_FILTER_CHAIN_PASS


# ---------------------------------------------------------------------------
# Test 6: example-based smoke checks for boundary conditions
# ---------------------------------------------------------------------------


class TestBoundaryBehaviour:
    """Targeted example tests for window-edge timing."""

    def test_event_exactly_at_window_threshold_opens_fresh_window(self) -> None:
        """At ``BURST_WINDOW_SECONDS`` exactly, the new event emits.

        The implementation uses a strict ``<`` comparison
        (``now - window_start < window_seconds``), so an event whose
        gap equals the threshold falls outside the window. This
        test pins that boundary.
        """

        window = BurstWindow()

        first = window.register(
            issue_key="PAY-1",
            delivery_id="d1",
            payload={},
            now=0.0,
        )
        second = window.register(
            issue_key="PAY-1",
            delivery_id="d2",
            payload={},
            now=BURST_WINDOW_SECONDS,
        )

        assert first == "coalesce_emit"
        assert second == "coalesce_emit"

    def test_negative_window_seconds_raises(self) -> None:
        """A negative window is rejected at construction time."""

        with pytest.raises(ValueError):
            BurstWindow(window_seconds=-1.0)

    def test_register_rejects_empty_issue_key(self) -> None:
        """``issue_key`` must be a non-empty string."""

        window = BurstWindow()
        with pytest.raises(ValueError):
            window.register(
                issue_key="",
                delivery_id="d1",
                payload={},
                now=0.0,
            )

    def test_register_rejects_empty_delivery_id(self) -> None:
        """``delivery_id`` must be a non-empty string."""

        window = BurstWindow()
        with pytest.raises(ValueError):
            window.register(
                issue_key="PAY-1",
                delivery_id="",
                payload={},
                now=0.0,
            )

    def test_flush_window_returns_none_for_unknown_key(self) -> None:
        """Flushing an issue with no open window yields ``None``."""

        window = BurstWindow()
        assert window.flush_window("PAY-999") is None

    def test_payload_isolation_via_shallow_copy(self) -> None:
        """Mutating the caller's payload after register does not affect buffer."""

        window = BurstWindow()
        payload: dict[str, Any] = {"v": 1}
        window.register(
            issue_key="PAY-1",
            delivery_id="d1",
            payload=payload,
            now=0.0,
        )
        # Mutate the caller's dict.
        payload["v"] = 999
        flush = window.flush_window("PAY-1")
        assert flush is not None
        _, latest_payload = flush
        # The buffered payload retains the value at register time.
        assert latest_payload == {"v": 1}
