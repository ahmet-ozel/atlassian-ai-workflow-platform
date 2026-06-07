"""Unit tests for ``src.routers.operations`` .
The router is exercised through :class:`fastapi.testclient.TestClient`
against an in-memory stub asyncpg pool. The ``require_admin`` dependency
is overridden with a permissive stub for the happy paths.
Coverage matrix:
* ``GET /admin/operations/license``  200 + correct usage objects when
  the pool is wired and license data exists.
* ``GET``  200 + empty list when no license tiers exist.
* ``GET``  200 + ``__default__`` sentinel entry for depts with no
  license assigned.
* ``GET``  503 with ``pg_pool_unavailable`` when the pool slot is
  ``None``.
* ``percent_used`` is ``max(concurrent%, daily%, monthly%)`` rounded to
  one decimal place.
* Response is sorted: named licenses first, ``__default__`` last."""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Bootstrap ``sys.path`` so ``import src.routers.operations`` resolves
# under direct ``pytest tests/unit`` invocations.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for _lib in ("audit_logger", "auth-shared", "http-shared"):
    _src = _WORKSPACE_ROOT / "libs" / _lib / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from src.auth.dependencies import AuthClaims, require_admin  # noqa: E402
from src.routers.operations import (  # noqa: E402
    _DEFAULT_LICENSE_SENTINEL,
    router,
)


# ---------------------------------------------------------------------------
# Stub asyncpg pool
# ---------------------------------------------------------------------------


class _FakeRecord(dict):
    """Minimal asyncpg Record stub - behaves like a dict."""

    def __getitem__(self, key: str) -> Any:
        return super().__getitem__(key)


class _FakeConn:
    """Stub asyncpg connection that returns pre-configured rows."""

    def __init__(self, query_results: dict[str, list[dict]]) -> None:
        # Maps a substring of the SQL query to the rows to return.
        self._results = query_results

    async def fetch(self, sql: str, *args: Any) -> list[_FakeRecord]:
        for key, rows in self._results.items():
            if key in sql:
                return [_FakeRecord(r) for r in rows]
        return []

    async def fetchrow(self, sql: str, *args: Any) -> _FakeRecord | None:
        for key, rows in self._results.items():
            if key in sql:
                return _FakeRecord(rows[0]) if rows else None
        return None


class _FakePool:
    """Stub asyncpg pool that yields a :class:`_FakeConn`."""

    def __init__(self, query_results: dict[str, list[dict]]) -> None:
        self._conn = _FakeConn(query_results)

    def acquire(self):
        """Return an async context manager yielding the fake connection."""

        class _CM:
            def __init__(self, conn: _FakeConn) -> None:
                self._conn = conn

            async def __aenter__(self) -> _FakeConn:
                return self._conn

            async def __aexit__(self, *_: Any) -> None:
                pass

        return _CM(self._conn)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app(
    pool: Any | None,
    actor_groups: tuple[str, ...] = ("admin",),
) -> FastAPI:
    """Build a minimal FastAPI app with the operations router mounted.

    The ``require_admin`` dependency is overridden to return a stub
    :class:`AuthClaims` with the given ``actor_groups``.
    """

    app = FastAPI()
    app.include_router(router)

    stub_actor = AuthClaims(sub="test-actor", groups=actor_groups)

    app.dependency_overrides[require_admin] = lambda: stub_actor
    app.state.pg_pool = pool
    return app


# ---------------------------------------------------------------------------
# Tests - pool unavailable
# ---------------------------------------------------------------------------


def test_license_503_when_pool_unavailable() -> None:
    """503 is returned when ``app.state.pg_pool`` is ``None``."""

    app = _make_app(pool=None)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/admin/operations/license")
    assert resp.status_code == 503
    body = resp.json()
    assert body["detail"]["reason"] == "pg_pool_unavailable"


# ---------------------------------------------------------------------------
# Tests - empty database
# ---------------------------------------------------------------------------


