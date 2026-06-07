"""Unit tests for ``src.routers.capabilities``.
The router exposes two endpoints:
* ``GET /api/v1/departments/capabilities`` - read the cached matrix.
* ``POST /api/v1/departments/{dept_id}/probe/{service}`` - re-run one probe.
These tests inject:
* A :class:`_FakeProber` that records every call and lets each test
  script the response per ``(dept_id, service)`` pair.
* The bundled :class:`InMemoryCapabilityProbeStore` for the cache.
* An override on :func:`require_admin` so the OIDC layer can be
  bypassed while still exercising the FastAPI request pipeline
  through :class:`fastapi.testclient.TestClient`.
The tests do **not** depend on the asyncpg-backed cache adapter from
- the router is wired against the
:class:`SupportsCapabilityProbeStore` protocol so the in-memory variant
is enough to verify routing / serialisation."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# sys.path bootstrap (mirror the pattern other tests in this package use).
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
for _lib in ("audit_logger", "auth-shared", "http-shared"):
    _src = _WORKSPACE_ROOT / "libs" / _lib / "src"
    if _src.is_dir() and str(_src) not in sys.path:
        sys.path.insert(0, str(_src))


from src.auth.dependencies import AuthClaims, require_admin  # noqa: E402
from src.routers.capabilities import (  # noqa: E402
    InMemoryCapabilityProbeStore,
    ProbeResult,
    SUPPORTED_SERVICES,
    router as capabilities_router,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProber:
    """In-memory :class:`SupportsCapabilityProber` stub.

    Each ``probe`` call records the requested ``(dept_id, service)``
    pair and returns the scripted result. By default every probe
    returns ``healthy``; tests override per-pair via
    :meth:`set_result`.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._scripted: dict[tuple[str, str], ProbeResult] = {}
        self._raise: Exception | None = None

    def set_result(
        self,
        *,
        dept_id: str,
        service: str,
        result: ProbeResult,
    ) -> None:
        self._scripted[(dept_id, service)] = result

    def raise_on_next(self, exc: Exception) -> None:
        self._raise = exc

    async def probe(self, *, dept_id: str, service: str) -> ProbeResult:
        self.calls.append((dept_id, service))
        if self._raise is not None:
            err = self._raise
            self._raise = None
            raise err

        scripted = self._scripted.get((dept_id, service))
        if scripted is not None:
            return scripted

        return ProbeResult(
            dept_id=dept_id,
            service=service,
            status="healthy",
            error=None,
            latency_ms=42,
            probed_at=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(
    *,
    prober: _FakeProber | None = None,
    store: InMemoryCapabilityProbeStore | None = None,
    actor_sub: str = "admin-1",
) -> tuple[FastAPI, _FakeProber, InMemoryCapabilityProbeStore]:
    """Build a minimal FastAPI app wired to the capabilities router."""

    app = FastAPI()
    app.include_router(capabilities_router)

    fake_prober = prober if prober is not None else _FakeProber()
    cache = store if store is not None else InMemoryCapabilityProbeStore()
    app.state.capability_prober = fake_prober
    app.state.capability_probe_store = cache

    app.dependency_overrides[require_admin] = lambda: AuthClaims(
        sub=actor_sub,
        groups=("admin",),
    )
    return app, fake_prober, cache


# ---------------------------------------------------------------------------
# GET /api/v1/departments/capabilities - matrix endpoint
# ---------------------------------------------------------------------------


def test_matrix_returns_all_departments_with_six_services() -> None:
    """Empty cache → matrix lists every dept × service cell."""

    app, _prober, _cache = _build_app()
    client = TestClient(app)

    response = client.get("/api/v1/departments/capabilities")

    assert response.status_code == 200, response.text
    body = response.json()

    # supported_services mirrors the constant.
    assert body["supported_services"] == list(SUPPORTED_SERVICES)

    # The bundled config has three departments.
    dept_ids = [d["dept_id"] for d in body["departments"]]
    assert set(dept_ids) == {"payment", "hr", "legal"}

    # Every dept block has exactly the six service keys.
    for dept_block in body["departments"]:
        assert set(dept_block["services"].keys()) == set(SUPPORTED_SERVICES)


def test_matrix_marks_unconfigured_services_as_not_configured() -> None:
    """When the dept config doesn't declare a service, the cell is
    ``not_configured`` - even without a cached row .
    The bundled ``config/departments.json`` declares a non-empty
    ``credential_ref`` under ``bot.{jira,bitbucket,confluence}`` for
    every dept and an ``llm_overrides`` block for ``payment`` only.
    HR and Legal have no LLM override so their ``llm`` cell must be
    ``not_configured`` straight out of the gate."""

    app, _prober, _cache = _build_app()
    client = TestClient(app)

    response = client.get("/api/v1/departments/capabilities")
    body = response.json()

    by_id = {d["dept_id"]: d for d in body["departments"]}

    # HR has no llm_overrides → llm = not_configured.
    assert by_id["hr"]["services"]["llm"]["status"] == "not_configured"
    # Legal has no llm_overrides → same.
    assert by_id["legal"]["services"]["llm"]["status"] == "not_configured"
    # Payment has llm_overrides → unknown until probed.
    assert by_id["payment"]["services"]["llm"]["status"] == "unknown"


def test_matrix_returns_cached_results_when_available() -> None:
    """Cached row → matrix surfaces it verbatim."""

    cache = InMemoryCapabilityProbeStore()
    app, _prober, _cache = _build_app(store=cache)
    client = TestClient(app)

    # Pre-populate one row (synchronous because the in-memory store
    # only mutates a dict; we drive it via TestClient round-trip).
    import asyncio
    asyncio.run(
        cache.upsert(
            ProbeResult(
                dept_id="payment",
                service="jira",
                status="healthy",
                error=None,
                latency_ms=120,
                probed_at=datetime(2025, 1, 2, 3, 4, tzinfo=timezone.utc),
            )
        )
    )

    response = client.get("/api/v1/departments/capabilities")
    body = response.json()
    by_id = {d["dept_id"]: d for d in body["departments"]}
    cell = by_id["payment"]["services"]["jira"]
    assert cell["status"] == "healthy"
    assert cell["latency_ms"] == 120
    assert cell["probed_at"] == "2025-01-02T03:04:00+00:00"


def test_matrix_endpoint_does_not_call_prober() -> None:
    """The GET endpoint reads cache only - no fresh probes ."""

    app, prober, _cache = _build_app()
    client = TestClient(app)

    client.get("/api/v1/departments/capabilities")
    assert prober.calls == []


# ---------------------------------------------------------------------------
# POST /api/v1/departments/{dept_id}/probe/{service} - single probe
# ---------------------------------------------------------------------------


def test_single_probe_returns_fresh_result_and_caches_it() -> None:
    """Happy path: prober is called, result is returned + cached."""

    cache = InMemoryCapabilityProbeStore()
    app, prober, _cache = _build_app(store=cache)
    client = TestClient(app)

    response = client.post("/api/v1/departments/payment/probe/jira")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dept_id"] == "payment"
    assert body["service"] == "jira"
    assert body["status"] == "healthy"
    assert body["latency_ms"] == 42

    # Prober was called exactly once.
    assert prober.calls == [("payment", "jira")]

    # The cache now has a row for the pair.
    import asyncio
    cached = asyncio.run(cache.get_one(dept_id="payment", service="jira"))
    assert cached is not None
    assert cached.status == "healthy"


def test_single_probe_for_unconfigured_service_skips_prober() -> None:
    """When dept config doesn't declare the service, the prober is
    NOT called and the response is ``not_configured`` ."""

    app, prober, _cache = _build_app()
    client = TestClient(app)

    # HR has no llm_overrides → llm probe is not_configured.
    response = client.post("/api/v1/departments/hr/probe/llm")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "not_configured"
    assert body["dept_id"] == "hr"
    assert body["service"] == "llm"
    # Prober was NOT called.
    assert prober.calls == []


def test_ssh_probe_needs_runner_not_bitbucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bitbucket credentials alone must not make SSH/Docker configured."""

    monkeypatch.delenv("EXECUTION_RUNNER_ASSIGNED", raising=False)
    monkeypatch.delenv("EXECUTION_RUNNER_AVAILABLE", raising=False)
    monkeypatch.delenv("SSH_HOST", raising=False)
    app, prober, _cache = _build_app()
    client = TestClient(app)

    response = client.post("/api/v1/departments/payment/probe/ssh")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "not_configured"
    assert prober.calls == []


def test_ssh_probe_runs_when_runner_env_flag_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured runner flag is enough to enable SSH probe."""

    monkeypatch.setenv("EXECUTION_RUNNER_ASSIGNED", "true")
    app, prober, _cache = _build_app()
    client = TestClient(app)

    response = client.post("/api/v1/departments/payment/probe/ssh")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "healthy"
    assert prober.calls == [("payment", "ssh")]


def test_single_probe_unknown_dept_returns_404() -> None:
    """Unknown dept_id → 404."""

    app, _prober, _cache = _build_app()
    client = TestClient(app)

    response = client.post("/api/v1/departments/no-such-dept/probe/jira")
    assert response.status_code == 404


def test_single_probe_unsupported_service_returns_400() -> None:
    """Service outside the enum → 400 with stable detail."""

    app, _prober, _cache = _build_app()
    client = TestClient(app)

    response = client.post("/api/v1/departments/payment/probe/elasticsearch")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "unsupported_service"
    assert detail["service"] == "elasticsearch"
    assert set(detail["supported"]) == set(SUPPORTED_SERVICES)


def test_single_probe_503_when_prober_unwired() -> None:
    """Prober slot ``None`` → 503 with prober_unavailable reason."""

    app = FastAPI()
    app.include_router(capabilities_router)
    app.state.capability_prober = None
    app.state.capability_probe_store = InMemoryCapabilityProbeStore()
    app.dependency_overrides[require_admin] = lambda: AuthClaims(
        sub="admin-1", groups=("admin",)
    )
    client = TestClient(app)

    response = client.post("/api/v1/departments/payment/probe/jira")
    assert response.status_code == 503
    assert response.json()["detail"]["reason"] == "prober_unavailable"


def test_single_probe_502_when_prober_raises() -> None:
    """Prober raises → 502 with stable error code, plus cached row."""

    cache = InMemoryCapabilityProbeStore()
    app, prober, _cache = _build_app(store=cache)
    prober.raise_on_next(RuntimeError("upstream timeout"))
    client = TestClient(app)

    response = client.post("/api/v1/departments/payment/probe/jira")

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["error"] == "prober_exception"
    assert detail["dept_id"] == "payment"
    assert detail["service"] == "jira"

    # Cache records the unhealthy outcome so the matrix stays
    # consistent with what the operator just saw.
    import asyncio
    cached = asyncio.run(cache.get_one(dept_id="payment", service="jira"))
    assert cached is not None
    assert cached.status == "unhealthy"
    assert cached.error is not None
    assert "RuntimeError" in cached.error


def test_single_probe_stamps_probed_at_when_prober_omits_it() -> None:
    """If the prober forgets to fill ``probed_at``, the router stamps it."""

    cache = InMemoryCapabilityProbeStore()
    app, prober, _cache = _build_app(store=cache)
    prober.set_result(
        dept_id="payment",
        service="jira",
        result=ProbeResult(
            dept_id="payment",
            service="jira",
            status="healthy",
            error=None,
            latency_ms=10,
            probed_at=None,  # <- explicitly omitted by the prober
        ),
    )
    client = TestClient(app)

    response = client.post("/api/v1/departments/payment/probe/jira")
    body = response.json()
    assert body["probed_at"] is not None


# ---------------------------------------------------------------------------
# Admin role enforcement (mirrors workflow_control)
# ---------------------------------------------------------------------------


def test_endpoints_require_admin_role() -> None:
    """Without the dependency override every route returns 401."""

    app = FastAPI()
    app.include_router(capabilities_router)
    app.state.capability_prober = _FakeProber()
    app.state.capability_probe_store = InMemoryCapabilityProbeStore()
    # No dependency_overrides so the real ``require_admin`` runs.

    client = TestClient(app)

    response = client.get("/api/v1/departments/capabilities")
    assert response.status_code == 401, response.text

    response = client.post("/api/v1/departments/payment/probe/jira")
    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------


def test_in_memory_store_returns_stable_order() -> None:
    """``get_all`` returns rows in (dept_id, service) order."""

    import asyncio

    store = InMemoryCapabilityProbeStore()

    async def _populate() -> None:
        await store.upsert(
            ProbeResult(dept_id="z", service="jira", status="healthy")
        )
        await store.upsert(
            ProbeResult(dept_id="a", service="docker", status="healthy")
        )
        await store.upsert(
            ProbeResult(dept_id="a", service="jira", status="healthy")
        )

    asyncio.run(_populate())
    rows = asyncio.run(store.get_all())
    keys = [(r.dept_id, r.service) for r in rows]
    assert keys == sorted(keys)


def test_in_memory_store_overwrites_on_upsert() -> None:
    """Second upsert for the same pair replaces the first row."""

    import asyncio

    store = InMemoryCapabilityProbeStore()

    async def _scenario() -> ProbeResult | None:
        await store.upsert(
            ProbeResult(
                dept_id="payment", service="jira",
                status="healthy", latency_ms=10,
            )
        )
        await store.upsert(
            ProbeResult(
                dept_id="payment", service="jira",
                status="unhealthy", error="auth_failed", latency_ms=999,
            )
        )
        return await store.get_one(dept_id="payment", service="jira")

    cached = asyncio.run(_scenario())
    assert cached is not None
    assert cached.status == "unhealthy"
    assert cached.error == "auth_failed"
    assert cached.latency_ms == 999
