"""Property tests: Dependency Chain Orchestration (Q11).

**Property 5: Dependency Chain Topological Order (Q11)**
**Validates: Requirements 5.1, 5.3, 5.4, 5.7, 5.8**

For any randomly generated acyclic dependency graph (max depth 3),
``start(root)`` must call ``compose.up`` in topological order
(dependencies before dependents) and skip services already in
``state="running"``.

**Property 6: Dependency Depth Guard (Q11)**
**Validates: Requirements 5.2**

For graphs with depth N > 3, ``MaxDependencyDepthExceededError`` must
be raised and a ``dependency_chain_max_depth_exceeded`` audit row written.

**Property 7: Dependency Failure Isolation (Q11)**
**Validates: Requirements 5.5**

When a random dependency fails (raises ``ComposeFailureError``), already
started sibling services remain in ``state="running"`` and a
``dependency_start_failed`` audit row is written.

Strategy
--------
Hypothesis generates random acyclic dependency graphs as adjacency dicts,
then wires a ``LifecycleService`` with fake collaborators to verify the
invariants above.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pytest
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.audit_writer import AuditEntry, AuditWriteOutcome  # noqa: E402
from src.lifecycle.compose_runner import (  # noqa: E402
    ComposeFailureError,
    ComposeResult,
    TestResult,
)
from src.lifecycle.health_probe import HealthSnapshot  # noqa: E402
from src.lifecycle.service import (  # noqa: E402
    DependencyStartFailedError,
    LifecycleService,
    LifecycleStateCache,
    MaxDependencyDepthExceededError,
    StartResponse,
)
from src.manifest import ManagedServiceEntry  # noqa: E402

# ---------------------------------------------------------------------------
# Fake collaborators
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditWriter:
    """Records every audit interaction."""

    precheck_calls: int = 0
    write_calls: list[AuditEntry] = field(default_factory=list)
    write_with_retry_calls: list[AuditEntry] = field(default_factory=list)
    precheck_raise: BaseException | None = None

    async def precheck(self) -> None:
        self.precheck_calls += 1
        if self.precheck_raise is not None:
            raise self.precheck_raise

    async def write(self, entry: AuditEntry) -> None:
        self.write_calls.append(entry)

    async def write_with_retry(self, entry: AuditEntry) -> AuditWriteOutcome:
        self.write_with_retry_calls.append(entry)
        return AuditWriteOutcome(deferred=False)


@dataclass
class _FakeVaultClient:
    """No-op Vault client."""

    writes: list[tuple[str, str, str]] = field(default_factory=list)
    stored: dict[str, dict[str, str]] = field(default_factory=dict)

    async def write_env_override(
        self, *, service_name: str, key: str, value: str
    ) -> None:
        self.writes.append((service_name, key, value))
        self.stored.setdefault(service_name, {})[key] = value

    async def read_env_overrides(self, *, service_name: str) -> dict[str, str]:
        return dict(self.stored.get(service_name, {}))

    async def delete_env_override(self, *, service_name: str, key: str) -> None:
        self.stored.get(service_name, {}).pop(key, None)


@dataclass
class _FakeHealthProbe:
    """Always returns a healthy snapshot."""

    calls: list[ManagedServiceEntry] = field(default_factory=list)

    async def probe(self, entry: ManagedServiceEntry) -> HealthSnapshot:
        self.calls.append(entry)
        return HealthSnapshot(
            ts=datetime.now(timezone.utc),
            healthz_status=200,
            healthz_body="ok",
            readyz_status=200,
            readyz_body="ok",
            state="healthy",
        )


@dataclass
class _FailingComposeRunner:
    """Fails for a specific service name; succeeds for all others."""

    fail_service: str
    up_calls: list[str] = field(default_factory=list)
    stop_calls: list[str] = field(default_factory=list)

    async def up(
        self,
        *,
        profile: str,
        service_name: str,
        env_overrides: dict[str, str] | None = None,
    ) -> ComposeResult:
        self.up_calls.append(service_name)
        if service_name == self.fail_service:
            raise ComposeFailureError(
                argv=("docker", "compose", "up", "-d", service_name),
                exit_code=1,
                stdout="",
                stderr=f"simulated failure for {service_name}",
            )
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "up", "-d", service_name),
        )

    async def stop(
        self, *, service_name: str, remove_volumes: bool = False
    ) -> ComposeResult:
        self.stop_calls.append(service_name)
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "stop", service_name),
        )

    async def logs(
        self, *, service_name: str, tail: int, follow: bool
    ) -> ComposeResult:
        return ComposeResult(
            exit_code=0,
            stdout="",
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
        return TestResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=tuple(argv),
        )


@dataclass
class _RecordingComposeRunner:
    """Records compose.up calls in order; always succeeds."""

    up_calls: list[str] = field(default_factory=list)
    stop_calls: list[str] = field(default_factory=list)

    async def up(
        self,
        *,
        profile: str,
        service_name: str,
        env_overrides: dict[str, str] | None = None,
    ) -> ComposeResult:
        self.up_calls.append(service_name)
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "up", "-d", service_name),
        )

    async def stop(
        self, *, service_name: str, remove_volumes: bool = False
    ) -> ComposeResult:
        self.stop_calls.append(service_name)
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "stop", service_name),
        )

    async def logs(
        self, *, service_name: str, tail: int, follow: bool
    ) -> ComposeResult:
        return ComposeResult(
            exit_code=0,
            stdout="",
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
        return TestResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=tuple(argv),
        )


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------

# A "service graph" is represented as a dict[str, list[str]] where
# keys are service names and values are their direct dependencies
# (which must also be keys in the dict).
#
# We generate acyclic graphs by assigning each node a "level" and only
# allowing edges from higher levels to lower levels (level 0 = leaf).
# This guarantees no cycles and gives us control over depth.

_SERVICE_NAME_STRATEGY = st.from_regex(r"svc-[a-z]{3,6}", fullmatch=True)


def _build_env_example(workspace: Path, service_name: str, *, is_root: bool = False) -> None:
    """Create a minimal .env.example for a service.

    Root service gets PORT=8080 (so tests can pass env_overrides={"PORT": "8080"}).
    Dependency services get an empty .env.example so that recursive _do_start
    calls with env_overrides={} pass form schema validation.
    """
    svc_dir = workspace / "services" / service_name
    svc_dir.mkdir(parents=True, exist_ok=True)
    content = "PORT=8080\n" if is_root else "# no required env vars\n"
    (svc_dir / ".env.example").write_text(content, encoding="utf-8")


def _make_entry(name: str, deps: list[str]) -> ManagedServiceEntry:
    """Build a ManagedServiceEntry with the given dependencies."""
    return ManagedServiceEntry(
        name=name,
        kind="http_service",
        compose_service_name=name,
        compose_profile=name,
        env_example_path=f"services/{name}/.env.example",
        health_endpoint="/healthz",
        test_command=None,
        depends_on_services=tuple(deps),
        feature_flag_dependency=(),
    )


def _topological_sort(graph: dict[str, list[str]]) -> list[str]:
    """Return a topological ordering of the graph (dependencies first).

    Uses Kahn's algorithm. Assumes the graph is acyclic.
    """
    in_degree: dict[str, int] = {node: 0 for node in graph}
    for node, deps in graph.items():
        for dep in deps:
            if dep in in_degree:
                in_degree[dep] = in_degree.get(dep, 0)  # ensure key exists
    # Recompute: in_degree[node] = number of nodes that depend on node
    in_degree = {node: 0 for node in graph}
    for node, deps in graph.items():
        for dep in deps:
            if dep in graph:
                in_degree[dep] = in_degree.get(dep, 0)

    # Actually compute: for each node, count how many others list it as dep
    in_degree = {node: 0 for node in graph}
    for _node, deps in graph.items():
        for dep in deps:
            if dep in in_degree:
                in_degree[dep] += 1

    queue = [n for n, d in in_degree.items() if d == 0]
    result: list[str] = []
    while queue:
        node = queue.pop(0)
        result.append(node)
        for other, deps in graph.items():
            if node in deps:
                in_degree[other] -= 1
                if in_degree[other] == 0:
                    queue.append(other)
    return result


def _make_service_with_graph(
    *,
    workspace: Path,
    graph: dict[str, list[str]],
    root: str,
    compose_runner: Any,
    initial_running: set[str] | None = None,
) -> tuple[LifecycleService, _FakeAuditWriter]:
    """Wire a LifecycleService for the given dependency graph."""
    audit = _FakeAuditWriter()
    vault = _FakeVaultClient()
    health = _FakeHealthProbe()

    # Build manifest entries
    entries = tuple(_make_entry(name, deps) for name, deps in graph.items())

    # Build initial state cache
    state: dict[str, LifecycleStateCache] = {}
    for name in graph:
        slot = LifecycleStateCache(name=name)
        if initial_running and name in initial_running:
            slot.state = "running"
        state[name] = slot

    async def _no_sleep(_: float) -> None:
        return None

    svc = LifecycleService(
        manifest=entries,
        state=state,
        audit=audit,  # type: ignore[arg-type]
        vault=vault,  # type: ignore[arg-type]
        compose=compose_runner,  # type: ignore[arg-type]
        health=health,  # type: ignore[arg-type]
        workspace_root=workspace,
        feature_flag_reader=None,
        health_ready_timeout_seconds=1.0,
        sleep=_no_sleep,
    )
    return svc, audit


# ---------------------------------------------------------------------------
# Hypothesis strategies for acyclic graphs
# ---------------------------------------------------------------------------

# We generate graphs with a fixed "root" node and up to 3 levels of deps.
# Level 0 = leaves (no deps), level 1 = depends on level 0, etc.
# Root is at the highest level.
#
# Graph shape: root -> [level-1 nodes] -> [level-0 nodes]
# Max depth = 3 means root at level 2 (0-indexed), so path length = 3.


@st.composite
def _acyclic_graph_strategy(draw: st.DrawFn, max_depth: int = 2) -> dict[str, list[str]]:
    """Generate a random acyclic dependency graph with depth <= max_depth.

    Returns a dict mapping service_name -> list[dependency_names].
    The root node is always named "root-svc".
    All nodes are manifest-resident (no external deps).
    """
    # Generate unique names for each level
    # Level 0: leaves (1-3 nodes)
    n_leaves = draw(st.integers(min_value=1, max_value=3))
    leaves = [f"leaf-{i}" for i in range(n_leaves)]

    if max_depth == 0:
        # Only root, no deps
        return {"root-svc": []}

    if max_depth == 1:
        # root -> leaves
        graph: dict[str, list[str]] = {"root-svc": leaves}
        for leaf in leaves:
            graph[leaf] = []
        return graph

    # max_depth == 2: root -> mid -> leaves
    n_mid = draw(st.integers(min_value=1, max_value=2))
    mid_nodes = [f"mid-{i}" for i in range(n_mid)]

    graph = {}
    # Each mid node depends on a subset of leaves
    for mid in mid_nodes:
        n_deps = draw(st.integers(min_value=1, max_value=max(1, n_leaves)))
        deps = draw(
            st.lists(
                st.sampled_from(leaves),
                min_size=1,
                max_size=n_deps,
                unique=True,
            )
        )
        graph[mid] = deps

    # Root depends on all mid nodes
    graph["root-svc"] = mid_nodes
    for leaf in leaves:
        graph[leaf] = []

    return graph


def _deep_graph_strategy() -> dict[str, list[str]]:
    """Return a graph with depth exactly 4 (exceeds MAX_DEPENDENCY_DEPTH=3).

    Chain: root-svc -> level3 -> level2 -> level1 -> leaf
    This creates a path of length 4, which exceeds the max depth of 3.
    """
    return {
        "root-svc": ["level3"],
        "level3": ["level2"],
        "level2": ["level1"],
        "level1": ["leaf"],
        "leaf": [],
    }


# ---------------------------------------------------------------------------
# Property 5: Dependency Chain Topological Order
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(graph=_acyclic_graph_strategy(max_depth=2))
def test_topological_order_compose_up_calls(
    graph: dict[str, list[str]],
    tmp_path: Path,
) -> None:
    """Property 5: start(root) calls compose.up in topological order.

    **Validates: Requirements 5.1, 5.3, 5.4, 5.7, 5.8**

    For any acyclic dependency graph with max depth 3:
    - compose.up is called for every non-running service in the graph.
    - Dependencies are started before their dependents (topological order).
    - Services already in state="running" are skipped (idempotent).
    """
    # Build workspace with .env.example for each service
    for name in graph:
        _build_env_example(tmp_path, name, is_root=(name == "root-svc"))

    compose = _RecordingComposeRunner()
    svc, audit = _make_service_with_graph(
        workspace=tmp_path,
        graph=graph,
        root="root-svc",
        compose_runner=compose,
        initial_running=None,
    )

    async def _run() -> StartResponse:
        return await svc.start(
            name="root-svc",
            env_overrides={"PORT": "8080"},
            actor="admin@test",
        )

    response = asyncio.run(_run())
    assert response.state == "running", (
        f"Expected state='running', got {response.state!r}"
    )

    # Compute the set of services reachable from root-svc (transitive closure)
    def _reachable(start: str, g: dict[str, list[str]]) -> set[str]:
        visited: set[str] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in visited or node not in g:
                continue
            visited.add(node)
            stack.extend(g[node])
        return visited

    reachable = _reachable("root-svc", graph)

    # Every reachable service should have been started
    assert set(compose.up_calls) == reachable, (
        f"Expected compose.up for all reachable services {reachable!r}, "
        f"got {set(compose.up_calls)!r}"
    )

    # Verify topological order: for each service, all its deps appear
    # before it in the up_calls list
    up_order = compose.up_calls
    for service, deps in graph.items():
        if service not in up_order:
            continue
        service_idx = up_order.index(service)
        for dep in deps:
            if dep in up_order:
                dep_idx = up_order.index(dep)
                assert dep_idx < service_idx, (
                    f"Dependency {dep!r} (index {dep_idx}) must be started "
                    f"before {service!r} (index {service_idx}). "
                    f"up_calls={up_order!r}, graph={graph!r}"
                )


@hyp_settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(graph=_acyclic_graph_strategy(max_depth=2))
def test_already_running_services_are_skipped(
    graph: dict[str, list[str]],
    tmp_path: Path,
) -> None:
    """Property 5 (idempotent skip): already running services are not restarted.

    **Validates: Requirements 5.3**

    When some services are already in state="running", compose.up must
    NOT be called for them.
    """
    # Build workspace
    for name in graph:
        _build_env_example(tmp_path, name, is_root=(name == "root-svc"))

    # Mark all leaf nodes (no deps) as already running
    already_running = {name for name, deps in graph.items() if not deps}

    compose = _RecordingComposeRunner()
    svc, audit = _make_service_with_graph(
        workspace=tmp_path,
        graph=graph,
        root="root-svc",
        compose_runner=compose,
        initial_running=already_running,
    )

    async def _run() -> StartResponse:
        return await svc.start(
            name="root-svc",
            env_overrides={"PORT": "8080"},
            actor="admin@test",
        )

    response = asyncio.run(_run())
    assert response.state == "running", (
        f"Expected state='running', got {response.state!r}"
    )

    # Already-running services must NOT appear in compose.up_calls
    for running_svc in already_running:
        assert running_svc not in compose.up_calls, (
            f"Service {running_svc!r} was already running but compose.up "
            f"was called for it. up_calls={compose.up_calls!r}"
        )

    # Non-running services that are reachable from root must have been started
    def _reachable(start: str, g: dict[str, list[str]]) -> set[str]:
        visited: set[str] = set()
        stack = [start]
        while stack:
            node = stack.pop()
            if node in visited or node not in g:
                continue
            visited.add(node)
            stack.extend(g[node])
        return visited

    reachable = _reachable("root-svc", graph)
    expected_started = reachable - already_running
    assert set(compose.up_calls) == expected_started, (
        f"Expected compose.up for {expected_started!r}, "
        f"got {set(compose.up_calls)!r}"
    )


# ---------------------------------------------------------------------------
# Property 6: Dependency Depth Guard
# ---------------------------------------------------------------------------


def test_depth_guard_raises_for_depth_4(tmp_path: Path) -> None:
    """Property 6: depth > 3 raises MaxDependencyDepthExceededError.

    **Validates: Requirements 5.2**

    A chain of depth 4 (root -> level3 -> level2 -> level1 -> leaf)
    must raise MaxDependencyDepthExceededError and write a
    dependency_chain_max_depth_exceeded audit row.
    """
    graph = _deep_graph_strategy()
    for name in graph:
        _build_env_example(tmp_path, name, is_root=(name == "root-svc"))

    compose = _RecordingComposeRunner()
    svc, audit = _make_service_with_graph(
        workspace=tmp_path,
        graph=graph,
        root="root-svc",
        compose_runner=compose,
        initial_running=None,
    )

    async def _run() -> None:
        await svc.start(
            name="root-svc",
            env_overrides={"PORT": "8080"},
            actor="admin@test",
        )

    with pytest.raises((MaxDependencyDepthExceededError, DependencyStartFailedError)):
        asyncio.run(_run())

    # Audit row must be written
    depth_exceeded_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "dependency_chain_max_depth_exceeded"
    ]
    assert len(depth_exceeded_rows) >= 1, (
        f"Expected at least 1 dependency_chain_max_depth_exceeded audit row, "
        f"got {len(depth_exceeded_rows)}. "
        f"All audit rows: {[e.action for e in audit.write_with_retry_calls]!r}"
    )


@hyp_settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    extra_depth=st.integers(min_value=1, max_value=3),
)
def test_depth_guard_deterministic_for_various_depths(
    extra_depth: int,
    tmp_path: Path,
) -> None:
    """Property 6: depth guard fires deterministically for any depth > 3.

    **Validates: Requirements 5.2**

    For any chain of depth 3 + extra_depth (where extra_depth >= 1),
    MaxDependencyDepthExceededError (or DependencyStartFailedError wrapping it)
    must be raised and the audit row written.
    """
    # Build a chain of depth 3 + extra_depth
    # root -> d1 -> d2 -> ... -> d(2+extra_depth) -> leaf
    depth = 3 + extra_depth
    nodes = ["root-svc"] + [f"dep-{i}" for i in range(depth - 1)] + ["leaf"]
    graph: dict[str, list[str]] = {}
    for i, node in enumerate(nodes):
        if i + 1 < len(nodes):
            graph[node] = [nodes[i + 1]]
        else:
            graph[node] = []

    for name in graph:
        _build_env_example(tmp_path, name, is_root=(name == "root-svc"))

    compose = _RecordingComposeRunner()
    svc, audit = _make_service_with_graph(
        workspace=tmp_path,
        graph=graph,
        root="root-svc",
        compose_runner=compose,
        initial_running=None,
    )

    async def _run() -> None:
        await svc.start(
            name="root-svc",
            env_overrides={"PORT": "8080"},
            actor="admin@test",
        )

    with pytest.raises((MaxDependencyDepthExceededError, DependencyStartFailedError)):
        asyncio.run(_run())

    # Audit row must be written
    depth_exceeded_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "dependency_chain_max_depth_exceeded"
    ]
    assert len(depth_exceeded_rows) >= 1, (
        f"Expected at least 1 dependency_chain_max_depth_exceeded audit row "
        f"for depth={depth}, got {len(depth_exceeded_rows)}. "
        f"All audit rows: {[e.action for e in audit.write_with_retry_calls]!r}"
    )


# ---------------------------------------------------------------------------
# Property 7: Dependency Failure Isolation
# ---------------------------------------------------------------------------


@hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(graph=_acyclic_graph_strategy(max_depth=1))
def test_dependency_failure_isolation(
    graph: dict[str, list[str]],
    tmp_path: Path,
) -> None:
    """Property 7: failing dep leaves sibling services running.

    **Validates: Requirements 5.5**

    When a random dependency fails (raises ComposeFailureError):
    - DependencyStartFailedError is raised (or wraps the failure).
    - Already-started sibling services remain in state="running".
    - A dependency_start_failed audit row is written.
    - The parent service is NOT started.
    """
    # We need at least 2 deps for "sibling" semantics
    deps = graph.get("root-svc", [])
    if len(deps) < 2:
        # Not enough siblings to test isolation; skip by returning early
        return

    for name in graph:
        _build_env_example(tmp_path, name, is_root=(name == "root-svc"))

    # Pick the last dep to fail (so at least one sibling starts first)
    fail_dep = deps[-1]
    sibling_deps = deps[:-1]

    compose = _FailingComposeRunner(fail_service=fail_dep)
    svc, audit = _make_service_with_graph(
        workspace=tmp_path,
        graph=graph,
        root="root-svc",
        compose_runner=compose,
        initial_running=None,
    )

    async def _run() -> None:
        await svc.start(
            name="root-svc",
            env_overrides={"PORT": "8080"},
            actor="admin@test",
        )

    with pytest.raises(DependencyStartFailedError) as exc_info:
        asyncio.run(_run())

    # The failed dependency must be identified
    assert exc_info.value.failed_dependency == fail_dep, (
        f"Expected failed_dependency={fail_dep!r}, "
        f"got {exc_info.value.failed_dependency!r}"
    )

    # Sibling services that started before the failure must remain running
    for sibling in sibling_deps:
        sibling_state = svc.state_cache[sibling].state
        assert sibling_state == "running", (
            f"Sibling {sibling!r} should remain 'running' after dep failure, "
            f"got state={sibling_state!r}. "
            f"fail_dep={fail_dep!r}, deps={deps!r}"
        )

    # dependency_start_failed audit row must be written
    fail_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "dependency_start_failed"
    ]
    assert len(fail_rows) >= 1, (
        f"Expected at least 1 dependency_start_failed audit row, "
        f"got {len(fail_rows)}. "
        f"All audit rows: {[e.action for e in audit.write_with_retry_calls]!r}"
    )

    # The audit row must identify the parent and failed dependency
    fail_row = fail_rows[0]
    assert fail_row.details_json["failed_dependency"] == fail_dep, (
        f"Audit failed_dependency mismatch: {fail_row.details_json!r}"
    )
    assert fail_row.details_json["parent_service"] == "root-svc", (
        f"Audit parent_service mismatch: {fail_row.details_json!r}"
    )

    # Root service must NOT have been started (compose.up not called for it)
    assert "root-svc" not in compose.up_calls, (
        f"root-svc should not have been started after dep failure. "
        f"up_calls={compose.up_calls!r}"
    )


@hyp_settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    n_deps=st.integers(min_value=2, max_value=4),
    fail_index=st.integers(min_value=1, max_value=3),
)
def test_dependency_failure_isolation_parametric(
    n_deps: int,
    fail_index: int,
    tmp_path: Path,
) -> None:
    """Property 7 (parametric): sibling isolation holds for any fail position.

    **Validates: Requirements 5.5**

    For a root with n_deps dependencies, failing the dep at fail_index
    must leave all deps started before it in state="running".
    """
    # Clamp fail_index to valid range (must be > 0 so at least one sibling starts)
    actual_fail_idx = (fail_index % (n_deps - 1)) + 1  # range [1, n_deps-1]

    dep_names = [f"dep-{i}" for i in range(n_deps)]
    fail_dep = dep_names[actual_fail_idx]
    pre_started_siblings = dep_names[:actual_fail_idx]

    graph: dict[str, list[str]] = {"root-svc": dep_names}
    for dep in dep_names:
        graph[dep] = []

    for name in graph:
        _build_env_example(tmp_path, name, is_root=(name == "root-svc"))

    compose = _FailingComposeRunner(fail_service=fail_dep)
    svc, audit = _make_service_with_graph(
        workspace=tmp_path,
        graph=graph,
        root="root-svc",
        compose_runner=compose,
        initial_running=None,
    )

    async def _run() -> None:
        await svc.start(
            name="root-svc",
            env_overrides={"PORT": "8080"},
            actor="admin@test",
        )

    with pytest.raises(DependencyStartFailedError):
        asyncio.run(_run())

    # Pre-started siblings must remain running
    for sibling in pre_started_siblings:
        sibling_state = svc.state_cache[sibling].state
        assert sibling_state == "running", (
            f"Pre-started sibling {sibling!r} should remain 'running', "
            f"got state={sibling_state!r}. "
            f"fail_dep={fail_dep!r}, n_deps={n_deps}, fail_idx={actual_fail_idx}"
        )

    # dependency_start_failed audit row must be written
    fail_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "dependency_start_failed"
    ]
    assert len(fail_rows) >= 1, (
        f"Expected at least 1 dependency_start_failed audit row, "
        f"got {len(fail_rows)}"
    )

    # Audit must identify the correct failed dependency
    assert fail_rows[0].details_json["failed_dependency"] == fail_dep, (
        f"Audit failed_dependency mismatch: expected {fail_dep!r}, "
        f"got {fail_rows[0].details_json.get('failed_dependency')!r}"
    )


# ---------------------------------------------------------------------------
# Property 6 (extended): Dependency Depth Guard over random DAG topologies
# ---------------------------------------------------------------------------
#
# The static / parametric tests above cover linear chains. Task 6.7 asks
# specifically for *random DAG* topologies so we exercise the depth guard
# under structures that mix branching with chain-shaped paths. The strategy
# below generates a DAG whose longest root-to-leaf path is exactly
# ``depth`` edges, optionally fattened with sibling branches that fork off
# intermediate nodes (so the graph is genuinely a DAG, not a chain).
#
# Invariants covered (Validates: Requirements 5.2):
#
#   * For every random DAG with longest-path-depth ``d``:
#       - ``d <= MAX_DEPENDENCY_DEPTH``  →  start(root) succeeds.
#       - ``d >  MAX_DEPENDENCY_DEPTH``  →  MaxDependencyDepthExceededError
#         (possibly wrapped in DependencyStartFailedError) is raised AND a
#         ``dependency_chain_max_depth_exceeded`` audit row is written.
#
#   * Cycles in the dependency graph (which manifest-time validation
#     normally rejects but unit tests bypass by constructing fake
#     ManagedServiceEntry instances directly) are caught at runtime by the
#     same depth guard — recursion grows the path until length >= 3 and the
#     guard fires deterministically.


@st.composite
def _dag_with_longest_path_strategy(
    draw: st.DrawFn,
    *,
    depth: int,
    branch_width: int = 2,
) -> dict[str, list[str]]:
    """Generate a random acyclic dependency graph with a controllable depth.

    The graph is shaped as a "spine" plus random sibling branches:

      root -> spine_1 -> spine_2 -> ... -> spine_{depth-1} -> spine_{depth}

    where each spine node may additionally depend on 0..``branch_width``
    auxiliary leaves (which have no further deps). The longest root-to-leaf
    path is exactly ``depth`` edges; the helper returns the adjacency dict
    in the format ``{service_name: [dependency_names]}``.

    A ``depth`` of 0 yields ``{"root-svc": []}`` (root has no deps).
    """
    if depth == 0:
        return {"root-svc": []}

    # Spine: root-svc -> spine_1 -> spine_2 -> ... -> spine_{depth}
    spine = ["root-svc"] + [f"spine-{i}" for i in range(1, depth + 1)]
    graph: dict[str, list[str]] = {node: [] for node in spine}
    for i in range(len(spine) - 1):
        graph[spine[i]].append(spine[i + 1])

    # Optionally fork random sibling leaves off each non-leaf spine node.
    # Each spine node (except the deepest) gets 0..branch_width extra leaves.
    aux_counter = 0
    for spine_node in spine[:-1]:
        n_extra = draw(st.integers(min_value=0, max_value=branch_width))
        for _ in range(n_extra):
            leaf_name = f"aux-{aux_counter}"
            aux_counter += 1
            graph[leaf_name] = []
            graph[spine_node].append(leaf_name)

    return graph


def _longest_path_depth(graph: dict[str, list[str]], root: str) -> int:
    """Compute the longest root-to-leaf path length (in edges) from ``root``.

    Assumes ``graph`` is acyclic. Used to verify our generators produce the
    expected depth.
    """
    memo: dict[str, int] = {}

    def _depth(node: str) -> int:
        if node in memo:
            return memo[node]
        deps = graph.get(node, [])
        if not deps:
            memo[node] = 0
            return 0
        memo[node] = 1 + max(_depth(d) for d in deps)
        return memo[node]

    return _depth(root)


@hyp_settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    depth=st.integers(min_value=0, max_value=2),
    data=st.data(),
)
def test_depth_guard_allows_random_dags_within_max_depth(
    depth: int,
    data: st.DataObject,
    tmp_path: Path,
) -> None:
    """Property 6 (within bounds): random DAGs with depth ≤ MAX succeed.

    **Validates: Requirements 5.2**

    For any random DAG whose longest root-to-leaf path is at most
    :data:`MAX_DEPENDENCY_DEPTH - 1 = 2` edges, ``start(root)`` must
    complete successfully and no ``dependency_chain_max_depth_exceeded``
    audit row is written.

    A path of ``depth`` edges produces a deepest recursion path of length
    ``depth`` (the deepest descendant is invoked with the parent chain as
    ``_recursion_path``); the guard fires when ``len(path) >= 3``, so any
    ``depth <= 2`` is accepted.
    """
    graph = data.draw(_dag_with_longest_path_strategy(depth=depth))
    # Sanity: our generator really produced the requested depth.
    assert _longest_path_depth(graph, "root-svc") == depth, (
        f"Generator bug: expected longest path = {depth}, "
        f"got {_longest_path_depth(graph, 'root-svc')}; graph={graph!r}"
    )

    for name in graph:
        _build_env_example(tmp_path, name, is_root=(name == "root-svc"))

    compose = _RecordingComposeRunner()
    svc, audit = _make_service_with_graph(
        workspace=tmp_path,
        graph=graph,
        root="root-svc",
        compose_runner=compose,
        initial_running=None,
    )

    async def _run() -> StartResponse:
        return await svc.start(
            name="root-svc",
            env_overrides={"PORT": "8080"},
            actor="admin@test",
        )

    response = asyncio.run(_run())
    assert response.state == "running", (
        f"Expected state='running' for depth={depth}, got {response.state!r}; "
        f"graph={graph!r}"
    )

    # No depth-exceeded audit must be written when we stay within bounds.
    depth_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "dependency_chain_max_depth_exceeded"
    ]
    assert depth_rows == [], (
        f"Unexpected depth-exceeded audit row(s) for valid depth={depth}: "
        f"{[e.details_json for e in depth_rows]!r}"
    )

    # Every node in the graph should have been started exactly once.
    assert sorted(compose.up_calls) == sorted(graph.keys()), (
        f"Expected one compose.up call per node {sorted(graph.keys())!r}, "
        f"got {compose.up_calls!r}"
    )


@hyp_settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    depth=st.integers(min_value=3, max_value=6),
    data=st.data(),
)
def test_depth_guard_rejects_random_dags_exceeding_max_depth(
    depth: int,
    data: st.DataObject,
    tmp_path: Path,
) -> None:
    """Property 6 (exceeding bounds): random DAGs with depth > MAX raise.

    **Validates: Requirements 5.2**

    For any random DAG whose longest root-to-leaf path is at least
    :data:`MAX_DEPENDENCY_DEPTH = 3` edges, ``start(root)`` must raise
    :class:`MaxDependencyDepthExceededError` (or :class:`DependencyStartFailedError`
    wrapping it through the recursive ``_start_dependencies`` boundary)
    and a ``dependency_chain_max_depth_exceeded`` audit row must be
    written. The branching siblings off the spine do *not* prevent the
    guard from firing — the guard depends purely on path length, not
    fan-out.
    """
    graph = data.draw(_dag_with_longest_path_strategy(depth=depth))
    assert _longest_path_depth(graph, "root-svc") == depth, (
        f"Generator bug: expected longest path = {depth}, "
        f"got {_longest_path_depth(graph, 'root-svc')}; graph={graph!r}"
    )

    for name in graph:
        _build_env_example(tmp_path, name, is_root=(name == "root-svc"))

    compose = _RecordingComposeRunner()
    svc, audit = _make_service_with_graph(
        workspace=tmp_path,
        graph=graph,
        root="root-svc",
        compose_runner=compose,
        initial_running=None,
    )

    async def _run() -> None:
        await svc.start(
            name="root-svc",
            env_overrides={"PORT": "8080"},
            actor="admin@test",
        )

    with pytest.raises((MaxDependencyDepthExceededError, DependencyStartFailedError)):
        asyncio.run(_run())

    depth_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "dependency_chain_max_depth_exceeded"
    ]
    assert len(depth_rows) >= 1, (
        f"Expected dependency_chain_max_depth_exceeded audit row for "
        f"depth={depth}, got 0. graph={graph!r}, "
        f"all audit actions: {[e.action for e in audit.write_with_retry_calls]!r}"
    )

    # The first audit row must reference a recursion path of the maximum
    # allowed length so the operator can see exactly where the guard fired.
    first_row = depth_rows[0]
    rec_path = first_row.details_json.get("recursion_path", [])
    assert isinstance(rec_path, list), (
        f"recursion_path must be a list, got {rec_path!r}"
    )
    assert len(rec_path) == 3, (
        f"recursion_path must have length 3 (= MAX_DEPENDENCY_DEPTH) at "
        f"the moment of guard firing; got len={len(rec_path)}, "
        f"path={rec_path!r}"
    )
    assert first_row.details_json.get("max_depth") == 3, (
        f"audit must record max_depth=3, got {first_row.details_json!r}"
    )


# ---------------------------------------------------------------------------
# Property 6 (cycles): cycles in the dependency graph are rejected at runtime
# ---------------------------------------------------------------------------


@st.composite
def _cyclic_graph_strategy(draw: st.DrawFn) -> dict[str, list[str]]:
    """Generate a dependency graph that contains a cycle reachable from root.

    Strategy: build a forward chain ``root-svc -> n_1 -> n_2 -> ... -> n_k``
    and then add a back-edge from ``n_k`` to one of the earlier nodes
    (``root-svc`` or any ``n_i``). This guarantees a directed cycle reachable
    from ``root-svc``. The chain length is randomised so cycles of varying
    sizes are exercised, including the minimum 2-node ``root-svc <-> n_1``
    cycle (a self-referential pair).

    Note: ``manifest._check_no_dependency_cycles`` would normally reject
    such a manifest at load time. By constructing :class:`ManagedServiceEntry`
    instances directly, this test bypasses load-time validation to verify
    that the *runtime* depth guard is also a sound defence-in-depth layer
    (matches the implementation note in :data:`MAX_DEPENDENCY_DEPTH`'s
    docstring).
    """
    chain_len = draw(st.integers(min_value=1, max_value=4))
    nodes = ["root-svc"] + [f"cyc-{i}" for i in range(1, chain_len + 1)]
    # Forward edges
    graph: dict[str, list[str]] = {n: [] for n in nodes}
    for i in range(len(nodes) - 1):
        graph[nodes[i]].append(nodes[i + 1])
    # Back-edge from the deepest node to one of the earlier nodes
    target_idx = draw(st.integers(min_value=0, max_value=len(nodes) - 2))
    graph[nodes[-1]].append(nodes[target_idx])
    return graph


@hyp_settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(graph=_cyclic_graph_strategy())
def test_cycles_in_dependency_graph_rejected_by_depth_guard(
    graph: dict[str, list[str]],
    tmp_path: Path,
) -> None:
    """Property 6 (cycles): cyclic dependency graphs are rejected at runtime.

    **Validates: Requirements 5.2**

    For any dependency graph that contains a cycle reachable from the root,
    ``start(root)`` must terminate (no unbounded recursion) and raise
    :class:`MaxDependencyDepthExceededError` (possibly wrapped in
    :class:`DependencyStartFailedError`). A
    ``dependency_chain_max_depth_exceeded`` audit row is written so the
    operator can pinpoint the cycle.

    This is a defence-in-depth complement to manifest-load-time cycle
    detection (``manifest._check_no_dependency_cycles``): even if a
    pathological manifest somehow bypassed the loader's DFS check, the
    runtime guard fires deterministically once recursion reaches
    :data:`MAX_DEPENDENCY_DEPTH`.
    """
    for name in graph:
        _build_env_example(tmp_path, name, is_root=(name == "root-svc"))

    compose = _RecordingComposeRunner()
    svc, audit = _make_service_with_graph(
        workspace=tmp_path,
        graph=graph,
        root="root-svc",
        compose_runner=compose,
        initial_running=None,
    )

    async def _run() -> None:
        await svc.start(
            name="root-svc",
            env_overrides={"PORT": "8080"},
            actor="admin@test",
        )

    with pytest.raises((MaxDependencyDepthExceededError, DependencyStartFailedError)):
        asyncio.run(_run())

    depth_rows = [
        e for e in audit.write_with_retry_calls
        if e.action == "dependency_chain_max_depth_exceeded"
    ]
    assert len(depth_rows) >= 1, (
        f"Expected dependency_chain_max_depth_exceeded audit row for cycle, "
        f"got 0. graph={graph!r}, "
        f"all audit actions: {[e.action for e in audit.write_with_retry_calls]!r}"
    )
    # The recursion path on which the guard fires must include the cycle's
    # repeated visit to the same node — so its length is exactly
    # MAX_DEPENDENCY_DEPTH = 3 at the moment of firing.
    first = depth_rows[0]
    rec_path = first.details_json.get("recursion_path", [])
    assert len(rec_path) == 3, (
        f"recursion_path must have length 3 when guard fires on cycle; "
        f"got len={len(rec_path)}, path={rec_path!r}, graph={graph!r}"
    )
