"""Invariant test: Needs-info signal delivery.

**: Needs-info signal delivery guarantee
------------------------------------------------
*For any* comment on a needs_info issue (where the commenter is not a
bot), the comment body SHALL be delivered as a Temporal signal to the
waiting workflow within 5 seconds of webhook receipt.

The webhook  workflow latency itself is a Temporal infrastructure
property (HTTP handler  ``Client.signal_workflow``  Temporal
front-end  workflow event history) and cannot be measured from a
unit test without a live cluster. What we *can* validate as a
property - and what explicitly asks for - is that the:class:`AutomationWorkflow.info_received` signal handler:

1. Accepts every payload shape the dispatcher (or a misconfigured
 data converter) can plausibly emit: ``str``, ``dict``, ``None``,
 and other Python scalars.
2. Never raises - a raising signal handler would block delivery and
 force Temporal to retry the signal indefinitely, blowing the 5s
 delivery budget.
3. Always flips the ``_info_received`` edge flag to ``True`` so the
 ``run`` body's ``wait_condition`` predicate fires on the next
 workflow tick.
4. Normalises the payload into a deterministic ``_pending_comment_body``
 string the re-analysis loop can feed back into ``llm_analyze_task``
 without further coercion.
5. Records non-empty normalised bodies into ``_info_received_history``
 in arrival order so an operator can audit the conversation that
 led the workflow back out of ``needs_info``.

The unit-level ``TestInfoReceivedSignalHandler`` covers fixed
examples; this Invariant test covers the *space* of payloads with
Hypothesis so a regression in payload coercion (e.g. a refactor that
suddenly raises on a ``bytes`` body) surfaces immediately.

The 5-second SLA is encoded as a per-call wall-clock budget on the
signal handler itself - every example must complete in well under
that bound. The handler does no I/O so the actual numbers run in
microseconds, but the assertion makes the SLA explicit in code.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap - match sibling Invariant tests so we import the
# real workflow class (not a stub).
# ---------------------------------------------------------------------------

_WORKER_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_DIR: Path = _WORKER_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# pylint: disable=wrong-import-position
from automation_worker.workflows.automation_workflow import (  # noqa: E402
    AutomationWorkflow,
    _SIGNAL_INFO_RECEIVED,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Signal delivery SLA from /. The handler itself
#: does no I/O, so the realistic runtime is microseconds - we set the
#: budget to a generous fraction of the 5s bound to leave headroom
#: for slow CI runners while still failing loudly on a regression
#: that introduces synchronous network I/O.
_SIGNAL_HANDLER_BUDGET_SECONDS: float = 1.0


# ---------------------------------------------------------------------------
# Hypothesis strategies - payloads the dispatcher may plausibly emit
# ---------------------------------------------------------------------------

# Plain strings - the normal shape coming out of
# ``WebhookDispatcher._signal_workflow`` (see
# ``platform/services/automation-service/src/webhooks/dispatcher.py``).
# Empty strings are allowed because Jira can emit a comment with no
# visible body (e.g. an attachment-only reply).
_string_payloads: st.SearchStrategy[str] = st.text(max_size=500)

# Dict payloads - some Temporal data converters wrap a single
# positional argument in ``{"comment_body": "..."}``. The handler
# must extract the field if present and otherwise coerce to "".
_dict_with_comment_body: st.SearchStrategy[dict[str, Any]] = st.fixed_dictionaries(
    {"comment_body": _string_payloads}
)
_dict_without_comment_body: st.SearchStrategy[dict[str, Any]] = st.dictionaries(
    keys=st.text(min_size=1, max_size=20).filter(
        lambda s: s != "comment_body"
    ),
    values=st.one_of(_string_payloads, st.integers(), st.none()),
    max_size=4,
)
_dict_payloads: st.SearchStrategy[dict[str, Any]] = st.one_of(
    _dict_with_comment_body, _dict_without_comment_body
)

# Other Python scalars - defensive coverage for misconfigured
# converters or future refactors that change the wire shape. The
# handler must coerce these via ``str(...)`` rather than raising.
_misc_scalar_payloads: st.SearchStrategy[Any] = st.one_of(
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.lists(st.text(max_size=20), max_size=4),
)

# Combined payload strategy. ``None`` is tested separately as a
# fixed example (it is a single value so Hypothesis adds no signal
# beyond the unit test) but included here for completeness.
_any_payload: st.SearchStrategy[Any] = st.one_of(
    _string_payloads,
    _dict_payloads,
    st.none(),
    _misc_scalar_payloads,
)


# ---------------------------------------------------------------------------
# Helpers - predict the post-state the handler should produce so we
# can compare against actual. These mirror the documented contract
# of ``info_received`` and are independent of the implementation
# details so a refactor that keeps the contract still passes.
# ---------------------------------------------------------------------------


def _expected_pending_body(payload: Any) -> str:
    """Predict ``_pending_comment_body`` for a given payload."""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        # Defensive: dispatcher may wrap into {"comment_body": "..."}.
        return str(payload.get("comment_body", "") or "")
    if payload is None:
        return ""
    return str(payload)


# ---------------------------------------------------------------------------
# - signal handler accepts any payload and reaches the
# expected post-state without raising or blocking.
# ---------------------------------------------------------------------------


class TestSignalHandlerAcceptsAnyPayload:
    """For any payload the dispatcher (or a misbehaving data converter)
 can deliver, ``info_received`` SHALL:

 * Return synchronously without raising.
 * Flip ``_info_received`` to ``True``.
 * Set ``_pending_comment_body`` to the documented coercion.
 * Append the normalised body to ``_info_received_history`` iff
 the body is non-empty.
 * Complete in well under the 5-second SLA.

 **"""

    @settings(max_examples=300, deadline=None)
    @given(payload=_any_payload)
    def test_handler_reaches_expected_post_state(self, payload: Any) -> None:
        wf = AutomationWorkflow()

        # Pre-condition - fresh workflow, signal flag unset.
        assert wf._info_received is False  # noqa: SLF001
        assert wf._pending_comment_body is None  # noqa: SLF001
        assert wf._info_received_history == []  # noqa: SLF001

        start = time.perf_counter()
        # The signal handler must never raise; a raising handler
        # would force Temporal to retry the signal and miss the 5s
        # delivery SLA.
        wf.info_received(payload)
        elapsed = time.perf_counter() - start

        # SLA: synchronous, no I/O - handler should finish far inside
        # the 5s budget. Generous slack so slow CI runners don't
        # produce false negatives.
        assert elapsed < _SIGNAL_HANDLER_BUDGET_SECONDS, (
            f"info_received took {elapsed:.3f}s for payload={payload!r}; "
            f"signal handler must stay well below the 5s SLA "
            f"(budget={_SIGNAL_HANDLER_BUDGET_SECONDS}s)."
        )

        # The wait_condition predicate the run body parks on
        # observes this flag - it must always flip on signal arrival
        # so the workflow can resume.
        assert wf._info_received is True, (  # noqa: SLF001
            f"Signal flag did not flip for payload={payload!r}"
        )

        expected = _expected_pending_body(payload)
        assert wf._pending_comment_body == expected, (  # noqa: SLF001
            f"_pending_comment_body={wf._pending_comment_body!r}; "  # noqa: SLF001
            f"expected={expected!r} for payload={payload!r}"
        )

        # Non-empty bodies are auditable - they go into the history
        # so operators can replay the conversation that resolved
        # needs_info. Empty bodies (None, "", dict missing the key)
        # only flip the edge flag.
        if expected:
            assert wf._info_received_history == [expected], (  # noqa: SLF001
                f"history={wf._info_received_history!r}; "  # noqa: SLF001
                f"expected=[{expected!r}] for payload={payload!r}"
            )
        else:
            assert wf._info_received_history == [], (  # noqa: SLF001
                f"history={wf._info_received_history!r} should be "  # noqa: SLF001
                f"empty for falsy payload={payload!r}"
            )


class TestSignalHandlerSequenceAccumulatesHistory:
    """Multiple signals on the same workflow accumulate non-empty
 bodies into ``_info_received_history`` in arrival order. The
 most recent body is exposed via ``_pending_comment_body`` so the
 re-analysis loop sees the latest reply.

 **(signal received  run body sees
 new comment text on next wait wake-up).
 """

    @settings(max_examples=100, deadline=None)
    @given(payloads=st.lists(_any_payload, min_size=1, max_size=8))
    def test_history_records_non_empty_bodies_in_order(
        self, payloads: list[Any]
    ) -> None:
        wf = AutomationWorkflow()

        for payload in payloads:
            wf.info_received(payload)

        # Edge flag stays True for the lifetime of the workflow once
        # any signal has arrived.
        assert wf._info_received is True  # noqa: SLF001

        # The exposed body is the most recently coerced payload -
        # even if that coercion is the empty string. This matches
        # the run body's "reset before wait, capture after wait"
        # contract.
        assert wf._pending_comment_body == _expected_pending_body(  # noqa: SLF001
            payloads[-1]
        )

        # History contains exactly the non-empty coerced bodies in
        # arrival order.
        expected_history = [
            _expected_pending_body(p)
            for p in payloads
            if _expected_pending_body(p)
        ]
        assert wf._info_received_history == expected_history, (  # noqa: SLF001
            f"history={wf._info_received_history!r}; "  # noqa: SLF001
            f"expected={expected_history!r}; payloads={payloads!r}"
        )


class TestSignalHandlerIsRegisteredWithTemporal:
    """Sanity: the handler is decorated with the Temporal signal
 decorator under the name the dispatcher emits. Without this
 registration Temporal silently drops the signal - which would
 violate the 5s delivery SLA in the worst possible way (no
 delivery at all)."""

    def test_signal_name_matches_dispatcher_contract(self) -> None:
        # The dispatcher emits ``info_received`` (see
        # platform/services/automation-service/src/webhooks/dispatcher.py
        # line 265: ``await self._signal_workflow(..., "info_received",...)``).
        assert _SIGNAL_INFO_RECEIVED == "info_received"

    def test_handler_is_registered_as_temporal_signal(self) -> None:
        wf = AutomationWorkflow()
        defn = getattr(
            wf.info_received, "__temporal_signal_definition", None
        )
        # SDK version variance: some versions store the definition
        # on the unbound function instead of the bound method.
        if defn is None:
            defn = getattr(
                AutomationWorkflow.info_received,
                "__temporal_signal_definition",
                None,
            )
        assert defn is not None, (
            "info_received is not registered as a Temporal signal - "
            "Temporal would drop signals silently, breaking."
        )
        assert getattr(defn, "name", None) == "info_received"
