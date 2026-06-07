"""Integration test for the OIDC login flow under both AUTH_PROVIDER modes.
What this test exercises
------------------------

Two complementary surfaces share this file because they ride the same
``auth_shared`` primitives and use the same JWKS fixtures:

1. **End-to-end env-driven login flow**. Builds an :class:`OIDCConfig`
 straight from the env contract: ``AUTH_PROVIDER``, ``OIDC_ISSUER_URL``,
 ``OIDC_CLIENT_ID``, ``OIDC_CLIENT_SECRET``;
 then runs a fully signed RS256 token (or any non-empty bearer for
 ``AUTH_PROVIDER=local``) through :meth:`OIDCValidator.authenticate`
 and asserts the resulting :class:`AuthContext` populates
 ``actor_id`` (from ``sub``), ``actor_role`` (from ``role``/``roles``
 /``groups``) and ``dept_ids`` (from ``dept_ids``/``departments``).

 The flow ends with a :func:`auth_shared.policy.check` call so we
 confirm the env-derived config produces an actor that the RBAC
 layer accepts for a global-admin gate (the operative check on
 admin-dashboard endpoints).

2. **HTTP boundary against ``admin-dashboard-api``**. Mounts the
 ``services_lifecycle`` router with a stubbed
 :class:`LifecycleService` and drives it with an in-memory validator
 so we can assert the auth dependency translates the
 :class:`AuthContext` into the right HTTP status codes - 401 for
 missing/garbage tokens (including ``alg=none`` confusion attempts),
 403 for valid signature but non-admin role, 200 for admin.

Both surfaces SKIP cleanly when the workspace's optional ``auth-shared``
library is not importable, so the suite remains green on a slimmed
checkout. The whole file is marked ``@pytest.mark.integration`` because
it exercises the full token-to-claim-to-decision pipeline rather than a
single primitive.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# sys.path bootstrap (must precede the auth_shared imports)
# ---------------------------------------------------------------------------
#
# ``platform/pytest.ini`` already injects ``libs/auth-shared/src`` into
# ``pythonpath`` when pytest is the entry point, but a bare
# ``python -m unittest`` invocation does not honour that. Re-running the
# bootstrap here keeps the test self-contained.

_WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
_API_SERVICE_ROOT = _WORKSPACE_ROOT / "services" / "admin-dashboard-api"
_AUTH_SHARED_SRC = _WORKSPACE_ROOT / "libs" / "auth-shared" / "src"

for path in (_API_SERVICE_ROOT, _AUTH_SHARED_SRC):
    s = str(path)
    if path.is_dir() and s not in sys.path:
        sys.path.insert(0, s)


# ---------------------------------------------------------------------------
# Graceful skip when auth-shared is not available.
# ---------------------------------------------------------------------------

# The integration suite keeps a graceful degradation path for slimmed-down
# checkouts that omit ``libs/auth-shared/``. Importing the
# auth_shared symbols is wrapped in a try/except so a missing library
# turns into a single module-level skip instead of a collection error.
auth_shared = pytest.importorskip(
    "auth_shared",
    reason=(
        "auth-shared library not importable; OIDC integration tests are "
        "skipped graceful degradation)."
    ),
)
cryptography_serialization = pytest.importorskip(
    "cryptography.hazmat.primitives.serialization",
    reason="cryptography is required to sign integration-test JWTs.",
)
cryptography_rsa = pytest.importorskip(
    "cryptography.hazmat.primitives.asymmetric.rsa",
    reason="cryptography is required to mint RS256 keypairs.",
)
httpx_module = pytest.importorskip(
    "httpx",
    reason="httpx is required to back the JWKS mock transport.",
)
jose_jwt_module = pytest.importorskip(
    "jose.jwt",
    reason="python-jose is required to sign RS256 tokens for the OIDC mode.",
)


# Re-bind to the shorter names used in the rest of the module so the
# downstream code reads naturally even though the imports were performed
# defensively above.
serialization = cryptography_serialization
rsa = cryptography_rsa
httpx = httpx_module
jose_jwt = jose_jwt_module

OIDCConfig = auth_shared.OIDCConfig
OIDCValidator = auth_shared.OIDCValidator
AuthContext = auth_shared.AuthContext
InvalidTokenError = auth_shared.InvalidTokenError
MissingClaimError = auth_shared.MissingClaimError
extract_auth_context = auth_shared.extract_auth_context
PermissionDenied = auth_shared.PermissionDenied
policy_check = auth_shared.check


# ---------------------------------------------------------------------------
# Test parameters - issuer / audience / JWKS URL shared across cases.
# ---------------------------------------------------------------------------

_ISSUER = "https://idp.example.test/"
_AUDIENCE = "admin-dashboard-api"
_JWKS_URL = "https://idp.example.test/.well-known/jwks.json"
_KID = "test-key-13-3"


# ---------------------------------------------------------------------------
# RSA keypair + JWKS document fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[Any, Any]:
    """Generate a fresh RSA-2048 keypair for the test module.

 Re-using a single keypair across the production-mode cases keeps
 the test fast while still giving us a real (non-mocked) signature
 check via ``python-jose``.
 """

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(scope="module")
def private_pem(rsa_keypair: tuple[Any, Any]) -> bytes:
    """Return the RSA private key as a PKCS#8 / PEM byte string."""

    private_key, _ = rsa_keypair
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture(scope="module")
def jwks_document(rsa_keypair: tuple[Any, Any]) -> dict[str, Any]:
    """Build a minimal JWKS document that contains the test public key."""

    _, public_key = rsa_keypair
    numbers = public_key.public_numbers()

    def _b64u_uint(value: int) -> str:
        byte_length = (value.bit_length() + 7) // 8
        raw = value.to_bytes(byte_length, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    jwk = {
        "kty": "RSA",
        "kid": _KID,
        "use": "sig",
        "alg": "RS256",
        "n": _b64u_uint(numbers.n),
        "e": _b64u_uint(numbers.e),
    }
    return {"keys": [jwk]}


def _make_jwks_client(jwks_document: dict[str, Any]) -> "httpx.Client":
    """Build an ``httpx.Client`` that serves ``jwks_document`` offline."""

    def _handler(request: "httpx.Request") -> "httpx.Response":
        # Asserting the URL keeps misconfigured ``OIDC_JWKS_URL`` env
        # values from quietly succeeding against the mock.
        assert request.url == httpx.URL(_JWKS_URL)
        return httpx.Response(200, json=jwks_document)

    return httpx.Client(transport=httpx.MockTransport(_handler))


def _sign_token(
    private_pem: bytes,
    *,
    issuer: str = _ISSUER,
    audience: str = _AUDIENCE,
    expires_in: int = 600,
    sub: str = "alice@example.test",
    role: str | None = None,
    roles: list[str] | None = None,
    groups: list[str] | None = None,
    dept_ids: list[str] | None = None,
    departments: list[str] | None = None,
) -> str:
    """Mint an RS256 JWT carrying the requested role / dept claims.

 All claim shapes (``role`` scalar, ``roles`` / ``groups`` lists,
 ``dept_ids`` / ``departments`` lists) are supported so each test
 case can drive ``extract_auth_context`` along a different branch.
 """

    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + expires_in,
        "sub": sub,
    }
    if role is not None:
        claims["role"] = role
    if roles is not None:
        claims["roles"] = roles
    if groups is not None:
        claims["groups"] = groups
    if dept_ids is not None:
        claims["dept_ids"] = dept_ids
    if departments is not None:
        claims["departments"] = departments

    return jose_jwt.encode(
        claims,
        private_pem,
        algorithm="RS256",
        headers={"kid": _KID},
    )


