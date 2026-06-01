"""Webhook HMAC verification with rotation overlap support.

Implements the R6.8 acceptance criterion: an Atlassian webhook signed
under either the *current* or the *previous* per-department secret
SHALL verify successfully for one hour after a rotation; once the
overlap window expires, only the new secret is accepted.

The helper is shared across the Jira, Bitbucket, and Confluence
webhook handlers in ``automation-service``; it intentionally lives in
``vault_client`` (rather than ``services/automation-service/...``)
because the storage layout — ``vault:webhooks/<provider>/<dept_id>``
plus its sibling ``.../previous`` slot with an embedded
``overlap_until`` ISO timestamp — is owned by this package's
:class:`~vault_client.client.VaultClient` rotation helpers.

Overlap window semantics
------------------------

* The active secret is read from
  ``vault:webhooks/<provider>/<dept_id>``.
* The previous secret (if any) is read from
  ``vault:webhooks/<provider>/<dept_id>/previous``. That payload
  carries an ``overlap_until`` ISO-8601 timestamp set by
  :meth:`VaultClient.rotate_webhook_secret` to ``rotated_at + 1h``.
* On verification:

  1. The active secret is tried first; constant-time match → ``True``.
  2. If a previous secret exists *and* ``now < overlap_until``, it is
     tried second; constant-time match → ``True``.
  3. Otherwise, ``False``.

Both candidate secrets are *always* attempted to keep the function's
running time independent of which secret happens to match — callers
that race on signature checks would otherwise leak which slot a
particular request was signed against.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from typing import Mapping

from .client import VaultClient
from .path import VaultPath

_ALGORITHM_PREFIX = "sha256="

# Allowed providers — kept in lock-step with
# :meth:`VaultClient.rotate_webhook_secret` so an unsupported value
# fails fast instead of silently mis-routing the lookup.
_ALLOWED_PROVIDERS: frozenset[str] = frozenset({"jira", "bitbucket", "confluence"})


def _signature_matches(secret: bytes, body: bytes, signature_header: str) -> bool:
    """Constant-time HMAC-SHA256 check against an ``X-Hub-Signature`` header.

    Returns ``False`` for malformed headers (missing prefix, empty
    digest) without raising — webhook handlers translate ``False`` to
    HTTP 401, while exceptions would surface as 500s.
    """
    if not signature_header or not signature_header.startswith(_ALGORITHM_PREFIX):
        return False
    received_hex = signature_header[len(_ALGORITHM_PREFIX):]
    if not received_hex:
        return False
    expected_hex = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_hex, received_hex)


def _coerce_secret(value: str) -> bytes:
    """Return the raw ``bytes`` form of a secret stored as ``str`` in Vault."""
    return value.encode("utf-8")


def _parse_overlap_until(payload: Mapping[str, str]) -> datetime | None:
    """Decode the ``overlap_until`` ISO-8601 stamp set during rotation.

    A missing or unparsable stamp is treated as "no longer valid" — the
    previous-slot fallback path is gated on a present, well-formed
    expiry, never on best-effort defaults.
    """
    raw = payload.get("overlap_until")
    if not raw:
        return None
    try:
        # ``fromisoformat`` accepts the output of ``datetime.isoformat()``
        # used by :meth:`VaultClient.rotate_webhook_secret`, including
        # the ``+00:00`` UTC offset.
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _ensure_aware(value: datetime) -> datetime:
    """Promote a naive datetime to UTC so comparisons are total."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def verify_webhook_hmac(
    vault: VaultClient,
    provider: str,
    dept_id: str,
    body: bytes,
    signature: str,
    now: datetime,
) -> bool:
    """Validate an Atlassian webhook signature with rotation overlap.

    Args:
        vault: The :class:`VaultClient` used to fetch the active and
            (optional) previous webhook secrets.
        provider: ``"jira"``, ``"bitbucket"`` or ``"confluence"``.
        dept_id: The department identifier; combined with *provider*
            to address ``vault:webhooks/<provider>/<dept_id>``.
        body: Raw HTTP request body bytes, exactly as received.
        signature: The ``X-Hub-Signature`` (or equivalent) header value
            in ``sha256=<hex>`` form.
        now: Current time used to evaluate the rotation overlap
            window. Pass ``datetime.now(timezone.utc)`` in production;
            tests pass a fixed instant for determinism.

    Returns:
        ``True`` if *signature* matches the active secret, **or** if it
        matches the previous secret while the rotation overlap window
        is still open. ``False`` in every other case (no secret at all,
        signature mismatch, expired overlap, malformed header).

    Raises:
        ValueError: If *provider* is not one of the supported Atlassian
            webhook sources. The caller's webhook handler turns this
            into an HTTP 400 rather than a generic 500.
    """
    if provider not in _ALLOWED_PROVIDERS:
        raise ValueError(
            f"unsupported webhook provider {provider!r}; "
            f"expected one of {sorted(_ALLOWED_PROVIDERS)}"
        )

    active_path = VaultPath.parse(f"vault:webhooks/{provider}/{dept_id}")
    previous_path = VaultPath.parse(
        f"vault:webhooks/{provider}/{dept_id}/previous"
    )

    # Always evaluate both candidates so the function's runtime does
    # not branch on which secret happens to match.
    active_match = False
    try:
        active_payload = vault.read(active_path)
    except KeyError:
        active_payload = None
    if active_payload is not None and "secret" in active_payload:
        active_match = _signature_matches(
            _coerce_secret(active_payload["secret"]),
            body,
            signature,
        )

    previous_match = False
    try:
        previous_payload = vault.read(previous_path)
    except KeyError:
        previous_payload = None
    if previous_payload is not None and "secret" in previous_payload:
        overlap_until = _parse_overlap_until(previous_payload)
        if overlap_until is not None and _ensure_aware(now) < _ensure_aware(
            overlap_until
        ):
            previous_match = _signature_matches(
                _coerce_secret(previous_payload["secret"]),
                body,
                signature,
            )

    return active_match or previous_match


__all__ = ["verify_webhook_hmac"]
