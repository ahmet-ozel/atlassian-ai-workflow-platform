"""Property test: Bot license cap monotonicity + enforcement.

For any license cap ``(max_concurrent, max_daily, max_monthly_token_usd)``
and usage state ``(curr_concurrent, curr_daily, curr_monthly_usd)`` pairs,
``enforce_license_cap(dept_id)`` behaviour satisfies:

(a) **Allow**: ``curr_concurrent < max_concurrent`` AND ``curr_daily < max_daily``
    AND ``curr_monthly_usd < max_monthly_token_usd``  call is a no-op (workflow
    start is permitted).

(b) **Reject**: at least one limit is met or exceeded (``>= max``)
    ``BotLicenseCapExceededError`` is raised + ``bot_license_cap_exceeded``
    audit is emitted + the error carries the correct ``limit_type``,
    ``current``, and ``max`` values.

(c) **Monotonicity**: for a single dimension (e.g. concurrent), the
    transition from "allowed" to "rejected" is one-way - once usage
    reaches the cap, no decrease in *other* dimensions can flip the
    decision back to "allowed" for that dimension.

(d) **Deterministic check order**: ``concurrent``  ``daily``
    ``monthly_token``. The *first* exceeded limit is reported; subsequent
    limits are not evaluated.

No real database is required; all SQL is intercepted by an in-memory
``asyncpg``-compatible fake pool.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))

from src.middleware.license_cap import (  # noqa: E402
    BotLicenseCapExceededError,
    DEFAULT_LICENSE_CAP,
    LicenseCap,
    enforce_license_cap,
)

# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------

_PROFILE = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# ---------------------------------------------------------------------------
# In-memory asyncpg fake
# ---------------------------------------------------------------------------


@dataclass
class _UsageState:
    """Mutable usage counters injected into the fake DB."""

    concurrent: int = 0
    daily: int = 0
    monthly_usd: Decimal = Decimal("0")
    license_id: str | None = None
    dept_id: str = "test-dept"


class _FakeConnection:
    """Minimal asyncpg connection fake that answers the three usage queries."""

    def __init__(self, state: _UsageState) -> None:
        self._state = state

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split()).lower()

        # Cap loader query (fetch_cap_for_dept)
        if "from automation.departments" in q and "bot_license_caps" in q:
            # Return NULL license_id  default cap will be used
            return {
                "license_id": self._state.license_id,
                "max_concurrent_workflows": None,
                "max_workflows_per_day": None,
                "max_token_usd_per_month": None,
            }

        # Concurrent count
        if "status = 'running'" in q:
            return {"n": self._state.concurrent}

        # Daily count
        if "created_at >=" in q and "cost_tracking" not in q:
            return {"n": self._state.daily}

        # Monthly token cost
        if "cost_tracking" in q or "cost_usd" in q:
            return {"total": self._state.monthly_usd}

        raise NotImplementedError(
            f"_FakeConnection.fetchrow: unsupported query: {query!r}"
        )


class _FakeAcquireCtx:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *_: object) -> bool:
        return False


class _FakePool:
    """asyncpg.Pool fake that routes all queries to _FakeConnection."""

    def __init__(self, state: _UsageState) -> None:
        self._state = state

    def acquire(self) -> _FakeAcquireCtx:
        return _FakeAcquireCtx(_FakeConnection(self._state))

# ---------------------------------------------------------------------------
# Fake audit logger
# ---------------------------------------------------------------------------


@dataclass
class _AuditEvent:
    action: str
    payload: dict[str, Any]


class _FakeAuditLogger:
    """Records audit events emitted by enforce_license_cap."""

    def __init__(self) -> None:
        self.events: list[_AuditEvent] = []

    async def write(self, event: Any) -> None:
        self.events.append(
            _AuditEvent(
                action=event.action,
                payload=event.payload,
            )
        )


# ---------------------------------------------------------------------------
# Helper: build a LicenseCap with explicit limits and wire a fake pool
# ---------------------------------------------------------------------------


def _make_pool_and_cap(
    *,
    max_concurrent: int,
    max_daily: int,
    max_monthly_usd: Decimal,
    curr_concurrent: int,
    curr_daily: int,
    curr_monthly_usd: Decimal,
    dept_id: str = "test-dept",
) -> tuple[_FakePool, LicenseCap]:
    """Return a fake pool pre-seeded with usage counters and the matching cap."""
    state = _UsageState(
        concurrent=curr_concurrent,
        daily=curr_daily,
        monthly_usd=curr_monthly_usd,
        license_id=None,
        dept_id=dept_id,
    )
    pool = _FakePool(state)
    cap = LicenseCap(
        license_id=None,
        max_concurrent_workflows=max_concurrent,
        max_workflows_per_day=max_daily,
        max_token_usd_per_month=max_monthly_usd,
    )
    return pool, cap


# ---------------------------------------------------------------------------
# Patched enforce_license_cap that uses an explicit cap (bypasses DB cap fetch)
# ---------------------------------------------------------------------------


async def _enforce_with_cap(
    *,
    cap: LicenseCap,
    pool: _FakePool,
    audit_logger: _FakeAuditLogger | None = None,
    dept_id: str = "test-dept",
    issue_key: str | None = None,
) -> None:
    """Call enforce_license_cap but inject a pre-built cap instead of DB lookup.

    We monkey-patch ``fetch_cap_for_dept`` on the module so the property
    tests exercise the enforcement logic without needing a real DB cap row.
    """
    import src.middleware.license_cap as _mod

    original = _mod.fetch_cap_for_dept

    async def _fake_fetch(db: Any, dept_id_: str) -> LicenseCap:  # noqa: ARG001
        return cap

    _mod.fetch_cap_for_dept = _fake_fetch  # type: ignore[assignment]
    try:
        await enforce_license_cap(
            dept_id=dept_id,
            db=pool,  # type: ignore[arg-type]
            audit_logger=audit_logger,
            issue_key=issue_key,
        )
    finally:
        _mod.fetch_cap_for_dept = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_SMALL_INT = st.integers(min_value=1, max_value=50)
_USAGE_INT = st.integers(min_value=0, max_value=60)
_SMALL_USD = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("2000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
_CAP_USD = st.decimals(
    min_value=Decimal("1"),
    max_value=Decimal("2000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)

# ---------------------------------------------------------------------------
# Allow when all usage < cap
# ---------------------------------------------------------------------------


class TestAllowWhenUnderCap:
    """``enforce_license_cap`` is a no-op when all usage is below cap."""

    @_PROFILE
    @given(
        max_concurrent=_SMALL_INT,
        max_daily=_SMALL_INT,
        max_monthly_usd=_CAP_USD,
        curr_concurrent=_USAGE_INT,
        curr_daily=_USAGE_INT,
        curr_monthly_usd=_SMALL_USD,
    )
    def test_no_exception_when_all_under_cap(
        self,
        max_concurrent: int,
        max_daily: int,
        max_monthly_usd: Decimal,
        curr_concurrent: int,
        curr_daily: int,
        curr_monthly_usd: Decimal,
    ) -> None:
        """No exception raised when every usage counter is strictly below its cap."""
        # Ensure all usage is strictly below cap
        curr_concurrent = min(curr_concurrent, max_concurrent - 1)
        curr_daily = min(curr_daily, max_daily - 1)
        curr_monthly_usd = min(curr_monthly_usd, max_monthly_usd - Decimal("0.01"))
        if curr_monthly_usd < Decimal("0"):
            curr_monthly_usd = Decimal("0")

        pool, cap = _make_pool_and_cap(
            max_concurrent=max_concurrent,
            max_daily=max_daily,
            max_monthly_usd=max_monthly_usd,
            curr_concurrent=curr_concurrent,
            curr_daily=curr_daily,
            curr_monthly_usd=curr_monthly_usd,
        )
        audit = _FakeAuditLogger()

        # Should not raise
        asyncio.run(
            _enforce_with_cap(cap=cap, pool=pool, audit_logger=audit)
        )

        # No audit event on the allow path
        assert audit.events == [], (
            f"Expected no audit events on allow path, got {audit.events!r}"
        )

# ---------------------------------------------------------------------------
# Reject when any usage >= cap
# ---------------------------------------------------------------------------


class TestRejectWhenAtOrOverCap:
    """``enforce_license_cap`` raises when any usage is at or above cap."""

    @_PROFILE
    @given(
        max_concurrent=_SMALL_INT,
        curr_concurrent=_USAGE_INT,
    )
    def test_concurrent_cap_exceeded_raises(
        self,
        max_concurrent: int,
        curr_concurrent: int,
    ) -> None:
        """Concurrent usage >= max_concurrent  BotLicenseCapExceededError."""
        curr_concurrent = max(curr_concurrent, max_concurrent)  # ensure >= cap

        pool, cap = _make_pool_and_cap(
            max_concurrent=max_concurrent,
            max_daily=100,
            max_monthly_usd=Decimal("1000"),
            curr_concurrent=curr_concurrent,
            curr_daily=0,
            curr_monthly_usd=Decimal("0"),
        )
        audit = _FakeAuditLogger()

        with pytest.raises(BotLicenseCapExceededError) as exc_info:
            asyncio.run(
                _enforce_with_cap(cap=cap, pool=pool, audit_logger=audit)
            )

        err = exc_info.value
        assert err.limit_type == "concurrent", (
            f"Expected limit_type='concurrent', got {err.limit_type!r}"
        )
        assert err.current == curr_concurrent
        assert err.max == max_concurrent
        assert len(audit.events) == 1
        assert audit.events[0].action == "bot_license_cap_exceeded"
        assert audit.events[0].payload["limit_type"] == "concurrent"

    @_PROFILE
    @given(
        max_daily=_SMALL_INT,
        curr_daily=_USAGE_INT,
    )
    def test_daily_cap_exceeded_raises(
        self,
        max_daily: int,
        curr_daily: int,
    ) -> None:
        """Daily usage >= max_daily  BotLicenseCapExceededError (concurrent OK)."""
        curr_daily = max(curr_daily, max_daily)  # ensure >= cap

        pool, cap = _make_pool_and_cap(
            max_concurrent=100,
            max_daily=max_daily,
            max_monthly_usd=Decimal("1000"),
            curr_concurrent=0,  # concurrent is fine
            curr_daily=curr_daily,
            curr_monthly_usd=Decimal("0"),
        )
        audit = _FakeAuditLogger()

        with pytest.raises(BotLicenseCapExceededError) as exc_info:
            asyncio.run(
                _enforce_with_cap(cap=cap, pool=pool, audit_logger=audit)
            )

        err = exc_info.value
        assert err.limit_type == "daily", (
            f"Expected limit_type='daily', got {err.limit_type!r}"
        )
        assert err.current == curr_daily
        assert err.max == max_daily
        assert len(audit.events) == 1
        assert audit.events[0].payload["limit_type"] == "daily"

    @_PROFILE
    @given(
        max_monthly_usd=_CAP_USD,
        curr_monthly_usd=_SMALL_USD,
    )
    def test_monthly_token_cap_exceeded_raises(
        self,
        max_monthly_usd: Decimal,
        curr_monthly_usd: Decimal,
    ) -> None:
        """Monthly token cost >= max  BotLicenseCapExceededError (others OK)."""
        curr_monthly_usd = max(curr_monthly_usd, max_monthly_usd)  # ensure >= cap

        pool, cap = _make_pool_and_cap(
            max_concurrent=100,
            max_daily=100,
            max_monthly_usd=max_monthly_usd,
            curr_concurrent=0,
            curr_daily=0,
            curr_monthly_usd=curr_monthly_usd,
        )
        audit = _FakeAuditLogger()

        with pytest.raises(BotLicenseCapExceededError) as exc_info:
            asyncio.run(
                _enforce_with_cap(cap=cap, pool=pool, audit_logger=audit)
            )

        err = exc_info.value
        assert err.limit_type == "monthly_token", (
            f"Expected limit_type='monthly_token', got {err.limit_type!r}"
        )
        assert err.current >= max_monthly_usd
        assert err.max == max_monthly_usd
        assert len(audit.events) == 1
        assert audit.events[0].payload["limit_type"] == "monthly_token"

# ---------------------------------------------------------------------------
# Deterministic check order: concurrent  daily  monthly_token
# ---------------------------------------------------------------------------


class TestDeterministicCheckOrder:
    """``limit_type`` check order is concurrent  daily  monthly_token.

    When multiple limits are simultaneously exceeded, the *first* one in
    the canonical order is reported.
    """

    def test_concurrent_wins_over_daily_and_monthly(self) -> None:
        """When concurrent AND daily AND monthly are all exceeded, concurrent wins."""
        pool, cap = _make_pool_and_cap(
            max_concurrent=5,
            max_daily=10,
            max_monthly_usd=Decimal("100"),
            curr_concurrent=5,   # at cap
            curr_daily=10,       # at cap
            curr_monthly_usd=Decimal("100"),  # at cap
        )

        with pytest.raises(BotLicenseCapExceededError) as exc_info:
            asyncio.run(_enforce_with_cap(cap=cap, pool=pool))

        assert exc_info.value.limit_type == "concurrent"

    def test_daily_wins_over_monthly_when_concurrent_ok(self) -> None:
        """When daily AND monthly are exceeded but concurrent is fine, daily wins."""
        pool, cap = _make_pool_and_cap(
            max_concurrent=100,
            max_daily=10,
            max_monthly_usd=Decimal("100"),
            curr_concurrent=0,   # fine
            curr_daily=10,       # at cap
            curr_monthly_usd=Decimal("100"),  # at cap
        )

        with pytest.raises(BotLicenseCapExceededError) as exc_info:
            asyncio.run(_enforce_with_cap(cap=cap, pool=pool))

        assert exc_info.value.limit_type == "daily"

    def test_monthly_reported_when_only_monthly_exceeded(self) -> None:
        """When only monthly is exceeded, monthly_token is reported."""
        pool, cap = _make_pool_and_cap(
            max_concurrent=100,
            max_daily=100,
            max_monthly_usd=Decimal("100"),
            curr_concurrent=0,
            curr_daily=0,
            curr_monthly_usd=Decimal("100"),  # at cap
        )

        with pytest.raises(BotLicenseCapExceededError) as exc_info:
            asyncio.run(_enforce_with_cap(cap=cap, pool=pool))

        assert exc_info.value.limit_type == "monthly_token"

    @_PROFILE
    @given(
        max_concurrent=_SMALL_INT,
        max_daily=_SMALL_INT,
        max_monthly_usd=_CAP_USD,
        curr_concurrent=_USAGE_INT,
        curr_daily=_USAGE_INT,
        curr_monthly_usd=_SMALL_USD,
    )
    def test_first_exceeded_limit_is_always_reported(
        self,
        max_concurrent: int,
        max_daily: int,
        max_monthly_usd: Decimal,
        curr_concurrent: int,
        curr_daily: int,
        curr_monthly_usd: Decimal,
    ) -> None:
        """The reported limit_type is always the first exceeded in canonical order."""
        pool, cap = _make_pool_and_cap(
            max_concurrent=max_concurrent,
            max_daily=max_daily,
            max_monthly_usd=max_monthly_usd,
            curr_concurrent=curr_concurrent,
            curr_daily=curr_daily,
            curr_monthly_usd=curr_monthly_usd,
        )

        concurrent_exceeded = curr_concurrent >= max_concurrent
        daily_exceeded = curr_daily >= max_daily
        monthly_exceeded = curr_monthly_usd >= max_monthly_usd

        any_exceeded = concurrent_exceeded or daily_exceeded or monthly_exceeded

        if not any_exceeded:
            # Should not raise
            asyncio.run(_enforce_with_cap(cap=cap, pool=pool))
            return

        with pytest.raises(BotLicenseCapExceededError) as exc_info:
            asyncio.run(_enforce_with_cap(cap=cap, pool=pool))

        err = exc_info.value

        # Determine expected limit_type by canonical order
        if concurrent_exceeded:
            expected = "concurrent"
        elif daily_exceeded:
            expected = "daily"
        else:
            expected = "monthly_token"

        assert err.limit_type == expected, (
            f"Expected first exceeded limit={expected!r} but got {err.limit_type!r}. "
            f"concurrent_exceeded={concurrent_exceeded}, daily_exceeded={daily_exceeded}, "
            f"monthly_exceeded={monthly_exceeded}"
        )

# ---------------------------------------------------------------------------
# Monotonicity invariant
# ---------------------------------------------------------------------------


class TestMonotonicity:
    """Monotonicity: the allowed  rejected transition is one-way.

    For a fixed cap, if usage N is rejected, then usage N+1 must also be
    rejected (for the same dimension). The decision never flips back from
    "rejected" to "allowed" as usage increases.
    """

    @_PROFILE
    @given(
        max_concurrent=_SMALL_INT,
        base_concurrent=st.integers(min_value=0, max_value=49),
    )
    def test_concurrent_monotonicity(
        self,
        max_concurrent: int,
        base_concurrent: int,
    ) -> None:
        """If concurrent=N is rejected, concurrent=N+1 is also rejected."""
        # Find a value at or above the cap
        at_cap = max(base_concurrent, max_concurrent)

        pool_at, cap = _make_pool_and_cap(
            max_concurrent=max_concurrent,
            max_daily=100,
            max_monthly_usd=Decimal("1000"),
            curr_concurrent=at_cap,
            curr_daily=0,
            curr_monthly_usd=Decimal("0"),
        )
        pool_above, _ = _make_pool_and_cap(
            max_concurrent=max_concurrent,
            max_daily=100,
            max_monthly_usd=Decimal("1000"),
            curr_concurrent=at_cap + 1,
            curr_daily=0,
            curr_monthly_usd=Decimal("0"),
        )

        # Both at_cap and at_cap+1 must be rejected
        with pytest.raises(BotLicenseCapExceededError):
            asyncio.run(_enforce_with_cap(cap=cap, pool=pool_at))

        with pytest.raises(BotLicenseCapExceededError):
            asyncio.run(_enforce_with_cap(cap=cap, pool=pool_above))

    @_PROFILE
    @given(
        max_concurrent=_SMALL_INT,
        below_concurrent=st.integers(min_value=0, max_value=49),
    )
    def test_below_cap_is_always_allowed_for_concurrent(
        self,
        max_concurrent: int,
        below_concurrent: int,
    ) -> None:
        """If concurrent < max_concurrent (and others are 0), call is allowed."""
        below_concurrent = min(below_concurrent, max_concurrent - 1)

        pool, cap = _make_pool_and_cap(
            max_concurrent=max_concurrent,
            max_daily=100,
            max_monthly_usd=Decimal("1000"),
            curr_concurrent=below_concurrent,
            curr_daily=0,
            curr_monthly_usd=Decimal("0"),
        )

        # Should not raise
        asyncio.run(_enforce_with_cap(cap=cap, pool=pool))

    @_PROFILE
    @given(
        max_concurrent=_SMALL_INT,
        curr_concurrent=_USAGE_INT,
    )
    def test_same_inputs_same_outcome_concurrent(
        self,
        max_concurrent: int,
        curr_concurrent: int,
    ) -> None:
        """Determinism: same (cap, usage) always produces the same outcome."""
        pool1, cap = _make_pool_and_cap(
            max_concurrent=max_concurrent,
            max_daily=100,
            max_monthly_usd=Decimal("1000"),
            curr_concurrent=curr_concurrent,
            curr_daily=0,
            curr_monthly_usd=Decimal("0"),
        )
        pool2, _ = _make_pool_and_cap(
            max_concurrent=max_concurrent,
            max_daily=100,
            max_monthly_usd=Decimal("1000"),
            curr_concurrent=curr_concurrent,
            curr_daily=0,
            curr_monthly_usd=Decimal("0"),
        )

        raised1 = False
        raised2 = False

        try:
            asyncio.run(_enforce_with_cap(cap=cap, pool=pool1))
        except BotLicenseCapExceededError:
            raised1 = True

        try:
            asyncio.run(_enforce_with_cap(cap=cap, pool=pool2))
        except BotLicenseCapExceededError:
            raised2 = True

        assert raised1 == raised2, (
            f"Non-determinism: run1 raised={raised1}, run2 raised={raised2} "
            f"for max_concurrent={max_concurrent}, curr_concurrent={curr_concurrent}"
        )

# ---------------------------------------------------------------------------
# Audit payload correctness
# ---------------------------------------------------------------------------


class TestAuditPayload:
    """Audit event payload is correct on rejection."""

    @_PROFILE
    @given(
        max_concurrent=_SMALL_INT,
        curr_concurrent=_USAGE_INT,
        issue_key=st.one_of(
            st.none(),
            st.from_regex(r"[A-Z]{2,6}-\d{1,5}", fullmatch=True),
        ),
    )
    def test_audit_payload_fields_on_concurrent_exceeded(
        self,
        max_concurrent: int,
        curr_concurrent: int,
        issue_key: str | None,
    ) -> None:
        """Audit event carries correct limit_type, current_value, max_value, dept_id, issue_key."""
        curr_concurrent = max(curr_concurrent, max_concurrent)  # ensure >= cap
        dept_id = "audit-test-dept"

        pool, cap = _make_pool_and_cap(
            max_concurrent=max_concurrent,
            max_daily=100,
            max_monthly_usd=Decimal("1000"),
            curr_concurrent=curr_concurrent,
            curr_daily=0,
            curr_monthly_usd=Decimal("0"),
            dept_id=dept_id,
        )
        audit = _FakeAuditLogger()

        with pytest.raises(BotLicenseCapExceededError):
            asyncio.run(
                _enforce_with_cap(
                    cap=cap,
                    pool=pool,
                    audit_logger=audit,
                    dept_id=dept_id,
                    issue_key=issue_key,
                )
            )

        assert len(audit.events) == 1
        ev = audit.events[0]
        assert ev.action == "bot_license_cap_exceeded"
        payload = ev.payload
        assert payload["limit_type"] == "concurrent"
        assert payload["current_value"] == curr_concurrent
        assert payload["max_value"] == max_concurrent
        assert payload["dept_id"] == dept_id
        assert payload["issue_key"] == issue_key

    def test_no_audit_on_allow_path(self) -> None:
        """No audit event is emitted when all limits are under cap."""
        pool, cap = _make_pool_and_cap(
            max_concurrent=10,
            max_daily=100,
            max_monthly_usd=Decimal("1000"),
            curr_concurrent=0,
            curr_daily=0,
            curr_monthly_usd=Decimal("0"),
        )
        audit = _FakeAuditLogger()

        asyncio.run(_enforce_with_cap(cap=cap, pool=pool, audit_logger=audit))

        assert audit.events == []

    def test_exactly_one_audit_event_per_rejection(self) -> None:
        """Exactly one audit event is emitted per rejected call (not multiple)."""
        pool, cap = _make_pool_and_cap(
            max_concurrent=5,
            max_daily=10,
            max_monthly_usd=Decimal("100"),
            curr_concurrent=5,   # concurrent at cap
            curr_daily=10,       # daily also at cap
            curr_monthly_usd=Decimal("100"),  # monthly also at cap
        )
        audit = _FakeAuditLogger()

        with pytest.raises(BotLicenseCapExceededError):
            asyncio.run(_enforce_with_cap(cap=cap, pool=pool, audit_logger=audit))

        # Only one audit event - for the first exceeded limit (concurrent)
        assert len(audit.events) == 1
        assert audit.events[0].payload["limit_type"] == "concurrent"

# ---------------------------------------------------------------------------
# Default cap sentinel
# ---------------------------------------------------------------------------


class TestDefaultCapSentinel:
    """``DEFAULT_LICENSE_CAP`` values match design defaults."""

    def test_default_cap_values(self) -> None:
        """DEFAULT_LICENSE_CAP matches the design-specified defaults."""
        assert DEFAULT_LICENSE_CAP.license_id is None
        assert DEFAULT_LICENSE_CAP.max_concurrent_workflows == 10
        assert DEFAULT_LICENSE_CAP.max_workflows_per_day == 100
        assert DEFAULT_LICENSE_CAP.max_token_usd_per_month == Decimal("1000.00")

    def test_default_cap_allows_zero_usage(self) -> None:
        """With zero usage, the default cap always allows a workflow start."""
        pool, _ = _make_pool_and_cap(
            max_concurrent=DEFAULT_LICENSE_CAP.max_concurrent_workflows,
            max_daily=DEFAULT_LICENSE_CAP.max_workflows_per_day,
            max_monthly_usd=DEFAULT_LICENSE_CAP.max_token_usd_per_month,
            curr_concurrent=0,
            curr_daily=0,
            curr_monthly_usd=Decimal("0"),
        )
        # Should not raise
        asyncio.run(
            _enforce_with_cap(cap=DEFAULT_LICENSE_CAP, pool=pool)
        )

    def test_default_cap_rejects_at_concurrent_limit(self) -> None:
        """Default cap rejects when concurrent usage reaches 10."""
        pool, _ = _make_pool_and_cap(
            max_concurrent=DEFAULT_LICENSE_CAP.max_concurrent_workflows,
            max_daily=DEFAULT_LICENSE_CAP.max_workflows_per_day,
            max_monthly_usd=DEFAULT_LICENSE_CAP.max_token_usd_per_month,
            curr_concurrent=10,  # at default cap
            curr_daily=0,
            curr_monthly_usd=Decimal("0"),
        )

        with pytest.raises(BotLicenseCapExceededError) as exc_info:
            asyncio.run(
                _enforce_with_cap(cap=DEFAULT_LICENSE_CAP, pool=pool)
            )

        assert exc_info.value.limit_type == "concurrent"
        assert exc_info.value.max == 10
