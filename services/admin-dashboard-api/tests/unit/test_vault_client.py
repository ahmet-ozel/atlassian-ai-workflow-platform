"""Unit tests for ``src.lifecycle.vault_client.VaultClient`` (task 5.1).

These tests exercise the Vault KV-v2 wrapper against an in-process
``httpx.MockTransport`` — no real Vault, no network, no disk I/O. They
validate the contract from design §3.5 and Requirements 9.1, 9.2, 9.5,
9.6.

Test layout:

* Construction guards (empty addr / token / mount).
* ``write_env_override``: URL, body, header on success; ``5xx`` and
  ``4xx`` raise :class:`VaultWriteError`; transport errors raise.
* ``read_env_overrides``: empty-prefix ``404`` on LIST → ``{}``;
  successful LIST + GET assemble the dict; soft-deleted keys
  (``404`` on per-key GET) are skipped; ``5xx`` on LIST or per-key
  GET raises.
* ``delete_env_override``: ``204`` and idempotent ``404`` succeed;
  ``5xx`` raises.
* Property P2 sentinel: a sequence of writes never opens or creates
  any file under the workspace tree (smoke check that the wrapper
  doesn't accidentally touch disk).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

# Make ``import src.lifecycle.vault_client`` work when pytest is invoked
# directly under ``services/admin-dashboard-api/``.
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.vault_client import VaultClient, VaultWriteError  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


VAULT_ADDR = "http://vault.test:8200"
VAULT_TOKEN = "dev-token-not-for-prod"


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    kv_mount: str = "secret",
) -> VaultClient:
    """Build a :class:`VaultClient` whose HTTP traffic is intercepted by
    ``handler``. The mock transport stays in-process; nothing escapes
    to the network.
    """

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return VaultClient(
        addr=VAULT_ADDR,
        token=VAULT_TOKEN,
        kv_mount=kv_mount,
        client=http_client,
    )


# ---------------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------------


def test_constructor_rejects_empty_addr() -> None:
    with pytest.raises(ValueError):
        VaultClient(addr="", token=VAULT_TOKEN)


def test_constructor_rejects_empty_token() -> None:
    with pytest.raises(ValueError):
        VaultClient(addr=VAULT_ADDR, token="")


def test_constructor_rejects_empty_kv_mount() -> None:
    with pytest.raises(ValueError):
        VaultClient(addr=VAULT_ADDR, token=VAULT_TOKEN, kv_mount="")


# ---------------------------------------------------------------------------
# write_env_override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_env_override_uses_kv_v2_data_url_and_body() -> None:
    """Requirement 9.1 + 9.6: PUT to the KV-v2 ``data/services/...`` path
    with body ``{"data": {"value": value}}`` and the ``X-Vault-Token``
    header set."""

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"request_id": "req-1"})

    client = _make_client(handler)
    try:
        await client.write_env_override(
            service_name="automation-service",
            key="OPENAI_API_KEY",
            value="sk-xxx",
        )
    finally:
        await client.aclose()

    assert captured["method"] == "PUT"
    assert (
        captured["url"]
        == f"{VAULT_ADDR}/v1/secret/data/services/automation-service/OPENAI_API_KEY"
    )
    assert captured["headers"]["x-vault-token"] == VAULT_TOKEN
    assert captured["body"] == {"data": {"value": "sk-xxx"}}


@pytest.mark.asyncio
async def test_write_env_override_respects_custom_kv_mount() -> None:
    """Tasks specify a configurable ``kv_mount`` (default ``secret``)."""

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(204)

    client = _make_client(handler, kv_mount="kv")
    try:
        await client.write_env_override(
            service_name="svc",
            key="K",
            value="v",
        )
    finally:
        await client.aclose()

    assert captured["url"] == f"{VAULT_ADDR}/v1/kv/data/services/svc/K"


@pytest.mark.asyncio
async def test_write_env_override_url_encodes_segments() -> None:
    """A key containing a ``/`` cannot escape the configured prefix."""

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200)

    client = _make_client(handler)
    try:
        await client.write_env_override(
            service_name="svc",
            key="WEIRD/KEY",
            value="v",
        )
    finally:
        await client.aclose()

    # ``/`` is percent-encoded as ``%2F``; the resulting URL is one
    # segment past ``services/svc/`` rather than two.
    assert captured["url"].endswith("/services/svc/WEIRD%2FKEY")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_write_env_override_raises_on_5xx(status: int) -> None:
    """Requirement 9.5: any 5xx is fatal."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="boom")

    client = _make_client(handler)
    try:
        with pytest.raises(VaultWriteError) as excinfo:
            await client.write_env_override(
                service_name="svc",
                key="K",
                value="v",
            )
    finally:
        await client.aclose()

    assert excinfo.value.status_code == status
    assert excinfo.value.operation == "write"
    assert excinfo.value.service_name == "svc"
    assert excinfo.value.key == "K"


