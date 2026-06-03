"""Test override wins per lifespan slot.

Pins the contract that the production lifespan handler skips populating
any ``app.state.<slot>`` whose value is already set before startup runs.
Tests that hand-build their own ``*EndpointDeps`` containers MUST continue
to observe their own sentinels after the lifespan starts.

Every shared infrastructure object (asyncpg pool, Vault client, audit
logger, Temporal client, ``httpx.AsyncClient``) is replaced with a
hand-rolled fake so the lifespan can run in milliseconds without
touching the network. The fakes implement just enough surface for the
production wiring to construct each ``*EndpointDeps`` container — the
slots we *don't* pre-populate are still expected to receive a fresh
production container.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest


# Bootstrap ``src`` onto ``sys.path`` so the test runs under both
# focused and root-level pytest invocations.
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))


# ``automation_service.__init__`` re-exports the FastAPI singleton as
# ``automation_service.app`` (the attribute shadows the submodule), so
# ``from automation_service import app`` returns the singleton. Reach
# the actual module via ``sys.modules`` after triggering the import.
import automation_service  # noqa: E402,F401  -- triggers the submodule import
app_module = sys.modules["automation_service.app"]


# ---------------------------------------------------------------------------
# Hand-rolled fakes for shared infrastructure
# ---------------------------------------------------------------------------


class _FakePool:
    """In-memory stand-in for :class:`asyncpg.Pool`.

    The lifespan handler keeps a reference, hands the pool to repository
    classes (``ProcessedEventsRepo``, ``DiffSummaryCacheRepo``,
    ``_AsyncpgDepartmentsRepo``, ...) and calls ``close()`` at shutdown.
    None of the per-router wiring helpers actually issue SQL during
    startup, so the fake only has to satisfy attribute presence and the
    async-close contract.
    """

    def __init__(self) -> None:
        self.closed = False

    async def acquire(self) -> Any:  # noqa: D401 - protocol shape
        return object()

    async def release(self, conn: object) -> None:  # noqa: D401
        return None

    async def close(self) -> None:
        self.closed = True


class _FakeVault:
    """Stand-in for :class:`vault_client.VaultClient`.

    The verifier closure in ``_wire_webhooks`` calls
    :func:`vault_client.verify_webhook_hmac` which itself calls
    ``vault.read(...)``; the fake returns ``KeyError`` so the verifier
    falls back to "no secret available". The lifespan never reaches
    that path during startup, but the surface is here for completeness.
    """

    def read(self, path: object) -> dict[str, Any]:  # noqa: D401
        raise KeyError(path)


class _FakeAuditWriter:
    """Stand-in for the asyncpg-backed audit writer used by AuditLogger."""

    async def write(self, event: object) -> None:  # noqa: D401
        return None


class _FakeTemporal:
    """Stand-in for :class:`temporal_client.TemporalClient`.

    ``connect`` is awaited during Phase A; ``close`` is awaited during
    Phase D. The class is duck-typed against the cancel router's
    :class:`SupportsTemporalCancel` protocol so the lifespan can park
    it on ``app.state.cancel.temporal_client`` without complaint.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Accept arbitrary positional / keyword arguments — the production
        # constructor takes ``host`` and ``namespace`` and we don't want
        # the fake to break when those names rotate.
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    def get_workflow_handle(self, workflow_id: str) -> Any:  # noqa: D401
        return object()


class _FakeHttpClient:
    """Stand-in for :class:`httpx.AsyncClient`.

    Only ``aclose`` is invoked during shutdown; the lifespan otherwise
    treats the client as an opaque resource.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Monkeypatch wiring
# ---------------------------------------------------------------------------


async def _fake_create_pool(*args: Any, **kwargs: Any) -> _FakePool:
    return _FakePool()


def _fake_vault_factory(env: object) -> _FakeVault:
    return _FakeVault()


def _patch_shared_infrastructure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every shared-infra constructor with the in-memory fakes.

    The lifespan handler imports each constructor at module level
    (``asyncpg.create_pool``, ``httpx.AsyncClient``, ``TemporalClient``,
    ``vault_factory.make_client``, ``AsyncpgAuditEventsWriter``), so we
    patch them on the :mod:`automation_service.app` module namespace
    rather than on the original source modules. That keeps the
    monkeypatch surface narrow and matches the way production code
    actually resolves the names at startup.

    The OIDC validator constructor reads ``AUTH_PROVIDER`` from the
    environment; flip it to ``local`` so the in-memory dev validator
    is used and no IdP URLs are required at startup.
    """

    monkeypatch.setattr(app_module.asyncpg, "create_pool", _fake_create_pool)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", _FakeHttpClient)
    monkeypatch.setattr(app_module, "TemporalClient", _FakeTemporal)
    monkeypatch.setattr(
        app_module.vault_factory, "make_client", _fake_vault_factory
    )
    # The asyncpg audit writer holds the pool by reference; nothing in
    # the helper's constructor issues SQL so the real class is fine.
    # ``AuditLogger`` likewise just wraps the writer.
    monkeypatch.setenv("AUTH_PROVIDER", "local")


# ---------------------------------------------------------------------------
# The lifespan slot set used by override-preservation tests.
# ---------------------------------------------------------------------------


SLOT_NAMES: tuple[str, ...] = (
    "dept_credentials",
    "admin",
    "webhooks",
    "cancel",
    "repo_sync",
    "po_review",
    "inbound",
    "webhook_v2",
    "webhook_pipeline",
)


@pytest.mark.parametrize("preset_slot", SLOT_NAMES)
@pytest.mark.asyncio
async def test_override_wins(
    preset_slot: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-populated ``app.state.<slot>`` survives lifespan startup.

    For each slot in turn, the test:

    1. Builds a fresh app via :func:`create_app`.
    2. Replaces every shared-infra constructor with an in-memory fake.
    3. Sets ``app.state.<slot>`` to a sentinel ``object()`` instance.
    4. Enters the production lifespan handler.
    5. Asserts the sentinel survives, every *other* slot is populated
       by production wiring, and shutdown still runs cleanly.
    """

    _patch_shared_infrastructure(monkeypatch)

    app = app_module.create_app()
    sentinel = object()
    setattr(app.state, preset_slot, sentinel)

    # The lifespan is registered on the app via FastAPI's
    # ``include_router`` chain. Using the underlying ``@asynccontextmanager``
    # directly keeps the assertion focused on the production handler and
    # avoids the merge wrappers Starlette stacks on
    # ``app.router.lifespan_context``.
    async with app_module.lifespan(app):
        # The pre-populated slot must be untouched.
        assert getattr(app.state, preset_slot) is sentinel, (
            f"production wiring overwrote the test override on slot "
            f"{preset_slot!r}"
        )
        # Every other slot must hold a fresh production container.
        for other in SLOT_NAMES:
            if other == preset_slot:
                continue
            assert getattr(app.state, other, None) is not None, (
                f"production wiring left slot {other!r} empty after "
                f"startup completed"
            )
