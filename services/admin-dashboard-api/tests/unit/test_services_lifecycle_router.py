"""Unit tests for ``src.routers.services_lifecycle``.

The router is exercised through :class:`fastapi.testclient.TestClient`
against a stub :class:`LifecycleService` that records every call. The
:func:`require_admin` dependency is overridden with a permissive stub
for the happy paths and a 401-raising stub for the auth failure case
so we can verify the gate without instantiating a real OIDC validator.

Coverage matrix:

* Each of the eight endpoints - ``GET /admin/services``,
  ``GET /admin/services/{name}``,
  ``POST /admin/services/{name}/start``,
  ``POST /admin/services/{name}/stop``,
  ``POST /admin/services/{name}/restart``,
  ``POST /admin/services/{name}/test``,
  ``GET /admin/services/{name}/logs``,
  ``GET /admin/services/{name}/health``.
* 401 when ``require_admin`` raises.
* 404 when :class:`UnknownServiceError` propagates.
* 422 when :class:`FormSchemaMismatchError` propagates.
* 502 + ``correlation_id`` UUID for Vault / Audit / Compose failures
  with a correlation id.
* 409 for :class:`TestPreconditionError`.
* Happy-path 200 / 202 status codes and shapes for every endpoint.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

# Bootstrap ``sys.path`` so ``import src.routers.services_lifecycle``
# resolves under direct ``pytest tests/unit`` invocations (mirrors the
# pattern used by the other unit-test modules in this folder).
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# ``libs/auth-shared`` is consumed via ``sys.path`` injection so the
# router's transitive ``from auth_shared import ...`` resolves under
# direct ``pytest tests/unit`` invocations too.
_WORKSPACE_ROOT = _SERVICE_ROOT.parents[1]
_AUTH_SHARED_SRC = _WORKSPACE_ROOT / "libs" / "auth-shared" / "src"
if _AUTH_SHARED_SRC.is_dir() and str(_AUTH_SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(_AUTH_SHARED_SRC))

from src.auth.dependencies import AuthClaims, require_admin  # noqa: E402
from src.lifecycle.audit_writer import AuditUnreachableError  # noqa: E402
from src.lifecycle.compose_runner import ComposeFailureError, ComposeResult  # noqa: E402
from src.lifecycle.health_probe import HealthSnapshot  # noqa: E402
from src.lifecycle.service import (  # noqa: E402
    FormSchemaField,
    LifecycleStateCache,
    RunTestsResponse,
    ServiceSummary,
    StartPlan,
    StartResponse,
    StopResponse,
    TestPreconditionError,
    TestSummary,
    UnknownServiceError,
)
from src.lifecycle.vault_client import VaultWriteError  # noqa: E402
from src.manifest import ManagedServiceEntry  # noqa: E402
from src.routers.services_lifecycle import (  # noqa: E402
    get_lifecycle_service,
    get_settings_dependency,
    router,
)


# ---------------------------------------------------------------------------
# Stub LifecycleService
# ---------------------------------------------------------------------------


@dataclass
class _StubCompose:
    """Minimal :class:`ComposeRunner` stand-in for the streaming-logs path.

    Returns an async iterator of pre-canned lines when ``follow=True``;
    a :class:`ComposeResult` otherwise. Records every call so tests can
    assert the ``service_name`` / ``tail`` arguments.
    """

    lines: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def logs(
        self,
        *,
        service_name: str,
        tail: int,
        follow: bool,
    ):
        self.calls.append(
            {"service_name": service_name, "tail": tail, "follow": follow}
        )
        if follow:
            return self._iter()
        return ComposeResult(
            exit_code=0,
            stdout="\n".join(self.lines),
            stderr="",
            argv=("docker", "compose", "logs", service_name),
        )

    async def _iter(self) -> AsyncIterator[str]:
        for line in self.lines:
            yield line


@dataclass
class _StubLifecycleService:
    """Stand-in for :class:`LifecycleService` recording every call.

    Configurable per test via the public ``raise_*`` attributes; the
    router is the system-under-test so the stub's behaviour stays as
    minimal as possible.
    """

    summaries: list[ServiceSummary] = field(default_factory=list)
    state_cache: dict[str, LifecycleStateCache] = field(default_factory=dict)
    by_name: dict[str, ManagedServiceEntry] = field(default_factory=dict)
    form_schema: dict[str, list[FormSchemaField]] = field(default_factory=dict)
    snapshot: HealthSnapshot | None = None
    logs_lines: list[str] = field(default_factory=list)

    start_response: StartResponse | None = None
    stop_response: StopResponse | None = None
    restart_response: StartResponse | None = None
    test_response: RunTestsResponse | None = None

    raise_on_start: BaseException | None = None
    raise_on_stop: BaseException | None = None
    raise_on_restart: BaseException | None = None
    raise_on_test: BaseException | None = None
    raise_on_logs: BaseException | None = None
    raise_on_health: BaseException | None = None
    raise_on_get_entry: BaseException | None = None
    raise_on_form_schema: BaseException | None = None
    raise_on_start_plan: BaseException | None = None
    raise_on_probe: BaseException | None = None

    start_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_calls: list[dict[str, Any]] = field(default_factory=list)
    restart_calls: list[dict[str, Any]] = field(default_factory=list)
    test_calls: list[dict[str, Any]] = field(default_factory=list)
    logs_calls: list[dict[str, Any]] = field(default_factory=list)
    health_calls: list[str] = field(default_factory=list)
    start_plan_calls: list[str] = field(default_factory=list)
    probe_calls: list[dict[str, Any]] = field(default_factory=list)
    # Record calls to
    # ``record_purge_vault_blocked`` so the unit tests can assert that
    # the production guard wrote an audit row before returning 403.
    purge_vault_blocked_calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_purge_vault_blocked: BaseException | None = None

    start_plans: dict[str, StartPlan] = field(default_factory=dict)

    compose: _StubCompose = field(default_factory=_StubCompose)

    # ---- read surface --------------------------------------------------

    async def list_summaries(self) -> list[ServiceSummary]:
        return list(self.summaries)

    def get_manifest_entry(self, name: str) -> ManagedServiceEntry:
        if self.raise_on_get_entry is not None:
            raise self.raise_on_get_entry
        try:
            return self.by_name[name]
        except KeyError as exc:
            raise UnknownServiceError(name) from exc

    def get_form_schema(self, name: str) -> list[FormSchemaField]:
        if self.raise_on_form_schema is not None:
            raise self.raise_on_form_schema
        if name not in self.by_name:
            raise UnknownServiceError(name)
        return list(self.form_schema.get(name, []))

    def compute_start_plan(self, name: str) -> StartPlan:
        self.start_plan_calls.append(name)
        if self.raise_on_start_plan is not None:
            raise self.raise_on_start_plan
        if name not in self.by_name:
            raise UnknownServiceError(name)
        plan = self.start_plans.get(name)
        if plan is not None:
            return plan
        # Default: trivial plan with the target as the only entry to
        # start. Tests that exercise non-trivial plans pre-populate
        # ``start_plans``.
        return StartPlan(
            target_service=name,
            will_start=(name,),
            already_running=(),
        )

    def build_log_redaction_pattern(self, entry: ManagedServiceEntry):
        return None  # no redaction in the stub - tests don't exercise it

    # ---- mutating surface ---------------------------------------------

    async def start(self, *, name, env_overrides, actor):
        self.start_calls.append(
            {"name": name, "env_overrides": dict(env_overrides), "actor": actor}
        )
        if self.raise_on_start is not None:
            raise self.raise_on_start
        assert self.start_response is not None, "test forgot to set start_response"
        return self.start_response

    async def stop(self, *, name, remove_volumes, purge_vault=False, actor):
        self.stop_calls.append(
            {
                "name": name,
                "remove_volumes": remove_volumes,
                "purge_vault": purge_vault,
                "actor": actor,
            }
        )
        if self.raise_on_stop is not None:
            raise self.raise_on_stop
        assert self.stop_response is not None, "test forgot to set stop_response"
        return self.stop_response

    async def restart(self, *, name, actor):
        self.restart_calls.append({"name": name, "actor": actor})
        if self.raise_on_restart is not None:
            raise self.raise_on_restart
        assert self.restart_response is not None, "test forgot to set restart_response"
        return self.restart_response

    async def run_tests(self, *, name, stream, actor):
        self.test_calls.append({"name": name, "stream": stream, "actor": actor})
        if self.raise_on_test is not None:
            raise self.raise_on_test
        assert self.test_response is not None, "test forgot to set test_response"
        return self.test_response

    async def logs(self, *, name, tail, follow):
        self.logs_calls.append({"name": name, "tail": tail, "follow": follow})
        if self.raise_on_logs is not None:
            raise self.raise_on_logs
        return list(self.logs_lines)

    async def health_of(self, *, name):
        self.health_calls.append(name)
        if self.raise_on_health is not None:
            raise self.raise_on_health
        if name not in self.by_name:
            raise UnknownServiceError(name)
        assert self.snapshot is not None, "test forgot to set snapshot"
        return self.snapshot

    async def run_connectivity_probe(self, *, name, actor):
        self.probe_calls.append({"name": name, "actor": actor})
        if self.raise_on_probe is not None:
            raise self.raise_on_probe
        if name not in self.by_name:
            raise UnknownServiceError(name)

    async def record_purge_vault_blocked(self, *, name, actor):
        """Stub for :meth:`LifecycleService.record_purge_vault_blocked`.

        Records the call so the production-guard tests can assert the
        audit row was written before the router returned 403.
        Mirrors the production helper's ``UnknownServiceError`` path
        when ``name`` is missing from ``by_name`` so router-level
        404 mapping stays consistent.
        """

        self.purge_vault_blocked_calls.append({"name": name, "actor": actor})
        if self.raise_on_purge_vault_blocked is not None:
            raise self.raise_on_purge_vault_blocked
        if name not in self.by_name:
            raise UnknownServiceError(name)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _entry(
    *,
    name: str = "automation-service",
    kind: str = "http_service",
    health_endpoint: str | None = "/healthz",
    test_command: str | None = "docker compose exec automation-service pytest -q",
) -> ManagedServiceEntry:
    return ManagedServiceEntry(
        name=name,
        kind=kind,  # type: ignore[arg-type]
        compose_service_name=name,
        compose_profile=name,
        env_example_path=f"services/{name}/.env.example",
        health_endpoint=health_endpoint,
        test_command=test_command,
    )


def _snapshot(state: str = "healthy") -> HealthSnapshot:
    return HealthSnapshot(
        ts=datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        healthz_status=200 if state == "healthy" else 503,
        healthz_body="ok" if state == "healthy" else "down",
        readyz_status=200 if state == "healthy" else 503,
        readyz_body="ok" if state == "healthy" else "down",
        state=state,  # type: ignore[arg-type]
    )


def _build_app(
    stub: _StubLifecycleService,
    *,
    actor_sub: str | None = "ops-1",
    deployment_profile: str = "dev",
) -> FastAPI:
    """Return a FastAPI app wired to the router with stub dependencies.

    ``actor_sub=None`` causes ``require_admin`` to raise 401 - used to
    cover the auth-gate path without instantiating an OIDC validator.

    ``deployment_profile`` drives the stop guard. Default ``"dev"``
    keeps every existing test on the
    permissive path; the production-guard tests pass ``"production"``
    to exercise the 403 + audit-row branch.
    """

    app = FastAPI()
    app.include_router(router)

    if actor_sub is not None:
        app.dependency_overrides[require_admin] = lambda: AuthClaims(
            sub=actor_sub, groups=("admin",)
        )
    else:
        def _deny():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing bearer token",
            )

        app.dependency_overrides[require_admin] = _deny

    app.dependency_overrides[get_lifecycle_service] = lambda: stub

    # Feed the deployment profile
    # via a Settings stub so the stop endpoint's production guard
    # picks the right branch without us having to mutate process env.
    class _StubSettings:
        def __init__(self, profile: str) -> None:
            self.deployment_profile = profile

    _settings_stub = _StubSettings(deployment_profile)
    app.dependency_overrides[get_settings_dependency] = lambda: _settings_stub
    return app


# ---------------------------------------------------------------------------
# 401 - auth gate
# ---------------------------------------------------------------------------


def test_list_services_returns_401_when_require_admin_fails() -> None:
    """Every endpoint MUST be gated on ``require_admin``.

    A failing dependency produces a 401 *before* the lifecycle stub is
    consulted; the stub records zero calls.
    """

    stub = _StubLifecycleService()
    client = TestClient(_build_app(stub, actor_sub=None))

    response = client.get("/admin/services")

    assert response.status_code == 401
    assert stub.start_calls == []  # short-circuit before lifecycle
    # And so are POST routes - sanity check one of them.
    response_post = client.post(
        "/admin/services/automation-service/start",
        json={"env_overrides": {}},
    )
    assert response_post.status_code == 401


# ---------------------------------------------------------------------------
# GET /admin/services
# ---------------------------------------------------------------------------


def test_list_services_returns_summary_array() -> None:
    """One row per Managed_Service."""

    stub = _StubLifecycleService(
        summaries=[
            ServiceSummary(
                name="automation-service",
                kind="http_service",
                state="running",
                last_started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                last_health_snapshot=_snapshot("healthy"),
            ),
            ServiceSummary(
                name="agent-runner-worker",
                kind="worker",
                state="stopped",
                last_started_at=None,
                last_health_snapshot=None,
            ),
        ],
    )
    client = TestClient(_build_app(stub))

    response = client.get("/admin/services")

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) == 2
    assert body[0]["name"] == "automation-service"
    assert body[0]["state"] == "running"
    assert body[0]["last_health_snapshot"]["state"] == "healthy"
    assert body[1]["state"] == "stopped"
    assert body[1]["last_health_snapshot"] is None


# ---------------------------------------------------------------------------
# GET /admin/services/{name}
# ---------------------------------------------------------------------------


def test_get_service_detail_returns_form_schema_and_snapshot() -> None:
    """Manifest entry + cached snapshot + form_schema."""

    entry = _entry()
    stub = _StubLifecycleService(
        by_name={entry.name: entry},
        form_schema={
            entry.name: [
                FormSchemaField(
                    key="PORT", default_value="8080", comment="Plain knob",
                    is_sensitive=False,
                ),
                FormSchemaField(
                    key="API_TOKEN", default_value="", comment="Sensitive.",
                    is_sensitive=True,
                ),
            ]
        },
        state_cache={
            entry.name: LifecycleStateCache(
                name=entry.name,
                state="running",
                last_started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
                last_health_snapshot=_snapshot("healthy"),
            )
        },
    )
    client = TestClient(_build_app(stub))

    response = client.get(f"/admin/services/{entry.name}")

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == entry.name
    assert body["state"] == "running"
    fields = body["form_schema"]["fields"]
    assert {f["key"] for f in fields} == {"PORT", "API_TOKEN"}
    assert any(f["is_sensitive"] for f in fields if f["key"] == "API_TOKEN")
    assert body["last_health_snapshot"]["state"] == "healthy"


def test_get_service_detail_404_when_unknown() -> None:
    stub = _StubLifecycleService()
    client = TestClient(_build_app(stub))

    response = client.get("/admin/services/does-not-exist")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /admin/services/{name}/start-plan
# ---------------------------------------------------------------------------


def test_start_plan_returns_topological_will_start_and_already_running() -> None:
    """Preview the dependency-chain plan before pressing Start.

    The router adapts the orchestrator's :class:`StartPlan` dataclass
    into the JSON shape ``{target_service, will_start[],
    already_running[]}``. Order is preserved verbatim so the UI can
    render the dependencies in the same sequence the lifecycle service will visit
    them.
    """

    entry = _entry()
    plan = StartPlan(
        target_service=entry.name,
        will_start=("atlassian-mcp", entry.name),
        already_running=("admin-dashboard-api",),
    )
    stub = _StubLifecycleService(
        by_name={entry.name: entry},
        start_plans={entry.name: plan},
    )
    client = TestClient(_build_app(stub))

    response = client.get(f"/admin/services/{entry.name}/start-plan")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "target_service": entry.name,
        "will_start": ["atlassian-mcp", entry.name],
        "already_running": ["admin-dashboard-api"],
    }
    assert stub.start_plan_calls == [entry.name]


def test_start_plan_404_when_unknown_service() -> None:
    """Unknown service  404 before the orchestrator allocates any state."""

    stub = _StubLifecycleService(
        raise_on_start_plan=UnknownServiceError("ghost"),
    )
    client = TestClient(_build_app(stub))

    response = client.get("/admin/services/ghost/start-plan")

    assert response.status_code == 404
    assert stub.start_plan_calls == ["ghost"]


def test_start_plan_returns_401_when_require_admin_fails() -> None:
    """The endpoint MUST be gated on ``require_admin``."""

    stub = _StubLifecycleService()
    client = TestClient(_build_app(stub, actor_sub=None))

    response = client.get("/admin/services/automation-service/start-plan")

    assert response.status_code == 401
    assert stub.start_plan_calls == []  # short-circuit before lifecycle


# ---------------------------------------------------------------------------
# POST /admin/services/{name}/start
# ---------------------------------------------------------------------------


def test_start_returns_202_with_correlation_id() -> None:
    """202 + ``{state, correlation_id}``."""

    entry = _entry()
    cid = uuid4()
    stub = _StubLifecycleService(
        by_name={entry.name: entry},
        start_response=StartResponse(
            state="running", correlation_id=cid, audit_write_deferred=False
        ),
    )
    client = TestClient(_build_app(stub))

    response = client.post(
        f"/admin/services/{entry.name}/start",
        json={"env_overrides": {"PORT": "8080", "API_TOKEN": "secret"}},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "running"
    assert UUID(body["correlation_id"]) == cid
    assert stub.start_calls[0]["env_overrides"] == {
        "PORT": "8080",
        "API_TOKEN": "secret",
    }


def test_start_404_on_unknown_service() -> None:
    stub = _StubLifecycleService(
        raise_on_start=UnknownServiceError("ghost"),
    )
    client = TestClient(_build_app(stub))

    response = client.post(
        "/admin/services/ghost/start", json={"env_overrides": {}}
    )

    assert response.status_code == 404


def test_start_422_on_form_schema_mismatch() -> None:
    """Invalid lifecycle state maps to router 422."""

    from src.lifecycle.service import FormSchemaMismatchError

    stub = _StubLifecycleService(
        raise_on_start=FormSchemaMismatchError(
            "env_overrides for 'automation-service' do not match form schema"
        ),
    )
    client = TestClient(_build_app(stub))

    response = client.post(
        "/admin/services/automation-service/start",
        json={"env_overrides": {"PORT": "8080"}},
    )

    assert response.status_code == 422
    assert "form schema" in response.json()["detail"]


def test_start_409_on_feature_flag_disabled_carries_blocking_flag() -> None:
    """Credential precondition failure maps to router 409 with structured envelope.

    The ``FeatureFlagDisabledError`` raised by the lifecycle handler
    maps to ``409 Conflict`` and carries an envelope
    of the shape ``{"error": "feature_flag_disabled", "blocking_flag":
    <name>, "detail": "..."}`` so the UI can render the targeted
    Feature Flags page link.
    """

    from src.lifecycle.service import FeatureFlagDisabledError

    stub = _StubLifecycleService(
        raise_on_start=FeatureFlagDisabledError(
            blocking_flag="FEATURE_FLAG_TASK_INTAKE_ENABLED"
        ),
    )
    client = TestClient(_build_app(stub))

    response = client.post(
        "/admin/services/automation-service/start",
        json={"env_overrides": {}},
    )

    assert response.status_code == 409
    body = response.json()
    # FastAPI wraps the structured detail under the "detail" key.
    assert isinstance(body["detail"], dict)
    assert body["detail"]["error"] == "feature_flag_disabled"
    assert body["detail"]["blocking_flag"] == "FEATURE_FLAG_TASK_INTAKE_ENABLED"
    assert "FEATURE_FLAG_TASK_INTAKE_ENABLED" in body["detail"]["detail"]


def test_restart_409_on_feature_flag_disabled_carries_blocking_flag() -> None:
    """``restart`` re-enters the feature-flag gate and returns the same 409 envelope."""

    from src.lifecycle.service import FeatureFlagDisabledError

    stub = _StubLifecycleService(
        raise_on_restart=FeatureFlagDisabledError(
            blocking_flag="FEATURE_FLAG_FORGE_ADDON_ENABLED"
        ),
    )
    client = TestClient(_build_app(stub))

    response = client.post("/admin/services/automation-service/restart")

    assert response.status_code == 409
    body = response.json()
    assert body["detail"]["error"] == "feature_flag_disabled"
    assert body["detail"]["blocking_flag"] == "FEATURE_FLAG_FORGE_ADDON_ENABLED"


def test_start_502_on_vault_failure_carries_correlation_id() -> None:
    """Vault write failures map to 502 with ``correlation_id``."""

    stub = _StubLifecycleService(
        raise_on_start=VaultWriteError(
            operation="write",
            service_name="automation-service",
            key="API_TOKEN",
            status_code=500,
            message="vault is on fire",
        ),
    )
    client = TestClient(_build_app(stub))

    response = client.post(
        "/admin/services/automation-service/start",
        json={"env_overrides": {"API_TOKEN": "x"}},
    )

    assert response.status_code == 502
    body = response.json()
    assert "correlation_id" in body
    assert UUID(body["correlation_id"])  # parses cleanly
    assert "Vault" in body["detail"]


def test_start_502_on_audit_unreachable_carries_correlation_id() -> None:
    """Audit write failures map to 502 with ``correlation_id``."""

    stub = _StubLifecycleService(
        raise_on_start=AuditUnreachableError("postgres is down"),
    )
    client = TestClient(_build_app(stub))

    response = client.post(
        "/admin/services/automation-service/start",
        json={"env_overrides": {}},
    )

    assert response.status_code == 502
    assert UUID(response.json()["correlation_id"])


def test_start_502_on_compose_failure_carries_correlation_id() -> None:
    """Compose non-zero exit  502 + correlation_id."""

    failing_result = ComposeResult(
        exit_code=1, stdout="", stderr="boom", argv=("docker", "compose", "up")
    )
    stub = _StubLifecycleService(
        raise_on_start=ComposeFailureError(
            "docker compose up failed with exit code 1",
            result=failing_result,
        ),
    )
    client = TestClient(_build_app(stub))

    response = client.post(
        "/admin/services/automation-service/start",
        json={"env_overrides": {}},
    )

    assert response.status_code == 502
    body = response.json()
    assert UUID(body["correlation_id"])
    assert "exit code 1" in body["detail"]


# ---------------------------------------------------------------------------
# POST /admin/services/{name}/stop
# ---------------------------------------------------------------------------


def test_stop_returns_200_with_state_and_noop() -> None:
    """200 + ``{state, [noop]}``.

    ``remove_volumes`` defaults to ``false`` when the body is omitted
    already stopped.
    """

    cid = uuid4()
    stub = _StubLifecycleService(
        stop_response=StopResponse(
            state="stopped", correlation_id=cid, noop=False
        ),
    )
    client = TestClient(_build_app(stub))

    response = client.post("/admin/services/automation-service/stop", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "stopped"
    assert body["noop"] is False
    assert UUID(body["correlation_id"]) == cid
    assert stub.stop_calls[0]["remove_volumes"] is False


def test_stop_idempotent_returns_noop_true() -> None:
    """Already-stopped  ``noop=True``."""

    cid = uuid4()
    stub = _StubLifecycleService(
        stop_response=StopResponse(
            state="stopped", correlation_id=cid, noop=True
        ),
    )
    client = TestClient(_build_app(stub))

    response = client.post(
        "/admin/services/automation-service/stop",
        json={"remove_volumes": True},
    )

    assert response.status_code == 200
    assert response.json()["noop"] is True
    assert stub.stop_calls[0]["remove_volumes"] is True


def test_stop_502_on_compose_failure() -> None:
    failing_result = ComposeResult(
        exit_code=1, stdout="", stderr="x", argv=("docker", "compose", "stop")
    )
    stub = _StubLifecycleService(
        raise_on_stop=ComposeFailureError("stop failed", result=failing_result),
    )
    client = TestClient(_build_app(stub))

    response = client.post("/admin/services/automation-service/stop", json={})

    assert response.status_code == 502
    assert UUID(response.json()["correlation_id"])


# ---------------------------------------------------------------------------
# POST /admin/services/{name}/stop - purge_vault production guard
# ---------------------------------------------------------------------------


def test_stop_purge_vault_default_false_does_not_invoke_guard() -> None:
    """``purge_vault`` defaults to ``False`` - production profile is
    irrelevant on this path.

    The body explicitly omits ``purge_vault`` so the guard cannot fire
    even when ``deployment_profile == "production"``. The router must
    delegate to ``LifecycleService.stop`` and return 200.
    """

    cid = uuid4()
    entry = _entry()
    stub = _StubLifecycleService(
        by_name={entry.name: entry},
        stop_response=StopResponse(
            state="stopped", correlation_id=cid, noop=False
        ),
    )
    client = TestClient(
        _build_app(stub, deployment_profile="production")
    )

    response = client.post(
        "/admin/services/automation-service/stop",
        json={},
    )

    assert response.status_code == 200
    assert response.json()["state"] == "stopped"
    # Guard never fired - no audit row written.
    assert stub.purge_vault_blocked_calls == []
    # Stop *was* invoked because the body's ``purge_vault`` defaulted
    # to false.
    assert len(stub.stop_calls) == 1
    assert stub.stop_calls[0]["remove_volumes"] is False


def test_stop_purge_vault_true_in_production_returns_403() -> None:
    """``purge_vault=true`` is forbidden on the
    production deployment profile.

    The endpoint must:

    * Return ``403 Forbidden`` with the
      ``purge_vault_forbidden_in_production`` error envelope.
    * Write a ``purge_vault_blocked_in_production`` audit row via
      :meth:`LifecycleService.record_purge_vault_blocked`.
    * NOT invoke ``LifecycleService.stop`` - Compose must remain
      untouched.
    """

    entry = _entry()
    stub = _StubLifecycleService(
        by_name={entry.name: entry},
        # ``stop_response`` deliberately unset so the test fails loudly
        # if the router accidentally falls through to ``svc.stop``.
    )
    client = TestClient(
        _build_app(stub, deployment_profile="production")
    )

    response = client.post(
        "/admin/services/automation-service/stop",
        json={"purge_vault": True},
    )

    assert response.status_code == 403
    body = response.json()
    detail = body["detail"]
    assert detail["error"] == "purge_vault_forbidden_in_production"
    assert "production" in detail["detail"].lower()
    # Audit row recorded with the canonical actor + service name.
    assert len(stub.purge_vault_blocked_calls) == 1
    assert stub.purge_vault_blocked_calls[0]["name"] == "automation-service"
    # ``svc.stop`` MUST NOT have been called - Compose stays untouched.
    assert stub.stop_calls == []


@pytest.mark.parametrize("profile", ["PRODUCTION", "Production", "production"])
def test_stop_purge_vault_production_match_is_case_insensitive(
    profile: str,
) -> None:
    """``deployment_profile`` matching is case
    insensitive.

    Operators commonly normalise the env var via shell exports
    (``DEPLOYMENT_PROFILE=Production``); the guard must catch every
    case-folding variant of ``"production"``.
    """

    entry = _entry()
    stub = _StubLifecycleService(by_name={entry.name: entry})
    client = TestClient(
        _build_app(stub, deployment_profile=profile)
    )

    response = client.post(
        "/admin/services/automation-service/stop",
        json={"purge_vault": True},
    )

    assert response.status_code == 403
    assert (
        response.json()["detail"]["error"]
        == "purge_vault_forbidden_in_production"
    )
    assert stub.stop_calls == []


@pytest.mark.parametrize("profile", ["dev", "staging", "DEV", "Staging", "test"])
def test_stop_purge_vault_true_allowed_outside_production(
    profile: str,
) -> None:
    """Non-production profiles let ``purge_vault``
    through.

    The body's ``purge_vault=true`` flag is accepted but the actual
    Vault-purge behaviour is handled separately; for now the router only
    delegates to ``LifecycleService.stop`` with the existing
    ``remove_volumes`` plumbing. The guard MUST NOT fire on dev /
    staging.
    """

    cid = uuid4()
    entry = _entry()
    stub = _StubLifecycleService(
        by_name={entry.name: entry},
        stop_response=StopResponse(
            state="stopped", correlation_id=cid, noop=False
        ),
    )
    client = TestClient(_build_app(stub, deployment_profile=profile))

    response = client.post(
        "/admin/services/automation-service/stop",
        json={"purge_vault": True},
    )

    assert response.status_code == 200
    assert response.json()["state"] == "stopped"
    # Guard MUST NOT fire on non-production profiles.
    assert stub.purge_vault_blocked_calls == []
    # Stop was invoked once.
    assert len(stub.stop_calls) == 1


def test_stop_purge_vault_true_unknown_service_returns_404() -> None:
    """Unknown service surfaces 404 even
    when ``purge_vault=true`` is passed in production.

    The 404 takes precedence over the production guard because an
    unknown service is a routing miss (not a security event) and we
    do not want to pollute the audit trail with rows whose
    ``service_name`` does not exist in the manifest.
    """

    stub = _StubLifecycleService()  # empty manifest
    client = TestClient(
        _build_app(stub, deployment_profile="production")
    )

    response = client.post(
        "/admin/services/does-not-exist/stop",
        json={"purge_vault": True},
    )

    assert response.status_code == 404
    # No audit row - guard short-circuited on the 404 path.
    assert stub.purge_vault_blocked_calls == []
    assert stub.stop_calls == []


def test_stop_purge_vault_true_audit_unreachable_still_returns_403() -> None:
    """Audit write is best-effort.

    A transient audit-DB outage MUST NOT escalate the 403 into a 502
    - the guard still has to refuse the destructive request. The
    audit row is the observability layer; the response code is the
    canonical security boundary.
    """

    entry = _entry()
    stub = _StubLifecycleService(
        by_name={entry.name: entry},
        raise_on_purge_vault_blocked=AuditUnreachableError("audit DB down"),
    )
    client = TestClient(
        _build_app(stub, deployment_profile="production")
    )

    response = client.post(
        "/admin/services/automation-service/stop",
        json={"purge_vault": True},
    )

    assert response.status_code == 403
    assert (
        response.json()["detail"]["error"]
        == "purge_vault_forbidden_in_production"
    )
    # The helper *was* called - the AuditUnreachableError fired
    # inside, but the router caught it and proceeded with the 403.
    assert len(stub.purge_vault_blocked_calls) == 1
    assert stub.stop_calls == []


def test_stop_request_schema_accepts_purge_vault_field() -> None:
    """``StopRequest`` schema accepts the
    ``purge_vault`` field with default ``False``.

    Direct schema-level test (no FastAPI involved) that documents the
    additive-only contract: existing callers passing only
    ``remove_volumes`` keep working, and the new flag defaults to
    ``False`` so the production guard cannot fire on a no-flag body.
    """

    from src.routers._models import StopRequest as _StopRequest

    # Field default is False (additive on top of remove_volumes).
    default = _StopRequest()
    assert default.remove_volumes is False
    assert default.purge_vault is False

    # Old-style body (only remove_volumes) parses cleanly.
    legacy = _StopRequest.model_validate({"remove_volumes": True})
    assert legacy.remove_volumes is True
    assert legacy.purge_vault is False

    # New body shape works too.
    new = _StopRequest.model_validate(
        {"remove_volumes": True, "purge_vault": True}
    )
    assert new.remove_volumes is True
    assert new.purge_vault is True


# ---------------------------------------------------------------------------
# POST /admin/services/{name}/restart
# ---------------------------------------------------------------------------


def test_restart_returns_202() -> None:
    """202 + ``{state, correlation_id}``."""

    cid = uuid4()
    stub = _StubLifecycleService(
        restart_response=StartResponse(state="running", correlation_id=cid),
    )
    client = TestClient(_build_app(stub))

    response = client.post("/admin/services/automation-service/restart")

    assert response.status_code == 202
    body = response.json()
    assert body["state"] == "running"
    assert UUID(body["correlation_id"]) == cid
    assert stub.restart_calls == [{"name": "automation-service", "actor": _actor_claims()}]


def _actor_claims() -> AuthClaims:
    """Return the same :class:`AuthClaims` the test app injects.

    Used for `==` comparisons against the stub's recorded calls so the
    assertions stay readable.
    """

    return AuthClaims(sub="ops-1", groups=("admin",))


def test_restart_404_on_unknown_service() -> None:
    stub = _StubLifecycleService(raise_on_restart=UnknownServiceError("ghost"))
    client = TestClient(_build_app(stub))

    response = client.post("/admin/services/ghost/restart")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /admin/services/{name}/test
# ---------------------------------------------------------------------------


def test_run_tests_returns_summary_when_present() -> None:
    """``{output, exit_code, summary}`` on success."""

    cid = uuid4()
    stub = _StubLifecycleService(
        test_response=RunTestsResponse(
            output="3 passed in 0.42s",
            exit_code=0,
            summary=TestSummary(passed=3, failed=0, duration_seconds=0.42),
            correlation_id=cid,
        ),
    )
    client = TestClient(_build_app(stub))

    response = client.post("/admin/services/automation-service/test")

    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0
    assert body["summary"]["passed"] == 3
    assert body["summary"]["duration_seconds"] == pytest.approx(0.42)


def test_run_tests_409_when_service_not_running() -> None:
    """409 ``service must be running before tests``."""

    stub = _StubLifecycleService(
        raise_on_test=TestPreconditionError("service must be running before tests"),
    )
    client = TestClient(_build_app(stub))

    response = client.post("/admin/services/automation-service/test")

    assert response.status_code == 409
    assert "running" in response.json()["detail"]


def test_run_tests_409_when_no_test_command_in_manifest() -> None:
    """409 ``service has no test_command in manifest``."""

    stub = _StubLifecycleService(
        raise_on_test=TestPreconditionError(
            "service has no test_command in manifest"
        ),
    )
    client = TestClient(_build_app(stub))

    response = client.post("/admin/services/agent-runner-worker/test")

    assert response.status_code == 409
    assert "test_command" in response.json()["detail"]


def test_run_tests_sse_when_stream_query_param() -> None:
    """``?stream=true`` returns ``text/event-stream``."""

    stub = _StubLifecycleService(
        test_response=RunTestsResponse(
            output="line one\nline two",
            exit_code=0,
            summary=None,
            correlation_id=uuid4(),
        ),
    )
    client = TestClient(_build_app(stub))

    response = client.post(
        "/admin/services/automation-service/test?stream=true"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "data: line one" in response.text
    assert "data: line two" in response.text
    # The terminal ``done`` event carries the exit_code.
    assert "event: done" in response.text


# ---------------------------------------------------------------------------
# GET /admin/services/{name}/logs
# ---------------------------------------------------------------------------


def test_get_logs_returns_lines_array_by_default() -> None:
    """JSON body ``{lines: [...]}``."""

    stub = _StubLifecycleService(
        logs_lines=["redis connected", "ready"],
    )
    client = TestClient(_build_app(stub))

    response = client.get("/admin/services/automation-service/logs")

    assert response.status_code == 200
    assert response.json() == {"lines": ["redis connected", "ready"]}
    # Default tail is 200.
    assert stub.logs_calls[0]["tail"] == 200
    assert stub.logs_calls[0]["follow"] is False


def test_get_logs_validates_tail_range() -> None:
    """``tail`` ∈ [1, 1000]; out-of-range  422."""

    stub = _StubLifecycleService()
    client = TestClient(_build_app(stub))

    too_small = client.get("/admin/services/automation-service/logs?tail=0")
    too_large = client.get("/admin/services/automation-service/logs?tail=1001")

    assert too_small.status_code == 422
    assert too_large.status_code == 422


def test_get_logs_streaming_returns_sse() -> None:
    """``follow=true`` returns ``text/event-stream``."""

    entry = _entry()
    stub = _StubLifecycleService(by_name={entry.name: entry})
    stub.compose.lines = ["frame one", "frame two"]
    client = TestClient(_build_app(stub))

    response = client.get(
        f"/admin/services/{entry.name}/logs?follow=true&tail=50"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "data: frame one" in response.text
    assert "data: frame two" in response.text
    assert stub.compose.calls[0] == {
        "service_name": entry.name,
        "tail": 50,
        "follow": True,
    }


def test_get_logs_404_on_unknown_service_streaming_path() -> None:
    """Streaming branch must still 404 unknown services up-front."""

    stub = _StubLifecycleService()
    client = TestClient(_build_app(stub))

    response = client.get("/admin/services/ghost/logs?follow=true")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /admin/services/{name}/health
# ---------------------------------------------------------------------------


def test_get_health_returns_snapshot() -> None:
    """Returns the fresh :class:`HealthSnapshot`."""

    entry = _entry()
    stub = _StubLifecycleService(
        by_name={entry.name: entry},
        snapshot=_snapshot("healthy"),
    )
    client = TestClient(_build_app(stub))

    response = client.get(f"/admin/services/{entry.name}/health")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "healthy"
    assert body["healthz_status"] == 200
    assert stub.health_calls == [entry.name]


def test_get_health_returns_unhealthy_snapshot_with_body() -> None:
    """Non-200 status  ``unhealthy``; body surfaced."""

    entry = _entry()
    stub = _StubLifecycleService(
        by_name={entry.name: entry},
        snapshot=_snapshot("unhealthy"),
    )
    client = TestClient(_build_app(stub))

    response = client.get(f"/admin/services/{entry.name}/health")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "unhealthy"
    assert body["healthz_body"] == "down"


def test_get_health_404_on_unknown_service() -> None:
    stub = _StubLifecycleService()
    client = TestClient(_build_app(stub))

    response = client.get("/admin/services/does-not-exist/health")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# POST /admin/services/{name}/probe
# ---------------------------------------------------------------------------


def test_probe_returns_200_with_credentials_status() -> None:
    """200 + ``{service_name, credentials_status, ...}`` on success."""

    from datetime import datetime, timezone

    probe_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    entry = _entry(name="automation-service")
    slot = LifecycleStateCache(
        name="automation-service",
        credentials_status="ok",
        credentials_probe_at=probe_at,
        credentials_probe_detail=None,
    )
    stub = _StubLifecycleService(
        by_name={"automation-service": entry},
        state_cache={"automation-service": slot},
    )
    client = TestClient(_build_app(stub))

    response = client.post("/admin/services/automation-service/probe")

    assert response.status_code == 200
    body = response.json()
    assert body["service_name"] == "automation-service"
    assert body["credentials_status"] == "ok"
    assert body["credentials_probe_detail"] is None
    # Verify the stub recorded the call
    assert len(stub.probe_calls) == 1
    assert stub.probe_calls[0]["name"] == "automation-service"


def test_probe_returns_200_with_failed_credentials_status() -> None:
    """Failed probe surfaces ``credentials_status='failed'`` + detail."""

    from datetime import datetime, timezone

    probe_at = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    entry = _entry(name="automation-service")
    slot = LifecycleStateCache(
        name="automation-service",
        credentials_status="failed",
        credentials_probe_at=probe_at,
        credentials_probe_detail="Connection refused: jira.example.com:443",
    )
    stub = _StubLifecycleService(
        by_name={"automation-service": entry},
        state_cache={"automation-service": slot},
    )
    client = TestClient(_build_app(stub))

    response = client.post("/admin/services/automation-service/probe")

    assert response.status_code == 200
    body = response.json()
    assert body["credentials_status"] == "failed"
    assert "Connection refused" in body["credentials_probe_detail"]


def test_probe_404_on_unknown_service() -> None:
    """Unknown service  404 Not Found."""

    stub = _StubLifecycleService()
    client = TestClient(_build_app(stub))

    response = client.post("/admin/services/does-not-exist/probe")

    assert response.status_code == 404


def test_probe_502_on_audit_unreachable() -> None:
    """AuditUnreachableError  502 + correlation_id."""

    entry = _entry(name="automation-service")
    slot = LifecycleStateCache(name="automation-service")
    stub = _StubLifecycleService(
        by_name={"automation-service": entry},
        state_cache={"automation-service": slot},
        raise_on_probe=AuditUnreachableError("db down"),
    )
    client = TestClient(_build_app(stub))

    response = client.post("/admin/services/automation-service/probe")

    assert response.status_code == 502
    body = response.json()
    assert "correlation_id" in body


def test_probe_returns_401_when_require_admin_fails() -> None:
    """Endpoint MUST be gated on ``require_admin``."""

    stub = _StubLifecycleService()
    client = TestClient(_build_app(stub, actor_sub=None))

    response = client.post("/admin/services/automation-service/probe")

    assert response.status_code == 401
