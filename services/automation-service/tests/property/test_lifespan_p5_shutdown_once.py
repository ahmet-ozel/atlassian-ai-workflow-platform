"""Property 5 — shutdown closes every owned resource exactly once.

# Feature: automation-service-wiring, Property 5: Shutdown closes once

For any successful run of the lifespan startup phase followed by an
unconditional shutdown, the handler invokes ``close`` / ``aclose``
exactly once for the asyncpg pool, exactly once for the httpx async
client and exactly once for the Temporal client it constructed during
startup. After shutdown returns, none of those three resources are
observable in an open state via their public ``is_closed`` /
``is_connected`` predicates.

Validates Requirements 4.1, 4.2 and 4.4 of the
``automation-service-wiring`` spec.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings, strategies as st


_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lifespan_fakes import (  # noqa: E402
    app_module,
    install_lifespan_fakes,
)


#: Mid-traffic actions interleaved during the ``yield`` window. Each
#: action is a no-op (the property is structural — close-count
#: invariance regardless of in-flight work) but the strategy still
#: explores arbitrary orderings to catch any state leak.
NON_FAILING_ACTIONS: tuple[str, ...] = (
    "noop_a",
    "noop_b",
    "noop_c",
    "noop_d",
)


async def _run_property(actions: list[str]) -> None:
    mp = pytest.MonkeyPatch()
    try:
        fakes = install_lifespan_fakes(mp)
        app = app_module.create_app()
        async with app_module.lifespan(app):
            for _ in actions:
                # Mid-traffic in-flight work — no-ops here, but the
                # property still asserts shutdown is unaffected.
                await asyncio.sleep(0)

        pool = fakes["pool"]
        http_client = fakes["http_client"]
        temporal = fakes["temporal"]

        assert pool.close_calls == 1, (
            f"pool.close awaited {pool.close_calls} times; expected 1"
        )
        assert http_client.aclose_calls == 1, (
            f"http_client.aclose awaited {http_client.aclose_calls} times; "
            "expected 1"
        )
        assert temporal.close_calls == 1, (
            f"temporal.close awaited {temporal.close_calls} times; "
            "expected 1"
        )

        assert pool.is_closed is True
        assert http_client.is_closed is True
        assert temporal.is_connected is False
    finally:
        mp.undo()


@given(
    actions=st.lists(
        st.sampled_from(NON_FAILING_ACTIONS), max_size=8
    ),
)
@settings(max_examples=200, deadline=None)
def test_shutdown_closes_each_resource_exactly_once(actions: list[str]) -> None:
    """``close`` / ``aclose`` awaited once per owned resource."""

    asyncio.run(_run_property(actions))