@pytest.mark.asyncio
async def test_write_env_override_raises_on_404() -> None:
    """Requirement 9.5 explicitly calls out 404 alongside 5xx."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": ["not found"]})

    client = _make_client(handler)
    try:
        with pytest.raises(VaultWriteError) as excinfo:
            await client.write_env_override(
                service_name="svc",
                key="K",
                value="v",
            )
    finally:
        await client.aclose()

    assert excinfo.value.status_code == 404


@pytest.mark.asyncio
async def test_write_env_override_does_not_leak_value_in_exception() -> None:
    """The exception message must never include the secret value."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal")

    client = _make_client(handler)
    secret_value = "super-sensitive-do-not-leak"
    try:
        with pytest.raises(VaultWriteError) as excinfo:
            await client.write_env_override(
                service_name="svc",
                key="API_KEY",
                value=secret_value,
            )
    finally:
        await client.aclose()

    assert secret_value not in str(excinfo.value)
    # The body comes from Vault, not our code; safe to surface ``"internal"``.
    assert "500" in str(excinfo.value)


@pytest.mark.asyncio
async def test_write_env_override_raises_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("vault is unreachable")

    client = _make_client(handler)
    try:
        with pytest.raises(VaultWriteError) as excinfo:
            await client.write_env_override(
                service_name="svc",
                key="K",
                value="v",
            )
    finally:
        await client.aclose()

    assert excinfo.value.status_code is None
    assert excinfo.value.operation == "write"


# ---------------------------------------------------------------------------
# read_env_overrides
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_env_overrides_returns_empty_dict_when_prefix_missing() -> None:
    """Design §3.5: ``404`` on LIST means first-time start → ``{}``."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Only the LIST should be hit when the prefix is empty.
        assert "metadata" in str(request.url)
        return httpx.Response(404, json={"errors": []})

    client = _make_client(handler)
    try:
        result = await client.read_env_overrides(service_name="svc")
    finally:
        await client.aclose()

    assert result == {}


@pytest.mark.asyncio
async def test_read_env_overrides_assembles_dict_via_list_plus_get() -> None:
    """Successful LIST + per-key GET yields the full ``{key: value}`` map."""

    list_url = f"{VAULT_ADDR}/v1/secret/metadata/services/svc/"
    data_prefix = f"{VAULT_ADDR}/v1/secret/data/services/svc/"
    secrets = {"OPENAI_API_KEY": "sk-1", "DB_PASSWORD": "p@ss"}

    requests_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests_seen.append(f"{request.method} {url}")
        if url.startswith(list_url) and request.method == "GET":
            return httpx.Response(200, json={"data": {"keys": list(secrets.keys())}})
        for key, value in secrets.items():
            if url == data_prefix + key and request.method == "GET":
                return httpx.Response(
                    200,
                    json={"data": {"data": {"value": value}, "metadata": {"version": 1}}},
                )
        return httpx.Response(599, text=f"unexpected request: {url}")

    client = _make_client(handler)
    try:
        result = await client.read_env_overrides(service_name="svc")
    finally:
        await client.aclose()

    assert result == secrets
    # Sanity: we hit LIST plus one GET per key.
    assert len(requests_seen) == 1 + len(secrets)


@pytest.mark.asyncio
async def test_read_env_overrides_skips_keys_with_404_on_get() -> None:
    """A key surfaced by LIST but soft-deleted by the time we GET it
    should be silently skipped, not raise."""

    list_url = f"{VAULT_ADDR}/v1/secret/metadata/services/svc/"
    data_prefix = f"{VAULT_ADDR}/v1/secret/data/services/svc/"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(list_url):
            return httpx.Response(
                200, json={"data": {"keys": ["LIVE_KEY", "GHOST_KEY"]}}
            )
        if url == data_prefix + "LIVE_KEY":
            return httpx.Response(200, json={"data": {"data": {"value": "alive"}}})
        if url == data_prefix + "GHOST_KEY":
            return httpx.Response(404, json={"errors": []})
        return httpx.Response(599)

    client = _make_client(handler)
    try:
        result = await client.read_env_overrides(service_name="svc")
    finally:
        await client.aclose()

    assert result == {"LIVE_KEY": "alive"}


@pytest.mark.asyncio
async def test_read_env_overrides_filters_directory_entries_from_list() -> None:
    """KV-v2 LIST may include trailing-slash directory entries; the
    wrapper drops them so a GET on a directory doesn't crash."""

    list_url = f"{VAULT_ADDR}/v1/secret/metadata/services/svc/"
    data_prefix = f"{VAULT_ADDR}/v1/secret/data/services/svc/"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(list_url):
            return httpx.Response(
                200, json={"data": {"keys": ["nested/", "REAL_KEY"]}}
            )
        if url == data_prefix + "REAL_KEY":
            return httpx.Response(200, json={"data": {"data": {"value": "ok"}}})
        return httpx.Response(599, text=f"unexpected: {url}")

    client = _make_client(handler)
    try:
        result = await client.read_env_overrides(service_name="svc")
    finally:
        await client.aclose()

    assert result == {"REAL_KEY": "ok"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503])
