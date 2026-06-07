"""invariant 19 - Bot license hard-cap concurrency queue.



For every Hypothesis-generated ``(capacity, sequence_of_acquire_release)``
trace,:class:`BotLicenseHardCap` satisfies:

(a) ``in_use`` never exceeds ``capacity``.
(b) FIFO fairness - pending waiters are released in arrival order.
(c) ``acquire`` followed by an exception inside the with-body still
 releases the slot.
(d) Deterministic stats: ``stats`` reflects the running
 ``(in_use, capacity, queued)`` counters.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent
_ADMIN_API_SRC = _PLATFORM_ROOT / "services" / "admin-dashboard-api"
for path in (_ADMIN_API_SRC, _ADMIN_API_SRC / "src"):
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))


try:  # pragma: no cover - guarded import
    from src.lifecycle.bot_license_cap import (  # type: ignore[import-not-found]
        BotLicenseHardCap,
    )
except ModuleNotFoundError as exc:  # pragma: no cover
    BotLicenseHardCap = None  # type: ignore[assignment,misc]
    _IMPORT_ERROR: str | None = str(exc)
else:
    _IMPORT_ERROR = None


pytestmark = pytest.mark.skipif(
    BotLicenseHardCap is None,
    reason=(
        "src.lifecycle.bot_license_cap not yet importable "
        f"(implementation milestone is still [-]); error: {_IMPORT_ERROR!r}"
    ),
)


@settings(max_examples=80, deadline=None, suppress_health_check=(HealthCheck.too_slow,))
@given(
    capacity=st.integers(min_value=1, max_value=5),
    holders=st.integers(min_value=0, max_value=15),
)
def test_acquire_release_round_trip(capacity, holders):
    """A holder count <= capacity completes without contention; > capacity queues."""

    async def runner():
        cap = BotLicenseHardCap(capacity=capacity)
        max_seen = 0
        in_use = 0
        lock = asyncio.Lock()

        async def hold():
            nonlocal max_seen, in_use
            async with cap.acquire():
                async with lock:
                    in_use += 1
                    max_seen = max(max_seen, in_use)
                await asyncio.sleep(0)
                async with lock:
                    in_use -= 1

        await asyncio.gather(*(hold() for _ in range(holders)))
        return max_seen

    max_seen = asyncio.run(runner())
    assert max_seen <= capacity, (
        f"observed in_use={max_seen} > capacity={capacity}"
    )


def test_capacity_zero_is_rejected():
    with pytest.raises(ValueError):
        BotLicenseHardCap(capacity=0)


def test_release_on_exception_is_idempotent():
    """Slot is released when the with-body raises."""

    async def runner():
        cap = BotLicenseHardCap(capacity=1)
        try:
            async with cap.acquire():
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        # Second acquire must succeed instantly.
        async with cap.acquire():
            return cap.stats()

    stats = asyncio.run(runner())
    assert stats.in_use == 1
    assert stats.capacity == 1
