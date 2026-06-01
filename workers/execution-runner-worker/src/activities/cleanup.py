"""Cleanup policy enforcement activity (Requirement — Task Cleanup Policy).

Provides :func:`apply_cleanup_policy` — a Temporal activity that enforces
the user-specified cleanup policy (``delete_on_success`` | ``always`` |
``never``) after a test execution completes.

The activity performs:
- ``docker rm -f`` of the task container (if container_id is provided).
- ``docker rmi`` of the task image (if image_id is provided).
- ``rm -rf {workspace_path}`` on the SSH runner host.

The policy logic:
- ``delete_on_success`` (aliased as ``on_success``): cleanup only when
  ``exit_code == 0``.
- ``always``: cleanup regardless of exit code.
- ``never``: skip all cleanup (workspace preserved for inspection).

Audit event ``workspace_cleanup_applied`` is emitted with the policy
and outcome for observability.
"""

from __future__ import annotations

import asyncio
import io
import os
from dataclasses import dataclass
from typing import Any

from temporalio import activity

__all__ = [
    "CleanupPolicyInput",
    "CleanupPolicyResult",
    "apply_cleanup_policy",
]


@dataclass(frozen=True)
class CleanupPolicyInput:
    """Input for the cleanup policy activity.

    Attributes
    ----------
    policy : str
        One of ``"always"``, ``"on_success"`` (or ``"delete_on_success"``),
        ``"never"``.
    exit_code : int | None
        Exit code of the test command. ``None`` when the command timed out
        or the runner was unreachable.
    workspace_path : str
        Remote directory to remove on the SSH runner.
    container_id : str | None
        Docker container ID to ``docker rm -f``. ``None`` when no
        container was used.
    image_id : str | None
        Docker image ID to ``docker rmi``. ``None`` when no custom image
        was built.
    runner_host : str | None
        SSH host override. When ``None`` the activity reads from
        ``SSH_HOST`` (canonical) with ``SSH_HOST_1`` accepted as a
        deprecated alias for backwards compatibility. The platform
        runs exactly one runner host — there is no per-department
        override.
    workflow_id : str
        Parent workflow ID for audit correlation.
    department_id : str
        Department slug for audit scoping.
    vault_path : str | None
        Explicit Vault path for the selected SSH runner. ``None`` keeps
        the legacy env-derived single-runner credential path.
    """

    policy: str
    exit_code: int | None
    workspace_path: str
    container_id: str | None = None
    image_id: str | None = None
    runner_host: str | None = None
    workflow_id: str = ""
    department_id: str = ""
    vault_path: str | None = None


@dataclass(frozen=True)
class CleanupPolicyResult:
    """Result of the cleanup policy activity.

    Attributes
    ----------
    cleanup_performed : bool
        ``True`` when at least one cleanup action was executed.
    policy : str
        The policy that was evaluated.
    actions : list[str]
        List of actions taken (e.g. ``["docker_rm", "docker_rmi",
        "workspace_rm"]``).
    skipped_reason : str | None
        Reason cleanup was skipped (e.g. ``"policy=never"`` or
        ``"policy=on_success,exit_code=1"``).
    errors : list[str]
        Non-fatal errors encountered during cleanup (best-effort).
    """

    cleanup_performed: bool
    policy: str
    actions: list[str]
    skipped_reason: str | None = None
    errors: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cleanup_performed": self.cleanup_performed,
            "policy": self.policy,
            "actions": self.actions,
            "skipped_reason": self.skipped_reason,
            "errors": self.errors or [],
        }


def _normalize_policy(policy: str) -> str:
    """Normalize policy string to canonical form.

    Accepts ``delete_on_success`` as an alias for ``on_success``.
    """
    normalized = policy.strip().lower()
    if normalized == "delete_on_success":
        return "on_success"
    return normalized


def _should_cleanup(policy: str, exit_code: int | None) -> tuple[bool, str | None]:
    """Determine whether cleanup should proceed.

    Returns (should_clean, skip_reason).
    """
    if policy == "always":
        return True, None
    if policy == "on_success":
        if exit_code is not None and exit_code == 0:
            return True, None
        return False, f"policy=on_success,exit_code={exit_code}"
    if policy == "never":
        return False, "policy=never"
    # Unknown policy — treat as never for safety.
    return False, f"unknown_policy={policy}"