async def test_read_env_overrides_raises_on_list_5xx(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="bad")

    client = _make_client(handler)
    try:
        with pytest.raises(VaultWriteError) as excinfo:
            await client.read_env_overrides(service_name="svc")
    finally:
        await client.aclose()

    assert excinfo.value.operation == "list"
    assert excinfo.value.status_code == status


@pytest.mark.asyncio
async def test_read_env_overrides_raises_on_per_key_get_5xx() -> None:
    list_url = f"{VAULT_ADDR}/v1/secret/metadata/services/svc/"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(list_url):
            return httpx.Response(200, json={"data": {"keys": ["K"]}})
        return httpx.Response(503, text="kv down")

    client = _make_client(handler)
    try:
        with pytest.raises(VaultWriteError) as excinfo:
            await client.read_env_overrides(service_name="svc")
    finally:
        await client.aclose()

    assert excinfo.value.operation == "read"
    assert excinfo.value.status_code == 503
    assert excinfo.value.key == "K"


# ---------------------------------------------------------------------------
# delete_env_override
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_env_override_sends_delete_to_data_url() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(204)

    client = _make_client(handler)
    try:
        await client.delete_env_override(service_name="svc", key="K")
    finally:
        await client.aclose()

    assert captured["method"] == "DELETE"
    assert captured["url"] == f"{VAULT_ADDR}/v1/secret/data/services/svc/K"
    assert captured["headers"]["x-vault-token"] == VAULT_TOKEN


@pytest.mark.asyncio
async def test_delete_env_override_treats_404_as_idempotent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": []})

    client = _make_client(handler)
    try:
        # Should NOT raise — deleting a missing key is a no-op so callers
        # can use this method for cleanup paths without pre-checking.
        await client.delete_env_override(service_name="svc", key="missing")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_delete_env_override_raises_on_5xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = _make_client(handler)
    try:
        with pytest.raises(VaultWriteError) as excinfo:
            await client.delete_env_override(service_name="svc", key="K")
    finally:
        await client.aclose()

    assert excinfo.value.operation == "delete"
    assert excinfo.value.status_code == 500


