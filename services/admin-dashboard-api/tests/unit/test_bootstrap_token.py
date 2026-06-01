"""Unit tests for the BootstrapTokenService class.

Validates Requirements 2.1, 2.2, 2.3, 2.4, 2.5 from the production-hardening spec:

* Requirement 2.1: Token generation when no admin exists (stdout output).
* Requirement 2.2: Token has 1-hour TTL; expired tokens are invalid.
* Requirement 2.3: Valid token → consumed successfully.
* Requirement 2.4: Expired/used token → validation fails.
* Requirement 2.5: OIDC configured → bootstrap mechanism disabled.
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Bootstrap sys.path so ``import src.*`` resolves under direct
# ``pytest tests/unit`` invocations from the service root.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
_AUTH_SHARED_SRC = _WORKSPACE_ROOT / "libs" / "auth-shared" / "src"
if _AUTH_SHARED_SRC.is_dir() and str(_AUTH_SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(_AUTH_SHARED_SRC))

from src.auth.bootstrap import BootstrapTokenService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_pool(
    *,
    admin_exists: bool = False,
    valid_token_exists: bool = False,
    consume_returns_row: bool = True,
) -> MagicMock:
    """Create a mock asyncpg pool with configurable query responses."""
    pool = MagicMock()
    conn = AsyncMock()

    # Setup context manager for pool.acquire()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    # Setup fetchval responses for generate_if_needed
    async def _fetchval_side_effect(query, *args):
        if "consumed_at IS NOT NULL" in query:
            return admin_exists
        if "consumed_at IS NULL" in query and "expires_at > now()" in query:
            return valid_token_exists
        return None

    conn.fetchval = AsyncMock(side_effect=_fetchval_side_effect)
    conn.execute = AsyncMock()

    # Setup fetchrow response for validate_and_consume
    if consume_returns_row:
        conn.fetchrow = AsyncMock(return_value={"id": "test-uuid-123"})
    else:
        conn.fetchrow = AsyncMock(return_value=None)

    return pool


# ---------------------------------------------------------------------------
# Token Generation (Requirement 2.1)
# ---------------------------------------------------------------------------


class TestGenerateIfNeeded:
    """Test BootstrapTokenService.generate_if_needed."""

    @pytest.mark.asyncio
    async def test_generates_token_when_no_admin_exists(self) -> None:
        """Requirement 2.1: First boot with no admin → generates token."""
        service = BootstrapTokenService()
        pool = _make_mock_pool(admin_exists=False, valid_token_exists=False)

        token = await service.generate_if_needed(pool)

        assert token is not None
        assert len(token) > 0
        # Token should be a base64url-safe string
        assert all(c.isalnum() or c in "-_" for c in token)

    @pytest.mark.asyncio
    async def test_returns_none_when_admin_already_exists(self) -> None:
        """Requirement 2.1: Admin already bootstrapped → no new token."""
        service = BootstrapTokenService()
        pool = _make_mock_pool(admin_exists=True)

        token = await service.generate_if_needed(pool)

        assert token is None

    @pytest.mark.asyncio
    async def test_returns_none_when_valid_token_already_pending(self) -> None:
        """Idempotency: valid unexpired token exists → no new token."""
        service = BootstrapTokenService()
        pool = _make_mock_pool(admin_exists=False, valid_token_exists=True)

        token = await service.generate_if_needed(pool)

        assert token is None

    @pytest.mark.asyncio
    async def test_stores_sha256_hash_not_plain_token(self) -> None:
        """Security: only the SHA-256 hash is stored in the database."""
        service = BootstrapTokenService()
        pool = _make_mock_pool(admin_exists=False, valid_token_exists=False)

        # Get the mock connection
        conn = await pool.acquire().__aenter__()

        token = await service.generate_if_needed(pool)

        # Verify execute was called with the hash, not the plain token
        execute_calls = conn.execute.call_args_list
        assert len(execute_calls) > 0

        # The INSERT call should contain the token hash
        insert_call = execute_calls[-1]
        stored_hash = insert_call[0][1]  # Second positional arg
        expected_hash = hashlib.sha256(token.encode()).hexdigest()
        assert stored_hash == expected_hash

    @pytest.mark.asyncio
    async def test_prints_token_to_stdout(self, capsys) -> None:
        """Requirement 2.1: Token is printed to stdout for operator."""
        service = BootstrapTokenService()
        pool = _make_mock_pool(admin_exists=False, valid_token_exists=False)

        token = await service.generate_if_needed(pool)

        captured = capsys.readouterr()
        assert token in captured.out
        assert "BOOTSTRAP ADMIN TOKEN" in captured.out

    @pytest.mark.asyncio
    async def test_token_expires_in_one_hour(self) -> None:
        """Requirement 2.2: Token TTL is 1 hour."""
        service = BootstrapTokenService()
        pool = _make_mock_pool(admin_exists=False, valid_token_exists=False)

        conn = await pool.acquire().__aenter__()

        await service.generate_if_needed(pool)

        # Verify the expires_at passed to INSERT is ~1 hour from now
        execute_calls = conn.execute.call_args_list
        insert_call = execute_calls[-1]
        expires_at = insert_call[0][2]  # Third positional arg

        now = datetime.now(timezone.utc)
        expected_expiry = now + timedelta(hours=1)

        # Allow 5 seconds tolerance for test execution time
        assert abs((expires_at - expected_expiry).total_seconds()) < 5


# ---------------------------------------------------------------------------
# Token Validation and Consumption (Requirements 2.3, 2.4)
# ---------------------------------------------------------------------------


class TestValidateAndConsume:
    """Test BootstrapTokenService.validate_and_consume."""

    @pytest.mark.asyncio
    async def test_valid_token_is_consumed_successfully(self) -> None:
        """Requirement 2.3: Valid token → consumed, returns True."""
        service = BootstrapTokenService()
        pool = _make_mock_pool(consume_returns_row=True)

        result = await service.validate_and_consume("valid-token-abc123xyz", pool)

        assert result is True

    @pytest.mark.asyncio
    async def test_invalid_token_returns_false(self) -> None:
        """Requirement 2.4: Invalid/expired/used token → returns False."""
        service = BootstrapTokenService()
        pool = _make_mock_pool(consume_returns_row=False)

        result = await service.validate_and_consume("invalid-token", pool)

        assert result is False

    @pytest.mark.asyncio
    async def test_validates_using_sha256_hash(self) -> None:
        """Token is hashed before DB lookup (plain token never sent to DB)."""
        service = BootstrapTokenService()
        pool = _make_mock_pool(consume_returns_row=True)

        conn = await pool.acquire().__aenter__()

        test_token = "my-secret-bootstrap-token-12345"
        await service.validate_and_consume(test_token, pool)

        # Verify the UPDATE query used the hash, not the plain token
        fetchrow_calls = conn.fetchrow.call_args_list
        assert len(fetchrow_calls) > 0

        update_call = fetchrow_calls[-1]
        passed_hash = update_call[0][1]  # Second positional arg
        expected_hash = hashlib.sha256(test_token.encode()).hexdigest()
        assert passed_hash == expected_hash

    @pytest.mark.asyncio
    async def test_consumed_token_cannot_be_reused(self) -> None:
        """Requirement 2.4: Already consumed token → returns False."""
        service = BootstrapTokenService()

        # First call succeeds (row returned)
        pool_first = _make_mock_pool(consume_returns_row=True)
        result_first = await service.validate_and_consume("token-abc", pool_first)
        assert result_first is True

        # Second call fails (no row returned — already consumed)
        pool_second = _make_mock_pool(consume_returns_row=False)
        result_second = await service.validate_and_consume("token-abc", pool_second)
        assert result_second is False


# ---------------------------------------------------------------------------
# TTL Expiry Behavior (Requirement 2.2)
# ---------------------------------------------------------------------------


class TestTTLExpiry:
    """Test token TTL expiry behavior."""

    def test_token_ttl_is_one_hour(self) -> None:
        """Requirement 2.2: TOKEN_TTL is exactly 1 hour."""
        service = BootstrapTokenService()
        assert service.TOKEN_TTL == timedelta(hours=1)

    @pytest.mark.asyncio
    async def test_expired_token_validation_fails(self) -> None:
        """Requirement 2.2/2.4: Expired token → validation returns False.

        The DB query includes `expires_at > now()` so expired tokens
        won't match, resulting in fetchrow returning None.
        """
        service = BootstrapTokenService()
        # Simulate expired token: DB returns None (no matching row)
        pool = _make_mock_pool(consume_returns_row=False)

        result = await service.validate_and_consume("expired-token-xyz", pool)

        assert result is False

    @pytest.mark.asyncio
    async def test_generate_skips_when_expired_token_exists_but_no_valid_one(
        self,
    ) -> None:
        """When only expired tokens exist (no valid pending), generate new one."""
        service = BootstrapTokenService()
        # admin_exists=False, valid_token_exists=False means all existing
        # tokens are expired → should generate a new one
        pool = _make_mock_pool(admin_exists=False, valid_token_exists=False)

        token = await service.generate_if_needed(pool)

        assert token is not None


# ---------------------------------------------------------------------------
# OIDC Disables Bootstrap (Requirement 2.5)
# ---------------------------------------------------------------------------


class TestOIDCConfiguration:
    """Test BootstrapTokenService.is_oidc_configured."""

    @pytest.mark.asyncio
    async def test_oidc_configured_when_all_vars_set_and_production_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement 2.5: OIDC fully configured → returns True."""
        monkeypatch.setenv("AUTH_MODE", "production")
        monkeypatch.setenv("OIDC_ISSUER", "https://accounts.google.com")
        monkeypatch.setenv("OIDC_AUDIENCE", "my-app-client-id")
        monkeypatch.setenv("OIDC_JWKS_URL", "https://accounts.google.com/.well-known/jwks.json")

        service = BootstrapTokenService()
        result = await service.is_oidc_configured()

        assert result is True

    @pytest.mark.asyncio
    async def test_oidc_not_configured_when_auth_mode_is_dev(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requirement 2.5: AUTH_MODE=dev → OIDC not configured."""
        monkeypatch.setenv("AUTH_MODE", "dev")
        monkeypatch.setenv("OIDC_ISSUER", "https://accounts.google.com")
        monkeypatch.setenv("OIDC_AUDIENCE", "my-app-client-id")
        monkeypatch.setenv("OIDC_JWKS_URL", "https://accounts.google.com/.well-known/jwks.json")

        service = BootstrapTokenService()
        result = await service.is_oidc_configured()

        assert result is False

    @pytest.mark.asyncio
    async def test_oidc_not_configured_when_issuer_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing OIDC_ISSUER → not configured."""
        monkeypatch.setenv("AUTH_MODE", "production")
        monkeypatch.delenv("OIDC_ISSUER", raising=False)
        monkeypatch.setenv("OIDC_AUDIENCE", "my-app-client-id")
        monkeypatch.setenv("OIDC_JWKS_URL", "https://example.com/jwks")

        service = BootstrapTokenService()
        result = await service.is_oidc_configured()

        assert result is False

    @pytest.mark.asyncio
    async def test_oidc_not_configured_when_audience_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing OIDC_AUDIENCE → not configured."""
        monkeypatch.setenv("AUTH_MODE", "production")
        monkeypatch.setenv("OIDC_ISSUER", "https://accounts.google.com")
        monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
        monkeypatch.setenv("OIDC_JWKS_URL", "https://example.com/jwks")

        service = BootstrapTokenService()
        result = await service.is_oidc_configured()

        assert result is False

    @pytest.mark.asyncio
    async def test_oidc_not_configured_when_jwks_url_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing OIDC_JWKS_URL → not configured."""
        monkeypatch.setenv("AUTH_MODE", "production")
        monkeypatch.setenv("OIDC_ISSUER", "https://accounts.google.com")
        monkeypatch.setenv("OIDC_AUDIENCE", "my-app-client-id")
        monkeypatch.delenv("OIDC_JWKS_URL", raising=False)

        service = BootstrapTokenService()
        result = await service.is_oidc_configured()

        assert result is False

    @pytest.mark.asyncio
    async def test_oidc_not_configured_when_vars_are_empty_strings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty string values → not configured."""
        monkeypatch.setenv("AUTH_MODE", "production")
        monkeypatch.setenv("OIDC_ISSUER", "")
        monkeypatch.setenv("OIDC_AUDIENCE", "")
        monkeypatch.setenv("OIDC_JWKS_URL", "")

        service = BootstrapTokenService()
        result = await service.is_oidc_configured()

        assert result is False

    @pytest.mark.asyncio
    async def test_oidc_not_configured_when_vars_are_whitespace_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whitespace-only values are treated as empty → not configured."""
        monkeypatch.setenv("AUTH_MODE", "production")
        monkeypatch.setenv("OIDC_ISSUER", "   ")
        monkeypatch.setenv("OIDC_AUDIENCE", "  ")
        monkeypatch.setenv("OIDC_JWKS_URL", " ")

        service = BootstrapTokenService()
        result = await service.is_oidc_configured()

        assert result is False

    @pytest.mark.asyncio
    async def test_oidc_not_configured_when_no_env_vars_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No OIDC env vars set at all → not configured (default dev mode)."""
        monkeypatch.delenv("AUTH_MODE", raising=False)
        monkeypatch.delenv("OIDC_ISSUER", raising=False)
        monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
        monkeypatch.delenv("OIDC_JWKS_URL", raising=False)

        service = BootstrapTokenService()
        result = await service.is_oidc_configured()

        assert result is False