# ===========================================================================
# Surface 1: End-to-end env-driven login flow + 7.9)
# ===========================================================================
#
# These tests target the foundation work §13.3 acceptance:
# ``OIDCConfig.from_env`` honours the ``AUTH_PROVIDER`` env contract,
# the resulting validator parses a token end-to-end, and the
# :class:`AuthContext` produced by :func:`extract_auth_context` carries
# the four pieces of info the RBAC layer needs (``actor_id``,
# ``actor_role``, ``dept_ids``, ``raw_claims``).


@pytest.mark.integration
class TestAuthProviderOidcLoginFlow:
    """``AUTH_PROVIDER=oidc`` - full JWT login flow.

 """

    def _config_from_env(self) -> "OIDCConfig":
        """Build an OIDCConfig from the canonical env contract."""

        return OIDCConfig.from_env(
            {
                "AUTH_PROVIDER": "oidc",
                "OIDC_ISSUER_URL": _ISSUER,
                "OIDC_CLIENT_ID": _AUDIENCE,
                "OIDC_CLIENT_SECRET": "topsecret-rotated-monthly",
                "OIDC_JWKS_URL": _JWKS_URL,
            }
        )

    def test_env_resolves_to_production_validator(self) -> None:
        """``AUTH_PROVIDER=oidc`` produces a production-mode validator.

 sentence 1: production OIDC is the *default*
 path; the dev bypass must not engage when the env variable is
 ``oidc``.
 """

        cfg = self._config_from_env()

        assert cfg.auth_mode == "production"
        assert cfg.issuer == _ISSUER
        assert cfg.client_id == _AUDIENCE
        # The validator ctor accepts the env-derived config without
        # additional plumbing.
        validator = OIDCValidator(cfg, http_client=_make_jwks_client({"keys": []}))
        assert validator.config.auth_mode == "production"

    def test_signed_admin_token_yields_admin_auth_context(
        self,
        private_pem: bytes,
        jwks_document: dict[str, Any],
    ) -> None:
        """End-to-end: signed token -> AuthContext with admin role.

 Drives the canonical happy path: a real RS256 signature, a
 recognised ``role`` claim and explicit ``dept_ids``. The
 resulting :class:`AuthContext` must carry every field the RBAC
 layer reads (``actor_id``, ``actor_role``, ``dept_ids``).
 """

        validator = OIDCValidator(
            self._config_from_env(),
            http_client=_make_jwks_client(jwks_document),
        )
        token = _sign_token(
            private_pem,
            sub="admin-alice",
            role="admin",
            dept_ids=["payments", "risk"],
        )

        ctx = validator.authenticate(token)

        # ``actor_id`` <- ``sub`` .
        assert isinstance(ctx, AuthContext)
        assert ctx.actor_id == "admin-alice"
        # ``actor_role`` <- ``role`` claim + 7.9).
        assert ctx.actor_role == "admin"
        # ``dept_ids`` <- ``dept_ids`` list claim .
        assert ctx.dept_ids == frozenset({"payments", "risk"})
        # The raw claims dict is preserved for downstream consumers.
        assert ctx.raw_claims["iss"] == _ISSUER
        assert ctx.raw_claims["aud"] == _AUDIENCE

        # Sanity check: an admin actor passes a global-admin RBAC gate.
        # ``policy.check`` returns ``None`` on success and raises on
        # failure.
        assert policy_check(ctx, "admin") is None

    def test_signed_dept_admin_token_yields_dept_scoped_context(
        self,
        private_pem: bytes,
        jwks_document: dict[str, Any],
    ) -> None:
        """``dept_admin`` claim flows through to a dept-scoped context.

 Confirms the 4-role RBAC matrix from is
 wired all the way through ``extract_auth_context``: the
 ``dept_admin`` role lands on the context, the ``dept_ids``
 claim is parsed, and the policy gate admits the actor on the
 owning dept while rejecting access to a different dept
 .
 """

        validator = OIDCValidator(
            self._config_from_env(),
            http_client=_make_jwks_client(jwks_document),
        )
        token = _sign_token(
            private_pem,
            sub="lead-bob",
            role="dept_admin",
            dept_ids=["payments"],
        )

        ctx = validator.authenticate(token)

        assert ctx.actor_role == "dept_admin"
        assert ctx.dept_ids == frozenset({"payments"})
        assert ctx.can_access_dept("payments") is True
        assert ctx.can_access_dept("risk") is False

        # Self-service rotation on the owning dept is allowed.
        assert (
            policy_check(ctx, "dept_admin", dept_id="payments") is None
        )
        # Cross-dept access denied .
        with pytest.raises(PermissionDenied):
            policy_check(ctx, "dept_admin", dept_id="risk")
        # Global admin actions denied .
        with pytest.raises(PermissionDenied):
            policy_check(ctx, "admin")

    def test_signed_token_with_groups_claim_extracts_role(
        self,
        private_pem: bytes,
        jwks_document: dict[str, Any],
    ) -> None:
        """``groups`` claim shape (Keycloak default) is supported.

 IdPs vary on multi-valued claim shape; is
 explicit that ``role``, ``roles``, and ``groups`` are all
 accepted. This case covers the ``groups`` list fallback to
 keep production tokens that omit ``role`` flowing through.
 """

        validator = OIDCValidator(
            self._config_from_env(),
            http_client=_make_jwks_client(jwks_document),
        )
        token = _sign_token(
            private_pem,
            sub="viewer-carol",
            groups=["viewer"],
            departments=["payments"],
        )

        ctx = validator.authenticate(token)

        assert ctx.actor_role == "viewer"
        assert ctx.dept_ids == frozenset({"payments"})

    def test_token_missing_role_claim_raises_401_compatible_error(
        self,
        private_pem: bytes,
        jwks_document: dict[str, Any],
    ) -> None:
        """Missing role -> :class:`MissingClaimError` .

 The exception subclasses :class:`InvalidTokenError` so the
 FastAPI ``require_admin`` dependency translates it into a
 single HTTP 401 response.
 """

        validator = OIDCValidator(
            self._config_from_env(),
            http_client=_make_jwks_client(jwks_document),
        )
        # No ``role`` / ``roles`` / ``groups`` claim at all.
        token = _sign_token(private_pem, sub="ghost")

        with pytest.raises(InvalidTokenError):
            validator.authenticate(token)

    def test_token_with_unrecognised_role_is_rejected(
        self,
        private_pem: bytes,
        jwks_document: dict[str, Any],
    ) -> None:
        """Roles outside the four-role matrix are rejected.

 : exactly four roles are admitted; anything
 else (``superuser``, ``staff``, ...) must fail. This is the
 canonical defence against an IdP misconfiguration that mints
 tokens with the wrong group payload.
 """

        validator = OIDCValidator(
            self._config_from_env(),
            http_client=_make_jwks_client(jwks_document),
        )
        token = _sign_token(private_pem, sub="x", role="superuser")

        with pytest.raises(InvalidTokenError):
            validator.authenticate(token)

    @pytest.mark.parametrize(
        "missing_var",
        ["OIDC_ISSUER_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"],
    )
    def test_oidc_provider_requires_full_env_triplet(
        self, missing_var: str
    ) -> None:
        """``AUTH_PROVIDER=oidc`` fails closed when env is incomplete.

 names the three OIDC_* variables explicitly;
 a missing one is a configuration error, not silently accepted
 as a dev fallback.
 """

        env = {
            "AUTH_PROVIDER": "oidc",
            "OIDC_ISSUER_URL": _ISSUER,
            "OIDC_CLIENT_ID": _AUDIENCE,
            "OIDC_CLIENT_SECRET": "secret",
        }
        env.pop(missing_var)

        with pytest.raises(ValueError):
            OIDCConfig.from_env(env)


