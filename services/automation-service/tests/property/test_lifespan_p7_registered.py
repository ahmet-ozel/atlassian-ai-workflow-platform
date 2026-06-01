"""Property 7 — lifespan handler is registered on the FastAPI app.

# Feature: automation-service-wiring, Property 7: Lifespan registered

For every :class:`Settings` instance produced by the strategy,
``create_app(settings).router.lifespan_context is not None`` and the
underlying callable's ``__qualname__`` resolves to the production
``lifespan`` symbol in :mod:`automation_service.app`.

Validates Requirements 1.1, 1.2 and 1.3 of the
``automation-service-wiring`` spec.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings, strategies as st


_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from _lifespan_fakes import app_module  # noqa: E402
from src.config import Settings  # type: ignore[import]  # noqa: E402


def _walk_closure_for_lifespan(fn: object, target_qualname: str) -> bool:
    """Walk the FastAPI/Starlette ``_merge_lifespan_context`` wrapper tree."""

    seen: set[int] = set()

    def _visit(node: object) -> bool:
        if node is None or id(node) in seen:
            return False
        seen.add(id(node))
        qualname = getattr(node, "__qualname__", "")
        if qualname == target_qualname:
            return True
        cells = getattr(node, "__closure__", None) or ()
        for cell in cells:
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if _visit(value):
                return True
        for attr in ("__wrapped__", "func"):
            if _visit(getattr(node, attr, None)):
                return True
        return False

    return _visit(fn)


@given(
    postgres_dsn=st.sampled_from(
        [
            "postgresql://ai:ai@postgres:5432/ai",
            "postgresql://x@1.2.3.4:5432/y",
        ]
    ),
    temporal_host=st.sampled_from(["temporal:7233", "localhost:7233"]),
)
@settings(max_examples=200, deadline=None)
def test_lifespan_registered_for_every_settings(
    postgres_dsn: str, temporal_host: str
) -> None:
    """The production lifespan is wrapped onto every ``create_app`` result."""

    cfg = Settings(postgres_dsn=postgres_dsn, temporal_host=temporal_host)
    app = app_module.create_app(cfg)
    assert app.router.lifespan_context is not None
    assert _walk_closure_for_lifespan(
        app.router.lifespan_context,
        app_module.lifespan.__qualname__,
    )
