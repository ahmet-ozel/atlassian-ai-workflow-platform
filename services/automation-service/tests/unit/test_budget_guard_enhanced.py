"""Unit tests for the enhanced Budget Guard.

Tests cover:
* :func:`check_budget` - pre-workflow check with 90% threshold warnings
* :func:`pre_llm_budget_guard` - inline guard before LLM calls
* :func:`get_budget_usage_snapshot` - Admin Dashboard data exposure
* :func:`configuration_error_response` - undefined dept_id handling
* :class:`BudgetCheckResult` - dataclass invariants
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
# Path setup - mirrors test_budget_policy.py
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
    BudgetCapPolicy,
    BudgetCaps,
    BudgetCheckResult,
    StaticBudgetCapsProvider,
    WARNING_THRESHOLD,
    check_budget,
    configuration_error_response,
    get_budget_usage_snapshot,
    pre_llm_budget_guard,
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
    """In-memory fetchval fake returning configured usage values."""

    dept_weekly: Decimal = Decimal("0")
    dept_monthly: Decimal = Decimal("0")
    user_weekly: Decimal = Decimal("0")
    user_monthly: Decimal = Decimal("0")
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    async def fetchval(self, query: str, *args: Any) -> Decimal:
        self.calls.append((query, args))
        is_user_scope = "user_id = $3" in query
        interval = args[1] if len(args) >= 2 else ""

        if is_user_scope:
            if "7 days" in interval:
                return self.user_weekly
            return self.user_monthly
        else:
            if "7 days" in interval:
                return self.dept_weekly
            return self.dept_monthly


@dataclass
class _JiraCommentRecorder:
    """Records Jira comments posted by check_budget."""

    comments: list[tuple[str, str]] = field(default_factory=list)

    async def __call__(self, issue_key: str, body: str) -> None:
        self.comments.append((issue_key, body))


def _fixed_clock() -> datetime:
    return datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _make_policy(
    *,
    caps: dict[str, BudgetCaps],
    usage_runner: _FakeUsageRunner,
) -> BudgetCapPolicy:
    """Build a BudgetCapPolicy with in-memory fakes."""
    audit_writer = _RecordingAuditWriter()
    audit_logger = AuditLogger(writer=audit_writer)
    return BudgetCapPolicy(
        caps_provider=StaticBudgetCapsProvider(caps=caps),
        usage_query=usage_runner,
        audit_logger=audit_logger,
        clock=_fixed_clock,
    )


# ---------------------------------------------------------------------------
# Default caps for tests
# ---------------------------------------------------------------------------

_DEFAULT_CAPS = BudgetCaps(
    weekly_usd_dept=Decimal("1000"),
    weekly_usd_user=Decimal("200"),
    monthly_usd_dept=Decimal("4000"),
    monthly_usd_user=Decimal("800"),
)


# ===========================================================================
# Tests: BudgetCheckResult dataclass
# ===========================================================================


class TestBudgetCheckResult:
    def test_allowed_result(self) -> None:
        result = BudgetCheckResult(
            allowed=True,
            exceeded_scope=None,
            warning_scopes=["dept_weekly"],
            current_usage={"dept_weekly": "900"},
        )
        assert result.allowed is True
        assert result.exceeded_scope is None
        assert result.warning_scopes == ["dept_weekly"]

    def test_denied_result(self) -> None:
        result = BudgetCheckResult(
            allowed=False,
            exceeded_scope="dept_weekly",
            warning_scopes=[],
            current_usage={"dept_weekly": "1000"},
        )
        assert result.allowed is False
        assert result.exceeded_scope == "dept_weekly"

    def test_frozen(self) -> None:
        result = BudgetCheckResult(
            allowed=True,
            exceeded_scope=None,
            warning_scopes=[],
            current_usage={},
        )
        with pytest.raises(AttributeError):
            result.allowed = False  # type: ignore[misc]


# ===========================================================================
# Tests: check_budget
# ===========================================================================


class TestCheckBudget:
    @pytest.mark.asyncio
    async def test_allows_when_below_90_percent(self) -> None:
        """Usage below 90% → allowed, no warnings."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("500"),
            dept_monthly=Decimal("2000"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        result = await check_budget(
            "eng", None, "ENG-123", policy=policy
        )
        assert result.allowed is True
        assert result.exceeded_scope is None
        assert result.warning_scopes == []

    @pytest.mark.asyncio
    async def test_warns_at_90_percent_threshold(self) -> None:
        """Usage at 90% → allowed with warning scopes."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("900"),  # 90% of 1000
            dept_monthly=Decimal("2000"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        jira_recorder = _JiraCommentRecorder()
        result = await check_budget(
            "eng", None, "ENG-123",
            policy=policy,
            jira_comment_callback=jira_recorder,
        )
        assert result.allowed is True
        assert result.exceeded_scope is None
        assert "dept_weekly" in result.warning_scopes

    @pytest.mark.asyncio
    async def test_warning_comment_posted_to_jira(self) -> None:
        """90% threshold triggers a Jira warning comment."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("950"),  # > 90% of 1000
            dept_monthly=Decimal("2000"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        jira_recorder = _JiraCommentRecorder()
        await check_budget(
            "eng", None, "ENG-123",
            policy=policy,
            jira_comment_callback=jira_recorder,
        )
        assert len(jira_recorder.comments) == 1
        issue_key, body = jira_recorder.comments[0]
        assert issue_key == "ENG-123"
        assert "dept_weekly" in body
        assert "%90" in body

    @pytest.mark.asyncio
    async def test_denies_when_limit_exceeded(self) -> None:
        """Usage >= cap → denied with exceeded scope."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("1000"),  # == cap
            dept_monthly=Decimal("2000"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        result = await check_budget(
            "eng", None, "ENG-123", policy=policy
        )
        assert result.allowed is False
        assert result.exceeded_scope == "dept_weekly"

    @pytest.mark.asyncio
    async def test_denial_posts_jira_comment(self) -> None:
        """Limit exceeded triggers a denial Jira comment."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("1100"),  # > cap
            dept_monthly=Decimal("2000"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        jira_recorder = _JiraCommentRecorder()
        await check_budget(
            "eng", None, "ENG-123",
            policy=policy,
            jira_comment_callback=jira_recorder,
        )
        assert len(jira_recorder.comments) == 1
        _, body = jira_recorder.comments[0]
        assert "dept_weekly" in body
        assert "reddedildi" in body

    @pytest.mark.asyncio
    async def test_undefined_dept_id_returns_configuration_error(self) -> None:
        """Unknown dept_id → denied with configuration_error scope."""
        usage_runner = _FakeUsageRunner()
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        result = await check_budget(
            "unknown_dept", None, "UNK-1", policy=policy
        )
        assert result.allowed is False
        assert result.exceeded_scope == "configuration_error"
        assert result.current_usage == {}

    @pytest.mark.asyncio
    async def test_user_scope_warning(self) -> None:
        """User-scoped 90% threshold is detected."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("500"),
            dept_monthly=Decimal("2000"),
            user_weekly=Decimal("185"),  # > 90% of 200
            user_monthly=Decimal("400"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        result = await check_budget(
            "eng", "user-42", "ENG-123", policy=policy
        )
        assert result.allowed is True
        assert "user_weekly" in result.warning_scopes

    @pytest.mark.asyncio
    async def test_user_scope_exceeded(self) -> None:
        """User-scoped limit exceeded → denied."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("500"),
            dept_monthly=Decimal("2000"),
            user_weekly=Decimal("200"),  # == cap
            user_monthly=Decimal("400"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        result = await check_budget(
            "eng", "user-42", "ENG-123", policy=policy
        )
        assert result.allowed is False
        assert result.exceeded_scope == "user_weekly"

    @pytest.mark.asyncio
    async def test_current_usage_populated(self) -> None:
        """current_usage dict contains all scope values."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("100"),
            dept_monthly=Decimal("300"),
            user_weekly=Decimal("50"),
            user_monthly=Decimal("150"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        result = await check_budget(
            "eng", "user-1", "ENG-1", policy=policy
        )
        assert result.current_usage["dept_weekly"] == "100"
        assert result.current_usage["dept_monthly"] == "300"
        assert result.current_usage["user_weekly"] == "50"
        assert result.current_usage["user_monthly"] == "150"

    @pytest.mark.asyncio
    async def test_jira_comment_failure_does_not_block(self) -> None:
        """Jira comment failure is swallowed (best-effort)."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("950"),  # > 90%
            dept_monthly=Decimal("2000"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )

        async def _failing_callback(issue_key: str, body: str) -> None:
            raise RuntimeError("MCP unavailable")

        # Should not raise
        result = await check_budget(
            "eng", None, "ENG-123",
            policy=policy,
            jira_comment_callback=_failing_callback,
        )
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_empty_dept_id_raises(self) -> None:
        """Empty dept_id raises ValueError."""
        usage_runner = _FakeUsageRunner()
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        with pytest.raises(ValueError, match="dept_id"):
            await check_budget("", None, "ENG-1", policy=policy)


# ===========================================================================
# Tests: pre_llm_budget_guard
# ===========================================================================


class TestPreLlmBudgetGuard:
    @pytest.mark.asyncio
    async def test_allows_when_below_cap(self) -> None:
        """All scopes below cap → returns True."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("500"),
            dept_monthly=Decimal("2000"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        allowed = await pre_llm_budget_guard("eng", None, policy=policy)
        assert allowed is True

    @pytest.mark.asyncio
    async def test_blocks_when_dept_weekly_exceeded(self) -> None:
        """dept_weekly exceeded → returns False."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("1000"),  # == cap
            dept_monthly=Decimal("2000"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        allowed = await pre_llm_budget_guard("eng", None, policy=policy)
        assert allowed is False

    @pytest.mark.asyncio
    async def test_blocks_when_user_weekly_exceeded(self) -> None:
        """user_weekly exceeded → returns False."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("500"),
            dept_monthly=Decimal("2000"),
            user_weekly=Decimal("200"),  # == cap
            user_monthly=Decimal("400"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        allowed = await pre_llm_budget_guard("eng", "user-1", policy=policy)
        assert allowed is False

    @pytest.mark.asyncio
    async def test_blocks_when_dept_monthly_exceeded(self) -> None:
        """dept_monthly exceeded → returns False."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("500"),
            dept_monthly=Decimal("4000"),  # == cap
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        allowed = await pre_llm_budget_guard("eng", None, policy=policy)
        assert allowed is False

    @pytest.mark.asyncio
    async def test_blocks_when_user_monthly_exceeded(self) -> None:
        """user_monthly exceeded → returns False."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("500"),
            dept_monthly=Decimal("2000"),
            user_weekly=Decimal("100"),
            user_monthly=Decimal("800"),  # == cap
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        allowed = await pre_llm_budget_guard("eng", "user-1", policy=policy)
        assert allowed is False

    @pytest.mark.asyncio
    async def test_undefined_dept_blocks(self) -> None:
        """Unknown dept_id → returns False (fail-closed)."""
        usage_runner = _FakeUsageRunner()
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        allowed = await pre_llm_budget_guard("unknown", None, policy=policy)
        assert allowed is False

    @pytest.mark.asyncio
    async def test_empty_dept_id_blocks(self) -> None:
        """Empty dept_id → returns False."""
        usage_runner = _FakeUsageRunner()
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        allowed = await pre_llm_budget_guard("", None, policy=policy)
        assert allowed is False

    @pytest.mark.asyncio
    async def test_rechecks_live_data(self) -> None:
        """Guard queries cost_tracking table (verifiable via calls)."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("500"),
            dept_monthly=Decimal("2000"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        await pre_llm_budget_guard("eng", None, policy=policy)
        # Should have issued SQL queries against cost_tracking
        assert len(usage_runner.calls) >= 2
        for query, _ in usage_runner.calls:
            assert "shared.cost_tracking" in query
            assert "cost_tag = 'production'" in query


# ===========================================================================
# Tests: get_budget_usage_snapshot
# ===========================================================================


class TestGetBudgetUsageSnapshot:
    @pytest.mark.asyncio
    async def test_returns_caps_and_usage(self) -> None:
        """Snapshot includes caps, usage, and percentages."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("500"),
            dept_monthly=Decimal("2000"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        snapshot = await get_budget_usage_snapshot("eng", None, policy=policy)
        assert snapshot["dept_id"] == "eng"
        assert snapshot["caps"]["dept_weekly"] == "1000"
        assert snapshot["caps"]["dept_monthly"] == "4000"
        assert snapshot["usage"]["dept_weekly"] == "500"
        assert snapshot["usage"]["dept_monthly"] == "2000"

    @pytest.mark.asyncio
    async def test_percentages_calculated(self) -> None:
        """Percentages are correctly computed."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("500"),  # 50% of 1000
            dept_monthly=Decimal("2000"),  # 50% of 4000
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        snapshot = await get_budget_usage_snapshot("eng", None, policy=policy)
        # 500/1000 * 100 = 50
        assert "50" in snapshot["percentages"]["dept_weekly"]

    @pytest.mark.asyncio
    async def test_warning_scopes_included(self) -> None:
        """Scopes at 90%+ are listed in warning_scopes."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("950"),  # 95% of 1000
            dept_monthly=Decimal("2000"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        snapshot = await get_budget_usage_snapshot("eng", None, policy=policy)
        assert "dept_weekly" in snapshot["warning_scopes"]

    @pytest.mark.asyncio
    async def test_unknown_dept_raises_key_error(self) -> None:
        """Unknown dept_id raises KeyError."""
        usage_runner = _FakeUsageRunner()
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        with pytest.raises(KeyError):
            await get_budget_usage_snapshot("unknown", None, policy=policy)

    @pytest.mark.asyncio
    async def test_user_scoped_data_included(self) -> None:
        """User-scoped data is included when user_id is provided."""
        usage_runner = _FakeUsageRunner(
            dept_weekly=Decimal("500"),
            dept_monthly=Decimal("2000"),
            user_weekly=Decimal("100"),
            user_monthly=Decimal("400"),
        )
        policy = _make_policy(
            caps={"eng": _DEFAULT_CAPS},
            usage_runner=usage_runner,
        )
        snapshot = await get_budget_usage_snapshot(
            "eng", "user-1", policy=policy
        )
        assert "user_weekly" in snapshot["percentages"]
        assert "user_monthly" in snapshot["percentages"]


# ===========================================================================
# Tests: configuration_error_response
# ===========================================================================


class TestConfigurationErrorResponse:
    def test_renders_dept_id(self) -> None:
        """Response includes the unknown dept_id."""
        resp = configuration_error_response(dept_id="unknown_dept")
        assert resp["error"] == "configuration_error"
        assert resp["dept_id"] == "unknown_dept"
        assert "unknown_dept" in resp["message"]

    def test_message_mentions_departments_json(self) -> None:
        """Response message references the config file."""
        resp = configuration_error_response(dept_id="foo")
        assert "departments.json" in resp["message"]
