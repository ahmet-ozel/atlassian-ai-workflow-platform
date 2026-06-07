"""Smoke tests for the ``routers.dept_credentials`` FastAPI router.

These tests live alongside the other ``services/automation-service/
tests/unit`` suites and reuse the same ``automation_service.app``
factory.  They assert only the *router-side* contracts that do not
depend on a live Postgres / Vault / probe client:

* The router is registered with the expected paths and methods.
* When ``app.state.dept_credentials`` is missing, every endpoint
  returns 500 with the wiring-error detail (defence-in-depth
  guard, mirrors :func:`automation_service.app.create_app` →
  ``app.state.<key>`` contracts used by sibling routers).
* Roles below ``dept_admin`` are denied on mutating endpoints
  before any orchestrator collaborator is touched (RBAC defence-
  in-depth).

The full CRUD + probe behaviour is covered by the property tests
under ``tests/property/test_dept_credential_crud.py`` and
``tests/property/test_dept_credential_rbac.py``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# sys.path bootstrap - mirrors test_app.py / test_repo_sync_api.py
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
_AUTOMATION_SRC = _AUTOMATION_ROOT / "src"
_PLATFORM_ROOT = _AUTOMATION_ROOT.parent.parent
_LIB_SRC_DIRS = tuple(
    _PLATFORM_ROOT / "libs" / lib / "src"
    for lib in (
        "audit_logger",
        "vault_client",
        "db-shared",
        "http-shared",
        "auth-shared",
        "temporal-shared",
        "mcp_client",
        "messages",
        "prompts",
        "pii-shared",
        "notification",
        "observability",
        "llm-orchestrator",
    )
)
for _p in (str(_AUTOMATION_ROOT), str(_AUTOMATION_SRC), *(str(p) for p in _LIB_SRC_DIRS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Make the readyz dependency probe deterministic for the smoke client.
os.environ.setdefault("LOG_LEVEL", "INFO")

from automation_service.app import create_app  # noqa: E402

from routers.dept_credentials import (  # noqa: E402
    DeptCredentialEndpointDeps,
    router as dept_credentials_router,
)


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditLogger:
    """Records every write call so tests can assert audit emission."""

    events: list[Any] = field(default_factory=list)

    async def write(self, event: Any) -> None:
        self.events.append(event)


@dataclass
class _FakeService:
    """Stand-in :class:`DeptCredentialService` that records calls."""

    add_calls: list[Any] = field(default_factory=list)
    remove_calls: list[Any] = field(default_factory=list)
    probe_calls: list[Any] = field(default_factory=list)

    async def add_or_update(self, request: Any, **kwargs: Any) -> Any:
        self.add_calls.append((request, kwargs))
        raise AssertionError("add_or_update should not be called in smoke test")

    async def remove(self, **kwargs: Any) -> Any:
        self.remove_calls.append(kwargs)
        raise AssertionError("remove should not be called in smoke test")

    async def probe(self, **kwargs: Any) -> Any:
        self.probe_calls.append(kwargs)
        raise AssertionError("probe should not be called in smoke test")


async def _empty_connection_factory() -> Any:
    """Connection factory that should never be invoked in these tests."""

    raise AssertionError(
        "connection_factory should not be invoked in router smoke tests"
    )


def _build_client(*, with_deps: bool = True) -> tuple[TestClient, _FakeService, _FakeAuditLogger]:
    """Return a ``TestClient`` whose ``app.state.dept_credentials`` is set."""

    app = create_app()
    service = _FakeService()
    audit = _FakeAuditLogger()
    if with_deps:
        app.state.dept_credentials = DeptCredentialEndpointDeps(
            service=service,  # type: ignore[arg-type]
            connection_factory=_empty_connection_factory,
            audit_logger=audit,  # type: ignore[arg-type]
            clock=lambda: datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
    # ``create_app()`` already mounts ``dept_credentials_router``,
    # so we do not call ``app.include_router`` again here - duplicate
    # registration would shadow the canonical wiring contract this
    # suite exercises.
    return TestClient(app), service, audit


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRouterRegistration:
    def test_expected_paths_registered(self) -> None:
        paths = {(tuple(sorted(r.methods)), r.path) for r in dept_credentials_router.routes}
        assert (("GET",), "/admin/departments") in paths
        assert (("GET",), "/admin/departments/{dept_id}") in paths
        assert (
            ("POST",),
            "/admin/departments/{dept_id}/credentials/{service}",
        ) in paths
        assert (
            ("DELETE",),
            "/admin/departments/{dept_id}/credentials/{service}",
        ) in paths
        assert (("POST",), "/admin/departments/{dept_id}/probe") in paths


class TestWiringMissing:
    def test_returns_500_when_state_not_wired(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            resp = client.get(
                "/admin/departments",
                headers={"X-Actor-Role": "admin", "X-Actor-Id": "alice"},
            )
        assert resp.status_code == 500
        body = resp.json()
        assert "dept_credentials" in body["detail"]


class TestRbacDefenseInDepth:
    """Mutating endpoints reject sub-``dept_admin`` roles immediately."""

    def test_post_credential_denied_for_lead(self) -> None:
        client, service, audit = _build_client()
        resp = client.post(
            "/admin/departments/payments/credentials/jira",
            json={
                "url": "https://example.atlassian.net",
                "username": "bot@example.com",
                "personal_token": "tok",
            },
            headers={
                "X-Actor-Role": "lead",
                "X-Actor-Id": "lead-1",
                "X-Actor-Dept-Id": "payments",
            },
        )
        assert resp.status_code == 403
        assert service.add_calls == []
        assert any(getattr(e, "action", None) == "rbac_denied" for e in audit.events)

    def test_delete_credential_denied_for_viewer(self) -> None:
        client, service, audit = _build_client()
        resp = client.delete(
            "/admin/departments/payments/credentials/jira",
            headers={
                "X-Actor-Role": "viewer",
                "X-Actor-Id": "viewer-1",
                "X-Actor-Dept-Id": "payments",
            },
        )
        assert resp.status_code == 403
        assert service.remove_calls == []

    def test_dept_admin_scope_mismatch_denied(self) -> None:
        client, service, audit = _build_client()
        resp = client.post(
            "/admin/departments/payments/probe",
            headers={
                "X-Actor-Role": "dept_admin",
                "X-Actor-Id": "alice",
                "X-Actor-Dept-Id": "marketing",
            },
        )
        assert resp.status_code == 403
        assert service.probe_calls == []
        denials = [e for e in audit.events if getattr(e, "action", None) == "rbac_denied"]
        assert denials, "expected an rbac_denied audit row on dept-scope mismatch"


class TestPathParamValidation:
    def test_invalid_service_returns_400(self) -> None:
        client, service, _ = _build_client()
        resp = client.post(
            "/admin/departments/payments/credentials/slack",
            json={
                "url": "https://example.atlassian.net",
                "username": "bot",
                "personal_token": "tok",
            },
            headers={
                "X-Actor-Role": "admin",
                "X-Actor-Id": "alice",
            },
        )
        assert resp.status_code == 400
        assert service.add_calls == []

    def test_invalid_dept_id_returns_400(self) -> None:
        client, service, _ = _build_client()
        resp = client.delete(
            "/admin/departments/INVALID-CAPS/credentials/jira",
            headers={
                "X-Actor-Role": "admin",
                "X-Actor-Id": "alice",
            },
        )
        assert resp.status_code == 400
        assert service.remove_calls == []

    def test_missing_token_returns_400(self) -> None:
        client, service, _ = _build_client()
        resp = client.post(
            "/admin/departments/payments/credentials/jira",
            json={
                "url": "https://example.atlassian.net",
                "username": "bot",
            },
            headers={
                "X-Actor-Role": "admin",
                "X-Actor-Id": "alice",
            },
        )
        assert resp.status_code == 400
        assert service.add_calls == []
