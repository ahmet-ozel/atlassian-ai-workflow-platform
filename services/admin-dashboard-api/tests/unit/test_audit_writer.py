"""Unit tests for ``src.lifecycle.audit_writer`` (task 5.4).

These tests exercise :class:`AuditWriter` against a hand-rolled fake
:class:`asyncpg.Pool` so we can verify the contract without spinning up
PostgreSQL:

* :meth:`AuditWriter.precheck` issues ``SELECT 1`` and propagates
  connection errors as :class:`AuditUnreachableError` (Requirement 11.6).
* :meth:`AuditWriter.write` builds the full INSERT argument tuple in
  column order and surfaces connection errors as
  :class:`AuditUnreachableError`.
* :meth:`AuditWriter.write_with_retry` returns ``deferred=False`` on
  success and ``deferred=True`` (queueing the entry) on connection
  failure (Requirement 11.7).
* :func:`details_with_env_keys` produces an env-key-only payload, never
  embeds Env_Override values, and refuses ``extra={"env_keys": ...}``
  (Property P6 / Requirement 11.3).
* The deferred-queue drainer retries entries that previously failed
  once the database becomes reachable again.

The tests use the standard library only (``asyncio``, ``unittest``-
style patterns are avoided in favour of the project's pytest +
``asyncio.run`` convention seen in ``test_require_admin.py``).
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

# ``admin-dashboard-api`` ships its source under ``src/``; add the
# service root to ``sys.path`` so ``import src.lifecycle.audit_writer``
# resolves under direct ``pytest tests/unit`` invocations (mirrors the
# bootstrap in ``test_env_parser.py`` / ``test_manifest.py`` /
# ``test_require_admin.py``).
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.audit_writer import (  # noqa: E402
    AuditEntry,
    AuditUnreachableError,
    AuditWriteOutcome,
    AuditWriter,
    details_with_env_keys,
)


# ---------------------------------------------------------------------------
# Fake asyncpg pool
# ---------------------------------------------------------------------------


class _FakeConnectionError(Exception):
    """Stand-in for asyncpg's PostgresConnectionError.

    Named so :func:`_is_connection_error` classifies it as a
    connection-level failure (it matches by class name).
    """


# Rename the class on the fly so :func:`_is_connection_error` recognises it
# via ``type(exc).__name__`` lookup.
_FakeConnectionError.__name__ = "PostgresConnectionError"


class _FakeConn:
    """Fake asyncpg connection that records every ``execute`` call.

    Configurable to raise either a connection error (which the writer
    must classify as ``AuditUnreachableError``) or a generic exception
    (which must propagate verbatim).
    """

    def __init__(
        self,
        *,
        log: list[tuple[str, tuple[Any, ...]]],
        raise_on_execute: BaseException | None = None,
    ) -> None:
        self._log = log
        self._raise = raise_on_execute

    async def execute(self, sql: str, *args: Any) -> str:
        if self._raise is not None:
            # Failed execute: do NOT record the call in the log so
            # tests can count *successful* writes by inspecting the
            # log length directly.
            raise self._raise
        self._log.append((sql, args))
        return "INSERT 0 1"


class _FakeAcquireCtx:
    """Async context manager returned by :meth:`_FakePool.acquire`."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class _FakePool:
    """Fake :class:`asyncpg.Pool` covering the writer's surface area.

    ``execute_log`` records every ``(sql, args)`` tuple that flowed
    through ``conn.execute`` so tests can assert column ordering and
    payload shape. ``raise_on_execute`` toggles the failure mode for
    the *next* call (consumed once, then reset to ``None``) so a single
    pool can be used to exercise both failure-then-success scenarios.
    """

    def __init__(self) -> None:
        self.execute_log: list[tuple[str, tuple[Any, ...]]] = []
        self.raise_on_execute: BaseException | None = None
        self.closed = False
        # When True, ``raise_on_execute`` is NOT consumed one-shot — every
        # acquired connection keeps raising. Used by the precheck
        # connection-error tests to simulate a genuinely-down DB where the
        # writer's one-shot pool-reset retry must also fail (→ 502).
        self.persist_failure = False

    def acquire(self) -> _FakeAcquireCtx:
        conn = _FakeConn(
            log=self.execute_log,
            raise_on_execute=self.raise_on_execute,
        )
        # One-shot: clear the failure trigger after handing it to the
        # connection so the *next* acquire returns a healthy conn — unless
        # ``persist_failure`` keeps the failure mode latched.
        if not self.persist_failure:
            self.raise_on_execute = None
        return _FakeAcquireCtx(conn)

    async def close(self) -> None:
        self.closed = True


