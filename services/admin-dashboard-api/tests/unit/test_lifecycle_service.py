"""Unit tests for ``src.lifecycle.service.LifecycleService``.
These tests exercise the orchestrator as a black box against fake
:class:`AuditWriter`, :class:`VaultClient`, :class:`ComposeRunner`,
and :class:`HealthProbe` collaborators. The fakes record every call
so we can assert ordering invariants - particularly the audit-or-
rollback semantics and the
``state="starting"``-before-response transition.
Coverage matrix
---------------
* ``start`` happy path - Vault writes per-key, audit precheck  pending
  row  Compose up  health probe  success row.
* ``start`` failures:
  - ``FormSchemaMismatchError`` on missing form key.
  - ``FormSchemaMismatchError`` on extra form key.
  - ``FormSchemaMismatchError`` on empty Sensitive_Env_Key value.
  - ``AuditUnreachableError`` when audit precheck fails.
  - ``VaultWriteError`` when a per-key Vault write fails.
  - ``ComposeFailureError`` when Compose ``up`` exits non-zero.
  - Health probe timeout - final state is ``failed``.
* ``stop`` idempotent behaviour.
* ``run_tests`` 409 semantics:
  - Service not running.
  - Manifest ``test_command`` is ``null``.
* ``logs`` redaction of Sensitive_Env_Key values
  ).
* ``health_of`` streak alert at threshold.
The tests follow the pytest + ``asyncio.run`` convention used by
``test_audit_writer.py`` so they integrate cleanly with the existing
unit-test runner."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pytest

# Bootstrap ``sys.path`` so ``import src.lifecycle.service`` resolves
# under direct ``pytest tests/unit`` invocations (mirrors the pattern
# used by the other unit-test modules in this folder).
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.audit_writer import (  # noqa: E402
    AuditEntry,
    AuditUnreachableError,
    AuditWriteOutcome,
)
from src.lifecycle.compose_runner import (  # noqa: E402
    ComposeFailureError,
    ComposeResult,
    TestResult,
)
from src.lifecycle.health_probe import HealthSnapshot  # noqa: E402
from src.lifecycle.service import (  # noqa: E402
    FeatureFlagDisabledError,
    FormSchemaMismatchError,
    LifecycleService,
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

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditWriter:
    """Records every audit interaction; configurable failure modes."""

    precheck_calls: int = 0
    write_calls: list[AuditEntry] = field(default_factory=list)
    write_with_retry_calls: list[AuditEntry] = field(default_factory=list)
    precheck_raise: BaseException | None = None
    write_raise: BaseException | None = None
    write_with_retry_deferred: bool = False

    async def precheck(self) -> None:
        self.precheck_calls += 1
        if self.precheck_raise is not None:
            raise self.precheck_raise

    async def write(self, entry: AuditEntry) -> None:
        self.write_calls.append(entry)
        if self.write_raise is not None:
            raise self.write_raise

    async def write_with_retry(self, entry: AuditEntry) -> AuditWriteOutcome:
        self.write_with_retry_calls.append(entry)
        return AuditWriteOutcome(deferred=self.write_with_retry_deferred)


@dataclass
class _FakeVaultClient:
    """Records every Vault interaction; configurable failure modes."""

    writes: list[tuple[str, str, str]] = field(default_factory=list)
    write_raise: BaseException | None = None
    stored: dict[str, dict[str, str]] = field(default_factory=dict)
    raise_on_key: str | None = None
    # ------------------------------------------------------------------
    # Vault purge instrumentation
    # ------------------------------------------------------------------
    #: Calls made to :meth:`list_env_override_keys`. Each entry is the
    #: ``service_name`` argument so ordering / count assertions stay
    #: cheap.
    list_calls: list[str] = field(default_factory=list)
    #: Calls made to :meth:`delete_env_override`. Each entry is a
    #: ``(service_name, key)`` tuple recorded **before** the optional
    #: ``raise_on_delete_after`` failure fires; this means
    #: ``len(delete_calls)`` reflects how many keys the orchestrator
    #: *attempted* to delete, while ``stored`` reflects which ones
    #: actually succeeded.
    delete_calls: list[tuple[str, str]] = field(default_factory=list)
    #: When set, the next :meth:`list_env_override_keys` call raises
    #: this exception. Used by the purge tests to simulate a Vault
    #: outage on the LIST step (zero deletions, partial-failure audit).
    list_raise: BaseException | None = None
    #: When set, :meth:`delete_env_override` raises this exception
    #: AFTER successfully deleting ``raise_on_delete_after`` keys.
    #: Used by the purge tests to simulate a Vault outage mid-flight
    #: (some keys deleted, partial-failure audit with the partial
    #: count).
    raise_on_delete_after: int | None = None
    delete_raise: BaseException | None = None

    async def write_env_override(
        self, *, service_name: str, key: str, value: str
    ) -> None:
        if self.raise_on_key == key:
            raise VaultWriteError(
                operation="write",
                service_name=service_name,
                key=key,
                status_code=500,
                message="injected failure",
            )
        if self.write_raise is not None:
            raise self.write_raise
        self.writes.append((service_name, key, value))
        self.stored.setdefault(service_name, {})[key] = value

    async def read_env_overrides(self, *, service_name: str) -> dict[str, str]:
        return dict(self.stored.get(service_name, {}))

    async def list_env_override_keys(self, *, service_name: str) -> list[str]:
        """Mirror of :meth:`VaultClient.list_env_override_keys`.

        Records every call so the purge tests can assert ordering;
        honours :attr:`list_raise` so callers can simulate a Vault
        outage on the LIST step.
        """

        self.list_calls.append(service_name)
        if self.list_raise is not None:
            raise self.list_raise
        return list(self.stored.get(service_name, {}).keys())

    async def delete_env_override(
        self, *, service_name: str, key: str
    ) -> None:
        """Soft-delete a single Env_Override key.

        Records every call so the purge tests can assert which keys
        the orchestrator attempted to remove. Honours
        :attr:`delete_raise` (always-fail) and
        :attr:`raise_on_delete_after` (fail after N successful
        deletes) so callers can simulate the two purge-failure modes
        handled by the lifecycle service.
        """

        self.delete_calls.append((service_name, key))
        if self.delete_raise is not None:
            raise self.delete_raise
        if (
            self.raise_on_delete_after is not None
            and len(self.delete_calls) > self.raise_on_delete_after
        ):
            raise VaultWriteError(
                operation="delete",
                service_name=service_name,
                key=key,
                status_code=500,
                message="injected purge failure",
            )
        self.stored.get(service_name, {}).pop(key, None)


@dataclass
class _FakeComposeRunner:
    """Records every Compose call; programmable per-method failure."""

    up_calls: list[dict[str, Any]] = field(default_factory=list)
    stop_calls: list[dict[str, Any]] = field(default_factory=list)
    logs_calls: list[dict[str, Any]] = field(default_factory=list)
    exec_test_calls: list[dict[str, Any]] = field(default_factory=list)

    up_raise: BaseException | None = None
    stop_raise: BaseException | None = None
    logs_stdout: str = ""
    test_stdout: str = ""
    test_exit_code: int = 0

    async def up(
        self,
        *,
        profile: str,
        service_name: str,
        env_overrides: dict[str, str] | None = None,
    ) -> ComposeResult:
        self.up_calls.append(
            {
                "profile": profile,
                "service_name": service_name,
                "env_overrides": dict(env_overrides or {}),
            }
        )
        if self.up_raise is not None:
            raise self.up_raise
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "up", "-d", service_name),
        )

    async def stop(
        self, *, service_name: str, remove_volumes: bool = False
    ) -> ComposeResult:
        self.stop_calls.append(
            {"service_name": service_name, "remove_volumes": remove_volumes}
        )
        if self.stop_raise is not None:
            raise self.stop_raise
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "stop", service_name),
        )

    async def logs(
        self, *, service_name: str, tail: int, follow: bool
    ) -> ComposeResult:
        self.logs_calls.append(
            {"service_name": service_name, "tail": tail, "follow": follow}
        )
        return ComposeResult(
            exit_code=0,
            stdout=self.logs_stdout,
            stderr="",
            argv=("docker", "compose", "logs", service_name),
        )

    async def exec_test(
        self,
        *,
        service_name: str,
        argv: Sequence[str],
        stream: bool = False,
    ) -> TestResult:
        self.exec_test_calls.append(
            {
                "service_name": service_name,
                "argv": tuple(argv),
                "stream": stream,
            }
        )
        return TestResult(
            exit_code=self.test_exit_code,
            stdout=self.test_stdout,
            stderr="",
            argv=tuple(argv),
        )


@dataclass
class _FakeHealthProbe:
    """Returns a programmed sequence of HealthSnapshots, then repeats the last."""

    snapshots: list[HealthSnapshot] = field(default_factory=list)
    calls: list[ManagedServiceEntry] = field(default_factory=list)
    _idx: int = 0

    async def probe(self, entry: ManagedServiceEntry) -> HealthSnapshot:
        self.calls.append(entry)
        if not self.snapshots:
            return HealthSnapshot(
                ts=datetime.now(timezone.utc),
                healthz_status=200,
                healthz_body="ok",
                readyz_status=200,
                readyz_body="ok",
                state="healthy",
            )
        idx = min(self._idx, len(self.snapshots) - 1)
        self._idx += 1
        return self.snapshots[idx]


@dataclass
class _FakeFeatureFlagReader:
    """Records every ``fetch_enabled_flags`` call.
    Backs the (feature-flag start gate) tests by returning a
    pre-canned ``flags`` map. Missing keys are absent from the returned
    dict so ``LifecycleService._check_feature_flags`` can exercise the
    "missing row  treat as disabled" branch."""

    flags: dict[str, bool] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    raise_exc: BaseException | None = None

    async def fetch_enabled_flags(
        self, names: "Sequence[str]"
    ) -> dict[str, bool]:
        self.calls.append(list(names))
        if self.raise_exc is not None:
            raise self.raise_exc
        return {name: self.flags[name] for name in names if name in self.flags}


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


HTTP_ENV_EXAMPLE = (
    "# Plain config knob\n"
    "PORT=8080\n"
    "# A sensitive token\n"
    'API_TOKEN=""\n'
)

WORKER_ENV_EXAMPLE = "WORKER_NAME=alpha\n"


def _build_workspace(tmp_path: Path) -> Path:
    """Materialise a synthetic workspace with two .env.example files."""

    http_dir = tmp_path / "services" / "automation-service"
    http_dir.mkdir(parents=True)
    (http_dir / ".env.example").write_text(HTTP_ENV_EXAMPLE, encoding="utf-8")

    worker_dir = tmp_path / "workers" / "agent-runner-worker"
    worker_dir.mkdir(parents=True)
    (worker_dir / ".env.example").write_text(WORKER_ENV_EXAMPLE, encoding="utf-8")

    return tmp_path


def _entries() -> tuple[ManagedServiceEntry, ...]:
    return (
        ManagedServiceEntry(
            name="automation-service",
            kind="http_service",
            compose_service_name="automation-service",
            compose_profile="automation-service",
            env_example_path="services/automation-service/.env.example",
            health_endpoint="/healthz",
            test_command=(
                "docker compose -f infra/docker-compose.yml exec "
                "automation-service pytest tests/integration/ -v"
            ),
        ),
        ManagedServiceEntry(
            name="agent-runner-worker",
            kind="worker",
            compose_service_name="agent-runner-worker",
            compose_profile="agent-runner-worker",
            env_example_path="workers/agent-runner-worker/.env.example",
            health_endpoint=None,
            test_command=None,
        ),
    )


def _entries_with_flag_gate(
    *, flag_names: tuple[str, ...] = ("FEATURE_FLAG_TASK_INTAKE_ENABLED",)
) -> tuple[ManagedServiceEntry, ...]:
    """Manifest fixture for the feature-flag start gate tests.
    Mirrors :func:`_entries` but tags ``automation-service`` with
    ``feature_flag_dependency=flag_names`` so the gate runs before
    form validation. Order of the tuple matters - the first disabled
    flag in manifest order is the deterministic ``blocking_flag``
    returned by the exception."""

    return (
        ManagedServiceEntry(
            name="automation-service",
            kind="http_service",
            compose_service_name="automation-service",
            compose_profile="automation-service",
            env_example_path="services/automation-service/.env.example",
            health_endpoint="/healthz",
            test_command=(
                "docker compose -f infra/docker-compose.yml exec "
                "automation-service pytest tests/integration/ -v"
            ),
            feature_flag_dependency=flag_names,
        ),
        ManagedServiceEntry(
            name="agent-runner-worker",
            kind="worker",
            compose_service_name="agent-runner-worker",
            compose_profile="agent-runner-worker",
            env_example_path="workers/agent-runner-worker/.env.example",
            health_endpoint=None,
            test_command=None,
        ),
    )


def _healthy_snapshot() -> HealthSnapshot:
    return HealthSnapshot(
        ts=datetime.now(timezone.utc),
        healthz_status=200,
        healthz_body="ok",
        readyz_status=200,
        readyz_body="ok",
        state="healthy",
    )


def _unhealthy_snapshot() -> HealthSnapshot:
    return HealthSnapshot(
        ts=datetime.now(timezone.utc),
        healthz_status=503,
        healthz_body="not ready",
        readyz_status=503,
        readyz_body="db down",
        state="unhealthy",
    )


def _make_service(
    *,
    workspace_root: Path,
    audit: _FakeAuditWriter | None = None,
    vault: _FakeVaultClient | None = None,
    compose: _FakeComposeRunner | None = None,
    health: _FakeHealthProbe | None = None,
    feature_flag_reader: _FakeFeatureFlagReader | None = None,
    manifest: tuple[ManagedServiceEntry, ...] | None = None,
    health_ready_timeout_seconds: float = 1.0,
    health_fail_streak_threshold: int = 3,
    initial_state: dict[str, LifecycleStateCache] | None = None,
    clock: "Any" = None,
) -> tuple[
    LifecycleService,
    _FakeAuditWriter,
    _FakeVaultClient,
    _FakeComposeRunner,
    _FakeHealthProbe,
]:
    audit = audit or _FakeAuditWriter()
    vault = vault or _FakeVaultClient()
    compose = compose or _FakeComposeRunner()
    health = health or _FakeHealthProbe(snapshots=[_healthy_snapshot()])

    # Replace asyncio.sleep with a no-op so the health-poll loop in
    # ``start`` does not waste real wall-clock time during tests.
    async def _no_sleep(_seconds: float) -> None:
        return None

    svc = LifecycleService(
        manifest=manifest if manifest is not None else _entries(),
        state=initial_state,
        audit=audit,  # type: ignore[arg-type]
        vault=vault,  # type: ignore[arg-type]
        compose=compose,  # type: ignore[arg-type]
        health=health,  # type: ignore[arg-type]
        workspace_root=workspace_root,
        feature_flag_reader=feature_flag_reader,  # type: ignore[arg-type]
        health_ready_timeout_seconds=health_ready_timeout_seconds,
        health_fail_streak_threshold=health_fail_streak_threshold,
        sleep=_no_sleep,
        clock=clock,
    )
    return svc, audit, vault, compose, health


class _AdvancingClock:
    """Clock that returns a UTC datetime advancing by ``step`` per call.

    Used by the streak-alert tests to defeat the
    ``HEALTH_POLL_INTERVAL_SECONDS / 2`` cache TTL - every call returns
    a moment far enough in the future that the cache always misses.
    """

    def __init__(self, step_seconds: float = 60.0) -> None:
        self._tick = 0
        self._step = step_seconds

    def __call__(self) -> datetime:
        ts = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        offset_seconds = self._tick * self._step
        self._tick += 1
        return datetime.fromtimestamp(
            ts.timestamp() + offset_seconds, tz=timezone.utc
        )


# ---------------------------------------------------------------------------
# Form schema
# ---------------------------------------------------------------------------


def test_get_form_schema_returns_lhs_keys(tmp_path: Path) -> None:
    workspace = _build_workspace(tmp_path)
    svc, *_ = _make_service(workspace_root=workspace)

    schema = svc.get_form_schema("automation-service")
    keys = {f.key for f in schema}
    assert keys == {"PORT", "API_TOKEN"}
    api_token = next(f for f in schema if f.key == "API_TOKEN")
    assert api_token.is_sensitive is True
    port = next(f for f in schema if f.key == "PORT")
    assert port.is_sensitive is False


def test_get_form_schema_unknown_service(tmp_path: Path) -> None:
    workspace = _build_workspace(tmp_path)
    svc, *_ = _make_service(workspace_root=workspace)

    with pytest.raises(UnknownServiceError):
        svc.get_form_schema("does-not-exist")


# ---------------------------------------------------------------------------
# compute_start_plan
# ---------------------------------------------------------------------------


def _entries_with_chain() -> tuple[ManagedServiceEntry, ...]:
    """Manifest fixture exercising a non-trivial dependency chain.

    Graph (manifest-resident nodes only):

    * ``automation-service``  ``atlassian-mcp``, ``postgres`` (external).
    * ``atlassian-mcp``  ``firecrawl``, ``vault`` (external).
    * ``firecrawl``, ``admin-dashboard-api`` are leaves.
    * ``admin-dashboard-api`` is unrelated to the chain - included so
      the plan does *not* leak unrelated services into ``will_start``.

    The external deps (``postgres``, ``vault``) are intentionally not
    manifest entries so the test can assert they are filtered out of
    the plan even though they appear in ``depends_on_services``.
    """

    return (
        ManagedServiceEntry(
            name="automation-service",
            kind="http_service",
            compose_service_name="automation-service",
            compose_profile="automation-service",
            env_example_path="services/automation-service/.env.example",
            health_endpoint="/healthz",
            test_command=None,
            depends_on_services=("atlassian-mcp", "postgres"),
        ),
        ManagedServiceEntry(
            name="atlassian-mcp",
            kind="infra",
            compose_service_name="atlassian-mcp",
            compose_profile="atlassian-mcp",
            env_example_path="services/atlassian-mcp/.env.example",
            health_endpoint=None,
            test_command=None,
            depends_on_services=("firecrawl", "vault"),
        ),
        ManagedServiceEntry(
            name="firecrawl",
            kind="infra",
            compose_service_name="firecrawl",
            compose_profile="firecrawl",
            env_example_path="services/firecrawl/.env.example",
            health_endpoint=None,
            test_command=None,
        ),
        ManagedServiceEntry(
            name="admin-dashboard-api",
            kind="http_service",
            compose_service_name="admin-dashboard-api",
            compose_profile="admin-dashboard-api",
            env_example_path="services/admin-dashboard-api/.env.example",
            health_endpoint="/healthz",
            test_command=None,
        ),
    )


def test_compute_start_plan_topological_order_with_external_deps_filtered(
    tmp_path: Path,
) -> None:
    """- dependencies appear *before* dependents; externals dropped.
    The post-order DFS over the dependency graph guarantees
    ``firecrawl`` lands before ``atlassian-mcp`` (its dependent) and
    ``automation-service`` (the root) lands last. The external
    dependencies ``postgres`` and ``vault`` - present in
    ``depends_on_services`` but not as manifest entries - must NOT
    appear in either output bucket because the lifecycle service
    cannot start them."""

    workspace = _build_workspace(tmp_path)
    svc, *_ = _make_service(
        workspace_root=workspace, manifest=_entries_with_chain()
    )

    plan = svc.compute_start_plan("automation-service")

    assert plan.target_service == "automation-service"
    # Dependencies before dependents (post-order).
    assert plan.will_start == (
        "firecrawl",
        "atlassian-mcp",
        "automation-service",
    )
    # Nothing is running yet.
    assert plan.already_running == ()


def test_compute_start_plan_already_running_filtered(tmp_path: Path) -> None:
    """- services already in ``running`` are partitioned into
    ``already_running`` and excluded from ``will_start`` so the UI
    can communicate the idempotent skip behaviour to the operator."""

    workspace = _build_workspace(tmp_path)
    initial_state: dict[str, LifecycleStateCache] = {
        "automation-service": LifecycleStateCache(
            name="automation-service", state="stopped"
        ),
        "atlassian-mcp": LifecycleStateCache(
            name="atlassian-mcp", state="running"
        ),
        "firecrawl": LifecycleStateCache(
            name="firecrawl", state="running"
        ),
        "admin-dashboard-api": LifecycleStateCache(
            name="admin-dashboard-api", state="stopped"
        ),
    }
    svc, *_ = _make_service(
        workspace_root=workspace,
        manifest=_entries_with_chain(),
        initial_state=initial_state,
    )

    plan = svc.compute_start_plan("automation-service")

    assert plan.target_service == "automation-service"
    assert plan.will_start == ("automation-service",)
    # ``already_running`` keeps the post-order traversal sequence so
    # the UI's "already running" list matches the visit order.
    assert plan.already_running == ("firecrawl", "atlassian-mcp")


def test_compute_start_plan_target_already_running(tmp_path: Path) -> None:
    """When the target itself is running the plan is fully a no-op:
    ``will_start`` is empty and the target appears in
    ``already_running``."""

    workspace = _build_workspace(tmp_path)
    initial_state: dict[str, LifecycleStateCache] = {
        "automation-service": LifecycleStateCache(
            name="automation-service", state="running"
        ),
        "atlassian-mcp": LifecycleStateCache(
            name="atlassian-mcp", state="running"
        ),
        "firecrawl": LifecycleStateCache(
            name="firecrawl", state="running"
        ),
        "admin-dashboard-api": LifecycleStateCache(
            name="admin-dashboard-api", state="stopped"
        ),
    }
    svc, *_ = _make_service(
        workspace_root=workspace,
        manifest=_entries_with_chain(),
        initial_state=initial_state,
    )

    plan = svc.compute_start_plan("automation-service")

    assert plan.will_start == ()
    assert plan.already_running == (
        "firecrawl",
        "atlassian-mcp",
        "automation-service",
    )


def test_compute_start_plan_unknown_service_raises(tmp_path: Path) -> None:
    """Unknown service  :class:`UnknownServiceError` (router 404)."""

    workspace = _build_workspace(tmp_path)
    svc, *_ = _make_service(workspace_root=workspace)

    with pytest.raises(UnknownServiceError):
        svc.compute_start_plan("does-not-exist")


def test_compute_start_plan_diamond_dependency_no_duplicates(
    tmp_path: Path,
) -> None:
    """Shared deps in a diamond ``A  B,C  D`` must appear once.

    The DFS ``visited`` set ensures ``D`` is post-ordered exactly
    once even though both ``B`` and ``C`` depend on it.
    """

    diamond_manifest: tuple[ManagedServiceEntry, ...] = (
        ManagedServiceEntry(
            name="A",
            kind="http_service",
            compose_service_name="A",
            compose_profile="A",
            env_example_path="services/A/.env.example",
            health_endpoint=None,
            test_command=None,
            depends_on_services=("B", "C"),
        ),
        ManagedServiceEntry(
            name="B",
            kind="http_service",
            compose_service_name="B",
            compose_profile="B",
            env_example_path="services/B/.env.example",
            health_endpoint=None,
            test_command=None,
            depends_on_services=("D",),
        ),
        ManagedServiceEntry(
            name="C",
            kind="http_service",
            compose_service_name="C",
            compose_profile="C",
            env_example_path="services/C/.env.example",
            health_endpoint=None,
            test_command=None,
            depends_on_services=("D",),
        ),
        ManagedServiceEntry(
            name="D",
            kind="infra",
            compose_service_name="D",
            compose_profile="D",
            env_example_path="services/D/.env.example",
            health_endpoint=None,
            test_command=None,
        ),
    )
    workspace = _build_workspace(tmp_path)
    svc, *_ = _make_service(
        workspace_root=workspace, manifest=diamond_manifest
    )

    plan = svc.compute_start_plan("A")

    # Only one occurrence of ``D``; ``D`` precedes both ``B`` and
    # ``C``; ``B`` and ``C`` precede ``A``.
    assert plan.will_start.count("D") == 1
    assert plan.will_start.index("D") < plan.will_start.index("B")
    assert plan.will_start.index("D") < plan.will_start.index("C")
    assert plan.will_start.index("B") < plan.will_start.index("A")
    assert plan.will_start.index("C") < plan.will_start.index("A")
    # Plan is exhaustive: visited every manifest node in the closure.
    assert set(plan.will_start) == {"A", "B", "C", "D"}


# ---------------------------------------------------------------------------
# start - happy path
# ---------------------------------------------------------------------------


def test_start_happy_path(tmp_path: Path) -> None:
    """End-to-end: precheck  vault writes  pending audit  compose
    health  success audit. ``state`` flips to ``starting`` before the
    response is built  and ends at ``running``."""

    workspace = _build_workspace(tmp_path)
    svc, audit, vault, compose, health = _make_service(workspace_root=workspace)

    async def run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret-1"},
            actor="ops-1",
        )

    response = asyncio.run(run())

    # Final response.
    assert isinstance(response, StartResponse)
    assert response.state == "running"
    assert response.audit_write_deferred is False

    # Order: precheck  vault writes  pending audit  compose up  health probe.
    assert audit.precheck_calls == 1
    assert len(vault.writes) == 2
    assert ("automation-service", "PORT", "8080") in vault.writes
    assert ("automation-service", "API_TOKEN", "secret-1") in vault.writes
    assert len(audit.write_calls) == 1
    pending = audit.write_calls[0]
    assert pending.action == "start"
    assert pending.outcome == "pending"
    # details_json contains keys, never values.
    assert pending.details_json == {"env_keys": ["PORT", "API_TOKEN"]}
    assert "secret-1" not in str(pending.details_json)

    assert len(compose.up_calls) == 1
    up = compose.up_calls[0]
    assert up["profile"] == "automation-service"
    assert up["service_name"] == "automation-service"
    assert up["env_overrides"] == {"PORT": "8080", "API_TOKEN": "secret-1"}

    assert len(health.calls) >= 1

    # write_with_retry is called once with outcome="success".
    assert len(audit.write_with_retry_calls) == 1
    final = audit.write_with_retry_calls[0]
    assert final.outcome == "success"
    assert final.action == "start"

    # State cache reflects success.
    slot = svc.state_cache["automation-service"]
    assert slot.state == "running"
    assert slot.last_started_at is not None
    assert slot.last_correlation_id == response.correlation_id


def test_start_state_is_starting_before_compose_returns(tmp_path: Path) -> None:
    """state[name] is ``"starting"`` before compose runs.
    We assert this by hooking compose.up to inspect the cache mid-flight."""

    workspace = _build_workspace(tmp_path)
    seen_state: list[str] = []

    class _PeekingCompose(_FakeComposeRunner):
        def __init__(self, svc_ref: list[LifecycleService]) -> None:
            super().__init__()
            self._svc_ref = svc_ref

        async def up(self, **kwargs: Any) -> ComposeResult:  # type: ignore[override]
            # Snapshot the state at the moment compose.up is invoked.
            slot = self._svc_ref[0].state_cache["automation-service"]
            seen_state.append(slot.state)
            return await super().up(**kwargs)

    svc_ref: list[LifecycleService] = []
    compose = _PeekingCompose(svc_ref)
    svc, *_ = _make_service(workspace_root=workspace, compose=compose)
    svc_ref.append(svc)

    async def run() -> None:
        await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops-1",
        )

    asyncio.run(run())
    assert seen_state == ["starting"]


# ---------------------------------------------------------------------------
# start - form schema mismatch
# ---------------------------------------------------------------------------


def test_start_missing_form_key_raises(tmp_path: Path) -> None:
    workspace = _build_workspace(tmp_path)
    svc, audit, vault, compose, _ = _make_service(workspace_root=workspace)

    async def run() -> None:
        await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080"},  # API_TOKEN missing
            actor="ops",
        )

    with pytest.raises(FormSchemaMismatchError, match="missing keys"):
        asyncio.run(run())

    # Side-effect-free on schema mismatch.
    assert audit.precheck_calls == 0
    assert vault.writes == []
    assert compose.up_calls == []


def test_start_extra_form_key_raises(tmp_path: Path) -> None:
    workspace = _build_workspace(tmp_path)
    svc, audit, vault, compose, _ = _make_service(workspace_root=workspace)

    async def run() -> None:
        await svc.start(
            name="automation-service",
            env_overrides={
                "PORT": "8080",
                "API_TOKEN": "ok",
                "EXTRA_KEY": "nope",
            },
            actor="ops",
        )

    with pytest.raises(FormSchemaMismatchError, match="extra keys"):
        asyncio.run(run())

    assert audit.precheck_calls == 0
    assert vault.writes == []
    assert compose.up_calls == []


def test_start_empty_sensitive_value_raises(tmp_path: Path) -> None:
    """an empty Sensitive_Env_Key value is rejected."""

    workspace = _build_workspace(tmp_path)
    svc, audit, vault, compose, _ = _make_service(workspace_root=workspace)

    async def run() -> None:
        await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": ""},
            actor="ops",
        )

    with pytest.raises(FormSchemaMismatchError, match="sensitive value required"):
        asyncio.run(run())

    assert vault.writes == []
    assert compose.up_calls == []


def test_start_llm_openai_does_not_require_other_provider_tokens(
    tmp_path: Path,
) -> None:
    workspace = _build_workspace(tmp_path)
    streamlit_dir = workspace / "ui" / "streamlit-app"
    streamlit_dir.mkdir(parents=True)
    (streamlit_dir / ".env").write_text(
        "\n".join(
            [
                "PORT=8501",
                "LLM_PROVIDER=openai",
                "LLM_MODEL_NAME=gpt-4o-mini",
                "OPENAI_API_KEY=",
                "OPENAI_BASE_URL=https://api.openai.com/v1",
                "VLLM_BASE_URL=http://host.docker.internal:8000/v1",
                "VLLM_API_KEY=",
                "ANTHROPIC_API_KEY=",
                "ANTHROPIC_BASE_URL=https://api.anthropic.com/v1",
                "CLIENT_SOURCE=streamlit-app",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    streamlit_entry = ManagedServiceEntry(
        name="streamlit-ui",
        kind="ui",
        compose_service_name="streamlit-ui",
        compose_profile="streamlit-ui",
        env_example_path="ui/streamlit-app/.env",
        health_endpoint="/_stcore/health",
        test_command=None,
    )
    svc, _, vault, compose, _ = _make_service(
        workspace_root=workspace,
        manifest=_entries() + (streamlit_entry,),
    )

    env_overrides = {
        "PORT": "8501",
        "LLM_PROVIDER": "openai",
        "LLM_MODEL_NAME": "gpt-4o-mini",
        "OPENAI_API_KEY": "openai-secret",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "VLLM_BASE_URL": "http://host.docker.internal:8000/v1",
        "VLLM_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_BASE_URL": "https://api.anthropic.com/v1",
        "CLIENT_SOURCE": "streamlit-app",
    }

    async def run() -> StartResponse:
        return await svc.start(
            name="streamlit-ui",
            env_overrides=env_overrides,
            actor="ops",
        )

    response = asyncio.run(run())

    assert response.state == "running"
    assert compose.up_calls[-1]["service_name"] == "streamlit-ui"
    assert vault.stored["streamlit-ui"]["OPENAI_API_KEY"] == "openai-secret"
    assert vault.stored["streamlit-ui"]["VLLM_API_KEY"] == ""
    assert vault.stored["streamlit-ui"]["ANTHROPIC_API_KEY"] == ""


def test_start_llm_vllm_requires_vllm_api_key(tmp_path: Path) -> None:
    workspace = _build_workspace(tmp_path)
    streamlit_dir = workspace / "ui" / "streamlit-app"
    streamlit_dir.mkdir(parents=True)
    (streamlit_dir / ".env").write_text(
        "\n".join(
            [
                "PORT=8501",
                "LLM_PROVIDER=vllm",
                "LLM_MODEL_NAME=qwen2.5-coder",
                "OPENAI_API_KEY=",
                "OPENAI_BASE_URL=https://api.openai.com/v1",
                "VLLM_BASE_URL=http://host.docker.internal:8000/v1",
                "VLLM_API_KEY=",
                "ANTHROPIC_API_KEY=",
                "ANTHROPIC_BASE_URL=https://api.anthropic.com/v1",
                "CLIENT_SOURCE=streamlit-app",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    streamlit_entry = ManagedServiceEntry(
        name="streamlit-ui",
        kind="ui",
        compose_service_name="streamlit-ui",
        compose_profile="streamlit-ui",
        env_example_path="ui/streamlit-app/.env",
        health_endpoint="/_stcore/health",
        test_command=None,
    )
    svc, _, vault, compose, _ = _make_service(
        workspace_root=workspace,
        manifest=_entries() + (streamlit_entry,),
    )

    async def run() -> StartResponse:
        return await svc.start(
            name="streamlit-ui",
            env_overrides={
                "PORT": "8501",
                "LLM_PROVIDER": "vllm",
                "LLM_MODEL_NAME": "qwen2.5-coder",
                "OPENAI_API_KEY": "",
                "OPENAI_BASE_URL": "https://api.openai.com/v1",
                "VLLM_BASE_URL": "http://host.docker.internal:8000/v1",
                "VLLM_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "ANTHROPIC_BASE_URL": "https://api.anthropic.com/v1",
                "CLIENT_SOURCE": "streamlit-app",
            },
            actor="ops",
        )

    with pytest.raises(FormSchemaMismatchError, match="VLLM_API_KEY"):
        asyncio.run(run())

    assert vault.writes == []
    assert compose.up_calls == []


# ---------------------------------------------------------------------------
# start - audit precheck failure
# ---------------------------------------------------------------------------


def test_start_audit_precheck_fail_short_circuits(tmp_path: Path) -> None:
    workspace = _build_workspace(tmp_path)
    audit = _FakeAuditWriter(
        precheck_raise=AuditUnreachableError("DB down"),
    )
    svc, _, vault, compose, _ = _make_service(
        workspace_root=workspace, audit=audit
    )

    async def run() -> None:
        await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    with pytest.raises(AuditUnreachableError):
        asyncio.run(run())

    # Vault is never touched and Compose is never invoked
    # .
    assert vault.writes == []
    assert compose.up_calls == []
    # State stays at the initial ``stopped``.
    assert svc.state_cache["automation-service"].state == "stopped"


# ---------------------------------------------------------------------------
# start - vault failure
# ---------------------------------------------------------------------------


def test_start_vault_write_fail_aborts_before_compose(tmp_path: Path) -> None:
    workspace = _build_workspace(tmp_path)
    vault = _FakeVaultClient(raise_on_key="API_TOKEN")
    svc, audit, _, compose, _ = _make_service(
        workspace_root=workspace, vault=vault
    )

    async def run() -> None:
        await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    with pytest.raises(VaultWriteError):
        asyncio.run(run())

    # Audit precheck still ran .
    assert audit.precheck_calls == 1
    # Pending audit row was NOT written - Vault failed first.
    assert audit.write_calls == []
    # Compose never invoked.
    assert compose.up_calls == []
    # State unchanged.
    assert svc.state_cache["automation-service"].state == "stopped"


# ---------------------------------------------------------------------------
# start - compose failure
# ---------------------------------------------------------------------------


def test_start_compose_up_fail_marks_failed_and_writes_failed_audit(
    tmp_path: Path,
) -> None:
    workspace = _build_workspace(tmp_path)
    failing_result = ComposeResult(
        exit_code=1, stdout="", stderr="image not found", argv=("docker",),
    )
    compose = _FakeComposeRunner(
        up_raise=ComposeFailureError("boom", result=failing_result),
    )
    svc, audit, _, _, _ = _make_service(
        workspace_root=workspace, compose=compose
    )

    async def run() -> None:
        await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    with pytest.raises(ComposeFailureError):
        asyncio.run(run())

    # State is ``failed``.
    assert svc.state_cache["automation-service"].state == "failed"
    # The pending audit row was written (Compose failed AFTER it).
    assert len(audit.write_calls) == 1
    assert audit.write_calls[0].outcome == "pending"
    # And a single failed write_with_retry row was emitted.
    assert len(audit.write_with_retry_calls) == 1
    assert audit.write_with_retry_calls[0].outcome == "failed"
    assert audit.write_with_retry_calls[0].action == "start"


# ---------------------------------------------------------------------------
# start - health probe timeout
# ---------------------------------------------------------------------------


def test_start_health_probe_timeout_marks_failed(tmp_path: Path) -> None:
    """Health probe never reports ``healthy``  state ends at ``failed``."""

    workspace = _build_workspace(tmp_path)
    health = _FakeHealthProbe(snapshots=[_unhealthy_snapshot()])
    svc, audit, _, compose, _ = _make_service(
        workspace_root=workspace,
        health=health,
        health_ready_timeout_seconds=0.5,
    )

    async def run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    response = asyncio.run(run())
    assert response.state == "failed"
    assert svc.state_cache["automation-service"].state == "failed"

    # Compose still ran (Compose was successful - the failure is health).
    assert len(compose.up_calls) == 1

    # Final audit row says outcome=failed.
    assert len(audit.write_with_retry_calls) == 1
    assert audit.write_with_retry_calls[0].outcome == "failed"


# ---------------------------------------------------------------------------
# stop - idempotent
# ---------------------------------------------------------------------------


def test_stop_idempotent_when_already_stopped(tmp_path: Path) -> None:
    """Repeated ``stop`` calls return ``noop=True`` and do not invoke Compose."""

    workspace = _build_workspace(tmp_path)
    svc, audit, _, compose, _ = _make_service(workspace_root=workspace)

    async def run() -> tuple[StopResponse, StopResponse]:
        first = await svc.stop(
            name="automation-service", remove_volumes=False, actor="ops"
        )
        second = await svc.stop(
            name="automation-service", remove_volumes=False, actor="ops"
        )
        return first, second

    first, second = asyncio.run(run())

    assert first.state == "stopped"
    assert first.noop is True
    assert second.state == "stopped"
    assert second.noop is True

    # Compose.stop is NEVER called when state was already ``stopped``.
    assert compose.stop_calls == []
    # Both calls produced one audit row each .
    assert len(audit.write_with_retry_calls) == 2
    assert all(e.outcome == "success" for e in audit.write_with_retry_calls)
    assert all(e.action == "stop" for e in audit.write_with_retry_calls)


def test_stop_running_service_invokes_compose(tmp_path: Path) -> None:
    """When state is ``running`` ``stop`` actually calls Compose."""

    workspace = _build_workspace(tmp_path)
    initial = {
        "automation-service": LifecycleStateCache(
            name="automation-service",
            state="running",
        ),
        "agent-runner-worker": LifecycleStateCache(name="agent-runner-worker"),
    }
    svc, _, _, compose, _ = _make_service(
        workspace_root=workspace, initial_state=initial
    )

    async def run() -> StopResponse:
        return await svc.stop(
            name="automation-service", remove_volumes=True, actor="ops"
        )

    response = asyncio.run(run())
    assert response.state == "stopped"
    assert response.noop is False
    assert len(compose.stop_calls) == 1
    assert compose.stop_calls[0]["service_name"] == "automation-service"
    assert compose.stop_calls[0]["remove_volumes"] is True


# ---------------------------------------------------------------------------
# record_purge_vault_blocked - the project
# ---------------------------------------------------------------------------


def test_record_purge_vault_blocked_writes_audit_row(tmp_path: Path) -> None:
    """    ``purge_vault_blocked_in_production`` audit row.
    The router calls this helper before returning ``403 Forbidden``
    when ``purge_vault=true`` is rejected on the production
    deployment profile. The audit row's ``service_name`` must match
    the manifest entry, ``actor`` must be the OIDC subject, and
    ``outcome="failed"`` so the row is filterable in security
    review queries."""

    workspace = _build_workspace(tmp_path)
    svc, audit, _, compose, _ = _make_service(workspace_root=workspace)

    async def run() -> None:
        await svc.record_purge_vault_blocked(
            name="automation-service",
            actor="ops-1",
        )

    asyncio.run(run())

    # Compose is NEVER touched on this path.
    assert compose.stop_calls == []
    assert compose.up_calls == []

    # Exactly one audit row, written via write_with_retry so a
    # transient DB outage does not escalate.
    assert len(audit.write_with_retry_calls) == 1
    row = audit.write_with_retry_calls[0]
    assert row.action == "purge_vault_blocked_in_production"
    assert row.outcome == "failed"
    assert row.actor == "ops-1"
    assert row.service_name == "automation-service"
    # ``details_json`` carries the actor for downstream pivots
    # without joining against an external IdP record.
    assert row.details_json["actor_id"] == "ops-1"
    assert row.details_json["service_name"] == "automation-service"
    assert row.details_json["reason"] == "deployment_profile_production"


def test_record_purge_vault_blocked_unknown_service_raises(
    tmp_path: Path,
) -> None:
    """    :class:`UnknownServiceError`.
    The router maps this to ``404 Not Found`` so a routing miss does
    not pollute the audit trail with rows whose ``service_name`` does
    not exist in the manifest."""

    workspace = _build_workspace(tmp_path)
    svc, audit, _, _, _ = _make_service(workspace_root=workspace)

    async def run() -> None:
        await svc.record_purge_vault_blocked(
            name="not-in-manifest", actor="ops-1"
        )

    with pytest.raises(UnknownServiceError):
        asyncio.run(run())

    # No audit row written - helper short-circuited on the
    # ``_require_entry`` lookup.
    assert audit.write_with_retry_calls == []


# ---------------------------------------------------------------------------
# stop + purge_vault - the project
# ---------------------------------------------------------------------------


def test_stop_purge_vault_false_skips_vault_calls(tmp_path: Path) -> None:
    """    leaves Vault untouched.
    Backwards compatibility: every existing caller that doesn't pass
    the flag must observe identical behaviour to the pre-task-15.2
    code path. No LIST, no DELETE, and no ``vault_overrides_purged``
    audit row."""

    workspace = _build_workspace(tmp_path)
    initial = {
        "automation-service": LifecycleStateCache(
            name="automation-service",
            state="running",
        ),
        "agent-runner-worker": LifecycleStateCache(name="agent-runner-worker"),
    }
    vault = _FakeVaultClient(
        stored={"automation-service": {"PORT": "8080", "API_TOKEN": "tok"}}
    )
    svc, audit, _, compose, _ = _make_service(
        workspace_root=workspace, vault=vault, initial_state=initial
    )

    async def run() -> StopResponse:
        return await svc.stop(
            name="automation-service",
            remove_volumes=False,
            purge_vault=False,
            actor="ops",
        )

    response = asyncio.run(run())

    assert response.state == "stopped"
    assert response.noop is False

    # Compose stop ran exactly once.
    assert len(compose.stop_calls) == 1

    # Vault must remain untouched on the default path.
    assert vault.list_calls == []
    assert vault.delete_calls == []
    assert vault.stored["automation-service"] == {
        "PORT": "8080",
        "API_TOKEN": "tok",
    }

    # Single ``stop`` audit row, no purge audit rows.
    assert len(audit.write_with_retry_calls) == 1
    assert audit.write_with_retry_calls[0].action == "stop"


def test_stop_purge_vault_true_happy_path_deletes_all_keys(
    tmp_path: Path,
) -> None:
    """    every key under ``services/{name}/``.
    Asserts:
    * Compose stop runs *before* any Vault interaction (purge happens
      after the canonical stop, never replaces it).
    * Vault LIST runs exactly once (single round-trip enumeration).
    * Vault DELETE runs once per key, and ``stored`` is empty
      afterwards.
    * Audit chain is ``[stop(success), vault_overrides_purged(success)]``
      with ``deleted_paths_count`` matching the original key count.
    * The HTTP-shape :class:`StopResponse` reports the canonical
      success state - the purge is a *side* effect, not a status
      modifier."""

    workspace = _build_workspace(tmp_path)
    initial = {
        "automation-service": LifecycleStateCache(
            name="automation-service",
            state="running",
        ),
        "agent-runner-worker": LifecycleStateCache(name="agent-runner-worker"),
    }
    vault = _FakeVaultClient(
        stored={
            "automation-service": {
                "PORT": "8080",
                "API_TOKEN": "tok",
                "DB_URL": "postgres://...",
            }
        }
    )
    svc, audit, _, compose, _ = _make_service(
        workspace_root=workspace, vault=vault, initial_state=initial
    )

    async def run() -> StopResponse:
        return await svc.stop(
            name="automation-service",
            remove_volumes=False,
            purge_vault=True,
            actor="ops",
        )

    response = asyncio.run(run())

    assert response.state == "stopped"
    assert response.noop is False

    # Compose ran first (one and only stop call).
    assert len(compose.stop_calls) == 1
    assert compose.stop_calls[0]["service_name"] == "automation-service"

    # LIST ran exactly once (single Vault round-trip).
    assert vault.list_calls == ["automation-service"]
    # DELETE ran once per key - order matches the dict iteration
    # order Python guarantees for insertion-ordered dicts.
    assert len(vault.delete_calls) == 3
    assert {key for _, key in vault.delete_calls} == {
        "PORT",
        "API_TOKEN",
        "DB_URL",
    }
    # Vault is now empty for this service.
    assert vault.stored["automation-service"] == {}

    # Audit chain: stop  vault_overrides_purged. Both successes.
    actions = [row.action for row in audit.write_with_retry_calls]
    assert actions == ["stop", "vault_overrides_purged"]
    purge_row = audit.write_with_retry_calls[1]
    assert purge_row.outcome == "success"
    assert purge_row.actor == "ops"
    assert purge_row.service_name == "automation-service"
    assert purge_row.details_json == {
        "service_name": "automation-service",
        "deleted_paths_count": 3,
    }
    # The purge row shares the same correlation_id as the stop row
    # so a single ``stop`` operation is queryable as one unit.
    assert (
        purge_row.correlation_id
        == audit.write_with_retry_calls[0].correlation_id
    )


def test_stop_purge_vault_true_zero_keys_still_emits_success_audit(
    tmp_path: Path,
) -> None:
    """    A service that was never started with overrides has zero keys to
    delete. The orchestrator must still emit
    ``vault_overrides_purged`` with ``deleted_paths_count=0`` so the
    security review sees "purge ran, found nothing" rather than
    absence-of-evidence."""

    workspace = _build_workspace(tmp_path)
    initial = {
        "automation-service": LifecycleStateCache(
            name="automation-service",
            state="running",
        ),
        "agent-runner-worker": LifecycleStateCache(name="agent-runner-worker"),
    }
    vault = _FakeVaultClient(stored={"automation-service": {}})
    svc, audit, _, _, _ = _make_service(
        workspace_root=workspace, vault=vault, initial_state=initial
    )

    async def run() -> StopResponse:
        return await svc.stop(
            name="automation-service",
            remove_volumes=False,
            purge_vault=True,
            actor="ops",
        )

    response = asyncio.run(run())
    assert response.state == "stopped"

    # LIST ran; nothing to DELETE.
    assert vault.list_calls == ["automation-service"]
    assert vault.delete_calls == []

    actions = [row.action for row in audit.write_with_retry_calls]
    assert actions == ["stop", "vault_overrides_purged"]
    assert audit.write_with_retry_calls[1].details_json == {
        "service_name": "automation-service",
        "deleted_paths_count": 0,
    }


def test_stop_purge_vault_true_list_failure_partial_audit(
    tmp_path: Path,
) -> None:
    """    Compose stop survives.
    The Compose stop has *already* committed by the time we reach
    the Vault purge step. A LIST failure must NOT roll the stop
    back; the orchestrator records the failure (``error_type``,
    ``partial_count=0``) and returns the canonical
    :class:`StopResponse`."""

    workspace = _build_workspace(tmp_path)
    initial = {
        "automation-service": LifecycleStateCache(
            name="automation-service",
            state="running",
        ),
        "agent-runner-worker": LifecycleStateCache(name="agent-runner-worker"),
    }
    vault = _FakeVaultClient(
        stored={"automation-service": {"PORT": "8080"}},
        list_raise=VaultWriteError(
            operation="list",
            service_name="automation-service",
            key=None,
            status_code=500,
            message="injected list failure",
        ),
    )
    svc, audit, _, compose, _ = _make_service(
        workspace_root=workspace, vault=vault, initial_state=initial
    )

    async def run() -> StopResponse:
        return await svc.stop(
            name="automation-service",
            remove_volumes=False,
            purge_vault=True,
            actor="ops",
        )

    # The exception MUST NOT propagate - best-effort purge.
    response = asyncio.run(run())
    assert response.state == "stopped"
    assert response.noop is False

    # Compose stop still fired exactly once.
    assert len(compose.stop_calls) == 1

    # LIST was attempted; nothing was DELETE'd because LIST failed.
    assert vault.list_calls == ["automation-service"]
    assert vault.delete_calls == []
    # Stored override survives - we couldn't enumerate it.
    assert vault.stored["automation-service"] == {"PORT": "8080"}

    # Audit chain: stop(success)  vault_purge_partial_failure(failed).
    actions = [row.action for row in audit.write_with_retry_calls]
    assert actions == ["stop", "vault_purge_partial_failure"]
    failure_row = audit.write_with_retry_calls[1]
    assert failure_row.outcome == "failed"
    assert failure_row.actor == "ops"
    assert failure_row.service_name == "automation-service"
    assert failure_row.details_json == {
        "service_name": "automation-service",
        "error_type": "VaultWriteError",
        "partial_count": 0,
    }


def test_stop_purge_vault_true_delete_failure_records_partial_count(
    tmp_path: Path,
) -> None:
    """    number of keys that DID succeed in ``partial_count``.
    Three keys, fail on the third delete: ``partial_count`` must be
    ``2`` so the operator's manual cleanup runbook can pick up where
    the automatic purge stopped."""

    workspace = _build_workspace(tmp_path)
    initial = {
        "automation-service": LifecycleStateCache(
            name="automation-service",
            state="running",
        ),
        "agent-runner-worker": LifecycleStateCache(name="agent-runner-worker"),
    }
    vault = _FakeVaultClient(
        stored={
            "automation-service": {
                "PORT": "8080",
                "API_TOKEN": "tok",
                "DB_URL": "postgres://...",
            }
        },
        # First two DELETEs succeed; the third raises.
        raise_on_delete_after=2,
    )
    svc, audit, _, compose, _ = _make_service(
        workspace_root=workspace, vault=vault, initial_state=initial
    )

    async def run() -> StopResponse:
        return await svc.stop(
            name="automation-service",
            remove_volumes=False,
            purge_vault=True,
            actor="ops",
        )

    # Best-effort: the failure does NOT propagate.
    response = asyncio.run(run())
    assert response.state == "stopped"

    # Compose stop fired once.
    assert len(compose.stop_calls) == 1
    assert vault.list_calls == ["automation-service"]
    # All three DELETEs were attempted; the third failed.
    assert len(vault.delete_calls) == 3
    # Two keys actually deleted from storage; the third remains.
    assert len(vault.stored["automation-service"]) == 1

    actions = [row.action for row in audit.write_with_retry_calls]
    assert actions == ["stop", "vault_purge_partial_failure"]
    failure_row = audit.write_with_retry_calls[1]
    assert failure_row.outcome == "failed"
    assert failure_row.details_json == {
        "service_name": "automation-service",
        "error_type": "VaultWriteError",
        # Two keys were successfully deleted before the failure.
        "partial_count": 2,
    }


def test_stop_purge_vault_true_skipped_on_idempotent_path(
    tmp_path: Path,
) -> None:
    """    Vault purge.
    The idempotent ``stop`` path is intentionally a no-op: it produces a
    success audit row but does NOT invoke Compose. Calling Vault
    LIST/DELETE here would be a side-effect that a previous
    successful stop has already covered, so the purge step is
    skipped entirely. The audit trail remains a single ``stop`` row."""

    workspace = _build_workspace(tmp_path)
    # Default initial state has ``automation-service`` already
    # ``stopped`` so the very first stop call hits the idempotent
    # branch.
    vault = _FakeVaultClient(
        stored={"automation-service": {"PORT": "8080"}}
    )
    svc, audit, _, compose, _ = _make_service(
        workspace_root=workspace, vault=vault
    )

    async def run() -> StopResponse:
        return await svc.stop(
            name="automation-service",
            remove_volumes=False,
            purge_vault=True,
            actor="ops",
        )

    response = asyncio.run(run())
    assert response.noop is True

    # Compose was NOT invoked (idempotent path).
    assert compose.stop_calls == []
    # Vault was also NOT invoked - no purge on the idempotent path.
    assert vault.list_calls == []
    assert vault.delete_calls == []
    assert vault.stored["automation-service"] == {"PORT": "8080"}

    # Single ``stop`` audit row, marked as a no-op.
    assert len(audit.write_with_retry_calls) == 1
    assert audit.write_with_retry_calls[0].action == "stop"
    assert audit.write_with_retry_calls[0].details_json == {"noop": True}


def test_stop_purge_vault_true_compose_failure_skips_vault_purge(
    tmp_path: Path,
) -> None:
    """    interaction.
    The purge step is gated on a successful Compose stop; if the
    stop itself fails the orchestrator must not touch Vault. The
    failed-stop audit row is the only row the operator sees."""

    workspace = _build_workspace(tmp_path)
    initial = {
        "automation-service": LifecycleStateCache(
            name="automation-service",
            state="running",
        ),
        "agent-runner-worker": LifecycleStateCache(name="agent-runner-worker"),
    }
    vault = _FakeVaultClient(
        stored={"automation-service": {"PORT": "8080"}}
    )
    compose = _FakeComposeRunner()
    compose.stop_raise = ComposeFailureError(
        "injected stop failure",
        result=ComposeResult(
            exit_code=1,
            stdout="",
            stderr="injected stop failure",
            argv=("docker", "compose", "stop"),
        ),
    )
    svc, audit, _, _, _ = _make_service(
        workspace_root=workspace,
        vault=vault,
        compose=compose,
        initial_state=initial,
    )

    async def run() -> StopResponse:
        return await svc.stop(
            name="automation-service",
            remove_volumes=False,
            purge_vault=True,
            actor="ops",
        )

    with pytest.raises(ComposeFailureError):
        asyncio.run(run())

    # Vault was never touched - Compose failure short-circuited.
    assert vault.list_calls == []
    assert vault.delete_calls == []
    assert vault.stored["automation-service"] == {"PORT": "8080"}

    # Audit chain: a single failed-stop row, no purge rows.
    actions = [row.action for row in audit.write_with_retry_calls]
    assert actions == ["stop"]
    assert audit.write_with_retry_calls[0].outcome == "failed"


# ---------------------------------------------------------------------------
# run_tests - 409 semantics
# ---------------------------------------------------------------------------


def test_run_tests_when_stopped_raises_409(tmp_path: Path) -> None:
    workspace = _build_workspace(tmp_path)
    svc, audit, _, compose, _ = _make_service(workspace_root=workspace)

    async def run() -> None:
        await svc.run_tests(
            name="automation-service", stream=False, actor="ops"
        )

    with pytest.raises(TestPreconditionError, match="must be running"):
        asyncio.run(run())

    # No compose.exec_test invocation, no audit row.
    assert compose.exec_test_calls == []
    assert audit.write_with_retry_calls == []


def test_run_tests_with_no_test_command_raises_409(tmp_path: Path) -> None:
    workspace = _build_workspace(tmp_path)
    initial = {
        "automation-service": LifecycleStateCache(name="automation-service"),
        "agent-runner-worker": LifecycleStateCache(
            name="agent-runner-worker", state="running"
        ),
    }
    svc, _, _, compose, _ = _make_service(
        workspace_root=workspace, initial_state=initial
    )

    async def run() -> None:
        await svc.run_tests(
            name="agent-runner-worker", stream=False, actor="ops"
        )

    with pytest.raises(TestPreconditionError, match="no test_command"):
        asyncio.run(run())

    assert compose.exec_test_calls == []


def test_run_tests_running_service_returns_summary(tmp_path: Path) -> None:
    """Happy path: ``running`` service, manifest test_command set, summary parsed."""

    workspace = _build_workspace(tmp_path)
    initial = {
        "automation-service": LifecycleStateCache(
            name="automation-service", state="running"
        ),
        "agent-runner-worker": LifecycleStateCache(name="agent-runner-worker"),
    }
    compose = _FakeComposeRunner(
        test_stdout="\n".join(
            [
                "collected 5 items",
                "tests/integration/test_a.py::test_one PASSED",
                "============ 5 passed, 0 failed in 1.23s ============",
            ]
        )
    )
    svc, audit, _, _, _ = _make_service(
        workspace_root=workspace,
        compose=compose,
        initial_state=initial,
    )

    async def run() -> RunTestsResponse:
        return await svc.run_tests(
            name="automation-service", stream=False, actor="ops"
        )

    response = asyncio.run(run())

    assert isinstance(response, RunTestsResponse)
    assert response.exit_code == 0
    assert isinstance(response.summary, TestSummary)
    assert response.summary.passed == 5
    assert response.summary.failed == 0
    assert response.summary.duration_seconds == pytest.approx(1.23)

    # Audit row written (1:1 with the test action).
    assert len(audit.write_with_retry_calls) == 1
    assert audit.write_with_retry_calls[0].action == "run_tests"
    assert audit.write_with_retry_calls[0].outcome == "success"

    # Compose argv strips the ``docker compose ... exec <svc>`` prefix
    # so ComposeRunner.exec_test does not duplicate it.
    assert len(compose.exec_test_calls) == 1
    argv = compose.exec_test_calls[0]["argv"]
    assert argv == ("pytest", "tests/integration/", "-v")


# ---------------------------------------------------------------------------
# logs - redaction
# ---------------------------------------------------------------------------


def test_logs_redacts_sensitive_env_values(tmp_path: Path) -> None:
    """``KEY=value`` and ``KEY: value`` tokens are masked when KEY is sensitive."""

    workspace = _build_workspace(tmp_path)
    sensitive_value = "super-secret-value-12345"
    public_value = "8080"
    log_text = "\n".join(
        [
            f"booting service with API_TOKEN={sensitive_value} PORT={public_value}",
            f"config: API_TOKEN: {sensitive_value}",
            "ready to accept connections",
        ]
    )
    compose = _FakeComposeRunner(logs_stdout=log_text)
    svc, *_ = _make_service(workspace_root=workspace, compose=compose)

    async def run() -> list[str]:
        return await svc.logs(
            name="automation-service", tail=200, follow=False
        )

    lines = asyncio.run(run())

    joined = "\n".join(lines)
    # The secret value is gone in every form.
    assert sensitive_value not in joined
    # The sensitive key is still readable so operators know what was masked.
    assert "API_TOKEN=<redacted>" in joined
    assert "API_TOKEN: <redacted>" in joined
    # Non-sensitive values are unchanged.
    assert f"PORT={public_value}" in joined
    # Plain log content survives.
    assert "ready to accept connections" in joined


def test_logs_no_redaction_when_no_sensitive_keys(tmp_path: Path) -> None:
    """Worker schema has no sensitive keys  log lines pass through unmodified."""

    workspace = _build_workspace(tmp_path)
    initial = {
        "automation-service": LifecycleStateCache(name="automation-service"),
        "agent-runner-worker": LifecycleStateCache(name="agent-runner-worker"),
    }
    compose = _FakeComposeRunner(logs_stdout="WORKER_NAME=alpha is alive")
    svc, *_ = _make_service(
        workspace_root=workspace,
        compose=compose,
        initial_state=initial,
    )

    async def run() -> list[str]:
        return await svc.logs(
            name="agent-runner-worker", tail=200, follow=False
        )

    lines = asyncio.run(run())
    assert lines == ["WORKER_NAME=alpha is alive"]


# ---------------------------------------------------------------------------
# health_of - streak alert
# ---------------------------------------------------------------------------


def test_health_of_streak_alert_emitted_once_at_threshold(tmp_path: Path) -> None:
    """Three consecutive ``unhealthy`` snapshots  one streak alert.

    Subsequent ``unhealthy`` snapshots must NOT emit another alert
    (the ``streak_alert_emitted`` flag suppresses duplicates).
    """

    workspace = _build_workspace(tmp_path)
    health = _FakeHealthProbe(
        snapshots=[
            _unhealthy_snapshot(),
            _unhealthy_snapshot(),
            _unhealthy_snapshot(),
            _unhealthy_snapshot(),  # would be 4th unhealthy poll
        ]
    )
    initial = {
        "automation-service": LifecycleStateCache(
            name="automation-service", state="running"
        ),
        "agent-runner-worker": LifecycleStateCache(name="agent-runner-worker"),
    }
    svc, audit, _, _, _ = _make_service(
        workspace_root=workspace,
        health=health,
        health_fail_streak_threshold=3,
        initial_state=initial,
        clock=_AdvancingClock(step_seconds=60.0),
    )

    async def run() -> None:
        # Probe twice short of the threshold.
        await svc.health_of(name="automation-service")
        await svc.health_of(name="automation-service")
        # Hitting the threshold here.
        await svc.health_of(name="automation-service")
        # Past the threshold - must NOT emit a second alert.
        await svc.health_of(name="automation-service")

    asyncio.run(run())

    # Exactly one ``health_streak_alert`` audit entry was written.
    streak_rows = [
        e
        for e in audit.write_with_retry_calls
        if e.action == "health_streak_alert"
    ]
    assert len(streak_rows) == 1
    row = streak_rows[0]
    assert row.outcome == "success"
    assert row.details_json == {
        "reason": "consecutive_unhealthy_polls",
        "streak": 3,
    }

    # State machine: running  unhealthy after first probe.
    slot = svc.state_cache["automation-service"]
    assert slot.state == "unhealthy"
    assert slot.consecutive_unhealthy_polls >= 3
    assert slot.streak_alert_emitted is True


def test_health_of_recovers_resets_streak(tmp_path: Path) -> None:
    """A ``healthy`` snapshot resets the streak counter and the alert flag."""

    workspace = _build_workspace(tmp_path)
    health = _FakeHealthProbe(
        snapshots=[
            _unhealthy_snapshot(),
            _unhealthy_snapshot(),
            _healthy_snapshot(),
        ]
    )
    initial = {
        "automation-service": LifecycleStateCache(
            name="automation-service", state="running"
        ),
        "agent-runner-worker": LifecycleStateCache(name="agent-runner-worker"),
    }
    svc, audit, _, _, _ = _make_service(
        workspace_root=workspace,
        health=health,
        health_fail_streak_threshold=3,
        initial_state=initial,
        clock=_AdvancingClock(step_seconds=60.0),
    )

    async def run() -> None:
        await svc.health_of(name="automation-service")
        await svc.health_of(name="automation-service")
        await svc.health_of(name="automation-service")

    asyncio.run(run())

    slot = svc.state_cache["automation-service"]
    assert slot.state == "running"
    assert slot.consecutive_unhealthy_polls == 0
    assert slot.streak_alert_emitted is False
    # No alert was emitted (recovery before threshold).
    streak_rows = [
        e
        for e in audit.write_with_retry_calls
        if e.action == "health_streak_alert"
    ]
    assert streak_rows == []


# ---------------------------------------------------------------------------
# list_summaries - cache TTL returns stale snapshots as None
# ---------------------------------------------------------------------------


def test_list_summaries_returns_summary_per_manifest_entry(tmp_path: Path) -> None:
    workspace = _build_workspace(tmp_path)
    svc, *_ = _make_service(workspace_root=workspace)

    async def run() -> list[ServiceSummary]:
        return await svc.list_summaries()

    summaries = asyncio.run(run())
    assert len(summaries) == 2
    names = {s.name for s in summaries}
    assert names == {"automation-service", "agent-runner-worker"}
    # All start in ``stopped`` with no health snapshot.
    for s in summaries:
        assert s.state == "stopped"
        assert s.last_health_snapshot is None
        assert s.last_started_at is None


# ---------------------------------------------------------------------------
# start - feature-flag gate
# ---------------------------------------------------------------------------


def test_start_no_op_when_no_feature_flag_dependency(tmp_path: Path) -> None:
    """baseline - empty ``feature_flag_dependency`` skips the gate.
    No reader is wired, no SELECT is issued, and the start proceeds
    unchanged. The default ``_entries`` manifest leaves the field
    empty, so this is the "fall through" branch."""

    workspace = _build_workspace(tmp_path)
    reader = _FakeFeatureFlagReader()
    svc, *_ = _make_service(
        workspace_root=workspace, feature_flag_reader=reader
    )

    async def run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    response = asyncio.run(run())
    assert response.state == "running"
    # Reader must NOT be consulted for empty dependency tuples.
    assert reader.calls == []


def test_start_proceeds_when_all_flags_enabled(tmp_path: Path) -> None:
    """- every required flag enabled  start runs to ``running``."""

    workspace = _build_workspace(tmp_path)
    reader = _FakeFeatureFlagReader(
        flags={"FEATURE_FLAG_TASK_INTAKE_ENABLED": True}
    )
    svc, audit, _, compose, _ = _make_service(
        workspace_root=workspace,
        feature_flag_reader=reader,
        manifest=_entries_with_flag_gate(),
    )

    async def run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    response = asyncio.run(run())
    assert response.state == "running"

    # Exactly one SELECT against shared.feature_flags .
    assert reader.calls == [["FEATURE_FLAG_TASK_INTAKE_ENABLED"]]
    # No block audit row - only the canonical pending + success rows.
    assert len(compose.up_calls) == 1
    block_actions = [
        e.action
        for e in audit.write_with_retry_calls
        if e.action == "service_start_blocked_feature_flag"
    ]
    assert block_actions == []


def test_start_blocked_when_single_flag_disabled(tmp_path: Path) -> None:
    """- disabled flag  409 path, ``blocking_flag`` is the name."""

    workspace = _build_workspace(tmp_path)
    reader = _FakeFeatureFlagReader(
        flags={"FEATURE_FLAG_TASK_INTAKE_ENABLED": False}
    )
    svc, audit, vault, compose, _ = _make_service(
        workspace_root=workspace,
        feature_flag_reader=reader,
        manifest=_entries_with_flag_gate(),
    )

    async def run() -> None:
        await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    with pytest.raises(FeatureFlagDisabledError) as excinfo:
        asyncio.run(run())
    assert excinfo.value.blocking_flag == "FEATURE_FLAG_TASK_INTAKE_ENABLED"

    # The feature-flag gate fires before form validation, audit
    # precheck, Vault writes, and Compose startup, so none of those
    # collaborators are touched.
    assert audit.precheck_calls == 0
    assert audit.write_calls == []
    assert vault.writes == []
    assert compose.up_calls == []
    assert svc.state_cache["automation-service"].state == "stopped"

    # The block audit row was emitted via write_with_retry.
    assert len(audit.write_with_retry_calls) == 1
    block = audit.write_with_retry_calls[0]
    assert block.action == "service_start_blocked_feature_flag"
    assert block.outcome == "failed"
    assert block.service_name == "automation-service"
    assert block.details_json["blocking_flag"] == "FEATURE_FLAG_TASK_INTAKE_ENABLED"
    assert block.details_json["flag_state"] == "disabled"


def test_start_blocked_when_flag_row_missing(tmp_path: Path) -> None:
    """- flag absent from ``shared.feature_flags``  treated as disabled.
    Catches manifest typos (``FEATURE_FLAG_TASK_INTAK``) before they
    can corrupt audit history."""

    workspace = _build_workspace(tmp_path)
    reader = _FakeFeatureFlagReader(flags={})  # zero rows
    svc, audit, _, compose, _ = _make_service(
        workspace_root=workspace,
        feature_flag_reader=reader,
        manifest=_entries_with_flag_gate(
            flag_names=("FEATURE_FLAG_TASK_INTAKE_ENABLED",)
        ),
    )

    async def run() -> None:
        await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    with pytest.raises(FeatureFlagDisabledError) as excinfo:
        asyncio.run(run())
    assert excinfo.value.blocking_flag == "FEATURE_FLAG_TASK_INTAKE_ENABLED"

    assert compose.up_calls == []
    block = audit.write_with_retry_calls[0]
    assert block.details_json["flag_state"] == "missing"


def test_start_blocking_flag_is_first_disabled_in_manifest_order(
    tmp_path: Path,
) -> None:
    """determinism - multiple disabled flags  manifest order wins.
    The lifecycle handler iterates ``feature_flag_dependency`` in
    insertion order so the first disabled
    flag is the deterministic ``blocking_flag``."""

    workspace = _build_workspace(tmp_path)
    reader = _FakeFeatureFlagReader(
        flags={
            "FEATURE_FLAG_TASK_INTAKE_ENABLED": False,
            "FEATURE_FLAG_FORGE_ADDON_ENABLED": False,
        }
    )
    svc, *_ = _make_service(
        workspace_root=workspace,
        feature_flag_reader=reader,
        manifest=_entries_with_flag_gate(
            flag_names=(
                "FEATURE_FLAG_TASK_INTAKE_ENABLED",
                "FEATURE_FLAG_FORGE_ADDON_ENABLED",
            )
        ),
    )

    async def run() -> None:
        await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    with pytest.raises(FeatureFlagDisabledError) as excinfo:
        asyncio.run(run())
    # Deterministic: first manifest entry wins.
    assert excinfo.value.blocking_flag == "FEATURE_FLAG_TASK_INTAKE_ENABLED"


def test_start_proceeds_when_no_reader_wired(tmp_path: Path) -> None:
    """Boot-time / unit-test branch - no reader  gate is inert.

    Mirrors the production wiring window where ``app.state.pg_pool``
    is not yet ready (Postgres still booting). Rather than refusing
    every start, the gate degrades to a no-op and the canonical
    The downstream audit precheck reports the DB outage.
    """

    workspace = _build_workspace(tmp_path)
    # Manifest demands a flag, but no reader is wired.
    svc, audit, _, compose, _ = _make_service(
        workspace_root=workspace,
        feature_flag_reader=None,
        manifest=_entries_with_flag_gate(),
    )

    async def run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "secret"},
            actor="ops",
        )

    response = asyncio.run(run())
    assert response.state == "running"
    # No block audit row, no FeatureFlagDisabledError.
    block_actions = [
        e.action
        for e in audit.write_with_retry_calls
        if e.action == "service_start_blocked_feature_flag"
    ]
    assert block_actions == []
    assert len(compose.up_calls) == 1


# ---------------------------------------------------------------------------
# Connectivity probe helper
# ---------------------------------------------------------------------------
#
# The lifecycle service's connectivity probe helper runs the manifest
# ``connectivity_probe_command`` after ``_wait_for_healthy`` succeeds
# and maps the probe outcome to the ``LifecycleStateCache.credentials_*``
# fields. The property test suite
# (``tests/property/test_connectivity_probe.py``) exercises the helper
# across Hypothesis-generated subprocess outcomes; the focused unit
# tests below pin down the four canonical mappings with explicit
# assertions so the orchestrator wiring is regression-tested by name.
#
# Behaviour table
# ---------------
# probe_command=None         no subprocess call; credentials_status=None.
# exit_code == 0             credentials_status="ok"; passed audit.
# exit_code != 0             credentials_status="failed"; failed audit;
# credentials_probe_detail = stderr[-500:].
# subprocess.TimeoutExpired  credentials_status="failed"; failed audit;
# exit_code=-1 sentinel in audit payload.
#
# In every case the lifecycle ``state`` MUST remain ``"running"`` -
# probe failure does not alter the start outcome. The helper is wired
# into ``_do_start`` between ``_wait_for_healthy`` and the final audit
# plus response.


def _entries_with_probe(
    *, probe_command: str | None
) -> tuple[ManagedServiceEntry, ...]:
    """Manifest fixture for connectivity probe helper tests.

    Single ``automation-service`` entry tagged with the supplied
    ``connectivity_probe_command`` so the focused tests can pin down
    each branch of ``_run_connectivity_probe`` without any other
    manifest noise.
    """

    return (
        ManagedServiceEntry(
            name="automation-service",
            kind="http_service",
            compose_service_name="automation-service",
            compose_profile="automation-service",
            env_example_path="services/automation-service/.env.example",
            health_endpoint="/healthz",
            test_command=None,
            connectivity_probe_command=probe_command,
        ),
    )


def test_step_9_5_skipped_when_probe_command_is_none(tmp_path: Path) -> None:
    """No probe command means no subprocess call and no status update.
    ``connectivity_probe_command``, the helper short-circuits and
    leaves ``credentials_status=None`` (distinct from ``"unknown"``,
    which means "probe configured but never run"). No audit event is
    emitted and no subprocess is spawned."""

    import subprocess
    from unittest.mock import patch

    workspace = _build_workspace(tmp_path)
    svc, audit, _, _, _ = _make_service(
        workspace_root=workspace,
        manifest=_entries_with_probe(probe_command=None),
    )

    async def run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    with patch.object(subprocess, "run") as mock_run:
        response = asyncio.run(run())

    # subprocess.run must NOT have been invoked.
    mock_run.assert_not_called()

    assert response.state == "running"
    slot = svc.state_cache["automation-service"]
    assert slot.credentials_status is None
    assert slot.credentials_probe_at is None
    assert slot.credentials_probe_detail is None
    probe_audits = [
        e for e in audit.write_with_retry_calls
        if "connectivity_probe" in e.action
    ]
    assert probe_audits == []


def test_step_9_5_exit_code_zero_marks_credentials_ok(tmp_path: Path) -> None:
    """A zero exit code marks credentials ok and emits a passed audit.
    ``credentials_status="ok"``, records the probe timestamp, and
    emits exactly one ``service_connectivity_probe_passed`` audit row
    with ``outcome="success"`` and ``exit_code=0``. ``credentials_probe_detail``
    must be ``None`` (no failure stderr to retain)."""

    import subprocess
    from unittest.mock import MagicMock, patch

    workspace = _build_workspace(tmp_path)
    svc, audit, _, _, _ = _make_service(
        workspace_root=workspace,
        manifest=_entries_with_probe(
            probe_command="python -m src.scripts.probe_atlassian"
        ),
    )

    proc = MagicMock()
    proc.returncode = 0
    proc.stderr = ""
    proc.stdout = ""

    async def run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    with patch.object(subprocess, "run", return_value=proc):
        response = asyncio.run(run())

    assert response.state == "running"
    slot = svc.state_cache["automation-service"]
    assert slot.credentials_status == "ok"
    assert slot.credentials_probe_at is not None
    assert slot.credentials_probe_detail is None

    passed = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_connectivity_probe_passed"
    ]
    assert len(passed) == 1
    entry = passed[0]
    assert entry.outcome == "success"
    assert entry.service_name == "automation-service"
    assert entry.details_json["exit_code"] == 0
    assert entry.details_json["command"] == "python -m src.scripts.probe_atlassian"

    # No failed audit rows on the success path.
    failed = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_connectivity_probe_failed"
    ]
    assert failed == []


def test_step_9_5_nonzero_exit_marks_credentials_failed_and_truncates_stderr(
    tmp_path: Path,
) -> None:
    """A non-zero exit marks credentials failed and emits a failed audit.
    ``credentials_status="failed"``, stores the last 500 characters of
    stderr in ``credentials_probe_detail`` (truncation), and emits one
    ``service_connectivity_probe_failed`` audit row with
    ``outcome="failed"``. The lifecycle ``state`` MUST remain
    ``"running"`` - a probe failure is informational only and does
    not flip the start outcome."""

    import subprocess
    from unittest.mock import MagicMock, patch

    workspace = _build_workspace(tmp_path)
    svc, audit, _, _, _ = _make_service(
        workspace_root=workspace,
        manifest=_entries_with_probe(probe_command="python -m probe.fail"),
    )

    long_stderr = "X" * 600 + "_TAIL_MARKER"
    proc = MagicMock()
    proc.returncode = 7
    proc.stderr = long_stderr
    proc.stdout = ""

    async def run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    with patch.object(subprocess, "run", return_value=proc):
        response = asyncio.run(run())

    # Probe failure does NOT change the lifecycle state.
    assert response.state == "running"

    slot = svc.state_cache["automation-service"]
    assert slot.credentials_status == "failed"
    assert slot.credentials_probe_at is not None
    # stderr truncation: detail is exactly the last 500 chars.
    assert slot.credentials_probe_detail is not None
    assert slot.credentials_probe_detail == long_stderr[-500:]
    assert len(slot.credentials_probe_detail) == 500
    assert slot.credentials_probe_detail.endswith("_TAIL_MARKER")

    failed = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_connectivity_probe_failed"
    ]
    assert len(failed) == 1
    entry = failed[0]
    assert entry.outcome == "failed"
    assert entry.service_name == "automation-service"
    assert entry.details_json["exit_code"] == 7
    assert entry.details_json["command"] == "python -m probe.fail"
    assert entry.details_json["stderr_summary"] == long_stderr[-500:]

    passed = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_connectivity_probe_passed"
    ]
    assert passed == []


def test_step_9_5_subprocess_timeout_marks_credentials_failed(
    tmp_path: Path,
) -> None:
    """A subprocess timeout marks credentials failed.
    ``subprocess.run`` raises :class:`subprocess.TimeoutExpired`, the
    helper maps the timeout to ``credentials_status="failed"``,
    records ``"TimeoutExpired"`` in ``credentials_probe_detail``, and
    emits one ``service_connectivity_probe_failed`` audit row with
    the sentinel ``exit_code=-1``. The lifecycle ``state`` remains
    ``"running"``."""

    import subprocess
    from unittest.mock import patch

    workspace = _build_workspace(tmp_path)
    svc, audit, _, _, _ = _make_service(
        workspace_root=workspace,
        manifest=_entries_with_probe(probe_command="python -m probe.slow"),
    )

    timeout_exc = subprocess.TimeoutExpired(
        cmd="python -m probe.slow", timeout=30
    )

    async def run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    with patch.object(subprocess, "run", side_effect=timeout_exc):
        response = asyncio.run(run())

    assert response.state == "running"
    slot = svc.state_cache["automation-service"]
    assert slot.credentials_status == "failed"
    assert slot.credentials_probe_at is not None
    assert slot.credentials_probe_detail is not None
    assert "TimeoutExpired" in slot.credentials_probe_detail

    failed = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_connectivity_probe_failed"
    ]
    assert len(failed) == 1
    entry = failed[0]
    assert entry.outcome == "failed"
    # -1 sentinel marks "subprocess never completed".
    assert entry.details_json["exit_code"] == -1
    assert entry.details_json["command"] == "python -m probe.slow"

    passed = [
        e for e in audit.write_with_retry_calls
        if e.action == "service_connectivity_probe_passed"
    ]
    assert passed == []


def test_step_9_5_runs_after_wait_for_healthy_before_final_audit(
    tmp_path: Path,
) -> None:
    """The probe runs *after* health check and *before* final audit.

    The ``_do_start`` wiring must place the probe call between
    ``_wait_for_healthy`` and the final ``start`` audit row.
    This test asserts the ordering through the
    audit-write log: the connectivity probe audit must appear *before*
    the canonical ``start`` audit row in ``write_with_retry_calls``.
    """

    import subprocess
    from unittest.mock import MagicMock, patch

    workspace = _build_workspace(tmp_path)
    svc, audit, _, _, _ = _make_service(
        workspace_root=workspace,
        manifest=_entries_with_probe(probe_command="python -m probe.ok"),
    )

    proc = MagicMock()
    proc.returncode = 0
    proc.stderr = ""
    proc.stdout = ""

    async def run() -> StartResponse:
        return await svc.start(
            name="automation-service",
            env_overrides={"PORT": "8080", "API_TOKEN": "tok"},
            actor="admin@test",
        )

    with patch.object(subprocess, "run", return_value=proc):
        response = asyncio.run(run())

    assert response.state == "running"

    actions = [e.action for e in audit.write_with_retry_calls]
    # Probe audit must appear; final ``start`` audit must come after it.
    assert "service_connectivity_probe_passed" in actions
    assert "start" in actions
    assert actions.index("service_connectivity_probe_passed") < actions.index(
        "start"
    ), (
        f"connectivity probe must run before final audit; "
        f"actions={actions!r}"
    )
