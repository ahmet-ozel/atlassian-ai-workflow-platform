"""Property test 9 — SSH Key Dual-Slot Rotation Safety.

Spec: ``platform-real-usage-gaps`` — Property 9.

**Validates: Requirements 8.2, 8.4, 8.7, 8.8**

Background
----------

SSH key rotation uses a dual-slot pattern in Vault:
``vault:ssh/runners/<runner_id>/active`` and
``vault:ssh/runners/<runner_id>/previous``. During the overlap window
(between ``rotate`` and ``finalize``), both slots hold valid keys so
the execution-runner-worker can fall back to the previous key if the
new active key hasn't been added to the target host's
``authorized_keys`` yet.

The :class:`SSHDualSlotConnector` in ``remote_ssh.py`` implements the
fallback logic: try active first, on ``AuthenticationException`` try
previous, emit audit if both fail.

Strategy
--------

We use Hypothesis to generate random rotation scenarios with a fake
Vault backend and a fake SSH client that accepts/rejects keys based on
a configurable ``authorized_keys`` set. The tests verify:

(a) **Overlap window**: After rotation but before finalize, both the
    active and previous keys are accepted by the connector (zero-downtime).
(b) **Finalize clears previous**: After ``finalize``, the previous slot
    is ``None`` — only the active key remains.
(c) **Faulty new key rescue**: When the new active key is not yet in
    ``authorized_keys`` (operator hasn't added it), the previous key
    acts as rescue and the connector succeeds via fallback.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping

from hypothesis import HealthCheck, given, settings, assume
from hypothesis import strategies as st

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap — expose vault_client and execution-runner-worker
# ---------------------------------------------------------------------------

_PLATFORM_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_VAULT_CLIENT_SRC: Final[Path] = (
    _PLATFORM_ROOT / "libs" / "vault_client" / "src"
)
_EXECUTION_RUNNER_ROOT: Final[Path] = (
    _PLATFORM_ROOT / "workers" / "execution-runner-worker"
)
_EXECUTION_RUNNER_SRC: Final[Path] = _EXECUTION_RUNNER_ROOT / "src"

for _p in (_VAULT_CLIENT_SRC, _EXECUTION_RUNNER_ROOT, _EXECUTION_RUNNER_SRC):
    _p_str = str(_p)
    if _p.is_dir() and _p_str not in sys.path:
        sys.path.insert(0, _p_str)

from vault_client.client import RotationResult, SshKey, VaultClient  # noqa: E402
from vault_client.path import VaultPath  # noqa: E402
from vault_client.ssh_keys import (  # noqa: E402
    _fingerprint,
    _make_ssh_key,
    finalize,
    generate_keypair,
    read_active,
    read_previous,
    rotate,
)


# ---------------------------------------------------------------------------
# Fake Vault Backend — in-memory dual-slot store
# ---------------------------------------------------------------------------


class FakeVaultBackend:
    """In-memory Vault backend that implements the VaultClient protocol.

    Stores secrets in a plain dict keyed by the VaultPath raw string.
    Implements ``rotate_ssh_key`` and ``clear_previous_ssh_slot`` with
    the same semantics as the real backends.
    """

    backend = "local-dev"

    def __init__(self) -> None:
        self._store: dict[str, dict[str, str]] = {}

    def read(self, path: VaultPath) -> Mapping[str, str]:
        if path.raw not in self._store:
            raise KeyError(f"no secret at {path.raw}")
        return self._store[path.raw]

    def write(self, path: VaultPath, data: Mapping[str, str]) -> None:
        self._store[path.raw] = dict(data)

    def delete(self, path: VaultPath) -> None:
        self._store.pop(path.raw, None)

    def rotate_ssh_key(
        self,
        runner_id: str,
        new_key: SshKey,
    ) -> RotationResult:
        from datetime import datetime, timezone

        active_path = VaultPath.parse(f"vault:ssh/runners/{runner_id}/active")
        previous_path = VaultPath.parse(
            f"vault:ssh/runners/{runner_id}/previous"
        )

        # Read current active (if any) to demote to previous
        current_active: dict[str, str] | None = None
        if active_path.raw in self._store:
            current_active = dict(self._store[active_path.raw])

        # Demote current active to previous
        if current_active is not None:
            self._store[previous_path.raw] = current_active
            result_previous_path: VaultPath | None = previous_path
        else:
            result_previous_path = None

        # Write new key to active
        self._store[active_path.raw] = {
            "private_pem": new_key.private_pem,
            "public_pem": new_key.public_pem,
            "fingerprint": new_key.fingerprint,
        }

        return RotationResult(
            active_path=active_path,
            previous_path=result_previous_path,
            rotated_at=datetime.now(timezone.utc),
            overlap_until=None,
        )

    def clear_previous_ssh_slot(self, runner_id: str) -> None:
        previous_path = VaultPath.parse(
            f"vault:ssh/runners/{runner_id}/previous"
        )
        self._store.pop(previous_path.raw, None)

    def rotate_webhook_secret(
        self,
        provider: str,
        dept_id: str,
        new_secret: str,
    ) -> RotationResult:
        raise NotImplementedError("not needed for SSH key rotation tests")


# ---------------------------------------------------------------------------
# Fake SSH Client — simulates authorized_keys acceptance
# ---------------------------------------------------------------------------


class FakeSSHClient:
    """Simulates SSH authentication against a set of authorized keys.

    The ``authorized_keys`` set contains fingerprints of keys that the
    target host accepts. If a connection attempt uses a key whose
    fingerprint is in the set, it succeeds; otherwise it raises
    ``AuthenticationException``.
    """

    def __init__(self, authorized_fingerprints: set[str]) -> None:
        self._authorized = authorized_fingerprints
        self.connection_attempts: list[tuple[str, str]] = []  # (slot, fingerprint)

    def try_connect(self, private_pem: str, fingerprint: str, slot: str) -> bool:
        """Attempt SSH connection. Returns True if accepted, False if rejected."""
        self.connection_attempts.append((slot, fingerprint))
        return fingerprint in self._authorized

    def add_authorized_key(self, fingerprint: str) -> None:
        """Simulate operator adding a key to authorized_keys."""
        self._authorized.add(fingerprint)

    def remove_authorized_key(self, fingerprint: str) -> None:
        """Simulate operator removing a key from authorized_keys."""
        self._authorized.discard(fingerprint)


# ---------------------------------------------------------------------------
# Fake SSHKeySlotReader — bridges FakeVaultBackend to the connector protocol
# ---------------------------------------------------------------------------


class FakeSlotReader:
    """Reads SSH key slots from the FakeVaultBackend.

    Implements the ``SSHKeySlotReader`` protocol expected by
    ``SSHDualSlotConnector``.
    """

    def __init__(self, vault: FakeVaultBackend) -> None:
        self._vault = vault

    def read_active_private_key(self, runner_id: str) -> str | None:
        active_path = VaultPath.parse(f"vault:ssh/runners/{runner_id}/active")
        try:
            data = self._vault.read(active_path)
            pem = data.get("private_pem", "")
            return pem if pem else None
        except KeyError:
            return None

    def read_previous_private_key(self, runner_id: str) -> str | None:
        previous_path = VaultPath.parse(
            f"vault:ssh/runners/{runner_id}/previous"
        )
        try:
            data = self._vault.read(previous_path)
            pem = data.get("private_pem", "")
            return pem if pem else None
        except KeyError:
            return None


# ---------------------------------------------------------------------------
# Fake Audit Writer
# ---------------------------------------------------------------------------


class FakeAuditWriter:
    """Records audit events in memory."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def write_audit(
        self,
        action: str,
        payload: dict[str, str | int | None],
    ) -> None:
        self.events.append({"action": action, "payload": dict(payload)})


