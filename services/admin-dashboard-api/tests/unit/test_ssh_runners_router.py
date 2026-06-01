"""Unit tests for ``src.routers.ssh_runners`` (platform-quick-fixes task 7.6).

**Validates: Requirements 4.10, 4.11, 4.13, 4.15**

The router exposes five endpoints:

* ``GET  /admin/ssh-runners``                    — list all runners.
* ``POST /admin/ssh-runners``                    — create a new runner.
* ``PATCH /admin/ssh-runners/{runner_id}``       — update runner fields.
* ``GET  /admin/departments/{dept_id}/ssh-runners``  — dept's runners.
* ``POST /admin/departments/{dept_id}/ssh-runners``  — update assignments.

Tests cover:

* 200 happy path for listing runners.
* 201 creation with Vault write.
* 200 update (status, host, port).
* 404 when runner not found.
* 409 when runner_id already exists.
* 503 when pg_pool or vault_client is unwired.
* Department assignment reconciliation with audit events.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
from src.routers.ssh_runners import (  # noqa: E402
    router as ssh_runners_router,
    dept_ssh_router,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc)


class _FakeRow(dict):
    """Dict subclass that supports attribute-style access for asyncpg compat."""

    def __getitem__(self, key: str) -> Any:
        return super().__getitem__(key)


def _make_runner_row(
    runner_id: str = "runner-01",
    host: str = "192.168.1.100",
    port: int = 22,
    username: str = "ai-runner",
    vault_path: str = "vault:ssh/runners/runner-01/active",
    status: str = "active",
) -> _FakeRow:
    return _FakeRow(
        runner_id=runner_id,
        host=host,
        port=port,
        username=username,
        vault_path=vault_path,
        status=status,
        created_at=_NOW,
        updated_at=_NOW,
    )


class _FakePool:
    """Fake asyncpg pool that records queries and returns canned results."""

    def __init__(
        self,
        *,
        fetch_results: list[list[_FakeRow]] | None = None,
        fetchrow_result: _FakeRow | None = None,
        fetchval_result: Any = None,
    ) -> None:
        self._fetch_results = fetch_results or []
        self._fetch_idx = 0
        self._fetchrow_result = fetchrow_result
        self._fetchval_result = fetchval_result
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[_FakeRow]:
        self.executed.append((query, args))
        if self._fetch_idx < len(self._fetch_results):
            result = self._fetch_results[self._fetch_idx]
            self._fetch_idx += 1
            return result
        return []

    async def fetchrow(self, query: str, *args: Any) -> _FakeRow | None:
        self.executed.append((query, args))
        return self._fetchrow_result

    async def fetchval(self, query: str, *args: Any) -> Any:
        self.executed.append((query, args))
        return self._fetchval_result

    async def execute(self, query: str, *args: Any) -> None:
        self.executed.append((query, args))


class _FakeVaultClient:
    """Fake Vault client that records writes."""

    def __init__(self, *, should_fail: bool = False) -> None:
        self._should_fail = should_fail
        self.writes: list[dict[str, Any]] = []

    async def write_env_override(
        self, *, service_name: str, key: str, value: str
    ) -> None:
        if self._should_fail:
            raise RuntimeError("Vault write failed")
        self.writes.append(
            {"service_name": service_name, "key": key, "value": value}
        )


class _FakeAuditSink:
    """Fake audit sink that records events."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def write(self, event: Any) -> None:
        self.events.append(event)


