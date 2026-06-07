"""``LocalDevBackend`` - encrypted-file Vault backend for development.

Implements :class:`vault_client.client.VaultClient` against a single
file on disk whose payload is encrypted end-to-end with libsodium's
authenticated XSalsa20-Poly1305 :class:`nacl.secret.SecretBox`.
Plain-text writes are rejected.

Threat model
------------

* The *file on disk* is unintelligible without the symmetric key, so
  ``test_no_disk_secret_leak.py`` cannot grep for plain-text Atlassian
  tokens or webhook secrets after a successful write.
* The key itself is sourced from the ``VAULT_LOCAL_KEY`` environment
  variable (32 bytes, hex- or base64-encoded). Plain-text strings (i.e.
  the env var being unset, empty, or a known-weak placeholder) are
  rejected at construction time so a misconfigured developer can never
  end up with an "encryption" backend that silently writes secrets in
  the clear.
* The on-disk format is a JSON file whose top-level value is a
  base64-encoded ``nonce || ciphertext`` blob - the JSON wrapper exists
  only so the file can carry forward-compatible metadata; the wrapper
  itself never contains a plain-text secret.

This backend is **not** suitable for production. The factory issues a
warning when ``VAULT_BACKEND=local-dev`` (see :mod:`vault_client.factory`).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping

import nacl.exceptions
import nacl.secret
import nacl.utils

from .client import Backend, RotationResult, SshKey, VaultClient
from .path import VaultPath

# Webhook secret overlap window; duplicated here so the local-dev
# backend can be exercised independently of the Hashicorp backend.
_WEBHOOK_ROTATION_OVERLAP = timedelta(hours=1)

# A hard-coded "weak placeholder" rejection list so a developer who
# pastes the README's example string never ends up with a backend
# whose key is publicly known.
_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "default",
        "dev",
        "dev-only",
        "_dev_only",
        "00000000000000000000000000000000",
    }
)

#: ``nacl.secret.SecretBox.KEY_SIZE`` is 32 bytes; surfaced as a module
#: constant so callers (e.g. tests) can generate a fresh key.
KEY_SIZE: int = nacl.secret.SecretBox.KEY_SIZE


def _decode_key(raw: str) -> bytes:
    """Decode a hex- or base64-encoded 32-byte key.

    Order: try hex first (common in existing projects), then
    standard base64, then URL-safe base64. The first decoder that
    yields exactly :data:`KEY_SIZE` bytes wins.
    """
    candidates: list[bytes] = []
    for decoder in (
        bytes.fromhex,
        base64.b64decode,
        base64.urlsafe_b64decode,
    ):
        try:
            candidates.append(decoder(raw))
        except (ValueError, binascii.Error):
            continue
    for cand in candidates:
        if len(cand) == KEY_SIZE:
            return cand
    raise ValueError(
        f"VAULT_LOCAL_KEY must decode to {KEY_SIZE} bytes "
        f"(got candidate lengths {[len(c) for c in candidates]})"
    )


class LocalDevBackend(VaultClient):
    """File-backed, libsodium-encrypted development Vault backend.

    Args:
        store_path: Path to the encrypted store file. Created on first
            write; parent directory must already exist.
        key: 32-byte symmetric key used by
            :class:`nacl.secret.SecretBox`. Use :func:`from_env` to
            source the key from ``VAULT_LOCAL_KEY``.
    """

    backend: Backend = "local-dev"

    def __init__(self, *, store_path: Path, key: bytes) -> None:
        if len(key) != KEY_SIZE:
            raise ValueError(
                f"LocalDevBackend key must be exactly {KEY_SIZE} bytes"
            )
        self._store_path = store_path
        self._box = nacl.secret.SecretBox(key)
        # ``SecretBox`` itself is thread-safe for stateless operations,
        # but the file-load / file-write critical section is not, so we
        # serialise both with a single mutex.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        default_store: str = ".vault-local.json",
    ) -> "LocalDevBackend":
        """Build a :class:`LocalDevBackend` from environment variables.

        Reads ``VAULT_LOCAL_KEY`` (required, 32 bytes after decoding)
        and ``VAULT_LOCAL_STORE`` (optional, defaults to
        ``./.vault-local.json``). Plain-text / placeholder keys listed
        in :data:`_FORBIDDEN_KEYS` are rejected so a developer cannot
        accidentally run with a public key.
        """
        raw = env.get("VAULT_LOCAL_KEY", "")
        if raw.strip().lower() in _FORBIDDEN_KEYS:
            raise ValueError(
                "VAULT_LOCAL_KEY is unset or contains a known weak "
                "placeholder; refusing to start the local-dev Vault "
                "backend (plain-text rejected)."
            )
        key = _decode_key(raw)
        store_path = Path(env.get("VAULT_LOCAL_STORE", default_store))
        return cls(store_path=store_path, key=key)

    # ------------------------------------------------------------------
    # Encrypted-file primitives
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, dict[str, str]]:
        """Load and decrypt the on-disk store (or return ``{}``)."""
        if not self._store_path.exists():
            return {}
        envelope = json.loads(self._store_path.read_text(encoding="utf-8"))
        ciphertext_b64 = envelope.get("ciphertext", "")
        if not ciphertext_b64:
            return {}
        try:
            blob = base64.b64decode(ciphertext_b64)
            plaintext = self._box.decrypt(blob)
        except (nacl.exceptions.CryptoError, ValueError, binascii.Error) as exc:
            raise RuntimeError(
                f"local-dev Vault store at {self._store_path} is "
                f"unreadable; key mismatch or file corruption: {exc!s}"
            ) from exc
        loaded = json.loads(plaintext.decode("utf-8"))
        # Defensive cast: the file format SHOULD already be the right
        # shape, but a misconfigured tool could have hand-edited it.
        return {
            str(k): {str(kk): str(vv) for kk, vv in v.items()}
            for k, v in loaded.items()
        }

    def _save(self, store: Mapping[str, Mapping[str, str]]) -> None:
        """Encrypt *store* and atomically write it to disk."""
        plaintext = json.dumps(
            {k: dict(v) for k, v in store.items()},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        # ``encrypt`` produces an ``EncryptedMessage`` whose default
        # ``bytes()`` representation is ``nonce || ciphertext`` - that's
        # the value we store. Decryption needs the same bytes back.
        nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)
        encrypted = self._box.encrypt(plaintext, nonce)
        envelope = {
            "version": 1,
            "ciphertext": base64.b64encode(bytes(encrypted)).decode("ascii"),
        }
        # Write to a sibling temp file then rename, so a crash mid-write
        # never leaves behind a half-encrypted store.
        tmp = self._store_path.with_suffix(self._store_path.suffix + ".tmp")
        tmp.write_text(json.dumps(envelope), encoding="utf-8")
        os.replace(tmp, self._store_path)

    # ------------------------------------------------------------------
    # KV operations
    # ------------------------------------------------------------------

    def read(self, path: VaultPath) -> Mapping[str, str]:
        with self._lock:
            store = self._load()
            try:
                value = store[path.raw]
            except KeyError as exc:
                raise KeyError(path.raw) from exc
            # Return a defensive copy so callers cannot mutate the
            # in-memory cache.
            return dict(value)

    def write(self, path: VaultPath, data: Mapping[str, str]) -> None:
        if not isinstance(data, Mapping):
            raise TypeError("LocalDevBackend.write expected Mapping[str, str]")
        # Coerce all keys/values to ``str`` and reject obviously wrong
        # shapes (e.g. nested dicts) - KV-v2 storage is intentionally
        # flat so callers cannot smuggle structured plain-text payloads.
        flat: dict[str, str] = {}
        for k, v in data.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise TypeError(
                    "LocalDevBackend.write requires str keys and str values"
                )
            flat[k] = v
        with self._lock:
            store = self._load()
            store[path.raw] = flat
            self._save(store)

    def delete(self, path: VaultPath) -> None:
        with self._lock:
            store = self._load()
            if path.raw in store:
                del store[path.raw]
                self._save(store)
            # Otherwise: idempotent no-op (matches Hashicorp KV-v2).

    # ------------------------------------------------------------------
    # Rotation helpers
    # ------------------------------------------------------------------

    def rotate_ssh_key(
        self,
        runner_id: str,
        new_key: SshKey,
    ) -> RotationResult:
        active = VaultPath.parse(f"vault:ssh/runners/{runner_id}/active")
        previous = VaultPath.parse(f"vault:ssh/runners/{runner_id}/previous")
        with self._lock:
            store = self._load()
            current = store.get(active.raw)
            if current is not None:
                store[previous.raw] = dict(current)
            store[active.raw] = {
                "private_pem": new_key.private_pem,
                "public_pem": new_key.public_pem,
                "fingerprint": new_key.fingerprint,
            }
            self._save(store)
        return RotationResult(
            active_path=active,
            previous_path=previous if current is not None else None,
            rotated_at=datetime.now(timezone.utc),
        )

    def clear_previous_ssh_slot(self, runner_id: str) -> None:
        """Idempotent removal of the ``previous`` SSH slot.

        Called once the freshly rotated key has been validated against
        the remote runner host. ``delete`` is already idempotent for
        absent paths, so callers can drive this from a fire-and-forget
        post-validation hook.
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
        rotated_at = datetime.now(timezone.utc)
        overlap_until: datetime | None = None
        with self._lock:
            store = self._load()
            current = store.get(active.raw)
            if current is not None:
                overlap_until = rotated_at + _WEBHOOK_ROTATION_OVERLAP
                store[previous.raw] = {
                    **current,
                    "overlap_until": overlap_until.isoformat(),
                }
            store[active.raw] = {"secret": new_secret}
            self._save(store)
        return RotationResult(
            active_path=active,
            previous_path=previous if current is not None else None,
            rotated_at=rotated_at,
            overlap_until=overlap_until,
        )


__all__ = ["KEY_SIZE", "LocalDevBackend"]