def _make_pool_factory(pool: _FakePool):
    async def _factory(*, dsn: str, **kwargs: Any) -> _FakePool:  # noqa: ARG001
        return pool

    return _factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    *,
    action: str = "start",
    outcome: str = "pending",
    details_json: dict[str, Any] | None = None,
    correlation_id: UUID | None = None,
) -> AuditEntry:
    return AuditEntry(
        id=uuid4(),
        actor="ops-1",
        actor_type="admin_dashboard_user",
        service_name="automation-service",
        action=action,  # type: ignore[arg-type]
        timestamp=datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        correlation_id=correlation_id or uuid4(),
        outcome=outcome,  # type: ignore[arg-type]
        details_json=details_json or {"env_keys": ["VAULT_TOKEN", "API_KEY"]},
    )


async def _build_writer(
    pool: _FakePool,
    *,
    queue: asyncio.Queue[AuditEntry] | None = None,
    retry_initial_delay: float = 0.001,
    retry_max_delay: float = 0.01,
) -> tuple[AuditWriter, asyncio.Queue[AuditEntry]]:
    q: asyncio.Queue[AuditEntry] = queue or asyncio.Queue()
    writer = AuditWriter(
        dsn="postgresql://test",
        deferred_queue=q,
        pool_factory=_make_pool_factory(pool),
        retry_initial_delay=retry_initial_delay,
        retry_max_delay=retry_max_delay,
    )
    await writer.start()
    return writer, q


# ---------------------------------------------------------------------------
# details_with_env_keys (Property P6 / Requirement 11.3)
# ---------------------------------------------------------------------------


def test_details_with_env_keys_lists_keys_only() -> None:
    payload = details_with_env_keys(["VAULT_TOKEN", "API_KEY"])

    assert payload == {"env_keys": ["VAULT_TOKEN", "API_KEY"]}
    # Make sure no value-shaped string accidentally leaked in.
    serialised = json.dumps(payload)
    assert "secret-value" not in serialised


def test_details_with_env_keys_preserves_order() -> None:
    payload = details_with_env_keys(["B", "A", "C"])
    assert payload["env_keys"] == ["B", "A", "C"]


def test_details_with_env_keys_accepts_extra_metadata() -> None:
    payload = details_with_env_keys(
        ["X_TOKEN"],
        extra={"reason": "compose_failed", "exit_code": 1},
    )

    assert payload == {
        "env_keys": ["X_TOKEN"],
        "reason": "compose_failed",
        "exit_code": 1,
    }


def test_details_with_env_keys_rejects_clobber_of_env_keys() -> None:
    with pytest.raises(ValueError, match="env_keys"):
        details_with_env_keys(
            ["X_TOKEN"],
            extra={"env_keys": ["should-not-be-here"]},
        )


def test_details_with_env_keys_accepts_tuple_input() -> None:
    payload = details_with_env_keys(("ALPHA_KEY", "BETA_KEY"))
    assert payload == {"env_keys": ["ALPHA_KEY", "BETA_KEY"]}
    assert isinstance(payload["env_keys"], list)


def test_details_with_env_keys_with_empty_iterable() -> None:
    payload = details_with_env_keys([])
    assert payload == {"env_keys": []}


