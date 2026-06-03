"""Readiness probe module for assistant-service.

Implements real dependency probes for PostgreSQL, Redis, and MCP
with a 3-second per-probe timeout. The :func:`check_readiness`
aggregation function runs all probes in parallel and returns a
summary suitable for the ``/readyz`` endpoint.

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
    """Single probe observation for an infrastructure dependency."""

    name: str
    reachable: bool
    latency_ms: float | None = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROBE_TIMEOUT_SECONDS: float = 3.0


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------


async def probe_postgres(dsn: str) -> DependencyProbeResult:
    """Probe PostgreSQL by executing ``SELECT 1`` with a 3s timeout."""
    import asyncpg

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
    and verify the PONG response.
    """
    start = time.monotonic()
    try:
        host, port = _parse_redis_url(url)

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        try:
            writer.write(b"*1\r\n$4\r\nPING\r\n")
            await writer.drain()

            remaining = PROBE_TIMEOUT_SECONDS - (time.monotonic() - start)
            response = await asyncio.wait_for(
                reader.readline(),
                timeout=max(remaining, 0.1),
            )

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


async def probe_mcp(base_url: str) -> DependencyProbeResult:
    """Probe MCP service via HTTP GET to its health endpoint.

    Attempts to reach the MCP server's ``/healthz`` endpoint.
    Any successful HTTP response confirms the service is reachable
    """
    start = time.monotonic()
    try:
        async with httpx.AsyncClient() as client:
            await client.get(
                f"{base_url.rstrip('/')}/healthz",
                timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS),
            )

        elapsed_ms = (time.monotonic() - start) * 1000.0
        return DependencyProbeResult(
            name="mcp",
            reachable=True,
            latency_ms=round(elapsed_ms, 2),
        )
    except (asyncio.TimeoutError, httpx.HTTPError, OSError, Exception):
        return DependencyProbeResult(
            name="mcp",
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
    """Parse a Redis URL into (host, port)."""
    default_port = 6379

    if "://" in url:
        remainder = url.split("://", 1)[1]
        if "@" in remainder:
            remainder = remainder.split("@", 1)[1]
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
