"""SSH activity module for the execution-runner-worker.

Provides :func:`ssh_connect_and_run` and :func:`ssh_cleanup`, Temporal
activities that execute commands and perform cleanup on remote SSH hosts.

SSH connections are established via **paramiko** and wrapped in
``asyncio.to_thread`` to avoid blocking the Temporal worker's event loop.

Connection retry (3x with exponential backoff) is configured by the caller
workflow via Temporal activity options (``RetryPolicy``):

    RetryPolicy(
        initial_interval=timedelta(seconds=1),
        backoff_coefficient=2.0,
        maximum_attempts=3,
    )

The activity itself does not implement internal retries - it raises on
connection failure and lets Temporal handle retry scheduling.

Total execution timeout (30 minutes) is enforced by the caller workflow
via ``start_to_close_timeout=timedelta(minutes=30)`` in activity options.
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass

from temporalio import activity

__all__ = [
    "RunResult",
    "SSHActivityError",
    "ssh_connect_and_run",
    "ssh_cleanup",
    "ssh_run_test",
    "HEARTBEAT_INTERVAL_S",
]


class SSHActivityError(RuntimeError):
    """Raised when an SSH operation fails.

    Attributes
    ----------
    host : str
        The target SSH host.
    cause : str
        Human-readable description of what went wrong.
    """

    def __init__(self, host: str, cause: str) -> None:
        self.host = host
        self.cause = cause
        super().__init__(f"ssh operation failed: host={host}: {cause}")


@dataclass(frozen=True)
class RunResult:
    """Result of a remote command execution via SSH.

    Attributes
    ----------
    stdout : str
        Standard output captured from the remote command.
    stderr : str
        Standard error captured from the remote command.
    exit_code : int
        Process exit code from the remote command.
    """

    stdout: str
    stderr: str
    exit_code: int


# ---------------------------------------------------------------------------
# Internal helpers (run in thread via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _ssh_execute_command(
    host: str,
    port: int,
    user: str,
    private_key: str,
    command: str,
    workspace_path: str,
    timeout_seconds: int,
) -> RunResult:
    """Connect to SSH host and execute a command (blocking).

    This function is designed to be called via ``asyncio.to_thread``
    so it does not block the event loop.

    Parameters
    ----------
    host:
        SSH host address.
    port:
        SSH port number.
    user:
        SSH username.
    private_key:
        PEM-encoded private key string.
    command:
        Shell command to execute on the remote host.
    workspace_path:
        Working directory on the remote host. The command is wrapped
        as ``cd {workspace_path} && {command}``.
    timeout_seconds:
        Maximum time in seconds to wait for the command to complete.

    Returns
    -------
    RunResult
        Captured stdout, stderr, and exit code.

    Raises
    ------
    SSHActivityError
        On connection failure, authentication failure, or timeout.
    """
    import paramiko  # noqa: PLC0415 - imported inside function to keep module importable without paramiko installed

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        # Load private key from string
        key_file = io.StringIO(private_key)
        try:
            pkey = paramiko.RSAKey.from_private_key(key_file)
        except paramiko.SSHException:
            # Try other key types
            key_file.seek(0)
            try:
                pkey = paramiko.Ed25519Key.from_private_key(key_file)
            except paramiko.SSHException:
                key_file.seek(0)
                try:
                    pkey = paramiko.ECDSAKey.from_private_key(key_file)
                except paramiko.SSHException as exc:
                    raise SSHActivityError(
                        host=host,
                        cause=f"unable to parse private key: {exc}",
                    ) from exc

        # Connect with timeout
        client.connect(
            hostname=host,
            port=port,
            username=user,
            pkey=pkey,
            timeout=30.0,  # connection timeout (not command timeout)
            allow_agent=False,
            look_for_keys=False,
        )

        # Build the full command with workspace_path as working directory
        if workspace_path:
            full_command = f"cd {workspace_path} && {command}"
        else:
            full_command = command

        # Execute command with timeout
        stdin, stdout_channel, stderr_channel = client.exec_command(
            full_command,
            timeout=float(timeout_seconds),
        )
        stdin.close()

        # Read output
        stdout_text = stdout_channel.read().decode("utf-8", errors="replace")
        stderr_text = stderr_channel.read().decode("utf-8", errors="replace")
        exit_code = stdout_channel.channel.recv_exit_status()

        return RunResult(
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=exit_code,
        )

    except SSHActivityError:
        raise
    except paramiko.AuthenticationException as exc:
        raise SSHActivityError(
            host=host,
            cause=f"authentication failed: {exc}",
        ) from exc
    except paramiko.SSHException as exc:
        raise SSHActivityError(
            host=host,
            cause=f"SSH error: {exc}",
        ) from exc
    except TimeoutError as exc:
        raise SSHActivityError(
            host=host,
            cause=f"connection or command timed out after {timeout_seconds}s",
        ) from exc
    except OSError as exc:
        raise SSHActivityError(
            host=host,
            cause=f"network error: {exc}",
        ) from exc
    finally:
        client.close()


def _ssh_cleanup_workspace(
    host: str,
    port: int,
    user: str,
    private_key: str,
    workspace_path: str,
) -> None:
    """Connect to SSH host and remove the workspace directory (blocking).

    Best-effort cleanup - exceptions are logged but not propagated.

    Parameters
    ----------
    host:
        SSH host address.
    port:
        SSH port number.
    user:
        SSH username.
    private_key:
        PEM-encoded private key string.
    workspace_path:
        Directory to remove on the remote host.
    """
    import paramiko  # noqa: PLC0415

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        # Load private key from string
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
                except paramiko.SSHException:
                    return  # Cannot parse key - best-effort, just return

        client.connect(
            hostname=host,
            port=port,
            username=user,
            pkey=pkey,
            timeout=15.0,
            allow_agent=False,
            look_for_keys=False,
        )

        # Remove workspace directory recursively
        cleanup_command = f"rm -rf {workspace_path}"
        client.exec_command(cleanup_command, timeout=60.0)

    except Exception:  # noqa: BLE001 - best-effort, swallow all exceptions
        pass
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Temporal activities
# ---------------------------------------------------------------------------


@activity.defn(name="ssh_connect_and_run")
async def ssh_connect_and_run(
    cred: dict[str, str | int],
    command: str,
    workspace_path: str,
    timeout_minutes: int = 30,
) -> dict[str, str | int]:
    """Connect to a remote SSH host and execute a command.

    Uses ``asyncio.to_thread`` to run the blocking paramiko SSH session
    without blocking the Temporal worker's event loop.

    Connection retry is handled by the caller workflow via Temporal
    activity options::

        RetryPolicy(
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2.0,
            maximum_attempts=3,
        )

    Total execution timeout (30 minutes) is enforced by the caller
    workflow via ``start_to_close_timeout``.

    Parameters
    ----------
    cred:
        SSH credentials dict with keys: ``host``, ``port``, ``user``,
        ``private_key``. Typically the output of
        :func:`vault_fetch_ssh_credentials` serialized as a dict.
    command:
        Shell command to execute on the remote host.
    workspace_path:
        Working directory on the remote host. The command is executed
        as ``cd {workspace_path} && {command}``.
    timeout_minutes:
        Maximum time in minutes for the command to complete.
        Defaults to 30 minutes.

    Returns
    -------
    dict
        Serializable representation of :class:`RunResult` with keys:
        ``stdout``, ``stderr``, ``exit_code``.

    Raises
    ------
    SSHActivityError
        On connection failure, authentication failure, or command timeout.
        The caller workflow's retry policy will handle retries.
    """
    host = str(cred["host"])
    port = int(cred["port"])
    user = str(cred["user"])
    private_key = str(cred["private_key"])

    activity.logger.info(
        "SSH connect and run: host=%s, port=%d, user=%s, workspace=%s, "
        "timeout=%d min, command=%s",
        host,
        port,
        user,
        workspace_path,
        timeout_minutes,
        command[:100],  # Truncate for logging
    )

    timeout_seconds = timeout_minutes * 60

    result = await asyncio.to_thread(
        _ssh_execute_command,
        host,
        port,
        user,
        private_key,
        command,
        workspace_path,
        timeout_seconds,
    )

    activity.logger.info(
        "SSH command completed: host=%s, exit_code=%d, "
        "stdout_len=%d, stderr_len=%d",
        host,
        result.exit_code,
        len(result.stdout),
        len(result.stderr),
    )

    # Return as dict for Temporal serialization
    return {
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
    }


@activity.defn(name="ssh_cleanup")
async def ssh_cleanup(
    cred: dict[str, str | int],
    workspace_path: str,
) -> None:
    """Remove the workspace directory on the remote SSH host.

    Best-effort cleanup - exceptions are swallowed and logged. The
    workflow continues regardless of whether cleanup succeeds.

    Uses ``asyncio.to_thread`` to run the blocking paramiko SSH session
    without blocking the Temporal worker's event loop.

    Parameters
    ----------
    cred:
        SSH credentials dict with keys: ``host``, ``port``, ``user``,
        ``private_key``.
    workspace_path:
        Directory to remove on the remote host.

    Returns
    -------
    None
        Always returns None. Failures are logged but not raised.
    """
    host = str(cred["host"])
    port = int(cred["port"])
    user = str(cred["user"])
    private_key = str(cred["private_key"])

    activity.logger.info(
        "SSH cleanup: host=%s, workspace=%s",
        host,
        workspace_path,
    )

    try:
        await asyncio.to_thread(
            _ssh_cleanup_workspace,
            host,
            port,
            user,
            private_key,
            workspace_path,
        )
        activity.logger.info(
            "SSH cleanup completed: host=%s, workspace=%s",
            host,
            workspace_path,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort, swallow all
        activity.logger.warning(
            "SSH cleanup failed (best-effort, continuing): host=%s, "
            "workspace=%s, error=%s",
            host,
            workspace_path,
            str(exc),
        )



# ---------------------------------------------------------------------------
# ssh_run_test - canonical SSH test runner activity
#
# Combines credential fetch, command execution, MinIO artifact upload, and
# 30-second heartbeating into a single activity so the canonical
# ``ExecutionRunWorkflow`` only orchestrates one activity call.
#
# Heartbeat strategy
# ------------------
# The blocking SSH command runs on a worker thread via
# ``asyncio.to_thread``.  A concurrent ``asyncio.Task`` calls
# ``activity.heartbeat()`` every 30 seconds. Heartbeating keeps the activity
# alive against the
# heartbeat timeout the workflow sets (default 90 s, ≥ 3× the heartbeat
# interval) and surfaces partial progress to Temporal so the activity
# can be resumed on a different worker after a crash.
#
# Command shaping
# ---------------
# Environment variables are exported via ``KEY=VALUE`` prefixes so the
# command sees them.  Values are shell-escaped with :func:`shlex.quote`
# to defang injection attempts coming from upstream payload data.  When a
# working directory is supplied the wrapped form
# ``cd {workdir} && {env_assignments}{command}`` is used.
#
# Result shape
# ------------
# The activity returns a JSON-serialisable dict matching
# :class:`ExecutionRunWorkflowOutput`'s fields, populated as follows:
#
# * ``status``           - ``"passed"`` (exit_code == 0),
# ``"failed"`` (exit_code != 0), or
# ``"timeout"`` (TimeoutError raised by SSH).
# * ``exit_code``        - process exit code, or ``None`` on timeout.
# * ``stdout_uri`` /     - ``s3://{bucket}/{prefix}/stdout.txt`` and
# ``stderr_uri``         ``stderr.txt``; ``None``
# if upload failed.
# * ``duration_seconds`` - wall-clock seconds spent inside the activity.
# * ``runner_id``        - echo of the input runner identifier so the
# parent can correlate retries against runner.
# * ``failure_reason``   - stable category string when ``status !=
# "passed"``: ``"non_zero_exit"``, ``"timeout"``,
# ``"runner_unreachable"``,
# ``"artifact_upload_failed"``, or ``None``.
# ---------------------------------------------------------------------------

import os
import shlex
import time
from typing import Any

import httpx

from src.activities.minio import (
    DEFAULT_BUCKET,
    _ensure_bucket_exists,
    _minio_access_key,
    _minio_endpoint,
    _minio_secret_key,
    _s3_headers,
)
from src.activities.vault import vault_fetch_ssh_credentials

#: Heartbeat cadence.  The activity beats every ``HEARTBEAT_INTERVAL_S``
#: seconds while the SSH command runs. 30 s matches the heartbeat-timeout
#: 3× safety margin
#: (default heartbeat_timeout 90 s in
#: :mod:`src.workflows.execution_run_workflow`).
HEARTBEAT_INTERVAL_S: float = 30.0

#: Standard MinIO key suffixes for the canonical activity.  The full
#: object key is ``{prefix}/{name}`` where ``prefix`` is the
#: ``artifact_minio_prefix`` workflow input.
_STDOUT_NAME: str = "stdout.txt"
_STDERR_NAME: str = "stderr.txt"


def _bucket_and_key_from_prefix(prefix: str) -> tuple[str, str]:
    """Split an ``s3://bucket/key/path`` or ``bucket/key/path`` prefix.

    Used by :func:`ssh_run_test` to decide which bucket/key path the
    stdout/stderr artifacts go into.

    Examples
    --------

    >>> _bucket_and_key_from_prefix("s3://ai-runs/exec-1700000000")
    ('ai-runs', 'exec-1700000000')

    >>> _bucket_and_key_from_prefix("ai-runs/exec-1700000000/")
    ('ai-runs', 'exec-1700000000')

    A bare prefix without an explicit bucket falls back to
    :data:`src.activities.minio.DEFAULT_BUCKET`::

    >>> _bucket_and_key_from_prefix("")
    ('ai-runs', '')

    Notes
    -----
    The function is **pure** - string parsing only - so it is safe to
    call from inside the activity body without touching the worker
    event loop.
    """
    raw = prefix or ""
    if raw.startswith("s3://"):
        raw = raw[len("s3://") :]
    raw = raw.strip().strip("/")
    if not raw:
        return (DEFAULT_BUCKET, "")
    if "/" in raw:
        bucket, key_prefix = raw.split("/", 1)
        return (bucket, key_prefix.rstrip("/"))
    # Single-segment prefix is interpreted as a bucket name.
    return (raw, "")


def _build_artifact_uri(bucket: str, key: str) -> str:
    """Return the canonical ``s3://{bucket}/{key}`` URI for an artifact."""
    return f"s3://{bucket}/{key}"