def test_license_empty_when_no_caps() -> None:
    """Empty list is returned when no license tiers exist in the DB.

    The ``__default__`` bucket is always included (for NULL-license
    depts), but when there are no depts with NULL license_id the
    usage counters are all zero.
    """

    # bot_license_caps has no rows  only the NULL bucket is returned.
    pool = _FakePool(
        {
            # DISTINCT license_id query returns empty  NULL bucket added.
            "DISTINCT license_id": [],
            # All usage counters return 0.
            "COUNT(*)": [{"n": 0}],
            "COALESCE(SUM": [{"total": Decimal("0")}],
            # cap row for NULL license_id  None (no row).
            "max_concurrent_workflows": [],
        }
    )
    app = _make_app(pool=pool)
    client = TestClient(app)
    resp = client.get("/admin/operations/license")
    assert resp.status_code == 200
    data = resp.json()
    # Should have exactly one entry: the __default__ bucket.
    assert len(data) == 1
    entry = data[0]
    assert entry["license_id"] == _DEFAULT_LICENSE_SENTINEL
    assert entry["max_concurrent"] == 10   # default cap
    assert entry["daily_max"] == 100       # default cap
    assert entry["current_concurrent"] == 0
    assert entry["daily_used"] == 0
    assert entry["percent_used"] == 0.0


# ---------------------------------------------------------------------------
# Tests - named license tier
# ---------------------------------------------------------------------------


def test_license_returns_named_tier() -> None:
    """A named license tier is returned with correct usage values."""

    pool = _FakePool(
        {
            "DISTINCT license_id": [{"license_id": "enterprise-2025"}],
            "max_concurrent_workflows": [
                {
                    "license_id": "enterprise-2025",
                    "max_concurrent_workflows": 20,
                    "max_workflows_per_day": 200,
                    "max_token_usd_per_month": Decimal("2000.00"),
                }
            ],
            "COUNT(*)": [{"n": 5}],
            "COALESCE(SUM": [{"total": Decimal("500.00")}],
        }
    )
    app = _make_app(pool=pool)
    client = TestClient(app)
    resp = client.get("/admin/operations/license")
    assert resp.status_code == 200
    data = resp.json()

    # Should have two entries: named tier + __default__ bucket.
    assert len(data) == 2

    # Named tier should come first (sorted before __default__).
    named = data[0]
    assert named["license_id"] == "enterprise-2025"
    assert named["max_concurrent"] == 20
    assert named["daily_max"] == 200
    assert named["current_concurrent"] == 5
    assert named["daily_used"] == 5
    assert named["monthly_token_usd_used"] == "500.00"
    assert named["monthly_token_usd_max"] == "2000.00"

    # __default__ bucket should be last.
    default = data[1]
    assert default["license_id"] == _DEFAULT_LICENSE_SENTINEL


# ---------------------------------------------------------------------------
# Tests - percent_used calculation
# ---------------------------------------------------------------------------


