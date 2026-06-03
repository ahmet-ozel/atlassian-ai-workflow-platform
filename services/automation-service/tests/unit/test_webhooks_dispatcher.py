"""Unit tests for the webhook pipeline Dispatcher stage.

Tests cover:
- Resolve assignee.accountId → dept_id from bot identity cache
- Not in bot identity table → DROP + audit dispatch_not_bot
- dept.mode == disabled → DROP + audit webhook_dept_disabled
- Normal assign/update → workflow start (trace_id generated)
- Cache refresh every 5min + instant query on cache miss
- Unassign event (assignee null) → DROP + audit dispatch_unassigned
- Comment on needs_info issue → Temporal signal info_received
- [iterate] comment → Iteration Manager start
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup — ensure src/ is importable
# ---------------------------------------------------------------------------

_TESTS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _TESTS_DIR.parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# Also add the audit_logger lib
_PLATFORM_ROOT = _TESTS_DIR.parents[3]
_AUDIT_LIB = _PLATFORM_ROOT / "libs" / "audit_logger" / "src"
if str(_AUDIT_LIB) not in sys.path:
    sys.path.insert(0, str(_AUDIT_LIB))

from webhooks.dispatcher import (  # noqa: E402
    WebhookDispatcher,
    DispatchResult,
    DepartmentConfig,
    BotIdentityEntry,
)
from webhooks.loop_guard import WebhookPayload  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeConnection:
    """Fake asyncpg connection for testing."""

    def __init__(
        self,
        bot_rows: list[dict[str, Any]] | None = None,
        dept_rows: list[dict[str, Any]] | None = None,
        work_item_row: dict[str, Any] | None = None,
    ) -> None:
        self._bot_rows = bot_rows or []
        self._dept_rows = dept_rows or []
        self._work_item_row = work_item_row
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.executed.append((query, args))
        if "department_bots" in query:
            return self._bot_rows
        if "departments" in query and "mode" in query:
            return self._dept_rows
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.executed.append((query, args))
        if "department_bots" in query:
            # Cache miss lookup by account_id
            for row in self._bot_rows:
                if row["account_id"] == args[0]:
                    return row
            return None
        if "departments" in query:
            for row in self._dept_rows:
                if row["id"] == args[0]:
                    return row
            return None
        if "work_items" in query:
            return self._work_item_row
        return None

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


class FakeTemporalClient:
    """Fake Temporal client that records calls."""

    def __init__(self) -> None:
        self.started_workflows: list[dict[str, Any]] = []
        self.sent_signals: list[dict[str, Any]] = []

    async def start_workflow(
        self,
        workflow_type: str,
        workflow_id: str,
        *,
        task_queue: str,
        args: Any = (),
        **kwargs: Any,
    ) -> None:
        self.started_workflows.append({
            "workflow_type": workflow_type,
            "workflow_id": workflow_id,
            "task_queue": task_queue,
            "args": args,
        })

    async def signal_workflow(
        self,
        workflow_id: str,
        signal_name: str,
        payload: Any = None,
    ) -> None:
        self.sent_signals.append({
            "workflow_id": workflow_id,
            "signal_name": signal_name,
            "payload": payload,
        })


class FakeAuditLogger:
    """Fake audit logger that records events."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def write(self, event: Any) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bot_rows() -> list[dict[str, Any]]:
    """Default bot identity rows."""
    return [
        {
            "account_id": "bot-account-123",
            "department_id": "payment",
            "service": "jira",
        },
        {
            "account_id": "bot-account-456",
            "department_id": "devops",
            "service": "jira",
        },
    ]


@pytest.fixture
def dept_rows() -> list[dict[str, Any]]:
    """Default department config rows.

    ``config_json`` mirrors ``departments.json``; only the ``approvers``
    field is consumed by the dispatcher today.
    """
    return [
        {
            "id": "payment",
            "mode": "active",
            "config_json": {"approvers": ["approver-user-1", "approver-user-2"]},
        },
        {
            "id": "devops",
            "mode": "disabled",
            "config_json": {"approvers": []},
        },
    ]


@pytest.fixture
def fake_conn(bot_rows, dept_rows) -> FakeConnection:
    return FakeConnection(bot_rows=bot_rows, dept_rows=dept_rows)


@pytest.fixture
def fake_pool(fake_conn) -> FakePool:
    return FakePool(conn=fake_conn)


@pytest.fixture
def fake_temporal() -> FakeTemporalClient:
    return FakeTemporalClient()


