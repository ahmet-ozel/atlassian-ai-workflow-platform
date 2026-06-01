"""SSH runner healthcheck activity (Requirement — SSH host downtime fallback).

Provides :func:`ssh_healthcheck` — a lightweight Temporal activity that
performs a TCP connect probe against the configured SSH runner host. The
canonical :class:`ExecutionRunWorkflow` calls this activity at the start
of its ``run`` method to detect an unreachable runner *before* committing
to the full ``ssh_run_test`` activity.

When the probe fails the workflow transitions to a "queued" state with
exponential backoff retries (max 30 minutes), posts a Jira bot comment
informing the user, and emits an ``ssh_host_unhealthy`` audit event.

The probe is intentionally minimal — a raw TCP socket connect to
``SSH_HOST:SSH_PORT_DEFAULT`` with a short timeout. It does NOT
authenticate or execute commands; it only validates network reachability.

Single-runner canonical contract
--------------------------------

The platform runs **exactly one** SSH runner host. ``SSH_HOST`` is the
canonical environment variable. ``SSH_HOST_1`` (and the legacy slots
``SSH_HOST_2`` / ``SSH_HOST_3``) are accepted as **deprecated aliases**
for backwards compatibility with existing deployments; new deployments
MUST set ``SSH_HOST``. There is no per-department SSH host override —
all departments share the same runner under ``RUNNER_BASE_PATH``.
"""

from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import dataclass
from typing import Any

from temporalio import activity

__all__ = [
    "SSHHealthcheckInput",
    "SSHHealthcheckResult",
    "ssh_healthcheck",
]


@dataclass(frozen=True)
class SSHHealthcheckInput:
    """Optional target override for the SSH healthcheck probe.

    When the execution workflow resolved a department runner from the
    admin-managed pool, it passes that runner's host/port here so the
    probe checks the same server that ``ssh_run_test`` will use.
    ``None`` preserves the legacy env-derived single-runner fallback.
    """

    host: str | None = None
    port: int | None = None
    runner_id: str | None = None


@dataclass(frozen=True)
class SSHHealthcheckResult:
    """Result of the SSH runner healthcheck probe.

    Attributes
    ----------
    healthy : bool
        ``True`` when the TCP connect probe succeeded within the
        timeout window.
    host : str
        The SSH host that was probed.
    port : int
        The SSH port that was probed.
    error : str | None
        Human-readable error description when ``healthy=False``.
        ``None`` when the probe succeeded.
    """

    healthy: bool
    host: str
    port: int
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "host": self.host,
            "port": self.port,
            "error": self.error,
        }


def _resolve_ssh_host(input: SSHHealthcheckInput | None = None) -> tuple[str, int]:
    """Read the SSH runner host/port from environment.

    Resolution order (single-runner canonical contract):

    1. ``SSH_HOST`` — canonical, single source of truth.
    2. ``SSH_HOST_1`` — deprecated legacy alias kept for backwards
       compatibility with existing deployments. Logged as a deprecation
       warning when consulted without ``SSH_HOST`` set.
    3. ``localhost`` — final fallback.

    ``SSH_HOST_2`` / ``SSH_HOST_3`` are **ignored**; the platform runs
    exactly one runner. New deployments MUST use ``SSH_HOST``.
    """
    if input is not None and input.host:
        port = input.port if input.port is not None else 22
        return input.host.strip(), int(port)

    host = os.environ.get("SSH_HOST", "").strip()
    if not host:
        legacy = os.environ.get("SSH_HOST_1", "").strip()
        if legacy:
            host = legacy
    if not host:
        host = "localhost"
    port_raw = os.environ.get("SSH_PORT_DEFAULT", "22")
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 22
    return host, port


def _resolve_connect_timeout() -> float:
    """Read ``SSH_CONNECT_TIMEOUT_S`` from environment (default 15s).

    The healthcheck uses a shorter timeout than the full SSH activity
    to fail fast — capped at the configured connect timeout.
    """
    raw = os.environ.get("SSH_CONNECT_TIMEOUT_S", "15")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 15.0
    return max(val, 1.0)


def _tcp_probe(host: str, port: int, timeout: float) -> str | None:
    """Perform a blocking TCP connect probe.

    Returns ``None`` on success, or an error string on failure.
    Designed to be called via ``asyncio.to_thread``.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return None  # success
    except OSError as exc:
        return f"TCP connect failed: {exc}"
    finally:
        sock.close()


@activity.defn(name="ssh_healthcheck")
async def ssh_healthcheck(
    input: SSHHealthcheckInput | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Lightweight TCP probe against the SSH runner host.

    Returns a serialisable dict matching :class:`SSHHealthcheckResult`.
    The activity never raises — a failed probe is communicated via
    ``healthy=False`` in the result so the workflow can branch on it
    deterministically.

    The probe runs on a worker thread via ``asyncio.to_thread`` to
    avoid blocking the Temporal worker's event loop.
    """
    if isinstance(input, dict):
        input = SSHHealthcheckInput(
            host=input.get("host"),
            port=input.get("port"),
            runner_id=input.get("runner_id"),
        )

    host, port = _resolve_ssh_host(input)
    timeout = _resolve_connect_timeout()

    activity.logger.info(
        "ssh_healthcheck: probing host=%s port=%d runner_id=%s timeout=%.1fs",
        host,
        port,
        input.runner_id if input is not None else None,
        timeout,
    )

    error = await asyncio.to_thread(_tcp_probe, host, port, timeout)

    if error is None:
        activity.logger.info(
            "ssh_healthcheck: host=%s port=%d is healthy", host, port
        )
        result = SSHHealthcheckResult(healthy=True, host=host, port=port)
    else:
        activity.logger.warning(
            "ssh_healthcheck: host=%s port=%d is UNHEALTHY: %s",
            host,
            port,
            error,
        )
        result = SSHHealthcheckResult(
            healthy=False, host=host, port=port, error=error
        )

    return result.to_dict()
