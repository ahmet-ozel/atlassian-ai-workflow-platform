"""Webhook Secret Rotation Overlap Window.

Background
----------

Webhook secret rotation uses a dual-slot pattern in Vault:
``vault:webhooks/<provider>/<dept_id>`` (active/current) and
``vault:webhooks/<provider>/<dept_id>/previous`` (previous with
``overlap_until`` timestamp). During the overlap window (default 1h,
configurable via ``WEBHOOK_ROTATION_OVERLAP_S``), both secrets are
accepted for HMAC verification so the operator has time to update the
provider-side webhook configuration.

The :func:`~vault_client.webhook_hmac.verify_webhook_hmac` function
implements the dual-secret verification logic. The
:class:`WebhookRotationFinalizeWorkflow` Temporal cron auto-finalizes
expired overlap windows every 10 minutes.

Strategy
--------

We use Hypothesis to generate random rotation scenarios with a fake
Vault backend and a fake gateway (HMAC signer). The tests verify:

(a) **Overlap acceptance**: After rotation, requests signed with either
    the current or previous secret are accepted (zero-downtime).
(b) **Finalize restricts to current only**: After ``finalize``, only
    requests signed with the current secret are accepted.
(c) **Auto-finalize on overlap expiry**: When the overlap window
    expires, the ``WebhookRotationFinalizeWorkflow`` clears the
    previous slot, after which only the current secret is accepted.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Mapping

from hypothesis import HealthCheck, given, settings, assume
from hypothesis import strategies as st

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap - expose vault_client and automation-worker
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_VAULT_CLIENT_SRC: Final[Path] = (
    _PLATFORM_ROOT / "libs" / "vault_client" / "src"
)
_AUTOMATION_WORKER_SRC: Final[Path] = (
    _PLATFORM_ROOT / "workers" / "automation-worker" / "src"
)

for _p in (_VAULT_CLIENT_SRC, _AUTOMATION_WORKER_SRC):
    _p_str = str(_p)
    if _p.is_dir() and _p_str not in sys.path:
        sys.path.insert(0, _p_str)

from vault_client.client import RotationResult, VaultClient  # noqa: E402
from vault_client.path import VaultPath  # noqa: E402
from vault_client.webhook_secrets import (  # noqa: E402
    _get_overlap_seconds,
    finalize,
    generate_secret,
    rotate,
)
from vault_client.webhook_hmac import verify_webhook_hmac  # noqa: E402


# ---------------------------------------------------------------------------
# Fake Vault Backend - in-memory dual-slot store for webhook secrets
# ---------------------------------------------------------------------------


class FakeWebhookVault:
    """In-memory Vault backend implementing the VaultClient protocol.

    Stores secrets in a plain dict keyed by VaultPath.raw. Implements
    ``rotate_webhook_secret`` with the same semantics as the real
    backends: demotes current to previous with an ``overlap_until``
    timestamp, writes new secret to current.
    """

    backend = "local-dev"

    def __init__(self, overlap_seconds: int = 3600) -> None:
        self._store: dict[str, dict[str, str]] = {}
        self._overlap_seconds = overlap_seconds

    def read(self, path: VaultPath) -> Mapping[str, str]:
        if path.raw not in self._store:
            raise KeyError(f"no secret at {path.raw}")
        return self._store[path.raw]

    def write(self, path: VaultPath, data: Mapping[str, str]) -> None:
        self._store[path.raw] = dict(data)

    def delete(self, path: VaultPath) -> None:
        self._store.pop(path.raw, None)

    def rotate_webhook_secret(
        self,
        provider: str,
        dept_id: str,
        new_secret: str,
    ) -> RotationResult:
        """Dual-slot webhook secret rotation with overlap window."""
        now = datetime.now(timezone.utc)
        overlap_until = now + timedelta(seconds=self._overlap_seconds)

        active_path = VaultPath.parse(f"vault:webhooks/{provider}/{dept_id}")
        previous_path = VaultPath.parse(
            f"vault:webhooks/{provider}/{dept_id}/previous"
        )

        # Read current active (if any) to demote to previous
        current_secret: str | None = None
        if active_path.raw in self._store:
            current_data = self._store[active_path.raw]
            current_secret = current_data.get("secret")

        # Demote current to previous with overlap_until
        if current_secret:
            self._store[previous_path.raw] = {
                "secret": current_secret,
                "overlap_until": overlap_until.isoformat(),
            }

        # Write new secret to active
        self._store[active_path.raw] = {"secret": new_secret}

        return RotationResult(
            active_path=active_path,
            previous_path=previous_path if current_secret else None,
            rotated_at=now,
            overlap_until=overlap_until if current_secret else None,
        )

    def rotate_ssh_key(self, runner_id: str, new_key: Any) -> RotationResult:
        raise NotImplementedError("not needed for webhook secret tests")

    def clear_previous_ssh_slot(self, runner_id: str) -> None:
        raise NotImplementedError("not needed for webhook secret tests")

    def has_previous_slot(self, provider: str, dept_id: str) -> bool:
        """Check if a previous slot exists (helper for test assertions)."""
        previous_path = f"vault:webhooks/{provider}/{dept_id}/previous"
        return previous_path in self._store

    def set_overlap_until(
        self, provider: str, dept_id: str, overlap_until: datetime
    ) -> None:
        """Override the overlap_until timestamp for testing expiry scenarios."""
        previous_path = VaultPath.parse(
            f"vault:webhooks/{provider}/{dept_id}/previous"
        )
        if previous_path.raw in self._store:
            self._store[previous_path.raw]["overlap_until"] = (
                overlap_until.isoformat()
            )


# ---------------------------------------------------------------------------
# Fake Gateway - HMAC signature generator
# ---------------------------------------------------------------------------


class FakeGateway:
    """Simulates an Atlassian/Bitbucket webhook sender that signs payloads.

    Given a secret, produces the ``X-Hub-Signature`` header value
    (``sha256=<hex>``) that the webhook handler expects.
    """

    @staticmethod
    def sign(secret: str, body: bytes) -> str:
        """Compute HMAC-SHA256 signature in ``sha256=<hex>`` format."""
        digest = hmac_mod.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return f"sha256={digest}"


# ---------------------------------------------------------------------------
# Fake Auto-Finalize Logic (simulates WebhookRotationFinalizeWorkflow)
# ---------------------------------------------------------------------------


def auto_finalize_expired(
    vault: FakeWebhookVault,
    provider: str,
    dept_id: str,
    now: datetime,
) -> bool:
    """Simulate the WebhookRotationFinalizeWorkflow's per-entry logic.

    Checks if the overlap window has expired and finalizes if so.
    Returns True if finalization was performed, False otherwise.
    """
    previous_path = VaultPath.parse(
        f"vault:webhooks/{provider}/{dept_id}/previous"
    )
    try:
        data = vault.read(previous_path)
    except KeyError:
        return False

    raw_until = data.get("overlap_until")
    if not raw_until:
        return False

    try:
        overlap_until = datetime.fromisoformat(raw_until)
    except ValueError:
        return False

    if overlap_until.tzinfo is None:
        overlap_until = overlap_until.replace(tzinfo=timezone.utc)

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if overlap_until <= now:
        # Overlap expired - finalize
        finalize(vault, dept_id, provider)
        return True

    return False


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Department ID strategy - alphanumeric + hyphens, valid for VaultPath.
_DEPT_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-"
_dept_id_strategy = st.text(
    alphabet=_DEPT_ID_ALPHABET,
    min_size=3,
    max_size=20,
).filter(lambda s: s[0].isalpha())

#: Provider strategy - one of the three supported providers.
_provider_strategy = st.sampled_from(["jira", "bitbucket", "confluence"])

#: Request body strategy - random bytes simulating webhook payload.
_body_strategy = st.binary(min_size=10, max_size=500)


# ---------------------------------------------------------------------------
# Overlap Window - Both Secrets Accepted After Rotation
# ---------------------------------------------------------------------------


class TestOverlapWindowBothSecretsAccepted:
    """Both secrets are accepted during the overlap window.

    After a rotation but before finalize, requests signed with either
    the current or previous secret are accepted. This ensures
    zero-downtime during the overlap window while the operator updates
    the provider-side webhook configuration.
    """

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        provider=_provider_strategy,
        body=_body_strategy,
    )
    def test_current_secret_accepted_after_rotation(
        self, dept_id: str, provider: str, body: bytes
    ) -> None:
        """After rotation, a request signed with the NEW
        (current) secret is accepted."""

        vault = FakeWebhookVault()
        gateway = FakeGateway()

        # Setup initial secret
        initial_secret = generate_secret()
        vault.rotate_webhook_secret(provider, dept_id, initial_secret)

        # Perform rotation - new secret replaces current
        new_secret = rotate(vault, dept_id, provider)

        # Sign request with the NEW secret
        signature = gateway.sign(new_secret, body)
        now = datetime.now(timezone.utc)

        result = verify_webhook_hmac(vault, provider, dept_id, body, signature, now)
        assert result is True, (
            "After rotation, request signed with the new (current) "
            "secret must be accepted"
        )

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        provider=_provider_strategy,
        body=_body_strategy,
    )
    def test_previous_secret_accepted_during_overlap(
        self, dept_id: str, provider: str, body: bytes
    ) -> None:
        """After rotation, a request signed with the OLD
        (previous) secret is accepted during the overlap window."""

        vault = FakeWebhookVault()
        gateway = FakeGateway()

        # Setup initial secret
        initial_secret = generate_secret()
        vault.rotate_webhook_secret(provider, dept_id, initial_secret)

        # Perform rotation
        _new_secret = rotate(vault, dept_id, provider)

        # Sign request with the OLD secret (now in previous slot)
        signature = gateway.sign(initial_secret, body)
        now = datetime.now(timezone.utc)

        result = verify_webhook_hmac(vault, provider, dept_id, body, signature, now)
        assert result is True, (
            "During overlap window, request signed with the previous "
            "secret must still be accepted"
        )

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        provider=_provider_strategy,
        body=_body_strategy,
    )
    def test_invalid_secret_rejected_during_overlap(
        self, dept_id: str, provider: str, body: bytes
    ) -> None:
        """A request signed with a completely unrelated secret
        is rejected even during the overlap window."""

        vault = FakeWebhookVault()
        gateway = FakeGateway()

        # Setup initial secret and rotate
        initial_secret = generate_secret()
        vault.rotate_webhook_secret(provider, dept_id, initial_secret)
        _new_secret = rotate(vault, dept_id, provider)

        # Sign with a random unrelated secret
        unrelated_secret = generate_secret()
        signature = gateway.sign(unrelated_secret, body)
        now = datetime.now(timezone.utc)

        result = verify_webhook_hmac(vault, provider, dept_id, body, signature, now)
        assert result is False, (
            "A request signed with an unrelated secret must be rejected "
            "even during the overlap window"
        )

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        provider=_provider_strategy,
        body=_body_strategy,
        num_rotations=st.integers(min_value=2, max_value=4),
    )
    def test_multiple_rotations_only_last_two_accepted(
        self, dept_id: str, provider: str, body: bytes, num_rotations: int
    ) -> None:
        """After multiple rotations, only the current and
        immediately previous secrets are accepted - older secrets
        are discarded."""

        vault = FakeWebhookVault()
        gateway = FakeGateway()

        secrets_history: list[str] = []

        # Initial secret
        initial_secret = generate_secret()
        vault.rotate_webhook_secret(provider, dept_id, initial_secret)
        secrets_history.append(initial_secret)

        # Perform multiple rotations
        for _ in range(num_rotations):
            new_secret = rotate(vault, dept_id, provider)
            secrets_history.append(new_secret)

        now = datetime.now(timezone.utc)

        # Current (last) secret should be accepted
        sig_current = gateway.sign(secrets_history[-1], body)
        assert verify_webhook_hmac(
            vault, provider, dept_id, body, sig_current, now
        ) is True, "Current secret must be accepted"

        # Previous (second-to-last) secret should be accepted
        sig_previous = gateway.sign(secrets_history[-2], body)
        assert verify_webhook_hmac(
            vault, provider, dept_id, body, sig_previous, now
        ) is True, "Previous secret must be accepted during overlap"

        # Older secrets (before previous) should be rejected
        for i, old_secret in enumerate(secrets_history[:-2]):
            sig_old = gateway.sign(old_secret, body)
            assert verify_webhook_hmac(
                vault, provider, dept_id, body, sig_old, now
            ) is False, (
                f"Secret at index {i} (older than previous) must be rejected"
            )


# ---------------------------------------------------------------------------
# Finalize Restricts to Current Only
# ---------------------------------------------------------------------------


class TestFinalizeRestrictsToCurrentOnly:
    """Finalize restricts verification to the current secret.

    After ``finalize`` is called, the previous secret slot is cleared.
    Only requests signed with the current secret are accepted.
    """

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        provider=_provider_strategy,
        body=_body_strategy,
    )
    def test_finalize_clears_previous_slot(
        self, dept_id: str, provider: str, body: bytes
    ) -> None:
        """After finalize, the previous secret slot is empty."""

        vault = FakeWebhookVault()

        # Setup and rotate
        initial_secret = generate_secret()
        vault.rotate_webhook_secret(provider, dept_id, initial_secret)
        _new_secret = rotate(vault, dept_id, provider)

        # Verify previous slot exists before finalize
        assert vault.has_previous_slot(provider, dept_id), (
            "Before finalize, previous slot should exist"
        )

        # Finalize
        finalize(vault, dept_id, provider)

        # Previous slot should be cleared
        assert not vault.has_previous_slot(provider, dept_id), (
            "After finalize, previous slot must be cleared"
        )

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        provider=_provider_strategy,
        body=_body_strategy,
    )
    def test_only_current_accepted_after_finalize(
        self, dept_id: str, provider: str, body: bytes
    ) -> None:
        """After finalize, only the current secret is
        accepted; the previous secret is rejected."""

        vault = FakeWebhookVault()
        gateway = FakeGateway()

        # Setup and rotate
        initial_secret = generate_secret()
        vault.rotate_webhook_secret(provider, dept_id, initial_secret)
        new_secret = rotate(vault, dept_id, provider)

        # Finalize
        finalize(vault, dept_id, provider)

        now = datetime.now(timezone.utc)

        # Current secret accepted
        sig_current = gateway.sign(new_secret, body)
        assert verify_webhook_hmac(
            vault, provider, dept_id, body, sig_current, now
        ) is True, "After finalize, current secret must be accepted"

        # Previous secret rejected
        sig_previous = gateway.sign(initial_secret, body)
        assert verify_webhook_hmac(
            vault, provider, dept_id, body, sig_previous, now
        ) is False, "After finalize, previous secret must be rejected"

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        provider=_provider_strategy,
    )
    def test_finalize_is_idempotent(
        self, dept_id: str, provider: str
    ) -> None:
        """Calling finalize multiple times is safe (idempotent).
        The second call is a no-op."""

        vault = FakeWebhookVault()

        # Setup and rotate
        initial_secret = generate_secret()
        vault.rotate_webhook_secret(provider, dept_id, initial_secret)
        _new_secret = rotate(vault, dept_id, provider)

        # First finalize
        finalize(vault, dept_id, provider)
        assert not vault.has_previous_slot(provider, dept_id)

        # Second finalize - should not raise
        finalize(vault, dept_id, provider)
        assert not vault.has_previous_slot(provider, dept_id)


# ---------------------------------------------------------------------------
# Auto-Finalize on Overlap Expiry
# ---------------------------------------------------------------------------


class TestAutoFinalizeOnOverlapExpiry:
    """Auto-finalize clears previous secrets after overlap expiry.

    When the overlap window (``WEBHOOK_ROTATION_OVERLAP_S``, default
    3600s) expires, the ``WebhookRotationFinalizeWorkflow`` clears the
    previous slot. After auto-finalize, only the current secret is
    accepted.
    """

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        provider=_provider_strategy,
        body=_body_strategy,
    )
    def test_previous_rejected_after_overlap_expires(
        self, dept_id: str, provider: str, body: bytes
    ) -> None:
        """After the overlap window expires, the previous
        secret is no longer accepted even without explicit finalize -
        the verify function checks overlap_until."""

        vault = FakeWebhookVault()
        gateway = FakeGateway()

        # Setup and rotate
        initial_secret = generate_secret()
        vault.rotate_webhook_secret(provider, dept_id, initial_secret)
        new_secret = rotate(vault, dept_id, provider)

        # Simulate time passing beyond the overlap window
        expired_time = datetime.now(timezone.utc) + timedelta(seconds=3601)

        # Previous secret should be rejected (overlap expired)
        sig_previous = gateway.sign(initial_secret, body)
        result = verify_webhook_hmac(
            vault, provider, dept_id, body, sig_previous, expired_time
        )
        assert result is False, (
            "After overlap window expires, previous secret must be rejected"
        )

        # Current secret should still be accepted
        sig_current = gateway.sign(new_secret, body)
        result = verify_webhook_hmac(
            vault, provider, dept_id, body, sig_current, expired_time
        )
        assert result is True, (
            "Current secret must always be accepted regardless of time"
        )

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        provider=_provider_strategy,
        body=_body_strategy,
    )
    def test_auto_finalize_clears_previous_on_expiry(
        self, dept_id: str, provider: str, body: bytes
    ) -> None:
        """The auto-finalize workflow clears the previous
        slot when overlap_until has passed, after which only current
        is accepted."""

        vault = FakeWebhookVault()
        gateway = FakeGateway()

        # Setup and rotate
        initial_secret = generate_secret()
        vault.rotate_webhook_secret(provider, dept_id, initial_secret)
        new_secret = rotate(vault, dept_id, provider)

        # Verify previous slot exists
        assert vault.has_previous_slot(provider, dept_id)

        # Simulate time passing beyond overlap window
        expired_time = datetime.now(timezone.utc) + timedelta(seconds=3601)

        # Run auto-finalize logic (simulates WebhookRotationFinalizeWorkflow)
        finalized = auto_finalize_expired(vault, provider, dept_id, expired_time)
        assert finalized is True, (
            "Auto-finalize should trigger when overlap window has expired"
        )

        # Previous slot should be cleared
        assert not vault.has_previous_slot(provider, dept_id), (
            "After auto-finalize, previous slot must be cleared"
        )

        # Only current secret accepted
        sig_current = gateway.sign(new_secret, body)
        assert verify_webhook_hmac(
            vault, provider, dept_id, body, sig_current, expired_time
        ) is True

        sig_previous = gateway.sign(initial_secret, body)
        assert verify_webhook_hmac(
            vault, provider, dept_id, body, sig_previous, expired_time
        ) is False

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        provider=_provider_strategy,
        body=_body_strategy,
        elapsed_fraction=st.floats(min_value=0.01, max_value=0.99),
    )
    def test_auto_finalize_does_not_trigger_before_expiry(
        self,
        dept_id: str,
        provider: str,
        body: bytes,
        elapsed_fraction: float,
    ) -> None:
        """Auto-finalize does NOT trigger while the overlap
        window is still active. Both secrets remain accepted."""

        vault = FakeWebhookVault()
        gateway = FakeGateway()

        # Setup and rotate
        initial_secret = generate_secret()
        vault.rotate_webhook_secret(provider, dept_id, initial_secret)
        new_secret = rotate(vault, dept_id, provider)

        # Time within the overlap window (fraction of 3600s)
        within_window_time = datetime.now(timezone.utc) + timedelta(
            seconds=int(3600 * elapsed_fraction)
        )

        # Auto-finalize should NOT trigger
        finalized = auto_finalize_expired(
            vault, provider, dept_id, within_window_time
        )
        assert finalized is False, (
            "Auto-finalize must not trigger while overlap window is active"
        )

        # Previous slot still exists
        assert vault.has_previous_slot(provider, dept_id)

        # Both secrets still accepted
        sig_current = gateway.sign(new_secret, body)
        assert verify_webhook_hmac(
            vault, provider, dept_id, body, sig_current, within_window_time
        ) is True

        sig_previous = gateway.sign(initial_secret, body)
        assert verify_webhook_hmac(
            vault, provider, dept_id, body, sig_previous, within_window_time
        ) is True

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        provider=_provider_strategy,
    )
    def test_auto_finalize_is_idempotent(
        self, dept_id: str, provider: str
    ) -> None:
        """Running auto-finalize multiple times after expiry is
        safe - subsequent calls are no-ops."""

        vault = FakeWebhookVault()

        # Setup and rotate
        initial_secret = generate_secret()
        vault.rotate_webhook_secret(provider, dept_id, initial_secret)
        _new_secret = rotate(vault, dept_id, provider)

        # Expire the overlap window
        expired_time = datetime.now(timezone.utc) + timedelta(seconds=3601)

        # First auto-finalize
        result1 = auto_finalize_expired(vault, provider, dept_id, expired_time)
        assert result1 is True

        # Second auto-finalize - should be no-op (previous already cleared)
        result2 = auto_finalize_expired(vault, provider, dept_id, expired_time)
        assert result2 is False, (
            "Second auto-finalize should be no-op (previous already cleared)"
        )

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        dept_id=_dept_id_strategy,
        provider=_provider_strategy,
        body=_body_strategy,
    )
    def test_full_lifecycle_rotate_overlap_autofinalize(
        self, dept_id: str, provider: str, body: bytes
    ) -> None:
        """Full lifecycle - rotate → overlap window
        (both accepted) → auto-finalize → only current accepted."""

        vault = FakeWebhookVault()
        gateway = FakeGateway()

        # Phase 1: Initial setup
        initial_secret = generate_secret()
        vault.rotate_webhook_secret(provider, dept_id, initial_secret)

        # Phase 2: Rotate
        new_secret = rotate(vault, dept_id, provider)

        # Phase 3: During overlap - both accepted
        during_overlap = datetime.now(timezone.utc) + timedelta(seconds=1800)

        sig_current = gateway.sign(new_secret, body)
        sig_previous = gateway.sign(initial_secret, body)

        assert verify_webhook_hmac(
            vault, provider, dept_id, body, sig_current, during_overlap
        ) is True
        assert verify_webhook_hmac(
            vault, provider, dept_id, body, sig_previous, during_overlap
        ) is True

        # Phase 4: After overlap expires - auto-finalize triggers
        after_overlap = datetime.now(timezone.utc) + timedelta(seconds=3601)
        auto_finalize_expired(vault, provider, dept_id, after_overlap)

        # Phase 5: Only current accepted
        assert verify_webhook_hmac(
            vault, provider, dept_id, body, sig_current, after_overlap
        ) is True
        assert verify_webhook_hmac(
            vault, provider, dept_id, body, sig_previous, after_overlap
        ) is False
