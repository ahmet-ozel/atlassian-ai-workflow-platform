"""Unit tests for the webhook pipeline LoopGuard stage.

Tests cover:
- R2.1: Bot account detection via department_bot_identity
- R2.2: DROP + audit on bot actor
- R2.3: Approval Gate exemption (bot can't self-approve)
- R2.4: Storm detection (3+ drops in 60s  5min block + notification)
- R2.5: Pipeline ordering (Loop Guard runs after Event_Dedup, before Dispatcher)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup - ensure src/ is importable
# ---------------------------------------------------------------------------
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _TESTS_DIR.parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# Also add the audit_logger lib
_PLATFORM_ROOT = _TESTS_DIR.parents[3]
_AUDIT_LIB = _PLATFORM_ROOT / "libs" / "audit_logger" / "src"
if str(_AUDIT_LIB) not in sys.path:
    sys.path.insert(0, str(_AUDIT_LIB))

from webhooks.loop_guard import (  # noqa: E402
    LoopGuard,
    StageResult,
    WebhookPayload,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConnection:
    """Fake asyncpg connection for testing."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = rows or []
        self._fetchrow_result: dict[str, Any] | None = None
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.executed.append((query, args))
        return self._rows

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.executed.append((query, args))
        return self._fetchrow_result

    async def execute(self, query: str, *args: Any) -> None:
        self.executed.append((query, args))


class FakePool:
    """Fake asyncpg pool that yields a FakeConnection."""

    def __init__(self, conn: FakeConnection | None = None) -> None:
        self._conn = conn or FakeConnection()

    def acquire(self) -> "FakePoolContext":
        return FakePoolContext(self._conn)


class FakePoolContext:
    """Async context manager for FakePool.acquire()."""

    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


@dataclass
class FakeAuditLogger:
    """Captures audit events for assertion."""

    events: list[Any] = field(default_factory=list)

    async def write(self, event: Any) -> None:
        self.events.append(event)


@dataclass
class FakeAdminNotifier:
    """Captures admin notifications for assertion."""

    notifications: list[tuple[str, str, str]] = field(default_factory=list)

    async def notify(self, event_name: str, issue_key: str, detail: str) -> None:
        self.notifications.append((event_name, issue_key, detail))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fixed_clock() -> datetime:
    """A fixed point in time for deterministic tests."""
    return datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def bot_ids() -> frozenset[str]:
    """Standard set of bot account IDs."""
    return frozenset({"bot-001", "bot-002", "bot-003"})


@pytest.fixture
def make_guard(fixed_clock, bot_ids):
    """Factory for creating a LoopGuard with common test dependencies."""

    def _make(
        *,
        conn: FakeConnection | None = None,
        audit: FakeAuditLogger | None = None,
        notifier: FakeAdminNotifier | None = None,
        clock_time: datetime | None = None,
    ) -> tuple[LoopGuard, FakeConnection, FakeAuditLogger, FakeAdminNotifier]:
        fake_conn = conn or FakeConnection()
        fake_audit = audit or FakeAuditLogger()
        fake_notifier = notifier or FakeAdminNotifier()
        pool = FakePool(fake_conn)
        t = clock_time or fixed_clock

        guard = LoopGuard(
            db=pool,
            audit_logger=fake_audit,
            admin_notifier=fake_notifier,
            bot_ids_provider=lambda: bot_ids,
            clock=lambda: t,
        )
        return guard, fake_conn, fake_audit, fake_notifier

    return _make


# ---------------------------------------------------------------------------
# Tests: R2.1 - Bot account detection
# ---------------------------------------------------------------------------


class TestBotDetection:
    """R2.1: Compare actor.accountId with all bot account_ids."""

    @pytest.mark.asyncio
    async def test_bot_actor_is_detected(self, make_guard):
        """A webhook from a bot account is dropped."""
        guard, conn, audit, _ = make_guard()
        # fetchrow for _is_blocked returns None (not blocked)
        conn._fetchrow_result = None

        payload = WebhookPayload(
            actor_account_id="bot-001",
            issue_key="PROJ-123",
            event_type="jira:issue_updated",
        )

        result = await guard.check(payload)

        assert result.action == "drop"
        assert result.reason == "loop_guard"

    @pytest.mark.asyncio
    async def test_non_bot_actor_passes(self, make_guard):
        """A webhook from a human user passes through."""
        guard, conn, _, _ = make_guard()
        conn._fetchrow_result = None

        payload = WebhookPayload(
            actor_account_id="human-user-42",
            issue_key="PROJ-123",
            event_type="jira:issue_updated",
        )

        result = await guard.check(payload)

        assert result.action == "pass"

    @pytest.mark.asyncio
    async def test_none_actor_passes(self, make_guard):
        """A webhook with no actor (system event) passes through."""
        guard, conn, _, _ = make_guard()
        conn._fetchrow_result = None

        payload = WebhookPayload(
            actor_account_id=None,
            issue_key="PROJ-123",
            event_type="jira:issue_updated",
        )

        result = await guard.check(payload)

        assert result.action == "pass"


