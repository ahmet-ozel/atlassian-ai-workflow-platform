"""Integration test — ``GET /readyz`` returns 200 when dependencies are reachable.

Validates: Requirement 6.3 of the ``automation-service-wiring`` spec.

The ``/readyz`` endpoint runs :func:`probe_postgres` and
:func:`probe_temporal` in parallel and returns 200 once both probes
succeed. The Compose-based smoke test version (task 8.3) stands up
real Postgres + Temporal; this in-process variant monkey-patches the
two probes to a successful result so the contract is exercised on
every CI shard without external infrastructure.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
_PLATFORM_ROOT = _AUTOMATION_ROOT.parents[1]

for _path in (
    _AUTOMATION_ROOT / "src",
    _AUTOMATION_ROOT,
    _PLATFORM_ROOT / "libs" / "audit_logger" / "src",
    _PLATFORM_ROOT / "libs" / "auth-shared" / "src",
    _PLATFORM_ROOT / "libs" / "http-shared" / "src",
    _PLATFORM_ROOT / "libs" / "vault_client" / "src",
    _PLATFORM_ROOT / "libs" / "temporal-shared" / "src",
    _PLATFORM_ROOT / "libs" / "observability" / "src",
):
    if _path.is_dir() and str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


import automation_service  # noqa: E402,F401
app_module = sys.modules["automation_service.app"]

_PROPERTY_DIR = _AUTOMATION_ROOT / "tests" / "property"
if str(_PROPERTY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROPERTY_DIR))

from _lifespan_fakes import install_lifespan_fakes  # noqa: E402
from automation_service import readiness as _readiness  # noqa: E402


def test_readyz_returns_200_when_dependencies_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /readyz`` returns 200 once Postgres + Temporal probes pass.

    The test patches :func:`probe_postgres` and :func:`probe_temporal`
    to return successful :class:`DependencyProbeResult` instances,
    enters the lifespan via ``TestClient``, then hits ``/readyz`` and
    asserts the 200-ready contract.
    """

    install_lifespan_fakes(monkeypatch)

    async def _probe_postgres(dsn: str) -> _readiness.DependencyProbeResult:
        return _readiness.DependencyProbeResult(
            name="postgres", reachable=True, latency_ms=1.0
        )

    async def _probe_temporal(host: str) -> _readiness.DependencyProbeResult:
        return _readiness.DependencyProbeResult(
            name="temporal", reachable=True, latency_ms=1.0
        )

    monkeypatch.setattr(_readiness, "probe_postgres", _probe_postgres)
    monkeypatch.setattr(_readiness, "probe_temporal", _probe_temporal)

    app = app_module.create_app()
    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 200, (
            f"/readyz returned {response.status_code}; expected 200"
        )
