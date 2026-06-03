"""Unit tests for ``src.routers.active_workflows`` .
per-dept concurrency saturation badge).
The router exposes a single read-only endpoint:
* ``GET /api/v1/departments/{dept_id}/active-workflows`` →
  ``{active, max_concurrent_workflows, saturation, source}``.
Tests cover:
* 200 happy path with cap configured (saturation computed).
* 200 path with cap unset (``max_concurrent_workflows=None`` →
  ``saturation=None``).
* 503 when the asyncpg pool is unwired."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for _lib in ("audit_logger", "auth-shared", "http-shared"):
    _src = _WORKSPACE_ROOT / "libs" / _lib / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))


from src.auth.dependencies import AuthClaims, require_admin  # noqa: E402
from src.routers.active_workflows import (  # noqa: E402
    router as active_workflows_router,
)


# ---------------------------------------------------------------------------
# Fakes
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(*, pool: Any | None) -> FastAPI:
    """Build a minimal FastAPI app wired to the router."""

    app = FastAPI()
    app.include_router(active_workflows_router)
    app.state.pg_pool = pool

    async def _fake_admin() -> AuthClaims:
        return AuthClaims(sub="admin-user-1", groups=("admin",))

    app.dependency_overrides[require_admin] = _fake_admin
    return app


def _patch_dept_config(
    *, dept_id: str, max_concurrent: int | None
) -> Any:
    """Context manager that patches departments.json read.

    Stubs out :func:`_load_dept_max_concurrent` so the test does not
    depend on the real config file.
    """

    return patch(
        "src.routers.active_workflows._load_dept_max_concurrent",
        return_value=max_concurrent,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestActiveWorkflowsEndpoint:
    def test_returns_count_with_cap(self) -> None:
        """Cap configured → saturation is computed."""
        pool = _FakePool(count=3)
        app = _build_app(pool=pool)
        with _patch_dept_config(dept_id="payment", max_concurrent=10):
            client = TestClient(app)
            res = client.get("/api/v1/departments/payment/active-workflows")
        assert res.status_code == 200
        body = res.json()
        assert body["dept_id"] == "payment"
        assert body["active"] == 3
        assert body["max_concurrent_workflows"] == 10
        assert body["saturation"] == 0.30
        assert body["source"] == "postgres"

    def test_returns_count_without_cap(self) -> None:
        """No cap → saturation is null but count is still returned."""
        pool = _FakePool(count=5)
        app = _build_app(pool=pool)
        with _patch_dept_config(dept_id="research", max_concurrent=None):
            client = TestClient(app)
            res = client.get(
                "/api/v1/departments/research/active-workflows"
            )
        assert res.status_code == 200
        body = res.json()
        assert body["dept_id"] == "research"
        assert body["active"] == 5
        assert body["max_concurrent_workflows"] is None
        assert body["saturation"] is None
        assert body["source"] == "postgres"

    def test_returns_503_when_pool_unwired(self) -> None:
        """Missing ``pg_pool`` → 503 with ``pg_pool_unavailable``."""
        app = _build_app(pool=None)
        client = TestClient(app)
        res = client.get("/api/v1/departments/payment/active-workflows")
        assert res.status_code == 503
        body = res.json()
        assert body["detail"]["reason"] == "pg_pool_unavailable"

    def test_zero_active_with_cap(self) -> None:
        """Zero active → saturation is 0.0."""
        pool = _FakePool(count=0)
        app = _build_app(pool=pool)
        with _patch_dept_config(dept_id="payment", max_concurrent=10):
            client = TestClient(app)
            res = client.get("/api/v1/departments/payment/active-workflows")
        assert res.status_code == 200
        body = res.json()
        assert body["active"] == 0
        assert body["saturation"] == 0.0