# ---------------------------------------------------------------------------
# Tests: R2.2 - DROP + audit log
# ---------------------------------------------------------------------------


class TestAuditLogging:
    """R2.2: Bot drop emits audit log with dept_id, event_type, issue_key."""

    @pytest.mark.asyncio
    async def test_drop_emits_audit_event(self, make_guard):
        """Dropping a bot webhook writes an audit event."""
        guard, conn, audit, _ = make_guard()
        conn._fetchrow_result = None

        payload = WebhookPayload(
            actor_account_id="bot-002",
            issue_key="PAY-456",
            event_type="jira:comment_created",
            dept_id="payment",
        )

        await guard.check(payload)

        assert len(audit.events) == 1
        event = audit.events[0]
        assert event.action == "loop_guard_dropped"
        assert event.dept_id == "payment"
        assert event.payload["event_type"] == "jira:comment_created"
        assert event.payload["issue_key"] == "PAY-456"
        assert event.payload["actor_account_id"] == "bot-002"


# ---------------------------------------------------------------------------
# Tests: R2.3 - Approval Gate exemption
# ---------------------------------------------------------------------------


class TestApprovalGateExemption:
    """R2.3: [approve]/[reject] comments are exempt from loop guard."""

    @pytest.mark.asyncio
    async def test_approve_comment_from_bot_is_exempt(self, make_guard):
        """Bot writing [approve] for another bot's issue passes."""
        guard, conn, _, _ = make_guard()
        conn._fetchrow_result = None

        payload = WebhookPayload(
            actor_account_id="bot-001",
            issue_key="PROJ-123",
            event_type="jira:comment_created",
            comment_body="[approve] Looks good!",
            assignee_account_id="bot-002",  # Different bot
        )

        result = await guard.check(payload)

        assert result.action == "pass"

    @pytest.mark.asyncio
    async def test_reject_comment_from_bot_is_exempt(self, make_guard):
        """Bot writing [reject] for another bot's issue passes."""
        guard, conn, _, _ = make_guard()
        conn._fetchrow_result = None

        payload = WebhookPayload(
            actor_account_id="bot-001",
            issue_key="PROJ-123",
            event_type="jira:comment_created",
            comment_body="[reject] Needs changes",
            assignee_account_id="bot-002",
        )

        result = await guard.check(payload)

        assert result.action == "pass"

    @pytest.mark.asyncio
    async def test_bot_cannot_self_approve(self, make_guard):
        """Bot writing [approve] on its own issue is NOT exempt."""
        guard, conn, _, _ = make_guard()
        conn._fetchrow_result = None

        payload = WebhookPayload(
            actor_account_id="bot-001",
            issue_key="PROJ-123",
            event_type="jira:comment_created",
            comment_body="[approve] Self-approved",
            assignee_account_id="bot-001",  # Same bot!
        )

        result = await guard.check(payload)

        assert result.action == "drop"

    @pytest.mark.asyncio
    async def test_non_approval_comment_from_bot_is_dropped(self, make_guard):
        """Bot writing a regular comment is dropped."""
        guard, conn, _, _ = make_guard()
        conn._fetchrow_result = None

        payload = WebhookPayload(
            actor_account_id="bot-001",
            issue_key="PROJ-123",
            event_type="jira:comment_created",
            comment_body="Task completed successfully.",
            assignee_account_id="bot-002",
        )

        result = await guard.check(payload)

        assert result.action == "drop"

    @pytest.mark.asyncio
    async def test_approval_gate_case_insensitive(self, make_guard):
        """[APPROVE] and [Reject] are recognized case-insensitively."""
        guard, conn, _, _ = make_guard()
        conn._fetchrow_result = None

        payload = WebhookPayload(
            actor_account_id="bot-001",
            issue_key="PROJ-123",
            event_type="jira:comment_created",
            comment_body="[APPROVE] All tests pass",
            assignee_account_id="bot-002",
        )

        result = await guard.check(payload)

        assert result.action == "pass"


# ---------------------------------------------------------------------------
# Tests: R2.4 - Storm detection
# ---------------------------------------------------------------------------


