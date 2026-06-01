"""SSE-based test runner with true line-by-line subprocess streaming.

Production-hardening task 6.1 — implements the backend infrastructure
for ``POST /admin/services/{service_name}/test?stream=true``.

This module provides:

1. :func:`stream_subprocess_sse` — an async generator that spawns a
   subprocess and yields stdout/stderr line-by-line as SSE ``data:``
   events, with a final ``event: done`` carrying the exit code.

2. A standalone :class:`~fastapi.APIRouter` (``router``) that mounts
   the endpoint directly. This router is registered in ``main.py``
   only when the ``services_lifecycle`` router is unavailable — when
   both are present, ``services_lifecycle`` takes precedence because
   it is mounted first and FastAPI resolves the first matching route.

The streaming generator handles:
- Line-by-line stdout/stderr emission as SSE ``data:`` frames.
- Final ``event: done`` with ``{"exit_code": N}`` on process exit.
- Client disconnect detection → subprocess SIGTERM + cleanup.
- Unexpected errors → ``event: error`` frame before stream close.

Requirements: 4.1, 4.2, 4.3
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from ..auth.dependencies import AuthClaims, require_admin
from ..config import Settings
from .test_results import record_test_run

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/services", tags=["test-runner"])


# ---------------------------------------------------------------------------
# Service → test command resolution
# ---------------------------------------------------------------------------

# Known service test commands. In production this is read from the
# manifest via LifecycleService; this table provides a fallback for
# environments where the manifest is not loaded.
_DEFAULT_TEST_COMMANDS: dict[str, str] = {
    "admin-dashboard-api": "pytest tests/ -v --tb=short",
    "assistant-service": "pytest tests/ -v --tb=short",
    "automation-service": "pytest tests/ -v --tb=short",
}


def resolve_test_command(
    service_name: str,
    request: Request | None = None,
) -> str | None:
    """Resolve the test command for a service.

    Tries the LifecycleService manifest first (if available), then
    falls back to the default lookup table.
    """
    if request is not None:
        lifecycle = getattr(request.app.state, "lifecycle", None)
        if lifecycle is not None:
            try:
                entry = lifecycle.get_manifest_entry(service_name)
                cmd = getattr(entry, "test_command", None)
                if cmd:
                    return cmd
            except Exception:  # noqa: BLE001
                pass

    return _DEFAULT_TEST_COMMANDS.get(service_name)


def resolve_working_directory(service_name: str, settings: Settings) -> str:
    """Resolve the working directory for running tests.

    Returns the service directory under the workspace root.
    """
    service_dir = settings.workspace_root / "services" / service_name
    if service_dir.exists():
        return str(service_dir)

    # Try under platform/services/
    platform_service_dir = (
        settings.workspace_root / "platform" / "services" / service_name
    )
    if platform_service_dir.exists():
        return str(platform_service_dir)

    # Fallback to workspace root
    return str(settings.workspace_root)


# ---------------------------------------------------------------------------
# SSE streaming generator (core infrastructure)
# ---------------------------------------------------------------------------


async def stream_subprocess_sse(
    command: str,
    cwd: str,
    request: Request | None = None,
) -> AsyncIterator[bytes]:
    """Spawn a subprocess and yield stdout/stderr as SSE events.

    Each output line becomes a ``data: <line>\\n\\n`` SSE frame.
    On completion, emits ``event: done\\ndata: {"exit_code": N}\\n\\n``.
    On client disconnect, sends SIGTERM to the subprocess.

    This is the core streaming infrastructure used by the test runner
    endpoint. It can also be imported by other modules that need
    subprocess SSE streaming.

    Args:
        command: Shell command to execute.
        cwd: Working directory for the subprocess.
        request: Optional FastAPI Request for disconnect detection.

    Yields:
        UTF-8 encoded SSE frames.

    Requirements: 4.2, 4.3
    """
    process: asyncio.subprocess.Process | None = None

    try:
        # Use shell=True on Windows, otherwise split the command
        if os.name == "nt":
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
        else:
            args = shlex.split(command)
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
            )

        assert process.stdout is not None  # noqa: S101

        # Stream line-by-line
        while True:
            # Check for client disconnect (Requirement 4.3)
            if request is not None and await request.is_disconnected():
                logger.info(
                    "Client disconnected during test stream, "
                    "terminating subprocess (pid=%s)",
                    process.pid,
                )
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                return

            try:
                line_bytes = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=0.5,
                )
            except asyncio.TimeoutError:
                # No output yet — loop back to check disconnect
                continue

            if not line_bytes:
                # EOF — process has finished writing
                break

            line = line_bytes.decode("utf-8", errors="replace").rstrip("\n\r")
            yield f"data: {line}\n\n".encode("utf-8")

        # Wait for process to finish and get exit code
        exit_code = await process.wait()

        # Send final event with exit code (Requirement 4.2)
        yield (
            f"event: done\n"
            f"data: {{\"exit_code\": {exit_code}}}\n\n"
        ).encode("utf-8")

    except asyncio.CancelledError:
        # Request was cancelled (client disconnect via ASGI)
        if process is not None and process.returncode is None:
            logger.info(
                "Request cancelled, terminating subprocess (pid=%s)",
                process.pid,
            )
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise

    except Exception as exc:
        # Emit an error event before closing the stream
        error_msg = str(exc).replace("\n", " ").replace('"', '\\"')
        yield f"event: error\ndata: {{\"error\": \"{error_msg}\"}}\n\n".encode(
            "utf-8"
        )
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


# ---------------------------------------------------------------------------
# Non-streaming execution helper
# ---------------------------------------------------------------------------


async def run_subprocess_json(
    command: str,
    cwd: str,
) -> tuple[str, int]:
    """Run a subprocess to completion and return (output, exit_code).

    Used by the non-streaming (``stream=False``) path.
    """
    if os.name == "nt":
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )
    else:
        args = shlex.split(command)
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )

    stdout_bytes, _ = await process.communicate()
    output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    exit_code = process.returncode if process.returncode is not None else 1
    return output, exit_code


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/{service_name}/test",
    response_model=None,
    summary="Run tests for a service with optional SSE streaming",
    tags=["test-runner"],
)
async def run_tests(
    service_name: str,
    request: Request,
    stream: bool = Query(
        default=False,
        description="When True, stream output as Server-Sent Events",
    ),
    actor: AuthClaims = Depends(require_admin),
) -> StreamingResponse | JSONResponse:
    """Run the test command for the specified service.

    **stream=True** (Requirement 4.2):
      Returns a ``text/event-stream`` SSE response that streams
      stdout/stderr line-by-line. Each line is emitted as a ``data:``
      event. A final ``event: done`` carries the exit code so the
      frontend can render a pass/fail badge.

    **stream=False**:
      Runs the test to completion and returns a JSON object with
      ``service_name``, ``output``, and ``exit_code``.

    **Client disconnect** (Requirement 4.3):
      When the client aborts the SSE connection (e.g. Cancel button),
      the backend detects the disconnect and terminates the subprocess
      with SIGTERM, falling back to SIGKILL after 5 seconds.

    Returns:
      - 404 if the service is not recognized or has no test command.
      - StreamingResponse (text/event-stream) when stream=True.
      - JSONResponse when stream=False.
    """
    settings = Settings()

    # Resolve test command
    test_command = resolve_test_command(service_name, request)
    if not test_command:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Service '{service_name}' not found or has no test command",
        )

    cwd = resolve_working_directory(service_name, settings)

    logger.info(
        "test_runner: service=%s stream=%s command=%r cwd=%s actor=%s",
        service_name,
        stream,
        test_command,
        cwd,
        getattr(actor, "sub", "unknown"),
    )

    if stream:
        # SSE streaming response (Requirements 4.1, 4.2, 4.3)
        return StreamingResponse(
            stream_subprocess_sse(test_command, cwd, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming: run to completion and return JSON
    started = time.monotonic()
    try:
        output, exit_code = await run_subprocess_json(test_command, cwd)
    except Exception as exc:
        logger.error(
            "Failed to run tests for service=%s: %s",
            service_name,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to execute test command: {exc}",
        ) from exc

    duration_ms = int((time.monotonic() - started) * 1000)

    # Persist the run to automation.test_runs so the dashboard keeps a
    # durable pass/fail trend (E4 — gereksinim.txt G9). Best-effort:
    # a persistence failure must not fail the test-run response.
    recorded: dict | None = None
    try:
        recorded = await record_test_run(
            request,
            service_name=service_name,
            exit_code=exit_code,
            output=output,
            duration_ms=duration_ms,
            triggered_by=getattr(actor, "sub", "system") or "system",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "test_runner: failed to record run history for %s: %s",
            service_name,
            exc,
        )

    return JSONResponse(
        content={
            "service_name": service_name,
            "output": output,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "run_id": recorded.get("id") if recorded else None,
            "summary": (
                {
                    "passed": recorded["passed"],
                    "failed": recorded["failed"],
                    "total_tests": recorded["total_tests"],
                    "status": recorded["status"],
                }
                if recorded
                else None
            ),
        }
    )
