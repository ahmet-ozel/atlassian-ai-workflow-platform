"""``VaultClient`` :class:`typing.Protocol` and supporting types.

Mirrors design.md §"libs/vault_client" — the protocol is the single
contract every backend (``HashicorpBackend``, ``LocalDevBackend``)
satisfies. Property test 11 (``test_vault_backends.py``) asserts the
two backends produce equivalent ``read(write(p, v)) == v`` round-trips
through this protocol.

The protocol is **synchronous** for now: most call sites in this spec
(boot-time credential resolution, atomic department create) are simple
read/write pairs and do not benefit from async. SSH dual-slot rotation
and webhook secret 1h overlap helpers are exposed as protocol methods
so backends can specialise them (e.g. the Hashicorp backend will use
KV-v2 versioning on the secret, while the local-dev backend simulates
slots with two separate file entries).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Mapping, Protocol, runtime_checkable

from .path import VaultPath


# ---------------------------------------------------------------------------
# Value objects exchanged across the protocol
# ---------------------------------------------------------------------------

#: Permitted backend identifiers; surfaced on each implementation as a
#: ``backend`` attribute so callers can branch (e.g. for development
#: warnings) without doing ``isinstance`` checks.
Backend = Literal["hashicorp", "local-dev"]


@dataclass(frozen=True, slots=True)
class SshKey:
    """An SSH key pair payload stored at a runner slot.

    The ``private_pem`` value is the raw key material — it MUST NOT be
    logged or echoed back to clients. The ``fingerprint`` and
    ``public_pem`` fields are safe-to-display.
    """

    private_pem: str
    public_pem: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class RotationResult:
    """Outcome of a slotted-rotation operation.

    Attributes:
        active_path: The Vault path that now holds the *current*
            credential (e.g. ``vault:ssh/runners/<id>/active``).
        previous_path: The Vault path that holds the *previous*
            credential during the overlap window. ``None`` when the
            previous slot has been cleared after successful validation.
        rotated_at: UTC timestamp at which the rotation completed.
        overlap_until: When the previous credential will stop being
            accepted. ``None`` for SSH dual-slot rotation, where the
            previous slot is cleared as soon as the new key validates;
            populated for webhook-secret rotation, where Atlassian
            payload signatures must be accepted under both secrets for
            up to one hour (R6.8).
    """

    active_path: VaultPath
    previous_path: VaultPath | None
    rotated_at: datetime
    overlap_until: datetime | None = None


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class VaultClient(Protocol):
    """Pluggable Vault KV / rotation client.

    Two implementations live in this package:

    * :class:`vault_client.hashicorp_backend.HashicorpBackend` —
      production Hashicorp Vault HTTP (KV v2).
    * :class:`vault_client.local_dev_backend.LocalDevBackend` —
      development-only encrypted file backend (libsodium / NaCl
      ``SecretBox``); rejects plain-text writes (R6.6).

    Protocol semantics
    ------------------

    * ``read`` / ``write`` / ``delete`` operate on a single
      :class:`VaultPath`; values are flat ``Mapping[str, str]`` to map
      cleanly onto Vault KV-v2 ``data:`` payloads.
    * ``rotate_ssh_key`` and ``rotate_webhook_secret`` are *opinionated*
      higher-level operations: implementations write to the
      ``active`` / ``previous`` paths or KV-v2 versions described in
      ``design.md`` and return a :class:`RotationResult` so callers
      can audit the rotation deterministically.
    """

    backend: Backend
    """Identifier surfaced for diagnostics and dev-warnings."""

    # ----- KV operations --------------------------------------------------

    def read(self, path: VaultPath) -> Mapping[str, str]:
        """Read the secret stored at *path*.

        Returns a flat mapping of string keys to string values
        (matching Vault KV-v2 ``data:`` shape). Raises
        :class:`KeyError` if no secret exists at *path*.
        """
        ...

    def write(self, path: VaultPath, data: Mapping[str, str]) -> None:
        """Write *data* to *path*, replacing any existing value."""
        ...

    def delete(self, path: VaultPath) -> None:
        """Remove the secret stored at *path*.

        Implementations MUST treat deletion of a non-existent path as a
        no-op (idempotency); this matches Hashicorp Vault KV-v2's
        ``DELETE /data/<path>`` semantics and keeps rollback paths in
        the atomic-create flow (R3.6) simple.
        """
        ...

    # ----- Rotation helpers -----------------------------------------------

    def rotate_ssh_key(
        self,
        runner_id: str,
        new_key: SshKey,
    ) -> RotationResult:
        """Dual-slot SSH key rotation (R6.7, MIMARI §13 E8).

        Writes *new_key* to ``vault:ssh/runners/<runner_id>/active``
        while preserving the prior active key at
        ``vault:ssh/runners/<runner_id>/previous`` until the caller
        validates the new key end-to-end.
        """
        ...

    def clear_previous_ssh_slot(self, runner_id: str) -> None:
        """Drop the ``previous`` SSH slot after the new key is validated (R6.7).

        Called by the runner-rotation orchestrator once the freshly
        rotated key has been verified end-to-end against the remote
        host. Implementations MUST treat a missing slot as a no-op so
        the caller can invoke this idempotently.
        """
        ...

    def rotate_webhook_secret(
        self,
        provider: str,
        dept_id: str,
        new_secret: str,
    ) -> RotationResult:
        """Per-department webhook secret rotation with 1h overlap (R6.8).

        After this call, both the new and the prior secret SHALL be
        considered valid for HMAC verification for one hour; after the
        overlap window expires only the new secret is accepted.
        Implementations encode this with KV-v2 versioning (Hashicorp)
        or a per-path ``previous`` slot (local-dev).
        """
        ...


__all__ = [
    "Backend",
    "RotationResult",
    "SshKey",
    "VaultClient",
]
