"""Unit tests for the Credential Guard module.
* : Production + dev-default POSTGRES_PASSWORD → blocked.
* : Production + dev-default VAULT_TOKEN → blocked.
* : /healthz returns 503 when credential_blocked is True.
* : Development env + dev credentials → not blocked (warning only)."""

from __future__ import annotations

import sys
from pathlib import Path

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

from src.lifecycle.credential_guard import (
    CredentialGuardResult,
    DevDefault,
    DEV_DEFAULTS,
    check_credentials,
)


# ---------------------------------------------------------------------------
# check_credentials - production + dev password → blocked (Req 1.1, 1.2)
# ---------------------------------------------------------------------------


class TestProductionBlocked:
    """Production environment with dev-default credentials must be blocked."""

    def test_production_with_dev_postgres_password_is_blocked(self) -> None:
        """PLATFORM_ENV=production + ai_dev_only → blocked."""
        env_vars = {"POSTGRES_PASSWORD": "ai_dev_only"}
        result = check_credentials(platform_env="production", env_vars=env_vars)

        assert result.blocked is True
        assert "Dev-only Postgres password" in result.violations

    def test_production_with_dev_vault_token_is_blocked(self) -> None:
        """PLATFORM_ENV=production + dev vault token → blocked."""
        env_vars = {"VAULT_TOKEN": "dev-token-not-for-prod"}
        result = check_credentials(platform_env="production", env_vars=env_vars)

        assert result.blocked is True
        assert "Dev-only Vault token" in result.violations

    def test_production_with_dev_minio_password_is_blocked(self) -> None:
        """Production + dev MinIO password → blocked."""
        env_vars = {"MINIO_ROOT_PASSWORD": "miniosecret_dev_only"}
        result = check_credentials(platform_env="production", env_vars=env_vars)

        assert result.blocked is True
        assert "Dev-only MinIO password" in result.violations

    def test_production_with_multiple_dev_credentials_reports_all(self) -> None:
        """All violations are reported when multiple dev credentials present."""
        env_vars = {
            "POSTGRES_PASSWORD": "ai_dev_only",
            "VAULT_TOKEN": "dev-token-not-for-prod",
            "MINIO_ROOT_PASSWORD": "miniosecret_dev_only",
        }
        result = check_credentials(platform_env="production", env_vars=env_vars)

        assert result.blocked is True
        assert len(result.violations) == 3


# ---------------------------------------------------------------------------
# check_credentials - development + dev password → NOT blocked (Req 1.4)
# ---------------------------------------------------------------------------


class TestDevelopmentNotBlocked:
    """Non-production environments must not be blocked regardless of credentials."""

    def test_development_with_dev_password_is_not_blocked(self) -> None:
        """PLATFORM_ENV=development + dev creds → not blocked."""
        env_vars = {"POSTGRES_PASSWORD": "ai_dev_only"}
        result = check_credentials(platform_env="development", env_vars=env_vars)

        assert result.blocked is False

    def test_empty_platform_env_with_dev_password_is_not_blocked(self) -> None:
        """Empty PLATFORM_ENV treated as non-production → not blocked."""
        env_vars = {"POSTGRES_PASSWORD": "ai_dev_only"}
        result = check_credentials(platform_env="", env_vars=env_vars)

        assert result.blocked is False

    def test_undefined_platform_env_with_dev_password_is_not_blocked(self) -> None:
        """Undefined (empty string) PLATFORM_ENV → not blocked."""
        env_vars = {
            "POSTGRES_PASSWORD": "ai_dev_only",
            "VAULT_TOKEN": "dev-token-not-for-prod",
        }
        result = check_credentials(platform_env="", env_vars=env_vars)

        assert result.blocked is False
        # Violations are still detected and reported
        assert len(result.violations) == 2

    def test_staging_env_with_dev_password_is_not_blocked(self) -> None:
        """Any non-'production' value → not blocked."""
        env_vars = {"POSTGRES_PASSWORD": "ai_dev_only"}
        result = check_credentials(platform_env="staging", env_vars=env_vars)

        assert result.blocked is False


# ---------------------------------------------------------------------------
# check_credentials - production + secure password → NOT blocked
# ---------------------------------------------------------------------------


class TestProductionSecureNotBlocked:
    """Production with secure (non-dev-default) credentials must not be blocked."""

    def test_production_with_secure_password_is_not_blocked(self) -> None:
        """Production + strong password → not blocked, no violations."""
        env_vars = {
            "POSTGRES_PASSWORD": "super-secure-prod-password-2024!",
            "VAULT_TOKEN": "s.AbCdEfGhIjKlMnOpQrStUv",
            "MINIO_ROOT_PASSWORD": "minio-prod-secret-key-xyz",
        }
        result = check_credentials(platform_env="production", env_vars=env_vars)

        assert result.blocked is False
        assert result.violations == []

    def test_production_with_missing_env_vars_is_not_blocked(self) -> None:
        """Production with env vars not set at all → not blocked."""
        env_vars: dict[str, str] = {}
        result = check_credentials(platform_env="production", env_vars=env_vars)

        assert result.blocked is False
        assert result.violations == []

    def test_production_partial_secure_partial_missing_not_blocked(self) -> None:
        """Production with some secure values and some missing → not blocked."""
        env_vars = {"POSTGRES_PASSWORD": "real-prod-password"}
        result = check_credentials(platform_env="production", env_vars=env_vars)

        assert result.blocked is False
        assert result.violations == []


# ---------------------------------------------------------------------------
# /healthz returns 503 when credential_blocked is True (Req 1.3)
# ---------------------------------------------------------------------------


class TestHealthzCredentialBlocked:
    """The /healthz endpoint must return 503 when credential guard blocks boot."""

    def test_healthz_returns_503_when_credential_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """/healthz → 503 with insecure_credentials reason."""
        import src.main as main_module
        from src.main import app

        # Simulate credential guard blocking boot by patching
        # check_credentials to return a blocked result.
        def _blocked_check(platform_env, env_vars, **kwargs):
            return CredentialGuardResult(
                blocked=True,
                violations=["Dev-only Postgres password"],
            )

        monkeypatch.setattr(main_module, "check_credentials", _blocked_check)

        with TestClient(app) as client:
            response = client.get("/healthz")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["reason"] == "insecure_credentials"

    def test_healthz_returns_200_when_credential_not_blocked(self) -> None:
        """Normal boot (no credential block) → /healthz returns 200."""
        from src.main import app

        with TestClient(app) as client:
            response = client.get("/healthz")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
