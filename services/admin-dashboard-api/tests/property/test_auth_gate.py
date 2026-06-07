"""- Auth gate blocks every endpoint before any side-effect.
the test asserts:
* A request with no ``Authorization`` header  HTTP 401.
* A request with a valid token whose principal is not admin  HTTP 403.
* The asyncpg fake, ``VaultClient`` fake and ``httpx.MockTransport``
  recorded ZERO calls in either case - the gate short-circuits before
  any router-level logic runs."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings, strategies as st


_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))


_API_ROOT = Path(__file__).resolve().parents[2]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))


from _llm_providers_fakes import (  # noqa: E402
    FakePool,
    FakeTester,
    FakeVault,
    RecordingAuditSink,
)
from src.auth.dependencies import get_validator  # noqa: E402
from src.llm_providers.error_handlers import (  # noqa: E402
    register_validation_error_handler,
)
from src.llm_providers.repository import LLMProviderRepository  # noqa: E402
from src.llm_providers.dept_override_repository import (  # noqa: E402
    DeptOverrideRepository,
)
from src.llm_providers.service import ProviderService  # noqa: E402
from src.routers.llm_providers import (  # noqa: E402
    department_router,
    router,
)


class _StubValidator:
    """Stand-in OIDCValidator that decodes a fixed token map.

    The instance is shared by every endpoint-under-test invocation
    so the route-level admin check resolves deterministically without
    a live IdP.  Token strings:

    * ``"admin-token"``  claims with ``role=admin``.
    * ``"viewer-token"``  claims with ``role=viewer`` (non-admin).
    """

    def validate(self, token: str) -> dict[str, Any]:
        if token == "admin-token":
            return {"sub": "admin-1", "groups": ["admin"], "role": "admin"}
        if token == "viewer-token":
            return {"sub": "viewer-1", "groups": ["viewer"], "role": "viewer"}
        from auth_shared import InvalidTokenError

        raise InvalidTokenError("unknown test token")


def _build_app() -> tuple[
    TestClient, FakePool, FakeVault, list[httpx.Request]
]:
    """Wire a fresh app with the LLM provider routers and stub validator."""

    pool = FakePool()
    vault = FakeVault()
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"model": "x"})

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)

    app = FastAPI()
    register_validation_error_handler(app)

    # Pre-build the service so the router's per-request resolver picks
    # it up via the ``llm_provider_service`` slot.
    from src.llm_providers.connection_tester import ConnectionTester

    audit = RecordingAuditSink()
    service = ProviderService(
        pool=pool,
        vault_client=vault,
        repo=LLMProviderRepository(),
        override_repo=DeptOverrideRepository(),
        connection_tester=ConnectionTester(http_client),
        audit_sink=audit,
    )
    app.state.llm_provider_service = service
    app.state.pg_pool = pool
    app.state.vault_client = vault
    app.state.http_client = http_client
    app.state.audit_logger = audit

    app.dependency_overrides[get_validator] = lambda: _StubValidator()

    app.include_router(router)
    app.include_router(department_router)
    return TestClient(app), pool, vault, captured


_CLIENT, _POOL, _VAULT, _CAPTURED = _build_app()


_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/admin/llm-providers"),
    ("POST", "/admin/llm-providers"),
    ("GET", f"/admin/llm-providers/{uuid4()}"),
    ("PUT", f"/admin/llm-providers/{uuid4()}"),
    ("DELETE", f"/admin/llm-providers/{uuid4()}"),
    ("POST", f"/admin/llm-providers/{uuid4()}/test"),
    ("POST", "/admin/llm-providers/test"),
    ("GET", "/admin/departments/payment-ops/llm-provider"),
    ("PUT", "/admin/departments/payment-ops/llm-provider"),
)


def _snapshot_state_unchanged(
    pool_writes: int, vault_writes: int, captured: int
) -> None:
    """Assert no side-effect happened after a 401 / 403."""

    assert len(_POOL.providers) == pool_writes
    assert len(_VAULT.writes) == vault_writes
    assert len(_CAPTURED) == captured


@given(idx=st.integers(min_value=0, max_value=len(_ROUTES) - 1))
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_missing_authorization_returns_401(idx: int) -> None:
    """No header  401 + no side-effects."""

    method, path = _ROUTES[idx]
    pool_before = len(_POOL.providers)
    vault_before = len(_VAULT.writes)
    captured_before = len(_CAPTURED)

    response = _CLIENT.request(method, path, content=b"{}")

    assert response.status_code == 401
    _snapshot_state_unchanged(pool_before, vault_before, captured_before)


@given(idx=st.integers(min_value=0, max_value=len(_ROUTES) - 1))
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_non_admin_token_returns_403(idx: int) -> None:
    """Valid non-admin token  403 + no side-effects."""

    method, path = _ROUTES[idx]
    pool_before = len(_POOL.providers)
    vault_before = len(_VAULT.writes)
    captured_before = len(_CAPTURED)

    response = _CLIENT.request(
        method,
        path,
        content=b"{}",
        headers={"Authorization": "Bearer viewer-token"},
    )

    assert response.status_code == 403
    _snapshot_state_unchanged(pool_before, vault_before, captured_before)