# ---------------------------------------------------------------------------
# Dual-Slot Connector (simplified for property testing)
# ---------------------------------------------------------------------------


class DualSlotConnector:
    """Simplified dual-slot SSH connector for property testing.

    Mirrors the logic of ``SSHDualSlotConnector`` from ``remote_ssh.py``
    but uses the ``FakeSSHClient`` instead of real paramiko connections.
    This allows us to test the rotation safety properties without
    network I/O.
    """

    def __init__(
        self,
        vault: FakeVaultBackend,
        ssh_client: FakeSSHClient,
        audit: FakeAuditWriter,
    ) -> None:
        self._vault = vault
        self._ssh = ssh_client
        self._audit = audit

    def connect(self, runner_id: str) -> tuple[str, str]:
        """Try active slot, fall back to previous on auth failure.

        Returns:
            ``(private_key_pem, slot_used)`` tuple where ``slot_used``
            is ``"active"`` or ``"previous"``.

        Raises:
            BothSlotsFailedError: Both slots failed authentication.
        """
        # Try active slot
        active_path = VaultPath.parse(f"vault:ssh/runners/{runner_id}/active")
        try:
            active_data = self._vault.read(active_path)
            active_pem = active_data.get("private_pem", "")
            active_fp = active_data.get("fingerprint", "")
        except KeyError:
            active_pem = ""
            active_fp = ""

        if active_pem and self._ssh.try_connect(active_pem, active_fp, "active"):
            return active_pem, "active"

        # Active failed or empty — try previous slot
        previous_path = VaultPath.parse(
            f"vault:ssh/runners/{runner_id}/previous"
        )
        try:
            prev_data = self._vault.read(previous_path)
            prev_pem = prev_data.get("private_pem", "")
            prev_fp = prev_data.get("fingerprint", "")
        except KeyError:
            prev_pem = ""
            prev_fp = ""

        if prev_pem and self._ssh.try_connect(prev_pem, prev_fp, "previous"):
            return prev_pem, "previous"

        # Both failed
        self._audit.write_audit(
            action="ssh_key_both_slots_failed",
            payload={
                "runner_id": runner_id,
                "active_slot_error": "auth_failed" if active_pem else "empty",
                "previous_slot_error": "auth_failed" if prev_pem else "empty",
            },
        )
        raise BothSlotsFailedError(runner_id)