@pytest.fixture
def fake_audit() -> FakeAuditLogger:
    return FakeAuditLogger()


@pytest.fixture
def dispatcher(fake_pool, fake_temporal, fake_audit) -> WebhookDispatcher:
    return WebhookDispatcher(
        db=fake_pool,
        temporal=fake_temporal,
        audit_logger=fake_audit,
    )


# ---------------------------------------------------------------------------
# Tests: Unassign event (assignee null) → DROP
# ---------------------------------------------------------------------------


class TestUnassignEvent:
    """Unassign event (assignee null) → DROP + audit dispatch_unassigned."""

    @pytest.mark.asyncio
    async def test_null_assignee_drops(self, dispatcher: WebhookDispatcher) -> None:
        payload = WebhookPayload(
            event_type="issue_updated",
            issue_key="PAY-100",
            assignee_account_id=None,
            actor_account_id="user-abc",
        )
        result = await dispatcher.dispatch(payload)
        assert result.action == "drop"
        assert result.reason == "dispatch_unassigned"

    @pytest.mark.asyncio
    async def test_null_assignee_audit_logged(
        self, dispatcher: WebhookDispatcher, fake_audit: FakeAuditLogger
    ) -> None:
        payload = WebhookPayload(
            event_type="issue_updated",
            issue_key="PAY-101",
            assignee_account_id=None,
        )
        await dispatcher.dispatch(payload)
        assert len(fake_audit.events) == 1
        assert fake_audit.events[0].action == "dispatch_unassigned"


# ---------------------------------------------------------------------------
# Tests: Not in bot identity table → DROP
# ---------------------------------------------------------------------------


class TestNotBotAssignee:
    """Assignee not in bot identity table → DROP + audit dispatch_not_bot."""

    @pytest.mark.asyncio
    async def test_unknown_assignee_drops(self, dispatcher: WebhookDispatcher) -> None:
        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="PAY-200",
            assignee_account_id="unknown-user-999",
            actor_account_id="user-abc",
        )
        result = await dispatcher.dispatch(payload)
        assert result.action == "drop"
        assert result.reason == "not_bot"

    @pytest.mark.asyncio
    async def test_unknown_assignee_audit_logged(
        self, dispatcher: WebhookDispatcher, fake_audit: FakeAuditLogger
    ) -> None:
        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="PAY-201",
            assignee_account_id="unknown-user-999",
        )
        await dispatcher.dispatch(payload)
        assert len(fake_audit.events) == 1
        assert fake_audit.events[0].action == "dispatch_not_bot"


# ---------------------------------------------------------------------------
# Tests: Department mode disabled → DROP
# ---------------------------------------------------------------------------


class TestDeptDisabled:
    """dept.mode == disabled → DROP + audit webhook_dept_disabled."""

    @pytest.mark.asyncio
    async def test_disabled_dept_drops(self, dispatcher: WebhookDispatcher) -> None:
        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="DEV-300",
            assignee_account_id="bot-account-456",  # devops dept (disabled)
            actor_account_id="user-abc",
        )
        result = await dispatcher.dispatch(payload)
        assert result.action == "drop"
        assert result.reason == "dept_disabled"
        assert result.dept_id == "devops"

    @pytest.mark.asyncio
    async def test_disabled_dept_audit_logged(
        self, dispatcher: WebhookDispatcher, fake_audit: FakeAuditLogger
    ) -> None:
        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="DEV-301",
            assignee_account_id="bot-account-456",
        )
        await dispatcher.dispatch(payload)
        assert len(fake_audit.events) == 1
        assert fake_audit.events[0].action == "webhook_dept_disabled"


# ---------------------------------------------------------------------------
# Tests: Normal assign/update → workflow start
# ---------------------------------------------------------------------------