class _FakeAdminProxy:
    """Fake admin proxy with audit sink."""

    def __init__(self, audit: _FakeAuditSink) -> None:
        self._audit = audit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(
    *,
    pool: Any | None = None,
    vault: Any | None = None,
    audit_sink: _FakeAuditSink | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app wired to the SSH runners routers."""
    app = FastAPI()
    app.include_router(ssh_runners_router)
    app.include_router(dept_ssh_router)
    app.state.pg_pool = pool
    app.state.vault_client = vault
    app.state.secret_rotation_audit_sink = None

    if audit_sink is not None:
        app.state.admin_proxy = _FakeAdminProxy(audit_sink)
    else:
        app.state.admin_proxy = None

    async def _fake_admin() -> AuthClaims:
        return AuthClaims(sub="admin-user-1", groups=("admin",))

    app.dependency_overrides[require_admin] = _fake_admin
    return app


# ---------------------------------------------------------------------------
# Tests — GET /admin/ssh-runners
# ---------------------------------------------------------------------------


class TestListSshRunners:
    def test_returns_runners(self) -> None:
        """Happy path — returns list of runners with active count."""
        pool = _FakePool(
            fetch_results=[
                [
                    _make_runner_row("runner-01"),
                    _make_runner_row("runner-02", host="10.0.0.5"),
                ]
            ]
        )
        app = _build_app(pool=pool)
        client = TestClient(app)
        res = client.get("/admin/ssh-runners")
        assert res.status_code == 200
        body = res.json()
        assert len(body["runners"]) == 2
        assert body["runners"][0]["runner_id"] == "runner-01"
        assert body["runners"][1]["host"] == "10.0.0.5"
        # New fields from production-hardening task 11.1
        assert body["active_runners"] == 2
        assert "healthcheck_cron_scheduled" in body

    def test_active_runners_counts_only_active(self) -> None:
        """active_runners counts only runners with status 'active'."""
        pool = _FakePool(
            fetch_results=[
                [
                    _make_runner_row("runner-01", status="active"),
                    _make_runner_row("runner-02", status="disabled"),
                    _make_runner_row("runner-03", status="quarantine"),
                ]
            ]
        )
        app = _build_app(pool=pool)
        client = TestClient(app)
        res = client.get("/admin/ssh-runners")
        assert res.status_code == 200
        body = res.json()
        assert body["active_runners"] == 1
        assert len(body["runners"]) == 3

    def test_returns_empty_list(self) -> None:
        """No runners → empty list with zero active count."""
        pool = _FakePool(fetch_results=[[]])
        app = _build_app(pool=pool)
        client = TestClient(app)
        res = client.get("/admin/ssh-runners")
        assert res.status_code == 200
        body = res.json()
        assert body["runners"] == []
        assert body["active_runners"] == 0

    def test_returns_503_when_pool_unwired(self) -> None:
        """Missing pg_pool → 503."""
        app = _build_app(pool=None)
        client = TestClient(app)
        res = client.get("/admin/ssh-runners")
        assert res.status_code == 503
        assert res.json()["detail"]["reason"] == "pg_pool_unavailable"


# ---------------------------------------------------------------------------
# Tests — POST /admin/ssh-runners
# ---------------------------------------------------------------------------


class TestCreateSshRunner:
    def test_creates_runner(self) -> None:
        """Happy path — creates runner and writes key to Vault."""
        pool = _FakePool(fetchval_result=None)  # No existing runner
        vault = _FakeVaultClient()
        app = _build_app(pool=pool, vault=vault)
        client = TestClient(app)
        res = client.post(
            "/admin/ssh-runners",
            json={
                "runner_id": "runner-new",
                "host": "10.0.0.10",
                "port": 22,
                "username": "ai-runner",
                "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----",
            },
        )
        assert res.status_code == 201
        body = res.json()
        assert body["runner_id"] == "runner-new"
        assert body["host"] == "10.0.0.10"
        assert body["status"] == "active"
        assert body["vault_path"] == "vault:ssh/runners/runner-new/active"
        # Verify Vault was called
        assert len(vault.writes) == 1
        assert vault.writes[0]["service_name"] == "ssh/runners/runner-new"
        assert vault.writes[0]["key"] == "active"

    def test_returns_409_on_duplicate(self) -> None:
        """Duplicate runner_id → 409."""
        pool = _FakePool(fetchval_result=1)  # Runner exists
        vault = _FakeVaultClient()
        app = _build_app(pool=pool, vault=vault)
        client = TestClient(app)
        res = client.post(
            "/admin/ssh-runners",
            json={
                "runner_id": "runner-01",
                "host": "10.0.0.10",
                "port": 22,
                "username": "ai-runner",
                "private_key": "fake-key",
            },
        )
        assert res.status_code == 409

    def test_returns_502_on_vault_failure(self) -> None:
        """Vault write failure → 502."""
        pool = _FakePool(fetchval_result=None)
        vault = _FakeVaultClient(should_fail=True)
        app = _build_app(pool=pool, vault=vault)
        client = TestClient(app)
        res = client.post(
            "/admin/ssh-runners",
            json={
                "runner_id": "runner-new",
                "host": "10.0.0.10",
                "port": 22,
                "username": "ai-runner",
                "private_key": "fake-key",
            },
        )
        assert res.status_code == 502

    def test_returns_503_when_vault_unwired(self) -> None:
        """Missing vault_client → 503."""
        pool = _FakePool(fetchval_result=None)
        app = _build_app(pool=pool, vault=None)
        client = TestClient(app)
        res = client.post(
            "/admin/ssh-runners",
            json={
                "runner_id": "runner-new",
                "host": "10.0.0.10",
                "port": 22,
                "username": "ai-runner",
                "private_key": "fake-key",
            },
        )
        assert res.status_code == 503


# ---------------------------------------------------------------------------
# Tests — PATCH /admin/ssh-runners/{runner_id}
# ---------------------------------------------------------------------------


class TestUpdateSshRunner:
    def test_updates_status(self) -> None:
        """Happy path — updates runner status."""
        updated_row = _make_runner_row("runner-01", status="disabled")
        pool = _FakePool(fetchrow_result=updated_row)
        app = _build_app(pool=pool)
        client = TestClient(app)
        res = client.patch(
            "/admin/ssh-runners/runner-01",
            json={"status": "disabled"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["runner_id"] == "runner-01"
        assert body["status"] == "disabled"

    def test_returns_404_when_not_found(self) -> None:
        """Runner not found → 404."""
        pool = _FakePool(fetchrow_result=None)
        app = _build_app(pool=pool)
        client = TestClient(app)
        res = client.patch(
            "/admin/ssh-runners/nonexistent",
            json={"status": "disabled"},
        )
        assert res.status_code == 404

    def test_returns_422_on_invalid_status(self) -> None:
        """Invalid status value → 422."""
        pool = _FakePool(fetchrow_result=_make_runner_row())
        app = _build_app(pool=pool)
        client = TestClient(app)
        res = client.patch(
            "/admin/ssh-runners/runner-01",
            json={"status": "invalid_status"},
        )
        assert res.status_code == 422


# ---------------------------------------------------------------------------
# Tests — GET /admin/departments/{dept_id}/ssh-runners
# ---------------------------------------------------------------------------


class TestListDeptSshRunners:
    def test_returns_assigned_runners(self) -> None:
        """Happy path — returns runners assigned to dept."""
        pool = _FakePool(
            fetch_results=[
                [_make_runner_row("runner-01"), _make_runner_row("runner-02")]
            ]
        )
        app = _build_app(pool=pool)
        client = TestClient(app)
        res = client.get("/admin/departments/payment/ssh-runners")
        assert res.status_code == 200
        body = res.json()
        assert body["dept_id"] == "payment"
        assert len(body["runners"]) == 2

    def test_returns_empty_when_no_assignments(self) -> None:
        """No assignments → empty list."""
        pool = _FakePool(fetch_results=[[]])
        app = _build_app(pool=pool)
        client = TestClient(app)
        res = client.get("/admin/departments/payment/ssh-runners")
        assert res.status_code == 200
        assert res.json()["runners"] == []


# ---------------------------------------------------------------------------
# Tests — POST /admin/departments/{dept_id}/ssh-runners
# ---------------------------------------------------------------------------


class TestUpdateDeptSshRunners:
    def test_assigns_runners(self) -> None:
        """Happy path — assigns runners and emits audit events."""
        # First fetch: validate runner_ids exist
        # Second fetch: get current assignments (empty)
        pool = _FakePool(
            fetch_results=[
                [_FakeRow(runner_id="runner-01"), _FakeRow(runner_id="runner-02")],
                [],  # no current assignments
            ]
        )
        audit_sink = _FakeAuditSink()
        app = _build_app(pool=pool, audit_sink=audit_sink)
        client = TestClient(app)
        res = client.post(
            "/admin/departments/payment/ssh-runners",
            json={"runner_ids": ["runner-01", "runner-02"]},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["dept_id"] == "payment"
        assert set(body["assigned"]) == {"runner-01", "runner-02"}
        assert set(body["added"]) == {"runner-01", "runner-02"}
        assert body["removed"] == []
        # Verify audit events were written
        assert len(audit_sink.events) == 2
        actions = {e.action for e in audit_sink.events}
        assert "dept_ssh_runner_assigned" in actions

    def test_removes_runners(self) -> None:
        """Removing runners emits unassigned audit events."""
        # When runner_ids is empty, validation is skipped.
        # First fetch: current assignments (the only fetch call made)
        pool = _FakePool(
            fetch_results=[
                [_FakeRow(runner_id="runner-01"), _FakeRow(runner_id="runner-02")],
            ]
        )
        audit_sink = _FakeAuditSink()
        app = _build_app(pool=pool, audit_sink=audit_sink)
        client = TestClient(app)
        res = client.post(
            "/admin/departments/payment/ssh-runners",
            json={"runner_ids": []},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["assigned"] == []
        assert set(body["removed"]) == {"runner-01", "runner-02"}
        # Verify audit events
        assert len(audit_sink.events) == 2
        actions = {e.action for e in audit_sink.events}
        assert "dept_ssh_runner_unassigned" in actions

    def test_returns_400_on_missing_runners(self) -> None:
        """Non-existent runner_ids → 400."""
        # First fetch: validate runner_ids — returns only runner-01
        pool = _FakePool(
            fetch_results=[
                [_FakeRow(runner_id="runner-01")],
            ]
        )
        app = _build_app(pool=pool)
        client = TestClient(app)
        res = client.post(
            "/admin/departments/payment/ssh-runners",
            json={"runner_ids": ["runner-01", "nonexistent"]},
        )
        assert res.status_code == 400
        body = res.json()
        assert "nonexistent" in body["detail"]["missing_runner_ids"]
