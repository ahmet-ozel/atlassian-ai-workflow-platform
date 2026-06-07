"""Docker activity module for the execution-runner-worker.

Provides Temporal activity functions for Docker container lifecycle
management: build, run, log collection, stop, cleanup, and healthcheck.

All Docker commands are executed on the remote SSH runner via paramiko.
SSH connections use the same credential resolution pattern as the
existing ssh.py and cleanup.py modules (Vault-based SSH credentials).

Activities:
- docker_build_image: Build a Docker image from a Dockerfile (300s timeout)
- docker_run_container: Run a container with resource limits and env vars
- docker_collect_logs: Collect container logs and upload to MinIO (max 50MB)
- docker_stop_container: Stop a container with configurable grace period
- docker_cleanup_container: Remove container/image based on cleanup policy
- docker_daemon_healthcheck: Check Docker daemon availability
"""

from __future__ import annotations

import asyncio
import io
import shlex
import time
from dataclasses import dataclass
from typing import Any, Literal

from temporalio import activity

__all__ = [
    "DockerBuildInput",
    "DockerBuildResult",
    "DockerRunInput",
    "DockerRunResult",
    "DockerCleanupInput",
    "DockerHealthcheckInput",
    "docker_build_image",
    "docker_run_container",
    "docker_collect_logs",
    "docker_stop_container",
    "docker_cleanup_container",
    "docker_daemon_healthcheck",
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DockerBuildInput:
    """Input parameters for docker_build_image activity.

    Attributes
    ----------
    dockerfile_path : str
        Path to the Dockerfile on the SSH runner.
    image_tag : str
        Tag to assign to the built image (e.g. ``myapp:latest``).
    workspace_path : str
        Build context directory on the SSH runner.
    build_timeout_seconds : int
        Maximum time for the build operation. Defaults to 300s.
    workflow_id : str
        Parent workflow ID for audit/logging context.
    """

    dockerfile_path: str
    image_tag: str
    workspace_path: str
    build_timeout_seconds: int = 300
    workflow_id: str = ""
    runner_id: str | None = None
    vault_path: str | None = None


@dataclass(frozen=True)
class DockerBuildResult:
    """Result of a Docker image build operation.

    Attributes
    ----------
    success : bool
        Whether the build completed successfully.
    image_id : str | None
        The built image ID (sha256 digest), or None on failure.
    error : str | None
        Error message if the build failed, None on success.
    duration_seconds : float
        Wall-clock time spent on the build operation.
    """

    success: bool
    image_id: str | None
    error: str | None
    duration_seconds: float


@dataclass(frozen=True)
class DockerRunInput:
    """Input parameters for docker_run_container activity.

    Attributes
    ----------
    image : str
        Docker image to use (e.g. ``python:3.12-slim``).
    command : str
        Shell command to execute inside the container.
    workspace_path : str
        Path to mount as the working directory inside the container.
    environment : dict[str, str] | None
        Environment variables to inject into the container via --env.
    cpu_limit : float
        Maximum CPU cores allocated to the container.
    memory_limit_mb : int
        Maximum memory in MB allocated to the container.
    timeout_seconds : int
        Default execution timeout (1800s = 30 min).
    max_timeout_seconds : int
        Maximum allowed timeout (7200s = 2 hours).
    workflow_id : str
        Parent workflow ID for audit/logging context.
    """

    image: str
    command: str
    workspace_path: str
    environment: dict[str, str] | None = None
    cpu_limit: float = 2.0
    memory_limit_mb: int = 2048
    timeout_seconds: int = 1800
    max_timeout_seconds: int = 7200
    workflow_id: str = ""
    runner_id: str | None = None
    vault_path: str | None = None


@dataclass(frozen=True)
class DockerRunResult:
    """Result of a Docker container execution.

    Attributes
    ----------
    container_id : str
        The Docker container ID.
    exit_code : int
        Process exit code from the container.
    stdout : str
        Standard output captured from the container.
    stderr : str
        Standard error captured from the container.
    log_artifact_uri : str | None
        MinIO URI of the uploaded log artifact, or None if upload failed.
    """

    container_id: str
    exit_code: int
    stdout: str
    stderr: str
    log_artifact_uri: str | None


@dataclass(frozen=True)
class DockerCleanupInput:
    """Input parameters for docker_cleanup_container activity.

    Attributes
    ----------
    container_id : str
        The Docker container ID to remove.
    image_id : str | None
        The Docker image ID to remove, or None to skip image removal.
    policy : Literal["on_success", "always", "never"]
        Cleanup policy determining when to remove resources.
    task_succeeded : bool
        Whether the task completed successfully.
    """

    container_id: str
    image_id: str | None
    policy: Literal["on_success", "always", "never"]
    task_succeeded: bool
    workflow_id: str = ""
    runner_id: str | None = None
    vault_path: str | None = None


@dataclass(frozen=True)
class DockerHealthcheckInput:
    """Target context for Docker daemon checks on the SSH runner."""

    workflow_id: str = ""
    runner_id: str | None = None
    vault_path: str | None = None


# ---------------------------------------------------------------------------
# Internal SSH helpers
# ---------------------------------------------------------------------------


def _get_ssh_client(
    host: str, port: int, user: str, private_key: str
) -> Any:
    """Create and connect a paramiko SSH client (blocking).

    Returns the connected client. Caller is responsible for closing.

    Raises
    ------
    RuntimeError
        On connection or authentication failure.
    """
    import paramiko  # noqa: PLC0415

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    key_file = io.StringIO(private_key)
    pkey = None
    for key_class in (paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey):
        try:
            key_file.seek(0)
            pkey = key_class.from_private_key(key_file)
            break
        except paramiko.SSHException:
            continue

    if pkey is None:
        raise RuntimeError("Unable to parse SSH private key")

    client.connect(
        hostname=host,
        port=port,
        username=user,
        pkey=pkey,
        timeout=30.0,
        allow_agent=False,
        look_for_keys=False,
    )
    return client


def _ssh_exec(
    host: str,
    port: int,
    user: str,
    private_key: str,
    command: str,
    timeout_seconds: int = 300,
) -> tuple[str, str, int]:
    """Execute a command on the SSH runner (blocking).

    Returns (stdout, stderr, exit_code).

    Raises
    ------
    RuntimeError
        On connection failure, auth failure, or timeout.
    """
    import paramiko  # noqa: PLC0415

    client = _get_ssh_client(host, port, user, private_key)
    try:
        stdin, stdout_ch, stderr_ch = client.exec_command(
            command, timeout=float(timeout_seconds)
        )
        stdin.close()

        stdout_text = stdout_ch.read().decode("utf-8", errors="replace")
        stderr_text = stderr_ch.read().decode("utf-8", errors="replace")
        exit_code = stdout_ch.channel.recv_exit_status()

        return stdout_text, stderr_text, exit_code
    except paramiko.SSHException as exc:
        raise RuntimeError(f"SSH error: {exc}") from exc
    except TimeoutError as exc:
        raise RuntimeError(
            f"Command timed out after {timeout_seconds}s"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Network error: {exc}") from exc
    finally:
        client.close()


async def _get_ssh_credentials(
    workflow_id: str,
    vault_path: str | None = None,
) -> Any:
    """Fetch SSH credentials from Vault.

    Returns an SSHCred dataclass with host, port, user, private_key.
    """
    from src.activities.vault import vault_fetch_ssh_credentials  # noqa: PLC0415

    return await vault_fetch_ssh_credentials(workflow_id, vault_path=vault_path)


# ---------------------------------------------------------------------------
# Temporal activities
# ---------------------------------------------------------------------------


@activity.defn(name="docker_build_image")
async def docker_build_image(input: DockerBuildInput) -> DockerBuildResult:
    """Build a Docker image from a Dockerfile on the SSH runner.

    Executes ``docker build`` via SSH with the specified timeout (default
    300 seconds). Returns the image ID on success or an error description
    on failure.

    Parameters
    ----------
    input:
        Build parameters including Dockerfile path, image tag, workspace
        path, and timeout.

    Returns
    -------
    DockerBuildResult
        Result containing success status, image ID, error, and duration.

    """
    activity.logger.info(
        "docker_build_image: tag=%s dockerfile=%s workspace=%s "
        "timeout=%ds workflow=%s",
        input.image_tag,
        input.dockerfile_path,
        input.workspace_path,
        input.build_timeout_seconds,
        input.workflow_id,
    )

    started_at = time.monotonic()

    try:
        cred = await _get_ssh_credentials(input.workflow_id, input.vault_path)
    except Exception as exc:
        duration = time.monotonic() - started_at
        activity.logger.error(
            "docker_build_image: credential fetch failed: %s", exc
        )
        return DockerBuildResult(
            success=False,
            image_id=None,
            error=f"SSH credential fetch failed: {exc}",
            duration_seconds=duration,
        )

    # Build the docker build command. ``docker build -f`` resolves the
    # Dockerfile path relative to the *current working directory*, not the
    # build-context argument, so we ``cd`` into the workspace first and
    # reference the Dockerfile relative to it (``.`` build context). This
    # makes a relative ``dockerfile_path`` such as ``Dockerfile`` resolve
    # inside the task workspace rather than the SSH login directory.
    dockerfile_arg = shlex.quote(input.dockerfile_path)
    tag_arg = shlex.quote(input.image_tag)
    workspace_arg = shlex.quote(input.workspace_path)

    build_cmd = (
        f"cd {workspace_arg} && "
        f"docker build -t {tag_arg} -f {dockerfile_arg} ."
    )

    try:
        stdout, stderr, exit_code = await asyncio.to_thread(
            _ssh_exec,
            cred.host,
            cred.port,
            cred.user,
            cred.private_key,
            build_cmd,
            input.build_timeout_seconds,
        )
    except RuntimeError as exc:
        duration = time.monotonic() - started_at
        error_msg = str(exc)
        activity.logger.error(
            "docker_build_image: build failed: %s", error_msg
        )
        return DockerBuildResult(
            success=False,
            image_id=None,
            error=error_msg,
            duration_seconds=duration,
        )

    duration = time.monotonic() - started_at

    if exit_code != 0:
        # Determine specific error type
        combined_output = f"{stdout}\n{stderr}".lower()
        if "no such file" in combined_output or "not found" in combined_output:
            error_msg = f"Dockerfile not found: {input.dockerfile_path}"
        elif "timed out" in combined_output:
            error_msg = f"Build timeout after {input.build_timeout_seconds}s"
        else:
            error_msg = stderr[:1000] if stderr else f"Build failed with exit code {exit_code}"

        activity.logger.error(
            "docker_build_image: build_failed - exit_code=%d error=%s",
            exit_code,
            error_msg[:200],
        )
        return DockerBuildResult(
            success=False,
            image_id=None,
            error=error_msg,
            duration_seconds=duration,
        )

    # Extract image ID from docker build output
    # docker build outputs "Successfully built <image_id>" or
    # with BuildKit: "writing image sha256:<hash>"
    image_id: str | None = None
    for line in reversed(stdout.splitlines()):
        line_stripped = line.strip()
        if line_stripped.startswith("sha256:"):
            image_id = line_stripped
            break
        if "successfully built" in line_stripped.lower():
            parts = line_stripped.split()
            if parts:
                image_id = parts[-1]
            break
        if "writing image" in line_stripped.lower():
            parts = line_stripped.split()
            for part in parts:
                if part.startswith("sha256:"):
                    image_id = part
                    break
            if image_id:
                break

    # If we couldn't extract image_id from output, use the tag
    if not image_id:
        image_id = input.image_tag

    activity.logger.info(
        "docker_build_image: success - image_id=%s duration=%.1fs",
        image_id,
        duration,
    )

    return DockerBuildResult(
        success=True,
        image_id=image_id,
        error=None,
        duration_seconds=duration,
    )


@activity.defn(name="docker_run_container")
async def docker_run_container(input: DockerRunInput) -> DockerRunResult:
    """Run a command inside a Docker container on the SSH runner.

    Creates and starts a container with resource limits (CPU, memory),
    volume mounts (workspace), and environment variables. Waits for
    completion or timeout.

    Parameters
    ----------
    input:
        Run parameters including image, command, workspace, env vars,
        resource limits, and timeout.

    Returns
    -------
    DockerRunResult
        Result containing container ID, exit code, stdout, stderr,
        and optional log artifact URI.

    """
    # Enforce max timeout
    effective_timeout = min(input.timeout_seconds, input.max_timeout_seconds)

    activity.logger.info(
        "docker_run_container: image=%s workspace=%s cpu=%.1f mem=%dMB "
        "timeout=%ds workflow=%s",
        input.image,
        input.workspace_path,
        input.cpu_limit,
        input.memory_limit_mb,
        effective_timeout,
        input.workflow_id,
    )

    try:
        cred = await _get_ssh_credentials(input.workflow_id, input.vault_path)
    except Exception as exc:
        activity.logger.error(
            "docker_run_container: credential fetch failed: %s", exc
        )
        return DockerRunResult(
            container_id="",
            exit_code=-1,
            stdout="",
            stderr=f"SSH credential fetch failed: {exc}",
            log_artifact_uri=None,
        )

    # Build docker run command with resource limits and options
    cmd_parts = ["docker run"]

    # Resource limits
    cmd_parts.append(f"--cpus={input.cpu_limit}")
    cmd_parts.append(f"--memory={input.memory_limit_mb}m")

    # Volume mount: workspace as read-write
    workspace_arg = shlex.quote(input.workspace_path)
    cmd_parts.append(f"-v {workspace_arg}:{workspace_arg}:rw")

    # Working directory inside container
    cmd_parts.append(f"-w {workspace_arg}")

    # Environment variables.
    if input.environment:
        for key, value in input.environment.items():
            env_arg = shlex.quote(f"{key}={value}")
            cmd_parts.append(f"--env {env_arg}")

    # Detach mode to get container ID, then wait
    # We use --rm=false so we can inspect logs before cleanup
    cmd_parts.append("--rm=false")

    # Image and command
    cmd_parts.append(shlex.quote(input.image))
    cmd_parts.append(f"sh -c {shlex.quote(input.command)}")

    docker_run_cmd = " ".join(cmd_parts)

    try:
        stdout, stderr, exit_code = await asyncio.to_thread(
            _ssh_exec,
            cred.host,
            cred.port,
            cred.user,
            cred.private_key,
            docker_run_cmd,
            effective_timeout,
        )
    except RuntimeError as exc:
        error_msg = str(exc)
        activity.logger.error(
            "docker_run_container: execution failed: %s", error_msg
        )
        return DockerRunResult(
            container_id="",
            exit_code=-1,
            stdout="",
            stderr=error_msg,
            log_artifact_uri=None,
        )

    # Extract container ID - docker run without -d prints output directly
    # We need to get the container ID. Let's query for the last container.
    container_id = ""
    try:
        cid_stdout, _, cid_exit = await asyncio.to_thread(
            _ssh_exec,
            cred.host,
            cred.port,
            cred.user,
            cred.private_key,
            "docker ps -lq",
            30,
        )
        if cid_exit == 0 and cid_stdout.strip():
            container_id = cid_stdout.strip()
    except RuntimeError:
        pass

    # Upload logs to MinIO
    log_artifact_uri = await _upload_logs_to_minio(
        stdout, input.workflow_id, container_id
    )

    activity.logger.info(
        "docker_run_container: completed - container=%s exit_code=%d "
        "stdout_len=%d stderr_len=%d",
        container_id,
        exit_code,
        len(stdout),
        len(stderr),
    )

    return DockerRunResult(
        container_id=container_id,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        log_artifact_uri=log_artifact_uri,
    )


@activity.defn(name="docker_collect_logs")
async def docker_collect_logs(
    container_id: str, max_bytes: int = 50 * 1024 * 1024
) -> str:
    """Collect logs from a running Docker container.

    Polls ``docker logs`` every 5 seconds and uploads accumulated logs
    to MinIO. Stops when max_bytes (default 50MB) is reached.

    Parameters
    ----------
    container_id:
        The Docker container ID to collect logs from.
    max_bytes:
        Maximum log size in bytes (default 50MB = 50 * 1024 * 1024).

    Returns
    -------
    str
        The MinIO URI of the uploaded log artifact, or empty string
        if upload failed.

    """
    activity.logger.info(
        "docker_collect_logs: container=%s max_bytes=%d",
        container_id,
        max_bytes,
    )

    try:
        cred = await _get_ssh_credentials("")
    except Exception as exc:
        activity.logger.error(
            "docker_collect_logs: credential fetch failed: %s", exc
        )
        return ""

    collected_logs = ""
    total_bytes = 0
    poll_interval = 5  # seconds

    # Collect logs in a loop until container stops or max_bytes reached
    while total_bytes < max_bytes:
        try:
            stdout, _, exit_code = await asyncio.to_thread(
                _ssh_exec,
                cred.host,
                cred.port,
                cred.user,
                cred.private_key,
                f"docker logs {shlex.quote(container_id)} 2>&1",
                30,
            )

            if exit_code != 0:
                # Container may have been removed or doesn't exist
                activity.logger.warning(
                    "docker_collect_logs: docker logs failed for %s",
                    container_id,
                )
                break

            collected_logs = stdout
            total_bytes = len(collected_logs.encode("utf-8"))

            # Truncate if exceeding max_bytes
            if total_bytes > max_bytes:
                collected_logs = collected_logs.encode("utf-8")[:max_bytes].decode(
                    "utf-8", errors="replace"
                )
                total_bytes = max_bytes
                break

        except RuntimeError as exc:
            activity.logger.warning(
                "docker_collect_logs: SSH error: %s", exc
            )
            break

        # Check if container is still running
        try:
            status_stdout, _, status_exit = await asyncio.to_thread(
                _ssh_exec,
                cred.host,
                cred.port,
                cred.user,
                cred.private_key,
                f"docker inspect --format='{{{{.State.Running}}}}' {shlex.quote(container_id)}",
                10,
            )
            if status_exit == 0 and "false" in status_stdout.lower():
                # Container has stopped
                break
        except RuntimeError:
            break

        # Heartbeat to keep the activity alive
        activity.heartbeat({"container_id": container_id, "bytes_collected": total_bytes})

        await asyncio.sleep(poll_interval)

    # Upload collected logs to MinIO
    log_uri = await _upload_logs_to_minio(
        collected_logs, "", container_id
    )

    activity.logger.info(
        "docker_collect_logs: done - container=%s bytes=%d uri=%s",
        container_id,
        total_bytes,
        log_uri or "upload_failed",
    )

    return log_uri or ""


@activity.defn(name="docker_stop_container")
async def docker_stop_container(
    container_id: str, grace_period: int = 30
) -> None:
    """Stop a running Docker container with a grace period.

    Executes ``docker stop -t {grace_period} {container_id}`` on the
    SSH runner. The grace period gives the container time to handle
    SIGTERM before SIGKILL is sent.

    Parameters
    ----------
    container_id:
        The Docker container ID to stop.
    grace_period:
        Seconds to wait before force-killing (default 30s).

    Returns
    -------
    None

    """
    activity.logger.info(
        "docker_stop_container: container=%s grace_period=%ds",
        container_id,
        grace_period,
    )

    try:
        cred = await _get_ssh_credentials("")
    except Exception as exc:
        activity.logger.error(
            "docker_stop_container: credential fetch failed: %s", exc
        )
        raise RuntimeError(
            f"Cannot stop container - SSH credential fetch failed: {exc}"
        ) from exc

    stop_cmd = f"docker stop -t {grace_period} {shlex.quote(container_id)}"

    try:
        stdout, stderr, exit_code = await asyncio.to_thread(
            _ssh_exec,
            cred.host,
            cred.port,
            cred.user,
            cred.private_key,
            stop_cmd,
            grace_period + 30,  # Allow extra time beyond grace period
        )
    except RuntimeError as exc:
        activity.logger.error(
            "docker_stop_container: failed to stop container %s: %s",
            container_id,
            exc,
        )
        raise

    if exit_code != 0:
        activity.logger.warning(
            "docker_stop_container: docker stop returned exit_code=%d "
            "stderr=%s (container may already be stopped)",
            exit_code,
            stderr[:200],
        )
    else:
        activity.logger.info(
            "docker_stop_container: container %s stopped successfully",
            container_id,
        )


@activity.defn(name="docker_cleanup_container")
async def docker_cleanup_container(input: DockerCleanupInput) -> None:
    """Remove a Docker container and optionally its image based on policy.

    Cleanup policy logic:
    - "always": Remove container and image regardless of task result.
    - "on_success": Remove only if task_succeeded is True.
    - "never": Skip all cleanup.

    Parameters
    ----------
    input:
        Cleanup parameters including container ID, image ID, policy,
        and task success status.

    Returns
    -------
    None

    """
    activity.logger.info(
        "docker_cleanup_container: container=%s image=%s policy=%s "
        "task_succeeded=%s",
        input.container_id,
        input.image_id,
        input.policy,
        input.task_succeeded,
    )

    # Determine if cleanup should proceed based on policy
    should_cleanup = _should_perform_cleanup(input.policy, input.task_succeeded)

    if not should_cleanup:
        activity.logger.info(
            "docker_cleanup_container: skipping cleanup - policy=%s "
            "task_succeeded=%s",
            input.policy,
            input.task_succeeded,
        )
        return

    try:
        cred = await _get_ssh_credentials(input.workflow_id, input.vault_path)
    except Exception as exc:
        activity.logger.error(
            "docker_cleanup_container: credential fetch failed: %s", exc
        )
        return  # Best-effort cleanup - don't fail the workflow

    # Remove container
    if input.container_id:
        rm_cmd = f"docker rm -f {shlex.quote(input.container_id)}"
        try:
            _, stderr, exit_code = await asyncio.to_thread(
                _ssh_exec,
                cred.host,
                cred.port,
                cred.user,
                cred.private_key,
                rm_cmd,
                30,
            )
            if exit_code == 0:
                activity.logger.info(
                    "docker_cleanup_container: removed container %s",
                    input.container_id,
                )
            else:
                activity.logger.warning(
                    "docker_cleanup_container: docker rm failed: %s",
                    stderr[:200],
                )
        except RuntimeError as exc:
            activity.logger.warning(
                "docker_cleanup_container: docker rm error: %s", exc
            )

    # Remove image
    if input.image_id:
        rmi_cmd = f"docker rmi {shlex.quote(input.image_id)}"
        try:
            _, stderr, exit_code = await asyncio.to_thread(
                _ssh_exec,
                cred.host,
                cred.port,
                cred.user,
                cred.private_key,
                rmi_cmd,
                30,
            )
            if exit_code == 0:
                activity.logger.info(
                    "docker_cleanup_container: removed image %s",
                    input.image_id,
                )
            else:
                activity.logger.warning(
                    "docker_cleanup_container: docker rmi failed: %s",
                    stderr[:200],
                )
        except RuntimeError as exc:
            activity.logger.warning(
                "docker_cleanup_container: docker rmi error: %s", exc
            )

    activity.logger.info(
        "docker_cleanup_container: cleanup complete for container=%s",
        input.container_id,
    )


@activity.defn(name="docker_daemon_healthcheck")
async def docker_daemon_healthcheck(
    input: DockerHealthcheckInput | dict[str, Any] | None = None,
) -> bool:
    """Check if the Docker daemon is accessible on the SSH runner.

    Executes ``docker info`` on the SSH runner and returns True if the
    command succeeds (exit code 0), indicating the Docker daemon is
    running and accessible.

    Returns
    -------
    bool
        True if Docker daemon is accessible, False otherwise.

    """
    if isinstance(input, dict):
        input = DockerHealthcheckInput(
            workflow_id=str(input.get("workflow_id") or ""),
            runner_id=input.get("runner_id"),
            vault_path=input.get("vault_path"),
        )
    if input is None:
        input = DockerHealthcheckInput()

    activity.logger.info(
        "docker_daemon_healthcheck: checking Docker daemon runner_id=%s",
        input.runner_id,
    )

    try:
        cred = await _get_ssh_credentials(input.workflow_id, input.vault_path)
    except Exception as exc:
        activity.logger.error(
            "docker_daemon_healthcheck: credential fetch failed: %s", exc
        )
        return False

    try:
        stdout, stderr, exit_code = await asyncio.to_thread(
            _ssh_exec,
            cred.host,
            cred.port,
            cred.user,
            cred.private_key,
            "docker info",
            15,  # Short timeout for healthcheck
        )
    except RuntimeError as exc:
        activity.logger.warning(
            "docker_daemon_healthcheck: Docker daemon unreachable: %s", exc
        )
        return False

    if exit_code != 0:
        activity.logger.warning(
            "docker_daemon_healthcheck: Docker daemon unhealthy - "
            "exit_code=%d stderr=%s",
            exit_code,
            stderr[:200],
        )
        return False

    activity.logger.info("docker_daemon_healthcheck: Docker daemon is healthy")
    return True


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _should_perform_cleanup(
    policy: Literal["on_success", "always", "never"], task_succeeded: bool
) -> bool:
    """Determine whether cleanup should be performed based on policy.

    Policy logic:
    - "always": Always cleanup.
    - "on_success": Cleanup only if task succeeded.
    - "never": Never cleanup.

    Parameters
    ----------
    policy:
        The cleanup policy.
    task_succeeded:
        Whether the task completed successfully.

    Returns
    -------
    bool
        True if cleanup should proceed.
    """
    if policy == "always":
        return True
    if policy == "on_success":
        return task_succeeded
    # "never" or unknown
    return False


def build_docker_run_command(input: DockerRunInput) -> str:
    """Build the docker run command string from input parameters.

    This is exposed as a module-level function for testability
    (property tests can verify env var injection without SSH).

    Parameters
    ----------
    input:
        Docker run input parameters.

    Returns
    -------
    str
        The complete docker run command string.
    """
    cmd_parts = ["docker run"]

    # Resource limits
    cmd_parts.append(f"--cpus={input.cpu_limit}")
    cmd_parts.append(f"--memory={input.memory_limit_mb}m")

    # Volume mount
    workspace_arg = shlex.quote(input.workspace_path)
    cmd_parts.append(f"-v {workspace_arg}:{workspace_arg}:rw")

    # Working directory
    cmd_parts.append(f"-w {workspace_arg}")

    # Environment variables.
    if input.environment:
        for key, value in sorted(input.environment.items()):
            env_arg = shlex.quote(f"{key}={value}")
            cmd_parts.append(f"--env {env_arg}")

    cmd_parts.append("--rm=false")

    # Image and command
    cmd_parts.append(shlex.quote(input.image))
    cmd_parts.append(f"sh -c {shlex.quote(input.command)}")

    return " ".join(cmd_parts)


async def _upload_logs_to_minio(
    logs: str, workflow_id: str, container_id: str
) -> str | None:
    """Upload log content to MinIO and return the artifact URI.

    Returns None if upload fails or MinIO credentials are not configured.
    """
    import os  # noqa: PLC0415

    from src.activities.minio import (  # noqa: PLC0415
        DEFAULT_BUCKET,
        _ensure_bucket_exists,
        _minio_access_key,
        _minio_endpoint,
        _minio_secret_key,
        _s3_headers,
    )

    if not logs:
        return None

    endpoint = _minio_endpoint()
    access_key = _minio_access_key()
    secret_key = _minio_secret_key()

    if not access_key or not secret_key:
        activity.logger.warning(
            "docker: MinIO credentials missing; skipping log upload"
        )
        return None

    import httpx  # noqa: PLC0415

    bucket = DEFAULT_BUCKET
    timestamp = int(time.time())
    key_id = container_id or workflow_id or "unknown"
    key = f"docker-logs/{key_id}/{timestamp}/container.log"

    payload = logs.encode("utf-8")

    try:
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
            if 200 <= response.status_code < 300:
                return f"s3://{bucket}/{key}"
            else:
                activity.logger.warning(
                    "docker: MinIO upload failed - status=%d",
                    response.status_code,
                )
                return None
    except Exception as exc:  # noqa: BLE001
        activity.logger.warning(
            "docker: MinIO upload error: %s", exc
        )
        return None