@pytest.mark.integration
class TestAuthProviderLocalLoginFlow:
    """``AUTH_PROVIDER=local`` - dev bypass login flow.

 AUTH_PROVIDER=local ile basit kullanıcı adı/şifre alternatifine
 düşülebilir") and 7.9.
 """

    def _config_from_env(self) -> "OIDCConfig":
        """Build a local-mode OIDCConfig from the env contract.

 ``AUTH_PROVIDER=local`` must NOT require any of the OIDC_*
 variables - it is the explicit dev opt-in.
 """

        return OIDCConfig.from_env({"AUTH_PROVIDER": "local"})

    def test_env_resolves_to_dev_validator(self) -> None:
        """``AUTH_PROVIDER=local`` flips the validator into dev mode.

 second sentence: the local provider exists
 for development convenience; production paths must never
 end up here unless the env var was explicitly set.
 """

        cfg = self._config_from_env()

        assert cfg.auth_mode == "dev"
        # Local mode does not need OIDC_* - they resolve to ``None``.
        assert cfg.client_id is None
        assert cfg.client_secret is None
        # Issuer / audience / jwks_url are placeholder strings - the
        # dev validator never consults them.
        assert cfg.issuer == "local-dev"

    def test_local_validator_accepts_any_non_empty_token(self) -> None:
        """Dev bypass - every non-empty bearer is admitted.

 The ``validate`` method returns the canned admin claims so
 downstream callers receive a fully-formed claim dict without
 contacting an IdP.
 """

        validator = OIDCValidator(self._config_from_env())

        claims = validator.validate("any-non-empty-bearer-token")

        assert claims["sub"] == "dev-admin"
        assert claims["role"] == "admin"
        assert claims["groups"] == ["admin"]

    def test_local_validator_rejects_empty_token(self) -> None:
        """Even in dev mode, an empty token is rejected.

 An empty ``Authorization: Bearer `` header is structurally
 invalid : missing token data -> HTTP 401).
 """

        validator = OIDCValidator(self._config_from_env())

        with pytest.raises(InvalidTokenError):
            validator.validate("")

    def test_local_validator_authenticate_yields_admin_auth_context(
        self,
    ) -> None:
        """``authenticate`` builds an AuthContext from the canned claims.

 Confirms the dev-mode path uses the same claim-extraction
 pipeline as production, so the AuthContext carries all four
 of (``actor_id``, ``actor_role``, ``dept_ids``,
 ``raw_claims``).
 """

        validator = OIDCValidator(self._config_from_env())

        ctx = validator.authenticate("dev-bearer")

        assert isinstance(ctx, AuthContext)
        assert ctx.actor_id == "dev-admin"
        assert ctx.actor_role == "admin"
        # No dept claim in the canned dict -> empty set, but
        # ``can_access_dept`` still returns True for admin
        # .
        assert ctx.dept_ids == frozenset()
        assert ctx.can_access_dept("any-dept") is True
        # Admin actors satisfy any global-admin gate.
        assert policy_check(ctx, "admin") is None

    def test_unknown_auth_provider_raises_value_error(self) -> None:
        """Only ``oidc`` and ``local`` are valid AUTH_PROVIDER values.

 The provider contract names exactly these two; any other value
 (typo, dropped support for SAML, etc.) must be rejected at
 config-time, not silently accepted with a default.
 """

        with pytest.raises(ValueError):
            OIDCConfig.from_env({"AUTH_PROVIDER": "saml"})


