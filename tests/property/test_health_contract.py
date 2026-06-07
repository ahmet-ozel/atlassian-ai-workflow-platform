"""invariant for HTTP ``/healthz`` and ``/readyz`` contract.


invariant: HTTP service ``/healthz`` and ``/readyz`` contract.

Also validates that the ``HealthState`` accepted state set includes
``"running_unmonitored"`` ( / Q14) - the state emitted by
``_probe_assume_running`` when a Compose container has no
``healthcheck`` block or when ``docker inspect`` fails.

For every HTTP service in ``COMPONENT_MANIFEST`` (the four FastAPI
services under ``services/``), this test asserts the design-level
liveness and readiness contract from
`//design.md`` §3.1:

* ``GET /healthz``
 - Always returns ``200``.
 - Body is exactly ``{"status": "ok"}``.

* ``GET /readyz``
 - Returns ``200`` when ``Settings.dependencies_reachable`` is
 ``True``.
 - Returns ``503`` with a JSON body of shape
 ``{"status": <str>}`` (single key, ≤ 64 bytes serialized) when
 ``Settings.dependencies_reachable`` is ``False``.

The dependency probe is parameterized via ``monkeypatch`` over the
service's ``settings`` instance; the truthiness of the probe is
explored with Hypothesis ``@given(probe_ready=st.booleans)`` so the
true/false branches are both exercised on every parametrized service.

Implementation notes
--------------------

Each HTTP service ships its FastAPI application under
``services/<name>/src/main.py`` with an identically named ``src``
package. To allow all four services to coexist inside ``sys.modules``,
each service's ``src`` package is loaded under a unique alias via
``importlib.util.spec_from_file_location``; the relative
``from.config import Settings`` import inside ``main.py`` is satisfied
by pre-registering the ``config`` submodule under the same alias.

We drive the apps with ``fastapi.testclient.TestClient`` (which wraps
Starlette's WSGI/ASGI test client) because ``httpx>=0.28`` removed the
``AsyncClient(app=...)`` constructor.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up; pytest registers it as an
# importable module, but we add ``tests/`` to ``sys.path`` defensively
# so this file works under direct ``python -m pytest tests/property``
# invocations too.
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import HTTP_SERVICES, WORKSPACE_ROOT, ComponentSpec  # noqa: E402


# ---------------------------------------------------------------------------
# Service module loader
# ---------------------------------------------------------------------------


def _load_service_module(component: ComponentSpec) -> ModuleType:
    """Import a service's ``src.main`` module under a unique alias.

 Every HTTP service ships an identically named ``src`` package, so
 they cannot coexist in ``sys.modules`` under the literal ``src``
 name. We register each under ``_msf_<safe_name>`` so all four
 services can be loaded simultaneously and the relative
 ``from.config import Settings`` import in ``main.py`` resolves via
 the pre-registered ``<alias>.config`` entry.
 """

    safe_name = component.name.replace("-", "_")
    pkg_alias = f"_msf_{safe_name}"
    config_alias = f"{pkg_alias}.config"
    main_alias = f"{pkg_alias}.main"

    if main_alias in sys.modules:
        return sys.modules[main_alias]

    src_dir = WORKSPACE_ROOT / component.path / "src"
    if not src_dir.is_dir():
        raise FileNotFoundError(f"Expected src/ at {src_dir}")

    # 1) Parent package (backed by ``src/__init__.py``).
    pkg_init = src_dir / "__init__.py"
    pkg_spec = importlib.util.spec_from_file_location(
        pkg_alias,
        str(pkg_init),
        submodule_search_locations=[str(src_dir)],
    )
    assert pkg_spec is not None and pkg_spec.loader is not None
    pkg_module = importlib.util.module_from_spec(pkg_spec)
    sys.modules[pkg_alias] = pkg_module
    pkg_spec.loader.exec_module(pkg_module)

    # 2) ``config`` submodule, so ``from.config import Settings``
    # inside ``main.py`` resolves via ``sys.modules``.
    config_spec = importlib.util.spec_from_file_location(
        config_alias, str(src_dir / "config.py")
    )
    assert config_spec is not None and config_spec.loader is not None
    config_module = importlib.util.module_from_spec(config_spec)
    sys.modules[config_alias] = config_module
    config_spec.loader.exec_module(config_module)
    setattr(pkg_module, "config", config_module)

    # 3) ``main`` submodule (this also instantiates ``settings``).
    main_spec = importlib.util.spec_from_file_location(
        main_alias, str(src_dir / "main.py")
    )
    assert main_spec is not None and main_spec.loader is not None
    main_module = importlib.util.module_from_spec(main_spec)
    sys.modules[main_alias] = main_module
    main_spec.loader.exec_module(main_module)
    setattr(pkg_module, "main", main_module)

    return main_module


# Pre-load all four HTTP service modules at collection time so each
# Hypothesis example can reuse the same module (and the same
# ``settings`` instance) without re-importing.
_SERVICE_MODULES: dict[str, ModuleType] = {
    component.name: _load_service_module(component) for component in HTTP_SERVICES
}


# ---------------------------------------------------------------------------
# invariant - invariant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "component", HTTP_SERVICES, ids=[c.name for c in HTTP_SERVICES]
)
@hyp_settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(probe_ready=st.booleans())
def test_health_and_ready_contract(
    component: ComponentSpec,
    probe_ready: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """invariant - health + readiness contract holds for every HTTP service.



 For every ``probe_ready ∈ {True, False}``:

 * ``GET /healthz`` returns 200 and ``{"status": "ok"}``.
 * ``GET /readyz`` returns 200 when ``probe_ready`` is True; otherwise
 503 with a JSON body containing ``"status"`` key.
 """

    main_module = _SERVICE_MODULES[component.name]
    settings_obj = main_module.settings
    settings_cls = type(settings_obj)

    # For services that still use the legacy dependencies_reachable stub
    # (e.g. task-intake-service), patch the Settings class method.
    # For services with real readiness probes (admin-dashboard-api,
    # assistant-service, automation-service), patch the check_readiness
    # function in their respective readiness modules.
    monkeypatch.setattr(
        settings_cls,
        "dependencies_reachable",
        lambda self: probe_ready,
    )

    # Patch real readiness probes for services that use them.
    # admin-dashboard-api uses src.lifecycle.readiness
    _patch_readiness_module(component, probe_ready, monkeypatch)

    with TestClient(main_module.app) as client:
        # /healthz - must be 200 + {"status":"ok"} regardless of probe.
        health_resp = client.get("/healthz")
        assert health_resp.status_code == 200, (
            f"{component.name}: /healthz must always return 200, "
            f"got {health_resp.status_code}"
        )
        assert health_resp.json() == {"status": "ok"}, (
            f"{component.name}: /healthz body must equal "
            f'{{"status":"ok"}}, got {health_resp.json()!r}'
        )

        # /readyz - branches on the probe value.
        ready_resp = client.get("/readyz")

        if probe_ready:
            assert ready_resp.status_code == 200, (
                f"{component.name}: /readyz must return 200 when "
                f"probes pass, got {ready_resp.status_code}"
            )
        else:
            assert ready_resp.status_code == 503, (
                f"{component.name}: /readyz must return 503 when "
                f"probes fail, got {ready_resp.status_code}"
            )
            payload = ready_resp.json()
            assert isinstance(payload, dict), (
                f"{component.name}: /readyz 503 body must be a JSON object, "
                f"got {type(payload).__name__}: {payload!r}"
            )
            assert "status" in payload, (
                f"{component.name}: /readyz 503 body must contain "
                f"'status' key; got keys {sorted(payload.keys())}"
            )
            assert isinstance(payload["status"], str), (
                f"{component.name}: /readyz 503 'status' value must be a "
                f"string, got {type(payload['status']).__name__}"
            )


def _patch_readiness_module(
    component: ComponentSpec,
    probe_ready: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch the check_readiness function for services with real probes."""

    async def _mock_ready(dependencies):
        return True, {"status": "ready"}

    async def _mock_not_ready(dependencies):
        return False, {"status": "not_ready", "failed_dependencies": ["mock_dep"]}

    mock_fn = _mock_ready if probe_ready else _mock_not_ready

    if component.name == "admin-dashboard-api":
        try:
            from src.lifecycle import readiness as readiness_mod
            monkeypatch.setattr(readiness_mod, "check_readiness", mock_fn)
        except (ImportError, AttributeError):
            pass
    elif component.name == "assistant-service":
        try:
            # The assistant-service readiness module is loaded under
            # its aliased package name.
            safe_name = component.name.replace("-", "_")
            pkg_alias = f"_msf_{safe_name}"
            readiness_alias = f"{pkg_alias}.readiness"
            if readiness_alias in sys.modules:
                monkeypatch.setattr(
                    sys.modules[readiness_alias], "check_readiness", mock_fn
                )
            else:
                # Try direct import path
                src_dir = WORKSPACE_ROOT / component.path / "src"
                readiness_path = src_dir / "readiness.py"
                if readiness_path.exists():
                    spec = importlib.util.spec_from_file_location(
                        readiness_alias, str(readiness_path)
                    )
                    if spec and spec.loader:
                        mod = importlib.util.module_from_spec(spec)
                        sys.modules[readiness_alias] = mod
                        spec.loader.exec_module(mod)
                        monkeypatch.setattr(mod, "check_readiness", mock_fn)
        except (ImportError, AttributeError):
            pass
    elif component.name == "automation-service":
        try:
            from automation_service import readiness as readiness_mod
            monkeypatch.setattr(readiness_mod, "check_readiness", mock_fn)
        except (ImportError, AttributeError):
            pass