def _shell_quote_environment(
    env: tuple[tuple[str, str], ...] | dict[str, str] | None,
) -> str:
    """Render an environment mapping as a leading ``KEY=VALUE`` prefix.

    The prefix can be prepended to a shell command so the command is
    invoked with the supplied environment variables exported.  Values
    are :func:`shlex.quote`-escaped to neutralise injection from
    untrusted callers.

    Returns
    -------
    str
        Either an empty string (when ``env`` is empty) or
        ``"KEY1=value1 KEY2=value2 "`` (note the trailing space).
    """
    if not env:
        return ""

    if isinstance(env, dict):
        items: list[tuple[str, str]] = list(env.items())
    else:
        items = [(str(k), str(v)) for (k, v) in env]

    if not items:
        return ""

    # Sort for deterministic command shaping (replay-friendly logging).
    items.sort(key=lambda kv: kv[0])
    parts: list[str] = []
    for key, value in items:
        # Reject keys containing characters that would break the shell
        # assignment grammar; the activity should fail loud rather than
        # accidentally execute injected payloads.
        if not key or any(ch in key for ch in ("=", " ", "\t", "\n", "'", '"')):
            raise ValueError(f"invalid environment variable name: {key!r}")
        parts.append(f"{key}={shlex.quote(value)}")
    return " ".join(parts) + " "