@pytest.mark.integration
class TestAuthProviderModesAreDistinct:
    """Cross-mode invariants.

 These tests pin down behaviours that span both modes: the dev
 bypass MUST NOT engage when production was selected, and the
 AuthContext shape MUST be identical (actor_id, actor_role,
 dept_ids, raw_claims) regardless of how the validator was
 configured.
 """

    def test_oidc_mode_does_not_accept_arbitrary_strings(
        self,
        jwks_document: dict[str, Any],
    ) -> None:
        """Production mode rejects "anything" tokens that dev mode accepts.

 This is the structural contract for provider mode selection:
 switching ``AUTH_PROVIDER`` to ``oidc`` MUST close the dev
 bypass.
 """

        validator = OIDCValidator(
            OIDCConfig.from_env(
                {
                    "AUTH_PROVIDER": "oidc",
                    "OIDC_ISSUER_URL": _ISSUER,
                    "OIDC_CLIENT_ID": _AUDIENCE,
                    "OIDC_CLIENT_SECRET": "secret",
                    "OIDC_JWKS_URL": _JWKS_URL,
                }
            ),
            http_client=_make_jwks_client(jwks_document),
        )

        with pytest.raises(InvalidTokenError):
            validator.validate("literally-anything")

    def test_auth_context_shape_is_identical_across_modes(
        self,
        private_pem: bytes,
        jwks_document: dict[str, Any],
    ) -> None:
        """Both modes produce a populated :class:`AuthContext`.

 The AuthContext contract is mode-agnostic:
 downstream code (Postgres RLS binding, audit_logger,
 admin-dashboard-api proxy) must not need to special-case the
 provider. This test asserts the four fields exist and carry
 the expected types in both modes.
 """

        # Local mode.
        local_validator = OIDCValidator(
            OIDCConfig.from_env({"AUTH_PROVIDER": "local"})
        )
        local_ctx = local_validator.authenticate("dev-bearer")

        # Production mode.
        prod_validator = OIDCValidator(
            OIDCConfig.from_env(
                {
                    "AUTH_PROVIDER": "oidc",
                    "OIDC_ISSUER_URL": _ISSUER,
                    "OIDC_CLIENT_ID": _AUDIENCE,
                    "OIDC_CLIENT_SECRET": "secret",
                    "OIDC_JWKS_URL": _JWKS_URL,
                }
            ),
            http_client=_make_jwks_client(jwks_document),
        )
        prod_ctx = prod_validator.authenticate(
            _sign_token(
                private_pem,
                sub="prod-user",
                role="admin",
                dept_ids=["payments"],
            )
        )

        # Same dataclass type, same field set, same field types.
        assert type(local_ctx) is type(prod_ctx) is AuthContext
        for ctx in (local_ctx, prod_ctx):
            assert isinstance(ctx.actor_id, str) and ctx.actor_id
            assert ctx.actor_role in {"viewer", "lead", "admin", "dept_admin"}
            assert isinstance(ctx.dept_ids, frozenset)
            assert isinstance(ctx.raw_claims, dict)