def _ssh_rm_workspace(
    host: str,
    port: int,
    user: str,
    private_key: str,
    workspace_path: str,
) -> str | None:
    """Remove workspace directory on the SSH runner (blocking).

    Returns ``None`` on success, error string on failure.
    Best-effort — does not raise.
    """
    import paramiko  # noqa: PLC0415

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        key_file = io.StringIO(private_key)
        try:
            pkey = paramiko.RSAKey.from_private_key(key_file)
        except paramiko.SSHException:
            key_file.seek(0)
            try:
                pkey = paramiko.Ed25519Key.from_private_key(key_file)
            except paramiko.SSHException:
                key_file.seek(0)
                try:
                    pkey = paramiko.ECDSAKey.from_private_key(key_file)
                except paramiko.SSHException as exc:
                    return f"key parse error: {exc}"

        client.connect(
            hostname=host,
            port=port,
            username=user,
            pkey=pkey,
            timeout=15.0,
            allow_agent=False,
            look_for_keys=False,
        )

        # Remove workspace directory
        import shlex  # noqa: PLC0415

        cmd = f"rm -rf {shlex.quote(workspace_path)}"
        _stdin, _stdout, stderr_ch = client.exec_command(cmd, timeout=60.0)
        exit_status = _stdout.channel.recv_exit_status()
        if exit_status != 0:
            err_text = stderr_ch.read().decode("utf-8", errors="replace")
            return f"rm -rf exited {exit_status}: {err_text[:200]}"
        return None

    except Exception as exc:  # noqa: BLE001
        return f"ssh cleanup error: {exc}"
    finally:
        client.close()


def _ssh_docker_cleanup(
    host: str,
    port: int,
    user: str,
    private_key: str,
    container_id: str | None,
    image_id: str | None,
) -> tuple[list[str], list[str]]:
    """Remove Docker container and image on the SSH runner (blocking).

    Returns (actions_taken, errors).
    """
    import paramiko  # noqa: PLC0415

    actions: list[str] = []
    errors: list[str] = []

    if not container_id and not image_id:
        return actions, errors

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        key_file = io.StringIO(private_key)
        try:
            pkey = paramiko.RSAKey.from_private_key(key_file)
        except paramiko.SSHException:
            key_file.seek(0)
            try:
                pkey = paramiko.Ed25519Key.from_private_key(key_file)
            except paramiko.SSHException:
                key_file.seek(0)
                try:
                    pkey = paramiko.ECDSAKey.from_private_key(key_file)
                except paramiko.SSHException as exc:
                    errors.append(f"key parse error: {exc}")
                    return actions, errors

        client.connect(
            hostname=host,
            port=port,
            username=user,
            pkey=pkey,
            timeout=15.0,
            allow_agent=False,
            look_for_keys=False,
        )

        import shlex  # noqa: PLC0415

        if container_id:
            cmd = f"docker rm -f {shlex.quote(container_id)}"
            _stdin, stdout_ch, stderr_ch = client.exec_command(cmd, timeout=30.0)
            exit_status = stdout_ch.channel.recv_exit_status()
            if exit_status == 0:
                actions.append("docker_rm")
            else:
                err = stderr_ch.read().decode("utf-8", errors="replace")[:200]
                errors.append(f"docker rm failed: {err}")

        if image_id:
            cmd = f"docker rmi {shlex.quote(image_id)}"
            _stdin, stdout_ch, stderr_ch = client.exec_command(cmd, timeout=30.0)
            exit_status = stdout_ch.channel.recv_exit_status()
            if exit_status == 0:
                actions.append("docker_rmi")
            else:
                err = stderr_ch.read().decode("utf-8", errors="replace")[:200]
                errors.append(f"docker rmi failed: {err}")

    except Exception as exc:  # noqa: BLE001
        errors.append(f"docker cleanup ssh error: {exc}")
    finally:
        client.close()

    return actions, errors