class BothSlotsFailedError(Exception):
    """Both active and previous SSH key slots failed authentication."""

    def __init__(self, runner_id: str) -> None:
        self.runner_id = runner_id
        super().__init__(
            f"SSH key authentication failed for runner={runner_id}: "
            "both active and previous slots failed"
        )


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: Runner ID strategy — ASCII alphanumeric + hyphens/underscores.
#: Must match VaultPath regex ``[a-zA-Z0-9/_-]+``.
_RUNNER_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789-_"
_runner_id_strategy = st.text(
    alphabet=_RUNNER_ID_ALPHABET,
    min_size=3,
    max_size=20,
).filter(lambda s: s[0].isalpha())


# ---------------------------------------------------------------------------
# Property 9a: Overlap Window — Both Slots Accepted During Rotation
# ---------------------------------------------------------------------------


class TestOverlapWindowBothSlotsAccepted:
    """**Validates: Requirements 8.2, 8.7, 8.8**

    After a rotation but before finalize, both the active and previous
    SSH keys are simultaneously valid. The connector can authenticate
    using either slot, ensuring zero-downtime during the rotation window.
    """

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(runner_id=_runner_id_strategy)
    def test_both_slots_accepted_during_overlap(
        self, runner_id: str
    ) -> None:
        """R8.2, R8.7: After rotation, both active and previous keys
        are accepted. The connector succeeds regardless of which key
        the target host has in authorized_keys."""

        vault = FakeVaultBackend()
        audit = FakeAuditWriter()

        # --- Initial key setup (simulate first-ever key) ---
        initial_priv, initial_pub = generate_keypair()
        initial_key = _make_ssh_key(initial_priv, initial_pub)
        vault.rotate_ssh_key(runner_id, initial_key)

        # Host has the initial key in authorized_keys
        ssh_client = FakeSSHClient({initial_key.fingerprint})
        connector = DualSlotConnector(vault, ssh_client, audit)

        # Verify initial connection works via active slot
        pem, slot = connector.connect(runner_id)
        assert slot == "active"
        assert pem == initial_key.private_pem

        # --- Perform rotation ---
        new_pub = rotate(vault, runner_id)
        # Derive the new key's fingerprint from the public key
        new_fp = _fingerprint(new_pub)

        # At this point:
        # - active slot = new key (NOT yet in authorized_keys)
        # - previous slot = initial key (still in authorized_keys)

        # The host still only has the OLD key — connector should
        # fall back to previous slot
        pem, slot = connector.connect(runner_id)
        assert slot == "previous", (
            "During overlap, when active key is not yet authorized, "
            "connector should fall back to previous slot"
        )
        assert pem == initial_key.private_pem

        # Now operator adds the new key to authorized_keys
        ssh_client.add_authorized_key(new_fp)

        # Both keys are now authorized — connector should prefer active
        pem, slot = connector.connect(runner_id)
        assert slot == "active", (
            "When both keys are authorized, connector should prefer active"
        )

        # Remove old key from authorized_keys (operator cleanup)
        ssh_client.remove_authorized_key(initial_key.fingerprint)

        # Only new key authorized — connector uses active
        pem, slot = connector.connect(runner_id)
        assert slot == "active"

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        runner_id=_runner_id_strategy,
        num_rotations=st.integers(min_value=2, max_value=5),
    )
    def test_multiple_rotations_maintain_overlap(
        self, runner_id: str, num_rotations: int
    ) -> None:
        """R8.2, R8.7: Multiple successive rotations always maintain
        the overlap invariant — previous slot holds the immediately
        prior key."""

        vault = FakeVaultBackend()
        audit = FakeAuditWriter()

        # Initial key
        priv, pub = generate_keypair()
        initial_key = _make_ssh_key(priv, pub)
        vault.rotate_ssh_key(runner_id, initial_key)

        authorized: set[str] = {initial_key.fingerprint}
        ssh_client = FakeSSHClient(authorized)
        connector = DualSlotConnector(vault, ssh_client, audit)

        prev_fingerprint = initial_key.fingerprint

        for i in range(num_rotations):
            # Rotate
            new_pub = rotate(vault, runner_id)
            new_fp = _fingerprint(new_pub)

            # Previous slot should hold the prior active key
            prev_key = read_previous(vault, runner_id)
            assert prev_key is not None, (
                f"After rotation #{i+1}, previous slot should not be empty"
            )
            assert prev_key.fingerprint == prev_fingerprint, (
                f"After rotation #{i+1}, previous slot should hold the "
                f"immediately prior key"
            )

            # Connection should still work (via previous slot since
            # new key isn't authorized yet)
            pem, slot = connector.connect(runner_id)
            assert slot == "previous", (
                f"Rotation #{i+1}: should fall back to previous"
            )

            # Operator adds new key
            authorized.add(new_fp)

            # Now active should be preferred
            pem, slot = connector.connect(runner_id)
            assert slot == "active"

            # Finalize to prepare for next rotation
            finalize(vault, runner_id)

            # Remove old key from authorized
            authorized.discard(prev_fingerprint)

            prev_fingerprint = new_fp


