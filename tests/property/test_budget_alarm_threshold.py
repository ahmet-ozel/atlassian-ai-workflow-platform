"""Budget alarm threshold behavior.

Background
----------

The ``BudgetCapPolicy._check_alarm_thresholds`` method in
``automation_service.budget.policy`` implements the budget alarm
threshold logic:

* When a workflow is allowed (no scope exceeded), the policy checks
 configured alarm thresholds from
 ``automation.budget_alarm_thresholds``.
* If the current usage percentage meets or exceeds ``threshold_pct``
 and the alarm has not already been sent in the current period,
 a notification is dispatched and ``last_alarmed_at`` is updated.
* If the alarm was already sent in the same period (weekly=7 days,
 monthly=30 days), it is NOT re-sent (deduplication).
* In a new period (last_alarmed_at is older than the period window),
 the alarm resets and can fire again.

Strategy
--------

We use Hypothesis to generate random budget configurations, usage
levels, and threshold settings, then verify four behaviors:

(a) Below threshold → no alarm dispatched.
(b) At or above threshold → alarm dispatched exactly once.
(c) Same period, threshold still breached → alarm NOT re-dispatched.
(d) New period (last_alarmed_at outside window) → alarm fires again.

The policy is exercised end-to-end with in-memory fakes for the
``AlarmThresholdStore``, ``NotificationDispatcher``,
``UsageQueryRunner``, and ``AuditLogger``.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Literal, Sequence

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_AUTOMATION_ROOT: Final[Path] = (
    _REPO_ROOT / "services" / "automation-service"
)

_SRC_DIRS: tuple[Path, ...] = (
    _AUTOMATION_ROOT / "src",
    _AUTOMATION_ROOT,
    _REPO_ROOT / "libs" / "audit_logger" / "src",
)
for _src in _SRC_DIRS:
    _src_str = str(_src)
    if _src.is_dir() and _src_str not in sys.path:
        sys.path.insert(0, _src_str)


from audit_logger import AuditEvent, AuditLogger  # noqa: E402

from automation_service.budget.policy import (  # noqa: E402
    AlarmThreshold,
    AlarmThresholdStore,
    BudgetCapPolicy,
    BudgetCaps,
    BudgetDecision,
    NotificationDispatcher,
    StaticBudgetCapsProvider,
)


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeNotificationDispatcher:
    """Records budget alarm notifications dispatched by the policy."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    async def send_budget_alarm(
        self,
        *,
        channel: Literal["slack", "email", "teams"],
        dept_id: str,
        period: str,
        scope: str,
        current_usd: Decimal,
        cap_usd: Decimal,
        threshold_pct: int,
        pct_used: Decimal,
    ) -> None:
        self.calls.append({
            "channel": channel,
            "dept_id": dept_id,
            "period": period,
            "scope": scope,
            "current_usd": current_usd,
            "cap_usd": cap_usd,
            "threshold_pct": threshold_pct,
            "pct_used": pct_used,
        })


@dataclass
class FakeAlarmThresholdStore:
    """In-memory alarm threshold store for testing."""

    thresholds: dict[str, list[AlarmThreshold]] = field(default_factory=dict)
    updated: list[tuple[str, datetime]] = field(default_factory=list)

    async def get_thresholds(self, dept_id: str) -> Sequence[AlarmThreshold]:
        return self.thresholds.get(dept_id, [])

    async def update_last_alarmed_at(
        self, threshold_id: str, alarmed_at: datetime
    ) -> None:
        self.updated.append((threshold_id, alarmed_at))
        # Also update the in-memory threshold so subsequent calls see it
        for dept_thresholds in self.thresholds.values():
            for i, t in enumerate(dept_thresholds):
                if t.id == threshold_id:
                    dept_thresholds[i] = AlarmThreshold(
                        id=t.id,
                        dept_id=t.dept_id,
                        period=t.period,
                        scope=t.scope,
                        threshold_pct=t.threshold_pct,
                        notify_channel=t.notify_channel,
                        last_alarmed_at=alarmed_at,
                    )
                    return


