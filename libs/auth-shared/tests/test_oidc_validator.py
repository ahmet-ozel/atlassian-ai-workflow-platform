"""Unit tests for :class:`auth_shared.OIDCValidator`.

Covers the four behaviours called out in tasks.md §4.1:

1. ``auth_mode="dev"`` rejects empty tokens with ``InvalidTokenError``
   and returns the canned admin claims for any non-empty token.
2. ``auth_mode="production"`` rejects missing / expired / wrong-issuer
   / wrong-audience tokens with ``InvalidTokenError``.
3. A token signed with the configured key and matching claims is
   accepted and the decoded claim dict is returned.
4. The JWKS document is cached in-memory so repeated validations do
   not refetch on every call (TTL ≥ 5 minutes).

The tests deliberately avoid network I/O by injecting an
``httpx.Client`` whose ``transport`` is an
``httpx.MockTransport``. RS256 keys are generated freshly per test
module so the suite is hermetic.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt as jose_jwt

from auth_shared import InvalidTokenError, OIDCConfig, OIDCValidator


# ---------------------------------------------------------------------------
# Test fixtures: a single RSA keypair shared by every signed-token test.
# ---------------------------------------------------------------------------

_ISSUER = "https://idp.example.test/"
_AUDIENCE = "admin-dashboard-api"
_JWKS_URL = "https://idp.example.test/.well-known/jwks.json"
_KID = "test-key-1"


@pytest.fixture(scope="module")
def rsa_keypair() -> tuple[Any, Any]:
    """Generate a fresh RSA-2048 keypair for the module."""

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture(scope="module")
def private_pem(rsa_keypair: tuple[Any, Any]) -> bytes:
    private_key, _ = rsa_keypair
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture(scope="module")
def jwks_document(rsa_keypair: tuple[Any, Any]) -> dict[str, Any]:
    """Build a minimal JWKS document containing the public key."""

    _, public_key = rsa_keypair
    numbers = public_key.public_numbers()

    def _b64u_uint(value: int) -> str:
        import base64

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


def _make_jwks_client(
    jwks_document: dict[str, Any], *, call_counter: list[int] | None = None
) -> httpx.Client:
    """Build an ``httpx.Client`` that serves ``jwks_document`` offline."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if call_counter is not None:
            call_counter.append(1)
        assert request.url == httpx.URL(_JWKS_URL)
        return httpx.Response(200, json=jwks_document)

    return httpx.Client(transport=httpx.MockTransport(_handler))


def _sign_token(
    private_pem: bytes,
    *,
    issuer: str = _ISSUER,
    audience: str = _AUDIENCE,
    expires_in: int = 600,
    extra_claims: dict[str, Any] | None = None,
    headers: dict[str, Any] | None = None,
) -> str:
    """Mint an RS256 JWT for testing."""

    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + expires_in,
        "sub": "alice@example.test",
        "groups": ["admin"],
    }
    if extra_claims:
        claims.update(extra_claims)
    return jose_jwt.encode(
        claims,
        private_pem,
        algorithm="RS256",
        headers=headers or {"kid": _KID},
    )


# ---------------------------------------------------------------------------
# Dev-mode tests.
# ---------------------------------------------------------------------------


class TestDevMode:
    def test_empty_token_raises_invalid_token_error(self) -> None:
        validator = OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="dev",
            )
        )

        with pytest.raises(InvalidTokenError):
            validator.validate("")

    def test_non_empty_token_returns_canned_admin_claims(self) -> None:
        validator = OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="dev",
            )
        )

        claims = validator.validate("anything-non-empty")

        # Dev-mode returns the canned admin claim dict. ``role`` is
        # carried alongside the legacy ``groups`` list so that the
        # ``extract_auth_context`` helper introduced in task 8.1
        # (Requirement 7.9) can map ``AUTH_PROVIDER=local`` tokens
        # straight to an :class:`AuthContext` without any plumbing
        # changes downstream.
        assert claims == {
            "sub": "dev-admin",
            "role": "admin",
            "groups": ["admin"],
        }

    def test_dev_mode_does_not_perform_network_io(
        self, jwks_document: dict[str, Any]
    ) -> None:
        """Dev mode never touches the JWKS endpoint."""

        call_counter: list[int] = []
        validator = OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="dev",
            ),
            http_client=_make_jwks_client(jwks_document, call_counter=call_counter),
        )

        validator.validate("anything")

        assert call_counter == []


# ---------------------------------------------------------------------------
# Production-mode tests.
# ---------------------------------------------------------------------------


