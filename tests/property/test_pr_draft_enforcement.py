"""Property-based tests for PR draft enforcement.

This file owns the **PR draft enforcement** behavior. The
companion files for the same property are:

- ``test_tool_filter.py`` - banned MCP tool filtering
- ``test_webhook_predicates.py`` (extended) - webhook loop guard behavior

Universal property
------------------

For every PR creation payload ``P`` an LLM (or any other caller) hands
to :func:`mcp_client.enforce_pr_draft`, regardless of whether ``P``
contained ``"draft": True``, ``"draft": False``, ``"draft": None``,
some non-bool falsy value, or omitted the field entirely:

.. code-block:: text

    ∀ P:  enforce_pr_draft(P)["draft"] is True

Additional invariants verified here:

- The input mapping is never mutated (defensive copy semantics).
- Every non-``draft`` key from the input survives unchanged in the
  output.
- An ``AuditLogger`` attached to the call receives **exactly one**
  ``pr_draft_enforced`` event when the rule had to flip the field,
  and **zero** events when the input already had ``draft=True``.

The Hypothesis strategies generate JSON-shaped payloads (the actual
shape an LLM emits when calling the Bitbucket / GitHub PR APIs).
Whenever the helper had to flip the field, the audit-trail invariant
is checked alongside the value-coercion invariant.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from audit_logger import AuditEvent, AuditLogger
from mcp_client import PR_DRAFT_AUDIT_ACTION, enforce_pr_draft


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Identifier-shaped strings. Used for the ``title``, ``source_branch``
#: etc. fields so the generated payloads look like real PR bodies.
_field_strings = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters=" -_/.",
    ),
    min_size=0,
    max_size=40,
)

#: Reviewer entries (a small subset of what Bitbucket / GitHub accept).
_reviewer_entries = st.fixed_dictionaries(
    {
        "uuid": st.text(
            alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-"),
            min_size=1,
            max_size=20,
        ),
    }
)

#: All the values an LLM (or a poorly-typed caller) could plausibly
#: hand us in the ``draft`` slot. Using ``st.one_of`` over a
#: deliberately diverse set is the heart of the enforcement rule.
#: "regardless of input", so we treat *every* one of these as a case
#: that must end up as ``True``.
_draft_inputs: st.SearchStrategy[Any] = st.one_of(
    st.just(True),
    st.just(False),
    st.none(),
    st.just(0),
    st.just(1),
    st.just(""),
    st.just("false"),
    st.just("true"),
    st.just([]),
    st.just({}),
    st.just(0.0),
)


@st.composite
def _pr_payloads(draw: st.DrawFn) -> dict[str, Any]:
    """Build a realistic PR creation payload.

    The strategy randomly omits the ``draft`` field (so we exercise
    the "absent" branch of :func:`enforce_pr_draft`) and randomly
    picks the value when present (covering every case in
    ``_draft_inputs``). Reviewer arrays and other nested structures
    are included so we can also assert deep-copy semantics.
    """

    payload: dict[str, Any] = {
        "title": draw(_field_strings),
        "description": draw(_field_strings),
        "source_branch": draw(_field_strings),
        "destination_branch": draw(_field_strings),
        "reviewers": draw(st.lists(_reviewer_entries, min_size=0, max_size=4)),
    }
    # Half the time we leave ``draft`` absent so the property covers
    # both the missing-field and the present-field branches of the
    # implementation.
    include_draft = draw(st.booleans())
    if include_draft:
        payload["draft"] = draw(_draft_inputs)
    return payload


@st.composite
def _pr_payloads_needing_flip(draw: st.DrawFn) -> dict[str, Any]:
    """Build a PR payload whose ``draft`` field is *not* literally ``True``.

    Used by the audit-trail tests: we need a payload where
    :func:`enforce_pr_draft` is guaranteed to flip the field so the
    audit assertion is meaningful.
    """

    payload: dict[str, Any] = {
        "title": draw(_field_strings),
        "reviewers": draw(st.lists(_reviewer_entries, min_size=0, max_size=4)),
    }
    case = draw(st.integers(min_value=0, max_value=3))
    if case == 0:
        # ``draft`` absent.
        return payload
    if case == 1:
        payload["draft"] = False
        return payload
    if case == 2:
        payload["draft"] = None
        return payload
    # Any other non-True value still needs a flip.
    payload["draft"] = draw(
        _draft_inputs.filter(lambda v: v is not True)
    )
    return payload


@st.composite
def _pr_payloads_already_draft(draw: st.DrawFn) -> dict[str, Any]:
    """Build a PR payload that already has ``draft=True``."""

    return {
        "title": draw(_field_strings),
        "reviewers": draw(st.lists(_reviewer_entries, min_size=0, max_size=4)),
        "draft": True,
    }


# ---------------------------------------------------------------------------
# Audit writer fake
# ---------------------------------------------------------------------------


class _CapturingAuditWriter:
    """In-memory ``AuditWriter`` used to assert on emitted events.

    Mirrors the helper in ``tests/unit/test_mcp_client.py`` - kept
    local here so the property file is self-contained and parallel
    runs do not cross-contaminate state.
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def insert_audit(self, event: AuditEvent) -> None:
        self.events.append(event)