@dataclass
class FakeUsageQueryRunner:
    """Returns pre-configured usage values for budget queries."""

    dept_weekly: Decimal = Decimal("0")
    user_weekly: Decimal = Decimal("0")
    dept_monthly: Decimal = Decimal("0")
    user_monthly: Decimal = Decimal("0")

    async def fetchval(self, query: str, *args: Any) -> Decimal:
        # Determine which scope based on query + args
        # args[0] = dept_id, args[1] = interval string, args[2] = user_id (optional)
        interval = args[1] if len(args) > 1 else ""
        is_user_query = len(args) > 2

        if "7 days" in interval:
            return self.user_weekly if is_user_query else self.dept_weekly
        elif "30 days" in interval:
            return self.user_monthly if is_user_query else self.dept_monthly
        return Decimal("0")


@dataclass
class FakeAuditLogger:
    """Records audit events written by the policy."""

    events: list[AuditEvent] = field(default_factory=list)

    async def write(self, event: AuditEvent) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Helper to build a policy with fakes
# ---------------------------------------------------------------------------


def _build_policy(
    *,
    dept_id: str,
    caps: BudgetCaps,
    usage_runner: FakeUsageQueryRunner,
    threshold_store: FakeAlarmThresholdStore,
    notification_dispatcher: FakeNotificationDispatcher,
    clock: datetime | None = None,
) -> tuple[BudgetCapPolicy, FakeAuditLogger]:
    """Build a BudgetCapPolicy wired with fakes."""
    audit_logger = FakeAuditLogger()
    caps_provider = StaticBudgetCapsProvider(caps={dept_id: caps})

    fixed_clock = clock or datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    policy = BudgetCapPolicy(
        caps_provider=caps_provider,
        usage_query=usage_runner,
        audit_logger=audit_logger,
        clock=lambda: fixed_clock,
        alarm_threshold_store=threshold_store,
        notification_dispatcher=notification_dispatcher,
    )
    return policy, audit_logger


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_dept_id_strategy = st.from_regex(r"^[a-z][a-z0-9\-]{1,10}$", fullmatch=True)

_period_strategy = st.sampled_from(["weekly", "monthly"])

_scope_strategy = st.sampled_from(["dept", "user"])

_channel_strategy = st.sampled_from(["slack", "email", "teams"])

_threshold_pct_strategy = st.integers(min_value=1, max_value=99)

