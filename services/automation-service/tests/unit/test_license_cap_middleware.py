"""Unit tests for ``src.middleware.license_cap`` (R16 / Q20).

Validates: Requirements 16.3, 16.4, 16.5 (uyumluluk spec).

Covers the workflow-start guard contract:

* ``fetch_cap_for_dept`` returns the dept's cap row when one is
  assigned, falls back to :data:`DEFAULT_LICENSE_CAP` when
  ``license_id`` is NULL, and degrades gracefully when the dept
  itself does not exist.
* ``enforce_license_cap`` short-circuits on the *first* exceeded
  limit in the deterministic order ``concurrent`` → ``daily`` →
  ``monthly_token`` (Property 17), writes a
  ``bot_license_cap_exceeded`` audit row and raises
  :class:`BotLicenseCapExceededError`.
* The success path (every cap below threshold) returns ``None`` and
  writes **no** audit row.
* An audit-logger failure does not mask the rejection signal (best
  effort).
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
# Path setup
# ---------------------------------------------------------------------------
_SERVICE_ROOT = Path(__file__).resolve().parents[2] / "src"
_LIBS_AUDIT = (
    Path(__file__).resolve().parents[4] / "libs" / "audit_logger" / "src"
)
sys.path.insert(0, str(_SERVICE_ROOT))
sys.path.insert(0, str(_LIBS_AUDIT))

from middleware.license_cap import (  # noqa: E402
    DEFAULT_LICENSE_CAP,
    BotLicenseCapExceededError,
    LicenseCap,
    enforce_license_cap,
    fetch_cap_for_dept,
)


# ---------------------------------------------------------------------------
# Fakes — asyncpg + audit logger
# ---------------------------------------------------------------------------


@dataclass
class _RecordedQuery:
    sql: str
    args: tuple[Any, ...]


class _ScriptedConnection:
    """Returns rows from a per-test ``responses`` list in call order.

    Each entry is either a dict (returned by ``fetchrow``) or ``None``.
    The connection records every SQL call so tests can assert the
    deterministic check order.
    """

    def __init__(self, responses: list[dict[str, Any] | None]) -> None:
        self._responses = list(responses)
        self.calls: list[_RecordedQuery] = []

    async def fetchrow(
        self, sql: str, *args: Any
    ) -> dict[str, Any] | None:
        self.calls.append(_RecordedQuery(sql=sql, args=args))
        if not self._responses:
            return None
        return self._responses.pop(0)


class _FakePool:
    def __init__(self, conn: _ScriptedConnection) -> None:
        self._conn = conn

    def acquire(self) -> "_AcquireCM":
        return _AcquireCM(self._conn)


class _AcquireCM:
    def __init__(self, conn: _ScriptedConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _ScriptedConnection:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        return None


@dataclass
class _FakeAuditLogger:
    """Records every ``write`` call; honours an optional raise flag."""

    events: list[Any] = field(default_factory=list)
    raise_on_write: bool = False

    async def write(self, event: Any) -> None:
        if self.raise_on_write:
            raise RuntimeError("audit pipeline down")
        self.events.append(event)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FROZEN_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _frozen_clock() -> datetime:
    return _FROZEN_NOW


def _make_pool(*responses: dict[str, Any] | None) -> _FakePool:
    return _FakePool(_ScriptedConnection(list(responses)))


def _cap_row(
    *,
    license_id: str = "lic-test",
    max_concurrent: int = 10,
    max_daily: int = 100,
    max_monthly_usd: str = "1000.00",
) -> dict[str, Any]:
    return {
        "license_id": license_id,
        "max_concurrent_workflows": max_concurrent,
        "max_workflows_per_day": max_daily,
        "max_token_usd_per_month": Decimal(max_monthly_usd),
    }


def _no_license_row() -> dict[str, Any]:
    return {
        "license_id": None,
        "max_concurrent_workflows": None,
        "max_workflows_per_day": None,
        "max_token_usd_per_month": None,
    }


# ---------------------------------------------------------------------------
# fetch_cap_for_dept
# ---------------------------------------------------------------------------


class TestFetchCapForDept:
    """Resolves dept → :class:`LicenseCap` (R16.1, R16.2, default cap)."""

    @pytest.mark.asyncio
    async def test_returns_default_cap_when_dept_missing(self) -> None:
        pool = _make_pool(None)  # dept row not found
        cap = await fetch_cap_for_dept(pool, "missing-dept")
        assert cap == DEFAULT_LICENSE_CAP
        assert cap.license_id is None
        assert cap.max_concurrent_workflows == 10
        assert cap.max_workflows_per_day == 100
        assert cap.max_token_usd_per_month == Decimal("1000.00")

    @pytest.mark.asyncio
    async def test_returns_default_cap_when_no_license(self) -> None:
        """``license_id IS NULL`` falls back to defaults."""

        pool = _make_pool(_no_license_row())
        cap = await fetch_cap_for_dept(pool, "payments")
        assert cap.license_id is None
        assert cap.max_concurrent_workflows == 10
        assert cap.max_workflows_per_day == 100
        assert cap.max_token_usd_per_month == Decimal("1000.00")

    @pytest.mark.asyncio
    async def test_returns_resolved_cap_when_license_assigned(self) -> None:
        pool = _make_pool(
            _cap_row(
                license_id="enterprise-2025",
                max_concurrent=25,
                max_daily=500,
                max_monthly_usd="5000.00",
            )
        )
        cap = await fetch_cap_for_dept(pool, "payments")
        assert cap == LicenseCap(
            license_id="enterprise-2025",
            max_concurrent_workflows=25,
            max_workflows_per_day=500,
            max_token_usd_per_month=Decimal("5000.00"),
        )


# ---------------------------------------------------------------------------
# enforce_license_cap — happy path
# ---------------------------------------------------------------------------


class TestEnforceAllows:
    """Below-threshold usage → ``None`` return, no audit row."""

    @pytest.mark.asyncio
    async def test_allows_when_every_limit_below_cap(self) -> None:
        pool = _make_pool(
            _cap_row(),                     # cap fetch
            {"n": 1},                        # concurrent
            {"n": 5},                        # daily
            {"total": Decimal("12.50")},     # monthly token
        )
        audit = _FakeAuditLogger()

        result = await enforce_license_cap(
            dept_id="payments",
            db=pool,
            audit_logger=audit,
            issue_key="PAY-100",
            now=_frozen_clock,
        )

        assert result is None
        assert audit.events == []

    @pytest.mark.asyncio
    async def test_default_cap_path_uses_dept_scoped_queries(self) -> None:
        """``license_id IS NULL`` exercises the dept-scoped SQL branch."""

        pool = _make_pool(
            _no_license_row(),               # cap fetch (no license)
            {"n": 0},                        # concurrent (dept-scoped)
            {"n": 0},                        # daily (dept-scoped)
            {"total": None},                 # monthly token (no rows)
        )
        audit = _FakeAuditLogger()

        await enforce_license_cap(
            dept_id="payments",
            db=pool,
            audit_logger=audit,
            now=_frozen_clock,
        )

        # The three usage queries scoped on the single dept_id
        # rather than license_id.
        usage_calls = pool._conn.calls[1:]
        for call in usage_calls:
            assert "department_id" in call.sql or "c.dept_id" in call.sql


# ---------------------------------------------------------------------------
# enforce_license_cap — rejection paths
# ---------------------------------------------------------------------------


class TestEnforceRejects:
    """Each cap rejection writes the audit row with the right payload."""

    @pytest.mark.asyncio
    async def test_rejects_on_concurrent_first(self) -> None:
        """Concurrent = max → 429; daily / monthly never queried."""

        pool = _make_pool(
            _cap_row(max_concurrent=3),
            {"n": 3},  # concurrent at cap
        )
        audit = _FakeAuditLogger()

        with pytest.raises(BotLicenseCapExceededError) as exc_info:
            await enforce_license_cap(
                dept_id="payments",
                db=pool,
                audit_logger=audit,
                issue_key="PAY-7",
                now=_frozen_clock,
            )

        err = exc_info.value
        assert err.limit_type == "concurrent"
        assert err.current == 3
        assert err.max == 3
        assert err.license_id == "lic-test"
        assert err.dept_id == "payments"
        assert err.issue_key == "PAY-7"

        # Only the cap fetch + concurrent query happened.
        assert len(pool._conn.calls) == 2

        # Audit row recorded.
        assert len(audit.events) == 1
        event = audit.events[0]
        assert event.action == "bot_license_cap_exceeded"
        assert event.actor_role == "system"
        assert event.dept_id == "payments"
        assert event.result == "denied"
        assert event.payload["limit_type"] == "concurrent"
        assert event.payload["current_value"] == 3
        assert event.payload["max_value"] == 3
        assert event.payload["license_id"] == "lic-test"
        assert event.payload["issue_key"] == "PAY-7"

    @pytest.mark.asyncio
    async def test_rejects_on_daily_when_concurrent_ok(self) -> None:
        pool = _make_pool(
            _cap_row(max_daily=50),
            {"n": 0},   # concurrent ok
            {"n": 50},  # daily at cap
        )
        audit = _FakeAuditLogger()

        with pytest.raises(BotLicenseCapExceededError) as exc_info:
            await enforce_license_cap(
                dept_id="payments",
                db=pool,
                audit_logger=audit,
                now=_frozen_clock,
            )

        assert exc_info.value.limit_type == "daily"
        assert exc_info.value.current == 50
        assert exc_info.value.max == 50
        # Three SQL calls (cap, concurrent, daily) — no monthly query.
        assert len(pool._conn.calls) == 3
        assert audit.events[0].payload["limit_type"] == "daily"

    @pytest.mark.asyncio
    async def test_rejects_on_monthly_token_when_others_ok(self) -> None:
        pool = _make_pool(
            _cap_row(max_monthly_usd="500.00"),
            {"n": 0},                          # concurrent
            {"n": 0},                          # daily
            {"total": Decimal("500.00")},      # monthly at cap
        )
        audit = _FakeAuditLogger()

        with pytest.raises(BotLicenseCapExceededError) as exc_info:
            await enforce_license_cap(
                dept_id="payments",
                db=pool,
                audit_logger=audit,
                now=_frozen_clock,
            )

        err = exc_info.value
        assert err.limit_type == "monthly_token"
        assert err.current == Decimal("500.00")
        assert err.max == Decimal("500.00")
        # JSON payload coerces Decimals to floats.
        assert audit.events[0].payload["current_value"] == 500.0
        assert audit.events[0].payload["max_value"] == 500.0


# ---------------------------------------------------------------------------
# Audit best-effort
# ---------------------------------------------------------------------------


class TestAuditBestEffort:
    """An audit-write failure must not mask the rejection signal."""

    @pytest.mark.asyncio
    async def test_audit_failure_does_not_swallow_exception(self) -> None:
        pool = _make_pool(
            _cap_row(max_concurrent=1),
            {"n": 1},
        )
        audit = _FakeAuditLogger(raise_on_write=True)

        with pytest.raises(BotLicenseCapExceededError):
            await enforce_license_cap(
                dept_id="payments",
                db=pool,
                audit_logger=audit,
                now=_frozen_clock,
            )

    @pytest.mark.asyncio
    async def test_no_audit_logger_skips_write(self) -> None:
        pool = _make_pool(
            _cap_row(max_concurrent=1),
            {"n": 1},
        )

        with pytest.raises(BotLicenseCapExceededError):
            await enforce_license_cap(
                dept_id="payments",
                db=pool,
                audit_logger=None,
                now=_frozen_clock,
            )
