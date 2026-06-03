"""Every router slot is populated after startup completes.

For any :class:`FastAPI` application produced by :func:`create_app` and
any successful run of the production lifespan startup phase, every slot
in ``SLOT_NAMES`` is set to a non-``None`` instance after startup
completes. The :class:`Settings` instance is sampled by Hypothesis so
the invariant holds across the full configuration space.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

# Make the in-tree property fakes module importable alongside ``src``.
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lifespan_fakes import (  # noqa: E402
    SLOT_NAMES,
    app_module,
    install_lifespan_fakes,
)
from src.config import Settings  # type: ignore[import]  # noqa: E402


def _make_settings(
    postgres_dsn: str,
    temporal_host: str,
    mcp_base_url: str,
    client_source: str,
) -> Settings:
    """Build a :class:`Settings` from sampled wire-shape strings."""

    return Settings(
        postgres_dsn=postgres_dsn,
        temporal_host=temporal_host,
        mcp_base_url=mcp_base_url,
        client_source=client_source,
    )


import asyncio  # noqa: E402


async def _run_property(
    postgres_dsn: str,
    temporal_host: str,
    mcp_base_url: str,
    client_source: str,
) -> None:
    mp = pytest.MonkeyPatch()
    try:
        install_lifespan_fakes(mp)
        cfg = _make_settings(
            postgres_dsn=postgres_dsn,
            temporal_host=temporal_host,
            mcp_base_url=mcp_base_url,
            client_source=client_source,
        )
        app = app_module.create_app(cfg)
        async with app_module.lifespan(app):
            for slot in SLOT_NAMES:
                assert getattr(app.state, slot, None) is not None, (
                    f"production wiring left slot {slot!r} empty after startup"
                )
    finally:
        mp.undo()


@given(
    postgres_dsn=st.sampled_from(
        [
            "postgresql://ai:ai@postgres:5432/ai",
            "postgresql://user:pwd@localhost:5432/test",
            "postgresql://x@10.0.0.1:5432/y",
        ]
    ),
    temporal_host=st.sampled_from(
        [
            "temporal:7233",
            "localhost:7233",
            "10.0.0.1:7233",
        ]
    ),
    mcp_base_url=st.sampled_from(
        [
            "http://atlassian-mcp:8090",
            "http://localhost:8090",
        ]
    ),
    client_source=st.sampled_from(
        [
            "automation-service",
            "automation-service-test",
        ]
    ),
)
@settings(max_examples=200, deadline=None)
def test_every_slot_is_populated_after_startup(
    postgres_dsn: str,
    temporal_host: str,
    mcp_base_url: str,
    client_source: str,
) -> None:
    """Every ``app.state.<slot>`` is non-``None`` after lifespan startup.

    The property holds for every :class:`Settings` instance the
    strategy produces; the assertion enumerates the canonical
    ``SLOT_NAMES`` tuple shared with the expected slot enumeration.

    Uses an explicit :class:`pytest.MonkeyPatch` instance per
    iteration rather than the function-scoped ``monkeypatch`` fixture
    (which Hypothesis would otherwise reuse across all 200 generated
    inputs, leaking patched state). Each call to
    :func:`asyncio.run` enters a fresh event loop so the lifespan's
    ``async with`` block runs to completion before the next iteration
    starts.
    """

    asyncio.run(
        _run_property(
            postgres_dsn=postgres_dsn,
            temporal_host=temporal_host,
            mcp_base_url=mcp_base_url,
            client_source=client_source,
        )
    )