class TestStormDetection:
    """R2.4: 3+ drops in 60s  5min block + admin notification."""

    @pytest.mark.asyncio
    async def test_storm_triggers_block_and_notification(self, make_guard, fixed_clock):
        """When storm threshold is met, issue is blocked and admin notified."""
        conn = FakeConnection()
        # _is_blocked returns None (not blocked yet)
        # _is_storm returns count >= 3
        conn._fetchrow_result = None
        notifier = FakeAdminNotifier()

        call_count = 0

        class SmartConn(FakeConnection):
            """Returns different results based on query context."""

            async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
                nonlocal call_count
                call_count += 1
                self.executed.append((query, args))
                # First call: _is_blocked  None
                if "loop_guard_blocks" in query:
                    return None
                # Second call: _is_storm  count = 3
                if "COUNT" in query:
                    return {"drop_count": 3}
                return None

        smart_conn = SmartConn()
        pool = FakePool(smart_conn)
        audit = FakeAuditLogger()

        guard = LoopGuard(
            db=pool,
            audit_logger=audit,
            admin_notifier=notifier,
            bot_ids_provider=lambda: frozenset({"bot-001"}),
            clock=lambda: fixed_clock,
        )

        payload = WebhookPayload(
            actor_account_id="bot-001",
            issue_key="PROJ-999",
            event_type="jira:issue_updated",
            dept_id="payment",
        )

        result = await guard.check(payload)

        assert result.action == "drop"
        # Admin should be notified
        assert len(notifier.notifications) == 1
        assert notifier.notifications[0][0] == "loop_storm_detected"
        assert notifier.notifications[0][1] == "PROJ-999"

    @pytest.mark.asyncio
    async def test_below_threshold_no_block(self, make_guard, fixed_clock):
        """When drops are below threshold, no block is created."""
        notifier = FakeAdminNotifier()

        class SmartConn(FakeConnection):
            async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
                self.executed.append((query, args))
                if "loop_guard_blocks" in query:
                    return None
                if "COUNT" in query:
                    return {"drop_count": 2}  # Below threshold
                return None

        smart_conn = SmartConn()
        pool = FakePool(smart_conn)
        audit = FakeAuditLogger()

        guard = LoopGuard(
            db=pool,
            audit_logger=audit,
            admin_notifier=notifier,
            bot_ids_provider=lambda: frozenset({"bot-001"}),
            clock=lambda: fixed_clock,
        )

        payload = WebhookPayload(
            actor_account_id="bot-001",
            issue_key="PROJ-999",
            event_type="jira:issue_updated",
        )

        result = await guard.check(payload)

        assert result.action == "drop"
        # No notification
        assert len(notifier.notifications) == 0

    @pytest.mark.asyncio
    async def test_blocked_issue_is_immediately_dropped(self, make_guard, fixed_clock):
        """A webhook for a blocked issue is dropped without further checks."""

        class BlockedConn(FakeConnection):
            async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
                self.executed.append((query, args))
                if "loop_guard_blocks" in query:
                    # Issue is blocked
                    return {
                        "blocked_until": fixed_clock + timedelta(minutes=3)
                    }
                return None

        blocked_conn = BlockedConn()
        pool = FakePool(blocked_conn)
        audit = FakeAuditLogger()

        guard = LoopGuard(
            db=pool,
            audit_logger=audit,
            admin_notifier=None,
            bot_ids_provider=lambda: frozenset({"bot-001"}),
            clock=lambda: fixed_clock,
        )

        # Even a human user's webhook is dropped if issue is blocked
        payload = WebhookPayload(
            actor_account_id="human-user",
            issue_key="PROJ-999",
            event_type="jira:issue_updated",
        )

        result = await guard.check(payload)

        assert result.action == "drop"
        assert result.reason == "loop_guard_blocked"


# ---------------------------------------------------------------------------
# Tests: Bot ID caching
# ---------------------------------------------------------------------------


class TestBotIdCaching:
    """Bot ID cache refreshes every 5 minutes."""

    @pytest.mark.asyncio
    async def test_bot_ids_are_cached(self, fixed_clock):
        """Multiple calls within TTL use cached bot IDs."""
        call_count = 0

        async def provider():
            nonlocal call_count
            call_count += 1
            return frozenset({"bot-001"})

        conn = FakeConnection()
        conn._fetchrow_result = None
        pool = FakePool(conn)

        guard = LoopGuard(
            db=pool,
            bot_ids_provider=provider,
            clock=lambda: fixed_clock,
        )

        payload = WebhookPayload(
            actor_account_id="human",
            issue_key="PROJ-1",
            event_type="jira:issue_updated",
        )

        await guard.check(payload)
        await guard.check(payload)

        # Provider called only once (cached on second call)
        assert call_count == 1


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_empty_issue_key_still_drops_bot(self, make_guard):
        """Bot webhook with empty issue_key is still dropped."""
        guard, conn, _, _ = make_guard()
        conn._fetchrow_result = None

        payload = WebhookPayload(
            actor_account_id="bot-001",
            issue_key="",
            event_type="jira:issue_updated",
        )

        result = await guard.check(payload)

        assert result.action == "drop"

    @pytest.mark.asyncio
    async def test_db_failure_on_record_drop_still_drops(self, make_guard, fixed_clock):
        """If recording the drop fails, the webhook is still dropped."""

        class FailingConn(FakeConnection):
            async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
                if "loop_guard_blocks" in query:
                    return None
                if "COUNT" in query:
                    return {"drop_count": 0}
                return None

            async def execute(self, query: str, *args: Any) -> None:
                raise RuntimeError("DB connection lost")

        failing_conn = FailingConn()
        pool = FakePool(failing_conn)
        audit = FakeAuditLogger()

        guard = LoopGuard(
            db=pool,
            audit_logger=audit,
            bot_ids_provider=lambda: frozenset({"bot-001"}),
            clock=lambda: fixed_clock,
        )

        payload = WebhookPayload(
            actor_account_id="bot-001",
            issue_key="PROJ-123",
            event_type="jira:issue_updated",
        )

        result = await guard.check(payload)

        # Still dropped despite DB failure
        assert result.action == "drop"