class TestWorkflowStart:
    """Normal assign/update → workflow start with trace_id."""

    @pytest.mark.asyncio
    async def test_active_dept_starts_workflow(
        self, dispatcher: WebhookDispatcher, fake_temporal: FakeTemporalClient
    ) -> None:
        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="PAY-400",
            assignee_account_id="bot-account-123",  # payment dept (active)
            actor_account_id="user-abc",
        )
        result = await dispatcher.dispatch(payload)
        assert result.action == "workflow_started"
        assert result.trace_id is not None
        assert result.dept_id == "payment"
        assert len(fake_temporal.started_workflows) == 1
        wf = fake_temporal.started_workflows[0]
        assert wf["workflow_type"] == "AutomationWorkflow"
        assert wf["workflow_id"] == "automation-jira-PAY-400"

    @pytest.mark.asyncio
    async def test_preserves_existing_trace_id(
        self, dispatcher: WebhookDispatcher
    ) -> None:
        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="PAY-401",
            assignee_account_id="bot-account-123",
            trace_id="existing-trace-id-xyz",
        )
        result = await dispatcher.dispatch(payload)
        assert result.trace_id == "existing-trace-id-xyz"

    @pytest.mark.asyncio
    async def test_generates_trace_id_when_missing(
        self, dispatcher: WebhookDispatcher
    ) -> None:
        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="PAY-402",
            assignee_account_id="bot-account-123",
            trace_id=None,
        )
        result = await dispatcher.dispatch(payload)
        assert result.trace_id is not None
        assert len(result.trace_id) > 0


# ---------------------------------------------------------------------------
# Tests: Cache refresh + cache miss
# ---------------------------------------------------------------------------


class TestCacheRefresh:
    """Cache refresh every 5min + instant query on cache miss."""

    @pytest.mark.asyncio
    async def test_cache_miss_queries_db(
        self, dispatcher: WebhookDispatcher
    ) -> None:
        """On cache miss, the dispatcher queries DB directly."""
        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="PAY-500",
            assignee_account_id="bot-account-123",
        )
        # First call triggers cache refresh + dispatch
        result = await dispatcher.dispatch(payload)
        assert result.action == "workflow_started"

    @pytest.mark.asyncio
    async def test_cache_populated_after_refresh(
        self, dispatcher: WebhookDispatcher
    ) -> None:
        """After refresh, cache contains bot identity entries."""
        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="PAY-501",
            assignee_account_id="bot-account-123",
        )
        await dispatcher.dispatch(payload)
        assert "bot-account-123" in dispatcher.bot_identity_cache
        assert dispatcher.bot_identity_cache["bot-account-123"].department_id == "payment"

    @pytest.mark.asyncio
    async def test_cache_invalidation_forces_refresh(
        self, dispatcher: WebhookDispatcher
    ) -> None:
        """invalidate_cache() forces next dispatch to refresh."""
        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="PAY-502",
            assignee_account_id="bot-account-123",
        )
        await dispatcher.dispatch(payload)
        dispatcher.invalidate_cache()
        assert dispatcher._last_cache_refresh == 0.0


# ---------------------------------------------------------------------------
# Tests: Comment on needs_info issue → signal
# ---------------------------------------------------------------------------


class TestNeedsInfoSignal:
    """Comment on needs_info issue → Temporal signal info_received."""

    @pytest.mark.asyncio
    async def test_comment_on_needs_info_signals(
        self, fake_temporal: FakeTemporalClient, fake_audit: FakeAuditLogger
    ) -> None:
        """Comment on needs_info issue sends info_received signal."""
        conn = FakeConnection(
            bot_rows=[{"account_id": "bot-123", "department_id": "pay", "service": "jira"}],
            dept_rows=[{"id": "pay", "mode": "active", "config_json": {}}],
            work_item_row={"status": "needs_info"},
        )
        pool = FakePool(conn=conn)
        dispatcher = WebhookDispatcher(
            db=pool, temporal=fake_temporal, audit_logger=fake_audit
        )

        payload = WebhookPayload(
            event_type="comment_created",
            issue_key="PAY-600",
            assignee_account_id="bot-123",
            comment_body="Here is the repo: github.com/org/repo",
        )
        result = await dispatcher.dispatch(payload)
        assert result.action == "signaled"
        assert len(fake_temporal.sent_signals) == 1
        signal = fake_temporal.sent_signals[0]
        assert signal["signal_name"] == "info_received"
        assert signal["payload"] == "Here is the repo: github.com/org/repo"

    @pytest.mark.asyncio
    async def test_comment_on_non_needs_info_starts_workflow(
        self, dispatcher: WebhookDispatcher, fake_temporal: FakeTemporalClient
    ) -> None:
        """Comment on non-needs_info issue starts normal workflow."""
        payload = WebhookPayload(
            event_type="comment_created",
            issue_key="PAY-601",
            assignee_account_id="bot-account-123",
            comment_body="Just a regular comment",
        )
        result = await dispatcher.dispatch(payload)
        # Not needs_info and not [iterate] → workflow start
        assert result.action == "workflow_started"


# ---------------------------------------------------------------------------
# Tests: [iterate] comment → Iteration Manager
# ---------------------------------------------------------------------------