class TestProductionMode:
    def test_valid_signed_token_returns_claims_dict(
        self, private_pem: bytes, jwks_document: dict[str, Any]
    ) -> None:
        token = _sign_token(private_pem)
        validator = OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="production",
            ),
            http_client=_make_jwks_client(jwks_document),
        )

        claims = validator.validate(token)

        assert claims["iss"] == _ISSUER
        assert claims["aud"] == _AUDIENCE
        assert claims["sub"] == "alice@example.test"
        assert claims["groups"] == ["admin"]

    def test_empty_token_raises_invalid_token_error(
        self, jwks_document: dict[str, Any]
    ) -> None:
        validator = OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="production",
            ),
            http_client=_make_jwks_client(jwks_document),
        )

        with pytest.raises(InvalidTokenError):
            validator.validate("")

    def test_garbage_token_raises_invalid_token_error(
        self, jwks_document: dict[str, Any]
    ) -> None:
        validator = OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="production",
            ),
            http_client=_make_jwks_client(jwks_document),
        )

        with pytest.raises(InvalidTokenError):
            validator.validate("not.a.real.jwt")

    def test_expired_token_raises_invalid_token_error(
        self, private_pem: bytes, jwks_document: dict[str, Any]
    ) -> None:
        token = _sign_token(private_pem, expires_in=-60)
        validator = OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="production",
            ),
            http_client=_make_jwks_client(jwks_document),
        )

        with pytest.raises(InvalidTokenError):
            validator.validate(token)

    def test_wrong_issuer_raises_invalid_token_error(
        self, private_pem: bytes, jwks_document: dict[str, Any]
    ) -> None:
        token = _sign_token(private_pem, issuer="https://attacker.example.test/")
        validator = OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="production",
            ),
            http_client=_make_jwks_client(jwks_document),
        )

        with pytest.raises(InvalidTokenError):
            validator.validate(token)

    def test_wrong_audience_raises_invalid_token_error(
        self, private_pem: bytes, jwks_document: dict[str, Any]
    ) -> None:
        token = _sign_token(private_pem, audience="some-other-api")
        validator = OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="production",
            ),
            http_client=_make_jwks_client(jwks_document),
        )

        with pytest.raises(InvalidTokenError):
            validator.validate(token)

    def test_unknown_kid_raises_invalid_token_error(
        self, private_pem: bytes, jwks_document: dict[str, Any]
    ) -> None:
        token = _sign_token(
            private_pem, headers={"kid": "rotated-out", "alg": "RS256"}
        )
        validator = OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="production",
            ),
            http_client=_make_jwks_client(jwks_document),
        )

        with pytest.raises(InvalidTokenError):
            validator.validate(token)

    def test_non_rs256_algorithm_is_rejected(
        self, private_pem: bytes, jwks_document: dict[str, Any]
    ) -> None:
        """Algorithm confusion attacks (e.g. ``alg=none``) are blocked."""

        # Build an unsigned token with ``alg=none`` manually; the JOSE
        # library's ``encode`` refuses to sign with ``none``, so we
        # craft the segments directly.
        import base64

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
                }
            ).encode()
        )
        token = f"{header}.{body}."

        validator = OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="production",
            ),
            http_client=_make_jwks_client(jwks_document),
        )

        with pytest.raises(InvalidTokenError):
            validator.validate(token)

    def test_jwks_fetch_failure_surfaces_as_invalid_token_error(
        self, private_pem: bytes
    ) -> None:
        """A 5xx response from the IdP is reported as an invalid token."""

        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="upstream down")

        client = httpx.Client(transport=httpx.MockTransport(_handler))
        token = _sign_token(private_pem)
        validator = OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="production",
            ),
            http_client=client,
        )

        with pytest.raises(InvalidTokenError):
            validator.validate(token)


# ---------------------------------------------------------------------------
# JWKS cache TTL.
# ---------------------------------------------------------------------------


class TestJWKSCache:
    def test_jwks_is_fetched_once_for_repeated_validations(
        self, private_pem: bytes, jwks_document: dict[str, Any]
    ) -> None:
        call_counter: list[int] = []
        validator = OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="production",
            ),
            http_client=_make_jwks_client(jwks_document, call_counter=call_counter),
        )

        for _ in range(5):
            validator.validate(_sign_token(private_pem))

        assert len(call_counter) == 1, (
            "JWKS document must be cached across validations; saw "
            f"{len(call_counter)} fetches"
        )

    def test_jwks_cache_ttl_is_clamped_to_at_least_five_minutes(
        self, jwks_document: dict[str, Any]
    ) -> None:
        """Spec: ``JWKS cache TTL min 5 dk``."""

        validator = OIDCValidator(
            OIDCConfig(
                issuer=_ISSUER,
                audience=_AUDIENCE,
                jwks_url=_JWKS_URL,
                auth_mode="production",
            ),
            jwks_cache_ttl_seconds=10,  # Try to relax below the minimum.
            http_client=_make_jwks_client(jwks_document),
        )

        # Internal attribute is intentional — the spec encodes a hard
        # lower bound that callers must not be able to weaken.
        assert validator._jwks_cache_ttl_seconds >= 300