# ---------------------------------------------------------------------------
# list_env_override_keys (platform-mimari-uyumluluk R14 / Q16 task 15.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_env_override_keys_returns_keys_from_metadata_list() -> None:
    """Successful LIST returns the bare key list (no prefix, no slashes)."""

    list_url = f"{VAULT_ADDR}/v1/secret/metadata/services/svc/"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(list_url)
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={"data": {"keys": ["PORT", "API_TOKEN", "DB_URL"]}},
        )

    client = _make_client(handler)
    try:
        keys = await client.list_env_override_keys(service_name="svc")
    finally:
        await client.aclose()

    # Order is preserved as Vault returned it; the lifecycle service
    # iterates this list to issue per-key deletes.
    assert keys == ["PORT", "API_TOKEN", "DB_URL"]


@pytest.mark.asyncio
async def test_list_env_override_keys_returns_empty_on_404() -> None:
    """A 404 on LIST means the prefix has never been written — the
    lifecycle service treats this as "nothing to purge"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": []})

    client = _make_client(handler)
    try:
        keys = await client.list_env_override_keys(service_name="svc")
    finally:
        await client.aclose()

    assert keys == []


@pytest.mark.asyncio
async def test_list_env_override_keys_filters_directory_entries() -> None:
    """Vault's LIST may include trailing-slash "directory" entries
    when nested folders exist; the wrapper filters them so callers
    only see leaf keys."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"keys": ["PORT", "nested/", "API_TOKEN"]}},
        )

    client = _make_client(handler)
    try:
        keys = await client.list_env_override_keys(service_name="svc")
    finally:
        await client.aclose()

    assert keys == ["PORT", "API_TOKEN"]


@pytest.mark.asyncio
async def test_list_env_override_keys_raises_on_5xx() -> None:
    """5xx errors surface :class:`VaultWriteError` so the caller can
    decide whether to treat the failure as fatal or best-effort."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _make_client(handler)
    try:
        with pytest.raises(VaultWriteError) as excinfo:
            await client.list_env_override_keys(service_name="svc")
    finally:
        await client.aclose()

    assert excinfo.value.operation == "list"
    assert excinfo.value.status_code == 503


# ---------------------------------------------------------------------------
# Property P2 — VaultClient itself never touches disk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vault_client_does_not_open_disk_files_during_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct sentinel for Requirement 9.2 / Property P2 surface area
    inside the wrapper itself: monkey-patch ``builtins.open`` to crash
    on any disk access for the duration of a multi-key write
    sequence."""

    import builtins

    real_open = builtins.open

    def explode(*args: Any, **kwargs: Any):
        # Only fail on *real* file paths — pytest internals occasionally
        # touch ``/dev/null`` style helpers; restrict to non-special.
        path = args[0] if args else kwargs.get("file")
        raise AssertionError(
            f"VaultClient must not perform disk I/O (Property P2); "
            f"open({path!r}) was called"
        )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"request_id": "ok"})

    client = _make_client(handler)
    monkeypatch.setattr(builtins, "open", explode)
    try:
        for key, value in {
            "OPENAI_API_KEY": "sk-x",
            "DB_PASSWORD": "p",
            "VAULT_TOKEN": "t",
        }.items():
            await client.write_env_override(
                service_name="svc",
                key=key,
                value=value,
            )
    finally:
        # Restore ``open`` *before* aclose so httpx can shut down its
        # transport without tripping the sentinel.
        monkeypatch.setattr(builtins, "open", real_open)
        await client.aclose()


# ---------------------------------------------------------------------------
# Async lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_context_manager_closes_owned_client() -> None:
    """When the wrapper builds its own client, ``__aexit__`` closes it."""

    async with VaultClient(addr=VAULT_ADDR, token=VAULT_TOKEN) as client:
        assert client is not None
    # If the underlying client wasn't closed, pytest-asyncio would
    # surface a ``ResourceWarning``; we rely on that signal.


@pytest.mark.asyncio
async def test_aclose_does_not_close_injected_client() -> None:
    """When the caller injects a client, the wrapper must not close it
    out from under them."""

    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    injected = httpx.AsyncClient(transport=transport)
    client = VaultClient(
        addr=VAULT_ADDR,
        token=VAULT_TOKEN,
        client=injected,
    )
    await client.aclose()
    # ``injected`` should still be usable.
    response = await injected.get(f"{VAULT_ADDR}/v1/secret/data/services/x/y")
    assert response.status_code == 200
    await injected.aclose()
