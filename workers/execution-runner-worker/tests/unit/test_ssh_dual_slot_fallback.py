"""Unit tests for the SSH dual-slot key fallback logic.

Spec: ``platform-real-usage-gaps`` Requirement 8.7 — task 8.3.

Tests the :class:`SSHDualSlotConnector` in
:mod:`src.runners.remote_ssh` which implements the active → previous
slot fallback during SSH key rotation windows.

Scenarios covered:

1. Active slot succeeds → returns active key, no fallback.
2. Active slot auth fails, previous slot succeeds → returns previous key.
3. Active slot auth fails, previous slot empty → BothSlotsFailedError + audit.
4. Active slot auth fails, previous slot auth fails → BothSlotsFailedError + audit.
5. Active slot empty, previous slot succeeds → returns previous key.
6. Active slot empty, previous slot empty → BothSlotsFailedError + audit.
7. Active slot non-auth error → raises immediately, no fallback.
8. Audit writer is called with correct payload on both-slots failure.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.runners.remote_ssh import (
    BothSlotsFailedError,
    SSHDualSlotConnector,
    SSHDualSlotResult,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class FakeSlotReader:
    """Fake SSHKeySlotReader for testing."""

    def __init__(
        self,
        active_key: str | None = None,
        previous_key: str | None = None,
    ) -> None:
        self._active_key = active_key
        self._previous_key = previous_key

    def read_active_private_key(self, runner_id: str) -> str | None:
        return self._active_key

    def read_previous_private_key(self, runner_id: str) -> str | None:
        return self._previous_key


class FakeAuditWriter:
    """Fake AuditWriter that records calls."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def write_audit(
        self,
        action: str,
        payload: dict[str, str | int | None],
    ) -> None:
        self.events.append((action, payload))


