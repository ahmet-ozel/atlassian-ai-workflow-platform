"""``BotLicenseHardCap`` (`operations surface` bot license cap wiring).


Concurrency-bounded queue that gates bot-license-paid actions so
the platform never exceeds the licensed concurrent-bot count. The
class is small on purpose - it only provides:

* :meth:`acquire` - async context manager; blocks until a slot is
  free, then yields. Cancellable via task cancellation.
* :meth:`stats` - current ``(in_use, capacity, queued)``.

invariant 19 (``test_bot_license_hard_cap.py``) pins:
(a) capacity bound, (b) FIFO fairness, (c) idempotent release on
context manager exit, (d) deterministic stats snapshot.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass

__all__ = ["BotLicenseHardCap", "BotLicenseStats"]


@dataclass(frozen=True, slots=True)
class BotLicenseStats:
    """Snapshot of the cap's current utilisation."""

    in_use: int
    capacity: int
    queued: int

    @property
    def saturated(self) -> bool:
        return self.in_use >= self.capacity


class BotLicenseHardCap:
    """Async semaphore tagged with FIFO-fair stats reporting.

    The implementation is a bounded :class:`asyncio.Semaphore` plus a
    small monotonic counter for the queued-acquirer count. Python's
    semaphore is FIFO-fair by virtue of its ``waiters`` deque, which
    matches the invariant-test fairness invariant.

    Args:
        capacity: Maximum number of concurrent bot-license-paid
            actions. Must be ``>= 1``; ``0`` would deadlock the
            entire workflow surface and is rejected at construction.
    """

    def __init__(self, *, capacity: int) -> None:
        if capacity < 1:
            raise ValueError(
                f"BotLicenseHardCap capacity must be >= 1; got {capacity}"
            )
        self._capacity = capacity
        self._sem = asyncio.Semaphore(capacity)
        self._queued = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    def stats(self) -> BotLicenseStats:
        # ``Semaphore._value`` is private but stable across Python
        # versions; we read it under no lock because the caller
        # treats the snapshot as best-effort. The test suite asserts
        # equality with explicit ``acquire`` / ``release`` traces.
        in_use = self._capacity - self._sem._value  # type: ignore[attr-defined]
        return BotLicenseStats(
            in_use=in_use,
            capacity=self._capacity,
            queued=self._queued,
        )

    @asynccontextmanager
    async def acquire(self):
        """Block until a slot is free, then yield to the caller."""

        self._queued += 1
        try:
            await self._sem.acquire()
        finally:
            self._queued -= 1
        try:
            yield
        finally:
            # Idempotent release: the semaphore guarantees a single
            # release per acquire even if the caller raises during
            # the with-body.
            self._sem.release()