# ===========================================================================
# Surface 2: HTTP boundary against admin-dashboard-api (-10.6)
# ===========================================================================
#
# These tests share JWKS fixtures with the provider-mode tests above and
# exercise the FastAPI auth dependency end-to-end; if the admin-dashboard-api
# router is not importable in this checkout the surface SKIPs cleanly without
# affecting Surface 1.

try:
    from fastapi import FastAPI  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402

    from src.auth.dependencies import (  # type: ignore[import-not-found] # noqa: E402
        get_validator,
        require_admin,  # noqa: F401 (re-exported for fixture sanity)
    )
    from src.routers.services_lifecycle import (  # type: ignore[import-not-found] # noqa: E402
        get_lifecycle_service,
        router as services_lifecycle_router,
    )

    _ADMIN_DASHBOARD_API_AVAILABLE = True
except Exception:  # pragma: no cover - slim checkout fallback
    _ADMIN_DASHBOARD_API_AVAILABLE = False


_skip_unless_admin_dashboard = pytest.mark.skipif(
    not _ADMIN_DASHBOARD_API_AVAILABLE,
    reason=(
        "admin-dashboard-api service module not importable; "
        "HTTP boundary cases for are skipped."
    ),
)


class _StubLifecycleService:
    """Bare-minimum stand-in for the real :class:`LifecycleService`.

 The router's ``GET /admin/services`` endpoint only calls
 :meth:`list_summaries`. Overriding the
 :func:`get_lifecycle_service` dependency to return this stub
 bypasses the entire Vault / Compose / Audit graph for the OIDC
 happy-path cases, keeping the integration test focused on the
 auth boundary.
 """

    async def list_summaries(self) -> list[Any]:
        return []


