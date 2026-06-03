"""Test overrides on ``app.state.<name>`` survive lifespan startup.

For any subset ``S`` of ``SLOT_NAMES`` pre-populated with sentinel
objects before lifespan startup runs, every slot in ``S`` still holds
its sentinel after startup completes and every slot not in ``S``
holds a fresh production-built container.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st


_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lifespan_fakes import (  # noqa: E402
    SLOT_NAMES,
    app_module,
    install_lifespan_fakes,
)


async def _run_property(preset_slots: list[str]) -> None:
    mp = pytest.MonkeyPatch()
    try:
        install_lifespan_fakes(mp)
        app = app_module.create_app()
        sentinels: dict[str, object] = {
            name: object() for name in preset_slots
        }
        for name, sentinel in sentinels.items():
            setattr(app.state, name, sentinel)
        async with app_module.lifespan(app):
            for slot in SLOT_NAMES:
                value = getattr(app.state, slot, None)
                if slot in sentinels:
                    assert value is sentinels[slot], (
                        f"production wiring overwrote sentinel on {slot!r}"
                    )
                else:
                    assert value is not None, (
                        f"production wiring left {slot!r} empty"
                    )
    finally:
        mp.undo()


@given(
    preset_slots=st.lists(
        st.sampled_from(SLOT_NAMES), unique=True, max_size=len(SLOT_NAMES)
    ),
)
@settings(max_examples=200, deadline=None)
def test_overrides_win(preset_slots: list[str]) -> None:
    """Pre-populated slots survive startup; the rest get production wiring."""

    asyncio.run(_run_property(preset_slots))
