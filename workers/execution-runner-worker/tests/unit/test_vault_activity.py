"""Unit tests for the vault activity module.

Tests vault_fetch_ssh_credentials activity including:
- Successful credential fetch from Vault KV-v2
- Missing fields raise CredentialResolutionError
- Invalid port values raise CredentialResolutionError
- Vault 404 raises CredentialResolutionError
- Vault transport errors raise CredentialResolutionError
- Empty VAULT_TOKEN raises CredentialResolutionError
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from src.activities.vault import (
    CredentialResolutionError,
    SSHCred,
    _read_kv2_secret,
    vault_fetch_ssh_credentials,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _vault_kv2_response(data: dict) -> dict:
    """Wrap data in Vault KV-v2 response envelope."""
    return {
        "data": {
            "data": data,
            "metadata": {
                "created_time": "2024-01-01T00:00:00Z",
                "version": 1,
            },
        }
    }


def _mock_transport(status_code: int, json_body: dict | None = None) -> httpx.MockTransport:
    """Create a mock transport that returns a fixed response."""

    def handler(request: httpx.Request) -> httpx.Response:
        if json_body is not None:
            return httpx.Response(
                status_code=status_code,
                json=json_body,
            )
        return httpx.Response(status_code=status_code, text="")

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Tests: SSHCred dataclass
# ---------------------------------------------------------------------------


class TestSSHCred:
    def test_frozen_dataclass(self) -> None:
        cred = SSHCred(host="runner.internal", port=22, user="ai-runner", private_key="-----BEGIN RSA PRIVATE KEY-----\nfoo\n-----END RSA PRIVATE KEY-----")
        assert cred.host == "runner.internal"
        assert cred.port == 22
        assert cred.user == "ai-runner"
        assert "BEGIN RSA PRIVATE KEY" in cred.private_key

    def test_immutable(self) -> None:
        cred = SSHCred(host="h", port=22, user="u", private_key="k")
        with pytest.raises(AttributeError):
            cred.host = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests: CredentialResolutionError
# ---------------------------------------------------------------------------


class TestCredentialResolutionError:
    def test_inherits_runtime_error(self) -> None:
        err = CredentialResolutionError("wf-123", "vault 404")
        assert isinstance(err, RuntimeError)

    def test_attributes(self) -> None:
        err = CredentialResolutionError(
            "exec-wf-1", "missing fields", missing_fields=["host", "port"]
        )
        assert err.workflow_id == "exec-wf-1"
        assert err.cause == "missing fields"
        assert err.missing_fields == ["host", "port"]

    def test_str_contains_workflow_id(self) -> None:
        err = CredentialResolutionError("exec-wf-42", "timeout")
        msg = str(err)
        assert "exec-wf-42" in msg
        assert "timeout" in msg

    def test_default_missing_fields(self) -> None:
        err = CredentialResolutionError("wf", "cause")
        assert err.missing_fields == []


# ---------------------------------------------------------------------------
# Tests: _read_kv2_secret
# ---------------------------------------------------------------------------


class TestReadKv2Secret:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        secret = {"host": "runner.internal", "port": "22", "user": "ai-runner", "private_key": "key"}
        transport = _mock_transport(200, _vault_kv2_response(secret))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await _read_kv2_secret(
                client, "http://vault:8200", "token", "secret", "ssh/runner/current"
            )
        assert result == secret

    @pytest.mark.asyncio
    async def test_404_raises(self) -> None:
        transport = _mock_transport(404)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(CredentialResolutionError) as exc_info:
                await _read_kv2_secret(
                    client, "http://vault:8200", "token", "secret", "ssh/runner/current"
                )
        assert "not found" in exc_info.value.cause

    @pytest.mark.asyncio
    async def test_500_raises(self) -> None:
        transport = _mock_transport(500, {"errors": ["internal error"]})
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(CredentialResolutionError) as exc_info:
                await _read_kv2_secret(
                    client, "http://vault:8200", "token", "secret", "ssh/runner/current"
                )
        assert "500" in exc_info.value.cause

    @pytest.mark.asyncio
    async def test_malformed_json_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not json{{{")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(CredentialResolutionError) as exc_info:
                await _read_kv2_secret(
                    client, "http://vault:8200", "token", "secret", "ssh/runner/current"
                )
        assert "not valid JSON" in exc_info.value.cause

    @pytest.mark.asyncio
    async def test_missing_data_envelope_raises(self) -> None:
        transport = _mock_transport(200, {"no_data_key": {}})
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(CredentialResolutionError) as exc_info:
                await _read_kv2_secret(
                    client, "http://vault:8200", "token", "secret", "ssh/runner/current"
                )
        assert "envelope" in exc_info.value.cause

    @pytest.mark.asyncio
    async def test_missing_inner_data_raises(self) -> None:
        transport = _mock_transport(200, {"data": {"metadata": {}}})
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(CredentialResolutionError) as exc_info:
                await _read_kv2_secret(
                    client, "http://vault:8200", "token", "secret", "ssh/runner/current"
                )
        assert "data.data" in exc_info.value.cause


# ---------------------------------------------------------------------------
# Tests: vault_fetch_ssh_credentials (activity)
# ---------------------------------------------------------------------------


class TestVaultFetchSSHCredentials:
    """Tests for the Temporal activity function."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        secret = {
            "host": "runner.internal",
            "port": "22",
            "user": "ai-runner",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\ndata\n-----END RSA PRIVATE KEY-----",
        }
        transport = _mock_transport(200, _vault_kv2_response(secret))

        with (
            patch("src.activities.vault._vault_addr", return_value="http://vault:8200"),
            patch("src.activities.vault._vault_token", return_value="test-token"),
            patch("src.activities.vault._kv_mount", return_value="secret"),
            patch("src.activities.vault._ssh_secret_path", return_value="ssh/runner/current"),
            patch("src.activities.vault.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
            patch("temporalio.activity.logger"),
        ):
            result = await vault_fetch_ssh_credentials("exec-wf-123")

        assert isinstance(result, SSHCred)
        assert result.host == "runner.internal"
        assert result.port == 22
        assert result.user == "ai-runner"
        assert "BEGIN RSA PRIVATE KEY" in result.private_key

    @pytest.mark.asyncio
    async def test_missing_host_raises(self) -> None:
        secret = {
            "host": "",
            "port": "22",
            "user": "ai-runner",
            "private_key": "key",
        }
        transport = _mock_transport(200, _vault_kv2_response(secret))

        with (
            patch("src.activities.vault._vault_addr", return_value="http://vault:8200"),
            patch("src.activities.vault._vault_token", return_value="test-token"),
            patch("src.activities.vault._kv_mount", return_value="secret"),
            patch("src.activities.vault._ssh_secret_path", return_value="ssh/runner/current"),
            patch("src.activities.vault.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
            patch("temporalio.activity.logger"),
        ):
            with pytest.raises(CredentialResolutionError) as exc_info:
                await vault_fetch_ssh_credentials("exec-wf-123")

        assert "host" in exc_info.value.missing_fields
        assert exc_info.value.workflow_id == "exec-wf-123"

    @pytest.mark.asyncio
    async def test_missing_private_key_raises(self) -> None:
        secret = {
            "host": "runner.internal",
            "port": "22",
            "user": "ai-runner",
            # private_key missing entirely
        }
        transport = _mock_transport(200, _vault_kv2_response(secret))

        with (
            patch("src.activities.vault._vault_addr", return_value="http://vault:8200"),
            patch("src.activities.vault._vault_token", return_value="test-token"),
            patch("src.activities.vault._kv_mount", return_value="secret"),
            patch("src.activities.vault._ssh_secret_path", return_value="ssh/runner/current"),
            patch("src.activities.vault.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
            patch("temporalio.activity.logger"),
        ):
            with pytest.raises(CredentialResolutionError) as exc_info:
                await vault_fetch_ssh_credentials("exec-wf-123")

        assert "private_key" in exc_info.value.missing_fields

    @pytest.mark.asyncio
    async def test_invalid_port_raises(self) -> None:
        secret = {
            "host": "runner.internal",
            "port": "not-a-number",
            "user": "ai-runner",
            "private_key": "key",
        }
        transport = _mock_transport(200, _vault_kv2_response(secret))

        with (
            patch("src.activities.vault._vault_addr", return_value="http://vault:8200"),
            patch("src.activities.vault._vault_token", return_value="test-token"),
            patch("src.activities.vault._kv_mount", return_value="secret"),
            patch("src.activities.vault._ssh_secret_path", return_value="ssh/runner/current"),
            patch("src.activities.vault.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
            patch("temporalio.activity.logger"),
        ):
            with pytest.raises(CredentialResolutionError) as exc_info:
                await vault_fetch_ssh_credentials("exec-wf-123")

        assert "invalid port" in exc_info.value.cause

    @pytest.mark.asyncio
    async def test_port_out_of_range_raises(self) -> None:
        secret = {
            "host": "runner.internal",
            "port": "99999",
            "user": "ai-runner",
            "private_key": "key",
        }
        transport = _mock_transport(200, _vault_kv2_response(secret))

        with (
            patch("src.activities.vault._vault_addr", return_value="http://vault:8200"),
            patch("src.activities.vault._vault_token", return_value="test-token"),
            patch("src.activities.vault._kv_mount", return_value="secret"),
            patch("src.activities.vault._ssh_secret_path", return_value="ssh/runner/current"),
            patch("src.activities.vault.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
            patch("temporalio.activity.logger"),
        ):
            with pytest.raises(CredentialResolutionError) as exc_info:
                await vault_fetch_ssh_credentials("exec-wf-123")

        assert "out of valid range" in exc_info.value.cause

    @pytest.mark.asyncio
    async def test_empty_vault_token_raises(self) -> None:
        with (
            patch("src.activities.vault._vault_addr", return_value="http://vault:8200"),
            patch("src.activities.vault._vault_token", return_value=""),
            patch("temporalio.activity.logger"),
        ):
            with pytest.raises(CredentialResolutionError) as exc_info:
                await vault_fetch_ssh_credentials("exec-wf-123")

        assert "VAULT_TOKEN" in exc_info.value.cause
        assert exc_info.value.workflow_id == "exec-wf-123"

    @pytest.mark.asyncio
    async def test_vault_404_raises(self) -> None:
        transport = _mock_transport(404)

        with (
            patch("src.activities.vault._vault_addr", return_value="http://vault:8200"),
            patch("src.activities.vault._vault_token", return_value="test-token"),
            patch("src.activities.vault._kv_mount", return_value="secret"),
            patch("src.activities.vault._ssh_secret_path", return_value="ssh/runner/current"),
            patch("src.activities.vault.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
            patch("temporalio.activity.logger"),
        ):
            with pytest.raises(CredentialResolutionError) as exc_info:
                await vault_fetch_ssh_credentials("exec-wf-123")

        assert "not found" in exc_info.value.cause

    @pytest.mark.asyncio
    async def test_integer_port_value(self) -> None:
        """Port can be stored as integer in Vault (JSON number)."""
        secret = {
            "host": "runner.internal",
            "port": 2222,
            "user": "ai-runner",
            "private_key": "key-data",
        }
        transport = _mock_transport(200, _vault_kv2_response(secret))

        with (
            patch("src.activities.vault._vault_addr", return_value="http://vault:8200"),
            patch("src.activities.vault._vault_token", return_value="test-token"),
            patch("src.activities.vault._kv_mount", return_value="secret"),
            patch("src.activities.vault._ssh_secret_path", return_value="ssh/runner/current"),
            patch("src.activities.vault.httpx.AsyncClient", return_value=httpx.AsyncClient(transport=transport)),
            patch("temporalio.activity.logger"),
        ):
            result = await vault_fetch_ssh_credentials("exec-wf-123")

        assert result.port == 2222