# ---------------------------------------------------------------------------
# precheck() — Requirement 11.6
# ---------------------------------------------------------------------------


def test_precheck_issues_select_one_on_healthy_pool() -> None:
    async def run() -> None:
        pool = _FakePool()
        writer, _ = await _build_writer(pool)
        try:
            await writer.precheck()

            assert len(pool.execute_log) == 1
            sql, args = pool.execute_log[0]
            assert sql == "SELECT 1"
            assert args == ()
        finally:
            await writer.close()

    asyncio.run(run())


def test_precheck_raises_audit_unreachable_on_connection_error() -> None:
    async def run() -> None:
        pool = _FakePool()
        # Persistent failure: the writer's one-shot pool-reset retry must
        # also hit the connection error before declaring the DB
        # unreachable (a genuinely-down DB → 502).
        pool.persist_failure = True
        pool.raise_on_execute = _FakeConnectionError("connection refused")
        writer, _ = await _build_writer(pool)
        try:
            with pytest.raises(AuditUnreachableError, match="precheck failed"):
                await writer.precheck()
        finally:
            await writer.close()

    asyncio.run(run())


def test_precheck_raises_audit_unreachable_on_oserror() -> None:
    async def run() -> None:
        pool = _FakePool()
        pool.persist_failure = True
        pool.raise_on_execute = OSError("network down")
        writer, _ = await _build_writer(pool)
        try:
            with pytest.raises(AuditUnreachableError):
                await writer.precheck()
        finally:
            await writer.close()

    asyncio.run(run())


def test_precheck_self_heals_stale_pool_via_reset() -> None:
    """A half-open pooled connection recovers on the one-shot pool reset.

    The first ``acquire`` raises a connection-level error (stale/idle TCP
    socket); the writer recreates the pool from the factory and retries.
    The one-shot failure trigger is cleared after the first acquire, so
    the retry succeeds and ``precheck`` returns without raising.
    """

    async def run() -> None:
        pool = _FakePool()
        pool.raise_on_execute = _FakeConnectionError("connection lost")
        writer, _ = await _build_writer(pool)
        try:
            await writer.precheck()
            # The retry's successful ``SELECT 1`` is recorded in the log.
            assert ("SELECT 1", ()) in pool.execute_log
        finally:
            await writer.close()

    asyncio.run(run())


def test_precheck_propagates_non_connection_errors_verbatim() -> None:
    """A ``ValueError`` from the driver must NOT be classified as connection-level."""

    async def run() -> None:
        pool = _FakePool()
        pool.raise_on_execute = ValueError("malformed query")
        writer, _ = await _build_writer(pool)
        try:
            with pytest.raises(ValueError, match="malformed query"):
                await writer.precheck()
        finally:
            await writer.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# write() — Requirement 11.1, 11.2, 11.6
# ---------------------------------------------------------------------------


def test_write_inserts_full_column_tuple() -> None:
    async def run() -> None:
        pool = _FakePool()
        writer, _ = await _build_writer(pool)
        entry = _make_entry()
        try:
            await writer.write(entry)

            assert len(pool.execute_log) == 1
            sql, args = pool.execute_log[0]

            # Column order matches the DDL exactly.
            assert "INSERT INTO shared.audit_log" in sql
            assert (
                "id, actor, actor_type, service_name, action, timestamp,"
                in sql
            )
            assert "correlation_id, outcome, details_json" in sql

            # Nine bind parameters, in the correct order and types.
            assert len(args) == 9
            assert args[0] == entry.id
            assert args[1] == entry.actor
            assert args[2] == entry.actor_type
            assert args[3] == entry.service_name
            assert args[4] == entry.action
            assert args[5] == entry.timestamp
            assert args[6] == entry.correlation_id
            assert args[7] == entry.outcome
            # details_json is serialised as a JSON string for the
            # ``::jsonb`` cast in the SQL.
            assert isinstance(args[8], str)
            assert json.loads(args[8]) == entry.details_json
        finally:
            await writer.close()

    asyncio.run(run())