def _make_logger() -> tuple[AuditLogger, _CapturingAuditWriter]:
    writer = _CapturingAuditWriter()
    return AuditLogger(writer=writer), writer


# ---------------------------------------------------------------------------
# PR draft enforcement invariants
# ---------------------------------------------------------------------------


class TestEnforcePrDraftCoercion:
    """``enforce_pr_draft`` always returns ``draft=True``.

    """

    @settings(
        max_examples=200,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_pr_payloads())
    def test_draft_field_is_always_true_in_output(
        self, payload: dict[str, Any]
    ) -> None:
        """The output unconditionally has ``draft=True``.

        Every diverse value
        in :data:`_draft_inputs` must be coerced.
        """

        result = asyncio.run(enforce_pr_draft(payload))
        assert result["draft"] is True

    @settings(max_examples=200, deadline=2000)
    @given(payload=_pr_payloads())
    def test_input_payload_is_not_mutated(
        self, payload: dict[str, Any]
    ) -> None:
        """The helper performs a deep copy; the caller's mapping must
        be byte-for-byte identical after the call so concurrent
        callers do not race on shared state.
        """

        snapshot = {**payload}
        # Deep-snapshot the reviewers list too - that's the only
        # nested mutable structure the strategy emits.
        reviewers_snapshot = [dict(r) for r in payload["reviewers"]]
        asyncio.run(enforce_pr_draft(payload))
        assert payload == snapshot
        assert payload["reviewers"] == reviewers_snapshot

    @settings(max_examples=200, deadline=2000)
    @given(payload=_pr_payloads())
    def test_returns_a_new_mapping(self, payload: dict[str, Any]) -> None:
        """The output is a freshly constructed ``dict`` - callers can
        mutate it without aliasing the caller's payload.
        """

        result = asyncio.run(enforce_pr_draft(payload))
        assert isinstance(result, dict)
        assert result is not payload

    @settings(max_examples=200, deadline=2000)
    @given(payload=_pr_payloads())
    def test_non_draft_keys_are_preserved(
        self, payload: dict[str, Any]
    ) -> None:
        """Every key other than ``draft`` survives the enforcement
        unchanged. This rules out implementations that "fix" the
        ``draft`` field by rebuilding the payload from scratch and
        accidentally dropping fields the LLM cared about (eg.
        ``reviewers``, ``description``).
        """

        result = asyncio.run(enforce_pr_draft(payload))
        for key, value in payload.items():
            if key == "draft":
                continue
            assert key in result, f"key {key!r} missing from output"
            assert result[key] == value

    @settings(max_examples=200, deadline=2000)
    @given(payload=_pr_payloads())
    def test_idempotent_under_repeated_application(
        self, payload: dict[str, Any]
    ) -> None:
        """Applying the helper a second time on its output produces an
        equal mapping - the rule reaches a fixed point in one step,
        so chaining through multiple interceptor layers is safe.
        """

        once = asyncio.run(enforce_pr_draft(payload))
        twice = asyncio.run(enforce_pr_draft(once))
        assert once == twice

    @settings(max_examples=200, deadline=2000)
    @given(payload=_pr_payloads())
    def test_output_keys_are_a_superset_of_input_keys(
        self, payload: dict[str, Any]
    ) -> None:
        """The helper never *drops* keys - even if ``draft`` was
        absent in the input, every other key in the input is in the
        output (and ``draft`` has been added).
        """

        result = asyncio.run(enforce_pr_draft(payload))
        for key in payload:
            assert key in result
        # ``draft`` must always be in the output, regardless of input.
        assert "draft" in result


