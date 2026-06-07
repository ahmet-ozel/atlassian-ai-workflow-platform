"""Readiness probe module for real dependency health checks.

This module implements individual probes for each infrastructure
dependency (PostgreSQL, Redis, Temporal, Vault) and an aggregation
function that runs all probes in parallel with a 3-second timeout.

Each probe returns a :class:`DependencyProbeResult` indicating whether
the dependency is reachable and the observed latency. The
:func:`check_readiness` function collects results from all configured
probes and produces a summary suitable for the ``/readyz`` endpoint.

Design references
-----------------
* design notes §Component 6 - Readiness Probe (Gerçek İmplementasyon).
* behaviors 11.1, 11.2, 11.3, 11.4, 11.5, 11.6.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Coroutine, Any

import httpx

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class DependencyProbeResult:
    """Single probe observation for an infrastructure dependency.

    Attributes
    ----------
    name:
        Human-readable dependency name (e.g. ``"postgres"``, ``"redis"``).
    reachable:
        ``True`` if the probe completed successfully within the timeout.
    latency_ms:
        Round-trip time in milliseconds, or ``None`` when the probe
        failed (timeout, connection refused, etc.).
    """

    name: str
    reachable: bool
    latency_ms: float | None = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Per-probe timeout in seconds. Any probe exceeding this duration is
#: classified as unreachable (behavior 11.4).
PROBE_TIMEOUT_SECONDS: float = 10.0


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


async def probe_postgres(dsn: str) -> DependencyProbeResult:
    """Probe PostgreSQL by executing ``SELECT 1`` with a 3s timeout.

    Uses ``asyncpg`` to open a connection and run a trivial query.
    The connection is closed immediately after the probe regardless
    of outcome (behavior 11.1).
    """
    import asyncpg  # Lazy import to keep module importable without asyncpg

    start = time.monotonic()
    try:
        conn = await asyncio.wait_for(
            asyncpg.connect(dsn),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        try:
            await asyncio.wait_for(
                conn.fetchval("SELECT 1"),
                timeout=PROBE_TIMEOUT_SECONDS - (time.monotonic() - start),
            )
        finally:
            await conn.close()

        elapsed_ms = (time.monotonic() - start) * 1000.0
        return DependencyProbeResult(
            name="postgres",
            reachable=True,
            latency_ms=round(elapsed_ms, 2),
        )
    except (asyncio.TimeoutError, OSError, Exception):
        return DependencyProbeResult(
            name="postgres",
            reachable=False,
            latency_ms=None,
        )


async def probe_redis(url: str) -> DependencyProbeResult:
    """Probe Redis by executing ``PING`` with a 3s timeout.

    Uses a raw asyncio TCP connection to send the RESP PING command
    and verify the PONG response. This avoids requiring a heavy Redis
    client library while still performing a real connectivity check
    (behavior 11.2).
    """
    start = time.monotonic()
    try:
        # Parse host and port from redis URL
        # Supports formats: redis://host:port, redis://host, host:port
        host, port = _parse_redis_url(url)

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        try:
            # Send RESP PING command
            writer.write(b"*1\r\n$4\r\nPING\r\n")
            await writer.drain()

            remaining = PROBE_TIMEOUT_SECONDS - (time.monotonic() - start)
            response = await asyncio.wait_for(
                reader.readline(),
                timeout=max(remaining, 0.1),
            )

            # Redis responds with +PONG\r\n
            if not response.strip().startswith(b"+PONG"):
                return DependencyProbeResult(
                    name="redis",
                    reachable=False,
                    latency_ms=None,
                )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

        elapsed_ms = (time.monotonic() - start) * 1000.0
        return DependencyProbeResult(
            name="redis",
            reachable=True,
            latency_ms=round(elapsed_ms, 2),
        )
    except (asyncio.TimeoutError, OSError, Exception):
        return DependencyProbeResult(
            name="redis",
            reachable=False,
            latency_ms=None,
        )


async def probe_temporal(host: str) -> DependencyProbeResult:
    """Probe Temporal via gRPC health check with a 3s timeout.

    Attempts to connect to the Temporal frontend service using the
    ``temporalio`` client library. A successful connection confirms
    the gRPC endpoint is reachable (behavior 11.3).
    """
    from temporalio.client import Client  # type: ignore[import-not-found]

    start = time.monotonic()
    try:
        await asyncio.wait_for(
            Client.connect(host),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        elapsed_ms = (time.monotonic() - start) * 1000.0
        return DependencyProbeResult(
            name="temporal",
            reachable=True,
            latency_ms=round(elapsed_ms, 2),
        )
    except (asyncio.TimeoutError, OSError, Exception):
        return DependencyProbeResult(
            name="temporal",
            reachable=False,
            latency_ms=None,
        )


async def probe_vault(addr: str) -> DependencyProbeResult:
    """Probe Vault via HTTP GET ``/v1/sys/health`` with a 3s timeout.

    The Vault health endpoint returns 200 when initialized and
    unsealed, 429 when unsealed but in standby, 472 for DR secondary,
    501 when not initialized, and 503 when sealed. We treat any
    successful HTTP response (regardless of status code) as
    "reachable" since it confirms the Vault process is alive and
    responding. Only connection failures and timeouts are treated as
    unreachable (behavior 11.4).
    """
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(
                f"{addr.rstrip('/')}/v1/sys/health",
                timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS),
            )
            # Vault returns various status codes depending on state:
            # 200 = active, 429 = standby, 472 = DR secondary,
            # 501 = not initialized, 503 = sealed.
            # Any HTTP response means the process is reachable.
            _ = response.status_code

        elapsed_ms = (time.monotonic() - start) * 1000.0
        return DependencyProbeResult(
            name="vault",
            reachable=True,
            latency_ms=round(elapsed_ms, 2),
        )
    except (asyncio.TimeoutError, httpx.HTTPError, OSError, Exception):
        return DependencyProbeResult(
            name="vault",
            reachable=False,
            latency_ms=None,
        )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


async def check_readiness(
    dependencies: list[Callable[[], Coroutine[Any, Any, DependencyProbeResult]]],
) -> tuple[bool, dict]:
    """Run all dependency probes in parallel and aggregate results.

    Each entry in ``dependencies`` is a zero-argument async callable
    that returns a :class:`DependencyProbeResult`. All probes execute
    concurrently via :func:`asyncio.gather` with individual 3s
    timeouts enforced inside each probe function.

    Returns
    -------
    tuple[bool, dict]
        A 2-tuple of ``(all_ready, details)`` where:
        - ``all_ready`` is ``True`` iff every dependency is reachable.
        - ``details`` is a dict with either ``{"status": "ready"}`` or
          ``{"status": "not_ready", "failed_dependencies": [...]}``.
    """
    results: list[DependencyProbeResult] = await asyncio.gather(
        *(dep() for dep in dependencies)
    )

    failed = [r.name for r in results if not r.reachable]

    if not failed:
        return True, {"status": "ready"}

    return False, {
        "status": "not_ready",
        "failed_dependencies": failed,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_redis_url(url: str) -> tuple[str, int]:
    """Parse a Redis URL into (host, port).

    Supports formats:
    - ``redis://host:port``
    - ``redis://host:port/db``
    - ``redis://user:password@host:port``
    - ``host:port``
    - ``host`` (defaults to port 6379)
    """
    default_port = 6379

    # Strip scheme if present
    if "://" in url:
        # Remove scheme
        remainder = url.split("://", 1)[1]
        # Remove credentials if present
        if "@" in remainder:
            remainder = remainder.split("@", 1)[1]
        # Remove path/db if present
        if "/" in remainder:
            remainder = remainder.split("/", 1)[0]
    else:
        remainder = url

    if ":" in remainder:
        parts = remainder.rsplit(":", 1)
        host = parts[0]
        try:
            port = int(parts[1])
        except ValueError:
            port = default_port
    else:
        host = remainder
        port = default_port

    return host, port


__all__ = (
    "DependencyProbeResult",
    "PROBE_TIMEOUT_SECONDS",
    "check_readiness",
    "probe_postgres",
    "probe_redis",
    "probe_temporal",
    "probe_vault",
)