@activity.defn(name="apply_cleanup_policy")
async def apply_cleanup_policy(
    policy: str,
    exit_code: int | None,
    workspace_path: str,
    container_id: str | None = None,
    image_id: str | None = None,
    workflow_id: str = "",
    department_id: str = "",
    vault_path: str | None = None,
) -> dict[str, Any]:
    """Enforce the user-specified cleanup policy after test execution.

    This activity is called as the **final step** of
    :class:`ExecutionRunWorkflow` to apply the cleanup policy specified
    in the Forge custom field.

    The activity is best-effort: individual cleanup failures (e.g.
    container already removed, image in use) are logged and reported
    in the result but do not cause the activity to fail.

    Parameters
    ----------
    policy:
        One of ``"always"``, ``"on_success"`` / ``"delete_on_success"``,
        ``"never"``.
    exit_code:
        Exit code of the test command. ``None`` on timeout/unreachable.
    workspace_path:
        Remote directory to remove.
    container_id:
        Docker container to ``docker rm -f``. ``None`` to skip.
    image_id:
        Docker image to ``docker rmi``. ``None`` to skip.
    workflow_id:
        Parent workflow ID for audit correlation.
    department_id:
        Department slug for audit.
    vault_path:
        Explicit Vault KV-v2 path for the selected runner credentials.
        This is supplied by ``ExecutionRunWorkflow`` after resolving the
        admin-panel runner assignment; when absent the legacy fallback is
        used.

    Returns
    -------
    dict
        Serialisable result matching :class:`CleanupPolicyResult`.
    """
    normalized_policy = _normalize_policy(policy)

    activity.logger.info(
        "apply_cleanup_policy: policy=%s exit_code=%s workspace=%s "
        "container=%s image=%s workflow=%s dept=%s",
        normalized_policy,
        exit_code,
        workspace_path,
        container_id,
        image_id,
        workflow_id,
        department_id,
    )

    should_clean, skip_reason = _should_cleanup(normalized_policy, exit_code)

    if not should_clean:
        activity.logger.info(
            "apply_cleanup_policy: skipping cleanup — %s", skip_reason
        )
        result = CleanupPolicyResult(
            cleanup_performed=False,
            policy=normalized_policy,
            actions=[],
            skipped_reason=skip_reason,
        )
        return result.to_dict()

    # Resolve SSH credentials for cleanup operations.
    # We use the same env vars as the ssh_run_test activity.
    from src.activities.vault import vault_fetch_ssh_credentials  # noqa: PLC0415

    actions: list[str] = []
    all_errors: list[str] = []

    try:
        cred = await vault_fetch_ssh_credentials(workflow_id, vault_path=vault_path)
    except Exception as exc:  # noqa: BLE001
        activity.logger.warning(
            "apply_cleanup_policy: vault lookup failed, cannot cleanup: %s",
            exc,
        )
        result = CleanupPolicyResult(
            cleanup_performed=False,
            policy=normalized_policy,
            actions=[],
            skipped_reason=f"vault_lookup_failed: {exc}",
        )
        return result.to_dict()

    # Docker cleanup (container + image).
    if container_id or image_id:
        docker_actions, docker_errors = await asyncio.to_thread(
            _ssh_docker_cleanup,
            cred.host,
            cred.port,
            cred.user,
            cred.private_key,
            container_id,
            image_id,
        )
        actions.extend(docker_actions)
        all_errors.extend(docker_errors)

    # Workspace cleanup.
    if workspace_path:
        ws_error = await asyncio.to_thread(
            _ssh_rm_workspace,
            cred.host,
            cred.port,
            cred.user,
            cred.private_key,
            workspace_path,
        )
        if ws_error is None:
            actions.append("workspace_rm")
        else:
            all_errors.append(ws_error)

    cleanup_performed = len(actions) > 0

    activity.logger.info(
        "apply_cleanup_policy: done — performed=%s actions=%s errors=%s",
        cleanup_performed,
        actions,
        all_errors,
    )

    result = CleanupPolicyResult(
        cleanup_performed=cleanup_performed,
        policy=normalized_policy,
        actions=actions,
        skipped_reason=None,
        errors=all_errors if all_errors else None,
    )
    return result.to_dict()