# ---------------------------------------------------------------------------
# Property 9b: Finalize Clears Previous Slot
# ---------------------------------------------------------------------------


class TestFinalizeClearsPreviousSlot:
    """**Validates: Requirements 8.4, 8.8**

    After ``finalize`` is called, the previous SSH key slot is cleared
    (null). Only the active key remains valid.
    """

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(runner_id=_runner_id_strategy)
    def test_finalize_nulls_previous_slot(self, runner_id: str) -> None:
        """R8.4: After finalize, read_previous returns None."""

        vault = FakeVaultBackend()

        # Initial key
        priv, pub = generate_keypair()
        initial_key = _make_ssh_key(priv, pub)
        vault.rotate_ssh_key(runner_id, initial_key)

        # Rotate — now previous slot has the initial key
        new_pub = rotate(vault, runner_id)

        # Verify previous is populated
        prev = read_previous(vault, runner_id)
        assert prev is not None, "Before finalize, previous should be populated"
        assert prev.fingerprint == initial_key.fingerprint

        # Finalize
        finalize(vault, runner_id)

        # Previous should now be None
        prev_after = read_previous(vault, runner_id)
        assert prev_after is None, (
            "After finalize, previous slot must be null"
        )

        # Active should still be valid
        active = read_active(vault, runner_id)
        assert active is not None
        assert active.fingerprint == _fingerprint(new_pub)

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(runner_id=_runner_id_strategy)
    def test_finalize_is_idempotent(self, runner_id: str) -> None:
        """R8.4: Calling finalize multiple times is safe (idempotent).
        The second call is a no-op."""

        vault = FakeVaultBackend()

        # Setup: initial + rotation
        priv, pub = generate_keypair()
        initial_key = _make_ssh_key(priv, pub)
        vault.rotate_ssh_key(runner_id, initial_key)
        rotate(vault, runner_id)

        # First finalize
        finalize(vault, runner_id)
        assert read_previous(vault, runner_id) is None

        # Second finalize — should not raise
        finalize(vault, runner_id)
        assert read_previous(vault, runner_id) is None

        # Active still intact
        assert read_active(vault, runner_id) is not None

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(runner_id=_runner_id_strategy)
    def test_after_finalize_only_active_key_works(
        self, runner_id: str
    ) -> None:
        """R8.4, R8.8: After finalize, only the active key can
        authenticate. If the active key is not in authorized_keys,
        the connector fails (no fallback available)."""

        vault = FakeVaultBackend()
        audit = FakeAuditWriter()

        # Initial key
        priv, pub = generate_keypair()
        initial_key = _make_ssh_key(priv, pub)
        vault.rotate_ssh_key(runner_id, initial_key)

        # Rotate
        new_pub = rotate(vault, runner_id)
        new_fp = _fingerprint(new_pub)

        # Operator adds new key to authorized_keys
        ssh_client = FakeSSHClient({new_fp})
        connector = DualSlotConnector(vault, ssh_client, audit)

        # Finalize — clears previous
        finalize(vault, runner_id)

        # Active key works
        pem, slot = connector.connect(runner_id)
        assert slot == "active"

        # Remove active key from authorized — should fail completely
        ssh_client.remove_authorized_key(new_fp)

        with pytest.raises(BothSlotsFailedError):
            connector.connect(runner_id)

        # Audit should record the failure
        assert len(audit.events) == 1
        assert audit.events[0]["action"] == "ssh_key_both_slots_failed"