def test_write_raises_audit_unreachable_on_connection_error() -> None:
    async def run() -> None:
        pool = _FakePool()
        pool.raise_on_execute = _FakeConnectionError("kaboom")
        writer, _ = await _build_writer(pool)
        try:
            with pytest.raises(AuditUnreachableError, match="write failed"):
                await writer.write(_make_entry())
        finally:
            await writer.close()

    asyncio.run(run())


def test_write_does_not_serialise_env_override_values() -> None:
    """Property P6 spot-check: only env *keys* end up in the serialised payload."""

    async def run() -> None:
        pool = _FakePool()
        writer, _ = await _build_writer(pool)
        entry = _make_entry(
            details_json=details_with_env_keys(["VAULT_TOKEN", "DB_PASSWORD"]),
        )
        try:
            await writer.write(entry)
            _, args = pool.execute_log[0]
            payload = args[8]
            assert isinstance(payload, str)
            # The keys appear...
            assert "VAULT_TOKEN" in payload
            assert "DB_PASSWORD" in payload
            # ...but no synthetic value-like strings exist on the entry.
            decoded = json.loads(payload)
            assert decoded == {"env_keys": ["VAULT_TOKEN", "DB_PASSWORD"]}
        finally:
            await writer.close()

    asyncio.run(run())


def test_write_before_start_raises_runtime_error() -> None:
    async def run() -> None:
        writer = AuditWriter(
            dsn="postgresql://test",
            deferred_queue=asyncio.Queue(),
            pool_factory=_make_pool_factory(_FakePool()),
        )
        # Note: ``start()`` is intentionally NOT called.
        with pytest.raises(RuntimeError, match="start"):
            await writer.write(_make_entry())

    asyncio.run(run())


# ---------------------------------------------------------------------------
# write_with_retry() — Requirement 11.7
# ---------------------------------------------------------------------------


def test_write_with_retry_returns_not_deferred_on_success() -> None:
    async def run() -> None:
        pool = _FakePool()
        writer, queue = await _build_writer(pool)
        try:
            outcome = await writer.write_with_retry(_make_entry())

            assert isinstance(outcome, AuditWriteOutcome)
            assert outcome.deferred is False
            # Nothing got queued because the write succeeded.
            assert queue.empty()
        finally:
            await writer.close()

    asyncio.run(run())


def test_write_with_retry_defers_on_connection_error() -> None:
    async def run() -> None:
        pool = _FakePool()
        # First write attempt fails with a connection error; the
        # subsequent drainer retry will also fail because the pool's
        # one-shot trigger has been consumed and the drainer's retry
        # loop runs in the same event loop. We close the writer
        # immediately to avoid the drainer racing with the assertion.
        pool.raise_on_execute = _FakeConnectionError("connection refused")
        # Use a long retry so the drainer doesn't retry before we close.
        writer, queue = await _build_writer(
            pool,
            retry_initial_delay=10.0,
            retry_max_delay=10.0,
        )
        try:
            entry = _make_entry()
            outcome = await writer.write_with_retry(entry)

            assert outcome.deferred is True
            # Entry is on the deferred queue.
            queued = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert queued is entry
            queue.task_done()
        finally:
            await writer.close()

    asyncio.run(run())


def test_write_with_retry_propagates_non_connection_errors() -> None:
    """A non-connection error must NOT be silently deferred."""

    async def run() -> None:
        pool = _FakePool()
        pool.raise_on_execute = ValueError("bad bind value")
        writer, _ = await _build_writer(pool)
        try:
            with pytest.raises(ValueError, match="bad bind"):
                await writer.write_with_retry(_make_entry())
        finally:
            await writer.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Deferred queue drainer
# ---------------------------------------------------------------------------