class TestIterateCommand:
    """[iterate] comment → Iteration Manager start with auth check."""

    @pytest.mark.asyncio
    async def test_iterate_comment_starts_iteration(
        self, dispatcher: WebhookDispatcher, fake_temporal: FakeTemporalClient
    ) -> None:
        payload = WebhookPayload(
            event_type="comment_created",
            issue_key="PAY-700",
            assignee_account_id="bot-account-123",
            actor_account_id="approver-user-1",  # in approvers
            comment_body="[iterate] add retry with exponential backoff",
        )
        result = await dispatcher.dispatch(payload)
        assert result.action == "iteration_started"
        assert result.dept_id == "payment"
        assert len(fake_temporal.started_workflows) == 1
        wf = fake_temporal.started_workflows[0]
        assert wf["workflow_type"] == "IterationWorkflow"
        assert "iteration-PAY-700" in wf["workflow_id"]
        assert wf["args"][0]["extra_instructions"] == "add retry with exponential backoff"

    @pytest.mark.asyncio
    async def test_iterate_case_insensitive(
        self, dispatcher: WebhookDispatcher, fake_temporal: FakeTemporalClient
    ) -> None:
        payload = WebhookPayload(
            event_type="comment_created",
            issue_key="PAY-701",
            assignee_account_id="bot-account-123",
            actor_account_id="approver-user-2",
            comment_body="[ITERATE] fix the tests",
        )
        result = await dispatcher.dispatch(payload)
        assert result.action == "iteration_started"

    @pytest.mark.asyncio
    async def test_iterate_without_extra_instructions(
        self, dispatcher: WebhookDispatcher, fake_temporal: FakeTemporalClient
    ) -> None:
        payload = WebhookPayload(
            event_type="comment_created",
            issue_key="PAY-702",
            assignee_account_id="bot-account-123",
            actor_account_id="approver-user-1",
            comment_body="[iterate]",
        )
        result = await dispatcher.dispatch(payload)
        assert result.action == "iteration_started"
        wf = fake_temporal.started_workflows[0]
        assert wf["args"][0]["extra_instructions"] is None

    @pytest.mark.asyncio
    async def test_iterate_by_reporter_starts_iteration(
        self, dispatcher: WebhookDispatcher, fake_temporal: FakeTemporalClient
    ) -> None:
        """Issue reporter is also authorized to iterate."""
        payload = WebhookPayload(
            event_type="comment_created",
            issue_key="PAY-703",
            assignee_account_id="bot-account-123",
            actor_account_id="reporter-user-x",
            reporter_account_id="reporter-user-x",  # actor == reporter
            comment_body="[iterate] please add tests",
        )
        result = await dispatcher.dispatch(payload)
        assert result.action == "iteration_started"
        assert len(fake_temporal.started_workflows) == 1

    @pytest.mark.asyncio
    async def test_iterate_unauthorized_actor_drops(
        self,
        dispatcher: WebhookDispatcher,
        fake_temporal: FakeTemporalClient,
        fake_audit: FakeAuditLogger,
    ) -> None:
        """Non-approver, non-reporter actor → drop + audit."""
        payload = WebhookPayload(
            event_type="comment_created",
            issue_key="PAY-704",
            assignee_account_id="bot-account-123",
            actor_account_id="random-user-zzz",  # not in approvers
            reporter_account_id="reporter-user-x",  # different from actor
            comment_body="[iterate] try again",
        )
        result = await dispatcher.dispatch(payload)
        assert result.action == "drop"
        assert result.reason == "iteration_unauthorized"
        assert result.dept_id == "payment"
        # No iteration workflow should have been started.
        assert all(
            wf["workflow_type"] != "IterationWorkflow"
            for wf in fake_temporal.started_workflows
        )
        # An audit event should record the rejection.
        assert any(
            event.action == "dispatch_iteration_unauthorized"
            for event in fake_audit.events
        )

    @pytest.mark.asyncio
    async def test_iterate_with_no_actor_drops(
        self, dispatcher: WebhookDispatcher, fake_temporal: FakeTemporalClient
    ) -> None:
        """Missing actor_account_id cannot pass authorization."""
        payload = WebhookPayload(
            event_type="comment_created",
            issue_key="PAY-705",
            assignee_account_id="bot-account-123",
            actor_account_id=None,
            reporter_account_id="reporter-user-x",
            comment_body="[iterate] anonymous attempt",
        )
        result = await dispatcher.dispatch(payload)
        assert result.action == "drop"
        assert result.reason == "iteration_unauthorized"