# ---------------------------------------------------------------------------
# Property 9c: Faulty New Key — Previous Slot as Rescue
# ---------------------------------------------------------------------------


class TestFaultyNewKeyPreviousRescue:
    """**Validates: Requirements 8.7, 8.8**

    When the newly rotated active key is faulty (not yet added to
    authorized_keys, or corrupted), the previous slot acts as a rescue
    mechanism. The connector falls back to the previous key and
    succeeds without downtime.
    """

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(runner_id=_runner_id_strategy)
    def test_previous_slot_rescues_when_active_rejected(
        self, runner_id: str
    ) -> None:
        """R8.7: When the active key is rejected (Permission denied),
        the connector falls back to the previous key and succeeds."""

        vault = FakeVaultBackend()
        audit = FakeAuditWriter()

        # Initial key — this will become the "previous" after rotation
        priv, pub = generate_keypair()
        initial_key = _make_ssh_key(priv, pub)
        vault.rotate_ssh_key(runner_id, initial_key)

        # Host only has the initial key
        ssh_client = FakeSSHClient({initial_key.fingerprint})
        connector = DualSlotConnector(vault, ssh_client, audit)

        # Rotate — new key is NOT in authorized_keys
        new_pub = rotate(vault, runner_id)
        new_fp = _fingerprint(new_pub)

        # The new active key is rejected, but previous (initial) works
        pem, slot = connector.connect(runner_id)
        assert slot == "previous", (
            "When active key is rejected, connector must fall back to previous"
        )
        assert pem == initial_key.private_pem

        # No audit event — connection succeeded via fallback
        assert len(audit.events) == 0

    @settings(
        max_examples=100,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(runner_id=_runner_id_strategy)
    def test_both_slots_fail_emits_audit(self, runner_id: str) -> None:
        """R8.7, R8.8: When both active and previous keys are rejected,
        the connector emits ``ssh_key_both_slots_failed`` audit and
        raises BothSlotsFailedError."""

        vault = FakeVaultBackend()
        audit = FakeAuditWriter()

        # Initial key
        priv, pub = generate_keypair()
        initial_key = _make_ssh_key(priv, pub)
        vault.rotate_ssh_key(runner_id, initial_key)

        # Rotate
        new_pub = rotate(vault, runner_id)

        # Host has NEITHER key in authorized_keys
        ssh_client = FakeSSHClient(set())
        connector = DualSlotConnector(vault, ssh_client, audit)

        with pytest.raises(BothSlotsFailedError) as exc_info:
            connector.connect(runner_id)

        assert exc_info.value.runner_id == runner_id

        # Audit event emitted
        assert len(audit.events) == 1
        event = audit.events[0]
        assert event["action"] == "ssh_key_both_slots_failed"
        assert event["payload"]["runner_id"] == runner_id

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(
        runner_id=_runner_id_strategy,
        num_failed_attempts=st.integers(min_value=1, max_value=5),
    )
    def test_previous_slot_rescue_is_repeatable(
        self, runner_id: str, num_failed_attempts: int
    ) -> None:
        """R8.7: The previous slot rescue works repeatedly — multiple
        connection attempts during the overlap window all succeed via
        the previous slot when active is faulty."""

        vault = FakeVaultBackend()
        audit = FakeAuditWriter()

        # Initial key
        priv, pub = generate_keypair()
        initial_key = _make_ssh_key(priv, pub)
        vault.rotate_ssh_key(runner_id, initial_key)

        # Rotate — new key NOT authorized
        rotate(vault, runner_id)

        # Host only has old key
        ssh_client = FakeSSHClient({initial_key.fingerprint})
        connector = DualSlotConnector(vault, ssh_client, audit)

        # Multiple connection attempts all succeed via previous
        for i in range(num_failed_attempts):
            pem, slot = connector.connect(runner_id)
            assert slot == "previous", f"Attempt #{i+1} should use previous"
            assert pem == initial_key.private_pem

        # No audit events — all connections succeeded
        assert len(audit.events) == 0

    @settings(
        max_examples=50,
        deadline=5000,
        suppress_health_check=(HealthCheck.too_slow,),
    )
    @given(runner_id=_runner_id_strategy)
    def test_rescue_then_operator_fixes_then_active_works(
        self, runner_id: str
    ) -> None:
        """R8.7, R8.8: Full lifecycle — previous rescues during the
        gap, then operator adds new key, then active works, then
        finalize clears previous."""

        vault = FakeVaultBackend()
        audit = FakeAuditWriter()

        # Initial key
        priv, pub = generate_keypair()
        initial_key = _make_ssh_key(priv, pub)
        vault.rotate_ssh_key(runner_id, initial_key)

        # Rotate
        new_pub = rotate(vault, runner_id)
        new_fp = _fingerprint(new_pub)

        # Phase 1: Only old key authorized — previous rescues
        ssh_client = FakeSSHClient({initial_key.fingerprint})
        connector = DualSlotConnector(vault, ssh_client, audit)

        pem, slot = connector.connect(runner_id)
        assert slot == "previous"

        # Phase 2: Operator adds new key — active now works
        ssh_client.add_authorized_key(new_fp)
        pem, slot = connector.connect(runner_id)
        assert slot == "active"

        # Phase 3: Finalize — previous cleared
        finalize(vault, runner_id)
        assert read_previous(vault, runner_id) is None

        # Phase 4: Only active key works
        pem, slot = connector.connect(runner_id)
        assert slot == "active"

        # Phase 5: Remove old key — still works (only active needed)
        ssh_client.remove_authorized_key(initial_key.fingerprint)
        pem, slot = connector.connect(runner_id)
        assert slot == "active"
