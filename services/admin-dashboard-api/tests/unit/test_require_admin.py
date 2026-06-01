"""Unit tests for ``src.auth.dependencies.require_admin``.

Covers the behaviour matrix from Requirements 10.1 / 10.2 / 10.3:

* Missing ``Authorization`` header → 401, claim inspection skipped.
* Non-``Bearer`` scheme (``Basic``, etc.) → 401.
* ``Bearer`` prefix present but token portion empty → 401.
* Validator raises :class:`InvalidTokenError` → 401.
* Token valid but no ``admin`` in ``groups`` *or* ``roles`` → 403.
* Token valid with ``admin`` in ``groups`` only → :class:`AuthClaims`.
* Token valid with ``admin`` in ``roles`` only → :class:`AuthClaims`.

The validator is exercised through a hand-written stub that records each
``validate`` call and emits canned claims or :class:`InvalidTokenError`.
A real ``OIDCValidator`` is **not** instantiated; we only want to verify
the FastAPI dependency wiring, not the JWKS validator (that's covered by
the ``libs/auth-shared`` test suite from task 4.1).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

# ``admin-dashboard-api`` ships its source under ``src/``; add the
# service root to ``sys.path`` so ``import src.auth`` resolves under
# direct ``pytest tests/unit`` invocations (mirrors the bootstrap in
# ``test_env_parser.py`` and ``test_manifest.py``).
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# ``libs/auth-shared`` is consumed via ``sys.path`` injection at test
# time (see ``pytest.ini``'s ``pythonpath``); when the test is invoked
# directly through ``pytest tests/unit`` from the service root we need
# to add it manually so ``from auth_shared import ...`` resolves.
_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
_AUTH_SHARED_SRC = _WORKSPACE_ROOT / "libs" / "auth-shared" / "src"
if _AUTH_SHARED_SRC.is_dir() and str(_AUTH_SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(_AUTH_SHARED_SRC))

from auth_shared import InvalidTokenError  # noqa: E402

from src.auth.dependencies import (  # noqa: E402
    AuthClaims,
    get_validator,
    require_admin,
)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _StubValidator:
    """Minimal stand-in for :class:`OIDCValidator`.

    Configurable per test: pass ``claims`` to return a canned dict, or
    set ``raise_invalid=True`` to simulate token validation failure.
    The stub records every call so tests can assert that ``validate``
    was *not* invoked when the dependency must short-circuit at the
    header-shape check.
    """

    def __init__(
        self,
        *,
        claims: dict[str, Any] | None = None,
        raise_invalid: bool = False,
    ) -> None:
        self._claims = claims or {}
        self._raise = raise_invalid
        self.calls: list[str] = []

    def validate(self, token: str) -> dict[str, Any]:
        self.calls.append(token)
        if self._raise:
            raise InvalidTokenError("stub: invalid")
        return dict(self._claims)


def _build_app(validator: _StubValidator) -> FastAPI:
    """Wire ``require_admin`` into a throwaway FastAPI app.

    The ``get_validator`` dependency is overridden so the test never
    instantiates a real :class:`OIDCValidator` (which would try to
    read :class:`Settings` and potentially hit the network).
    """

    app = FastAPI()

    @app.get("/protected")
    def _protected(claims: AuthClaims = Depends(require_admin)) -> dict:
        return {"sub": claims.sub, "groups": list(claims.groups)}

    app.dependency_overrides[get_validator] = lambda: validator
    return app


# ---------------------------------------------------------------------------
# 401 — missing / malformed Authorization header (Requirement 10.2)
# ---------------------------------------------------------------------------


def test_missing_authorization_header_returns_401_without_validation() -> None:
    """No header at all → 401, validator MUST NOT be invoked.

    Requirement 10.2 mandates the 401 fires *before* any claim
    inspection so anonymous probes can't even tell whether a service
    name is valid.
    """

    validator = _StubValidator(claims={"sub": "x", "groups": ["admin"]})
    client = TestClient(_build_app(validator))

    response = client.get("/protected")

    assert response.status_code == 401
    assert response.json() == {"detail": "missing bearer token"}
    assert validator.calls == []  # validator was never reached


def test_non_bearer_scheme_returns_401_without_validation() -> None:
    """``Basic <creds>`` is rejected before claim inspection."""

    validator = _StubValidator(claims={"sub": "x", "groups": ["admin"]})
    client = TestClient(_build_app(validator))

    response = client.get(
        "/protected",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "missing bearer token"}
    assert validator.calls == []


def test_empty_token_after_bearer_returns_401() -> None:
    """``Bearer `` (with trailing space and nothing else) → 401."""

    validator = _StubValidator(claims={"sub": "x", "groups": ["admin"]})
    client = TestClient(_build_app(validator))

    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer "},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "empty bearer token"}
    assert validator.calls == []  # empty token is rejected before validate()


def test_whitespace_only_token_returns_401() -> None:
    """``Bearer    `` (only whitespace after the scheme) → 401."""

    validator = _StubValidator(claims={"sub": "x", "groups": ["admin"]})
    client = TestClient(_build_app(validator))

    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer    "},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "empty bearer token"}
    assert validator.calls == []


# ---------------------------------------------------------------------------
# 401 — validator rejects the token (Requirement 10.2 second clause)
# ---------------------------------------------------------------------------


def test_invalid_token_raises_401() -> None:
    """Validator raising :class:`InvalidTokenError` → 401 ``invalid token``."""

    validator = _StubValidator(raise_invalid=True)
    client = TestClient(_build_app(validator))

    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer some-bogus-token"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid token"}
    assert validator.calls == ["some-bogus-token"]


# ---------------------------------------------------------------------------
# 403 — valid token without admin claim (Requirement 10.3)
# ---------------------------------------------------------------------------


def test_no_admin_in_groups_or_roles_returns_403() -> None:
    """Authenticated non-admin → 403 ``admin claim required``.

    Requirement 10.3 second sentence: read-only access is *not* granted
    to authenticated non-admin users.
    """

    validator = _StubValidator(
        claims={"sub": "alice", "groups": ["user", "billing"], "roles": ["viewer"]}
    )
    client = TestClient(_build_app(validator))

    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer valid-but-not-admin"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "admin claim required"}
    assert validator.calls == ["valid-but-not-admin"]


def test_missing_groups_and_roles_returns_403() -> None:
    """Token with no group claims at all → 403."""

    validator = _StubValidator(claims={"sub": "alice"})
    client = TestClient(_build_app(validator))

    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer valid-no-groups"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "admin claim required"}


def test_non_iterable_groups_value_returns_403() -> None:
    """A malformed ``groups`` claim (e.g. a string) is treated as empty."""

    validator = _StubValidator(claims={"sub": "alice", "groups": "admin"})
    client = TestClient(_build_app(validator))

    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer malformed-groups"},
    )

    # ``"admin"`` as a *string* (not a list containing ``"admin"``) is
    # rejected — group claims must be iterables of strings.
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Happy paths — admin in groups, admin in roles (Requirement 10.3)
# ---------------------------------------------------------------------------


def test_admin_in_groups_returns_auth_claims() -> None:
    """``groups: ["admin"]`` → 200 + AuthClaims with the admin group."""

    validator = _StubValidator(
        claims={"sub": "ops-1", "groups": ["admin", "platform"]}
    )
    client = TestClient(_build_app(validator))

    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer admin-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "ops-1"
    # Groups are returned as a sorted tuple so the wire-format is
    # deterministic regardless of how the IdP ordered them.
    assert body["groups"] == ["admin", "platform"]


def test_admin_in_roles_only_returns_auth_claims() -> None:
    """``roles: ["admin"]`` (no ``groups`` claim) is also sufficient.

    Requirement 10.3 takes the **union** of ``groups`` and ``roles`` so
    IdPs that surface RBAC under either name are both accepted.
    """

    validator = _StubValidator(claims={"sub": "ops-2", "roles": ["admin"]})
    client = TestClient(_build_app(validator))

    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer admin-via-roles"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "ops-2"
    assert body["groups"] == ["admin"]


def test_admin_in_both_groups_and_roles_is_deduplicated() -> None:
    """Overlapping ``admin`` entries collapse to a single tuple element."""

    validator = _StubValidator(
        claims={
            "sub": "ops-3",
            "groups": ["admin", "ops"],
            "roles": ["admin", "approver"],
        }
    )
    client = TestClient(_build_app(validator))

    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer admin-everywhere"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "ops-3"
    assert sorted(body["groups"]) == ["admin", "approver", "ops"]


def test_bearer_scheme_is_case_insensitive() -> None:
    """``bearer`` and ``BEARER`` are both accepted (RFC 7235 §2.1).

    The behaviour matters because some HTTP clients (older curl, custom
    SDKs) normalise the scheme to lowercase or uppercase.
    """

    validator = _StubValidator(claims={"sub": "ops", "groups": ["admin"]})
    client = TestClient(_build_app(validator))

    for prefix in ("Bearer", "bearer", "BEARER", "BeArEr"):
        response = client.get(
            "/protected",
            headers={"Authorization": f"{prefix} admin-tok"},
        )
        assert response.status_code == 200, prefix


def test_token_missing_sub_returns_401() -> None:
    """Token validates but has no ``sub`` claim → 401 ``invalid token``.

    The lifecycle audit log relies on ``sub`` for the ``actor`` field
    (Requirement 11.2); rejecting at this point fails closed and
    prevents writing audit entries with an undefined actor.
    """

    validator = _StubValidator(claims={"groups": ["admin"]})
    client = TestClient(_build_app(validator))

    response = client.get(
        "/protected",
        headers={"Authorization": "Bearer admin-but-no-sub"},
    )

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Direct unit calls (no FastAPI app) — exercise the dependency in isolation
# ---------------------------------------------------------------------------


def test_direct_call_returns_auth_claims_dataclass() -> None:
    """``require_admin`` is callable as a regular coroutine.

    Bypassing FastAPI's dependency wiring confirms the function returns
    a frozen :class:`AuthClaims` dataclass with ``groups`` as a tuple.
    """

    import asyncio

    class _Req:
        # Minimal :class:`fastapi.Request` stub — the dependency only
        # touches ``request.headers`` so a duck-typed ``headers`` mapping
        # is sufficient.
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers

    validator = _StubValidator(claims={"sub": "ops", "groups": ["admin"]})
    request = _Req({"authorization": "Bearer good"})

    claims = asyncio.run(require_admin(request, validator=validator))  # type: ignore[arg-type]

    assert isinstance(claims, AuthClaims)
    assert claims.sub == "ops"
    assert isinstance(claims.groups, tuple)
    assert "admin" in claims.groups


def test_direct_call_raises_http_exception_on_missing_header() -> None:
    """Direct invocation surfaces :class:`HTTPException` with the right code."""

    import asyncio

    class _Req:
        def __init__(self, headers: dict[str, str]) -> None:
            self.headers = headers

    validator = _StubValidator()
    request = _Req({})

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(require_admin(request, validator=validator))  # type: ignore[arg-type]

    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == "missing bearer token"
    assert validator.calls == []