# ---------------------------------------------------------------------------
# Tests: Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_db_failure_on_resolve_returns_not_bot(
        self, fake_temporal: FakeTemporalClient, fake_audit: FakeAuditLogger
    ) -> None:
        """DB failure during resolve → treated as not_bot (graceful degradation)."""

        class FailingPool:
            def acquire(self):
                return FailingContext()

        class FailingContext:
            async def __aenter__(self):
                raise RuntimeError("DB connection failed")

            async def __aexit__(self, *args):
                pass

        dispatcher = WebhookDispatcher(
            db=FailingPool(),
            temporal=fake_temporal,
            audit_logger=fake_audit,
        )
        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="PAY-800",
            assignee_account_id="bot-account-123",
        )
        result = await dispatcher.dispatch(payload)
        # Cache refresh fails, cache miss also fails → not_bot
        assert result.action == "drop"
        assert result.reason == "not_bot"

    @pytest.mark.asyncio
    async def test_no_audit_logger_uses_logging(
        self, fake_pool: FakePool, fake_temporal: FakeTemporalClient
    ) -> None:
        """Without audit logger, falls back to structured logging."""
        dispatcher = WebhookDispatcher(
            db=fake_pool,
            temporal=fake_temporal,
            audit_logger=None,
        )
        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="PAY-801",
            assignee_account_id=None,
        )
        # Should not raise
        result = await dispatcher.dispatch(payload)
        assert result.action == "drop"


# ---------------------------------------------------------------------------
# Tests: _extract_approvers helper
# ---------------------------------------------------------------------------


class TestExtractApprovers:
    """Robust extraction of approvers from departments.config_json."""

    def test_dict_with_string_list(self) -> None:
        from webhooks.dispatcher import _extract_approvers

        result = _extract_approvers({"approvers": ["alice", "bob"]})
        assert result == ("alice", "bob")

    def test_json_string_payload(self) -> None:
        """asyncpg may return jsonb as already-decoded dict, but some
        test fakes pass the raw JSON string — both must work."""
        from webhooks.dispatcher import _extract_approvers

        result = _extract_approvers('{"approvers": ["carol"]}')
        assert result == ("carol",)

    def test_none_returns_empty(self) -> None:
        from webhooks.dispatcher import _extract_approvers

        assert _extract_approvers(None) == ()

    def test_missing_key_returns_empty(self) -> None:
        from webhooks.dispatcher import _extract_approvers

        assert _extract_approvers({"other_key": ["x"]}) == ()

    def test_invalid_list_entries_filtered(self) -> None:
        """Non-string and empty entries are dropped silently."""
        from webhooks.dispatcher import _extract_approvers

        result = _extract_approvers(
            {"approvers": ["alice", "", None, 42, "bob"]}
        )
        assert result == ("alice", "bob")

    def test_non_list_value_returns_empty(self) -> None:
        from webhooks.dispatcher import _extract_approvers

        assert _extract_approvers({"approvers": "alice"}) == ()

    def test_invalid_json_string_returns_empty(self) -> None:
        from webhooks.dispatcher import _extract_approvers

        assert _extract_approvers("{not valid json") == ()


# ---------------------------------------------------------------------------
# Tests: Per-dept concurrency cap enforcement
# ---------------------------------------------------------------------------