FAKE_ACTIVE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nactive-key-material\n-----END OPENSSH PRIVATE KEY-----"
FAKE_PREVIOUS_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nprevious-key-material\n-----END OPENSSH PRIVATE KEY-----"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSSHDualSlotConnector:
    """Tests for SSHDualSlotConnector.connect()."""

    @patch("src.runners.remote_ssh._try_connect_with_key")
    def test_active_slot_succeeds(self, mock_connect: MagicMock) -> None:
        """Active slot connects successfully — no fallback needed."""
        mock_connect.return_value = None  # success

        reader = FakeSlotReader(active_key=FAKE_ACTIVE_KEY, previous_key=FAKE_PREVIOUS_KEY)
        audit = FakeAuditWriter()
        connector = SSHDualSlotConnector(slot_reader=reader, audit_writer=audit)

        result = connector.connect(
            runner_id="runner-1",
            host="10.0.0.5",
            port=22,
            user="ai-runner",
        )

        assert result == SSHDualSlotResult(
            private_key=FAKE_ACTIVE_KEY,
            slot_used="active",
        )
        # Only one connection attempt (active slot)
        mock_connect.assert_called_once_with(
            "10.0.0.5", 22, "ai-runner", FAKE_ACTIVE_KEY, 30.0
        )
        # No audit events
        assert len(audit.events) == 0

    @patch("src.runners.remote_ssh._try_connect_with_key")
    def test_active_auth_fails_previous_succeeds(
        self, mock_connect: MagicMock
    ) -> None:
        """Active slot auth fails, previous slot succeeds — fallback works."""
        import paramiko

        def side_effect(host, port, user, key, timeout):
            if key == FAKE_ACTIVE_KEY:
                raise paramiko.AuthenticationException("Permission denied")
            # Previous key succeeds
            return None

        mock_connect.side_effect = side_effect

        reader = FakeSlotReader(active_key=FAKE_ACTIVE_KEY, previous_key=FAKE_PREVIOUS_KEY)
        audit = FakeAuditWriter()
        connector = SSHDualSlotConnector(slot_reader=reader, audit_writer=audit)

        result = connector.connect(
            runner_id="runner-1",
            host="10.0.0.5",
            port=22,
            user="ai-runner",
        )

        assert result == SSHDualSlotResult(
            private_key=FAKE_PREVIOUS_KEY,
            slot_used="previous",
        )
        # Two connection attempts
        assert mock_connect.call_count == 2
        # No audit events (one slot worked)
        assert len(audit.events) == 0

    @patch("src.runners.remote_ssh._try_connect_with_key")
    def test_active_auth_fails_previous_empty(
        self, mock_connect: MagicMock
    ) -> None:
        """Active slot auth fails, previous slot empty → BothSlotsFailedError."""
        import paramiko

        mock_connect.side_effect = paramiko.AuthenticationException(
            "Permission denied"
        )

        reader = FakeSlotReader(active_key=FAKE_ACTIVE_KEY, previous_key=None)
        audit = FakeAuditWriter()
        connector = SSHDualSlotConnector(slot_reader=reader, audit_writer=audit)

        with pytest.raises(BothSlotsFailedError) as exc_info:
            connector.connect(
                runner_id="runner-1",
                host="10.0.0.5",
                port=22,
                user="ai-runner",
            )

        assert exc_info.value.runner_id == "runner-1"
        assert "Permission denied" in exc_info.value.active_error
        assert exc_info.value.previous_error is None

        # Audit event emitted
        assert len(audit.events) == 1
        action, payload = audit.events[0]
        assert action == "ssh_key_both_slots_failed"
        assert payload["runner_id"] == "runner-1"
        assert payload["host"] == "10.0.0.5"
        assert payload["port"] == 22

    @patch("src.runners.remote_ssh._try_connect_with_key")
    def test_both_slots_auth_fail(self, mock_connect: MagicMock) -> None:
        """Both active and previous slots fail auth → BothSlotsFailedError."""
        import paramiko

        mock_connect.side_effect = paramiko.AuthenticationException(
            "Permission denied"
        )

        reader = FakeSlotReader(active_key=FAKE_ACTIVE_KEY, previous_key=FAKE_PREVIOUS_KEY)
        audit = FakeAuditWriter()
        connector = SSHDualSlotConnector(slot_reader=reader, audit_writer=audit)

        with pytest.raises(BothSlotsFailedError) as exc_info:
            connector.connect(
                runner_id="runner-1",
                host="10.0.0.5",
                port=22,
                user="ai-runner",
            )

        assert exc_info.value.runner_id == "runner-1"
        assert exc_info.value.active_error is not None
        assert exc_info.value.previous_error is not None

        # Audit event emitted
        assert len(audit.events) == 1
        action, payload = audit.events[0]
        assert action == "ssh_key_both_slots_failed"
        assert payload["active_slot_error"] is not None
        assert payload["previous_slot_error"] is not None

    @patch("src.runners.remote_ssh._try_connect_with_key")
    def test_active_empty_previous_succeeds(
        self, mock_connect: MagicMock
    ) -> None:
        """Active slot empty, previous slot succeeds → uses previous."""
        mock_connect.return_value = None  # success

        reader = FakeSlotReader(active_key=None, previous_key=FAKE_PREVIOUS_KEY)
        audit = FakeAuditWriter()
        connector = SSHDualSlotConnector(slot_reader=reader, audit_writer=audit)

        result = connector.connect(
            runner_id="runner-1",
            host="10.0.0.5",
            port=22,
            user="ai-runner",
        )

        assert result == SSHDualSlotResult(
            private_key=FAKE_PREVIOUS_KEY,
            slot_used="previous",
        )
        assert len(audit.events) == 0

    @patch("src.runners.remote_ssh._try_connect_with_key")
    def test_both_slots_empty(self, mock_connect: MagicMock) -> None:
        """Both slots empty → BothSlotsFailedError + audit."""
        reader = FakeSlotReader(active_key=None, previous_key=None)
        audit = FakeAuditWriter()
        connector = SSHDualSlotConnector(slot_reader=reader, audit_writer=audit)

        with pytest.raises(BothSlotsFailedError) as exc_info:
            connector.connect(
                runner_id="runner-1",
                host="10.0.0.5",
                port=22,
                user="ai-runner",
            )

        assert exc_info.value.runner_id == "runner-1"
        assert "empty or missing" in exc_info.value.active_error

        # Audit event emitted
        assert len(audit.events) == 1
        assert audit.events[0][0] == "ssh_key_both_slots_failed"

    @patch("src.runners.remote_ssh._try_connect_with_key")
    def test_active_non_auth_error_no_fallback(
        self, mock_connect: MagicMock
    ) -> None:
        """Non-auth error on active slot → raises immediately, no fallback."""
        mock_connect.side_effect = OSError("Connection refused")

        reader = FakeSlotReader(active_key=FAKE_ACTIVE_KEY, previous_key=FAKE_PREVIOUS_KEY)
        audit = FakeAuditWriter()
        connector = SSHDualSlotConnector(slot_reader=reader, audit_writer=audit)

        with pytest.raises(OSError, match="Connection refused"):
            connector.connect(
                runner_id="runner-1",
                host="10.0.0.5",
                port=22,
                user="ai-runner",
            )

        # Only one attempt — no fallback for non-auth errors
        mock_connect.assert_called_once()
        # No audit events (not a both-slots-failed scenario)
        assert len(audit.events) == 0

    @patch("src.runners.remote_ssh._try_connect_with_key")
    def test_audit_payload_structure(self, mock_connect: MagicMock) -> None:
        """Verify the audit payload contains all required fields."""
        import paramiko

        mock_connect.side_effect = paramiko.AuthenticationException(
            "Permission denied (publickey)"
        )

        reader = FakeSlotReader(active_key=FAKE_ACTIVE_KEY, previous_key=FAKE_PREVIOUS_KEY)
        audit = FakeAuditWriter()
        connector = SSHDualSlotConnector(slot_reader=reader, audit_writer=audit)

        with pytest.raises(BothSlotsFailedError):
            connector.connect(
                runner_id="ssh-runner-prod",
                host="192.168.1.100",
                port=2222,
                user="deploy",
                timeout=15.0,
            )

        assert len(audit.events) == 1
        action, payload = audit.events[0]
        assert action == "ssh_key_both_slots_failed"
        assert payload["runner_id"] == "ssh-runner-prod"
        assert payload["host"] == "192.168.1.100"
        assert payload["port"] == 2222
        assert "Permission denied" in str(payload["active_slot_error"])
        assert "Permission denied" in str(payload["previous_slot_error"])

    @patch("src.runners.remote_ssh._try_connect_with_key")
    def test_no_audit_writer_does_not_crash(
        self, mock_connect: MagicMock
    ) -> None:
        """When audit_writer is None, both-slots failure still raises cleanly."""
        import paramiko

        mock_connect.side_effect = paramiko.AuthenticationException(
            "Permission denied"
        )

        reader = FakeSlotReader(active_key=FAKE_ACTIVE_KEY, previous_key=None)
        connector = SSHDualSlotConnector(slot_reader=reader, audit_writer=None)

        with pytest.raises(BothSlotsFailedError):
            connector.connect(
                runner_id="runner-1",
                host="10.0.0.5",
                port=22,
                user="ai-runner",
            )


class TestBothSlotsFailedError:
    """Tests for the BothSlotsFailedError exception class."""

    def test_with_both_errors(self) -> None:
        exc = BothSlotsFailedError(
            runner_id="runner-1",
            active_error="active key rejected",
            previous_error="previous key rejected",
        )
        assert exc.runner_id == "runner-1"
        assert exc.active_error == "active key rejected"
        assert exc.previous_error == "previous key rejected"
        assert "runner-1" in str(exc)
        assert "active key rejected" in str(exc)
        assert "previous key rejected" in str(exc)

    def test_with_no_previous_error(self) -> None:
        exc = BothSlotsFailedError(
            runner_id="runner-2",
            active_error="Permission denied",
            previous_error=None,
        )
        assert exc.previous_error is None
        assert "empty/missing" in str(exc)

    def test_is_exception(self) -> None:
        exc = BothSlotsFailedError("r", "err")
        assert isinstance(exc, Exception)
