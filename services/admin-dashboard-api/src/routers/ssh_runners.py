"""``SshRunnersRouter`` - SSH runner pool CRUD + department assignment.

Provides the admin API surface for managing the multi-SSH runner pool:

* ``GET  /admin/ssh-runners``                    - list all runners with
  active count and healthcheck cron status.
* ``POST /admin/ssh-runners``                    - create a new runner.
* ``PATCH /admin/ssh-runners/{runner_id}``       - update runner fields.
* ``GET  /admin/departments/{dept_id}/ssh-runners``  - runners assigned to dept.
* ``POST /admin/departments/{dept_id}/ssh-runners``  - update runner assignments.

The runner pool lives in ``infrastructure.ssh_runners`` and assignments
in ``infrastructure.dept_ssh_assignments`` (created by migration
``013_ssh_runner_pool.sql``).

Private keys are written to Vault at
``vault:ssh/runners/{runner_id}/active`` and the ``vault_path`` column
stores the reference. The key material is never returned by the API.

Runner assignment changes emit audit events:
- ``dept_ssh_runner_assigned`` - when a runner is newly assigned.
- ``dept_ssh_runner_unassigned`` - when a runner is removed from a dept.

The ``GET /admin/ssh-runners`` endpoint also verifies that the
``ssh_healthcheck_cron`` Temporal workflow is scheduled and enables it
if not.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import posixpath
import secrets
import shlex
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
import paramiko

from audit_logger import AuditEvent

from ..auth.dependencies import AuthClaims, require_admin

__all__ = ["router", "dept_ssh_router"]

logger = logging.getLogger(__name__)


def _validate_base_path(value: str) -> str:
    """Validate a remote absolute workspace root."""
    path = value.strip()
    if not path.startswith("/") or ".." in path.split("/"):
        raise ValueError("base_path must be an absolute path without '..'")
    return path.rstrip("/") or "/"


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/admin/ssh-runners",
    tags=["ssh-runners"],
    dependencies=[Depends(require_admin)],
)

dept_ssh_router = APIRouter(
    prefix="/admin/departments",
    tags=["ssh-runners"],
    dependencies=[Depends(require_admin)],
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class SshRunnerCreateRequest(BaseModel):
    """Request body for creating a new SSH runner."""

    runner_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Unique identifier for the runner.",
    )
    host: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="SSH host address.",
    )
    port: int = Field(
        default=22,
        ge=1,
        le=65535,
        description="SSH port number.",
    )
    username: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="SSH username for the runner.",
    )
    private_key: str = Field(
        ...,
        min_length=1,
        description="SSH private key (will be stored in Vault).",
    )
    base_path: str = Field(
        default="/var/ai-runner",
        min_length=1,
        max_length=512,
        description="Remote workspace root for task folders.",
    )

    def model_post_init(self, __context: Any) -> None:
        self.base_path = _validate_base_path(self.base_path)


class SshRunnerUpdateRequest(BaseModel):
    """Request body for updating an SSH runner."""

    host: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="New SSH host address.",
    )
    port: int | None = Field(
        default=None,
        ge=1,
        le=65535,
        description="New SSH port number.",
    )
    status: str | None = Field(
        default=None,
        description="New status (active, disabled, quarantine).",
    )
    base_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="New remote workspace root.",
    )

    def model_post_init(self, __context: Any) -> None:
        if self.status is not None and self.status not in (
            "active",
            "disabled",
            "quarantine",
        ):
            raise ValueError(
                "status must be one of: active, disabled, quarantine"
            )
        if self.base_path is not None:
            self.base_path = _validate_base_path(self.base_path)


class DeptSshAssignmentRequest(BaseModel):
    """Request body for updating department runner assignments."""

    runner_ids: list[str] = Field(
        ...,
        description="List of runner_ids to assign to the department.",
    )
    bot_service: str | None = Field(
        default=None,
        description="Optional bot service this SSH assignment belongs to.",
    )
    bot_account_id: str | None = Field(
        default=None,
        description="Optional bot account_id this SSH assignment belongs to.",
    )

    def model_post_init(self, __context: Any) -> None:
        if self.bot_service is not None and self.bot_service not in (
            "jira",
            "bitbucket",
            "confluence",
        ):
            raise ValueError(
                "bot_service must be jira, bitbucket, confluence, or null"
            )


class SshRunnerResponse(BaseModel):
    """Response model for a single SSH runner."""

    runner_id: str
    host: str
    port: int
    username: str
    base_path: str
    vault_path: str
    status: str
    created_at: str
    updated_at: str
    bot_service: str | None = None
    bot_account_id: str | None = None


class SshRunnerListResponse(BaseModel):
    """Response model for the SSH runners list with active count and cron status."""

    active_runners: int = Field(
        description="Number of runners with status 'active'."
    )
    runners: list[SshRunnerResponse]
    healthcheck_cron_scheduled: bool = Field(
        description=(
            "Whether the ssh_healthcheck_cron Temporal workflow is "
            "currently scheduled."
        ),
    )


class DockerSmokeResponse(BaseModel):
    """Result of a real Docker build/run/cleanup smoke on an SSH runner."""

    runner_id: str
    status: str
    exit_code: int
    duration_ms: int
    stdout: str
    stderr: str
    workspace: str


class DeptSshAssignmentResponse(BaseModel):
    """Response model for a department's runner assignments."""

    dept_id: str
    runners: list[SshRunnerResponse]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_pg_pool(request: Request) -> Any:
    """Return the asyncpg pool from app state, or raise 503."""
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "pg_pool_unavailable",
            },
        )
    return pool


