"""Task 5.1 — ``test_lifespan_registration`` (automation-service-wiring).

Pins the contract that :func:`automation_service.app.create_app` returns a
:class:`fastapi.FastAPI` whose ``router.lifespan_context`` resolves to the
production :func:`automation_service.app.lifespan` callable. Validates
Requirements 1.1, 1.2 and 1.3 of the ``automation-service-wiring`` spec —
the lifespan handler must be defined, must be registered via the
``lifespan=`` keyword on the ``FastAPI(...)`` constructor and must be
reachable through the application's router.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Make the in-tree ``src`` directory importable so ``automation_service``
# resolves under both focused and root-level pytest invocations. This
# mirrors the bootstrap used by the sibling lifespan unit tests
# (``tests/unit/test_lifespan_close_quietly.py``).
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))


from automation_service.app import create_app, lifespan  # noqa: E402


def _walk_closure_for_lifespan(fn: object, target_qualname: str) -> bool:
    """Return ``True`` when *target_qualname* appears in *fn*'s closure tree.

    FastAPI wraps every sub-router-attached lifespan in
    :func:`fastapi.routing._merge_lifespan_context`, which stashes the
    nested handlers inside the wrapper's closure cells.  We walk those
    cells recursively to locate the production ``lifespan`` symbol —
    that proves Requirement 1.2 (``lifespan=lifespan`` was passed to
    ``FastAPI(...)``) even when FastAPI's include_router calls layer
    additional ``_merge_lifespan_context`` shells on top.
    """

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
        # ``functools.wraps`` chains and ``functools.partial`` wrappers.
        for attr in ("__wrapped__", "func"):
            nested = getattr(node, attr, None)
            if _visit(nested):
                return True
        return False

    return _visit(fn)


def test_lifespan_registered() -> None:
    """The production lifespan is attached to the ``FastAPI`` app.

    Validates Requirements 1.1 + 1.2 + 1.3: the ``lifespan`` keyword
    argument on the ``FastAPI(...)`` constructor surfaces the production
    callable through ``router.lifespan_context``, and the wrapped
    handler chain contains the module's :func:`lifespan` symbol
    (FastAPI's :func:`include_router` calls stack
    ``_merge_lifespan_context`` shells on top of the user-supplied
    handler, so we walk the closure tree to locate the original).
    """

    app = create_app()

    # Requirement 1.3 — the application's router carries a lifespan
    # context (Starlette wraps the ``@asynccontextmanager`` factory in
    # a ``_AsyncLiftContextManager`` and stores it here).
    assert app.router.lifespan_context is not None

    # Requirement 1.2 — the production ``lifespan`` callable must be
    # reachable through the wrapper chain. The merge-lifespan wrapping
    # makes a direct identity check brittle (FastAPI re-wraps on every
    # ``include_router``), so we walk the closure cells until we find
    # a node whose ``__qualname__`` matches the production symbol's.
    assert _walk_closure_for_lifespan(
        app.router.lifespan_context,
        lifespan.__qualname__,
    ), (
        "create_app().router.lifespan_context does not contain the "
        f"production lifespan callable (qualname={lifespan.__qualname__!r}). "
        "Did the FastAPI(...) constructor lose its lifespan= keyword?"
    )
