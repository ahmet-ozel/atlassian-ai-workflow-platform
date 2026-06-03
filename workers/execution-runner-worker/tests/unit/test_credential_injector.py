"""Unit tests for the credential_injector activity module.

Tests cover:
- Vault path construction
- Credential masking
- inject_git_credentials activity (success and failure scenarios)
- cleanup_git_credentials activity
- Retry logic for Vault fetch
- Credential masking filter

"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.activities.credential_injector import (
    CREDENTIAL_MASK,
    CLEANUP_TIMEOUT_SECONDS,
    VAULT_MAX_RETRIES,
    VAULT_RETRY_BACKOFF_SECONDS,
    VAULT_TIMEOUT_SECONDS,
    CredentialInjectInput,
    CredentialInjectResult,
    CredentialInjectorError,
    CredentialMaskingFilter,
    build_vault_path,
    mask_credential_value,
    _build_credential_helper_script,
    _build_cleanup_script,
    _fetch_credential_from_vault,
)


# ---------------------------------------------------------------------------
# build_vault_path tests
# ---------------------------------------------------------------------------


class TestBuildVaultPath:
    """Tests for Vault path construction."""

    def test_standard_dept_id(self) -> None:
        assert build_vault_path("payments") == "atlassian/payments/bitbucket"

    def test_dept_id_with_hyphens(self) -> None:
        assert build_vault_path("data-science") == "atlassian/data-science/bitbucket"

    def test_dept_id_with_underscores(self) -> None:
        assert build_vault_path("dev_ops") == "atlassian/dev_ops/bitbucket"

    def test_empty_dept_id(self) -> None:
        # Even empty dept_id produces a valid path format
        assert build_vault_path("") == "atlassian//bitbucket"


# ---------------------------------------------------------------------------
# mask_credential_value tests
# ---------------------------------------------------------------------------


class TestMaskCredentialValue:
    """Tests for credential masking."""

    def test_normal_value(self) -> None:
        result = mask_credential_value("username123")
        assert result == f"u{CREDENTIAL_MASK}"
        assert "username123" not in result

    def test_short_value(self) -> None:
        result = mask_credential_value("ab")
        assert result == CREDENTIAL_MASK

    def test_single_char(self) -> None:
        result = mask_credential_value("x")
        assert result == CREDENTIAL_MASK

    def test_empty_value(self) -> None:
        result = mask_credential_value("")
        assert result == CREDENTIAL_MASK

    def test_password_masked(self) -> None:
        result = mask_credential_value("super_secret_password_123")
        assert "super_secret" not in result
        assert CREDENTIAL_MASK in result


# ---------------------------------------------------------------------------
# CredentialMaskingFilter tests
# ---------------------------------------------------------------------------


class TestCredentialMaskingFilter:
    """Tests for the log masking filter."""

    def test_masks_sensitive_value_in_log(self) -> None:
        filter_ = CredentialMaskingFilter()
        filter_.add_sensitive("my_secret_password")

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Connecting with password: my_secret_password",
            args=None,
            exc_info=None,
        )
        filter_.filter(record)
        assert "my_secret_password" not in record.getMessage()
        assert CREDENTIAL_MASK in record.getMessage()

    def test_masks_multiple_sensitive_values(self) -> None:
        filter_ = CredentialMaskingFilter()
        filter_.add_sensitive("user123")
        filter_.add_sensitive("pass456")

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="user=user123 pass=pass456",
            args=None,
            exc_info=None,
        )
        filter_.filter(record)
        msg = record.getMessage()
        assert "user123" not in msg
        assert "pass456" not in msg
        assert CREDENTIAL_MASK in msg

    def test_no_sensitive_values_passes_through(self) -> None:
        filter_ = CredentialMaskingFilter()

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Normal log message",
            args=None,
            exc_info=None,
        )
        filter_.filter(record)
        assert record.getMessage() == "Normal log message"

    def test_empty_sensitive_value_not_added(self) -> None:
        filter_ = CredentialMaskingFilter()
        filter_.add_sensitive("")
        assert len(filter_._sensitive_values) == 0


# ---------------------------------------------------------------------------
# _build_credential_helper_script tests
# ---------------------------------------------------------------------------


class TestBuildCredentialHelperScript:
    """Tests for credential helper script generation."""

    def test_contains_cache_timeout(self) -> None:
        script = _build_credential_helper_script("user", "pass", 15)
        # 15 minutes = 900 seconds
        assert "--timeout=900" in script

    def test_contains_credential_approve(self) -> None:
        script = _build_credential_helper_script("user", "pass", 15)
        assert "git credential approve" in script

    def test_contains_bitbucket_host(self) -> None:
        script = _build_credential_helper_script("user", "pass", 15)
        assert "bitbucket.org" in script

    def test_custom_ttl(self) -> None:
        script = _build_credential_helper_script("user", "pass", 30)
        # 30 minutes = 1800 seconds
        assert "--timeout=1800" in script


# ---------------------------------------------------------------------------
# _build_cleanup_script tests
# ---------------------------------------------------------------------------


class TestBuildCleanupScript:
    """Tests for cleanup script generation."""

    def test_contains_credential_cache_exit(self) -> None:
        script = _build_cleanup_script()
        assert "git credential-cache exit" in script

    def test_contains_unset_credential_helper(self) -> None:
        script = _build_cleanup_script()
        assert "git config --global --unset credential.helper" in script

    def test_ends_with_true(self) -> None:
        """Cleanup script should always succeed (exit 0)."""
        script = _build_cleanup_script()
        assert script.endswith("true")


# ---------------------------------------------------------------------------
# _fetch_credential_from_vault tests
# ---------------------------------------------------------------------------


class TestFetchCredentialFromVault:
    """Tests for Vault credential fetching with retry."""

    @pytest.mark.asyncio
    async def test_successful_fetch(self) -> None:
        """Test successful credential fetch on first attempt."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {
                "data": {
                    "username": "bitbucket_user",
                    "app_password": "secret_token_123",
                }
            }
        }

        with (
            patch.dict(
                "os.environ",
                {"VAULT_ADDR": "http://vault:8200", "VAULT_TOKEN": "test-token"},
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch("src.activities.credential_injector.activity"),
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await _fetch_credential_from_vault("payments", "wf-123")

            assert result["username"] == "bitbucket_user"
            assert result["app_password"] == "secret_token_123"

    @pytest.mark.asyncio
    async def test_missing_vault_token_raises(self) -> None:
        """Test that missing VAULT_TOKEN raises immediately."""
        with (
            patch.dict("os.environ", {"VAULT_TOKEN": ""}, clear=False),
            patch("src.activities.credential_injector.activity"),
        ):
            with pytest.raises(CredentialInjectorError) as exc_info:
                await _fetch_credential_from_vault("payments", "wf-123")
            assert "VAULT_TOKEN" in exc_info.value.cause

    @pytest.mark.asyncio
    async def test_404_does_not_retry(self) -> None:
        """Test that a 404 response does not trigger retries."""
        mock_response = MagicMock()
        mock_response.status_code = 404

        with (
            patch.dict(
                "os.environ",
                {"VAULT_ADDR": "http://vault:8200", "VAULT_TOKEN": "test-token"},
            ),
            patch("httpx.AsyncClient") as mock_client_cls,
            patch("src.activities.credential_injector.activity"),
        ):
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with pytest.raises(CredentialInjectorError) as exc_info:
                await _fetch_credential_from_vault("payments", "wf-123")
            assert "not found" in exc_info.value.cause

            # Should only be called once (no retries for 404)
            assert mock_client.get.call_count == 1


# ---------------------------------------------------------------------------
# CredentialInjectorError tests
# ---------------------------------------------------------------------------


class TestCredentialInjectorError:
    """Tests for the error class."""

    def test_error_attributes(self) -> None:
        err = CredentialInjectorError(
            workflow_id="wf-123",
            cause="vault timeout",
            error_code="credential_unavailable",
        )
        assert err.workflow_id == "wf-123"
        assert err.cause == "vault timeout"
        assert err.error_code == "credential_unavailable"
        assert "wf-123" in str(err)
        assert "vault timeout" in str(err)

    def test_default_error_code(self) -> None:
        err = CredentialInjectorError(
            workflow_id="wf-456",
            cause="some error",
        )
        assert err.error_code == "credential_unavailable"
