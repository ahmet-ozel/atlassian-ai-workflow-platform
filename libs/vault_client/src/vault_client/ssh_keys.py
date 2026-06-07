"""SSH key rotation helper - Ed25519 keygen + dual-slot management.

Implements the R8.2 / R8.4 acceptance criteria: generate Ed25519 key
pairs and orchestrate the ``active`` / ``previous`` dual-slot rotation
pattern through the :class:`~vault_client.client.VaultClient` protocol.

The rotation lifecycle is:

1. **rotate(runner_id)** - generate a fresh Ed25519 keypair, demote the
   current ``active`` slot to ``previous``, write the new key to
   ``active``, and return the new public key (so the operator can add
   it to the target host's ``~/.ssh/authorized_keys``).
2. **finalize(runner_id)** - once the operator confirms the new key
   works, clear the ``previous`` slot so only the new key is accepted.

Between steps 1 and 2, the execution-runner-worker tries both slots
(active first, previous as fallback) ensuring zero-downtime rotation.

Key generation
--------------

Uses the ``cryptography`` library's Ed25519 implementation. The private
key is serialized as PEM (PKCS8, no encryption - Vault provides
at-rest encryption). The public key is serialized in OpenSSH format
(``ssh-ed25519 AAAA...``) for direct use in ``authorized_keys``.

The fingerprint is the SHA-256 hash of the raw public key bytes,
base64-encoded, matching the ``ssh-keygen -l`` output format
(``SHA256:<base64>``).
"""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from .client import RotationResult, SshKey, VaultClient
from .path import VaultPath


def generate_keypair() -> tuple[str, str]:
    """Generate a new Ed25519 SSH keypair.

    Returns:
        A ``(private_pem, public_openssh)`` tuple where:

        * ``private_pem`` is the PKCS8-encoded PEM private key (no
          passphrase - Vault provides at-rest encryption).
        * ``public_openssh`` is the public key in OpenSSH wire format
          (``ssh-ed25519 AAAA...``), suitable for appending to
          ``~/.ssh/authorized_keys`` on the target host.
    """
    private_key = Ed25519PrivateKey.generate()

    private_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.OpenSSH,
        encryption_algorithm=NoEncryption(),
    ).decode("utf-8")

    public_openssh = (
        private_key.public_key()
        .public_bytes(
            encoding=Encoding.OpenSSH,
            format=PublicFormat.OpenSSH,
        )
        .decode("utf-8")
    )

    return private_pem, public_openssh


def _fingerprint(public_openssh: str) -> str:
    """Compute the SHA-256 fingerprint of an OpenSSH public key.

    Matches the ``SHA256:<base64>`` format produced by
    ``ssh-keygen -l -E sha256``.

    Args:
        public_openssh: The public key in OpenSSH format
            (``ssh-ed25519 AAAA...``).

    Returns:
        Fingerprint string in ``SHA256:<base64-no-padding>`` form.
    """
    # The OpenSSH format is: "<algo> <base64-blob> [comment]"
    parts = public_openssh.strip().split()
    if len(parts) < 2:
        raise ValueError(
            "malformed OpenSSH public key - expected "
            "'<algorithm> <base64-blob> [comment]'"
        )
    raw_bytes = base64.b64decode(parts[1])
    digest = hashlib.sha256(raw_bytes).digest()
    b64 = base64.b64encode(digest).rstrip(b"=").decode("ascii")
    return f"SHA256:{b64}"


def _make_ssh_key(private_pem: str, public_openssh: str) -> SshKey:
    """Construct an :class:`SshKey` value object from raw key material."""
    return SshKey(
        private_pem=private_pem,
        public_pem=public_openssh,
        fingerprint=_fingerprint(public_openssh),
    )


def rotate(vault: VaultClient, runner_id: str) -> str:
    """Rotate the SSH key for a runner, returning the new public key.

    Performs the dual-slot rotation:

    1. Generates a fresh Ed25519 keypair.
    2. Demotes the current ``active`` slot to ``previous`` (via the
       :meth:`VaultClient.rotate_ssh_key` protocol method).
    3. Writes the new key to ``active`` and updates ``rotated_at``.

    Args:
        vault: A :class:`VaultClient` instance (any backend).
        runner_id: The runner identifier used to address the Vault
            path ``vault:ssh/runners/<runner_id>/active``.

    Returns:
        The new public key in OpenSSH format (``ssh-ed25519 AAAA...``).
        The operator must add this to the target host's
        ``~/.ssh/authorized_keys`` before calling :func:`finalize`.
    """
    private_pem, public_openssh = generate_keypair()
    ssh_key = _make_ssh_key(private_pem, public_openssh)

    vault.rotate_ssh_key(runner_id, ssh_key)

    # Write rotated_at timestamp to a metadata path so the API can
    # report when the last rotation occurred without parsing Vault
    # version metadata (which local-dev backend doesn't support).
    meta_path = VaultPath.parse(f"vault:ssh/runners/{runner_id}/meta")
    vault.write(
        meta_path,
        {"rotated_at": datetime.now(timezone.utc).isoformat()},
    )

    return public_openssh


def finalize(vault: VaultClient, runner_id: str) -> None:
    """Finalize a rotation by clearing the previous SSH key slot.

    Called after the operator has verified that the new key works
    against the target host. After this call, only the ``active``
    slot contains a valid key; the ``previous`` slot is empty.

    Args:
        vault: A :class:`VaultClient` instance (any backend).
        runner_id: The runner identifier.
    """
    vault.clear_previous_ssh_slot(runner_id)


def read_active(vault: VaultClient, runner_id: str) -> SshKey | None:
    """Read the active SSH key for a runner, or ``None`` if absent.

    Useful for the execution-runner-worker's connection logic and for
    the admin API's ``GET /admin/security/ssh-runners`` endpoint.
    """
    active_path = VaultPath.parse(f"vault:ssh/runners/{runner_id}/active")
    try:
        data = vault.read(active_path)
    except KeyError:
        return None
    return SshKey(
        private_pem=data.get("private_pem", ""),
        public_pem=data.get("public_pem", ""),
        fingerprint=data.get("fingerprint", ""),
    )


def read_previous(vault: VaultClient, runner_id: str) -> SshKey | None:
    """Read the previous SSH key for a runner, or ``None`` if absent.

    Used by the execution-runner-worker as a fallback when the active
    key is rejected (``Permission denied``).
    """
    previous_path = VaultPath.parse(f"vault:ssh/runners/{runner_id}/previous")
    try:
        data = vault.read(previous_path)
    except KeyError:
        return None
    return SshKey(
        private_pem=data.get("private_pem", ""),
        public_pem=data.get("public_pem", ""),
        fingerprint=data.get("fingerprint", ""),
    )


def read_rotation_meta(vault: VaultClient, runner_id: str) -> datetime | None:
    """Read the ``rotated_at`` timestamp for a runner, or ``None``.

    Returns the UTC datetime of the last successful rotation, used by
    the admin API to populate the ``last_rotated_at`` field.
    """
    meta_path = VaultPath.parse(f"vault:ssh/runners/{runner_id}/meta")
    try:
        data = vault.read(meta_path)
    except KeyError:
        return None
    raw = data.get("rotated_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


__all__ = [
    "finalize",
    "generate_keypair",
    "read_active",
    "read_previous",
    "read_rotation_meta",
    "rotate",
]