def _build_full_command(
    command: str,
    *,
    workdir: str | None,
    env_prefix: str,
) -> str:
    """Wrap a command with optional ``cd`` and an environment prefix."""
    body = f"{env_prefix}{command}"
    if workdir:
        quoted_workdir = shlex.quote(workdir)
        return f"mkdir -p {quoted_workdir} && cd {quoted_workdir} && {body}"
    return body


def _resolve_command_timeout_seconds() -> int:
    """Read ``SSH_COMMAND_TIMEOUT_S`` from the environment.

    Used as the per-command timeout passed to ``paramiko.exec_command``.
    The Temporal-level ``start_to_close_timeout`` is enforced
    independently by the workflow.

    Returns
    -------
    int
        Seconds.  Defaults to ``1800`` (30 min) when the env var is
        missing or unparseable.
    """
    raw = os.environ.get("SSH_COMMAND_TIMEOUT_S", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1800
    if value <= 0:
        return 1800
    return value


async def _upload_text_artifact(
    bucket: str,
    key: str,
    payload: bytes,
) -> str | None:
    """Upload a UTF-8 text artifact to MinIO and return its URI.

    Returns ``None`` when MinIO credentials are missing - the caller
    treats a ``None`` URI as ``failure_reason="artifact_upload_failed"``.

    The function deliberately does **not** raise on transport failures -
    activity-level retry is governed by the workflow's
    ``RetryPolicy(maximum_attempts=3)``; a transient MinIO outage should
    drive Temporal to retry the whole activity rather than surface a
    half-successful run.
    """
    endpoint = _minio_endpoint()
    access_key = _minio_access_key()
    secret_key = _minio_secret_key()

    if not access_key or not secret_key:
        activity.logger.warning(
            "ssh_run_test: MinIO credentials missing; skipping upload "
            "of bucket=%s key=%s",
            bucket,
            key,
        )
        return None

    async with httpx.AsyncClient(timeout=60.0) as client:
        await _ensure_bucket_exists(
            client, endpoint, bucket, access_key, secret_key
        )
        url, headers = _s3_headers(
            method="PUT",
            bucket=bucket,
            key=key,
            access_key=access_key,
            secret_key=secret_key,
            endpoint=endpoint,
            payload=payload,
        )
        response = await client.put(url, headers=headers, content=payload)
        if not (200 <= response.status_code < 300):
            activity.logger.warning(
                "ssh_run_test: MinIO upload failed bucket=%s key=%s "
                "status=%d body=%r",
                bucket,
                key,
                response.status_code,
                response.text[:200],
            )
            return None

    return _build_artifact_uri(bucket, key)


@activity.defn(name="ssh_run_test")
async def ssh_run_test(
    runner_id: str | None,
    command: str,
    env: tuple[tuple[str, str], ...] | dict[str, str] | None,
    artifact_minio_prefix: str | None,
    workdir: str | None = None,
    parent_workflow_id: str = "",
    vault_path: str | None = None,
) -> dict[str, Any]:
    """Run a test command on a remote SSH runner and capture its output.

    Combines four side effects into a single Temporal activity so the
    canonical :class:`ExecutionRunWorkflow` keeps to a single activity
    call:

    1. Vault lookup for SSH credentials (reusing
       :func:`vault_fetch_ssh_credentials`).  ``parent_workflow_id`` is
       passed through so the credential resolution audit log keeps the
       parent context.
    2. Blocking ``paramiko`` SSH session executed on a worker thread via
       :func:`asyncio.to_thread`, with the host/port/user/private_key
       sourced from Vault.
    3. UTF-8 stdout / stderr uploads to
       ``s3://{bucket}/{prefix}/stdout.txt`` and ``stderr.txt``.
    4. Heartbeat emission every :data:`HEARTBEAT_INTERVAL_S` seconds for
       the duration of the SSH session.

    Parameters
    ----------
    runner_id:
        Caller-supplied runner identifier.  Echoed in the result so the
        workflow can correlate retries.  Used for logging only; the
        actual Vault path comes from the ``vault_path`` argument; see
        the parameter docstring below.
    command:
        Shell command to execute on the remote host.  Treated as opaque
        text - the activity wraps it in a ``cd`` / env-export prefix
        when applicable.
    env:
        Environment variables to export before invoking the command.
        Either a mapping of ``str  str`` or a tuple of
        ``(key, value)`` pairs (the message-dataclass shape - frozen
        and hashable).
    artifact_minio_prefix:
        Path under which ``stdout.txt`` and ``stderr.txt`` are stored.
        Accepts ``s3://bucket/path`` or bare ``bucket/path`` forms;
        a missing bucket falls back to
        :data:`src.activities.minio.DEFAULT_BUCKET` (``ai-runs``).
    workdir:
        Optional remote working directory.  When supplied the command
        is wrapped as ``cd {workdir} && {env}{command}``.
    parent_workflow_id:
        Parent ``ExecutionRunWorkflow`` id - propagated to
        :func:`vault_fetch_ssh_credentials` so its error context
        carries the workflow that requested the run.
    vault_path:
        Vault KV-v2 path for this specific runner's credentials, set by the workflow from
        ``runner_resolver.RunnerResolution.vault_path``. Accepts the
        canonical ``vault:ssh/runners/{runner_id}/active`` shape
        produced by the admin SSH-runner CRUD. ``None`` triggers the
        legacy fallback (env-derived single-runner path) so existing
        single-runner deployments keep working unchanged.

    Returns
    -------
    dict
        JSON-serialisable result mirroring
        :class:`temporal_shared.messages.ExecutionRunWorkflowOutput`'s
        fields.
    """
    activity.logger.info(
        "ssh_run_test: runner_id=%s parent=%s prefix=%s workdir=%s "
        "vault_path=%s command=%s",
        runner_id,
        parent_workflow_id,
        artifact_minio_prefix,
        workdir,
        vault_path,
        command[:200],
    )

    bucket, key_prefix = _bucket_and_key_from_prefix(
        artifact_minio_prefix or ""
    )
    stdout_key = (
        f"{key_prefix}/{_STDOUT_NAME}" if key_prefix else _STDOUT_NAME
    )
    stderr_key = (
        f"{key_prefix}/{_STDERR_NAME}" if key_prefix else _STDERR_NAME
    )

    # Step 1 - fetch credentials from Vault.
    # Pass the resolved ``vault_path`` so the admin-panel-managed runner's
    # secret is read (not the global ``ssh/runner/current``).
    try:
        cred = await vault_fetch_ssh_credentials(
            parent_workflow_id or (runner_id or ""),
            vault_path=vault_path,
        )
    except Exception as exc:  # noqa: BLE001 - surface as runner_unreachable
        activity.logger.warning(
            "ssh_run_test: vault credential lookup failed: %s", exc
        )
        return {
            "status": "failed",
            "exit_code": None,
            "stdout_uri": None,
            "stderr_uri": None,
            "duration_seconds": 0.0,
            "runner_id": runner_id,
            "failure_reason": "runner_unreachable",
        }

    env_prefix = _shell_quote_environment(env)
    full_command = _build_full_command(
        command, workdir=workdir, env_prefix=env_prefix
    )

    activity.logger.info(
        "ssh_run_test: executing on host=%s port=%d user=%s",
        cred.host,
        cred.port,
        cred.user,
    )

    # Step 2/4 - run the command in a thread, heartbeat every 30 s.
    started_at = time.monotonic()
    timeout_seconds = _resolve_command_timeout_seconds()

    ssh_task = asyncio.create_task(
        asyncio.to_thread(
            _ssh_execute_command,
            cred.host,
            cred.port,
            cred.user,
            cred.private_key,
            command,
            workdir or "",
            timeout_seconds,
        )
        if not env_prefix and not workdir
        else asyncio.to_thread(
            _ssh_execute_command,
            cred.host,
            cred.port,
            cred.user,
            cred.private_key,
            full_command,
            "",  # workdir is already encoded in full_command
            timeout_seconds,
        )
    )

    async def _heartbeat_loop() -> None:
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                # Emit progress so Temporal can resume the activity on
                # another worker after a crash.  The payload doubles as
                # an audit breadcrumb for long-running test commands.
                activity.heartbeat(
                    {
                        "runner_id": runner_id,
                        "elapsed_seconds": time.monotonic() - started_at,
                    }
                )
        except asyncio.CancelledError:
            return

    heartbeat_task = asyncio.create_task(_heartbeat_loop())

    status: str
    exit_code: int | None
    stdout_text: str = ""
    stderr_text: str = ""
    failure_reason: str | None = None

    try:
        try:
            run_result: RunResult = await ssh_task
            stdout_text = run_result.stdout
            stderr_text = run_result.stderr
            exit_code = run_result.exit_code
            if exit_code == 0:
                status = "passed"
            else:
                status = "failed"
                failure_reason = "non_zero_exit"
        except SSHActivityError as exc:
            # ``timed out`` cause string is produced by
            # _ssh_execute_command on TimeoutError.
            if "timed out" in (exc.cause or "").lower():
                status = "timeout"
                exit_code = None
                failure_reason = "timeout"
            else:
                status = "failed"
                exit_code = None
                failure_reason = "runner_unreachable"
            stderr_text = exc.cause or str(exc)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            # Heartbeat cancellation is best-effort cleanup.
            pass

    duration_seconds = time.monotonic() - started_at

    # Step 3 - upload stdout / stderr to MinIO.  Failure here flips
    # ``failure_reason`` to ``artifact_upload_failed`` only if the
    # command itself succeeded; a failed command keeps its own reason.
    stdout_uri = await _upload_text_artifact(
        bucket, stdout_key, stdout_text.encode("utf-8")
    )
    stderr_uri = await _upload_text_artifact(
        bucket, stderr_key, stderr_text.encode("utf-8")
    )

    if (stdout_uri is None or stderr_uri is None) and status == "passed":
        # Successful test, but artifacts are missing - flag so the
        # caller can decide whether to treat as a soft failure.
        status = "failed"
        failure_reason = "artifact_upload_failed"

    return {
        "status": status,
        "exit_code": exit_code,
        "stdout_uri": stdout_uri,
        "stderr_uri": stderr_uri,
        "duration_seconds": duration_seconds,
        "runner_id": runner_id,
        "failure_reason": failure_reason,
    }
