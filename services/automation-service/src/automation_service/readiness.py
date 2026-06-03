"""Readiness probe module for automation-service.

Implements real dependency probes for PostgreSQL and Temporal
with a 3-second per-probe timeout. The :func:`check_readiness`
aggregation function runs all probes in parallel and returns a
summary suitable for the ``/readyz`` endpoint.

"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Coroutine, Any


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


async def probe_temporal(host: str) -> DependencyProbeResult:
    """Probe Temporal via gRPC health check with a 3s timeout.

    Attempts to connect to the Temporal frontend service using the
    ``temporalio`` client library.
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
