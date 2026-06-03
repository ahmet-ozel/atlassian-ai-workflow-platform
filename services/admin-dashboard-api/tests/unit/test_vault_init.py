"""Unit tests for the ``POST /admin/vault/init`` endpoint.

These tests cover the main Vault init outcomes:

* Execute vault operator init with 5 key shares and 3 threshold.
* Write root token to Vault's own secret engine after init.
* Return HTTP 409 if Vault is already initialized.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

# Bootstrap sys.path so ``import src.*`` resolves under direct
# ``pytest tests/unit`` invocations from the service root.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_http_client():
    """Create a mock httpx.AsyncClient."""
    client = AsyncMock(spec=httpx.AsyncClient)
    return client


@pytest.fixture
def client(mock_http_client):
    """Create a test client with mocked app state."""
    from src.main import app

    with TestClient(app, raise_server_exceptions=False) as c:
        app.state.http_client = mock_http_client
        yield c


# ---------------------------------------------------------------------------
# POST /admin/vault/init endpoint tests
# ---------------------------------------------------------------------------


class TestVaultInitEndpoint:
    """Test the POST /admin/vault/init endpoint."""

    def test_vault_already_initialized_returns_409(
        self, client, mock_http_client
    ) -> None:
        """Already initialized → 409 Conflict."""
        # Mock the /v1/sys/init GET check — Vault reports initialized
        init_check_response = MagicMock()
        init_check_response.status_code = 200
        init_check_response.json.return_value = {"initialized": True}

        mock_http_client.get = AsyncMock(return_value=init_check_response)

        response = client.post("/admin/vault/init")

        assert response.status_code == 409
        body = response.json()
        assert body["detail"]["error"] == "vault_already_initialized"

    def test_successful_init_returns_keys_and_token(
        self, client, mock_http_client
    ) -> None:
        """Successful init returns unseal keys and root token."""
        # Mock the /v1/sys/init GET check — Vault is NOT initialized
        init_check_response = MagicMock()
        init_check_response.status_code = 200
        init_check_response.json.return_value = {"initialized": False}

        # Mock the /v1/sys/init PUT — successful init
        init_response = MagicMock()
        init_response.status_code = 200
        init_response.json.return_value = {
            "keys": [
                "key-share-1-hex",
                "key-share-2-hex",
                "key-share-3-hex",
                "key-share-4-hex",
                "key-share-5-hex",
            ],
            "keys_base64": [
                "key-share-1-b64",
                "key-share-2-b64",
                "key-share-3-b64",
                "key-share-4-b64",
                "key-share-5-b64",
            ],
            "root_token": "s.root-token-value",
        }

        # Mock the root token write to secret engine
        write_response = MagicMock()
        write_response.status_code = 200

        mock_http_client.get = AsyncMock(return_value=init_check_response)
        mock_http_client.put = AsyncMock(return_value=init_response)
        mock_http_client.post = AsyncMock(return_value=write_response)

        response = client.post("/admin/vault/init")

        assert response.status_code == 200
        body = response.json()
        assert body["message"] == "vault_initialized"
        assert len(body["unseal_keys"]) == 5
        assert len(body["unseal_keys_base64"]) == 5
        assert body["root_token"] == "s.root-token-value"

    def test_init_with_custom_shares_and_threshold(
        self, client, mock_http_client
    ) -> None:
        """Custom key shares and threshold are accepted."""
        # Mock the /v1/sys/init GET check — Vault is NOT initialized
        init_check_response = MagicMock()
        init_check_response.status_code = 200
        init_check_response.json.return_value = {"initialized": False}

        # Mock the /v1/sys/init PUT — successful init with 3 shares
        init_response = MagicMock()
        init_response.status_code = 200
        init_response.json.return_value = {
            "keys": ["key-1", "key-2", "key-3"],
            "keys_base64": ["key-1-b64", "key-2-b64", "key-3-b64"],
            "root_token": "s.custom-root-token",
        }

        # Mock the root token write
        write_response = MagicMock()
        write_response.status_code = 200

        mock_http_client.get = AsyncMock(return_value=init_check_response)
        mock_http_client.put = AsyncMock(return_value=init_response)
        mock_http_client.post = AsyncMock(return_value=write_response)

        response = client.post(
            "/admin/vault/init",
            json={"secret_shares": 3, "secret_threshold": 2},
        )

        assert response.status_code == 200
        body = response.json()
        assert len(body["unseal_keys"]) == 3
        assert body["root_token"] == "s.custom-root-token"

        # Verify the PUT was called with correct parameters
        put_call = mock_http_client.put.call_args
        assert put_call is not None
        call_json = put_call.kwargs.get("json") or put_call[1].get("json")
        assert call_json["secret_shares"] == 3
        assert call_json["secret_threshold"] == 2

    def test_threshold_greater_than_shares_returns_400(
        self, client, mock_http_client
    ) -> None:
        """Threshold > shares is invalid → 400."""
        response = client.post(
            "/admin/vault/init",
            json={"secret_shares": 3, "secret_threshold": 5},
        )

        assert response.status_code == 400
        body = response.json()
        assert body["detail"]["error"] == "invalid_parameters"

    def test_vault_communication_error_returns_502(
        self, client, mock_http_client
    ) -> None:
        """Vault unreachable → 502 Bad Gateway."""
        mock_http_client.get = AsyncMock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        response = client.post("/admin/vault/init")

        assert response.status_code == 502
        body = response.json()
        assert body["detail"]["error"] == "vault_communication_error"

    def test_vault_init_failure_returns_502(
        self, client, mock_http_client
    ) -> None:
        """Vault init PUT returns non-200 → 502."""
        # Mock the /v1/sys/init GET check — Vault is NOT initialized
        init_check_response = MagicMock()
        init_check_response.status_code = 200
        init_check_response.json.return_value = {"initialized": False}

        # Mock the /v1/sys/init PUT — failure
        init_response = MagicMock()
        init_response.status_code = 500
        init_response.text = "Internal Server Error"

        mock_http_client.get = AsyncMock(return_value=init_check_response)
        mock_http_client.put = AsyncMock(return_value=init_response)

        response = client.post("/admin/vault/init")

        assert response.status_code == 502
        body = response.json()
        assert body["detail"]["error"] == "vault_init_failed"

    def test_root_token_write_failure_is_non_fatal(
        self, client, mock_http_client
    ) -> None:
        """Root token write failure is non-fatal — keys still returned."""
        # Mock the /v1/sys/init GET check — Vault is NOT initialized
        init_check_response = MagicMock()
        init_check_response.status_code = 200
        init_check_response.json.return_value = {"initialized": False}

        # Mock the /v1/sys/init PUT — successful init
        init_response = MagicMock()
        init_response.status_code = 200
        init_response.json.return_value = {
            "keys": ["k1", "k2", "k3", "k4", "k5"],
            "keys_base64": ["k1b", "k2b", "k3b", "k4b", "k5b"],
            "root_token": "s.my-root-token",
        }

        # Mock the root token write — fails
        mock_http_client.get = AsyncMock(return_value=init_check_response)
        mock_http_client.put = AsyncMock(return_value=init_response)
        mock_http_client.post = AsyncMock(
            side_effect=httpx.ConnectError("Vault write failed")
        )

        response = client.post("/admin/vault/init")

        # Should still succeed — the write-back is best-effort
        assert response.status_code == 200
        body = response.json()
        assert body["root_token"] == "s.my-root-token"
        assert len(body["unseal_keys"]) == 5

    def test_no_http_client_returns_503(
        self, client, mock_http_client
    ) -> None:
        """When http_client is None → 503 Service Unavailable."""
        from src.main import app

        # Temporarily set http_client to None to simulate unavailability
        app.state.http_client = None

        try:
            response = client.post("/admin/vault/init")
            assert response.status_code == 503
            body = response.json()
            assert body["detail"]["reason"] == "http_client_unavailable"
        finally:
            # Restore the mock client for other tests
            app.state.http_client = mock_http_client

    def test_default_shares_and_threshold(
        self, client, mock_http_client
    ) -> None:
        """Default request uses 5 shares and 3 threshold."""
        # Mock the /v1/sys/init GET check — Vault is NOT initialized
        init_check_response = MagicMock()
        init_check_response.status_code = 200
        init_check_response.json.return_value = {"initialized": False}

        # Mock the /v1/sys/init PUT — successful init
        init_response = MagicMock()
        init_response.status_code = 200
        init_response.json.return_value = {
            "keys": ["k1", "k2", "k3", "k4", "k5"],
            "keys_base64": ["k1b", "k2b", "k3b", "k4b", "k5b"],
            "root_token": "s.token",
        }

        # Mock the root token write
        write_response = MagicMock()
        write_response.status_code = 200

        mock_http_client.get = AsyncMock(return_value=init_check_response)
        mock_http_client.put = AsyncMock(return_value=init_response)
        mock_http_client.post = AsyncMock(return_value=write_response)

        response = client.post("/admin/vault/init")

        assert response.status_code == 200

        # Verify the PUT was called with default 5/3 parameters
        put_call = mock_http_client.put.call_args
        assert put_call is not None
        call_json = put_call.kwargs.get("json") or put_call[1].get("json")
        assert call_json["secret_shares"] == 5
        assert call_json["secret_threshold"] == 3