# Budget cap values — always positive to avoid division by zero
_cap_strategy = st.decimals(
    min_value=Decimal("10"),
    max_value=Decimal("10000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)


# ---------------------------------------------------------------------------
# Behavior: Below threshold → no alarm dispatched
# ---------------------------------------------------------------------------


class TestBelowThresholdNoAlarm:
    """When the current usage percentage is strictly below the configured
 threshold_pct, no alarm notification is dispatched.
 """

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        threshold_pct=_threshold_pct_strategy,
        period=_period_strategy,
        scope=_scope_strategy,
        channel=_channel_strategy,
        cap=_cap_strategy,
    )
    def test_below_threshold_no_alarm(
        self,
        dept_id: str,
        threshold_pct: int,
        period: str,
        scope: str,
        channel: str,
        cap: Decimal,
    ) -> None:
        """: No alarm when usage is below threshold_pct."""
        # Compute a usage value strictly below the threshold
        # usage_pct < threshold_pct → usage < cap * threshold_pct / 100
        max_usage = (cap * Decimal(str(threshold_pct)) / Decimal("100")) - Decimal("0.01")
        usage_val = max(Decimal("0"), max_usage)

        # Ensure usage is actually below threshold
        pct_used = (usage_val / cap) * Decimal("100")
        assume(pct_used < Decimal(str(threshold_pct)))

        # Set up caps high enough that the policy won't deny
        caps = BudgetCaps(
            weekly_usd_dept=cap,
            weekly_usd_user=cap,
            monthly_usd_dept=cap,
            monthly_usd_user=cap,
        )

        # Set usage based on period/scope
        usage_runner = FakeUsageQueryRunner(
            dept_weekly=usage_val if period == "weekly" else Decimal("0"),
            user_weekly=usage_val if period == "weekly" else Decimal("0"),
            dept_monthly=usage_val if period == "monthly" else Decimal("0"),
            user_monthly=usage_val if period == "monthly" else Decimal("0"),
        )

        threshold_id = str(uuid.uuid4())
        threshold_store = FakeAlarmThresholdStore(
            thresholds={
                dept_id: [
                    AlarmThreshold(
                        id=threshold_id,
                        dept_id=dept_id,
                        period=period,
                        scope=scope,
                        threshold_pct=threshold_pct,
                        notify_channel=channel,
                        last_alarmed_at=None,
                    )
                ]
            }
        )

        dispatcher = FakeNotificationDispatcher()

        policy, _ = _build_policy(
            dept_id=dept_id,
            caps=caps,
            usage_runner=usage_runner,
            threshold_store=threshold_store,
            notification_dispatcher=dispatcher,
        )

        # Use user_id for user-scope thresholds
        user_id = "test-user" if scope == "user" else None

        decision = asyncio.run(policy.enforce(dept_id=dept_id, user_id=user_id))

        # Policy should allow (usage is below cap)
        assert decision.allowed is True
        # No alarm should have been dispatched
        assert len(dispatcher.calls) == 0


# ---------------------------------------------------------------------------
# Behavior: At or above threshold → alarm dispatched once
# ---------------------------------------------------------------------------


class TestAboveThresholdAlarmFires:
    """When the current usage percentage meets or exceeds threshold_pct
 and no alarm has been sent in the current period, exactly one
 alarm notification is dispatched.
 """

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        threshold_pct=_threshold_pct_strategy,
        period=_period_strategy,
        scope=_scope_strategy,
        channel=_channel_strategy,
        cap=_cap_strategy,
    )
    def test_above_threshold_alarm_fires(
        self,
        dept_id: str,
        threshold_pct: int,
        period: str,
        scope: str,
        channel: str,
        cap: Decimal,
    ) -> None:
        """: Alarm fires when usage >= threshold_pct of cap."""
        # Compute a usage value at or above the threshold but below the cap
        # (so the policy allows the workflow but triggers the alarm)
        threshold_usage = (cap * Decimal(str(threshold_pct)) / Decimal("100"))
        # Use exactly the threshold value (at threshold)
        usage_val = threshold_usage

        # Ensure usage is at or above threshold but below cap
        assume(usage_val < cap)
        pct_used = (usage_val / cap) * Decimal("100")
        assume(pct_used >= Decimal(str(threshold_pct)))

        caps = BudgetCaps(
            weekly_usd_dept=cap,
            weekly_usd_user=cap,
            monthly_usd_dept=cap,
            monthly_usd_user=cap,
        )

        usage_runner = FakeUsageQueryRunner(
            dept_weekly=usage_val if period == "weekly" else Decimal("0"),
            user_weekly=usage_val if period == "weekly" else Decimal("0"),
            dept_monthly=usage_val if period == "monthly" else Decimal("0"),
            user_monthly=usage_val if period == "monthly" else Decimal("0"),
        )

        threshold_id = str(uuid.uuid4())
        threshold_store = FakeAlarmThresholdStore(
            thresholds={
                dept_id: [
                    AlarmThreshold(
                        id=threshold_id,
                        dept_id=dept_id,
                        period=period,
                        scope=scope,
                        threshold_pct=threshold_pct,
                        notify_channel=channel,
                        last_alarmed_at=None,  # Never alarmed before
                    )
                ]
            }
        )

        dispatcher = FakeNotificationDispatcher()

        policy, audit_logger = _build_policy(
            dept_id=dept_id,
            caps=caps,
            usage_runner=usage_runner,
            threshold_store=threshold_store,
            notification_dispatcher=dispatcher,
        )

        user_id = "test-user" if scope == "user" else None

        decision = asyncio.run(policy.enforce(dept_id=dept_id, user_id=user_id))

        # Policy should allow (usage is below cap)
        assert decision.allowed is True
        # Exactly one alarm should have been dispatched
        assert len(dispatcher.calls) == 1
        # Verify alarm payload
        alarm = dispatcher.calls[0]
        assert alarm["dept_id"] == dept_id
        assert alarm["period"] == period
        assert alarm["scope"] == scope
        assert alarm["channel"] == channel
        assert alarm["threshold_pct"] == threshold_pct
        assert alarm["current_usd"] == usage_val
        assert alarm["cap_usd"] == cap

        # last_alarmed_at should have been updated
        assert len(threshold_store.updated) == 1
        assert threshold_store.updated[0][0] == threshold_id


# ---------------------------------------------------------------------------
# Behavior: Same period, already alarmed → no re-dispatch
# ---------------------------------------------------------------------------