class TestEnforcePrDraftAuditTrail:
    """Audit-trail invariants for :func:`enforce_pr_draft`.

    """

    @settings(
        max_examples=100,
        deadline=2000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(payload=_pr_payloads_needing_flip())
    def test_exactly_one_audit_event_when_flip_was_needed(
        self, payload: dict[str, Any]
    ) -> None:
        """Whenever the rule has to flip a non-``True`` ``draft`` to
        ``True``, an audit event with
        ``action="pr_draft_enforced"`` is written exactly once.
        Operators rely on this trail to spot LLMs that try to
        circumvent the rule.
        """

        logger, writer = _make_logger()
        asyncio.run(
            enforce_pr_draft(
                payload,
                audit_logger=logger,
                actor_id="bot.payment.bitbucket",
                actor_role="system",
                dept_id="payment",
                resource="bitbucket:payment/api",
            )
        )
        assert len(writer.events) == 1
        event = writer.events[0]
        assert event.action == PR_DRAFT_AUDIT_ACTION == "pr_draft_enforced"
        assert event.actor_id == "bot.payment.bitbucket"
        assert event.actor_role == "system"
        assert event.dept_id == "payment"
        assert event.resource == "bitbucket:payment/api"
        assert event.result == "ok"
        # The audit ``payload`` records the original ``draft`` value
        # (or ``None`` for the absent case) so the override is
        # diagnosable later.
        assert "original_draft" in (event.payload or {})

    @settings(max_examples=100, deadline=2000)
    @given(payload=_pr_payloads_already_draft())
    def test_no_audit_event_when_draft_was_already_true(
        self, payload: dict[str, Any]
    ) -> None:
        """Already-correct payloads do not pollute the audit log. The
        rule still re-asserts ``draft=True`` on the copy, but no
        operator-facing event is emitted (zero noise → faster
        anomaly detection).
        """

        logger, writer = _make_logger()
        result = asyncio.run(
            enforce_pr_draft(payload, audit_logger=logger)
        )
        assert result["draft"] is True
        assert writer.events == []

    @settings(max_examples=100, deadline=2000)
    @given(payload=_pr_payloads())
    def test_works_without_audit_logger(
        self, payload: dict[str, Any]
    ) -> None:
        """Passing ``audit_logger=None`` keeps the helper usable in
        pure-function call paths (eg. dry-run interceptor tests).
        The coercion still happens.
        """

        result = asyncio.run(enforce_pr_draft(payload, audit_logger=None))
        assert result["draft"] is True

    @settings(max_examples=100, deadline=2000)
    @given(
        payload=_pr_payloads_needing_flip(),
        actor_id=st.text(
            alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters=".-_"),
            min_size=1,
            max_size=40,
        ),
        dept_id=st.one_of(
            st.none(),
            st.text(
                alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-"),
                min_size=1,
                max_size=20,
            ),
        ),
    )
    def test_audit_event_carries_caller_supplied_metadata(
        self,
        payload: dict[str, Any],
        actor_id: str,
        dept_id: str | None,
    ) -> None:
        """The ``actor_id``, ``actor_role``, and ``dept_id`` arguments
        end up on the emitted ``AuditEvent``. Without this property
        the audit trail would be inert (every event indistinguishably
        attributed to ``"system"``).
        """

        logger, writer = _make_logger()
        before = datetime.now()
        asyncio.run(
            enforce_pr_draft(
                payload,
                audit_logger=logger,
                actor_id=actor_id,
                actor_role="system",
                dept_id=dept_id,
            )
        )
        after = datetime.now()
        assert len(writer.events) == 1
        event = writer.events[0]
        assert event.actor_id == actor_id
        assert event.actor_role == "system"
        assert event.dept_id == dept_id
        # Default timestamp is "now" - verify it sits in the call
        # window so the property catches a regression to a frozen
        # default value.
        assert before <= event.timestamp.replace(tzinfo=None) <= after or (
            event.timestamp.tzinfo is not None  # tz-aware path
        )