def test_percent_used_is_max_of_three_dimensions() -> None:
    """``percent_used`` equals ``max(concurrent%, daily%, monthly%)``."""

    # concurrent: 3/10 = 30%
    # daily: 80/100 = 80%   max
    # monthly: 100/1000 = 10%
    pool = _FakePool(
        {
            "DISTINCT license_id": [{"license_id": "basic"}],
            "max_concurrent_workflows": [
                {
                    "license_id": "basic",
                    "max_concurrent_workflows": 10,
                    "max_workflows_per_day": 100,
                    "max_token_usd_per_month": Decimal("1000.00"),
                }
            ],
            # concurrent count
            "wi.status = 'running'": [{"n": 3}],
            # daily count
            "wi.created_at >= ": [{"n": 80}],
            # monthly cost
            "COALESCE(SUM": [{"total": Decimal("100.00")}],
        }
    )

    # Use a custom pool that returns different values per query type.
    class _SmartConn:
        async def fetch(self, sql: str, *args: Any) -> list[_FakeRecord]:
            if "DISTINCT license_id" in sql:
                return [_FakeRecord({"license_id": "basic"})]
            return []

        async def fetchrow(self, sql: str, *args: Any) -> _FakeRecord | None:
            if "max_concurrent_workflows" in sql and "COUNT" not in sql:
                return _FakeRecord(
                    {
                        "license_id": "basic",
                        "max_concurrent_workflows": 10,
                        "max_workflows_per_day": 100,
                        "max_token_usd_per_month": Decimal("1000.00"),
                    }
                )
            if "status = 'running'" in sql:
                return _FakeRecord({"n": 3})
            if "created_at >=" in sql and "cost_usd" not in sql:
                return _FakeRecord({"n": 80})
            if "COALESCE(SUM" in sql:
                return _FakeRecord({"total": Decimal("100.00")})
            if "COUNT(*)" in sql:
                return _FakeRecord({"n": 0})
            return None

    class _SmartPool:
        def acquire(self):
            conn = _SmartConn()

            class _CM:
                async def __aenter__(self) -> _SmartConn:
                    return conn

                async def __aexit__(self, *_: Any) -> None:
                    pass

            return _CM()

    app = _make_app(pool=_SmartPool())
    client = TestClient(app)
    resp = client.get("/admin/operations/license")
    assert resp.status_code == 200
    data = resp.json()

    # Find the "basic" entry.
    basic = next((e for e in data if e["license_id"] == "basic"), None)
    assert basic is not None
    # daily% = 80/100 = 80.0 is the max.
    assert basic["percent_used"] == 80.0


# ---------------------------------------------------------------------------
# Tests - default sentinel ordering
# ---------------------------------------------------------------------------


def test_default_sentinel_is_last_in_sorted_output() -> None:
    """``__default__`` bucket appears after all named license tiers."""

    pool = _FakePool(
        {
            "DISTINCT license_id": [
                {"license_id": "tier-b"},
                {"license_id": "tier-a"},
            ],
            "max_concurrent_workflows": [
                {
                    "license_id": "tier-a",
                    "max_concurrent_workflows": 5,
                    "max_workflows_per_day": 50,
                    "max_token_usd_per_month": Decimal("500.00"),
                }
            ],
            "COUNT(*)": [{"n": 0}],
            "COALESCE(SUM": [{"total": Decimal("0")}],
        }
    )
    app = _make_app(pool=pool)
    client = TestClient(app)
    resp = client.get("/admin/operations/license")
    assert resp.status_code == 200
    data = resp.json()

    # Last entry must be the __default__ sentinel.
    assert data[-1]["license_id"] == _DEFAULT_LICENSE_SENTINEL
    # Named tiers come before it.
    named_ids = [e["license_id"] for e in data[:-1]]
    assert _DEFAULT_LICENSE_SENTINEL not in named_ids


# ---------------------------------------------------------------------------
# Tests - dept_admin returns empty when no dept_ids claim
# ---------------------------------------------------------------------------


def test_dept_admin_returns_empty_without_dept_ids_claim() -> None:
    """``dept_admin`` actor with no ``dept_ids`` claim gets empty list.

    This is the safe-fail behaviour: under-exposure rather than
    over-exposure. When the auth-shared library is updated to surface
    ``dept_ids`` as a first-class claim, this test will need updating.
    """

    pool = _FakePool(
        {
            "DISTINCT license_id": [{"license_id": "enterprise-2025"}],
            "COUNT(*)": [{"n": 0}],
            "COALESCE(SUM": [{"total": Decimal("0")}],
        }
    )
    app = _make_app(pool=pool, actor_groups=("dept_admin",))
    client = TestClient(app)
    resp = client.get("/admin/operations/license")
    assert resp.status_code == 200
    assert resp.json() == []