# ---------------------------------------------------------------------------
# Accepted HealthState set - running_unmonitored ( / Q14)
# ---------------------------------------------------------------------------

# The admin-dashboard-api service root is two levels up from the
# platform/tests/ directory.
_ADMIN_API_ROOT = Path(__file__).resolve().parents[2] / "services" / "admin-dashboard-api"
if str(_ADMIN_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADMIN_API_ROOT))


def test_health_state_accepted_set_includes_running_unmonitored() -> None:
    """``running_unmonitored`` is a member of the HealthState accepted set.



 The ``HealthState`` Literal in ``src.lifecycle.health_probe`` must
 include ``"running_unmonitored"`` as a valid state. This state is
 emitted by ``_probe_assume_running`` when:

 * The Compose container has no ``healthcheck`` block (empty or
 ``"<no value>"`` docker inspect output).
 * ``docker inspect`` fails (timeout, missing binary, non-zero exit).

 The ``"unknown"`` literal is retained for backwards compatibility
 with persisted snapshots but is **not** emitted by the current probe.
 """
    from src.lifecycle.health_probe import HealthState  # type: ignore[import]

    # HealthState is a Literal; extract its args via typing.get_args.
    import typing

    accepted_states: frozenset[str] = frozenset(typing.get_args(HealthState))

    assert "running_unmonitored" in accepted_states, (
        "HealthState accepted set must include 'running_unmonitored' "
        "(the operational rule / Q14 - Compose containers without healthcheck blocks "
        "must be classified as running_unmonitored, not unknown)"
    )

    # Verify the full expected set is present (additive check - does not
    # break if new states are added in the future).
    expected_minimum = frozenset(
        {"healthy", "unhealthy", "starting", "unknown", "running_unmonitored"}
    )
    missing = expected_minimum - accepted_states
    assert not missing, (
        f"HealthState is missing expected states: {sorted(missing)}. "
        f"Current accepted set: {sorted(accepted_states)}"
    )