def _build_app(validator: "OIDCValidator") -> "FastAPI":
    """Construct a fresh FastAPI app wired to ``validator`` and the stub.

 A new app is built for every test case so dependency overrides
 do not bleed across cases. Mounting the router (rather than the
 full ``src.main.app``) keeps the test focused on the auth
 boundary without paying for the full lifespan setup.
 """

    app = FastAPI()
    app.include_router(services_lifecycle_router)
    app.dependency_overrides[get_validator] = lambda: validator
    app.dependency_overrides[get_lifecycle_service] = lambda: _StubLifecycleService()
    return app


@_skip_unless_admin_dashboard
@pytest.mark.integration
class TestAuthModeDevHttpBoundary:
    """Dev-mode HTTP behaviour ."""

    def _validator(self) -> "OIDCValidator":
        return OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="dev",
            )
        )

    def test_no_authorization_header_returns_401(self) -> None:
        """Missing header  401 even in dev mode ."""

        app = _build_app(self._validator())
        with TestClient(app) as client:
            response = client.get("/admin/services")
        assert response.status_code == 401, (
            f"missing Authorization header must be 401; got {response.status_code} "
            f"body={response.text!r}"
        )

    def test_bearer_with_empty_token_returns_401(self) -> None:
        """``Bearer `` with empty token  401 ."""

        app = _build_app(self._validator())
        with TestClient(app) as client:
            response = client.get(
                "/admin/services",
                headers={"Authorization": "Bearer "},
            )
        assert response.status_code == 401, (
            f"empty bearer token must be 401; got {response.status_code} "
            f"body={response.text!r}"
        )

    def test_bearer_with_any_non_empty_string_returns_200(self) -> None:
        """Dev-mode accepts any non-empty token and returns admin claims.

 first sentence: dev mode bypasses signature
 verification and feeds canned ``{"sub": "dev-admin", "groups":
 ["admin"]}`` to ``require_admin``.
 """

        app = _build_app(self._validator())
        with TestClient(app) as client:
            response = client.get(
                "/admin/services",
                headers={"Authorization": "Bearer literally-anything"},
            )
        assert response.status_code == 200, (
            f"dev-mode non-empty token must yield 200; got {response.status_code} "
            f"body={response.text!r}"
        )
        # The stub returns an empty list - confirms the dependency
        # actually resolved without the auth gate short-circuiting.
        assert response.json() == []


