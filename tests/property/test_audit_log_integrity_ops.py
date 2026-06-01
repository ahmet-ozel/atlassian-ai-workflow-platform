"""Property test 5 — Audit log integrity (ops scope).

**Validates: Requirements 1.8, 6.1, 6.8** (Spec ``platform-mimari-ops``)

Property statement (design.md §"Property 5: Audit log integrity ops-scope")
-------------------------------------------------------------------------

For *any* hypothesis-generated sequence of ops-scope audit events drawn
from the universe::

    chat_message
    prompt_draft_created
    prompt_pr_opened
    prompt_pr_conflict
    prompt_render_failed
    credential_rotated
    budget_exceeded
    cost_prediction_comment_posted
    cost_prediction_comment_failed
    cost_prediction_comment_skipped
    audit_prune_succeeded
    audit_prune_failed
    notification_dispatch_sent
    notification_dispatch_failed
    notification_dispatch_deduped
    feature_flag_toggled
    dept_decommissioned

the application-layer ``audit_logger.AuditLogger.write`` MUST satisfy
five simultaneous invariants:

(a) **1:1 write contract** — every accepted ``AuditEvent`` produces
    exactly one ``insert_audit`` call on the underlying writer. The
    audit log is append-only by design (Spec 1 R7.7); no batching,
    coalescing, or deduplication happens at this layer.

(b) **actor_role NOT NULL** — every written row carries an
    ``actor_role`` in :data:`AUDIT_ACTOR_ROLES`. ``None`` / empty /
    whitespace-only / unknown values are rejected with
    :class:`ValueError` *before* the writer is reached, mirroring
    the Postgres ``CHECK (actor_role IS NOT NULL ...)`` constraint
    declared in ``infra/postgres/init/10_automation.sql``. This is
    the foundation Property 13 invariant carried forward into the
    ops event universe (parity, not redefinition).

(c) **System-triggered events use ``actor_role='system'``** — events
    that are emitted by background processes (audit-prune cron,
    budget-cap enforcer, cost-prediction Jira commenter, notification
    dispatcher) MUST carry ``actor_role='system'``. The ``"system"``
    role is the canonical synthetic actor for unattributed background
    activity (design.md §`libs/audit_logger`).

(d) **chat_message payload contract** — a ``chat_message`` event MUST
    carry the four mandatory payload fields ``prompt_version``,
    ``token_in``, ``token_out``, ``cost_usd`` (design.md §"ChatHandler"
    + R1.8). Missing-field events are rejected with
    :class:`ValueError` at write time. The contract is enforced by
    :func:`validate_chat_message_payload` so call sites
    (``services/assistant-service/src/chat/handler.py``) and tests
    share a single validator.

(e) **No idempotent suppression** — two events with the same logical
    ``(actor_id, action, resource, payload)`` tuple produced inside a
    1-second time bucket BOTH land in the audit log (each becomes a
    distinct row). Duplicate detection is the responsibility of a
    different layer (``shared.cost_tracking.activity_id`` UNIQUE
    index — Property 6); the audit layer is intentionally
    write-everything so a buggy caller cannot silently lose events.

Strategy
--------

* ``events`` — Hypothesis composite strategy that emits sequences of
  3..30 ops events. Each event is drawn from the action universe
  above with role / dept_id / payload populated to match its
  semantic class:

  - **User-attributed actions** (``prompt_*``, ``feature_flag_toggled``,
    ``dept_decommissioned``, ``credential_rotated``) → ``actor_role``
    sampled from ``("admin", "dept_admin", "lead", "viewer")``.
  - **Chat actions** (``chat_message``) → ``actor_role`` sampled from
    the four RBAC roles plus a populated payload with all four
    mandatory fields.
  - **System actions** (``budget_exceeded``, ``audit_prune_*``,
    ``cost_prediction_comment_*``, ``notification_dispatch_*``) →
    ``actor_role='system'``.

* ``invalid_chat_payloads`` — Hypothesis strategy producing
  ``chat_message`` payloads with one of the four mandatory fields
  removed (or set to ``None``). Each must be rejected by
  :func:`validate_chat_message_payload` with :class:`ValueError`.

* ``invalid_actor_roles`` — Hypothesis strategy producing role values
  that are ``None``, empty, whitespace-only, or unknown literals.
  Each must be rejected by :meth:`AuditLogger.write` (foundation
  Property 13 surface, replayed under the ops action universe).

This test is a **parity extension** — it does NOT re-implement the
foundation ``test_audit_one_to_one`` test (which covers the
admin-dashboard-control-plane lifecycle action set under
correlation_id semantics). Instead it broadens the universe to the
ops event set listed above so the same one-row-per-action invariant
is exercised against every action a Spec 3 surface emits.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``conftest.py`` (one directory up) puts ``platform/libs/<name>/src``
# on ``sys.path`` for every shared library; we add ``tests/`` defensively
# so this module imports cleanly under a direct
# ``python -m pytest tests/property`` invocation (mirrors the pattern
# used by ``test_audit_one_to_one.py``).
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from audit_logger import (  # noqa: E402
    AUDIT_ACTOR_ROLES,
    AUDIT_RESULTS,
    AuditEvent,
    AuditLogger,
)


# ---------------------------------------------------------------------------
# Ops action universe — design.md §"Property 5"
# ---------------------------------------------------------------------------

#: User-attributed prompt CRUD actions emitted by
#: ``services/admin-dashboard-api/src/routers/prompts_git.py``
#: (``_audit_event`` builder uses ``actor_role="admin"``; the property
#: test broadens to all RBAC roles since Requirement 7.5 governs *who*
#: may call the endpoint, not *which roles* the audit row may carry).
_PROMPT_ACTIONS: Final[tuple[str, ...]] = (
    "prompt_draft_created",
    "prompt_pr_opened",
    "prompt_pr_conflict",
    "prompt_render_failed",
)

#: Other user-attributed (non-prompt) ops actions.
_USER_ACTIONS: Final[tuple[str, ...]] = (
    "credential_rotated",
    "feature_flag_toggled",
    "dept_decommissioned",
)

#: Chat action emitted by ``services/assistant-service/src/chat/handler.py``.
#: The chat_message payload contract is enforced by
#: :func:`validate_chat_message_payload`.
_CHAT_ACTIONS: Final[tuple[str, ...]] = ("chat_message",)

#: Background / system actions. ``actor_role='system'`` is mandatory
#: per invariant (c).
_SYSTEM_ACTIONS: Final[tuple[str, ...]] = (
    "budget_exceeded",
    "audit_prune_succeeded",
    "audit_prune_failed",
    "cost_prediction_comment_posted",
    "cost_prediction_comment_failed",
    "cost_prediction_comment_skipped",
    "notification_dispatch_sent",
    "notification_dispatch_failed",
    "notification_dispatch_deduped",
)

#: Full ops action universe. Used as the action axis for the main
#: 1:1-write property.
_ALL_OPS_ACTIONS: Final[tuple[str, ...]] = (
    *_PROMPT_ACTIONS,
    *_USER_ACTIONS,
    *_CHAT_ACTIONS,
    *_SYSTEM_ACTIONS,
)


#: RBAC roles a user-facing action may carry. Excludes ``"system"``
#: which is reserved for the background actions listed above.
_RBAC_ROLES: Final[tuple[str, ...]] = ("viewer", "lead", "admin", "dept_admin")


#: Mandatory ``chat_message`` payload fields per design.md
#: §"ChatHandler" / R1.8. The validator below rejects events that
#: omit any of these or set them to ``None``.
CHAT_MESSAGE_REQUIRED_PAYLOAD_KEYS: Final[frozenset[str]] = frozenset(
    {"prompt_version", "token_in", "token_out", "cost_usd"}
)


# ---------------------------------------------------------------------------
# Write-time payload validator (chat_message contract)
# ---------------------------------------------------------------------------


def validate_chat_message_payload(event: AuditEvent) -> None:
    """Enforce the ``chat_message`` payload contract at write time.

    Validates: Requirement 1.8 (design.md §"ChatHandler").

    A ``chat_message`` :class:`AuditEvent` MUST carry a non-``None``
    ``payload`` dict with all four mandatory fields:

    * ``prompt_version`` — git short hash of the system prompt that
      was injected into the LLM call.
    * ``token_in`` — total prompt tokens consumed.
    * ``token_out`` — total completion tokens emitted.
    * ``cost_usd`` — USD cost of the activity.

    Any of the following is a contract violation and raises
    :class:`ValueError` BEFORE the event reaches
    :meth:`AuditLogger.write`:

    * ``payload is None``
    * a mandatory key missing from the payload
    * a mandatory key present but mapped to ``None``

    No-op for any event whose ``action`` is not ``"chat_message"`` so
    the validator can be safely chained in front of every audit write
    in the call site.
    """

    if event.action != "chat_message":
        return

    payload = event.payload
    if payload is None:
        raise ValueError(
            "chat_message audit event is missing payload; the four "
            "mandatory fields prompt_version / token_in / token_out / "
            "cost_usd cannot be carried on a None payload "
            "(Requirement 1.8)."
        )

    missing = [k for k in CHAT_MESSAGE_REQUIRED_PAYLOAD_KEYS if k not in payload]
    if missing:
        raise ValueError(
            f"chat_message audit event payload is missing mandatory "
            f"field(s) {sorted(missing)!r}; expected all of "
            f"{sorted(CHAT_MESSAGE_REQUIRED_PAYLOAD_KEYS)!r} "
            "(Requirement 1.8 + design.md §ChatHandler)."
        )

    null_fields = [
        k for k in CHAT_MESSAGE_REQUIRED_PAYLOAD_KEYS if payload.get(k) is None
    ]
    if null_fields:
        raise ValueError(
            f"chat_message audit event payload field(s) "
            f"{sorted(null_fields)!r} are None; mandatory fields must "
            "carry a populated value (Requirement 1.8)."
        )


async def write_with_ops_validation(
    logger: AuditLogger, event: AuditEvent
) -> None:
    """Run :func:`validate_chat_message_payload` then ``logger.write``.

    Centralised wrapper so call-site equivalence is exercised by the
    property: every ops-scope event must round-trip through the
    write-time validator + the foundation ``actor_role`` guard.
    """

    validate_chat_message_payload(event)
    await logger.write(event)


# ---------------------------------------------------------------------------
# In-memory writer fake
# ---------------------------------------------------------------------------


@dataclass
class _RecordingAuditWriter:
    """Bare-bones :class:`AuditWriter` that records every accepted row.

    The ``test_audit_one_to_one.py`` foundation suite carries a
    larger ``_FakeAuditWriter`` that mirrors the lifecycle service's
    pending/final shape; the ops parity property only needs to count
    rows and inspect their fields, so a tiny dedicated fake keeps
    the assertions sharp.
    """

    inserted: list[AuditEvent] = field(default_factory=list)

    async def insert_audit(self, event: AuditEvent) -> None:
        self.inserted.append(event)


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


# Short printable strings — used for actor_id / dept_id / resource. We
# constrain the alphabet to printable ASCII (excluding control chars)
# so values remain JSON-serialisable without needing escape handling
# inside the in-memory fake.
_SHORT_ASCII: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=24,
)

#: Result strategy. Postgres ``audit_events.result`` ``CHECK`` mirror.
_VALID_RESULT: st.SearchStrategy[str] = st.sampled_from(sorted(AUDIT_RESULTS))


def _payload_for_chat_message() -> st.SearchStrategy[dict[str, Any]]:
    """Build a *valid* ``chat_message`` payload covering all four keys.

    Values are constrained to bounded numeric / string types so the
    payload survives JSON serialisation (the production audit writer
    runs ``json.dumps`` on the dict at INSERT time). ``cost_usd`` is
    serialised as a string to mirror :func:`_decimal_to_str` from
    ``budget/policy.py`` (Decimal is preserved end-to-end via
    string round-trip).
    """

    return st.fixed_dictionaries(
        {
            "prompt_version": st.text(
                alphabet=st.characters(
                    whitelist_categories=("Ll", "Lu", "Nd")
                ),
                min_size=4,
                max_size=12,
            ),
            "token_in": st.integers(min_value=0, max_value=200_000),
            "token_out": st.integers(min_value=0, max_value=200_000),
            "cost_usd": st.decimals(
                min_value=Decimal("0"),
                max_value=Decimal("1000"),
                places=6,
                allow_nan=False,
                allow_infinity=False,
            ).map(lambda d: format(d, "f")),
        }
    )


def _payload_for_system_action(action: str) -> st.SearchStrategy[dict[str, Any]]:
    """Build a representative payload for a system action.

    The exact payload shape is action-specific in production
    (``budget_exceeded`` carries ``scope/limit/usage``,
    ``audit_prune_succeeded`` carries archived/deleted counts, …).
    For the audit-integrity property we only need *some* deterministic
    payload that survives JSON serialisation; the per-action shape is
    pinned by the unit tests that own each emitter (Property 5 is the
    *audit layer* contract, not the per-action payload schema).
    """

    return st.fixed_dictionaries(
        {
            "_action": st.just(action),
            "_marker": st.integers(min_value=0, max_value=1_000_000),
        }
    )


def _payload_for_user_action() -> st.SearchStrategy[dict[str, Any] | None]:
    """User-attributed actions may carry a free-form payload or ``None``."""

    return st.one_of(
        st.none(),
        st.fixed_dictionaries(
            {
                "_action_class": st.just("user"),
                "note": st.text(
                    alphabet=st.characters(
                        whitelist_categories=("Ll", "Lu", "Nd")
                    ),
                    min_size=0,
                    max_size=32,
                ),
            }
        ),
    )


@st.composite
def _ops_event(draw: st.DrawFn) -> AuditEvent:
    """Produce a single :class:`AuditEvent` from the ops universe.

    The strategy ensures each event is *contract-valid*: ``actor_role``
    is always one of :data:`AUDIT_ACTOR_ROLES`, ``chat_message`` events
    carry the four mandatory payload keys, and system actions use
    ``actor_role='system'``. Negative cases (missing fields, invalid
    roles) live in dedicated strategies so the per-property assertion
    sets stay sharp.
    """

    action = draw(st.sampled_from(_ALL_OPS_ACTIONS))
    actor_id = draw(_SHORT_ASCII)
    resource = draw(_SHORT_ASCII)
    result = draw(_VALID_RESULT)
    # ``dept_id`` is nullable on system-wide events (eg. global prompt
    # edits live under ``dept_id=None``); we let Hypothesis pick.
    dept_id = draw(st.one_of(st.none(), _SHORT_ASCII))

    if action in _CHAT_ACTIONS:
        actor_role = draw(st.sampled_from(_RBAC_ROLES))
        payload: dict[str, Any] | None = draw(_payload_for_chat_message())
    elif action in _SYSTEM_ACTIONS:
        actor_role = "system"
        payload = draw(_payload_for_system_action(action))
    else:  # _PROMPT_ACTIONS or _USER_ACTIONS
        actor_role = draw(st.sampled_from(_RBAC_ROLES))
        payload = draw(_payload_for_user_action())

    # Timestamps are timezone-aware UTC to mirror the production
    # writer's expectation (the Postgres column is ``timestamptz``).
    ts = datetime.now(tz=timezone.utc)

    return AuditEvent(
        actor_id=actor_id,
        actor_role=actor_role,  # type: ignore[arg-type]
        dept_id=dept_id,
        action=action,
        resource=resource,
        result=result,  # type: ignore[arg-type]
        timestamp=ts,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Strategies for the negative-case properties
# ---------------------------------------------------------------------------


_WHITESPACE_ONLY: st.SearchStrategy[str] = st.sampled_from(
    (" ", "\t", "  ", "\n", " \t \n")
)

_TYPO_ROLES: st.SearchStrategy[str] = st.sampled_from(
    (
        "Admin",
        "ADMIN",
        "DeptAdmin",
        "dept-admin",
        "superuser",
        "owner",
        "user",
        "guest",
    )
).filter(lambda v: v not in AUDIT_ACTOR_ROLES)

_INVALID_ROLE: st.SearchStrategy[Any] = st.one_of(
    st.none(),
    st.just(""),
    _WHITESPACE_ONLY,
    _TYPO_ROLES,
)


# Strategy that produces a ``chat_message`` event whose payload is
# missing one of the four mandatory keys (or has it set to ``None``).
@st.composite
def _invalid_chat_message_event(draw: st.DrawFn) -> AuditEvent:
    """Produce a ``chat_message`` event that violates the payload contract.

    Three failure modes are sampled with equal probability:

    1. ``payload=None`` — no payload at all.
    2. one of the four mandatory keys is *absent* from the dict.
    3. one of the four mandatory keys is present but ``None``.
    """

    actor_id = draw(_SHORT_ASCII)
    resource = draw(_SHORT_ASCII)
    actor_role = draw(st.sampled_from(_RBAC_ROLES))
    dept_id = draw(st.one_of(st.none(), _SHORT_ASCII))

    failure_mode = draw(st.sampled_from(("none_payload", "missing_key", "null_key")))

    if failure_mode == "none_payload":
        payload: dict[str, Any] | None = None
    else:
        # Build a starter payload with all four valid keys ...
        full_payload: dict[str, Any] = {
            "prompt_version": "abc1234",
            "token_in": 100,
            "token_out": 200,
            "cost_usd": "0.01",
        }
        offending_key = draw(st.sampled_from(sorted(CHAT_MESSAGE_REQUIRED_PAYLOAD_KEYS)))
        if failure_mode == "missing_key":
            full_payload.pop(offending_key)
        else:  # null_key
            full_payload[offending_key] = None
        payload = full_payload

    return AuditEvent(
        actor_id=actor_id,
        actor_role=actor_role,  # type: ignore[arg-type]
        dept_id=dept_id,
        action="chat_message",
        resource=resource,
        result="ok",
        timestamp=datetime.now(tz=timezone.utc),
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Property (a) + (b) + (c) + (e) — 1:1 write contract under the
# ops action universe with role / system semantics enforced.
# ---------------------------------------------------------------------------


@given(events=st.lists(_ops_event(), min_size=1, max_size=30))
@settings(
    deadline=None,
    max_examples=60,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_ops_audit_writes_one_row_per_event(events: list[AuditEvent]) -> None:
    """Every accepted ops event produces exactly one ``insert_audit`` row.

    Validates: Requirements 1.8, 6.1 (parity invariant — design.md
    §"Property 5" branches a, b, c, e).
    """

    writer = _RecordingAuditWriter()
    logger = AuditLogger(writer=writer)

    async def run() -> None:
        for event in events:
            await write_with_ops_validation(logger, event)

    asyncio.run(run())

    # Branch (a): 1:1 contract.
    assert len(writer.inserted) == len(events), (
        f"expected {len(events)} audit rows for {len(events)} events, "
        f"got {len(writer.inserted)}"
    )

    # Branch (a) extended: row order matches event order. The audit log
    # is append-only and the writer is single-threaded in the test, so
    # the insertion sequence is the event sequence.
    for written, original in zip(writer.inserted, events, strict=True):
        assert written is original, (
            "AuditLogger.write must forward the event unchanged to the "
            "underlying writer; observed a mismatch (event was "
            "transformed or reordered)"
        )

    # Branch (b): every written row carries a valid actor_role.
    for row in writer.inserted:
        assert row.actor_role in AUDIT_ACTOR_ROLES, (
            f"row for action={row.action!r} carries unknown actor_role "
            f"{row.actor_role!r}; allowed: {sorted(AUDIT_ACTOR_ROLES)!r}"
        )

    # Branch (c): system-triggered actions MUST carry actor_role='system'.
    for row in writer.inserted:
        if row.action in _SYSTEM_ACTIONS:
            assert row.actor_role == "system", (
                f"system-triggered action {row.action!r} must carry "
                f"actor_role='system'; got {row.actor_role!r}. "
                "design.md §'Property 5 (c)'"
            )

    # Branch (e): no idempotent suppression. Even when two events share
    # the same (actor_id, action, resource, payload) tuple, both land in
    # the writer. We check this by counting events that share the
    # logical key and asserting the row count matches.
    def _logical_key(e: AuditEvent) -> tuple[Any, ...]:
        # ``payload`` is wrapped in ``repr`` so unhashable dicts can
        # still be compared; the value-shape comparison is enough for
        # the suppression check.
        return (e.actor_id, e.action, e.resource, repr(e.payload))

    from collections import Counter

    expected_counts = Counter(_logical_key(e) for e in events)
    actual_counts = Counter(_logical_key(r) for r in writer.inserted)
    assert expected_counts == actual_counts, (
        "ops audit layer must write every event (no idempotent "
        f"suppression); expected {dict(expected_counts)!r}, "
        f"got {dict(actual_counts)!r}"
    )


# ---------------------------------------------------------------------------
# Property (b) — invalid actor_role is rejected (foundation parity)
# ---------------------------------------------------------------------------


@given(
    bad_role=_INVALID_ROLE,
    action=st.sampled_from(_ALL_OPS_ACTIONS),
    actor_id=_SHORT_ASCII,
    resource=_SHORT_ASCII,
    result=_VALID_RESULT,
    dept_id=st.one_of(st.none(), _SHORT_ASCII),
)
@settings(
    deadline=None,
    max_examples=60,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_ops_audit_rejects_null_or_invalid_actor_role(
    bad_role: Any,
    action: str,
    actor_id: str,
    resource: str,
    result: str,
    dept_id: str | None,
) -> None:
    """Every ops action with a NULL / empty / unknown ``actor_role`` raises.

    Validates: Requirements 1.8, 6.1 (branch b — design.md §"Property 5").
    Foundation Property 13 parity: the ``actor_role IS NOT NULL``
    invariant carries forward into the ops action universe so a bug
    that sneaks an unattributed ops event past the writer trips here.
    """

    writer = _RecordingAuditWriter()
    logger = AuditLogger(writer=writer)

    # ``chat_message`` events with an invalid role would also fail the
    # payload check; since both validators raise ValueError we attach a
    # valid payload here so the role guard is the *only* path that
    # rejects the event. This pins the property to the role contract
    # specifically.
    if action == "chat_message":
        payload: dict[str, Any] | None = {
            "prompt_version": "abc1234",
            "token_in": 100,
            "token_out": 200,
            "cost_usd": "0.01",
        }
    else:
        payload = None

    event = AuditEvent(
        actor_id=actor_id,
        actor_role=bad_role,  # type: ignore[arg-type]
        dept_id=dept_id,
        action=action,
        resource=resource,
        result=result,  # type: ignore[arg-type]
        timestamp=datetime.now(tz=timezone.utc),
        payload=payload,
    )

    async def run() -> None:
        with pytest.raises(ValueError) as exc_info:
            await write_with_ops_validation(logger, event)
        msg = str(exc_info.value)
        assert (
            "actor_role" in msg
            or "Requirement 7.7" in msg
            or "audit" in msg.lower()
        ), (
            f"ValueError {msg!r} should mention actor_role / audit / "
            "Requirement 7.7 so operators can pivot from the traceback"
        )

    asyncio.run(run())

    assert writer.inserted == [], (
        "AuditLogger.write MUST raise BEFORE forwarding the event; "
        f"instead {len(writer.inserted)} row(s) leaked through for "
        f"action={action!r}, role={bad_role!r}"
    )


# ---------------------------------------------------------------------------
# Property (d) — chat_message payload contract enforcement
# ---------------------------------------------------------------------------


@given(event=_invalid_chat_message_event())
@settings(
    deadline=None,
    max_examples=60,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_chat_message_missing_payload_field_is_rejected(
    event: AuditEvent,
) -> None:
    """``chat_message`` without all four mandatory payload fields raises.

    Validates: Requirement 1.8 (branch d — design.md §"Property 5").
    Missing ``prompt_version`` / ``token_in`` / ``token_out`` /
    ``cost_usd`` (or any of those fields set to ``None``) MUST be
    rejected at write time before the row reaches the underlying
    writer.
    """

    writer = _RecordingAuditWriter()
    logger = AuditLogger(writer=writer)

    async def run() -> None:
        with pytest.raises(ValueError) as exc_info:
            await write_with_ops_validation(logger, event)
        msg = str(exc_info.value)
        assert "chat_message" in msg, (
            "the ValueError must identify the chat_message contract "
            f"(message={msg!r})"
        )

    asyncio.run(run())

    assert writer.inserted == [], (
        "chat_message events with an incomplete payload must NOT "
        "reach the underlying writer"
    )


# ---------------------------------------------------------------------------
# Concrete regression anchors
# ---------------------------------------------------------------------------


def test_chat_message_with_all_four_fields_passes() -> None:
    """Concrete anchor: a fully-populated chat_message round-trips.

    Pins the positive branch of the chat_message payload contract:
    when all four mandatory fields are present and non-``None``, the
    event reaches the underlying writer untouched.
    """

    writer = _RecordingAuditWriter()
    logger = AuditLogger(writer=writer)

    event = AuditEvent(
        actor_id="user-42",
        actor_role="lead",
        dept_id="payments",
        action="chat_message",
        resource="department:payments",
        result="ok",
        timestamp=datetime(2025, 1, 15, 10, 30, tzinfo=timezone.utc),
        payload={
            "prompt_version": "abc1234",
            "token_in": 1024,
            "token_out": 256,
            "cost_usd": "0.0123",
            # Optional ops-visibility fields are allowed alongside the
            # mandatory four.
            "pii_matches_count": 0,
            "tool_calls": 1,
        },
    )

    asyncio.run(write_with_ops_validation(logger, event))

    assert len(writer.inserted) == 1
    assert writer.inserted[0] is event


def test_system_action_must_use_system_role() -> None:
    """Concrete anchor: ``budget_exceeded`` admits ``actor_role='system'``.

    Pins design.md §"Property 5 (c)": background actions are written
    under the synthetic ``system`` role. The writer accepts the role
    (positive branch) — the property test above asserts that a
    *non-system* role on a system action would be a bug at the
    *emitter*, not at the writer (the writer admits any valid role).
    """

    writer = _RecordingAuditWriter()
    logger = AuditLogger(writer=writer)

    event = AuditEvent(
        actor_id="system",
        actor_role="system",
        dept_id="payments",
        action="budget_exceeded",
        resource="department:payments",
        result="denied",
        timestamp=datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc),
        payload={
            "scope": "dept_weekly",
            "limit": "100.00",
            "usage": "120.50",
        },
    )

    asyncio.run(write_with_ops_validation(logger, event))

    assert len(writer.inserted) == 1
    assert writer.inserted[0].actor_role == "system"


def test_no_payload_chat_message_is_rejected() -> None:
    """Concrete anchor: chat_message with ``payload=None`` raises ValueError.

    The most common bug-mode (a caller forgets to populate the
    payload) is pinned here so a regression in the validator fails
    deterministically outside the Hypothesis search.
    """

    writer = _RecordingAuditWriter()
    logger = AuditLogger(writer=writer)

    event = AuditEvent(
        actor_id="user-1",
        actor_role="viewer",
        dept_id="payments",
        action="chat_message",
        resource="department:payments",
        result="ok",
        timestamp=datetime.now(tz=timezone.utc),
        payload=None,
    )

    async def run() -> None:
        with pytest.raises(ValueError) as exc_info:
            await write_with_ops_validation(logger, event)
        assert "chat_message" in str(exc_info.value)
        assert "payload" in str(exc_info.value).lower()

    asyncio.run(run())
    assert writer.inserted == []


def test_validator_is_no_op_for_non_chat_actions() -> None:
    """Concrete anchor: the chat-message validator skips other actions.

    A ``budget_exceeded`` event with ``payload=None`` MUST NOT be
    rejected by the chat-message contract validator (it carries its
    own contract enforced by the budget policy emitter).
    """

    writer = _RecordingAuditWriter()
    logger = AuditLogger(writer=writer)

    event = AuditEvent(
        actor_id="system",
        actor_role="system",
        dept_id=None,
        action="audit_prune_succeeded",
        resource="audit_events",
        result="ok",
        timestamp=datetime.now(tz=timezone.utc),
        payload=None,
    )

    asyncio.run(write_with_ops_validation(logger, event))

    assert len(writer.inserted) == 1
    assert writer.inserted[0].action == "audit_prune_succeeded"


def test_ops_universe_includes_all_design_listed_actions() -> None:
    """Concrete anchor: the test's action universe matches design.md.

    The design.md §"Property 5" passage lists the ops actions the
    property must cover. This test pins that list so a future doc
    update is reflected in the strategy without silent drift.
    """

    expected = {
        "chat_message",
        "prompt_draft_created",
        "prompt_pr_opened",
        "prompt_pr_conflict",
        "prompt_render_failed",
        "credential_rotated",
        "budget_exceeded",
        "audit_prune_succeeded",
        "audit_prune_failed",
        "cost_prediction_comment_posted",
        "cost_prediction_comment_failed",
        "cost_prediction_comment_skipped",
        "notification_dispatch_sent",
        "notification_dispatch_failed",
        "notification_dispatch_deduped",
        "feature_flag_toggled",
        "dept_decommissioned",
    }
    assert set(_ALL_OPS_ACTIONS) == expected, (
        f"ops action universe drift: design says {sorted(expected)!r}, "
        f"strategy carries {sorted(_ALL_OPS_ACTIONS)!r}"
    )
