"""Unit tests for the dispatcher's Budget Enforcement runtime guard.

Ensures :class:`webhooks.dispatcher.WebhookDispatcher` invokes
:func:`automation_service.budget.policy.check_budget` **before** any
workflow start (both the normal Jira assign/update path and the
``[iterate]`` comment path), and that:

* On ``allow`` the workflow is started normally.
* On ``deny`` (cap reached) the dispatcher emits a
  ``DispatchResult(action="budget_exceeded", status_code=429,
  body=deny_response_body(...))`` and does **not** issue a Temporal
  start RPC. The ``budget_exceeded`` audit row + the Jira rejection
  comment are written by ``check_budget`` itself.
* The four scopes (``dept_weekly``, ``user_weekly``, ``dept_monthly``,
  ``user_monthly``) are honoured in the policy's canonical order.
* A 90% threshold scope still allows the workflow to start but
  surfaces a Jira warning comment (the dispatcher does not block).
* An undefined ``dept_id`` (missing from ``budget_caps`` config)
  yields a configuration-error response.

"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup — mirrors test_webhooks_dispatcher.py + test_budget_policy.py
# ---------------------------------------------------------------------------

_TESTS_DIR = Path(__file__).resolve().parent
_AUTOMATION_ROOT = _TESTS_DIR.parent.parent
_PLATFORM_ROOT = _AUTOMATION_ROOT.parents[1]

for path in (
    _AUTOMATION_ROOT / "src",
    _AUTOMATION_ROOT,
    _PLATFORM_ROOT / "libs" / "audit_logger" / "src",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from audit_logger import AuditEvent, AuditLogger  # noqa: E402

from automation_service.budget.policy import (  # noqa: E402
    BudgetCapPolicy,
    BudgetCaps,
    StaticBudgetCapsProvider,
)
from webhooks.dispatcher import WebhookDispatcher  # noqa: E402
from webhooks.loop_guard import WebhookPayload  # noqa: E402


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class _FakeConnection:
    """Minimal asyncpg connection fake used by the dispatcher cache."""

    def __init__(
        self,
        bot_rows: list[dict[str, Any]],
        dept_rows: list[dict[str, Any]],
        work_item_row: dict[str, Any] | None = None,
    ) -> None:
        self._bot_rows = bot_rows
        self._dept_rows = dept_rows
        self._work_item_row = work_item_row

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "department_bots" in query:
            return self._bot_rows
        if "departments" in query and "mode" in query:
            return self._dept_rows
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "department_bots" in query:
            for row in self._bot_rows:
                if row["account_id"] == args[0]:
                    return row
            return None
        if "departments" in query and "mode" in query:
            for row in self._dept_rows:
                if row["id"] == args[0]:
                    return row
            return None
        if "work_items" in query:
            return self._work_item_row
        return None


class _FakePool:
    """Async context manager surface compatible with asyncpg pools."""

    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    def acquire(self) -> "_FakePoolContext":
        return _FakePoolContext(self._conn)


class _FakePoolContext:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeTemporalClient:
    """Records workflow start RPCs so tests can assert non-issuance."""

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
        self.sent_signals.append(
            {
                "workflow_id": workflow_id,
                "signal_name": signal_name,
                "payload": payload,
            }
        )


@dataclass
class _RecordingAuditWriter:
    """Records :class:`AuditEvent` writes by the policy."""

    events: list[AuditEvent] = field(default_factory=list)

    async def insert_audit(self, event: AuditEvent) -> None:
        self.events.append(event)


class _DispatcherAuditLogger:
    """Lightweight audit logger compatible with the dispatcher's protocol.

    The dispatcher's ``_audit`` helper expects an object with an
    async ``write(event)`` method; tests use a recording stub so we
    can correlate dispatcher-side events (e.g. ``dispatch_workflow_started``)
    with policy-side events (``budget_exceeded``).
    """

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class _FakeUsageRunner:
    """In-memory ``fetchval`` fake yielding canned cost-aggregate values."""

    dept_weekly: Decimal = Decimal("0")
    dept_monthly: Decimal = Decimal("0")
    user_weekly: Decimal = Decimal("0")
    user_monthly: Decimal = Decimal("0")

    async def fetchval(self, query: str, *args: Any) -> Decimal:
        is_user_scope = "user_id = $3" in query
        interval = args[1] if len(args) >= 2 else ""
        if is_user_scope:
            if interval == "7 days":
                return self.user_weekly
            return self.user_monthly
        if interval == "7 days":
            return self.dept_weekly
        return self.dept_monthly


class _RecordingJiraCommenter:
    """Records ``post_comment`` calls made by the dispatcher / policy."""

    def __init__(self) -> None:
        self.comments: list[tuple[str, str, str]] = []

    async def post_comment(self, dept_id: str, issue_key: str, body: str) -> None:
        self.comments.append((dept_id, issue_key, body))


def _fixed_clock() -> datetime:
    return datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Default fixtures
# ---------------------------------------------------------------------------


_DEFAULT_CAPS = BudgetCaps(
    weekly_usd_dept=Decimal("1000"),
    weekly_usd_user=Decimal("200"),
    monthly_usd_dept=Decimal("4000"),
    monthly_usd_user=Decimal("800"),
)


def _make_policy(
    *,
    caps: BudgetCaps = _DEFAULT_CAPS,
    dept_id: str = "payment",
    usage_runner: _FakeUsageRunner | None = None,
    audit_writer: _RecordingAuditWriter | None = None,
) -> tuple[BudgetCapPolicy, _FakeUsageRunner, _RecordingAuditWriter]:
    """Build a :class:`BudgetCapPolicy` with in-memory fakes.

    Returns a (policy, usage_runner, audit_writer) tuple so test
    cases can introspect the side-effect channel of interest.
    """

    runner = usage_runner or _FakeUsageRunner()
    writer = audit_writer or _RecordingAuditWriter()
    audit_logger = AuditLogger(writer=writer)
    provider = StaticBudgetCapsProvider(caps={dept_id: caps})
    policy = BudgetCapPolicy(
        caps_provider=provider,
        usage_query=runner,
        audit_logger=audit_logger,
        clock=_fixed_clock,
    )
    return policy, runner, writer


def _make_dispatcher(
    *,
    budget_policy: BudgetCapPolicy | None,
    audit_logger: _DispatcherAuditLogger | None = None,
    jira_commenter: _RecordingJiraCommenter | None = None,
    bot_account_id: str = "bot-account-123",
    dept_id: str = "payment",
    work_item_row: dict[str, Any] | None = None,
) -> tuple[WebhookDispatcher, _FakeTemporalClient, _DispatcherAuditLogger]:
    """Build a dispatcher wired to the fake DB / Temporal / audit stack."""

    bot_rows = [
        {
            "account_id": bot_account_id,
            "department_id": dept_id,
            "service": "jira",
        }
    ]
    dept_rows = [
        {
            "id": dept_id,
            "mode": "active",
            "config_json": {"approvers": ["approver-user-1"]},
        }
    ]
    conn = _FakeConnection(
        bot_rows=bot_rows,
        dept_rows=dept_rows,
        work_item_row=work_item_row,
    )
    pool = _FakePool(conn)
    temporal = _FakeTemporalClient()
    audit = audit_logger or _DispatcherAuditLogger()
    dispatcher = WebhookDispatcher(
        db=pool,
        temporal=temporal,
        audit_logger=audit,
        jira_commenter=jira_commenter,
        budget_policy=budget_policy,
    )
    return dispatcher, temporal, audit


def _payload(
    *,
    issue_key: str = "PAY-100",
    bot_account_id: str = "bot-account-123",
    actor_account_id: str | None = "user-alice",
    comment_body: str | None = None,
    event_type: str = "issue_assigned",
) -> WebhookPayload:
    return WebhookPayload(
        event_type=event_type,
        issue_key=issue_key,
        assignee_account_id=bot_account_id,
        actor_account_id=actor_account_id,
        reporter_account_id="reporter-user-x",
        comment_body=comment_body,
    )


def _budget_audit_events(audit: _DispatcherAuditLogger) -> list[AuditEvent]:
    return [ev for ev in audit.events if ev.action == "budget_exceeded"]


# ===========================================================================
# Tests: cap not exceeded → workflow proceeds normally
# ===========================================================================


class TestCapNotExceeded:
    """When usage is below all caps the dispatcher starts the workflow."""

    @pytest.mark.asyncio
    async def test_workflow_starts_when_below_cap(self) -> None:
        policy, _, audit_writer = _make_policy(
            usage_runner=_FakeUsageRunner(
                dept_weekly=Decimal("100"),
                dept_monthly=Decimal("400"),
                user_weekly=Decimal("20"),
                user_monthly=Decimal("80"),
            )
        )
        dispatcher, temporal, dispatcher_audit = _make_dispatcher(
            budget_policy=policy,
        )

        result = await dispatcher.dispatch(_payload(issue_key="PAY-1001"))

        assert result.action == "workflow_started"
        assert result.status_code is None
        assert result.body is None
        # Workflow start RPC was issued.
        assert len(temporal.started_workflows) == 1
        assert temporal.started_workflows[0]["workflow_type"] == "AutomationWorkflow"
        # No ``budget_exceeded`` audit row was written.
        assert audit_writer.events == []
        # Dispatcher audit only carries the ``dispatch_workflow_started`` row.
        actions = [ev.action for ev in dispatcher_audit.events]
        assert "budget_exceeded" not in actions

    @pytest.mark.asyncio
    async def test_no_budget_policy_wired_means_no_op(self) -> None:
        """Legacy callers that don't pass ``budget_policy`` are unaffected."""
        dispatcher, temporal, _ = _make_dispatcher(budget_policy=None)

        result = await dispatcher.dispatch(_payload(issue_key="PAY-1002"))

        assert result.action == "workflow_started"
        assert len(temporal.started_workflows) == 1