class _RecordingJiraCommenter:
    """Records ``post_comment`` calls so the test can assert on them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def post_comment(
        self, dept_id: str, issue_key: str, body: str
    ) -> None:
        self.calls.append((dept_id, issue_key, body))


class _CountingTemporal(FakeTemporalClient):
    """:class:`FakeTemporalClient` extended with ``count_workflows``.

    The dispatcher's concurrency gate calls ``count_workflows`` on
    the same client object it uses for ``start_workflow`` /
    ``signal_workflow``; this stub returns a configured count so the
    Visibility primary path is exercised.
    """

    def __init__(self, *, visibility_count: int) -> None:
        super().__init__()
        self._visibility_count = visibility_count
        self.visibility_queries: list[str | None] = []

    async def count_workflows(self, query: str | None = None) -> Any:
        self.visibility_queries.append(query)

        class _Result:
            def __init__(self, count: int) -> None:
                self.count = count

        return _Result(self._visibility_count)


class TestConcurrencyCapEnforcement:
    """Per-dept concurrency limit gate."""

    @pytest.mark.asyncio
    async def test_no_cap_starts_workflow(
        self,
        bot_rows: list[dict[str, Any]],
        fake_audit: FakeAuditLogger,
    ) -> None:
        """``max_concurrent_workflows`` absent → no gate, normal start."""
        dept_rows = [
            {
                "id": "payment",
                "mode": "active",
                "config_json": {"approvers": []},  # no max_concurrent
            }
        ]
        conn = FakeConnection(bot_rows=bot_rows, dept_rows=dept_rows)
        pool = FakePool(conn=conn)
        temporal = _CountingTemporal(visibility_count=999)  # would exceed
        dispatcher = WebhookDispatcher(
            db=pool, temporal=temporal, audit_logger=fake_audit
        )

        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="PAY-900",
            assignee_account_id="bot-account-123",
        )
        result = await dispatcher.dispatch(payload)

        assert result.action == "workflow_started"
        assert len(temporal.started_workflows) == 1

    @pytest.mark.asyncio
    async def test_under_cap_starts_workflow(
        self,
        bot_rows: list[dict[str, Any]],
        fake_audit: FakeAuditLogger,
    ) -> None:
        """``current < max`` → workflow starts normally."""
        dept_rows = [
            {
                "id": "payment",
                "mode": "active",
                "config_json": {
                    "approvers": [],
                    "max_concurrent_workflows": 5,
                },
            }
        ]
        conn = FakeConnection(bot_rows=bot_rows, dept_rows=dept_rows)
        pool = FakePool(conn=conn)
        temporal = _CountingTemporal(visibility_count=2)  # under 5
        dispatcher = WebhookDispatcher(
            db=pool, temporal=temporal, audit_logger=fake_audit
        )

        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="PAY-901",
            assignee_account_id="bot-account-123",
        )
        result = await dispatcher.dispatch(payload)

        assert result.action == "workflow_started"
        assert len(temporal.started_workflows) == 1

    @pytest.mark.asyncio
    async def test_at_cap_drops_with_audit_and_comment(
        self,
        bot_rows: list[dict[str, Any]],
        fake_audit: FakeAuditLogger,
    ) -> None:
        """``current >= max`` → drop + audit + Jira comment."""
        dept_rows = [
            {
                "id": "payment",
                "mode": "active",
                "config_json": {
                    "approvers": [],
                    "max_concurrent_workflows": 3,
                },
            }
        ]
        conn = FakeConnection(bot_rows=bot_rows, dept_rows=dept_rows)
        pool = FakePool(conn=conn)
        temporal = _CountingTemporal(visibility_count=3)  # at cap
        commenter = _RecordingJiraCommenter()
        dispatcher = WebhookDispatcher(
            db=pool,
            temporal=temporal,
            audit_logger=fake_audit,
            jira_commenter=commenter,
        )

        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="PAY-902",
            assignee_account_id="bot-account-123",
        )
        result = await dispatcher.dispatch(payload)

        # Workflow must NOT have started.
        assert result.action == "drop"
        assert result.reason == "concurrency_limit_exceeded"
        assert len(temporal.started_workflows) == 0

        # Audit row written.
        actions = [e.action for e in fake_audit.events]
        assert "dispatch_concurrency_rejected" in actions

        # Jira comment posted.
        assert len(commenter.calls) == 1
        dept_id, issue_key, body = commenter.calls[0]
        assert dept_id == "payment"
        assert issue_key == "PAY-902"
        assert "limit" in body.lower() or "limit" in body

    @pytest.mark.asyncio
    async def test_rejection_works_without_commenter(
        self,
        bot_rows: list[dict[str, Any]],
        fake_audit: FakeAuditLogger,
    ) -> None:
        """Missing ``jira_commenter`` does not break the gate — audit
        still fires and the workflow is still rejected."""
        dept_rows = [
            {
                "id": "payment",
                "mode": "active",
                "config_json": {
                    "approvers": [],
                    "max_concurrent_workflows": 1,
                },
            }
        ]
        conn = FakeConnection(bot_rows=bot_rows, dept_rows=dept_rows)
        pool = FakePool(conn=conn)
        temporal = _CountingTemporal(visibility_count=5)  # over cap
        dispatcher = WebhookDispatcher(
            db=pool,
            temporal=temporal,
            audit_logger=fake_audit,
            jira_commenter=None,
        )

        payload = WebhookPayload(
            event_type="issue_assigned",
            issue_key="PAY-903",
            assignee_account_id="bot-account-123",
        )
        result = await dispatcher.dispatch(payload)
        assert result.action == "drop"
        assert result.reason == "concurrency_limit_exceeded"
        assert len(temporal.started_workflows) == 0
