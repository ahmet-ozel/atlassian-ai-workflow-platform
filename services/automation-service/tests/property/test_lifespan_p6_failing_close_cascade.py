"""Property 6 — a failing close does not block the remaining closes.

# Feature: automation-service-wiring, Property 6: Failing close cascade

For any successful startup followed by a shutdown in which an
arbitrary non-empty subset of the owned resources' close coroutines
raise, every other owned resource still has its close coroutine
awaited exactly once. Shutdown returns without re-raising as long as
``_close_quietly`` swallows + logs each failure.

Validates Requirement 4.3 of the ``automation-service-wiring`` spec.
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
    FakeHttpClient,
    FakePool,
    FakeTemporal,
    FakeVault,
    app_module,
)


def _make_failing_pool(failing: set[str]) -> Any:
    class _Pool(FakePool):
        async def close(self) -> None:  # type: ignore[override]
            self.close_calls += 1
            self.is_closed = True
            if "pool" in failing:
                raise RuntimeError("pool close failed")
    return _Pool()


def _make_failing_http(failing: set[str]) -> Any:
    class _Http(FakeHttpClient):
        async def aclose(self) -> None:  # type: ignore[override]
            self.aclose_calls += 1
            self.is_closed = True
            if "http" in failing:
                raise RuntimeError("http close failed")
    return _Http()


def _make_failing_temporal(failing: set[str]) -> Any:
    class _Temporal(FakeTemporal):
        async def close(self) -> None:  # type: ignore[override]
            self.close_calls += 1
            self.is_connected = False
            if "temporal" in failing:
                raise RuntimeError("temporal close failed")
    return _Temporal()


async def _run_property(failing: list[str]) -> None:
    failing_set = set(failing)
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("AUTH_PROVIDER", "local")

        pool = _make_failing_pool(failing_set)
        http_client = _make_failing_http(failing_set)
        temporal = _make_failing_temporal(failing_set)

        async def _make_pool(*args: Any, **kwargs: Any) -> Any:
            return pool

        def _make_http(*args: Any, **kwargs: Any) -> Any:
            return http_client

        def _make_temporal(*args: Any, **kwargs: Any) -> Any:
            return temporal

        mp.setattr(app_module.asyncpg, "create_pool", _make_pool)
        mp.setattr(app_module.httpx, "AsyncClient", _make_http)
        mp.setattr(app_module, "TemporalClient", _make_temporal)
        mp.setattr(
            app_module.vault_factory, "make_client", lambda env: FakeVault()
        )

        app = app_module.create_app()
        async with app_module.lifespan(app):
            pass

        assert pool.close_calls == 1
        assert http_client.aclose_calls == 1
        assert temporal.close_calls == 1
    finally:
        mp.undo()


@given(
    failing=st.lists(
        st.sampled_from(["pool", "http", "temporal"]),
        unique=True,
        min_size=1,
    ),
)
@settings(max_examples=200, deadline=None)
def test_failing_close_cascade(failing: list[str]) -> None:
    """Every other closer still runs exactly once after a failure subset."""

    asyncio.run(_run_property(failing))
