"""Unit tests for ``src.main``.
These tests validate the readiness / liveness contract and the
lifespan wiring for the service:
* ``GET /healthz`` returns 200 ``{"status": "ok"}`` whether or not the
  manifest loaded, matching the rest of the service stack.
* ``GET /readyz`` returns 503 ``{"status": "not_ready", "reason":
  "manifest_invalid"}`` when ``load_manifest`` raised
  :class:`ManifestLoadError` during startup. Body must be ≤64 bytes.
* ``GET /readyz`` returns 200 ``{"status": "ready"}`` on the happy
  path (manifest loaded + ``Settings.dependencies_reachable`` true) —
* The lifespan context attaches the LifecycleService singleton on
  ``app.state.lifecycle`` and tears it down on shutdown.
* ``get_lifecycle_service`` raises ``HTTPException(503)`` when the
  manifest failed to load, mirroring the readiness probe.
The tests deliberately use the *real* manifest checked into the
workspace because ``Settings`` resolves ``workspace_root`` to the
repository root by default, and the validator setup is independent of
the manifest path. For the failure-mode test we monkey-patch
``src.main.load_manifest`` to raise."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Bootstrap sys.path so ``import src.main`` resolves under direct
# ``pytest tests/unit`` invocations from the service root (mirrors
# the convention used in test_require_admin.py / test_manifest.py).
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
_AUTH_SHARED_SRC = _WORKSPACE_ROOT / "libs" / "auth-shared" / "src"
if _AUTH_SHARED_SRC.is_dir() and str(_AUTH_SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(_AUTH_SHARED_SRC))

import src.main as main_module  # noqa: E402
from src.main import app, get_lifecycle_service  # noqa: E402
from src.manifest import ManifestLoadError  # noqa: E402


# ---------------------------------------------------------------------------
# /healthz — always 200
# ---------------------------------------------------------------------------


def test_healthz_returns_200_on_happy_path() -> None:
    """``GET /healthz`` returns 200 when the manifest loads cleanly."""

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_returns_200_even_when_manifest_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/healthz`` MUST stay green when the manifest fails to load.
    Manifest failures surface on ``/readyz`` only; flapping the liveness
    probe would force a restart loop without
    fixing anything (the operator has to edit the manifest file)."""

    def _explode(_workspace_root: Path) -> None:
        raise ManifestLoadError("synthetic manifest failure for test")

    monkeypatch.setattr(main_module, "load_manifest", _explode)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /readyz — happy path + manifest_invalid
# ---------------------------------------------------------------------------


def test_readyz_returns_200_on_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/readyz`` returns 200 when all dependency probes pass.
    We mock the readiness probes to simulate all dependencies being
    reachable ."""
    from src.lifecycle import readiness as readiness_mod

    async def _mock_check_readiness(dependencies):
        return True, {"status": "ready"}

    monkeypatch.setattr(readiness_mod, "check_readiness", _mock_check_readiness)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_readyz_returns_503_with_manifest_invalid_when_manifest_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/readyz`` returns 503 + ``manifest_invalid``.
    The body shape is fixed by :
    ``{"status": "not_ready", "reason": "manifest_invalid"}``."""

    def _explode(_workspace_root: Path) -> None:
        raise ManifestLoadError("synthetic manifest failure for test")

    monkeypatch.setattr(main_module, "load_manifest", _explode)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "reason": "manifest_invalid",
    }


def test_readyz_manifest_invalid_body_under_64_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 503 body for ``manifest_invalid`` MUST be ≤64 bytes.
    so logs/metrics remain the structured surface for diagnostics.
    The canonical body
    ``{"status":"not_ready","reason":"manifest_invalid"}`` is 51
    bytes — well within budget."""

    def _explode(_workspace_root: Path) -> None:
        raise ManifestLoadError("synthetic")

    monkeypatch.setattr(main_module, "load_manifest", _explode)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    # Use raw bytes (response.content) rather than .text so we are
    # measuring exactly what hit the wire.
    assert len(response.content) <= 64, (
        f"readyz manifest_invalid body is {len(response.content)} bytes, "
        f"must be <=64; body={response.content!r}"
    )


def test_readyz_returns_503_when_dependencies_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real probe failure drives /readyz to 503 .
    When any dependency probe reports unreachable, the endpoint returns
    503 with ``{"status": "not_ready", "failed_dependencies": [...]}``."""
    from src.lifecycle import readiness as readiness_mod

    async def _mock_check_readiness(dependencies):
        return False, {
            "status": "not_ready",
            "failed_dependencies": ["postgres"],
        }

    monkeypatch.setattr(readiness_mod, "check_readiness", _mock_check_readiness)

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "failed_dependencies": ["postgres"],
    }


# ---------------------------------------------------------------------------
# Lifespan — LifecycleService wiring
# ---------------------------------------------------------------------------


def test_lifespan_attaches_lifecycle_service_when_manifest_valid() -> None:
    """The lifespan context wires a LifecycleService onto app.state.

    The service is constructed once per process and reused across
    requests so the in-memory state cache and audit-deferred queue
    are shared.
    """

    with TestClient(app) as _client:
        # Inside the ``with`` block the lifespan startup has run; the
        # singleton must be present.
        assert app.state.lifecycle is not None
        assert app.state.manifest is not None
        assert len(app.state.manifest) > 0
        assert app.state.manifest_error is None
        assert app.state.http_client is not None
        assert app.state.audit_writer is not None
        assert app.state.oidc_validator is not None


def test_lifespan_records_manifest_error_when_load_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``app.state.manifest_error`` carries the ManifestLoadError instance."""

    sentinel = ManifestLoadError("synthetic")

    def _explode(_workspace_root: Path) -> None:
        raise sentinel

    monkeypatch.setattr(main_module, "load_manifest", _explode)

    with TestClient(app) as _client:
        assert app.state.manifest_error is sentinel
        # No LifecycleService is constructed when manifest load fails.
        assert app.state.lifecycle is None


# ---------------------------------------------------------------------------
# get_lifecycle_service dependency
# ---------------------------------------------------------------------------


def test_get_lifecycle_service_returns_singleton_on_happy_path() -> None:
    """The dependency factory returns the lifespan-built singleton."""

    with TestClient(app) as _client:
        # Re-use the request scope by constructing a tiny dummy that
        # exposes ``app.state.lifecycle`` the same way FastAPI would.
        class _DummyRequest:
            def __init__(self) -> None:
                self.app = app

        result = get_lifecycle_service(_DummyRequest())  # type: ignore[arg-type]
        assert result is app.state.lifecycle
        assert result is not None


def test_get_lifecycle_service_raises_503_when_manifest_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 with ``manifest_invalid`` reason mirrors the /readyz contract."""

    def _explode(_workspace_root: Path) -> None:
        raise ManifestLoadError("synthetic")

    monkeypatch.setattr(main_module, "load_manifest", _explode)

    with TestClient(app) as _client:

        class _DummyRequest:
            def __init__(self) -> None:
                self.app = app

        with pytest.raises(HTTPException) as exc_info:
            get_lifecycle_service(_DummyRequest())  # type: ignore[arg-type]

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail == {
            "status": "not_ready",
            "reason": "manifest_invalid",
        }
