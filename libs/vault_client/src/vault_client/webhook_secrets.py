"""Webhook secret rotation helper — dual-slot management with overlap window.

Implements the R9.2 / R9.3 acceptance criteria: generate a
cryptographically random 32-byte HMAC secret and orchestrate the
``secret_current`` / ``secret_previous`` dual-slot rotation pattern
through the :class:`~vault_client.client.VaultClient` protocol.

The rotation lifecycle is:

1. **rotate(vault, dept_id, provider)** — generate a fresh 32-byte
   secret, demote the current ``secret_current`` to
   ``secret_previous`` (with an ``overlap_until`` timestamp), write
   the new secret to ``secret_current``, update ``rotated_at``, and
   return the new secret (so the operator can paste it into the
   Atlassian/Bitbucket webhook configuration UI).
2. **finalize(vault, dept_id, provider)** — once the operator has
   updated the provider-side webhook secret, clear the
   ``secret_previous`` slot so only the new secret is accepted.

Between steps 1 and 2, the automation-service webhook HMAC verifier
(:func:`~vault_client.webhook_hmac.verify_webhook_hmac`) accepts
signatures computed with either secret, providing a zero-downtime
overlap window (default 1 hour, configurable via
``WEBHOOK_ROTATION_OVERLAP_S``).

Secret generation
-----------------

Uses :func:`secrets.token_hex` to produce 32 cryptographically random
bytes (64 hex characters). The hex encoding is chosen for
compatibility with Atlassian webhook secret configuration fields which
expect printable ASCII strings.

Vault path layout
-----------------

* Active secret: ``vault:webhooks/<provider>/<dept_id>``
  — payload: ``{"secret": "<hex>"}``
* Previous secret: ``vault:webhooks/<provider>/<dept_id>/previous``
  — payload: ``{"secret": "<hex>", "overlap_until": "<ISO-8601>"}``
* Rotation metadata: ``vault:webhooks/<provider>/<dept_id>/meta``
  — payload: ``{"rotated_at": "<ISO-8601>"}``
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone

from .client import VaultClient
from .path import VaultPath

#: Default overlap window in seconds (1 hour). Can be overridden via
#: the ``WEBHOOK_ROTATION_OVERLAP_S`` environment variable.
_DEFAULT_OVERLAP_S: int = 3600

#: Allowed provider values — kept in sync with
#: :data:`webhook_hmac._ALLOWED_PROVIDERS`.
_ALLOWED_PROVIDERS: frozenset[str] = frozenset({"jira", "bitbucket", "confluence"})


def _get_overlap_seconds() -> int:
    """Read the overlap window duration from env or use the default."""
    raw = os.environ.get("WEBHOOK_ROTATION_OVERLAP_S")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            pass
    return _DEFAULT_OVERLAP_S


def _validate_provider(provider: str) -> None:
    """Raise ValueError if provider is not a supported webhook source."""
    if provider not in _ALLOWED_PROVIDERS:
        raise ValueError(
            f"unsupported webhook provider {provider!r}; "
            f"expected one of {sorted(_ALLOWED_PROVIDERS)}"
        )


def generate_secret() -> str:
    """Generate a cryptographically random 32-byte webhook secret.

    Returns:
        A 64-character hex string representing 32 random bytes,
        suitable for use as an HMAC-SHA256 webhook signing secret.
    """
    return secrets.token_hex(32)


def rotate(vault: VaultClient, dept_id: str, provider: str) -> str:
    """Rotate the webhook secret for a department × provider pair.

    Performs the dual-slot rotation:

    1. Generates a fresh 32-byte random secret.
    2. Demotes the current ``secret_current`` to ``secret_previous``
       via the :meth:`VaultClient.rotate_webhook_secret` protocol
       method, which sets the ``overlap_until`` timestamp.
    3. Writes the new secret to ``secret_current`` and updates
       ``rotated_at``.

    Args:
        vault: A :class:`VaultClient` instance (any backend).
        dept_id: The department identifier.
        provider: One of ``"jira"``, ``"bitbucket"``, or
            ``"confluence"``.

    Returns:
        The new secret as a hex string. The operator must configure
        this in the Atlassian/Bitbucket webhook UI before calling
        :func:`finalize`.

    Raises:
        ValueError: If *provider* is not a supported webhook source.
    """
    _validate_provider(provider)

    new_secret = generate_secret()

    # Delegate the slot demotion to the VaultClient protocol method
    # which handles active → previous promotion with overlap_until.
    vault.rotate_webhook_secret(provider, dept_id, new_secret)

    # Write rotated_at timestamp to a metadata path so the API can
    # report when the last rotation occurred.
    meta_path = VaultPath.parse(f"vault:webhooks/{provider}/{dept_id}/meta")
    vault.write(
        meta_path,
        {"rotated_at": datetime.now(timezone.utc).isoformat()},
    )

    return new_secret


def finalize(vault: VaultClient, dept_id: str, provider: str) -> None:
    """Finalize a rotation by clearing the previous webhook secret slot.

    Called after the operator has updated the provider-side webhook
    configuration with the new secret. After this call, only the
    ``secret_current`` slot contains a valid secret; the
    ``secret_previous`` slot is empty and HMAC verification will only
    accept signatures computed with the current secret.

    Args:
        vault: A :class:`VaultClient` instance (any backend).
        dept_id: The department identifier.
        provider: One of ``"jira"``, ``"bitbucket"``, or
            ``"confluence"``.

    Raises:
        ValueError: If *provider* is not a supported webhook source.
    """
    _validate_provider(provider)

    previous_path = VaultPath.parse(
        f"vault:webhooks/{provider}/{dept_id}/previous"
    )
    vault.delete(previous_path)


def read_rotation_meta(
    vault: VaultClient, dept_id: str, provider: str
) -> datetime | None:
    """Read the ``rotated_at`` timestamp for a dept × provider pair.

    Returns the UTC datetime of the last successful rotation, used by
    the admin API to populate the ``last_rotated_at`` field in the
    ``GET /admin/security/webhooks`` response.

    Args:
        vault: A :class:`VaultClient` instance (any backend).
        dept_id: The department identifier.
        provider: One of ``"jira"``, ``"bitbucket"``, or
            ``"confluence"``.

    Returns:
        The UTC datetime of the last rotation, or ``None`` if no
        rotation has ever been performed.

    Raises:
        ValueError: If *provider* is not a supported webhook source.
    """
    _validate_provider(provider)

    meta_path = VaultPath.parse(f"vault:webhooks/{provider}/{dept_id}/meta")
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


def read_overlap_remaining(
    vault: VaultClient, dept_id: str, provider: str
) -> int | None:
    """Return the remaining overlap window in seconds, or ``None``.

    Used by the admin API to populate the
    ``overlap_window_remaining_s`` field. Returns ``None`` if no
    previous secret exists (no active overlap window).

    Args:
        vault: A :class:`VaultClient` instance (any backend).
        dept_id: The department identifier.
        provider: One of ``"jira"``, ``"bitbucket"``, or
            ``"confluence"``.

    Returns:
        Remaining seconds in the overlap window (≥ 0), or ``None``
        if no overlap is active.

    Raises:
        ValueError: If *provider* is not a supported webhook source.
    """
    _validate_provider(provider)

    previous_path = VaultPath.parse(
        f"vault:webhooks/{provider}/{dept_id}/previous"
    )
    try:
        data = vault.read(previous_path)
    except KeyError:
        return None

    raw_until = data.get("overlap_until")
    if not raw_until:
        return None

    try:
        overlap_until = datetime.fromisoformat(raw_until)
    except ValueError:
        return None

    now = datetime.now(timezone.utc)
    if overlap_until.tzinfo is None:
        overlap_until = overlap_until.replace(tzinfo=timezone.utc)

    remaining = (overlap_until - now).total_seconds()
    return max(0, int(remaining))


__all__ = [
    "finalize",
    "generate_secret",
    "read_overlap_remaining",
    "read_rotation_meta",
    "rotate",
]
