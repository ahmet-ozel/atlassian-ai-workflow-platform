"""Remote-SSH runner - workspace path derivation + dual-slot key fallback.

Scope of this module
--------------------

Two responsibilities:

1. **Workspace path derivation** - any code in the remote-SSH runner
   that needs to know **where** a task's workspace is on the remote host
   must go through :func:`derive_workspace_path` so that the workspace
   layout ``{settings.runner_base_path}/{issue_key}/iter-{iter_n}`` is
   derived in exactly one place -
   :func:`runners.workspace_path.build_workspace_path`.

2. **Dual-slot SSH key fallback** - when establishing an SSH connection,
   the runner tries the ``active`` Vault slot first. If the connection fails
   with ``Permission denied`` (paramiko ``AuthenticationException``), it falls
   back to the ``previous`` slot. If both slots fail, an
   ``ssh_key_both_slots_failed`` audit event is emitted and the workflow is
   signalled to enter a ``queued`` retry loop.

Why a thin wrapper instead of inlining ``build_workspace_path``?

* It binds the ``base`` argument to ``settings.runner_base_path`` once,
  so call sites cannot accidentally pass a wrong ``base`` (e.g.
  hard-coding ``/var/ai-runner``, which would silently desync from a
  ``RUNNER_BASE_PATH`` override).
* It gives a single grep target - ``derive_workspace_path`` - for the
  next person who has to audit "every place the SSH runner builds a
  remote path".
* The validation contract (``InvalidIssueKeyError``,
  ``InvalidIterError``, path-traversal rejection) is inherited verbatim
  from the central helper; no duplicated regex / range checks.

Settings are resolved lazily on each call so a test can monkeypatch
``RUNNER_BASE_PATH`` between invocations without re-importing the
module. The ``settings`` parameter is exposed for tests that want to
inject a stub instead of touching the environment.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Protocol

from src.config import Settings
from src.runners.workspace_path import (
    InvalidIssueKeyError,
    InvalidIterError,
    build_workspace_path,
)

__all__ = [
    "InvalidIssueKeyError",
    "InvalidIterError",
    "derive_workspace_path",
    "SSHDualSlotConnector",
    "SSHDualSlotResult",
    "BothSlotsFailedError",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Workspace path derivation
# ---------------------------------------------------------------------------


def derive_workspace_path(
    issue_key: str,
    iter_n: int,
    *,
    settings: Settings | None = None,
) -> str:
    """Return the remote-host workspace path for ``(issue_key, iter_n)``.

    Thin wrapper around
    :func:`runners.workspace_path.build_workspace_path` that binds the
    ``base`` argument to :attr:`Settings.runner_base_path` (the
    ``RUNNER_BASE_PATH`` env var, with ``SSH_BASE_PATH`` as a deprecated
    alias - see :mod:`src.config`).

    Args:
        issue_key: Jira-style task key (e.g. ``PAY-4211``). Validated by
            :func:`build_workspace_path`; values that do not match
            ``^[A-Z][A-Z0-9_]*-\\d+$`` raise :class:`InvalidIssueKeyError`
            **before** any remote command is ever issued (path-traversal
            safety).
        iter_n: Iteration counter ``0..999``. Out-of-range or non-int
            values raise :class:`InvalidIterError`.
        settings: Optional :class:`Settings` instance; if omitted a fresh
            one is constructed and the env vars are resolved at call
            time. Tests typically pass an explicit instance to avoid
            touching the process environment.

    Returns:
        ``"{runner_base_path}/{issue_key}/iter-{iter_n}"`` with
        forward-slash separators, ready for SSH/POSIX consumption.

    Raises:
        InvalidIssueKeyError: ``issue_key`` failed the regex guard.
        InvalidIterError: ``iter_n`` is not an int in ``[0, 999]``.
    """

    resolved = settings if settings is not None else Settings()
    return build_workspace_path(resolved.runner_base_path, issue_key, iter_n)


# ---------------------------------------------------------------------------
# Dual-slot SSH key fallback
# ---------------------------------------------------------------------------


class SSHKeySlotReader(Protocol):
    """Protocol for reading SSH key slots from Vault.

    Implementations provide access to the ``active`` and ``previous``
    SSH key slots for a given runner. The
    :mod:`vault_client.ssh_keys` module's ``read_active`` and
    ``read_previous`` functions satisfy this protocol when wrapped.
    """

    def read_active_private_key(self, runner_id: str) -> str | None:
        """Return the PEM-encoded private key from the active slot.

        Returns ``None`` if the active slot is empty or missing.
        """
        ...

    def read_previous_private_key(self, runner_id: str) -> str | None:
        """Return the PEM-encoded private key from the previous slot.

        Returns ``None`` if the previous slot is empty or missing.
        """
        ...


class AuditWriter(Protocol):
    """Protocol for writing audit events.

    Implementations emit structured audit events to the platform's
    audit log (Postgres ``automation.audit_events`` table or equivalent).
    """

    def write_audit(
        self,
        action: str,
        payload: dict[str, str | int | None],
    ) -> None:
        """Write an audit event with the given action and payload."""
        ...


@dataclass(frozen=True)
class SSHDualSlotResult:
    """Result of a dual-slot SSH connection attempt.

    Attributes
    ----------
    private_key : str
        The PEM-encoded private key that successfully authenticated.
    slot_used : str
        Which slot was used: ``"active"`` or ``"previous"``.
    """

    private_key: str
    slot_used: str


class BothSlotsFailedError(Exception):
    """Raised when both active and previous SSH key slots fail authentication.

    This signals that the workflow should enter a ``queued`` retry loop
    and an ``ssh_key_both_slots_failed`` audit event should be emitted.

    Attributes
    ----------
    runner_id : str
        The runner identifier for which both slots failed.
    active_error : str
        Error message from the active slot attempt.
    previous_error : str | None
        Error message from the previous slot attempt, or ``None`` if
        the previous slot was empty/missing.
    """

    def __init__(
        self,
        runner_id: str,
        active_error: str,
        previous_error: str | None = None,
    ) -> None:
        self.runner_id = runner_id
        self.active_error = active_error
        self.previous_error = previous_error
        msg = (
            f"SSH key authentication failed for runner={runner_id}: "
            f"active slot error: {active_error}"
        )
        if previous_error:
            msg += f"; previous slot error: {previous_error}"
        else:
            msg += "; previous slot is empty/missing"
        super().__init__(msg)


def _try_connect_with_key(
    host: str,
    port: int,
    user: str,
    private_key_pem: str,
    timeout: float = 30.0,
) -> None:
    """Attempt an SSH connection with the given private key (blocking).

    This function performs only the authentication handshake - it does
    NOT execute any commands. It is used to validate that a key is
    accepted by the remote host.

    Raises:
        paramiko.AuthenticationException: If the key is rejected.
        Exception: On other connection errors (network, timeout, etc.).
    """
    import paramiko  # noqa: PLC0415

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        # Parse the private key
        key_file = io.StringIO(private_key_pem)
        try:
            pkey = paramiko.Ed25519Key.from_private_key(key_file)
        except paramiko.SSHException:
            key_file.seek(0)
            try:
                pkey = paramiko.RSAKey.from_private_key(key_file)
            except paramiko.SSHException:
                key_file.seek(0)
                try:
                    pkey = paramiko.ECDSAKey.from_private_key(key_file)
                except paramiko.SSHException as exc:
                    raise paramiko.AuthenticationException(
                        f"unable to parse private key: {exc}"
                    ) from exc

        client.connect(
            hostname=host,
            port=port,
            username=user,
            pkey=pkey,
            timeout=timeout,
            allow_agent=False,
            look_for_keys=False,
        )
    finally:
        client.close()


class SSHDualSlotConnector:
    """Manages dual-slot SSH key fallback for zero-downtime rotation.

    During a key rotation window, both the ``active`` and ``previous``
    Vault slots contain valid SSH keys. This connector tries the
    ``active`` slot first; if authentication fails (``Permission
    denied`` / paramiko ``AuthenticationException``), it falls back to
    the ``previous`` slot.

    If both slots fail, it emits an ``ssh_key_both_slots_failed`` audit
    event and raises :class:`BothSlotsFailedError` to signal the
    workflow to enter a ``queued`` retry loop.

    Usage::

        connector = SSHDualSlotConnector(
            slot_reader=my_slot_reader,
            audit_writer=my_audit_writer,
        )
        result = connector.connect(
            runner_id="runner-1",
            host="10.0.0.5",
            port=22,
            user="ai-runner",
        )
        # result.private_key contains the working key
        # result.slot_used is "active" or "previous"
    """

    def __init__(
        self,
        slot_reader: SSHKeySlotReader,
        audit_writer: AuditWriter | None = None,
    ) -> None:
        """Initialize the dual-slot connector.

        Args:
            slot_reader: Provider of active/previous SSH key material.
            audit_writer: Optional audit event writer. When provided,
                ``ssh_key_both_slots_failed`` events are emitted on
                dual-slot failure.
        """
        self._slot_reader = slot_reader
        self._audit_writer = audit_writer

    def connect(
        self,
        runner_id: str,
        host: str,
        port: int,
        user: str,
        timeout: float = 30.0,
    ) -> SSHDualSlotResult:
        """Try active slot, fall back to previous on auth failure.

        Attempts SSH authentication using the ``active`` Vault slot
        key. If the connection raises ``AuthenticationException``
        (Permission denied), retries with the ``previous`` slot key.

        If both slots fail authentication (or the previous slot is
        empty), emits ``ssh_key_both_slots_failed`` audit and raises
        :class:`BothSlotsFailedError`.

        Non-authentication errors (network unreachable, timeout, etc.)
        from the active slot are raised immediately without attempting
        the previous slot - those failures are not key-related.

        Args:
            runner_id: Runner identifier for Vault path resolution.
            host: SSH host address.
            port: SSH port number.
            user: SSH username.
            timeout: Connection timeout in seconds.

        Returns:
            :class:`SSHDualSlotResult` with the working key and which
            slot was used.

        Raises:
            BothSlotsFailedError: Both slots failed authentication.
            Exception: Non-authentication errors (network, timeout).
        """
        import paramiko  # noqa: PLC0415

        # --- Step 1: Read active slot ---
        active_key = self._slot_reader.read_active_private_key(runner_id)
        if not active_key:
            # No active key at all - this is a configuration error,
            # not a rotation scenario. Emit audit and raise.
            active_error = "active slot is empty or missing"
            previous_key = self._slot_reader.read_previous_private_key(runner_id)
            if previous_key:
                # Try previous as last resort
                try:
                    _try_connect_with_key(host, port, user, previous_key, timeout)
                    logger.warning(
                        "SSH active slot empty for runner=%s, "
                        "connected via previous slot",
                        runner_id,
                    )
                    return SSHDualSlotResult(
                        private_key=previous_key,
                        slot_used="previous",
                    )
                except Exception as prev_exc:
                    previous_error = str(prev_exc)
                    self._emit_both_slots_failed(
                        runner_id, host, port, active_error, previous_error
                    )
                    raise BothSlotsFailedError(
                        runner_id, active_error, previous_error
                    ) from prev_exc
            else:
                self._emit_both_slots_failed(
                    runner_id, host, port, active_error, None
                )
                raise BothSlotsFailedError(runner_id, active_error, None)

        # --- Step 2: Try active slot ---
        try:
            _try_connect_with_key(host, port, user, active_key, timeout)
            return SSHDualSlotResult(
                private_key=active_key,
                slot_used="active",
            )
        except paramiko.AuthenticationException as active_exc:
            active_error = str(active_exc)
            logger.warning(
                "SSH active slot authentication failed for runner=%s "
                "host=%s: %s - trying previous slot",
                runner_id,
                host,
                active_error,
            )
        except Exception:
            # Non-authentication error (network, timeout, etc.) -
            # raise immediately, this is not a key rotation issue.
            raise

        # --- Step 3: Fall back to previous slot ---
        previous_key = self._slot_reader.read_previous_private_key(runner_id)
        if not previous_key:
            logger.error(
                "SSH previous slot is empty for runner=%s - "
                "both slots failed",
                runner_id,
            )
            self._emit_both_slots_failed(
                runner_id, host, port, active_error, None
            )
            raise BothSlotsFailedError(runner_id, active_error, None)

        try:
            _try_connect_with_key(host, port, user, previous_key, timeout)
            logger.info(
                "SSH previous slot succeeded for runner=%s host=%s "
                "(active slot was rejected - rotation in progress?)",
                runner_id,
                host,
            )
            return SSHDualSlotResult(
                private_key=previous_key,
                slot_used="previous",
            )
        except paramiko.AuthenticationException as prev_exc:
            previous_error = str(prev_exc)
            logger.error(
                "SSH both slots failed for runner=%s host=%s: "
                "active=%s, previous=%s",
                runner_id,
                host,
                active_error,
                previous_error,
            )
            self._emit_both_slots_failed(
                runner_id, host, port, active_error, previous_error
            )
            raise BothSlotsFailedError(
                runner_id, active_error, previous_error
            ) from prev_exc
        except Exception as prev_exc:
            # Previous slot had a non-auth error (network, parse, etc.)
            previous_error = str(prev_exc)
            logger.error(
                "SSH previous slot non-auth error for runner=%s: %s",
                runner_id,
                previous_error,
            )
            self._emit_both_slots_failed(
                runner_id, host, port, active_error, previous_error
            )
            raise BothSlotsFailedError(
                runner_id, active_error, previous_error
            ) from prev_exc

    def _emit_both_slots_failed(
        self,
        runner_id: str,
        host: str,
        port: int,
        active_error: str,
        previous_error: str | None,
    ) -> None:
        """Emit ``ssh_key_both_slots_failed`` audit event."""
        if self._audit_writer is None:
            return

        payload: dict[str, str | int | None] = {
            "runner_id": runner_id,
            "host": host,
            "port": port,
            "active_slot_error": active_error,
            "previous_slot_error": previous_error,
        }

        try:
            self._audit_writer.write_audit(
                action="ssh_key_both_slots_failed",
                payload=payload,
            )
        except Exception:  # noqa: BLE001 - audit is best-effort
            logger.warning(
                "Failed to emit ssh_key_both_slots_failed audit for "
                "runner=%s (best-effort, continuing)",
                runner_id,
                exc_info=True,
            )