def test_drainer_processes_queued_entry_when_db_recovers() -> None:
    """End-to-end: queue an entry, then let the drainer write it.

    Sequence:
      1. Pool is healthy, so no failure trigger.
      2. We push an entry directly onto the queue (simulating a prior
         deferred write).
      3. The background drainer pops it and writes it.
      4. We observe the INSERT in the pool's execute_log.
    """

    async def run() -> None:
        pool = _FakePool()
        writer, queue = await _build_writer(pool)
        try:
            entry = _make_entry()
            await queue.put(entry)

            # Wait for the drainer to process the queued entry.
            await asyncio.wait_for(queue.join(), timeout=1.0)

            # The drainer issued exactly one INSERT.
            inserts = [
                row
                for row in pool.execute_log
                if "INSERT INTO shared.audit_log" in row[0]
            ]
            assert len(inserts) == 1
            _, args = inserts[0]
            assert args[0] == entry.id
        finally:
            await writer.close()

    asyncio.run(run())


def test_drainer_retries_until_db_recovers() -> None:
    """Drainer keeps retrying (with backoff) while the DB is unreachable."""

    async def run() -> None:
        pool = _FakePool()
        # Fail the FIRST drainer attempt only; subsequent acquire() calls
        # see ``raise_on_execute=None`` because the trigger is one-shot.
        pool.raise_on_execute = _FakeConnectionError("temporary outage")

        writer, queue = await _build_writer(
            pool,
            retry_initial_delay=0.005,
            retry_max_delay=0.01,
        )
        try:
            entry = _make_entry()
            await queue.put(entry)

            # The drainer hits the connection error, re-queues the entry,
            # sleeps briefly, then succeeds on the second pass.
            await asyncio.wait_for(queue.join(), timeout=2.0)

            inserts = [
                row
                for row in pool.execute_log
                if "INSERT INTO shared.audit_log" in row[0]
            ]
            assert len(inserts) == 1
            _, args = inserts[0]
            assert args[0] == entry.id
        finally:
            await writer.close()

    asyncio.run(run())


def test_drainer_drops_entries_with_non_connection_errors_and_keeps_running() -> None:
    """A bind-time error must not freeze the drainer for subsequent entries."""

    async def run() -> None:
        pool = _FakePool()
        # First entry will hit a non-connection error; second will succeed.
        pool.raise_on_execute = ValueError("bad bind on first entry")

        writer, queue = await _build_writer(
            pool,
            retry_initial_delay=0.005,
            retry_max_delay=0.01,
        )
        try:
            bad_entry = _make_entry()
            good_entry = _make_entry()
            await queue.put(bad_entry)
            await queue.put(good_entry)

            await asyncio.wait_for(queue.join(), timeout=2.0)

            inserts = [
                row
                for row in pool.execute_log
                if "INSERT INTO shared.audit_log" in row[0]
            ]
            # The bad entry was dropped; the good entry was inserted.
            assert len(inserts) == 1
            _, args = inserts[0]
            assert args[0] == good_entry.id
        finally:
            await writer.close()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# close() and idempotency
# ---------------------------------------------------------------------------


def test_close_is_idempotent_and_closes_pool() -> None:
    async def run() -> None:
        pool = _FakePool()
        writer, _ = await _build_writer(pool)

        await writer.close()
        assert pool.closed is True

        # Second close is a no-op.
        await writer.close()

    asyncio.run(run())


def test_start_after_close_raises() -> None:
    async def run() -> None:
        pool = _FakePool()
        writer, _ = await _build_writer(pool)
        await writer.close()

        with pytest.raises(RuntimeError, match="closed"):
            await writer.start()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# AuditEntry immutability
# ---------------------------------------------------------------------------


def test_audit_entry_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    entry = _make_entry()
    with pytest.raises(FrozenInstanceError):
        entry.actor = "someone-else"  # type: ignore[misc]


def test_audit_write_outcome_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    outcome = AuditWriteOutcome(deferred=True)
    with pytest.raises(FrozenInstanceError):
        outcome.deferred = False  # type: ignore[misc]
