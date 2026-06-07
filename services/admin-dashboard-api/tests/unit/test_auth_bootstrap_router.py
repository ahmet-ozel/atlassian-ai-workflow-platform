"""Unit tests for the ``POST /auth/bootstrap`` endpoint.
* : Valid token  create admin user, invalidate token, 201.
* : Expired/used token  401 with error body.
* : OIDC active  410 with bootstrap_disabled_oidc_active."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# Bootstrap sys.path so ``import src.*`` resolves under direct
# ``pytest tests/unit`` invocations from the service root.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
_AUTH_SHARED_SRC = _WORKSPACE_ROOT / "libs" / "auth-shared" / "src"
if _AUTH_SHARED_SRC.is_dir() and str(_AUTH_SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(_AUTH_SHARED_SRC))


from src.routers.auth_bootstrap import _is_valid_token_format


# ---------------------------------------------------------------------------
# Token format validation tests
# ---------------------------------------------------------------------------


class TestTokenFormatValidation:
    """Test the _is_valid_token_format helper."""

    def test_valid_base64url_token_43_chars(self) -> None:
        """Standard secrets.token_urlsafe(32) output is 43 chars."""
        # A typical token_urlsafe(32) output
        token = "abcdefghijklmnopqrstuvwxyz0123456789_-ABCDE"
        assert _is_valid_token_format(token) is True

    def test_valid_token_32_chars_minimum(self) -> None:
        """Minimum length of 32 chars is accepted."""
        token = "a" * 32
        assert _is_valid_token_format(token) is True

    def test_valid_token_64_chars_maximum(self) -> None:
        """Maximum length of 64 chars is accepted."""
        token = "b" * 64
        assert _is_valid_token_format(token) is True

    def test_invalid_token_too_short(self) -> None:
        """Tokens shorter than 32 chars are rejected."""
        token = "a" * 31
        assert _is_valid_token_format(token) is False

    def test_invalid_token_too_long(self) -> None:
        """Tokens longer than 64 chars are rejected."""
        token = "a" * 65
        assert _is_valid_token_format(token) is False

    def test_invalid_token_empty_string(self) -> None:
        """Empty string is rejected."""
        assert _is_valid_token_format("") is False

    def test_invalid_token_with_spaces(self) -> None:
        """Tokens with spaces are rejected."""
        token = "a" * 16 + " " + "b" * 16
        assert _is_valid_token_format(token) is False

    def test_invalid_token_with_special_chars(self) -> None:
        """Tokens with non-base64url characters are rejected."""
        token = "a" * 31 + "!"
        assert _is_valid_token_format(token) is False

    def test_valid_token_with_underscore_and_hyphen(self) -> None:
        """Base64url uses _ and - as special chars."""
        token = "abc_def-ghi_jkl-mno_pqr-stu_vwx-yz0"
        assert _is_valid_token_format(token) is True


# ---------------------------------------------------------------------------
# POST /auth/bootstrap endpoint tests
# ---------------------------------------------------------------------------


class TestBootstrapEndpoint:
    """Test the POST /auth/bootstrap endpoint."""

    @pytest.fixture
    def mock_pool(self):
        """Create a mock asyncpg pool."""
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
        conn.execute = AsyncMock()
        return pool

    @pytest.fixture
    def client(self, mock_pool):
        """Create a test client with mocked app state."""
        from src.main import app

        with TestClient(app, raise_server_exceptions=False) as c:
            # Set pg_pool AFTER lifespan runs (lifespan may set it to None
            # when it can't connect to a real database).
            app.state.pg_pool = mock_pool
            yield c

    def test_oidc_active_returns_410(self, client, mock_pool) -> None:
        """OIDC active  410 Gone."""
        with patch(
            "src.auth.bootstrap.BootstrapTokenService.is_oidc_configured",
            return_value=True,
            new_callable=AsyncMock,
        ):
            response = client.post(
                "/auth/bootstrap",
                json={"token": "a" * 43},
            )

        assert response.status_code == 410
        body = response.json()
        assert body["detail"]["error"] == "bootstrap_disabled_oidc_active"

    def test_invalid_token_format_returns_400(self, client) -> None:
        """Invalid format  400."""
        with patch(
            "src.auth.bootstrap.BootstrapTokenService.is_oidc_configured",
            return_value=False,
            new_callable=AsyncMock,
        ):
            response = client.post(
                "/auth/bootstrap",
                json={"token": "short"},
            )

        assert response.status_code == 400
        body = response.json()
        assert body["detail"]["error"] == "invalid_token_format"

    def test_expired_or_used_token_returns_401(self, client, mock_pool) -> None:
        """Expired/used token  401."""
        with patch(
            "src.auth.bootstrap.BootstrapTokenService.is_oidc_configured",
            return_value=False,
            new_callable=AsyncMock,
        ), patch(
            "src.auth.bootstrap.BootstrapTokenService.validate_and_consume",
            return_value=False,
            new_callable=AsyncMock,
        ):
            response = client.post(
                "/auth/bootstrap",
                json={"token": "a" * 43},
            )

        assert response.status_code == 401
        body = response.json()
        assert body["detail"]["error"] == "bootstrap_token_expired_or_used"

    def test_valid_token_creates_admin_returns_201(
        self, client, mock_pool
    ) -> None:
        """Valid token  create admin, 201."""
        with patch(
            "src.auth.bootstrap.BootstrapTokenService.is_oidc_configured",
            return_value=False,
            new_callable=AsyncMock,
        ), patch(
            "src.auth.bootstrap.BootstrapTokenService.validate_and_consume",
            return_value=True,
            new_callable=AsyncMock,
        ):
            response = client.post(
                "/auth/bootstrap",
                json={"token": "a" * 43},
            )

        assert response.status_code == 201
        body = response.json()
        assert body["message"] == "admin_created"
        assert "user_id" in body
        # user_id should be a valid UUID format
        import uuid

        uuid.UUID(body["user_id"])  # Raises if invalid

    def test_missing_token_field_returns_422(self, client) -> None:
        """Missing required 'token' field  422 Unprocessable Entity."""
        response = client.post(
            "/auth/bootstrap",
            json={},
        )
        assert response.status_code == 422

    def test_empty_token_returns_422(self, client) -> None:
        """Empty token string  422 (pydantic min_length=1 validation)."""
        response = client.post(
            "/auth/bootstrap",
            json={"token": ""},
        )
        assert response.status_code == 422

    def test_no_db_pool_returns_503(self) -> None:
        """When pg_pool is None  503 Service Unavailable."""
        from src.main import app

        original_pg_pool = getattr(app.state, "pg_pool", None)
        app.state.pg_pool = None

        try:
            with patch(
                "src.auth.bootstrap.BootstrapTokenService.is_oidc_configured",
                return_value=False,
                new_callable=AsyncMock,
            ):
                with TestClient(app, raise_server_exceptions=False) as client:
                    response = client.post(
                        "/auth/bootstrap",
                        json={"token": "a" * 43},
                    )

            assert response.status_code == 503
        finally:
            app.state.pg_pool = original_pg_pool
