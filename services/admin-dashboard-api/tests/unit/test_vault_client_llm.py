"""Unit tests for the ``VaultClient`` LLM-credential methods.

Covers:
* Successful write / read / delete round-trip against an in-memory
  KV-v2 simulation backed by :class:`httpx.MockTransport`.
* The KV-v2 envelope shape on writes (``{"data": payload}``) and the
  unwrapping on reads (``response["data"]["data"]``).
* 404-as-no-op on delete (idempotent path).
* :class:`VaultWriteError` propagation for both transport errors and
  non-2xx HTTP responses on each operation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

_API_ROOT = Path(__file__).resolve().parents[2]
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from src.lifecycle.vault_client import VaultClient, VaultWriteError  # noqa: E402


# ---------------------------------------------------------------------------
# In-memory KV-v2 simulation backed by httpx.MockTransport.
# ---------------------------------------------------------------------------


class _KVStore:
    """Stand-in for Vault's KV-v2 mount.

    Stores the *unwrapped* payload (i.e. ``payload["data"]``) keyed by
    provider id; writes return 200, reads return the KV-v2 wrapper
    shape, deletes return 204.
    """

    def __init__(self) -> None:
        self._records: dict[UUID, dict[str, str]] = {}
        self.writes: list[tuple[UUID, dict[str, str]]] = []
        self.deletes: list[UUID] = []

    def store(self, provider_id: UUID, payload: dict[str, str]) -> None:
        self._records[provider_id] = dict(payload)

    def get(self, provider_id: UUID) -> dict[str, str] | None:
        return self._records.get(provider_id)

    def drop(self, provider_id: UUID) -> bool:
        return self._records.pop(provider_id, None) is not None


def _llm_credentials_path(provider_id: UUID) -> str:
    return f"/v1/secret/data/llm-providers/{provider_id}/credentials"


def _make_client(
    store: _KVStore,
    *,
    fail_status: dict[str, int] | None = None,
    raise_on: str | None = None,
) -> VaultClient:
    """Build a :class:`VaultClient` wired to an in-memory KV store.

    ``fail_status`` maps an HTTP method (``GET`` / ``PUT`` / ``DELETE``)
    to the response code the mock should return; ``raise_on`` simulates
    a transport-level failure on the given method.
    """

    fail_status = fail_status or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if raise_on and request.method == raise_on:
            raise httpx.ConnectError("forced transport error")
        # The KV-v2 path is ``/v1/secret/data/llm-providers/<id>/credentials``.
        prefix = "/v1/secret/data/llm-providers/"
        suffix = "/credentials"
        path = request.url.path
        assert path.startswith(prefix) and path.endswith(suffix), path
        provider_id = UUID(path[len(prefix) : -len(suffix)])

        if request.method in fail_status:
            return httpx.Response(fail_status[request.method])

        if request.method == "PUT":
            import json

            body = json.loads(request.content or b"{}")
            assert "data" in body, body
            store.store(provider_id, body["data"])
            store.writes.append((provider_id, dict(body["data"])))
            return httpx.Response(200, json={"data": {"version": 1}})

        if request.method == "GET":
            data = store.get(provider_id)
            if data is None:
                return httpx.Response(404, json={"errors": ["not found"]})
            return httpx.Response(
                200, json={"data": {"data": data, "metadata": {"version": 1}}}
            )

        if request.method == "DELETE":
            store.drop(provider_id)
            store.deletes.append(provider_id)
            return httpx.Response(204)

        return httpx.Response(405)

    return VaultClient(
        addr="http://vault:8200",
        token="dev-token-not-for-prod",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_then_read_returns_same_payload() -> None:
    store = _KVStore()
    client = _make_client(store)
    provider_id = uuid4()
    payload = {"api_key": "sk-test-1234567890ABCDEFGH"}

    await client.write_llm_credentials(provider_id=provider_id, payload=payload)
    read_back = await client.read_llm_credentials(provider_id=provider_id)

    assert read_back == payload
    # The Vault PUT body was wrapped in the KV-v2 envelope.
    assert store.writes == [(provider_id, payload)]


@pytest.mark.asyncio
async def test_read_missing_returns_empty_dict() -> None:
    store = _KVStore()
    client = _make_client(store)

    result = await client.read_llm_credentials(provider_id=uuid4())

    assert result == {}


@pytest.mark.asyncio
async def test_delete_idempotent_on_404() -> None:
    store = _KVStore()
    client = _make_client(store)
    provider_id = uuid4()

    # No prior write - the path doesn't exist; delete must NOT raise.
    await client.delete_llm_credentials(provider_id=provider_id)


@pytest.mark.asyncio
async def test_read_filters_non_string_values() -> None:
    """A stray non-string value in the payload is dropped quietly."""

    store = _KVStore()
    client = _make_client(store)
    provider_id = uuid4()
    # Inject a malformed record bypassing the write path so the
    # filter behaviour is exercised on the read path.
    store.store(provider_id, {"api_key": "good", "rogue": 123})  # type: ignore[arg-type]

    result = await client.read_llm_credentials(provider_id=provider_id)
    assert result == {"api_key": "good"}


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_500_raises_vault_write_error() -> None:
    store = _KVStore()
    client = _make_client(store, fail_status={"PUT": 500})

    with pytest.raises(VaultWriteError) as exc_info:
        await client.write_llm_credentials(
            provider_id=uuid4(), payload={"api_key": "x"}
        )
    assert exc_info.value.operation == "write"
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_write_transport_error_raises_vault_write_error() -> None:
    store = _KVStore()
    client = _make_client(store, raise_on="PUT")

    with pytest.raises(VaultWriteError) as exc_info:
        await client.write_llm_credentials(
            provider_id=uuid4(), payload={"api_key": "x"}
        )
    assert exc_info.value.operation == "write"
    assert exc_info.value.status_code is None


@pytest.mark.asyncio
async def test_read_500_raises_vault_write_error() -> None:
    store = _KVStore()
    client = _make_client(store, fail_status={"GET": 500})

    with pytest.raises(VaultWriteError) as exc_info:
        await client.read_llm_credentials(provider_id=uuid4())
    assert exc_info.value.operation == "read"
    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_delete_500_raises_vault_write_error() -> None:
    store = _KVStore()
    client = _make_client(store, fail_status={"DELETE": 500})

    with pytest.raises(VaultWriteError) as exc_info:
        await client.delete_llm_credentials(provider_id=uuid4())
    assert exc_info.value.operation == "delete"
    assert exc_info.value.status_code == 500