class TestSamePeriodNoReDispatch:
    """When the alarm was already sent in the current period (i.e.,
 last_alarmed_at is within the period window), the alarm is NOT
 re-dispatched even if the threshold is still breached.
 """

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        threshold_pct=_threshold_pct_strategy,
        period=_period_strategy,
        scope=_scope_strategy,
        channel=_channel_strategy,
        cap=_cap_strategy,
        days_ago=st.integers(min_value=0, max_value=5),
    )
    def test_same_period_no_re_dispatch(
        self,
        dept_id: str,
        threshold_pct: int,
        period: str,
        scope: str,
        channel: str,
        cap: Decimal,
        days_ago: int,
    ) -> None:
        """: No re-dispatch when already alarmed in same period."""
        now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

        # last_alarmed_at is within the current period window
        # weekly = 7 days, monthly = 30 days
        # days_ago is 0-5, always within both windows
        last_alarmed = now - timedelta(days=days_ago)

        # Usage above threshold
        threshold_usage = (cap * Decimal(str(threshold_pct)) / Decimal("100"))
        usage_val = threshold_usage + Decimal("1")
        assume(usage_val < cap)

        caps = BudgetCaps(
            weekly_usd_dept=cap,
            weekly_usd_user=cap,
            monthly_usd_dept=cap,
            monthly_usd_user=cap,
        )

        usage_runner = FakeUsageQueryRunner(
            dept_weekly=usage_val if period == "weekly" else Decimal("0"),
            user_weekly=usage_val if period == "weekly" else Decimal("0"),
            dept_monthly=usage_val if period == "monthly" else Decimal("0"),
            user_monthly=usage_val if period == "monthly" else Decimal("0"),
        )

        threshold_id = str(uuid.uuid4())
        threshold_store = FakeAlarmThresholdStore(
            thresholds={
                dept_id: [
                    AlarmThreshold(
                        id=threshold_id,
                        dept_id=dept_id,
                        period=period,
                        scope=scope,
                        threshold_pct=threshold_pct,
                        notify_channel=channel,
                        last_alarmed_at=last_alarmed,  # Already alarmed
                    )
                ]
            }
        )

        dispatcher = FakeNotificationDispatcher()

        policy, _ = _build_policy(
            dept_id=dept_id,
            caps=caps,
            usage_runner=usage_runner,
            threshold_store=threshold_store,
            notification_dispatcher=dispatcher,
            clock=now,
        )

        user_id = "test-user" if scope == "user" else None

        decision = asyncio.run(policy.enforce(dept_id=dept_id, user_id=user_id))

        # Policy should allow
        assert decision.allowed is True
        # No alarm should be dispatched (already alarmed in period)
        assert len(dispatcher.calls) == 0
        # No update to last_alarmed_at
        assert len(threshold_store.updated) == 0


# ---------------------------------------------------------------------------
# Behavior: New period (last_alarmed_at outside window) → alarm resets
# ---------------------------------------------------------------------------


class TestNewPeriodAlarmResets:
    """When the last alarm was sent in a previous period (last_alarmed_at
 is older than the period window), the alarm resets and fires again
 if the threshold is still breached.
 """

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        threshold_pct=_threshold_pct_strategy,
        period=_period_strategy,
        scope=_scope_strategy,
        channel=_channel_strategy,
        cap=_cap_strategy,
    )
    def test_new_period_alarm_resets(
        self,
        dept_id: str,
        threshold_pct: int,
        period: str,
        scope: str,
        channel: str,
        cap: Decimal,
    ) -> None:
        """,: Alarm fires again in a new period."""
        now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

        # last_alarmed_at is OUTSIDE the current period window
        if period == "weekly":
            # More than 7 days ago
            last_alarmed = now - timedelta(days=8)
        else:
            # More than 30 days ago
            last_alarmed = now - timedelta(days=31)

        # Usage above threshold but below cap
        threshold_usage = (cap * Decimal(str(threshold_pct)) / Decimal("100"))
        usage_val = threshold_usage + Decimal("0.01")
        assume(usage_val < cap)

        pct_used = (usage_val / cap) * Decimal("100")
        assume(pct_used >= Decimal(str(threshold_pct)))

        caps = BudgetCaps(
            weekly_usd_dept=cap,
            weekly_usd_user=cap,
            monthly_usd_dept=cap,
            monthly_usd_user=cap,
        )

        usage_runner = FakeUsageQueryRunner(
            dept_weekly=usage_val if period == "weekly" else Decimal("0"),
            user_weekly=usage_val if period == "weekly" else Decimal("0"),
            dept_monthly=usage_val if period == "monthly" else Decimal("0"),
            user_monthly=usage_val if period == "monthly" else Decimal("0"),
        )

        threshold_id = str(uuid.uuid4())
        threshold_store = FakeAlarmThresholdStore(
            thresholds={
                dept_id: [
                    AlarmThreshold(
                        id=threshold_id,
                        dept_id=dept_id,
                        period=period,
                        scope=scope,
                        threshold_pct=threshold_pct,
                        notify_channel=channel,
                        last_alarmed_at=last_alarmed,  # Old alarm, outside window
                    )
                ]
            }
        )

        dispatcher = FakeNotificationDispatcher()

        policy, _ = _build_policy(
            dept_id=dept_id,
            caps=caps,
            usage_runner=usage_runner,
            threshold_store=threshold_store,
            notification_dispatcher=dispatcher,
            clock=now,
        )

        user_id = "test-user" if scope == "user" else None

        decision = asyncio.run(policy.enforce(dept_id=dept_id, user_id=user_id))

        # Policy should allow
        assert decision.allowed is True
        # Alarm should fire again (new period)
        assert len(dispatcher.calls) == 1
        # Verify the alarm was for the correct threshold
        alarm = dispatcher.calls[0]
        assert alarm["dept_id"] == dept_id
        assert alarm["period"] == period
        assert alarm["scope"] == scope
        assert alarm["threshold_pct"] == threshold_pct

        # last_alarmed_at should have been updated
        assert len(threshold_store.updated) == 1
        assert threshold_store.updated[0][0] == threshold_id


