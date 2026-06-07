"""Unit tests for the ``audit_logger`` package.

The tests cover three concerns:

1. :class:`AuditEvent` is frozen and matches the design.md schema.
2. :class:`AuditLogger.write` rejects events with a missing /
   empty / unknown ``actor_role`` *before* any DB round-trip.
3. The runtime-mirror constants (:data:`AUDIT_ACTOR_ROLES`,
   :data:`AUDIT_RESULTS`) stay in sync with the ``Literal`` types
   declared on :class:`AuditEvent`.
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import datetime, timezone
from typing import get_args

import pytest

from audit_logger import (
    AUDIT_ACTOR_ROLES,
    AUDIT_RESULTS,
    AuditEvent,
    AuditLogger,
    AuditWriter,
)
from audit_logger.event import AuditResult, AuditRole


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CapturingWriter:
    """In-memory :class:`AuditWriter` used by every test in this module."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def insert_audit(self, event: AuditEvent) -> None:
        self.events.append(event)


def _ok_event(**overrides: object) -> AuditEvent:
    """Build a baseline well-formed :class:`AuditEvent`.

    Tests pass keyword overrides to deviate from the baseline (eg.
    ``actor_role=None`` for the validation tests).
    """

    base: dict[str, object] = {
        "actor_id": "bot.payment.jira",
        "actor_role": "system",
        "dept_id": "payment",
        "action": "capability_denied",
        "resource": "workflow:code_change_with_test",
        "result": "denied",
        "timestamp": datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        "payload": {"missing": ["bitbucket_write"]},
    }
    base.update(overrides)
    # ``AuditEvent`` is frozen so we build it from kwargs.
    return AuditEvent(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AuditEvent dataclass shape
# ---------------------------------------------------------------------------


def test_audit_event_is_frozen_dataclass() -> None:
    """Design.md mandates ``frozen=True`` so audit rows stay append-only."""

    assert is_dataclass(AuditEvent)
    event = _ok_event()
    # ``frozen=True`` forbids attribute mutation; that's the invariant
    # we care about here. (The dataclass itself is not hashable because
    # ``payload`` is a ``dict`` - append-only does not require hashing.)
    with pytest.raises(FrozenInstanceError):
        event.actor_id = "someone-else"  # type: ignore[misc]


def test_audit_event_field_set_matches_design_doc() -> None:
    """The 8 fields from design.md `libs/audit_logger` are all present."""

    expected = {
        "actor_id",
        "actor_role",
        "dept_id",
        "action",
        "resource",
        "result",
        "timestamp",
        "payload",
    }
    actual = {f.name for f in fields(AuditEvent)}
    assert actual == expected, (
        f"AuditEvent fields drifted from design.md schema; "
        f"expected={sorted(expected)!r}, actual={sorted(actual)!r}"
    )


def test_audit_event_role_runtime_set_matches_literal_type() -> None:
    """``AUDIT_ACTOR_ROLES`` is in sync with the ``AuditRole`` Literal."""

    assert AUDIT_ACTOR_ROLES == frozenset(get_args(AuditRole))


def test_audit_event_result_runtime_set_matches_literal_type() -> None:
    """``AUDIT_RESULTS`` is in sync with the ``AuditResult`` Literal."""

    assert AUDIT_RESULTS == frozenset(get_args(AuditResult))


# ---------------------------------------------------------------------------
# AuditLogger.write - mandatory actor_role
# ---------------------------------------------------------------------------


def test_write_persists_well_formed_event() -> None:
    """The happy path delegates to the injected writer."""

    writer = _CapturingWriter()
    logger = AuditLogger(writer=writer)
    event = _ok_event()

    asyncio.run(logger.write(event))

    assert writer.events == [event]


def test_write_rejects_none_actor_role() -> None:
    """``actor_role=None`` raises before any INSERT."""

    writer = _CapturingWriter()
    logger = AuditLogger(writer=writer)
    # We bypass the ``Literal`` annotation by constructing through
    # ``object.__setattr__``-equivalent kwargs; ``Literal`` is a
    # static-only hint so this is legal Python.
    event = _ok_event(actor_role=None)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="actor_role is required"):
        asyncio.run(logger.write(event))

    assert writer.events == [], (
        "writer.insert_audit must NOT be called when validation fails"
    )


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
def test_write_rejects_blank_actor_role(blank: str) -> None:
    """Whitespace-only roles fail the same way ``None`` does."""

    writer = _CapturingWriter()
    logger = AuditLogger(writer=writer)
    event = _ok_event(actor_role=blank)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="non-empty string"):
        asyncio.run(logger.write(event))

    assert writer.events == []


def test_write_rejects_unknown_actor_role() -> None:
    """A typo'd role gets a precise error before the DB round-trip."""

    writer = _CapturingWriter()
    logger = AuditLogger(writer=writer)
    event = _ok_event(actor_role="superuser")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="not one of the allowed roles"):
        asyncio.run(logger.write(event))

    assert writer.events == []


@pytest.mark.parametrize(
    "role", ["viewer", "lead", "admin", "dept_admin", "system"]
)
def test_write_accepts_every_documented_role(role: str) -> None:
    """All 5 design.md roles round-trip through the logger."""

    writer = _CapturingWriter()
    logger = AuditLogger(writer=writer)
    event = _ok_event(actor_role=role)  # type: ignore[arg-type]

    asyncio.run(logger.write(event))

    assert writer.events == [event]


def test_audit_writer_protocol_is_runtime_checkable() -> None:
    """``AuditWriter`` is ``@runtime_checkable`` so test fakes type-check."""

    writer = _CapturingWriter()
    assert isinstance(writer, AuditWriter)