# ===========================================================================
# Tests: dept_weekly cap exceeded → 429 + no workflow start
# ===========================================================================


class TestDeptWeeklyCapExceeded:
    """Department weekly cap reached → HTTP 429 + audit + Jira comment."""

    @pytest.mark.asyncio
    async def test_returns_budget_exceeded_429(self) -> None:
        policy, _, audit_writer = _make_policy(
            usage_runner=_FakeUsageRunner(
                dept_weekly=Decimal("1000"),  # == cap
                dept_monthly=Decimal("2000"),
            )
        )
        commenter = _RecordingJiraCommenter()
        dispatcher, temporal, dispatcher_audit = _make_dispatcher(
            budget_policy=policy,
            jira_commenter=commenter,
        )

        result = await dispatcher.dispatch(_payload(issue_key="PAY-1100"))

        assert result.action == "budget_exceeded"
        assert result.status_code == 429
        assert result.dept_id == "payment"
        assert result.body == {
            "error": "budget_exceeded",
            "dept_id": "payment",
            "scope": "dept_weekly",
        }

    @pytest.mark.asyncio
    async def test_workflow_start_not_invoked(self) -> None:
        policy, _, _ = _make_policy(
            usage_runner=_FakeUsageRunner(dept_weekly=Decimal("1500"))
        )
        dispatcher, temporal, _ = _make_dispatcher(budget_policy=policy)

        await dispatcher.dispatch(_payload(issue_key="PAY-1101"))

        assert temporal.started_workflows == []

    @pytest.mark.asyncio
    async def test_audit_budget_exceeded_row_written(self) -> None:
        policy, _, audit_writer = _make_policy(
            usage_runner=_FakeUsageRunner(dept_weekly=Decimal("1100"))
        )
        dispatcher, _, _ = _make_dispatcher(budget_policy=policy)

        await dispatcher.dispatch(_payload(issue_key="PAY-1102"))

        # ``check_budget`` writes a single ``budget_exceeded`` row with
        # ``actor_role='system'`` and ``result='denied'`` per the
        # canonical policy contract.
        rows = [ev for ev in audit_writer.events if ev.action == "budget_exceeded"]
        assert len(rows) == 1
        ev = rows[0]
        assert ev.actor_role == "system"
        assert ev.result == "denied"
        assert ev.dept_id == "payment"
        assert ev.payload["scope"] == "dept_weekly"
        assert ev.payload["limit"] == "1000"
        assert "usage" in ev.payload
        assert ev.payload["user_id"] == "user-alice"

    @pytest.mark.asyncio
    async def test_jira_rejection_comment_posted(self) -> None:
        policy, _, _ = _make_policy(
            usage_runner=_FakeUsageRunner(dept_weekly=Decimal("1500"))
        )
        commenter = _RecordingJiraCommenter()
        dispatcher, _, _ = _make_dispatcher(
            budget_policy=policy,
            jira_commenter=commenter,
        )

        await dispatcher.dispatch(_payload(issue_key="PAY-1103"))

        # ``check_budget`` posts a denial comment via the adapter the
        # dispatcher passes in. The body identifies the offending scope.
        assert len(commenter.comments) == 1
        dept_id, issue_key, body = commenter.comments[0]
        assert dept_id == "payment"
        assert issue_key == "PAY-1103"
        assert "dept_weekly" in body
        assert "reddedildi" in body


