"""Vault KV-v2 wrapper for Env_Override storage.

This module implements :class:`VaultClient`, the **only** persistence
path the admin-dashboard control plane uses for operator-supplied
``Env_Override`` values. Every key/value pair sent through
``POST /admin/services/{name}/start`` is round-tripped through the live Vault
KV-v2 mount; nothing is staged on the host filesystem, in temp files, or in log
lines.

Endpoints (Vault KV-v2 - see Vault docs ``secret/kv-v2``):

* ``PUT  {addr}/v1/{kv_mount}/data/services/{service_name}/{key}``
  body ``{"data": {"value": value}}`` → atomic per-key write.
* ``LIST {addr}/v1/{kv_mount}/metadata/services/{service_name}/?list=true``
  → ``{"data": {"keys": [...]}}``; ``404`` means the prefix is empty
  (no first-time start has happened yet) and is **not** an error.
* ``GET  {addr}/v1/{kv_mount}/data/services/{service_name}/{key}``
  → ``{"data": {"data": {"value": "..."}}}``.
* ``DELETE {addr}/v1/{kv_mount}/data/services/{service_name}/{key}``
  → soft-delete latest version.

Authentication uses the ``X-Vault-Token`` header on every request.

Failure handling: any ``404`` outside of the empty-prefix LIST case, any
``5xx``, and any non-2xx returned by the *write* or *delete* endpoints raises
:class:`VaultWriteError`. The lifecycle service wraps that into a
``502 Bad Gateway`` response and transitions the service into ``failed`` state.

Design constraints honoured:

* **No disk I/O.** The implementation only opens an in-memory
  :class:`httpx.AsyncClient`; it never imports :mod:`pathlib` or
  :func:`open` for read/write.
* **Pure async.** All public methods are coroutines suitable for use
  from FastAPI request handlers.
* **No secret leakage in exceptions.** :class:`VaultWriteError`
  carries the service name, key, status code, and operation name -
  but never the value being written.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

__all__ = ["VaultClient", "VaultWriteError"]


class VaultWriteError(Exception):
    """Raised when a Vault KV-v2 write/read/delete fails fatally.

    The lifecycle service maps this to ``502 Bad Gateway`` and the
    ``failed`` service state.

    Attributes
    ----------
    operation:
        The Vault operation that failed (``"write"``, ``"read"``,
        ``"list"``, ``"delete"``).
    service_name:
        The Managed_Service whose env override was being touched.
    key:
        The env-override key (or ``None`` for a list operation).
    status_code:
        The HTTP status code returned by Vault, or ``None`` when the
        failure was a transport error (DNS, TCP, TLS, ...). The error
        is raised for ``404`` and ``5xx``; ``4xx`` responses other than
        ``404`` are also treated as fatal because they indicate a
        misconfigured token / policy and the operator cannot recover by retry.
    """

    def __init__(
        self,
        *,
        operation: str,
        service_name: str,
        key: str | None,
        status_code: int | None,
        message: str,
    ) -> None:
        self.operation = operation
        self.service_name = service_name
        self.key = key
        self.status_code = status_code
        # The exception ``str()`` intentionally never contains the
        # secret value - only metadata. This lets handlers log the
        # error freely without leaking material.
        super().__init__(
            f"Vault {operation} failed for service={service_name!r} "
            f"key={key!r} status={status_code}: {message}"
        )


def _quote_segment(value: str) -> str:
    """URL-encode a single Vault path segment.

    Vault path segments follow the same percent-encoding rules as
    generic URL path components. We disallow ``/`` so a malicious key
    can't escape the configured ``services/<name>/`` prefix.
    """

    return quote(value, safe="")


class VaultClient:
    """Async wrapper around the Vault KV-v2 HTTP API.

    Parameters
    ----------
    addr:
        Base URL of the Vault server, e.g. ``"http://vault:8200"``.
        Must not end in ``/v1`` - the wrapper appends the API prefix.
    token:
        Vault token. Sent on every request as the ``X-Vault-Token``
        header. The token is held only in memory; the wrapper never
        writes it to disk.
    kv_mount:
        Mount path of the KV-v2 secret engine. Defaults to ``"secret"``.
    client:
        Optional pre-built :class:`httpx.AsyncClient`. Tests inject one
        backed by ``httpx.MockTransport`` (or :mod:`respx`) to stay
        wholly off the network. When ``None``, a fresh client is built
        on first use; callers should then ``await close()`` to release
        connections.
    timeout:
        Per-request timeout in seconds. Only used when ``client`` is
        ``None``.
    """

    #: ``Authorization`` header name per Vault HTTP API.
    _TOKEN_HEADER = "X-Vault-Token"

    def __init__(
        self,
        *,
        addr: str,
        token: str,
        kv_mount: str = "secret",
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not addr:
            raise ValueError("addr must be a non-empty Vault base URL")
        if not token:
            raise ValueError("token must be a non-empty Vault token")
        if not kv_mount:
            raise ValueError("kv_mount must be a non-empty mount path")

        # Strip a single trailing slash so URL joins are deterministic;
        # we never accept ``http://vault:8200/v1`` because the path
        # construction below already includes ``/v1/``.
        self._addr = addr.rstrip("/")
        self._token = token
        self._kv_mount = kv_mount.strip("/")
        self._owns_client = client is None
        self._client = client
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Release the underlying HTTP client when this wrapper owns it.

        Safe to call multiple times. When the caller injected a client
        (the test path), the caller is responsible for closing it.
        """

        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def __aenter__(self) -> "VaultClient":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def write_kv2_secret(
        self,
        *,
        path: str,
        data: dict[str, str],
    ) -> None:
        """Write a multi-key secret to a Vault KV-v2 path.

        :func:`write_env_override` is scoped to
        ``data/services/{service_name}/{key}`` with a single
        ``{"data": {"value": ...}}`` shape - perfect for env overrides but
        wrong for multi-field secrets like SSH credentials which the worker
        fetches as ``{host, port, user, private_key}``.

        This method writes ``{"data": data}`` to ``{kv_mount}/data/{path}``
        so the canonical SSH secret shape can be stored at
        ``ssh/runners/{runner_id}/active`` and the worker's
        :func:`vault_fetch_ssh_credentials` finds all four required
        fields at the resolved ``vault_path``.

        Parameters
        ----------
        path:
            KV-v2 path relative to the mount, e.g.
            ``"ssh/runners/runner-01/active"``. Forward slashes are
            preserved (Vault treats them as namespace separators).
        data:
            Flat mapping of string keys to string values to store.
        """
        # Normalise the path: strip the canonical ``vault:`` prefix and
        # any leading slash so callers can pass either form.
        normalised = path
        if normalised.startswith("vault:"):
            normalised = normalised[len("vault:"):]
        normalised = normalised.strip().lstrip("/")
        url = f"{self._addr}/v1/{self._kv_mount}/data/{normalised}"

        try:
            response = await self._request(
                "PUT",
                url,
                headers={self._TOKEN_HEADER: self._token},
                json={"data": dict(data)},
            )
        except httpx.HTTPError as exc:
            raise VaultWriteError(
                operation="write_kv2",
                service_name=normalised,
                key="<bulk>",
                status_code=None,
                message=f"transport error: {exc.__class__.__name__}",
            ) from exc

        if not _is_success(response.status_code):
            raise VaultWriteError(
                operation="write_kv2",
                service_name=normalised,
                key="<bulk>",
                status_code=response.status_code,
                message=_short_body(response),
            )

    async def read_kv2_secret(self, *, path: str) -> dict[str, str] | None:
        """Read a flat KV-v2 secret from an arbitrary Vault path."""

        normalised = self._normalise_kv2_path(path)
        url = self._kv2_data_url(normalised)
        try:
            response = await self._request(
                "GET",
                url,
                headers={self._TOKEN_HEADER: self._token},
            )
        except httpx.HTTPError as exc:
            raise VaultWriteError(
                operation="read_kv2",
                service_name=normalised,
                key="<bulk>",
                status_code=None,
                message=f"transport error: {exc.__class__.__name__}",
            ) from exc

        if response.status_code == 404:
            return None
        if not _is_success(response.status_code):
            raise VaultWriteError(
                operation="read_kv2",
                service_name=normalised,
                key="<bulk>",
                status_code=response.status_code,
                message=_short_body(response),
            )

        payload = _safe_json(response)
        if not isinstance(payload, dict):
            return {}
        outer = payload.get("data")
        if not isinstance(outer, dict):
            return {}
        inner = outer.get("data")
        if not isinstance(inner, dict):
            return {}
        return {str(k): str(v) for k, v in inner.items() if isinstance(k, str)}

    async def delete_kv2_secret(self, *, path: str) -> None:
        """Soft-delete the latest version of an arbitrary KV-v2 secret."""

        normalised = self._normalise_kv2_path(path)
        url = self._kv2_data_url(normalised)
        try:
            response = await self._request(
                "DELETE",
                url,
                headers={self._TOKEN_HEADER: self._token},
            )
        except httpx.HTTPError as exc:
            raise VaultWriteError(
                operation="delete_kv2",
                service_name=normalised,
                key="<bulk>",
                status_code=None,
                message=f"transport error: {exc.__class__.__name__}",
            ) from exc

        if response.status_code == 404:
            return
        if not _is_success(response.status_code):
            raise VaultWriteError(
                operation="delete_kv2",
                service_name=normalised,
                key="<bulk>",
                status_code=response.status_code,
                message=_short_body(response),
            )

    async def write_env_override(
        self,
        *,
        service_name: str,
        key: str,
        value: str,
    ) -> None:
        """Write a single Env_Override key/value into Vault KV-v2.

        ``PUT {addr}/v1/{kv_mount}/data/services/{service_name}/{key}``
        with body ``{"data": {"value": value}}``. Each key is its own
        atomic write: a partial failure across N keys leaves the
        already-written keys intact, and the lifecycle service surfaces
        the failure as ``502``.
        """

        url = self._data_url(service_name, key)
        try:
            response = await self._request(
                "PUT",
                url,
                headers={self._TOKEN_HEADER: self._token},
                json={"data": {"value": value}},
            )
        except httpx.HTTPError as exc:
            raise VaultWriteError(
                operation="write",
                service_name=service_name,
                key=key,
                status_code=None,
                message=f"transport error: {exc.__class__.__name__}",
            ) from exc

        # Vault returns 200 for KV-v2 ``data`` writes (and historically
        # 204 for KV-v1). Anything else - 4xx misconfiguration, 404 on
        # an unmounted engine, 5xx server failure - is fatal.
        if not _is_success(response.status_code):
            raise VaultWriteError(
                operation="write",
                service_name=service_name,
                key=key,
                status_code=response.status_code,
                message=_short_body(response),
            )

    async def read_env_overrides(
        self,
        *,
        service_name: str,
    ) -> dict[str, str]:
        """Return ``{key: value}`` for every Env_Override the operator
        has previously stored for ``service_name``.

        Implementation: ``LIST`` the metadata prefix to enumerate keys,
        then ``GET`` each one. A ``404`` on the LIST means the operator
        has never started this service before; we return ``{}`` instead
        of raising; write failures and genuine 5xx errors on read still
        surface as errors.
        """

        keys = await self._list_keys(service_name)
        result: dict[str, str] = {}
        for key in keys:
            value = await self._read_value(service_name, key)
            if value is not None:
                # Vault may transiently surface a key in LIST whose
                # latest version was soft-deleted; treat it as absent.
                result[key] = value
        return result

    async def list_env_override_keys(
        self,
        *,
        service_name: str,
    ) -> list[str]:
        """List every Env_Override key currently stored under ``service_name``.

        ``LIST {addr}/v1/{kv_mount}/metadata/services/{service_name}/?list=true``.
        Returns the bare key names (no ``services/{name}/`` prefix and
        no trailing slash on directory entries - those are filtered
        out so callers receive a flat list of leaf keys).

        Used by the lifecycle stop endpoint's ``purge_vault=true``
        path to enumerate every path that needs to be soft-deleted after
        Compose down. A
        ``404`` on the LIST means the prefix has never been written
        to (the operator never started the service with overrides) -
        in that case we return ``[]`` rather than raising, so the
        caller can short-circuit cleanly.

        Any non-404 / non-2xx response - DNS failures, 5xx, 4xx
        misconfigurations - surfaces as :class:`VaultWriteError` so
        the caller can decide whether to treat the failure as fatal
        or best-effort. The lifecycle stop endpoint specifically
        catches that exception and emits a
        ``vault_purge_partial_failure`` audit row without aborting
        the (already-successful) Compose stop.
        """

        return await self._list_keys(service_name)

    async def delete_env_override(
        self,
        *,
        service_name: str,
        key: str,
    ) -> None:
        """Soft-delete the latest version of one Env_Override key.

        ``DELETE {addr}/v1/{kv_mount}/data/services/{service_name}/{key}``.
        Vault returns ``204`` on success and ``404`` when the path
        never existed. We treat ``404`` as a no-op (idempotent delete);
        any ``5xx`` becomes :class:`VaultWriteError`.
        """

        url = self._data_url(service_name, key)
        try:
            response = await self._request(
                "DELETE",
                url,
                headers={self._TOKEN_HEADER: self._token},
            )
        except httpx.HTTPError as exc:
            raise VaultWriteError(
                operation="delete",
                service_name=service_name,
                key=key,
                status_code=None,
                message=f"transport error: {exc.__class__.__name__}",
            ) from exc

        if response.status_code == 404:
            return  # idempotent
        if not _is_success(response.status_code):
            raise VaultWriteError(
                operation="delete",
                service_name=service_name,
                key=key,
                status_code=response.status_code,
                message=_short_body(response),
            )

    # ------------------------------------------------------------------
    # LLM provider credential helpers
    # ------------------------------------------------------------------
    #
    # Target Vault KV-v2 path: ``secret/data/llm-providers/{provider_id}/credentials``.
    # Re-uses the existing ``X-Vault-Token`` header, the KV-v2 envelope shape
    # (``{"data": {...}}``), the :class:`VaultWriteError` taxonomy and the
    # 404-as-no-op delete semantics already implemented for the env-override
    # surface above. The ``service_name`` slot on the error is repurposed to
    # carry the stringified provider UUID so the error messages stay
    # consistent across operations.

    def _llm_credentials_url(self, provider_id: UUID) -> str:
        """Return the canonical Vault KV-v2 data URL for *provider_id*."""

        return (
            f"{self._addr}/v1/{self._kv_mount}/data/llm-providers/"
            f"{_quote_segment(str(provider_id))}/credentials"
        )

    async def write_llm_credentials(
        self, *, provider_id: UUID, payload: dict[str, str]
    ) -> None:
        """Write the LLM credential *payload* into Vault KV-v2.

        ``PUT {addr}/v1/{kv_mount}/data/llm-providers/{provider_id}/credentials``
        with body ``{"data": payload}``. The *payload* is whatever
        :class:`~llm_providers.service.ProviderService` builds for the
        provider type (e.g. ``{"api_key": "sk-..."}`` for OpenAI, with
        the optional ``"organization"`` field included only when the
        operator set one). The plain credential is **never** logged or
        echoed back to the caller; only the masked form derived via
        :func:`~llm_providers.masking.mask` ever leaves this process.

        On any non-2xx response (including 4xx misconfigurations and
        5xx server failures) a :class:`VaultWriteError` is raised so the
        service layer can issue ``ROLLBACK`` and surface
        ``502 vault_write_failed``.
        """

        url = self._llm_credentials_url(provider_id)
        try:
            response = await self._request(
                "PUT",
                url,
                headers={self._TOKEN_HEADER: self._token},
                json={"data": payload},
            )
        except httpx.HTTPError as exc:
            raise VaultWriteError(
                operation="write",
                service_name=f"llm-providers/{provider_id}",
                key="credentials",
                status_code=None,
                message=f"transport error: {exc.__class__.__name__}",
            ) from exc

        if not _is_success(response.status_code):
            raise VaultWriteError(
                operation="write",
                service_name=f"llm-providers/{provider_id}",
                key="credentials",
                status_code=response.status_code,
                message=_short_body(response),
            )

    async def read_llm_credentials(
        self, *, provider_id: UUID
    ) -> dict[str, str]:
        """Return the credential payload stored for *provider_id*.

        ``GET {addr}/v1/{kv_mount}/data/llm-providers/{provider_id}/credentials``.
        Returns the inner ``data.data`` dict so callers receive the raw
        credential payload as a plain ``{str: str}`` mapping.  A 404 is
        treated as an empty payload (``{}``) because a provider row may
        exist in Postgres with Vault material still being written
        through a future migration step; the service layer handles the
        empty case by emitting an empty mask.

        Any non-404 non-2xx surfaces as :class:`VaultWriteError`.
        """

        url = self._llm_credentials_url(provider_id)
        try:
            response = await self._request(
                "GET",
                url,
                headers={self._TOKEN_HEADER: self._token},
            )
        except httpx.HTTPError as exc:
            raise VaultWriteError(
                operation="read",
                service_name=f"llm-providers/{provider_id}",
                key="credentials",
                status_code=None,
                message=f"transport error: {exc.__class__.__name__}",
            ) from exc

        if response.status_code == 404:
            return {}
        if not _is_success(response.status_code):
            raise VaultWriteError(
                operation="read",
                service_name=f"llm-providers/{provider_id}",
                key="credentials",
                status_code=response.status_code,
                message=_short_body(response),
            )

        payload = _safe_json(response)
        if not isinstance(payload, dict):
            return {}
        outer = payload.get("data")
        if not isinstance(outer, dict):
            return {}
        inner = outer.get("data")
        if not isinstance(inner, dict):
            return {}
        # Filter to ``{str: str}`` so a stray non-string value cannot
        # break downstream masking / JSON serialisation.
        return {
            str(k): str(v)
            for k, v in inner.items()
            if isinstance(k, str) and isinstance(v, str)
        }

    async def delete_llm_credentials(
        self, *, provider_id: UUID
    ) -> None:
        """Soft-delete the latest credential version for *provider_id*.

        ``DELETE {addr}/v1/{kv_mount}/data/llm-providers/{provider_id}/credentials``.
        Vault returns ``204`` on success and ``404`` when the path was
        never written (or already deleted). We treat ``404`` as a no-op
        (idempotent delete); any other non-2xx becomes
        :class:`VaultWriteError` so the service layer can surface
        ``502 vault_delete_failed`` and leave the Postgres row intact.
        """

        url = self._llm_credentials_url(provider_id)
        try:
            response = await self._request(
                "DELETE",
                url,
                headers={self._TOKEN_HEADER: self._token},
            )
        except httpx.HTTPError as exc:
            raise VaultWriteError(
                operation="delete",
                service_name=f"llm-providers/{provider_id}",
                key="credentials",
                status_code=None,
                message=f"transport error: {exc.__class__.__name__}",
            ) from exc

        if response.status_code == 404:
            return  # idempotent
        if not _is_success(response.status_code):
            raise VaultWriteError(
                operation="delete",
                service_name=f"llm-providers/{provider_id}",
                key="credentials",
                status_code=response.status_code,
                message=_short_body(response),
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _data_url(self, service_name: str, key: str) -> str:
        return (
            f"{self._addr}/v1/{self._kv_mount}/data/services/"
            f"{_quote_segment(service_name)}/{_quote_segment(key)}"
        )

    @staticmethod
    def _normalise_kv2_path(path: str) -> str:
        normalised = path
        if normalised.startswith("vault:"):
            normalised = normalised[len("vault:"):]
        return normalised.strip().lstrip("/")

    def _kv2_data_url(self, path: str) -> str:
        return f"{self._addr}/v1/{self._kv_mount}/data/{path}"

    def _metadata_list_url(self, service_name: str) -> str:
        # Trailing slash is required so Vault treats the path as a
        # directory; the ``?list=true`` query parameter selects LIST
        # semantics on the GET verb (Vault accepts both LIST verb and
        # ``?list=true`` query - we use the query form for httpx
        # compatibility).
        return (
            f"{self._addr}/v1/{self._kv_mount}/metadata/services/"
            f"{_quote_segment(service_name)}/?list=true"
        )

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._client is not None:
            return await self._client.request(method, url, **kwargs)
        return await asyncio.to_thread(self._sync_request, method, url, kwargs)

    def _sync_request(
        self,
        method: str,
        url: str,
        kwargs: dict[str, Any],
    ) -> httpx.Response:
        with httpx.Client(timeout=self._timeout, trust_env=False) as client:
            return client.request(method, url, **kwargs)

    async def _list_keys(self, service_name: str) -> list[str]:
        url = self._metadata_list_url(service_name)
        try:
            response = await self._request(
                "GET",
                url,
                headers={self._TOKEN_HEADER: self._token},
            )
        except httpx.HTTPError as exc:
            raise VaultWriteError(
                operation="list",
                service_name=service_name,
                key=None,
                status_code=None,
                message=f"transport error: {exc.__class__.__name__}",
            ) from exc

        if response.status_code == 404:
            # Empty prefix on first start.
            return []
        if not _is_success(response.status_code):
            raise VaultWriteError(
                operation="list",
                service_name=service_name,
                key=None,
                status_code=response.status_code,
                message=_short_body(response),
            )

        payload = _safe_json(response)
        keys = _extract_list_keys(payload)
        # Filter out directory-style entries (``foo/``) - Vault returns
        # them when the listing has nested folders, but env overrides
        # are flat by design.
        return [k for k in keys if isinstance(k, str) and not k.endswith("/")]

    async def _read_value(self, service_name: str, key: str) -> str | None:
        url = self._data_url(service_name, key)
        try:
            response = await self._request(
                "GET",
                url,
                headers={self._TOKEN_HEADER: self._token},
            )
        except httpx.HTTPError as exc:
            raise VaultWriteError(
                operation="read",
                service_name=service_name,
                key=key,
                status_code=None,
                message=f"transport error: {exc.__class__.__name__}",
            ) from exc

        if response.status_code == 404:
            # Key was listed but its latest version is soft-deleted.
            return None
        if not _is_success(response.status_code):
            raise VaultWriteError(
                operation="read",
                service_name=service_name,
                key=key,
                status_code=response.status_code,
                message=_short_body(response),
            )

        payload = _safe_json(response)
        return _extract_kv2_value(payload)


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _is_success(status_code: int) -> bool:
    return 200 <= status_code < 300


def _short_body(response: httpx.Response) -> str:
    """Return at most 200 characters of the response body for error context."""

    try:
        text = response.text
    except Exception:  # pragma: no cover - httpx body decoding edge case
        return "<unreadable body>"
    if len(text) > 200:
        return text[:200] + "…"
    return text


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _extract_list_keys(payload: Any) -> list[Any]:
    """Pull ``data.keys`` out of a Vault LIST response, defensively."""

    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    keys = data.get("keys")
    if not isinstance(keys, list):
        return []
    return keys


def _extract_kv2_value(payload: Any) -> str | None:
    """Pull ``data.data.value`` out of a Vault KV-v2 GET response.

    KV-v2 wraps the user-visible secret as ``{"data": {"data":
    {"value": "..."}}}``. Anything that doesn't conform (missing keys,
    non-string value, non-dict ``data``) returns ``None`` so the caller
    can skip the entry rather than crashing the whole read.
    """

    if not isinstance(payload, dict):
        return None
    outer = payload.get("data")
    if not isinstance(outer, dict):
        return None
    inner = outer.get("data")
    if not isinstance(inner, dict):
        return None
    value = inner.get("value")
    if isinstance(value, str):
        return value
    return None
