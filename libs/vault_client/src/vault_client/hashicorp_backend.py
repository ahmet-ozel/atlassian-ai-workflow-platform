"""Hashicorp Vault HTTP backend (KV v2 mount).

Implements :class:`vault_client.client.VaultClient` against a real
Hashicorp Vault server speaking the KV-v2 API. ``vault:atlassian/...``
references are translated to ``<mount>/data/atlassian/...`` HTTP paths,
matching the design note in ``design.md`` §"Araştırma Notları".

This module deliberately keeps the surface area minimal - it exposes
only the protocol methods plus the constructor parameters. Higher-level
concerns (retries, circuit breakers, request signing) live in caller
code; the property test (``test_vault_backends.py``) injects a fake
HTTP transport via :class:`httpx.MockTransport` to assert protocol
equivalence with the local-dev backend without standing up a Vault
server.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Mapping

import httpx

from .client import Backend, RotationResult, SshKey, VaultClient
from .path import VaultPath

# Webhook secret overlap window (R6.8).
_WEBHOOK_ROTATION_OVERLAP = timedelta(hours=1)


class HashicorpBackend(VaultClient):
    """Real Hashicorp Vault KV-v2 HTTP backend.

    Args:
        addr: Base URL of the Vault server, e.g.
            ``"https://vault.internal:8200"``.
        token: Vault token used for the ``X-Vault-Token`` header.
        mount: KV-v2 mount path. Defaults to ``"secret"``.
        client: Optional pre-configured :class:`httpx.Client` - useful
            for tests that wire an :class:`httpx.MockTransport`.
        timeout: Per-request HTTP timeout in seconds. Default: 5.0.
    """

    backend: Backend = "hashicorp"

    def __init__(
        self,
        addr: str,
        token: str,
        *,
        mount: str = "secret",
        client: httpx.Client | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._addr = addr.rstrip("/")
        self._token = token
        self._mount = mount.strip("/")
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _data_url(self, path: VaultPath) -> str:
        """Translate ``vault:<rel>`` to ``<addr>/v1/<mount>/data/<rel>``."""
        return f"{self._addr}/v1/{self._mount}/data/{path.relative}"

    def _headers(self) -> dict[str, str]:
        return {"X-Vault-Token": self._token}

    # ------------------------------------------------------------------
    # KV operations
    # ------------------------------------------------------------------

    def read(self, path: VaultPath) -> Mapping[str, str]:
        resp = self._client.get(self._data_url(path), headers=self._headers())
        if resp.status_code == 404:
            raise KeyError(path.raw)
        resp.raise_for_status()
        envelope = resp.json()
        # KV v2 wraps the payload as ``{"data": {"data": {...}, "metadata": {...}}}``.
        data = envelope.get("data", {}).get("data", {})
        return {str(k): str(v) for k, v in data.items()}

    def write(self, path: VaultPath, data: Mapping[str, str]) -> None:
        resp = self._client.post(
            self._data_url(path),
            headers=self._headers(),
            json={"data": dict(data)},
        )
        resp.raise_for_status()

    def delete(self, path: VaultPath) -> None:
        resp = self._client.delete(self._data_url(path), headers=self._headers())
        # 404 is treated as success (idempotent deletion).
        if resp.status_code in (200, 204, 404):
            return
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Rotation helpers (R6.7, R6.8)
    # ------------------------------------------------------------------

    def rotate_ssh_key(
        self,
        runner_id: str,
        new_key: SshKey,
    ) -> RotationResult:
        active = VaultPath.parse(f"vault:ssh/runners/{runner_id}/active")
        previous = VaultPath.parse(f"vault:ssh/runners/{runner_id}/previous")

        # 1. Move current active -> previous (best-effort; absent slot
        # on the very first rotation is a no-op).
        try:
            current = self.read(active)
        except KeyError:
            current = None
        if current is not None:
            self.write(previous, current)

        # 2. Write the new key to the active slot.
        self.write(
            active,
            {
                "private_pem": new_key.private_pem,
                "public_pem": new_key.public_pem,
                "fingerprint": new_key.fingerprint,
            },
        )

        return RotationResult(
            active_path=active,
            previous_path=previous if current is not None else None,
            rotated_at=datetime.now(timezone.utc),
        )

    def clear_previous_ssh_slot(self, runner_id: str) -> None:
        """Idempotent removal of the ``previous`` SSH slot (R6.7).

        Called once the freshly rotated key has been validated against
        the remote runner host. We rely on Vault KV-v2's ``DELETE``
        being a no-op for missing paths, so ``delete`` already handles
        the "previous slot already cleared" case.
        """
        previous = VaultPath.parse(f"vault:ssh/runners/{runner_id}/previous")
        self.delete(previous)

    def rotate_webhook_secret(
        self,
        provider: str,
        dept_id: str,
        new_secret: str,
    ) -> RotationResult:
        active = VaultPath.parse(f"vault:webhooks/{provider}/{dept_id}")
        previous = VaultPath.parse(
            f"vault:webhooks/{provider}/{dept_id}/previous"
        )

        try:
            current = self.read(active)
        except KeyError:
            current = None
        rotated_at = datetime.now(timezone.utc)
        if current is not None:
            # Stash the prior secret + an explicit expiry timestamp so
            # the webhook handler (R6.8) can refuse it after one hour.
            overlap_until = rotated_at + _WEBHOOK_ROTATION_OVERLAP
            self.write(
                previous,
                {
                    **current,
                    "overlap_until": overlap_until.isoformat(),
                },
            )
        else:
            overlap_until = None

        self.write(active, {"secret": new_secret})

        return RotationResult(
            active_path=active,
            previous_path=previous if current is not None else None,
            rotated_at=rotated_at,
            overlap_until=overlap_until,
        )

    # ------------------------------------------------------------------
    # Context-manager / cleanup support
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying ``httpx.Client`` if we own it."""
        if self._owns_client:
            self._client.close()


__all__ = ["HashicorpBackend"]
