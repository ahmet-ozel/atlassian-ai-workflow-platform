"""Unit tests for the canonical ``automation_service.app`` factory.

Covers Requirement 1.10 (``automation-service`` exposes an HTTP surface
with a ``/healthz`` endpoint that the Compose stack can probe) and the
shape of the legacy ``/readyz`` contract carried over from the
multi-service-scaffold skeleton.

The tests deliberately exercise both the module-level ``app`` object
(used by ``uvicorn src.main:app``) and the factory ``create_app()``
(used by tests / future ASGI lifespan wiring) so that any drift between
the two paths is caught at this layer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Make the in-tree ``src`` directory importable so ``automation_service``
# resolves without an ``hatch build``-generated install. This mirrors
# the bootstrap used in the sibling property tests.
_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT / "src"))
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))


from automation_service.app import app as module_app  # noqa: E402
from automation_service.app import create_app  # noqa: E402
from src.config import Settings  # noqa: E402


class TestHealthz:
    """``GET /healthz`` always returns 200 (Requirement 1.10)."""

    def test_module_level_app_returns_ok(self) -> None:
        with TestClient(module_app) as client:
            resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_factory_built_app_returns_ok(self) -> None:
        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_healthz_does_not_require_dependencies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Liveness probe must not depend on Postgres / Vault / Temporal.

        Even if every downstream is unreachable, ``/healthz`` returns
        200 so the orchestrator does not kill an otherwise-healthy
        process (per design §"Liveness vs readiness").
        """

        class BrokenSettings(Settings):
            def dependencies_reachable(self) -> bool:  # type: ignore[override]
                return False

        app = create_app(settings=BrokenSettings())
        with TestClient(app) as client:
            resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestReadyz:
    """``GET /readyz`` flips between 200 and 503 based on real probes."""

    def test_ready_when_dependencies_reachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from automation_service import readiness as readiness_mod

        async def _mock_check_readiness(dependencies):
            return True, {"status": "ready"}

        monkeypatch.setattr(readiness_mod, "check_readiness", _mock_check_readiness)

        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ready"}

    def test_not_ready_returns_503(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from automation_service import readiness as readiness_mod

        async def _mock_check_readiness(dependencies):
            return False, {
                "status": "not_ready",
                "failed_dependencies": ["postgres", "temporal"],
            }

        monkeypatch.setattr(readiness_mod, "check_readiness", _mock_check_readiness)

        app = create_app()
        with TestClient(app) as client:
            resp = client.get("/readyz")
        assert resp.status_code == 503
        assert resp.json() == {
            "status": "not_ready",
            "failed_dependencies": ["postgres", "temporal"],
        }


class TestLegacyReExport:
    """``src.main`` continues to expose the same ``app`` object.

    The Dockerfile's ``CMD ["uvicorn", "src.main:app", ...]`` and any
    existing tests that import ``from src.main import app`` must keep
    working after the canonical app moved into ``automation_service``.
    """

    def test_src_main_app_is_module_level_app(self) -> None:
        # Importing inside the test keeps the module-level imports at
        # the top of the file focused on the new package.
        from src.main import app as legacy_app

        assert legacy_app is module_app