# ===========================================================================
# Tests: user_weekly cap exceeded → 429
# ===========================================================================


class TestUserWeeklyCapExceeded:
    """Per-user weekly cap reached for the same dept → HTTP 429."""

    @pytest.mark.asyncio
    async def test_returns_budget_exceeded_for_user_weekly(self) -> None:
        policy, _, audit_writer = _make_policy(
            usage_runner=_FakeUsageRunner(
                dept_weekly=Decimal("100"),  # below cap
                user_weekly=Decimal("250"),  # > 200 cap
                dept_monthly=Decimal("500"),
                user_monthly=Decimal("100"),
            )
        )
        commenter = _RecordingJiraCommenter()
        dispatcher, temporal, _ = _make_dispatcher(
            budget_policy=policy,
            jira_commenter=commenter,
        )

        result = await dispatcher.dispatch(_payload(issue_key="PAY-1200"))

        assert result.action == "budget_exceeded"
        assert result.status_code == 429
        assert result.body["scope"] == "user_weekly"
        # No workflow start RPC issued.
        assert temporal.started_workflows == []
        # Single ``budget_exceeded`` audit row carrying the user_id.
        rows = [ev for ev in audit_writer.events if ev.action == "budget_exceeded"]
        assert len(rows) == 1
        assert rows[0].payload["scope"] == "user_weekly"
        assert rows[0].payload["user_id"] == "user-alice"


