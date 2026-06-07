"""Unit tests for Approval Gate signal forwarding in the dispatcher.

When a Jira comment carries ``[approve]`` or ``[reject]`` the
dispatcher forwards an ``approval_received`` signal to the running
:class:`ApprovalGateWorkflow` child so it can resume (or reject)
instead of timing out at 24h. Coverage:

* ``[approve]`` → signal forwarded with parsed user/decision payload.
* ``[reject]`` → same forwarding path.
* Plain comment → no signal sent.
* Case-insensitive matching: ``[APPROVE]`` / ``[Reject]``.
* Best-effort: a Temporal failure produces an
  ``approval_signal_forwarding_failed`` audit row but does not break
  dispatch.
* The forward branch sits between needs_info and normal workflow
  start - needs_info still wins when both fire.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup - mirror test_webhooks_dispatcher.py so imports resolve
# regardless of pytest's cwd.
# ---------------------------------------------------------------------------

_TESTS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _TESTS_DIR.parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

_PLATFORM_ROOT = _TESTS_DIR.parents[3]
_AUDIT_LIB = _PLATFORM_ROOT / "libs" / "audit_logger" / "src"
if str(_AUDIT_LIB) not in sys.path:
    sys.path.insert(0, str(_AUDIT_LIB))

from webhooks.dispatcher import WebhookDispatcher  # noqa: E402
from webhooks.loop_guard import WebhookPayload  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes (kept self-contained so this file can run in isolation)
# ---------------------------------------------------------------------------


class FakeConnection:
    """Asyncpg-like connection that returns canned rows by query shape."""

    def __init__(
        self,
        bot_rows: list[dict[str, Any]] | None = None,
        dept_rows: list[dict[str, Any]] | None = None,
        work_item_row: dict[str, Any] | None = None,
    ) -> None:
        self._bot_rows = bot_rows or []
        self._dept_rows = dept_rows or []
        self._work_item_row = work_item_row

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "department_bots" in query:
            return self._bot_rows
        if "departments" in query and "mode" in query:
            return self._dept_rows
        return []

    async def fetchrow(
        self, query: str, *args: Any
    ) -> dict[str, Any] | None:
        if "department_bots" in query:
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
        return None


class FakePool:
    def __init__(self, conn: FakeConnection | None = None) -> None:
        self._conn = conn or FakeConnection()

    def acquire(self) -> "FakePoolContext":
        return FakePoolContext(self._conn)


class FakePoolContext:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


class FakeTemporalClient:
    """Records ``signal_workflow`` and ``start_workflow`` calls."""

    def __init__(
        self,
        *,
        signal_raises: BaseException | None = None,
    ) -> None:
        self.started_workflows: list[dict[str, Any]] = []
        self.sent_signals: list[dict[str, Any]] = []
        self._signal_raises = signal_raises

    async def start_workflow(
        self,
        workflow_type: str,
        workflow_id: str,
        *,
        task_queue: str,
        args: Any = (),
        **kwargs: Any,
    ) -> None:
        self.started_workflows.append(
            {
                "workflow_type": workflow_type,
                "workflow_id": workflow_id,
                "task_queue": task_queue,
                "args": args,
            }
        )

    async def signal_workflow(
        self,
        workflow_id: str,
        signal_name: str,
        payload: Any = None,
    ) -> None:
        if self._signal_raises is not None:
            raise self._signal_raises
        self.sent_signals.append(
            {
                "workflow_id": workflow_id,
                "signal_name": signal_name,
                "payload": payload,
            }
        )


class FakeAuditLogger:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def write(self, event: Any) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bot_rows() -> list[dict[str, Any]]:
    return [
        {
            "account_id": "bot-pay-1",
            "department_id": "payment",
            "service": "jira",
        },
    ]


@pytest.fixture
def dept_rows() -> list[dict[str, Any]]:
    return [
        {
            "id": "payment",
            "mode": "active",
            "config_json": {"approvers": ["po-account-1"]},
        }
    ]


@pytest.fixture
def fake_pool(bot_rows, dept_rows) -> FakePool:
    return FakePool(
        conn=FakeConnection(bot_rows=bot_rows, dept_rows=dept_rows)
    )


@pytest.fixture
def fake_temporal() -> FakeTemporalClient:
    return FakeTemporalClient()


@pytest.fixture
def fake_audit() -> FakeAuditLogger:
    return FakeAuditLogger()


@pytest.fixture
def dispatcher(
    fake_pool: FakePool,
    fake_temporal: FakeTemporalClient,
    fake_audit: FakeAuditLogger,
) -> WebhookDispatcher:
    return WebhookDispatcher(
        db=fake_pool,
        temporal=fake_temporal,
        audit_logger=fake_audit,
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


_ISSUE_KEY = "PAY-123"
_EXPECTED_CHILD_ID = (
    "ApprovalGateWorkflow-automation-jira-PAY-123-PAY-123"
)


def _comment_payload(body: str, *, actor: str = "po-account-1") -> WebhookPayload:
    """Build a comment-shaped payload for the dispatcher tests."""
    return WebhookPayload(
        event_type="comment_created",
        issue_key=_ISSUE_KEY,
        assignee_account_id="bot-pay-1",
        actor_account_id=actor,
        comment_body=body,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestApprovalSignalForwarding:
    """``[approve]``/``[reject]`` comments forward an approval_received signal."""

    @pytest.mark.asyncio
    async def test_approve_comment_forwards_signal(
        self,
        dispatcher: WebhookDispatcher,
        fake_temporal: FakeTemporalClient,
    ) -> None:
        result = await dispatcher.dispatch(_comment_payload("[approve]"))

        assert result.action == "approval_forwarded"
        assert result.dept_id == "payment"
        assert len(fake_temporal.sent_signals) == 1
        signal = fake_temporal.sent_signals[0]
        assert signal["workflow_id"] == _EXPECTED_CHILD_ID
        assert signal["signal_name"] == "approval_received"
        assert signal["payload"] == {
            "user_id": "po-account-1",
            "decision": "[approve]",
        }
        # No new AutomationWorkflow should have been started.
        assert all(
            wf["workflow_type"] != "AutomationWorkflow"
            for wf in fake_temporal.started_workflows
        )

    @pytest.mark.asyncio
    async def test_reject_comment_forwards_signal(
        self,
        dispatcher: WebhookDispatcher,
        fake_temporal: FakeTemporalClient,
    ) -> None:
        result = await dispatcher.dispatch(
            _comment_payload("[reject] not aligned with the brief")
        )

        assert result.action == "approval_forwarded"
        assert len(fake_temporal.sent_signals) == 1
        signal = fake_temporal.sent_signals[0]
        assert signal["workflow_id"] == _EXPECTED_CHILD_ID
        assert signal["signal_name"] == "approval_received"
        assert signal["payload"]["user_id"] == "po-account-1"
        assert signal["payload"]["decision"].startswith("[reject]")

    @pytest.mark.asyncio
    async def test_audit_row_records_decision(
        self,
        dispatcher: WebhookDispatcher,
        fake_audit: FakeAuditLogger,
    ) -> None:
        await dispatcher.dispatch(_comment_payload("[approve] LGTM"))

        forwarded = [
            e for e in fake_audit.events
            if e.action == "approval_signal_forwarded"
        ]
        assert len(forwarded) == 1
        evt = forwarded[0]
        assert evt.dept_id == "payment"
        assert evt.payload.get("decision") == "approve"
        assert evt.payload.get("issue_key") == _ISSUE_KEY
        assert evt.payload.get("actor_account_id") == "po-account-1"
        # Forwarding success → result must be "ok".
        assert evt.result == "ok"

    @pytest.mark.asyncio
    async def test_plain_comment_is_not_forwarded(
        self,
        dispatcher: WebhookDispatcher,
        fake_temporal: FakeTemporalClient,
    ) -> None:
        result = await dispatcher.dispatch(
            _comment_payload("just a normal comment with no markers")
        )

        # Plain comments fall through to the normal workflow start.
        assert result.action == "workflow_started"
        # No approval signal was emitted.
        approval_signals = [
            s for s in fake_temporal.sent_signals
            if s["signal_name"] == "approval_received"
        ]
        assert approval_signals == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        ["[APPROVE]", "[Reject]", "Looks good - [Approve] please"],
    )
    async def test_case_insensitive_match(
        self,
        dispatcher: WebhookDispatcher,
        fake_temporal: FakeTemporalClient,
        body: str,
    ) -> None:
        result = await dispatcher.dispatch(_comment_payload(body))

        assert result.action == "approval_forwarded"
        assert len(fake_temporal.sent_signals) == 1
        signal = fake_temporal.sent_signals[0]
        assert signal["workflow_id"] == _EXPECTED_CHILD_ID
        assert signal["signal_name"] == "approval_received"
        assert signal["payload"]["decision"] == body

    @pytest.mark.asyncio
    async def test_signal_failure_audited_but_does_not_raise(
        self,
        bot_rows: list[dict[str, Any]],
        dept_rows: list[dict[str, Any]],
        fake_audit: FakeAuditLogger,
    ) -> None:
        """Best-effort: temporal RPC failure → audit row, no exception."""
        pool = FakePool(
            conn=FakeConnection(bot_rows=bot_rows, dept_rows=dept_rows)
        )
        temporal = FakeTemporalClient(
            signal_raises=RuntimeError("workflow not found"),
        )
        dispatcher = WebhookDispatcher(
            db=pool, temporal=temporal, audit_logger=fake_audit
        )

        result = await dispatcher.dispatch(_comment_payload("[approve]"))

        # Dispatch flow stays whole - the action still resolves to
        # ``approval_forwarded`` so the webhook layer returns 200.
        assert result.action == "approval_forwarded"
        # Failure audit row written.
        actions = [e.action for e in fake_audit.events]
        assert "approval_signal_forwarding_failed" in actions
        # No success row.
        assert "approval_signal_forwarded" not in actions

    @pytest.mark.asyncio
    async def test_needs_info_wins_over_approval(
        self,
        bot_rows: list[dict[str, Any]],
        dept_rows: list[dict[str, Any]],
        fake_audit: FakeAuditLogger,
    ) -> None:
        """When the issue is in needs_info AND the comment carries
        ``[approve]``, the needs_info branch wins (iterate → needs_info →
        approval ordering). The test pins the documented ordering so a
        future refactor can't silently swap it."""
        conn = FakeConnection(
            bot_rows=bot_rows,
            dept_rows=dept_rows,
            work_item_row={"status": "needs_info"},
        )
        pool = FakePool(conn=conn)
        temporal = FakeTemporalClient()
        dispatcher = WebhookDispatcher(
            db=pool, temporal=temporal, audit_logger=fake_audit
        )

        result = await dispatcher.dispatch(_comment_payload("[approve]"))

        assert result.action == "signaled"
        signal_names = [s["signal_name"] for s in temporal.sent_signals]
        assert "info_received" in signal_names
        assert "approval_received" not in signal_names
