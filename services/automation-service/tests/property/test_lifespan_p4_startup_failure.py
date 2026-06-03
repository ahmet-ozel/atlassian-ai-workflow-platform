"""Startup failure aborts cleanly without partial wiring.

For any startup phase that raises during construction of one of the
shared infrastructure objects (pool, vault, audit, temporal, oidc),
the lifespan handler propagates the exception out of
``__aenter__`` and leaves no router slot populated by production
wiring on ``app.state``. Any resource that was successfully
constructed before the failing one is closed before the exception
propagates.
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
    SLOT_NAMES,
    FakeHttpClient,
    FakePool,
    FakeTemporal,
    FakeVault,
    app_module,
)


class _FailingConstructorError(RuntimeError):
    """Sentinel exception distinguishable from real lifespan errors."""


async def _run_property(failing: str) -> None:
    """Inject a failing constructor and assert clean cleanup.

    ``failing`` selects which resource's constructor raises:

    * ``"pool"`` — ``asyncpg.create_pool`` raises after the HTTP
      client is built; the http client must be closed.
    * ``"vault"`` — ``vault_factory.make_client`` raises after the
      pool is built; pool + http client must both be closed.
    * ``"audit"`` — ``AsyncpgAuditEventsWriter`` raises (simulated
      via ``AuditLogger``); pool + http client closed.
    * ``"temporal"`` — ``TemporalClient.connect()`` raises after the
      audit logger is built; pool + http client closed.
    * ``"oidc"`` — ``OIDCValidator(OIDCConfig.from_env(...))`` raises
      after Temporal is connected; temporal + pool + http client closed.
    """

    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("AUTH_PROVIDER", "local")

        http_client = FakeHttpClient()
        pool: FakePool | None = None
        temporal: FakeTemporal | None = None

        def _make_http(*args: Any, **kwargs: Any) -> FakeHttpClient:
            return http_client

        async def _make_pool(*args: Any, **kwargs: Any) -> FakePool:
            nonlocal pool
            if failing == "pool":
                raise _FailingConstructorError("pool failed")
            pool = FakePool()
            return pool

        def _make_vault(env: object) -> FakeVault:
            if failing == "vault":
                raise _FailingConstructorError("vault failed")
            return FakeVault()

        def _make_temporal(*args: Any, **kwargs: Any) -> FakeTemporal:
            nonlocal temporal
            temporal = FakeTemporal()
            return temporal

        mp.setattr(app_module.asyncpg, "create_pool", _make_pool)
        mp.setattr(app_module.httpx, "AsyncClient", _make_http)
        mp.setattr(app_module, "TemporalClient", _make_temporal)
        mp.setattr(app_module.vault_factory, "make_client", _make_vault)

        if failing == "audit":
            # The AuditLogger wraps the writer; raise from the writer ctor.
            class _BrokenWriter:
                def __init__(self, *, pool: object) -> None:
                    raise _FailingConstructorError("audit writer failed")

            mp.setattr(app_module, "AsyncpgAuditEventsWriter", _BrokenWriter)

        if failing == "temporal":
            class _BrokenTemporal(FakeTemporal):
                async def connect(self) -> None:  # type: ignore[override]
                    raise _FailingConstructorError("temporal connect failed")

            def _make_broken_temporal(*args: Any, **kwargs: Any) -> _BrokenTemporal:
                nonlocal temporal
                temporal = _BrokenTemporal()
                return temporal

            mp.setattr(app_module, "TemporalClient", _make_broken_temporal)

        if failing == "oidc":
            class _BrokenOIDC:
                def __init__(self, *args: Any, **kwargs: Any) -> None:
                    raise _FailingConstructorError("oidc failed")

            mp.setattr(app_module, "OIDCValidator", _BrokenOIDC)

        app = app_module.create_app()

        with pytest.raises(_FailingConstructorError):
            async with app_module.lifespan(app):
                pytest.fail("lifespan unexpectedly yielded after failure")

        # http_client is always constructed first, so its closer must
        # always have run (regardless of which downstream construction
        # raised).
        assert http_client.aclose_calls == 1, (
            f"http_client.aclose called {http_client.aclose_calls} times "
            f"after failing={failing!r}; expected 1"
        )

        # Pool is constructed after http_client; closed if it was built.
        if failing != "pool":
            assert pool is not None
            assert pool.close_calls == 1, (
                f"pool.close called {pool.close_calls} times "
                f"after failing={failing!r}; expected 1"
            )

        # Temporal is connected after the audit logger; closed if connect
        # succeeded (only oidc path keeps temporal in a successfully-
        # connected state before failure).
        if failing == "oidc":
            assert temporal is not None
            assert temporal.close_calls == 1, (
                "temporal.close should run once when OIDC construction fails"
            )

        # No production-wired slot survives.
        for slot in SLOT_NAMES:
            assert getattr(app.state, slot, None) is None, (
                f"slot {slot!r} populated despite failing={failing!r}"
            )
    finally:
        mp.undo()


@given(failing=st.sampled_from(["pool", "vault", "audit", "temporal", "oidc"]))
@settings(max_examples=200, deadline=None)
def test_startup_failure_cleanup(failing: str) -> None:
    """Any startup constructor failure aborts cleanly with prior closers."""

    asyncio.run(_run_property(failing))