# ===========================================================================
# Tests: dept_monthly cap exceeded → 429
# ===========================================================================


class TestDeptMonthlyCapExceeded:
    """Department monthly cap reached → HTTP 429."""

    @pytest.mark.asyncio
    async def test_returns_budget_exceeded_for_dept_monthly(self) -> None:
        policy, _, audit_writer = _make_policy(
            usage_runner=_FakeUsageRunner(
                dept_weekly=Decimal("100"),
                user_weekly=Decimal("20"),
                dept_monthly=Decimal("4500"),  # > 4000 cap
                user_monthly=Decimal("80"),
            )
        )
        commenter = _RecordingJiraCommenter()
        dispatcher, temporal, _ = _make_dispatcher(
            budget_policy=policy,
            jira_commenter=commenter,
        )

        result = await dispatcher.dispatch(_payload(issue_key="PAY-1300"))

        assert result.action == "budget_exceeded"
        assert result.status_code == 429
        assert result.body["scope"] == "dept_monthly"
        assert temporal.started_workflows == []
        rows = [ev for ev in audit_writer.events if ev.action == "budget_exceeded"]
        assert rows and rows[0].payload["scope"] == "dept_monthly"


# ===========================================================================
# Tests: 90% warning threshold → workflow proceeds + Jira warning
# ===========================================================================


