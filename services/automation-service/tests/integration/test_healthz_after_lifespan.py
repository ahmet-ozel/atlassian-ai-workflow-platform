"""Integration test — ``GET /healthz`` returns 200 after lifespan startup.

Validates: Requirement 6.2 of the ``automation-service-wiring`` spec.

The test wires the production lifespan handler in-process via FastAPI's
``TestClient`` (which enters the lifespan on ``__enter__``) and asserts
``/healthz`` returns HTTP 200 with ``{"status": "ok"}``.

This is the in-process variant of the Compose-based smoke test
described in ``.kiro/specs/automation-service-wiring/tasks.md`` task
8.2. The Compose-harness version runs the same assertion against a
live container; that variant is delivered alongside the
``platform/tests/integration/`` Docker harness and is opt-in via the
``--run-docker`` pytest flag registered in ``platform/tests/conftest.py``.
The in-process variant here runs unconditionally on every CI shard so
the contract is exercised without needing the Compose stack to be up.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

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

# Reach the property-suite's fakes module — it carries the
# ``install_lifespan_fakes`` helper that monkey-patches every shared
# infrastructure constructor with in-memory stand-ins.
_PROPERTY_DIR = _AUTOMATION_ROOT / "tests" / "property"
if str(_PROPERTY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROPERTY_DIR))

from _lifespan_fakes import install_lifespan_fakes  # noqa: E402


def test_healthz_returns_200_after_lifespan_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``GET /healthz`` returns 200 with ``{"status": "ok"}`` post-startup.

    The test enters the FastAPI lifespan via ``TestClient`` (which runs
    the ``async with`` block on ``__enter__``) and then hits ``/healthz``
    — the response must be a clean 200 with the literal
    ``{"status": "ok"}`` body. The lifespan's collaborator construction
    runs end-to-end so the assertion also catches accidental
    ``/healthz`` regressions (e.g. someone wiring a dependency into the
    handler).
    """

    install_lifespan_fakes(monkeypatch)

    app = app_module.create_app()
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200, (
            f"/healthz returned {response.status_code}; expected 200"
        )
        assert response.json() == {"status": "ok"}, (
            f"/healthz body was {response.json()!r}; expected "
            "{'status': 'ok'}"
        )