def _get_vault_client(request: Request) -> Any:
    """Return the Vault client from app state, or raise 503."""
    vault = getattr(request.app.state, "vault_client", None)
    if vault is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "vault_client_unavailable",
            },
        )
    return vault


def _get_audit_sink(request: Request) -> Any | None:
    """Return the audit sink for writing events."""
    explicit = getattr(request.app.state, "secret_rotation_audit_sink", None)
    if explicit is not None:
        return explicit
    proxy = getattr(request.app.state, "admin_proxy", None)
    if proxy is not None:
        return getattr(proxy, "_audit", None)
    return None


async def _write_audit(
    request: Request,
    *,
    actor: AuthClaims,
    action: str,
    dept_id: str | None,
    resource: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Best-effort audit write that never raises."""
    sink = _get_audit_sink(request)
    if sink is None:
        return
    event = AuditEvent(
        actor_id=actor.sub,
        actor_role="admin",
        dept_id=dept_id,
        action=action,
        resource=resource,
        result="ok",
        timestamp=datetime.now(timezone.utc),
        payload=payload or {},
    )
    try:
        await sink.write(event)
    except Exception as exc:  # noqa: BLE001 - audit must never block
        logger.warning(
            "ssh_runners audit write failed (action=%s): %s",
            action,
            exc,
        )


def _row_to_response(row: Any) -> SshRunnerResponse:
    """Convert a database row to a response model."""
    try:
        base_path = row["base_path"]
    except Exception:  # noqa: BLE001 - backward-compatible fake rows
        base_path = "/var/ai-runner"
    try:
        bot_service = row["bot_service"]
        bot_account_id = row["bot_account_id"]
    except Exception:  # noqa: BLE001
        bot_service = None
        bot_account_id = None
    return SshRunnerResponse(
        runner_id=row["runner_id"],
        host=row["host"],
        port=row["port"],
        username=row["username"],
        base_path=base_path,
        vault_path=row["vault_path"],
        status=row["status"],
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
        bot_service=bot_service,
        bot_account_id=bot_account_id,
    )


async def _write_ssh_secret(
    vault: Any,
    *,
    runner_id: str,
    host: str,
    port: int,
    username: str,
    private_key: str,
) -> None:
    """Write SSH credentials using the richest Vault API available."""
    data = {
        "host": host,
        "port": str(port),
        "user": username,
        "private_key": private_key,
    }
    kv2_writer = getattr(vault, "write_kv2_secret", None)
    if callable(kv2_writer):
        await kv2_writer(path=f"ssh/runners/{runner_id}/active", data=data)
        return
    env_writer = getattr(vault, "write_env_override", None)
    if callable(env_writer):
        await env_writer(
            service_name=f"ssh/runners/{runner_id}",
            key="active",
            value=json.dumps(data, separators=(",", ":")),
        )
        return
    raise AttributeError("vault client has no supported write method")


async def _read_ssh_secret(vault: Any, runner_id: str) -> dict[str, str]:
    """Read the active SSH credential payload for a runner from Vault."""

    kv2_reader = getattr(vault, "read_kv2_secret", None)
    if callable(kv2_reader):
        data = await kv2_reader(path=f"ssh/runners/{runner_id}/active")
        if data:
            return {str(k): str(v) for k, v in data.items()}
    raise KeyError(f"vault:ssh/runners/{runner_id}/active")


def _load_private_key(private_key: str) -> paramiko.PKey:
    """Parse an OpenSSH private key without writing it to disk."""

    key_stream = io.StringIO(private_key)
    loaders = (
        paramiko.Ed25519Key.from_private_key,
        paramiko.RSAKey.from_private_key,
        paramiko.ECDSAKey.from_private_key,
    )
    last_exc: Exception | None = None
    for loader in loaders:
        key_stream.seek(0)
        try:
            return loader(key_stream)
        except Exception as exc:  # noqa: BLE001 - try next key type
            last_exc = exc
    raise ValueError("unsupported_private_key") from last_exc


def _run_docker_smoke_sync(
    *,
    runner: Any,
    secret: dict[str, str],
) -> DockerSmokeResponse:
    """Run a remote Docker build/run/cleanup smoke over SSH."""

    started = datetime.now(timezone.utc)
    runner_id = str(runner["runner_id"])
    base_path = _validate_base_path(str(runner["base_path"] or "/var/ai-runner"))
    run_id = f"admin-docker-smoke-{secrets.token_hex(4)}"
    workspace = posixpath.join(base_path, run_id)
    image_tag = f"{run_id}:latest"

    script = f"""
set -eu
WORK={shlex.quote(workspace)}
IMAGE={shlex.quote(image_tag)}
CONTAINER={shlex.quote(run_id)}
cleanup() {{
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker rmi "$IMAGE" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}}
trap cleanup EXIT
mkdir -p "$WORK"
cat > "$WORK/Dockerfile" <<'EOF'
FROM alpine:3.20
CMD ["sh", "-c", "echo docker-smoke-ok"]
EOF
image_id="$(docker build -q -t "$IMAGE" "$WORK")"
output="$(docker run --name "$CONTAINER" "$IMAGE")"
printf 'workspace=%s\nimage=%s\nimage_id=%s\noutput=%s\ncleanup=done\n' \
  "$WORK" "$IMAGE" "$image_id" "$output"
"""

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=str(secret.get("host") or runner["host"]),
            port=int(secret.get("port") or runner["port"]),
            username=str(secret.get("user") or runner["username"]),
            pkey=_load_private_key(str(secret["private_key"])),
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
        )
        _, stdout, stderr = client.exec_command(
            f"sh -lc {shlex.quote(script)}",
            timeout=180,
        )
        exit_code = int(stdout.channel.recv_exit_status())
        stdout_text = stdout.read().decode("utf-8", errors="replace")
        stderr_text = stderr.read().decode("utf-8", errors="replace")
    finally:
        client.close()

    duration_ms = int(
        (datetime.now(timezone.utc) - started).total_seconds() * 1000
    )
    ok = (
        exit_code == 0
        and "docker-smoke-ok" in stdout_text
        and "cleanup=done" in stdout_text
    )
    return DockerSmokeResponse(
        runner_id=runner_id,
        status="passed" if ok else "failed",
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout=stdout_text[-4000:],
        stderr=stderr_text[-4000:],
        workspace=workspace,
    )


# ---------------------------------------------------------------------------
# SSH Healthcheck Cron - Temporal schedule verification
# ---------------------------------------------------------------------------

#: Workflow ID used for the SSH healthcheck cron schedule.
_SSH_HEALTHCHECK_CRON_SCHEDULE_ID: str = "ssh-healthcheck-cron"

#: Task queue where the SSHHealthcheckCronWorkflow is registered.
_EXECUTION_RUNNER_TASK_QUEUE: str = "execution-runner"

#: Cron expression: every 5 minutes.
_SSH_HEALTHCHECK_CRON_EXPRESSION: str = "*/5 * * * *"


async def _check_and_enable_healthcheck_cron(
    request: Request,
) -> bool:
    """Verify ssh_healthcheck_cron is scheduled; enable if not.

    Returns ``True`` when the schedule is confirmed active (either
    already existed or was just created). Returns ``False`` when
    Temporal is unreachable or the schedule could not be created.
    """
    # Try to get a Temporal client connection. The admin-dashboard-api
    # stores the temporal_host in settings; we connect lazily here.
    try:
        from temporalio.client import (  # type: ignore[import-not-found]
            Client,
            Schedule,
            ScheduleActionStartWorkflow,
            ScheduleSpec,
        )
        from temporalio.service import RPCError  # type: ignore[import-not-found]
    except ImportError:
        logger.info(
            "temporalio SDK not available - cannot verify "
            "ssh_healthcheck_cron schedule"
        )
        return False

    # Resolve Temporal host from app settings
    temporal_host: str = getattr(
        request.app.state, "_temporal_host_for_schedule", ""
    )
    if not temporal_host:
        # Fall back to settings
        try:
            from ..config import Settings

            s = Settings()
            temporal_host = s.temporal_host
        except Exception:  # noqa: BLE001
            temporal_host = "temporal:7233"

    try:
        client = await Client.connect(temporal_host)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ssh_healthcheck_cron: cannot connect to Temporal at %s: %s",
            temporal_host,
            exc,
        )
        return False

    schedule_id = _SSH_HEALTHCHECK_CRON_SCHEDULE_ID

    # Check if schedule already exists
    try:
        handle = client.get_schedule_handle(schedule_id)
        await handle.describe()
        logger.debug(
            "ssh_healthcheck_cron schedule already active (id=%s)",
            schedule_id,
        )
        return True
    except RPCError:
        # Schedule not found - fall through to create
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ssh_healthcheck_cron schedule probe failed: %s - "
            "attempting create",
            exc,
        )

    # Create the schedule
    try:
        schedule = Schedule(
            action=ScheduleActionStartWorkflow(
                "SSHHealthcheckCronWorkflow",
                id=f"{schedule_id}-run",
                task_queue=_EXECUTION_RUNNER_TASK_QUEUE,
            ),
            spec=ScheduleSpec(
                cron_expressions=[_SSH_HEALTHCHECK_CRON_EXPRESSION]
            ),
        )
        await client.create_schedule(schedule_id, schedule)
        logger.info(
            "ssh_healthcheck_cron schedule created: id=%s, cron=%s, "
            "queue=%s",
            schedule_id,
            _SSH_HEALTHCHECK_CRON_EXPRESSION,
            _EXECUTION_RUNNER_TASK_QUEUE,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Failed to create ssh_healthcheck_cron schedule: %s", exc
        )
        return False


# ---------------------------------------------------------------------------
# GET /admin/ssh-runners - list all runners
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="List all SSH runners with active count and healthcheck status",
    response_model=SshRunnerListResponse,
)
async def list_ssh_runners(
    request: Request,
    actor: AuthClaims = Depends(require_admin),
) -> SshRunnerListResponse:
    """Return all SSH runners with active count and healthcheck cron status.

    The response includes:
    - ``active_runners``: count of runners with status ``"active"``.
    - ``runners``: full list of all runners regardless of status.
    - ``healthcheck_cron_scheduled``: whether the ``ssh_healthcheck_cron``
      Temporal workflow is scheduled. If not scheduled, this endpoint
      attempts to enable it.
    """
    pool = _get_pg_pool(request)

    rows = await pool.fetch(
        """
        SELECT runner_id, host, port, username, base_path, vault_path, status,
               created_at, updated_at
        FROM infrastructure.ssh_runners
        ORDER BY created_at ASC
        """
    )

    runners = [_row_to_response(row) for row in rows]
    active_runners = sum(1 for r in runners if r.status == "active")

    # Verify ssh_healthcheck_cron Temporal workflow is scheduled;
    # enable if not. Best-effort - if Temporal is
    # unreachable we report False and the FE can surface a warning.
    healthcheck_cron_scheduled = await _check_and_enable_healthcheck_cron(
        request
    )

    return SshRunnerListResponse(
        active_runners=active_runners,
        runners=runners,
        healthcheck_cron_scheduled=healthcheck_cron_scheduled,
    )


# ---------------------------------------------------------------------------
# POST /admin/ssh-runners - create a new runner
# ---------------------------------------------------------------------------


@router.post(
    "",
    summary="Create a new SSH runner",
    status_code=status.HTTP_201_CREATED,
    response_model=SshRunnerResponse,
)
async def create_ssh_runner(
    request: Request,
    body: SshRunnerCreateRequest = Body(...),
    actor: AuthClaims = Depends(require_admin),
) -> SshRunnerResponse:
    """Create a new SSH runner. The private key is stored in Vault.

    Steps:
    1. Write the private_key to Vault at
       ``vault:ssh/runners/{runner_id}/active``.
    2. Insert the runner row into ``infrastructure.ssh_runners`` with
       the vault_path reference.
    3. Return the created runner (without the private key).
    """
    pool = _get_pg_pool(request)
    vault = _get_vault_client(request)

    # Check for duplicate runner_id
    existing = await pool.fetchval(
        "SELECT 1 FROM infrastructure.ssh_runners WHERE runner_id = $1",
        body.runner_id,
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"runner '{body.runner_id}' already exists",
        )

    # Write the FULL SSH credential shape - ``{host, port, user,
    # private_key}`` - to Vault at
    # ``ssh/runners/{runner_id}/active``. Previously this stored only
    # the private key under a single ``value`` field, but the worker's
    # ``vault_fetch_ssh_credentials`` expects all four keys at the
    # same path. The mismatch meant admin-panel-managed runners could
    # never authenticate because the secret was incomplete.
    vault_path = f"vault:ssh/runners/{body.runner_id}/active"
    try:
        await _write_ssh_secret(
            vault,
            runner_id=body.runner_id,
            host=body.host,
            port=body.port,
            username=body.username,
            private_key=body.private_key,
        )
    except Exception as exc:
        logger.error(
            "Failed to write SSH credentials to Vault for runner %s: %s",
            body.runner_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "vault_write_failed",
                "runner_id": body.runner_id,
                "message": str(exc),
            },
        ) from exc

    # Insert into database
    now = datetime.now(timezone.utc)
    try:
        await pool.execute(
            """
            INSERT INTO infrastructure.ssh_runners
                (runner_id, host, port, username, base_path, vault_path, status,
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, 'active', $7, $7)
            """,
            body.runner_id,
            body.host,
            body.port,
            body.username,
            body.base_path,
            vault_path,
            now,
        )
    except Exception as exc:
        logger.error(
            "Failed to insert SSH runner %s into database: %s",
            body.runner_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "db_insert_failed",
                "runner_id": body.runner_id,
                "message": str(exc),
            },
        ) from exc

    return SshRunnerResponse(
        runner_id=body.runner_id,
        host=body.host,
        port=body.port,
        username=body.username,
        base_path=body.base_path,
        vault_path=vault_path,
        status="active",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )


# ---------------------------------------------------------------------------
# PATCH /admin/ssh-runners/{runner_id} - update a runner
# ---------------------------------------------------------------------------


@router.patch(
    "/{runner_id}",
    summary="Update an SSH runner",
    response_model=SshRunnerResponse,
)
async def update_ssh_runner(
    request: Request,
    runner_id: str,
    body: SshRunnerUpdateRequest = Body(...),
    actor: AuthClaims = Depends(require_admin),
) -> SshRunnerResponse:
    """Update an existing SSH runner's host, port, or status.

    Only provided fields are updated. The private key cannot be
    changed through this endpoint (use the key rotation endpoint).
    """
    pool = _get_pg_pool(request)

    # Verify runner exists
    existing = await pool.fetchrow(
        """
        SELECT runner_id, host, port, username, base_path, vault_path, status,
               created_at, updated_at
        FROM infrastructure.ssh_runners
        WHERE runner_id = $1
        """,
        runner_id,
    )
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"runner '{runner_id}' not found",
        )

    # Build update fields
    updates: dict[str, Any] = {}
    if body.host is not None:
        updates["host"] = body.host
    if body.port is not None:
        updates["port"] = body.port
    if body.status is not None:
        updates["status"] = body.status
    if body.base_path is not None:
        updates["base_path"] = body.base_path

    if not updates:
        # Nothing to update - return current state
        return _row_to_response(existing)

    # Build dynamic UPDATE query
    set_clauses: list[str] = []
    params: list[Any] = []
    param_idx = 1

    for col, val in updates.items():
        set_clauses.append(f"{col} = ${param_idx}")
        params.append(val)
        param_idx += 1

    set_clauses.append(f"updated_at = ${param_idx}")
    now = datetime.now(timezone.utc)
    params.append(now)
    param_idx += 1

    params.append(runner_id)
    query = (
        f"UPDATE infrastructure.ssh_runners "
        f"SET {', '.join(set_clauses)} "
        f"WHERE runner_id = ${param_idx} "
        f"RETURNING runner_id, host, port, username, base_path, vault_path, status, "
        f"created_at, updated_at"
    )

    row = await pool.fetchrow(query, *params)
    return _row_to_response(row)


@router.post(
    "/{runner_id}/docker-smoke",
    summary="Run a Docker build/run/cleanup smoke test on an SSH runner",
    response_model=DockerSmokeResponse,
)
async def run_ssh_runner_docker_smoke(
    request: Request,
    runner_id: str,
    actor: AuthClaims = Depends(require_admin),
) -> DockerSmokeResponse:
    """Run a real Docker build/run/cleanup on the configured runner path."""

    pool = _get_pg_pool(request)
    vault = _get_vault_client(request)
    runner = await pool.fetchrow(
        """
        SELECT runner_id, host, port, username, base_path, vault_path, status,
               created_at, updated_at
        FROM infrastructure.ssh_runners
        WHERE runner_id = $1
        """,
        runner_id,
    )
    if runner is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"runner '{runner_id}' not found",
        )
    if runner["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"runner '{runner_id}' is not active",
        )

    try:
        secret = await _read_ssh_secret(vault, runner_id)
        result = await asyncio.to_thread(
            _run_docker_smoke_sync,
            runner=runner,
            secret=secret,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "ssh_secret_missing",
                "runner_id": runner_id,
                "message": str(exc),
            },
        ) from exc
    except Exception as exc:
        logger.exception("Docker smoke failed for SSH runner %s", runner_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "error": "docker_smoke_failed",
                "runner_id": runner_id,
                "message": str(exc),
            },
        ) from exc

    await _write_audit(
        request,
        actor=actor,
        action="ssh_runner_docker_smoke",
        dept_id=None,
        resource=f"ssh_runner:{runner_id}",
        payload={
            "runner_id": runner_id,
            "status": result.status,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "workspace": result.workspace,
        },
    )
    return result


# ---------------------------------------------------------------------------
# GET /admin/departments/{dept_id}/ssh-runners - dept's assigned runners
# ---------------------------------------------------------------------------


@dept_ssh_router.get(
    "/{dept_id}/ssh-runners",
    summary="List SSH runners assigned to a department",
    response_model=DeptSshAssignmentResponse,
)
async def list_dept_ssh_runners(
    request: Request,
    dept_id: str,
    actor: AuthClaims = Depends(require_admin),
) -> DeptSshAssignmentResponse:
    """Return all SSH runners assigned to the given department."""
    pool = _get_pg_pool(request)

    rows = await pool.fetch(
        """
        SELECT r.runner_id, r.host, r.port, r.username, r.base_path, r.vault_path,
               r.status, r.created_at, r.updated_at,
               a.bot_service, a.bot_account_id
        FROM infrastructure.ssh_runners r
        JOIN infrastructure.dept_ssh_assignments a
            ON a.runner_id = r.runner_id
        WHERE a.dept_id = $1
        ORDER BY a.priority ASC, r.created_at ASC
        """,
        dept_id,
    )

    runners = [_row_to_response(row) for row in rows]
    return DeptSshAssignmentResponse(dept_id=dept_id, runners=runners)


# ---------------------------------------------------------------------------
# POST /admin/departments/{dept_id}/ssh-runners - update assignments
# ---------------------------------------------------------------------------


@dept_ssh_router.post(
    "/{dept_id}/ssh-runners",
    summary="Update SSH runner assignments for a department",
)
async def update_dept_ssh_runners(
    request: Request,
    dept_id: str,
    body: DeptSshAssignmentRequest = Body(...),
    actor: AuthClaims = Depends(require_admin),
) -> dict[str, Any]:
    """Update the set of SSH runners assigned to a department.

    This endpoint performs a full reconciliation:
    1. Fetches current assignments for the department.
    2. Determines which runners to add and which to remove.
    3. Inserts new assignments and deletes removed ones.
    4. Emits audit events for each change:
       - ``dept_ssh_runner_assigned`` for new assignments.
       - ``dept_ssh_runner_unassigned`` for removed assignments.
    """
    pool = _get_pg_pool(request)

    await pool.execute(
        """
        INSERT INTO automation.departments
            (id, display_name, default_language, web_search_enabled, mode)
        VALUES ($1, $1, 'tr', TRUE, 'active')
        ON CONFLICT (id) DO NOTHING
        """,
        dept_id,
    )

    # Validate that all requested runner_ids exist
    if body.runner_ids:
        existing_runners = await pool.fetch(
            """
            SELECT runner_id FROM infrastructure.ssh_runners
            WHERE runner_id = ANY($1::text[])
            """,
            body.runner_ids,
        )
        existing_ids = {row["runner_id"] for row in existing_runners}
        missing = set(body.runner_ids) - existing_ids
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "runners_not_found",
                    "missing_runner_ids": sorted(missing),
                },
            )

    # Get current assignments
    current_rows = await pool.fetch(
        """
        SELECT runner_id FROM infrastructure.dept_ssh_assignments
        WHERE dept_id = $1
        """,
        dept_id,
    )
    current_ids = {row["runner_id"] for row in current_rows}
    desired_ids = set(body.runner_ids)

    to_add = desired_ids - current_ids
    to_remove = current_ids - desired_ids

    now = datetime.now(timezone.utc)

    # Remove unassigned runners
    if to_remove:
        await pool.execute(
            """
            DELETE FROM infrastructure.dept_ssh_assignments
            WHERE dept_id = $1 AND runner_id = ANY($2::text[])
            """,
            dept_id,
            list(to_remove),
        )

    # Add new assignments
    if to_add:
        # Insert with default priority=100
        for runner_id in to_add:
            await pool.execute(
                """
                INSERT INTO infrastructure.dept_ssh_assignments
                    (dept_id, runner_id, bot_service, bot_account_id,
                     priority, assigned_at)
                VALUES ($1, $2, $3, $4, 100, $5)
                ON CONFLICT (dept_id, runner_id) DO NOTHING
                """,
                dept_id,
                runner_id,
                body.bot_service,
                body.bot_account_id,
                now,
            )

    if desired_ids and (body.bot_service or body.bot_account_id):
        await pool.execute(
            """
            UPDATE infrastructure.dept_ssh_assignments
            SET bot_service = $3, bot_account_id = $4
            WHERE dept_id = $1 AND runner_id = ANY($2::text[])
            """,
            dept_id,
            list(desired_ids),
            body.bot_service,
            body.bot_account_id,
        )

    # Emit audit events
    for runner_id in to_add:
        await _write_audit(
            request,
            actor=actor,
            action="dept_ssh_runner_assigned",
            dept_id=dept_id,
            resource=f"department:{dept_id}/runner:{runner_id}",
            payload={
                "dept_id": dept_id,
                "runner_id": runner_id,
                "bot_service": body.bot_service,
                "bot_account_id": body.bot_account_id,
                "assigned_at": now.isoformat(),
            },
        )

    for runner_id in to_remove:
        await _write_audit(
            request,
            actor=actor,
            action="dept_ssh_runner_unassigned",
            dept_id=dept_id,
            resource=f"department:{dept_id}/runner:{runner_id}",
            payload={
                "dept_id": dept_id,
                "runner_id": runner_id,
                "unassigned_at": now.isoformat(),
            },
        )

    return {
        "dept_id": dept_id,
        "assigned": sorted(desired_ids),
        "added": sorted(to_add),
        "removed": sorted(to_remove),
    }