class TestWarningThreshold:
    """At the 90% threshold the workflow starts but a Jira warning is posted."""

    @pytest.mark.asyncio
    async def test_workflow_starts_with_warning_comment(self) -> None:
        policy, _, audit_writer = _make_policy(
            usage_runner=_FakeUsageRunner(
                dept_weekly=Decimal("950"),  # 95% of 1000
                user_weekly=Decimal("20"),
                dept_monthly=Decimal("400"),
                user_monthly=Decimal("80"),
            )
        )
        commenter = _RecordingJiraCommenter()
        dispatcher, temporal, _ = _make_dispatcher(
            budget_policy=policy,
            jira_commenter=commenter,
        )

        result = await dispatcher.dispatch(_payload(issue_key="PAY-1400"))

        # Workflow proceeds.
        assert result.action == "workflow_started"
        assert len(temporal.started_workflows) == 1
        # ``budget_exceeded`` audit row is NOT written.
        assert audit_writer.events == []
        # A warning comment was posted (90% threshold).
        assert len(commenter.comments) == 1
        _, issue_key, body = commenter.comments[0]
        assert issue_key == "PAY-1400"
        assert "%90" in body or "uyarı" in body.lower()
        assert "dept_weekly" in body


# ===========================================================================
# Tests: undefined dept_id → configuration_error
# ===========================================================================


class TestUndefinedDept:
    """A dept missing from ``budget_caps`` triggers a configuration error."""

    @pytest.mark.asyncio
    async def test_returns_budget_configuration_error(self) -> None:
        # Build a policy keyed on ``payment`` only; the dispatcher
        # resolves the assignee to a different dept ``ghost`` so the
        # caps provider raises ``KeyError`` at check-time.
        policy, _, _ = _make_policy(dept_id="payment")
        dispatcher, temporal, _ = _make_dispatcher(
            budget_policy=policy,
            dept_id="ghost",  # different from the policy's caps key
        )

        result = await dispatcher.dispatch(_payload(issue_key="GH-1"))

        assert result.action == "budget_configuration_error"
        assert result.status_code == 422
        assert result.body is not None
        assert result.body["error"] == "configuration_error"
        assert result.body["dept_id"] == "ghost"
        # Workflow start was NOT invoked.
        assert temporal.started_workflows == []


# ===========================================================================
# Tests: [iterate] path also runs the budget gate
# ===========================================================================


class TestIteratePathBudgetGate:
    """The ``[iterate]`` flow must also short-circuit on cap breach."""

    @pytest.mark.asyncio
    async def test_iterate_blocked_when_cap_exceeded(self) -> None:
        policy, _, audit_writer = _make_policy(
            usage_runner=_FakeUsageRunner(dept_weekly=Decimal("1500"))
        )
        commenter = _RecordingJiraCommenter()
        dispatcher, temporal, _ = _make_dispatcher(
            budget_policy=policy,
            jira_commenter=commenter,
        )

        result = await dispatcher.dispatch(
            _payload(
                issue_key="PAY-1500",
                event_type="comment_created",
                actor_account_id="approver-user-1",  # authorized to iterate
                comment_body="[iterate] try again with the new logic",
            )
        )

        assert result.action == "budget_exceeded"
        assert result.status_code == 429
        # No iteration workflow started.
        assert all(
            wf["workflow_type"] != "IterationWorkflow"
            for wf in temporal.started_workflows
        )
        # Audit row written by the policy.
        rows = [ev for ev in audit_writer.events if ev.action == "budget_exceeded"]
        assert len(rows) == 1
        assert rows[0].payload["scope"] == "dept_weekly"