# ---------------------------------------------------------------------------
# Integration: consecutive enforce calls demonstrate deduplication
# ---------------------------------------------------------------------------


class TestConsecutiveEnforceDeduplication:
    """Two consecutive enforce calls with the same threshold breached
 should only dispatch the alarm on the first call. The second call
 sees the updated last_alarmed_at and skips.
 """

    @settings(
        max_examples=30,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        threshold_pct=_threshold_pct_strategy,
        period=_period_strategy,
        channel=_channel_strategy,
        cap=_cap_strategy,
    )
    def test_consecutive_calls_deduplicate(
        self,
        dept_id: str,
        threshold_pct: int,
        period: str,
        channel: str,
        cap: Decimal,
    ) -> None:
        """,: Second call in same period does not re-alarm."""
        now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

        # Usage above threshold but below cap (dept scope for simplicity)
        threshold_usage = (cap * Decimal(str(threshold_pct)) / Decimal("100"))
        usage_val = threshold_usage + Decimal("0.01")
        assume(usage_val < cap)

        pct_used = (usage_val / cap) * Decimal("100")
        assume(pct_used >= Decimal(str(threshold_pct)))

        caps = BudgetCaps(
            weekly_usd_dept=cap,
            weekly_usd_user=cap,
            monthly_usd_dept=cap,
            monthly_usd_user=cap,
        )

        usage_runner = FakeUsageQueryRunner(
            dept_weekly=usage_val if period == "weekly" else Decimal("0"),
            user_weekly=Decimal("0"),
            dept_monthly=usage_val if period == "monthly" else Decimal("0"),
            user_monthly=Decimal("0"),
        )

        threshold_id = str(uuid.uuid4())
        threshold_store = FakeAlarmThresholdStore(
            thresholds={
                dept_id: [
                    AlarmThreshold(
                        id=threshold_id,
                        dept_id=dept_id,
                        period=period,
                        scope="dept",
                        threshold_pct=threshold_pct,
                        notify_channel=channel,
                        last_alarmed_at=None,  # Never alarmed
                    )
                ]
            }
        )

        dispatcher = FakeNotificationDispatcher()

        policy, _ = _build_policy(
            dept_id=dept_id,
            caps=caps,
            usage_runner=usage_runner,
            threshold_store=threshold_store,
            notification_dispatcher=dispatcher,
            clock=now,
        )

        # First call — alarm should fire
        decision1 = asyncio.run(policy.enforce(dept_id=dept_id, user_id=None))
        assert decision1.allowed is True
        assert len(dispatcher.calls) == 1

        # Second call — alarm should NOT fire (already alarmed in period)
        decision2 = asyncio.run(policy.enforce(dept_id=dept_id, user_id=None))
        assert decision2.allowed is True
        # Still only 1 alarm total
        assert len(dispatcher.calls) == 1