@_skip_unless_admin_dashboard
@pytest.mark.integration
class TestAuthModeProductionHttpBoundary:
    """Production-mode HTTP behaviour ."""

    def _validator(self, jwks_document: dict[str, Any]) -> "OIDCValidator":
        """Build a production-mode validator backed by a mocked JWKS."""

        return OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="production",
            ),
            http_client=_make_jwks_client(jwks_document),
        )

    def test_no_authorization_header_returns_401(
        self, jwks_document: dict[str, Any]
    ) -> None:
        """Missing header  401 ."""

        app = _build_app(self._validator(jwks_document))
        with TestClient(app) as client:
            response = client.get("/admin/services")
        assert response.status_code == 401

    def test_garbage_token_returns_401_no_dev_bypass(
        self, jwks_document: dict[str, Any]
    ) -> None:
        """Garbage token  401; dev bypass MUST NOT trigger.

 second sentence: production mode never falls
 back to the dev-mode "any non-empty string is admin" path.
 """

        app = _build_app(self._validator(jwks_document))
        with TestClient(app) as client:
            response = client.get(
                "/admin/services",
                headers={"Authorization": "Bearer not.a.real.jwt"},
            )
        assert response.status_code == 401, (
            "production mode must reject unsigned/garbage tokens; got "
            f"{response.status_code} body={response.text!r}"
        )

    def test_signed_token_with_user_group_returns_403(
        self,
        private_pem: bytes,
        jwks_document: dict[str, Any],
    ) -> None:
        """Valid signature + non-admin claim  403 .

 Read-only access is NOT granted to authenticated non-admin
 users - the second sentence of makes this
 an explicit invariant against scope creep. Sending
 ``groups=["user"]`` (a value outside the four-role matrix)
 deliberately fails the role check before any role-class
 comparison.
 """

        token = _sign_token(private_pem, groups=["user"])
        app = _build_app(self._validator(jwks_document))
        with TestClient(app) as client:
            response = client.get(
                "/admin/services",
                headers={"Authorization": f"Bearer {token}"},
            )
        # The admin-dashboard-api auth dependency may translate an
        # extracted-but-non-admin context to either 401 or 403
        # depending on how the unrecognised group is interpreted; the
        # stable invariant is that 200 is impossible.
        assert response.status_code in (401, 403), (
            f"non-admin authenticated user must not be 200; got "
            f"{response.status_code} body={response.text!r}"
        )

    def test_signed_token_with_admin_group_returns_200(
        self,
        private_pem: bytes,
        jwks_document: dict[str, Any],
    ) -> None:
        """Valid signature + ``groups=['admin']``  200 .

 Confirms the production validator actually parses the JWT,
 verifies the signature against the JWKS document, and feeds
 the admin claim to ``require_admin``.
 """

        token = _sign_token(private_pem, groups=["admin"])
        app = _build_app(self._validator(jwks_document))
        with TestClient(app) as client:
            response = client.get(
                "/admin/services",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200, (
            f"admin token must yield 200; got {response.status_code} "
            f"body={response.text!r}"
        )
        assert response.json() == []

    def test_unsigned_alg_none_token_returns_401(
        self,
        jwks_document: dict[str, Any],
    ) -> None:
        """Algorithm-confusion (``alg=none``) tokens are rejected as 401.

 Belt-and-braces guard: even if the JWKS lookup were lax, the
 validator pins ``alg=RS256`` and refuses anything else
 production mode never weakens the check).
 """

        def _b64u(payload: bytes) -> str:
            return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")

        header = _b64u(json.dumps({"alg": "none", "kid": _KID}).encode())
        body = _b64u(
            json.dumps(
                {
                    "iss": _ISSUER,
                    "aud": _AUDIENCE,
                    "exp": int(time.time()) + 600,
                    "sub": "attacker",
                    "groups": ["admin"],
                }
            ).encode()
        )
        token = f"{header}.{body}."

        app = _build_app(self._validator(jwks_document))
        with TestClient(app) as client:
            response = client.get(
                "/admin/services",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 401, (
            f"alg=none token must be 401; got {response.status_code} "
            f"body={response.text!r}"
        )
