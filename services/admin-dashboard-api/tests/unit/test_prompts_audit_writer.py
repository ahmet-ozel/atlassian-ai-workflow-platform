"""Unit tests for ``src.prompts.audit_writer`` (the project 6.3).
Exercises the asyncpg-backed audit sink that the PromptsGitRouter
writes through. The tests do not stand up a real Postgres instance -
they inject a fake pool whose ``acquire`` context manager yields a
fake connection that records every ``execute`` call. That keeps the
test suite hermetic while still validating:
* The exact INSERT statement targets ``automation.audit_events`` with
  the expected column ordering (mirrors
  ``infra/postgres/init/10_automation.sql``).
* ``payload`` is JSON-encoded with stable key ordering.
* The application-layer ``actor_role`` invariant from
  :class:`audit_logger.AuditLogger` raises ``ValueError`` *before*
  any SQL round-trip when ``actor_role`` is ``None`` / empty
  .
* Connection-level errors (``OSError``,
  ``ConnectionRefusedError``, ``asyncio.TimeoutError``) are
  swallowed at WARNING - the audit write must never mask the
  underlying request outcome.
* Non-connection errors (eg. CHECK constraint violations) are also
  swallowed but logged at ERROR.
* All four prompt mutation event actions
  (``prompt_draft_created``, ``prompt_pr_opened``,
  ``prompt_render_failed``, ``prompt_pr_conflict``) round-trip
  cleanly - the writer is not action-aware."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# Bootstrap sys.path so the tests can be run via ``pytest`` directly
# from the service root without requiring ``pip install -e``.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for lib_dir in (
    _WORKSPACE_ROOT / "libs" / "audit_logger" / "src",
):
    if lib_dir.is_dir() and str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))


from audit_logger import AuditEvent  # noqa: E402

from src.prompts.audit_writer import (  # noqa: E402
    AsyncpgAuditEventsWriter,
    AsyncpgAuditSink,
    _is_connection_error,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeConnection:
    """Records every ``execute`` call on a single connection."""

    def __init__(self, *, raise_on_execute: BaseException | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._raise = raise_on_execute

    async def execute(self, query: str, *args: Any) -> None:
        self.calls.append((query, args))
        if self._raise is not None:
            raise self._raise


class _FakeAcquireContext:
    """``async with pool.acquire() as conn:`` fake."""

    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


class _FakePool:
    """Asyncpg-pool-shaped fake.

    ``acquire`` returns a single shared connection - the writer
    issues exactly one ``execute`` per ``insert_audit`` call so a
    shared connection is sufficient.
    """

    def __init__(self, *, raise_on_execute: BaseException | None = None) -> None:
        self.connection = _FakeConnection(raise_on_execute=raise_on_execute)

    def acquire(self) -> _FakeAcquireContext:
        return _FakeAcquireContext(self.connection)

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event_factory():
    """Build a well-formed :class:`AuditEvent` with overridable kwargs."""

    def _make(**overrides: Any) -> AuditEvent:
        defaults: dict[str, Any] = {
            "actor_id": "alice",
            "actor_role": "admin",
            "dept_id": None,
            "action": "prompt_draft_created",
            "resource": "prompt:platform/prompts/x.md",
            "result": "ok",
            "timestamp": datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            "payload": {"path": "platform/prompts/x.md", "branch": "draft/alice-1"},
        }
        defaults.update(overrides)
        return AuditEvent(**defaults)

    return _make


# ---------------------------------------------------------------------------
# AsyncpgAuditEventsWriter - happy path
# ---------------------------------------------------------------------------


class TestInsertAuditHappyPath:
    @pytest.mark.asyncio
    async def test_inserts_into_automation_audit_events(self, event_factory) -> None:
        pool = _FakePool()
        writer = AsyncpgAuditEventsWriter(pool=pool)

        event = event_factory()
        await writer.insert_audit(event)

        assert len(pool.connection.calls) == 1
        sql, args = pool.connection.calls[0]
        # Exact target table + column ordering (mirrors 10_automation.sql).
        assert "INSERT INTO automation.audit_events" in sql
        assert "(actor_id, actor_role, dept_id, action, resource, result," in sql
        assert "payload, created_at)" in sql
        # ``$7::jsonb`` cast keeps Postgres happy with the encoded payload.
        assert "$7::jsonb" in sql

        # Argument ordering matches the column order. The payload
        # column is delivered as a JSON string for the cast.
        assert args[0] == "alice"  # actor_id
        assert args[1] == "admin"  # actor_role
        assert args[2] is None  # dept_id
        assert args[3] == "prompt_draft_created"  # action
        assert args[4] == "prompt:platform/prompts/x.md"  # resource
        assert args[5] == "ok"  # result
        # Payload is JSON-encoded with sorted keys (deterministic).
        assert json.loads(args[6]) == {
            "path": "platform/prompts/x.md",
            "branch": "draft/alice-1",
        }
        assert args[7] == event.timestamp

    @pytest.mark.asyncio
    async def test_payload_none_passes_through_as_sql_null(
        self, event_factory
    ) -> None:
        pool = _FakePool()
        writer = AsyncpgAuditEventsWriter(pool=pool)

        await writer.insert_audit(event_factory(payload=None))

        _sql, args = pool.connection.calls[0]
        # NULL is preserved (rather than the literal string ``"null"``)
        # so the JSONB column ends up SQL NULL.
        assert args[6] is None

    @pytest.mark.asyncio
    async def test_payload_with_decimal_and_uuid_round_trips(
        self, event_factory
    ) -> None:
        from decimal import Decimal
        from uuid import UUID

        pool = _FakePool()
        writer = AsyncpgAuditEventsWriter(pool=pool)

        payload = {
            "cost": Decimal("0.0007"),
            "request_id": UUID("12345678-1234-5678-1234-567812345678"),
        }
        await writer.insert_audit(event_factory(payload=payload))

        _sql, args = pool.connection.calls[0]
        # The default ``json.dumps(default=str)`` falls back to
        # str() for non-JSON-native types so the encoded payload is
        # always a string, never a TypeError.
        decoded = json.loads(args[6])
        assert decoded["cost"] == "0.0007"
        assert decoded["request_id"] == "12345678-1234-5678-1234-567812345678"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "action",
        [
            "prompt_draft_created",
            "prompt_pr_opened",
            "prompt_render_failed",
            "prompt_pr_conflict",
        ],
    )
    async def test_all_four_prompt_actions_round_trip(
        self, event_factory, action: str
    ) -> None:
        pool = _FakePool()
        writer = AsyncpgAuditEventsWriter(pool=pool)

        await writer.insert_audit(event_factory(action=action))

        _sql, args = pool.connection.calls[0]
        assert args[3] == action


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_connection_error_is_swallowed_with_warning(
        self, event_factory, caplog: pytest.LogCaptureFixture
    ) -> None:
        pool = _FakePool(raise_on_execute=ConnectionRefusedError("pg down"))
        writer = AsyncpgAuditEventsWriter(pool=pool)

        with caplog.at_level(logging.WARNING):
            # Returns cleanly - must NOT propagate the connection error.
            await writer.insert_audit(event_factory())

        assert any(
            "audit_events insert failed" in rec.message
            and "connection-level" in rec.message
            for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_timeout_error_is_classified_as_connection(
        self, event_factory, caplog: pytest.LogCaptureFixture
    ) -> None:
        pool = _FakePool(raise_on_execute=asyncio.TimeoutError())
        writer = AsyncpgAuditEventsWriter(pool=pool)

        with caplog.at_level(logging.WARNING):
            await writer.insert_audit(event_factory())

        assert any(
            "connection-level" in rec.message for rec in caplog.records
        )

    @pytest.mark.asyncio
    async def test_non_connection_error_is_swallowed_at_error(
        self, event_factory, caplog: pytest.LogCaptureFixture
    ) -> None:
        # A simulated CHECK-constraint violation (any non-connection
        # exception). The writer logs at ERROR but still does not
        # raise - audit failures must not mask request outcome.
        pool = _FakePool(raise_on_execute=ValueError("CHECK violation"))
        writer = AsyncpgAuditEventsWriter(pool=pool)

        with caplog.at_level(logging.ERROR):
            await writer.insert_audit(event_factory())

        assert any(
            "non-connection" in rec.message for rec in caplog.records
        )


class TestConnectionErrorClassifier:
    @pytest.mark.parametrize(
        "exc",
        [
            OSError("broken pipe"),
            ConnectionError(),
            ConnectionRefusedError(),
            asyncio.TimeoutError(),
        ],
    )
    def test_built_in_exceptions_are_connection_errors(
        self, exc: BaseException
    ) -> None:
        assert _is_connection_error(exc) is True

    @pytest.mark.parametrize(
        "name",
        [
            "PostgresConnectionError",
            "ConnectionDoesNotExistError",
            "ConnectionFailureError",
            "CannotConnectNowError",
            "InterfaceError",
        ],
    )
    def test_asyncpg_named_classes_classify_via_class_name(
        self, name: str
    ) -> None:
        # Build a synthetic exception whose class name matches one of
        # asyncpg's connection-level errors; the classifier must
        # recognise it without importing :mod:`asyncpg`.
        cls = type(name, (Exception,), {})
        assert _is_connection_error(cls("x")) is True

    def test_unrelated_exception_is_not_connection_error(self) -> None:
        assert _is_connection_error(ValueError("nope")) is False
        assert _is_connection_error(KeyError("nope")) is False


# ---------------------------------------------------------------------------
# AsyncpgAuditSink - application-layer guard
# ---------------------------------------------------------------------------


class TestAsyncpgAuditSinkGuards:
    @pytest.mark.asyncio
    async def test_actor_role_none_raises_before_sql(
        self, event_factory
    ) -> None:
        """``AuditLogger.write`` rejects None ``actor_role`` upfront.
        This is 's application-layer guard - the
        Postgres CHECK constraint enforces the same at the database
        layer, but the application guard means we never even open a
        connection for a malformed event."""

        pool = _FakePool()
        sink = AsyncpgAuditSink(pool=pool)

        bad_event = event_factory(actor_role=None)  # type: ignore[arg-type]

        with pytest.raises(ValueError):
            await sink.write(bad_event)

        # No SQL was issued - the guard fired before delegation.
        assert pool.connection.calls == []

    @pytest.mark.asyncio
    async def test_actor_role_empty_raises_before_sql(
        self, event_factory
    ) -> None:
        pool = _FakePool()
        sink = AsyncpgAuditSink(pool=pool)

        bad_event = event_factory(actor_role="")  # type: ignore[arg-type]

        with pytest.raises(ValueError):
            await sink.write(bad_event)

        assert pool.connection.calls == []

    @pytest.mark.asyncio
    async def test_valid_event_round_trips_through_sink(
        self, event_factory
    ) -> None:
        pool = _FakePool()
        sink = AsyncpgAuditSink(pool=pool)

        await sink.write(event_factory())

        assert len(pool.connection.calls) == 1
        sql, args = pool.connection.calls[0]
        assert "INSERT INTO automation.audit_events" in sql
        assert args[1] == "admin"
