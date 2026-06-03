"""Unit tests for ``concurrency``.

Covers:
* :func:`check_dept_concurrency` allow / reject behaviour for each
  combination of ``max_concurrent_workflows`` (None / set) and
  observed count.
* Temporal Visibility primary path + Postgres fallback when
  Visibility raises.
* :func:`extract_max_concurrent` parsing edge cases.

"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

# Path setup — ensure src/ is importable
_TESTS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _TESTS_DIR.parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from concurrency import (  # noqa: E402
    AUTOMATION_WORKFLOW_TYPE,
    ConcurrencyLimitExceeded,
    check_dept_concurrency,
    count_active_workflows,
    extract_max_concurrent,
)


# ---------------------------------------------------------------------------
# Fakes (scoped down — full asyncpg fakes live in test_webhooks_dispatcher.py)
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, count: int) -> None:
        self._count = count
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(
        self, query: str, *args: Any
    ) -> dict[str, Any] | None:
        self.queries.append((query, args))
        return {"n": self._count}


class _FakePool:
    def __init__(self, count: int) -> None:
        self._conn = _FakeConn(count)

    def acquire(self) -> "_FakePoolCtx":
        return _FakePoolCtx(self._conn)


class _FakePoolCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeTemporalVisibility:
    def __init__(
        self, *, count: int = 0, raises: Exception | None = None
    ) -> None:
        self._count = count
        self._raises = raises
        self.queries: list[str | None] = []

    async def count_workflows(self, query: str | None = None) -> Any:
        self.queries.append(query)
        if self._raises is not None:
            raise self._raises

        class _Result:
            def __init__(self, count: int) -> None:
                self.count = count

        return _Result(self._count)


# ---------------------------------------------------------------------------
# extract_max_concurrent
# ---------------------------------------------------------------------------


class TestExtractMaxConcurrent:
    """``extract_max_concurrent`` — parsing edge cases."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            ({"max_concurrent_workflows": 5}, 5),
            ({"max_concurrent_workflows": 1}, 1),
            ({"max_concurrent_workflows": 50}, 50),
            ({"max_concurrent_workflows": None}, None),
            ({}, None),
            ({"max_concurrent_workflows": 0}, None),  # < 1
            ({"max_concurrent_workflows": -3}, None),
            ({"max_concurrent_workflows": True}, None),  # bool != int
            ({"max_concurrent_workflows": "10"}, None),  # str
            ({"max_concurrent_workflows": 10.5}, None),  # float
            (None, None),
            ("not-a-dict", None),
        ],
    )
    def test_parses(self, value: Any, expected: int | None) -> None:
        assert extract_max_concurrent(value) == expected


# ---------------------------------------------------------------------------
# count_active_workflows — Temporal primary + Postgres fallback
# ---------------------------------------------------------------------------


class TestCountActiveWorkflows:
    @pytest.mark.asyncio
    async def test_temporal_primary_returns_count(self) -> None:
        pool = _FakePool(count=99)  # should not be touched
        temporal = _FakeTemporalVisibility(count=4)

        count, source = await count_active_workflows(
            dept_id="payment", db=pool, temporal=temporal
        )
        assert count == 4
        assert source == "temporal"
        # Verify the query mentions all three discriminators.
        assert len(temporal.queries) == 1
        q = temporal.queries[0]
        assert q is not None
        assert AUTOMATION_WORKFLOW_TYPE in q
        assert "Running" in q
        assert "DeptId" in q
        assert "payment" in q

    @pytest.mark.asyncio
    async def test_temporal_failure_falls_back_to_postgres(self) -> None:
        pool = _FakePool(count=2)
        temporal = _FakeTemporalVisibility(raises=RuntimeError("rpc down"))

        count, source = await count_active_workflows(
            dept_id="payment", db=pool, temporal=temporal
        )
        assert count == 2
        assert source == "postgres"

    @pytest.mark.asyncio
    async def test_no_temporal_uses_postgres(self) -> None:
        pool = _FakePool(count=7)
        count, source = await count_active_workflows(
            dept_id="devops", db=pool, temporal=None
        )
        assert count == 7
        assert source == "postgres"


# ---------------------------------------------------------------------------
# check_dept_concurrency — allow / reject behaviour
# ---------------------------------------------------------------------------


class TestCheckDeptConcurrency:
    @pytest.mark.asyncio
    async def test_max_none_skips_check_even_when_count_high(self) -> None:
        """``max_concurrent_workflows = None`` → silent allow."""
        pool = _FakePool(count=100)
        result = await check_dept_concurrency(
            "payment", None, db=pool, temporal=None
        )
        assert result.max_allowed is None
        assert result.current == 100
        assert result.source == "postgres"

    @pytest.mark.asyncio
    async def test_under_cap_allows(self) -> None:
        """``count < max`` → silent allow."""
        pool = _FakePool(count=5)
        result = await check_dept_concurrency(
            "payment", 10, db=pool, temporal=None
        )
        assert result.current == 5
        assert result.max_allowed == 10

    @pytest.mark.asyncio
    async def test_at_cap_rejects(self) -> None:
        """``count == max`` → reject (>= comparison)."""
        pool = _FakePool(count=10)
        with pytest.raises(ConcurrencyLimitExceeded) as exc_info:
            await check_dept_concurrency(
                "payment", 10, db=pool, temporal=None
            )
        assert exc_info.value.dept_id == "payment"
        assert exc_info.value.current == 10
        assert exc_info.value.max_allowed == 10

    @pytest.mark.asyncio
    async def test_over_cap_rejects(self) -> None:
        """``count > max`` → reject."""
        pool = _FakePool(count=15)
        with pytest.raises(ConcurrencyLimitExceeded) as exc_info:
            await check_dept_concurrency(
                "payment", 10, db=pool, temporal=None
            )
        assert exc_info.value.current == 15

    @pytest.mark.asyncio
    async def test_uses_temporal_when_wired(self) -> None:
        """Visibility API count is preferred when both sources agree."""
        pool = _FakePool(count=99)
        temporal = _FakeTemporalVisibility(count=3)
        result = await check_dept_concurrency(
            "payment", 10, db=pool, temporal=temporal
        )
        assert result.current == 3
        assert result.source == "temporal"
