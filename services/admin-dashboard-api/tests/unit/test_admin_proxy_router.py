"""FastAPI router tests for ``src.routers.admin_proxy`` .
These tests wire :class:`AdminProxy` and :func:`require_auth_context`
into a throwaway FastAPI app via dependency overrides - no real
OIDC validator, no asyncpg pool, no upstream automation-service. The
goal is to verify the router-level glue:
* The catch-all path matcher covers every ``/admin/{departments,
  probe-artifacts, ssh-runners, prompts/global}/...`` shape.
* Missing / invalid bearer tokens are rejected at the auth boundary
  (HTTP 401) before the proxy is reached.
* Forwarded responses bubble back through the router unchanged.
* Non-proxied paths under ``/admin`` (specifically ``/admin/services``)
  remain claimable by other routers - i.e. the catch-all does not
  swallow them."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Bootstrap sys.path
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for lib_dir in (
    _WORKSPACE_ROOT / "libs" / "auth-shared" / "src",
    _WORKSPACE_ROOT / "libs" / "audit_logger" / "src",
):
    if lib_dir.is_dir() and str(lib_dir) not in sys.path:
        sys.path.insert(0, str(lib_dir))

from auth_shared import AuthContext, InvalidTokenError  # noqa: E402

from src.auth.dependencies import get_validator  # noqa: E402
from src.proxy import AdminProxy, ProxyResponse  # noqa: E402
from src.routers.admin_proxy import (  # noqa: E402
    get_admin_proxy,
    require_auth_context,
    router as admin_proxy_router,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _StubValidator:
    """OIDC validator stub returning canned claims."""

    def __init__(
        self,
        *,
        claims: dict[str, Any] | None = None,
        raise_invalid: bool = False,
    ) -> None:
        self._claims = claims or {}
        self._raise = raise_invalid

    def validate(self, token: str) -> dict[str, Any]:
        if self._raise:
            raise InvalidTokenError("stub: invalid")
        return dict(self._claims)


class _StubProxy:
    """Stand-in for :class:`AdminProxy` that records every forward call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._next_response = ProxyResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"ok":true}',
        )

    def set_response(self, response: ProxyResponse) -> None:
        self._next_response = response

    async def forward(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        headers: Any,
        actor: AuthContext,
        query_string: str = "",
    ) -> ProxyResponse:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "body": body,
                "actor_id": actor.actor_id,
                "actor_role": actor.actor_role,
                "query_string": query_string,
            }
        )
        return self._next_response


def _build_app(
    validator: _StubValidator,
    proxy: _StubProxy,
) -> FastAPI:
    """Wire the router with overridden dependencies."""

    app = FastAPI()
    app.include_router(admin_proxy_router)
    app.dependency_overrides[get_validator] = lambda: validator
    app.dependency_overrides[get_admin_proxy] = lambda: proxy
    return app


# ---------------------------------------------------------------------------
# Auth boundary
# ---------------------------------------------------------------------------


class TestAuthBoundary:
    """"""

    def test_missing_authorization_returns_401(self) -> None:
        validator = _StubValidator()
        proxy = _StubProxy()
        client = TestClient(_build_app(validator, proxy))

        response = client.post("/admin/departments", json={"id": "x"})

        assert response.status_code == 401
        # Proxy was never reached.
        assert proxy.calls == []

    def test_invalid_token_returns_401(self) -> None:
        validator = _StubValidator(raise_invalid=True)
        proxy = _StubProxy()
        client = TestClient(_build_app(validator, proxy))

        response = client.post(
            "/admin/departments",
            json={"id": "x"},
            headers={"Authorization": "Bearer bogus"},
        )

        assert response.status_code == 401
        assert proxy.calls == []

    def test_token_without_role_claim_returns_401(self) -> None:
        # ``extract_auth_context`` raises ``MissingClaimError`` (a
        # subclass of ``InvalidTokenError``) which the dependency
        # surfaces as 401.
        validator = _StubValidator(claims={"sub": "user-1"})  # no role
        proxy = _StubProxy()
        client = TestClient(_build_app(validator, proxy))

        response = client.post(
            "/admin/departments",
            json={"id": "x"},
            headers={"Authorization": "Bearer good"},
        )

        assert response.status_code == 401
        assert proxy.calls == []


