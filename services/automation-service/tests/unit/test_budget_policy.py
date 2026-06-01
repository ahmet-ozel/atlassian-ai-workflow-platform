"""Unit tests for ``automation_service.budget.policy.BudgetCapPolicy``.

Validates: Requirement 5.5 (BudgetCapPolicy enforcement — task 7.3).

The tests exercise the policy against an in-memory caps provider, a
list-backed asyncpg fake, and a recording :class:`AuditWriter`. They
cover:

* the four-scope ordering (``dept_weekly → user_weekly → dept_monthly
  → user_monthly``),
* the ``cost_tag = 'production'`` SQL filter (the policy does not
  re-aggregate sandbox / probe rows itself, but we assert the SQL it
  issues carries the literal),
* the audit event shape on deny (``action='budget_exceeded'``,
  ``actor_role='system'``, ``result='denied'``, scope+limit+usage in
  the payload),
* the ``user_id is None`` short-circuit so a system workflow cannot
  be denied on a user-scoped cap, and
* the value-object invariants of :class:`BudgetDecision`.
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
# Path setup — keep the bootstrap consistent with the other unit tests
# (sys.path injection so ``automation_service`` and ``audit_logger``
# resolve without an editable install).
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
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
    SCOPE_ORDER,
    BudgetCapPolicy,
    BudgetCaps,
    BudgetDecision,
    StaticBudgetCapsProvider,
    deny_response_body,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _RecordingAuditWriter:
    """Append-only writer that records every event for assertions."""

    events: list[AuditEvent] = field(default_factory=list)

    async def insert_audit(self, event: AuditEvent) -> None:
        self.events.append(event)


@dataclass
class _FakeUsageRunner:
    """In-memory ``fetchval`` fake driven by a (sql_substr, args) → value table.

    Test cases populate ``responses`` keyed by a tuple of distinguishing
    SQL fragments + the positional arg tuple. The fake also records
    every call into ``calls`` so tests can assert on the SQL the
    policy issues (e.g. that ``cost_tag = 'production'`` is present).
    """

    dept_weekly: Decimal = Decimal("0")
    dept_monthly: Decimal = Decimal("0")
    user_weekly: Decimal = Decimal("0")
    user_monthly: Decimal = Decimal("0")
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    async def fetchval(self, query: str, *args: Any) -> Decimal:
        self.calls.append((query, args))

        # Distinguish dept-only vs user-scoped queries by the presence
        # of the ``user_id = $3`` filter the policy adds.
        is_user_scope = "user_id = $3" in query
        # Window detection by the formatted interval literal in args.
        interval = args[1] if len(args) >= 2 else ""

        if is_user_scope:
            if interval == "7 days":
                return self.user_weekly
            if interval == "30 days":
                return self.user_monthly
        else:
            if interval == "7 days":
                return self.dept_weekly
            if interval == "30 days":
                return self.dept_monthly
        return Decimal("0")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FROZEN_NOW = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _frozen_clock() -> datetime:
    return _FROZEN_NOW


def _make_caps(
    *,
    weekly_dept: str = "100",
    weekly_user: str = "20",
    monthly_dept: str = "300",
    monthly_user: str = "60",
) -> BudgetCaps:
    return BudgetCaps(
        weekly_usd_dept=Decimal(weekly_dept),
        weekly_usd_user=Decimal(weekly_user),
        monthly_usd_dept=Decimal(monthly_dept),
        monthly_usd_user=Decimal(monthly_user),
    )


def _make_policy(
    *,
    caps: BudgetCaps | None = None,
    runner: _FakeUsageRunner | None = None,
    writer: _RecordingAuditWriter | None = None,
) -> tuple[BudgetCapPolicy, _FakeUsageRunner, _RecordingAuditWriter]:
    caps = caps or _make_caps()
    runner = runner or _FakeUsageRunner()
    writer = writer or _RecordingAuditWriter()
    provider = StaticBudgetCapsProvider(caps={"payment": caps})
    policy = BudgetCapPolicy(
        caps_provider=provider,
        usage_query=runner,
        audit_logger=AuditLogger(writer=writer),
        clock=_frozen_clock,
    )
    return policy, runner, writer


# ---------------------------------------------------------------------------
# BudgetDecision invariants
# ---------------------------------------------------------------------------


class TestBudgetDecision:
    def test_allow_has_no_scope(self) -> None:
        d = BudgetDecision.allow()
        assert d.allowed is True
        assert d.deny_scope is None

    def test_deny_carries_scope(self) -> None:
        for scope in SCOPE_ORDER:
            d = BudgetDecision.deny(scope)
            assert d.allowed is False
            assert d.deny_scope == scope

    def test_deny_rejects_unknown_scope(self) -> None:
        with pytest.raises(ValueError, match="scope must be one of"):
            BudgetDecision.deny("yearly")  # type: ignore[arg-type]

    def test_decision_is_frozen(self) -> None:
        d = BudgetDecision.allow()
        with pytest.raises((AttributeError, TypeError)):
            d.allowed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Allow path
# ---------------------------------------------------------------------------


class TestAllow:
    @pytest.mark.asyncio
    async def test_allows_when_all_scopes_below_caps(self) -> None:
        policy, _, writer = _make_policy()

        decision = await policy.enforce(dept_id="payment", user_id="user-1")

        assert decision == BudgetDecision.allow()
        assert writer.events == [], "no audit on allow"

    @pytest.mark.asyncio
    async def test_allows_at_caps_minus_epsilon(self) -> None:
        runner = _FakeUsageRunner(
            dept_weekly=Decimal("99.99"),
            user_weekly=Decimal("19.99"),
            dept_monthly=Decimal("299.99"),
            user_monthly=Decimal("59.99"),
        )
        policy, _, writer = _make_policy(runner=runner)

        decision = await policy.enforce(dept_id="payment", user_id="user-1")

        assert decision.allowed is True
        assert writer.events == []

    @pytest.mark.asyncio
    async def test_user_id_none_skips_user_scopes(self) -> None:
        # User caps would deny if user_id were attributed, but the
        # policy must short-circuit user scopes when user_id is None.
        runner = _FakeUsageRunner(
            dept_weekly=Decimal("10"),
            user_weekly=Decimal("99999"),  # well above the 20 cap
            dept_monthly=Decimal("10"),
            user_monthly=Decimal("99999"),
        )
        policy, _, writer = _make_policy(runner=runner)

        decision = await policy.enforce(dept_id="payment", user_id=None)

        assert decision == BudgetDecision.allow()
        assert writer.events == []


# ---------------------------------------------------------------------------
# Deny path — four scopes, ordering, audit shape
# ---------------------------------------------------------------------------


class TestDeny:
    @pytest.mark.asyncio
    async def test_deny_dept_weekly_emits_audit(self) -> None:
        runner = _FakeUsageRunner(dept_weekly=Decimal("100"))  # == cap
        policy, _, writer = _make_policy(runner=runner)

        decision = await policy.enforce(dept_id="payment", user_id="user-1")

        assert decision == BudgetDecision.deny("dept_weekly")
        assert len(writer.events) == 1
        ev = writer.events[0]
        assert ev.action == "budget_exceeded"
        assert ev.actor_role == "system"
        assert ev.result == "denied"
        assert ev.dept_id == "payment"
        assert ev.actor_id == "user-1"  # user_id rendered as actor when present
        assert ev.timestamp == _FROZEN_NOW
        assert ev.resource == "department:payment"
        assert ev.payload == {
            "scope": "dept_weekly",
            "limit": "100",
            "usage": "100",
            "user_id": "user-1",
        }

    @pytest.mark.asyncio
    async def test_deny_user_weekly_only_when_dept_weekly_is_under(self) -> None:
        runner = _FakeUsageRunner(
            dept_weekly=Decimal("10"),  # under 100 cap
            user_weekly=Decimal("25"),  # over 20 cap
        )
        policy, _, writer = _make_policy(runner=runner)

        decision = await policy.enforce(dept_id="payment", user_id="user-1")

        assert decision == BudgetDecision.deny("user_weekly")
        assert len(writer.events) == 1
        assert writer.events[0].payload["scope"] == "user_weekly"
        assert writer.events[0].payload["limit"] == "20"
        assert writer.events[0].payload["usage"] == "25"

    @pytest.mark.asyncio
    async def test_deny_dept_monthly_when_weekly_scopes_are_under(self) -> None:
        runner = _FakeUsageRunner(
            dept_weekly=Decimal("0"),
            user_weekly=Decimal("0"),
            dept_monthly=Decimal("301"),  # over 300 cap
        )
        policy, _, writer = _make_policy(runner=runner)

        decision = await policy.enforce(dept_id="payment", user_id="user-1")

        assert decision == BudgetDecision.deny("dept_monthly")
        assert writer.events[0].payload["scope"] == "dept_monthly"

    @pytest.mark.asyncio
    async def test_deny_user_monthly_last_in_order(self) -> None:
        runner = _FakeUsageRunner(
            dept_weekly=Decimal("0"),
            user_weekly=Decimal("0"),
            dept_monthly=Decimal("0"),
            user_monthly=Decimal("60"),  # == cap
        )
        policy, _, writer = _make_policy(runner=runner)

        decision = await policy.enforce(dept_id="payment", user_id="user-1")

        assert decision == BudgetDecision.deny("user_monthly")
        assert writer.events[0].payload["scope"] == "user_monthly"

    @pytest.mark.asyncio
    async def test_deny_uses_first_breach_when_multiple_scopes_exceed(self) -> None:
        # Every scope is over its cap; the policy must report the
        # first one in SCOPE_ORDER (dept_weekly) and write exactly
        # one audit row.
        runner = _FakeUsageRunner(
            dept_weekly=Decimal("999"),
            user_weekly=Decimal("999"),
            dept_monthly=Decimal("999"),
            user_monthly=Decimal("999"),
        )
        policy, _, writer = _make_policy(runner=runner)

        decision = await policy.enforce(dept_id="payment", user_id="user-1")

        assert decision == BudgetDecision.deny("dept_weekly")
        assert len(writer.events) == 1, "deny is single-shot, not multi-fire"

    @pytest.mark.asyncio
    async def test_audit_payload_omits_user_id_when_none(self) -> None:
        runner = _FakeUsageRunner(dept_weekly=Decimal("100"))
        policy, _, writer = _make_policy(runner=runner)

        await policy.enforce(dept_id="payment", user_id=None)

        ev = writer.events[0]
        assert "user_id" not in ev.payload
        assert ev.actor_id == "system"


# ---------------------------------------------------------------------------
# SQL invariants
# ---------------------------------------------------------------------------


class TestSqlInvariants:
    @pytest.mark.asyncio
    async def test_usage_queries_filter_on_production_cost_tag(self) -> None:
        policy, runner, _ = _make_policy()

        await policy.enforce(dept_id="payment", user_id="user-1")

        assert runner.calls, "policy should have issued at least one query"
        for sql, _args in runner.calls:
            assert "cost_tag = 'production'" in sql, (
                "every usage aggregate must filter sandbox/probe rows out — "
                "Requirement 5.5"
            )

    @pytest.mark.asyncio
    async def test_usage_queries_use_now_minus_interval_window(self) -> None:
        policy, runner, _ = _make_policy()

        await policy.enforce(dept_id="payment", user_id="user-1")

        intervals = {args[1] for _sql, args in runner.calls}
        assert intervals == {"7 days", "30 days"}

    @pytest.mark.asyncio
    async def test_user_id_none_skips_user_scope_sql(self) -> None:
        policy, runner, _ = _make_policy()

        await policy.enforce(dept_id="payment", user_id=None)

        for sql, _args in runner.calls:
            assert "user_id = $3" not in sql, (
                "user-scoped query must not be issued when user_id is None"
            )


# ---------------------------------------------------------------------------
# Decimal coercion + null handling
# ---------------------------------------------------------------------------


class TestNullCoercion:
    @pytest.mark.asyncio
    async def test_null_aggregate_is_treated_as_zero(self) -> None:
        # Some asyncpg fakes return ``None`` instead of Decimal("0").
        # The policy must coerce it so an empty cost_tracking table
        # cannot trip the >= cap comparison.
        class _NullRunner:
            calls: list[Any] = []

            async def fetchval(self, query: str, *args: Any) -> Any:
                return None

        policy, _, writer = _make_policy(runner=_NullRunner())  # type: ignore[arg-type]

        decision = await policy.enforce(dept_id="payment", user_id="user-1")

        assert decision == BudgetDecision.allow()
        assert writer.events == []

    @pytest.mark.asyncio
    async def test_int_aggregate_is_coerced(self) -> None:
        # asyncpg in some configurations returns int for SUM of an
        # integer-cast subquery; the policy must accept it.
        class _IntRunner:
            async def fetchval(self, query: str, *args: Any) -> Any:
                return 100  # equals the dept_weekly cap

        policy, _, writer = _make_policy(runner=_IntRunner())  # type: ignore[arg-type]

        decision = await policy.enforce(dept_id="payment", user_id="user-1")

        assert decision == BudgetDecision.deny("dept_weekly")
        assert writer.events[0].payload["usage"] == "100"


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    @pytest.mark.asyncio
    async def test_empty_dept_id_raises_value_error(self) -> None:
        policy, _, _ = _make_policy()
        with pytest.raises(ValueError, match="dept_id must be a non-empty string"):
            await policy.enforce(dept_id="", user_id=None)

    @pytest.mark.asyncio
    async def test_unknown_dept_raises_key_error(self) -> None:
        policy, _, _ = _make_policy()
        with pytest.raises(KeyError):
            await policy.enforce(dept_id="unknown", user_id=None)


# ---------------------------------------------------------------------------
# deny_response_body helper
# ---------------------------------------------------------------------------


class TestDenyResponseBody:
    def test_renders_scope_and_dept(self) -> None:
        body = deny_response_body(
            BudgetDecision.deny("user_weekly"), dept_id="payment"
        )
        assert body == {
            "error": "budget_exceeded",
            "dept_id": "payment",
            "scope": "user_weekly",
        }

    def test_rejects_allow_decision(self) -> None:
        with pytest.raises(ValueError, match="allow decision"):
            deny_response_body(BudgetDecision.allow(), dept_id="payment")
