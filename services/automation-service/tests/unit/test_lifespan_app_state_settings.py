"""Task 5.2 — ``test_create_app_settings_state`` (automation-service-wiring).

Pins the contract that :func:`automation_service.app.create_app` stashes
the resolved :class:`Settings` instance on ``app.state.settings``. The
production lifespan handler reads ``app.state.settings`` to resolve every
infrastructure connection string (``postgres_dsn``, ``temporal_host``,
``mcp_base_url``, ...), so this attribute is part of the public contract
between :func:`create_app` and :func:`lifespan` (Requirements 1.3 and 7.3
of the ``automation-service-wiring`` spec).
"""

from __future__ import annotations

import sys
from pathlib import Path


# Make the in-tree ``src`` directory importable so ``automation_service``
# resolves under both focused and root-level pytest invocations.
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))


from automation_service.app import create_app  # noqa: E402
from src.config import Settings  # noqa: E402


def test_create_app_settings_state_is_caller_supplied() -> None:
    """A caller-supplied ``Settings`` survives the round trip identically.

    ``create_app(s).state.settings is s`` — the factory MUST NOT clone /
    re-validate the instance. Tests that inject ``Settings`` overrides
    (e.g. an in-memory ``postgres_dsn``) rely on this identity so the
    lifespan handler reads the exact override.
    """

    custom = Settings(
        postgres_dsn="postgresql://test:test@127.0.0.1:5432/test",
        temporal_host="localhost:7233",
    )

    app = create_app(custom)

    assert app.state.settings is custom


def test_create_app_default_settings_state_is_settings_instance() -> None:
    """Omitting the argument leaves ``app.state.settings`` as a Settings.

    The factory must build a default :class:`Settings` from the process
    environment when no override is provided, and that default is the
    object the lifespan handler reads.
    """

    app = create_app()

    assert isinstance(app.state.settings, Settings)