# ---------------------------------------------------------------------------
# Catch-all routing
# ---------------------------------------------------------------------------


class TestCatchAllRouting:
    """The router covers every documented ``/admin/*`` shape."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/admin/departments"),
            ("POST", "/admin/departments/wizard"),
            ("POST", "/admin/departments/payments/credentials/rotate"),
            ("POST", "/admin/departments/payments/disable"),
            ("GET", "/admin/probe-artifacts"),
            ("DELETE", "/admin/probe-artifacts/abc-123"),
            ("POST", "/admin/ssh-runners"),
            ("PATCH", "/admin/ssh-runners/runner-1"),
            ("PUT", "/admin/prompts/global"),
            ("GET", "/admin/prompts/global/v3"),
        ],
    )
    def test_all_proxied_paths_route_to_proxy(
        self,
        method: str,
        path: str,
    ) -> None:
        validator = _StubValidator(
            claims={"sub": "u1", "role": "admin"},
        )
        proxy = _StubProxy()
        client = TestClient(_build_app(validator, proxy))

        response = client.request(
            method,
            path,
            headers={"Authorization": "Bearer good"},
        )

        assert response.status_code == 200, (
            f"expected 200 for {method} {path}, got {response.status_code}"
        )
        assert len(proxy.calls) == 1
        assert proxy.calls[0]["method"] == method
        assert proxy.calls[0]["path"] == path

    def test_non_proxied_admin_path_is_not_swallowed(self) -> None:
        # ``/admin/services`` is owned by the lifecycle router and
        # MUST stay claimable by other registrations. The catch-all
        # in admin_proxy_router only covers the four documented
        # prefixes.
        validator = _StubValidator(claims={"sub": "u1", "role": "admin"})
        proxy = _StubProxy()
        client = TestClient(_build_app(validator, proxy))

        response = client.get(
            "/admin/services",
            headers={"Authorization": "Bearer good"},
        )

        # No router handles /admin/services in this throwaway app, so
        # FastAPI returns 404. The crucial assertion is that the
        # proxy was NOT called - the /admin/services path is left for
        # services_lifecycle to claim.
        assert response.status_code == 404
        assert proxy.calls == []


# ---------------------------------------------------------------------------
# Response forwarding
# ---------------------------------------------------------------------------


class TestResponseForwarding:
    """Proxy responses bubble back through the router verbatim."""

    def test_proxy_response_status_and_body_are_returned(self) -> None:
        validator = _StubValidator(claims={"sub": "u1", "role": "admin"})
        proxy = _StubProxy()
        proxy.set_response(
            ProxyResponse(
                status_code=201,
                headers={"content-type": "application/json"},
                body=b'{"id":"payments"}',
            )
        )
        client = TestClient(_build_app(validator, proxy))

        response = client.post(
            "/admin/departments",
            json={"id": "payments"},
            headers={"Authorization": "Bearer good"},
        )

        assert response.status_code == 201
        assert response.json() == {"id": "payments"}

    def test_query_string_is_forwarded(self) -> None:
        validator = _StubValidator(claims={"sub": "u1", "role": "admin"})
        proxy = _StubProxy()
        client = TestClient(_build_app(validator, proxy))

        client.get(
            "/admin/probe-artifacts?state=partial_orphan&limit=50",
            headers={"Authorization": "Bearer good"},
        )

        assert proxy.calls[0]["query_string"] == (
            "state=partial_orphan&limit=50"
        )

    def test_actor_context_carries_through_to_proxy(self) -> None:
        validator = _StubValidator(
            claims={
                "sub": "alice",
                "role": "dept_admin",
                "dept_ids": ["payments"],
            },
        )
        proxy = _StubProxy()
        client = TestClient(_build_app(validator, proxy))

        client.post(
            "/admin/departments/payments/credentials/rotate",
            headers={"Authorization": "Bearer good"},
        )

        assert proxy.calls[0]["actor_id"] == "alice"
        assert proxy.calls[0]["actor_role"] == "dept_admin"
