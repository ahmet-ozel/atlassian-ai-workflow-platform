"""Shutdown continues through close errors.

Pins the behavior that a failing close on one owned resource MUST NOT
block the remaining closes from running, and the lifespan handler's
``__aexit__`` MUST return without re-raising so the shutdown grace-period
contract still holds.

The test injects a fake :class:`asyncpg.Pool` whose ``close()`` raises
:class:`RuntimeError("boom")`, runs the production lifespan startup +
shutdown, and asserts that :meth:`httpx.AsyncClient.aclose` and
:meth:`TemporalClient.close` were still awaited exactly once each and
no exception escaped.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


# Bootstrap ``src`` onto ``sys.path``.
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))


import automation_service  # noqa: E402,F401
app_module = sys.modules["automation_service.app"]


# ---------------------------------------------------------------------------
# Fakes - pool whose close() raises, http_client + temporal that count calls
# ---------------------------------------------------------------------------


class _BrokenClosePool:
    """Pool whose ``close()`` raises :class:`RuntimeError`.

    The lifespan's ``_close_quietly`` shutdown primitive logs the
    failure at WARNING and continues to the next closer; this fake is
    the canary that proves it.
    """

    def __init__(self) -> None:
        self.close_calls = 0

    async def acquire(self) -> Any:
        return object()

    async def release(self, conn: object) -> None:
        return None

    async def close(self) -> None:
        self.close_calls += 1
        raise RuntimeError("boom")


class _CountingHttpClient:
    """``httpx.AsyncClient`` stand-in counting ``aclose`` invocations."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.aclose_calls = 0

    async def aclose(self) -> None:
        self.aclose_calls += 1


class _CountingTemporal:
    """``TemporalClient`` stand-in counting ``close`` invocations."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.connect_calls = 0
        self.close_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    def get_workflow_handle(self, workflow_id: str) -> Any:
        return object()


class _FakeVault:
    def read(self, path: object) -> dict[str, Any]:
        raise KeyError(path)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_continues_through_close_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ``pool.close()`` MUST NOT block ``http_client.aclose()``.

    Three resources are wired so the test can verify every shutdown step
    runs exactly once:

    * a pool whose ``close()`` raises ``RuntimeError`` - the canary
    * a counting ``httpx.AsyncClient`` - must still be ``aclose()``-d
    * a counting ``TemporalClient`` - must still be ``close()``-d

    The assertion is two-fold: every closer is awaited exactly once,
    *and* the lifespan ``__aexit__`` returns without raising.
    """

    pool = _BrokenClosePool()
    http_client = _CountingHttpClient()
    temporal = _CountingTemporal()

    async def _make_pool(*args: Any, **kwargs: Any) -> _BrokenClosePool:
        return pool

    def _make_http(*args: Any, **kwargs: Any) -> _CountingHttpClient:
        return http_client

    def _make_temporal(*args: Any, **kwargs: Any) -> _CountingTemporal:
        return temporal

    monkeypatch.setattr(app_module.asyncpg, "create_pool", _make_pool)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", _make_http)
    monkeypatch.setattr(app_module, "TemporalClient", _make_temporal)
    monkeypatch.setattr(
        app_module.vault_factory,
        "make_client",
        lambda env: _FakeVault(),
    )
    monkeypatch.setenv("AUTH_PROVIDER", "local")

    app = app_module.create_app()

    # The async-context-manager protocol below MUST exit cleanly even
    # though ``pool.close()`` raises inside the ``finally`` block.
    async with app_module.lifespan(app):
        pass

    # Every closer was invoked exactly once.
    assert pool.close_calls == 1, (
        f"pool.close called {pool.close_calls} times; expected 1"
    )
    assert http_client.aclose_calls == 1, (
        f"http_client.aclose called {http_client.aclose_calls} times; "
        "expected 1 even after the pool close raised"
    )
    assert temporal.close_calls == 1, (
        f"temporal.close called {temporal.close_calls} times; expected 1"
    )
