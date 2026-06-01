"""Hand-rolled fakes for the ``automation-service-wiring`` property suite.

The lifespan property tests under
``tests/property/test_lifespan_p*.py`` exercise the production
lifespan handler with in-memory fakes for the shared infrastructure
constructors (``asyncpg.create_pool``, ``httpx.AsyncClient``,
``TemporalClient``, ``vault_factory.make_client``). The fakes are
collected here so each property test stays focused on its assertion
rather than on the setup boilerplate.

The fakes are intentionally minimal: they implement just enough
surface for the lifespan's Phase A construction + Phase D shutdown +
per-router ``_wire_*`` helpers to run without a real backend. Every
fake exposes a small handful of observable counters
(``aclose_calls``, ``close_calls``, ``connect_calls``) so a property
test can assert "every owned resource was closed exactly once" or
"every resource was constructed before the failing one was closed".
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


# Bootstrap ``src`` onto ``sys.path``.
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))


# Trigger the submodule import and reach the actual module object via
# ``sys.modules`` — ``automation_service.__init__`` re-exports the
# FastAPI singleton as ``automation_service.app`` so the attribute
# shadows the submodule under ``from automation_service import app``.
import automation_service  # noqa: F401
app_module = sys.modules["automation_service.app"]


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


class FakePool:
    """In-memory stand-in for :class:`asyncpg.Pool`."""

    def __init__(self) -> None:
        self.close_calls = 0
        self.is_closed = False

    async def acquire(self) -> Any:
        return object()

    async def release(self, conn: object) -> None:
        return None

    async def close(self) -> None:
        self.close_calls += 1
        self.is_closed = True


class FakeVault:
    """Stand-in for :class:`vault_client.VaultClient`."""

    def read(self, path: object) -> dict[str, Any]:
        raise KeyError(path)


class FakeTemporal:
    """Stand-in for :class:`temporal_client.TemporalClient`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.connect_calls = 0
        self.close_calls = 0
        self.is_connected = False

    async def connect(self) -> None:
        self.connect_calls += 1
        self.is_connected = True

    async def close(self) -> None:
        self.close_calls += 1
        self.is_connected = False

    def get_workflow_handle(self, workflow_id: str) -> Any:
        return object()


class FakeHttpClient:
    """Stand-in for :class:`httpx.AsyncClient`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.aclose_calls = 0
        self.is_closed = False

    async def aclose(self) -> None:
        self.aclose_calls += 1
        self.is_closed = True


def install_lifespan_fakes(monkeypatch: Any) -> dict[str, Any]:
    """Replace every shared-infra constructor with the in-memory fakes.

    Returns a dict carrying references to the *instances* the lifespan
    constructed so the property tests can assert against close counters
    after the handler exits. The dict's keys mirror the lifespan's own
    ``app.state.*`` attribute names so a property can compare identity
    (``state.pool is fakes["pool"]``).

    The OIDC validator constructor reads ``AUTH_PROVIDER`` from the
    environment; flip it to ``local`` so the in-memory dev validator
    is used and no IdP URLs are required at startup.
    """

    fakes: dict[str, Any] = {}

    async def _make_pool(*args: Any, **kwargs: Any) -> FakePool:
        pool = FakePool()
        fakes["pool"] = pool
        return pool

    def _make_http(*args: Any, **kwargs: Any) -> FakeHttpClient:
        http = FakeHttpClient()
        fakes["http_client"] = http
        return http

    def _make_temporal(*args: Any, **kwargs: Any) -> FakeTemporal:
        temporal = FakeTemporal()
        fakes["temporal"] = temporal
        return temporal

    def _make_vault(env: object) -> FakeVault:
        vault = FakeVault()
        fakes["vault"] = vault
        return vault

    monkeypatch.setattr(app_module.asyncpg, "create_pool", _make_pool)
    monkeypatch.setattr(app_module.httpx, "AsyncClient", _make_http)
    monkeypatch.setattr(app_module, "TemporalClient", _make_temporal)
    monkeypatch.setattr(app_module.vault_factory, "make_client", _make_vault)
    monkeypatch.setenv("AUTH_PROVIDER", "local")

    return fakes
