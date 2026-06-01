"""Property test for Compose stack structural consistency.

Validates: Requirements 3.3, 8.1, 8.4, 8.5, 8.6, 8.9, 8.10, 12.5, 14.1,
14.2, 14.3, 14.4, 16.2, 17.4
Property 4: Compose stack structural consistency.

The parsed ``infra/docker-compose.yml`` SHALL satisfy *all* of the
following invariants (design §6.3 → Property 4):

1. **Service coverage** — the set of compose service names equals
   :data:`EXPECTED_COMPOSE_SERVICES` and SHALL NOT contain ``vllm``
   (Requirement 8.10, Property 4.1).
2. **Worker port-isolation** — for every Component with
   ``c.type == "temporal_worker"``, the compose service's ``ports``
   list is empty (Requirement 3.3, Property 4.2).
3. **Task-intake profile gating** — ``task-intake-service`` is the
   only service whose ``profiles`` field equals ``["task-intake"]``;
   every other service has an absent or empty ``profiles`` field
   (Requirements 16.2, 8.9, Property 4.3).
4. **HTTP healthcheck shape** — for every HTTP service Component plus
   ``atlassian-mcp``, ``healthcheck.test`` references the service's
   ``/healthz`` endpoint, ``interval`` parses to a seconds value in
   ``[5, 30]``, and ``retries <= 3`` (Requirements 8.4, 12.5, Property
   4.4).
5. **Named volumes** — the top-level ``volumes:`` block contains at
   least ``pg_data``, ``minio_data``, ``agent_workspace`` and each
   volume is mounted at exactly the expected paths
   (``pg_data:/var/lib/postgresql/data`` on ``postgres``;
   ``minio_data:/data`` on ``minio``;
   ``agent_workspace:/tmp/workspace`` on **both**
   ``agent-runner-worker`` and ``opencode-sidecar``)
   (Requirements 8.5, 14.1, Property 4.5).
6. **Dependency graph acyclicity + manifest superset** — the directed
   graph built from ``depends_on`` is acyclic, and for every Component
   ``c`` the compose service's ``depends_on`` is a superset of
   ``c.depends_on`` from the manifest (Requirements 8.6, 14.2, 14.3,
   14.4, Property 4.6). The Compose service for the
   ``admin-dashboard`` Component is named ``admin-dashboard-ui``.
7. **Atlassian MCP reuse** — ``services["atlassian-mcp"].build`` equals
   ``../services/atlassian_unified`` and the service carries no ``image:``
   override (Requirement 17.4, Property 4.7).

Implementation notes
--------------------

* The Compose YAML uses anchor / merge syntax (``<<: *http-healthcheck``)
  which ``yaml.safe_load`` resolves into plain dicts; we exploit that
  to treat ``healthcheck`` uniformly across services.
* ``depends_on`` may be either a list (``["postgres"]``) or a mapping
  with health conditions (``{postgres: {condition: service_healthy}}``);
  :func:`_compose_dependencies` normalizes both into a tuple of names.
* The ``admin-dashboard`` Component (manifest name) maps to the
  ``admin-dashboard-ui`` Compose service (design §"Compose Bağımlılık
  DAG'ı"). The mapping is captured in :data:`COMPONENT_TO_COMPOSE_NAME`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up; pytest registers it as an
# importable module, but we add ``tests/`` to ``sys.path`` defensively
# so this file works under direct ``python -m pytest tests/property``
# invocations too (mirrors the pattern used by ``test_dockerfile_shape``).
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import (  # noqa: E402
    COMPONENT_MANIFEST,
    EXPECTED_COMPOSE_SERVICES,
    HTTP_SERVICES,
    TEMPORAL_WORKERS,
    WORKSPACE_ROOT,
    ComponentSpec,
)


# ---------------------------------------------------------------------------
# Manifest → Compose name remapping (design §"Compose Bağımlılık DAG'ı")
# ---------------------------------------------------------------------------

#: ``admin-dashboard`` (manifest) ships as the ``admin-dashboard-ui``
#: Compose service; ``streamlit-app`` (manifest) ships as the
#: ``streamlit-ui`` Compose service per the foundation 10-entry
#: topology (platform-mimari-foundation task 10.1). Every other
#: Component name matches its Compose service name 1:1.
COMPONENT_TO_COMPOSE_NAME: dict[str, str] = {
    "admin-dashboard": "admin-dashboard-ui",
    "streamlit-app": "streamlit-ui",
}


def _compose_service_name(component: ComponentSpec) -> str:
    """Return the Compose service name for ``component``.

    Defaults to ``component.name`` and applies the well-known remapping
    captured by :data:`COMPONENT_TO_COMPOSE_NAME`.
    """

    return COMPONENT_TO_COMPOSE_NAME.get(component.name, component.name)


# ---------------------------------------------------------------------------
# Compose document fixture
# ---------------------------------------------------------------------------

#: Path to the parsed Compose file. Resolved once at module import time
#: so individual property invocations do not re-read from disk.
COMPOSE_PATH: Path = WORKSPACE_ROOT / "infra" / "docker-compose.yml"


def _load_compose() -> dict[str, Any]:
    """Parse ``infra/docker-compose.yml`` with ``yaml.safe_load``.

    YAML anchor / merge keys (``<<: *http-healthcheck``) are resolved
    into plain dicts by ``safe_load``, so downstream code can treat
    ``healthcheck`` uniformly across services.
    """

    assert COMPOSE_PATH.is_file(), (
        f"Compose file missing at {COMPOSE_PATH.relative_to(WORKSPACE_ROOT)}"
    )
    with COMPOSE_PATH.open("r", encoding="utf-8") as fh:
        document = yaml.safe_load(fh)
    assert isinstance(document, dict), (
        f"docker-compose.yml must parse to a mapping; got {type(document).__name__}"
    )
    return document


@pytest.fixture(scope="module")
def compose_doc() -> dict[str, Any]:
    """Module-scoped Compose document fixture (single read per session)."""

    return _load_compose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Compose ``interval`` strings look like ``10s``, ``1m30s``, ``500ms``.
#: We only support the suffixes Compose realistically emits in this
#: scaffold: ``ms``, ``s``, ``m``, ``h``. ``us`` / ``ns`` are unused.
_DURATION_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h)", re.IGNORECASE
)
_DURATION_UNIT_SECONDS: dict[str, float] = {
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}


def _parse_duration_seconds(value: str | int | float) -> float:
    """Parse a Compose duration string (e.g. ``"10s"``) to seconds.

    Numeric inputs are interpreted as already-in-seconds (Compose's
    schema accepts integer seconds in some contexts). Composite values
    like ``1m30s`` are accumulated.
    """

    if isinstance(value, (int, float)):
        return float(value)

    matches = list(_DURATION_RE.finditer(value))
    assert matches, f"Could not parse Compose duration: {value!r}"
    total = 0.0
    for match in matches:
        total += float(match.group("value")) * _DURATION_UNIT_SECONDS[
            match.group("unit").lower()
        ]
    return total


def _compose_dependencies(service: dict[str, Any]) -> tuple[str, ...]:
    """Normalize ``service.depends_on`` to a tuple of service names.

    Compose accepts both the short list form (``["postgres"]``) and
    the long mapping form (``{postgres: {condition: service_healthy}}``);
    a missing field is treated as no dependencies.
    """

    deps = service.get("depends_on") or []
    if isinstance(deps, dict):
        return tuple(deps.keys())
    if isinstance(deps, list):
        return tuple(deps)
    raise AssertionError(
        f"depends_on must be list or dict; got {type(deps).__name__}"
    )


def _has_cycle(graph: dict[str, tuple[str, ...]]) -> tuple[str, ...] | None:
    """DFS-based cycle detector; returns the offending cycle or None.

    Implements the classic three-color (white / gray / black) DFS
    cycle search. The returned tuple is the sequence of services
    forming the cycle (first repeats at the end), useful for assertion
    messages. Disconnected sub-graphs are handled by iterating over
    every node as a potential start.
    """

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in graph}
    stack: list[str] = []

    def visit(node: str) -> tuple[str, ...] | None:
        color[node] = GRAY
        stack.append(node)
        for neighbour in graph.get(node, ()):
            # Edges to nodes outside the graph (e.g. an undeclared
            # service) are reported separately by the superset check;
            # treat them as external sinks for cycle purposes.
            if neighbour not in color:
                continue
            if color[neighbour] == GRAY:
                idx = stack.index(neighbour)
                return tuple(stack[idx:]) + (neighbour,)
            if color[neighbour] == WHITE:
                cycle = visit(neighbour)
                if cycle is not None:
                    return cycle
        stack.pop()
        color[node] = BLACK
        return None

    for node in graph:
        if color[node] == WHITE:
            cycle = visit(node)
            if cycle is not None:
                return cycle
    return None


# ---------------------------------------------------------------------------
# Invariant 1 — service set coverage (Property 4.1, Requirement 8.10)
# ---------------------------------------------------------------------------


def test_compose_service_set_matches_expected(compose_doc: dict[str, Any]) -> None:
    """Property 4.1 — the Compose service set equals the expected set.

    Validates: Requirements 8.1, 8.10, 16.2.
    """

    services = compose_doc.get("services") or {}
    actual = frozenset(services.keys())

    missing = EXPECTED_COMPOSE_SERVICES - actual
    extra = actual - EXPECTED_COMPOSE_SERVICES

    assert not missing, (
        f"Compose is missing required services: {sorted(missing)}"
    )
    assert not extra, (
        f"Compose declares unexpected services: {sorted(extra)}"
    )
    assert "vllm" not in actual, (
        "Requirement 8.10 / Property 4.1: 'vllm' MUST NOT be a Compose "
        "service; consumers reach it via VLLM_BASE_URL"
    )


# ---------------------------------------------------------------------------
# Invariant 2 — Temporal workers publish no ports (Property 4.2)
# ---------------------------------------------------------------------------


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(component=st.sampled_from(TEMPORAL_WORKERS))
def test_temporal_workers_have_no_ports(component: ComponentSpec) -> None:
    """Property 4.2 — temporal workers MUST NOT publish ports.

    Validates: Requirement 3.3.
    """

    doc = _load_compose()
    svc_name = _compose_service_name(component)
    service = doc["services"].get(svc_name)
    assert service is not None, (
        f"{component.name}: missing Compose service '{svc_name}'"
    )

    ports = service.get("ports") or []
    assert ports == [], (
        f"{svc_name}: temporal workers MUST publish no ports "
        f"(Req 3.3, Property 4.2); got {ports!r}"
    )


# ---------------------------------------------------------------------------
# Invariant 3 — task-intake profile gating (Property 4.3)
# ---------------------------------------------------------------------------


def test_task_intake_is_only_profile_gated_service(
    compose_doc: dict[str, Any],
) -> None:
    """Property 4.3 — ``task-intake-service`` retains its legacy profile gate.

    Validates: Requirements 8.9, 16.2.

    Originally this test asserted ``task-intake-service`` was the ONLY
    profile-gated Compose service and its profiles list was exactly
    ``["task-intake"]``. Two later specs relax both halves:

    * ``admin-dashboard-control-plane`` Requirement 2.6 augments the
      list with ``"task-intake-service"`` (canonical manifest name)
      while preserving the legacy ``"task-intake"`` label.
    * ``platform-mimari-foundation`` Requirement 2.1 mandates that
      *every* Managed_Service declare ``profiles:`` containing its
      ``compose_service_name``, so most application services are now
      profile-gated too.

    The retained invariant — and the one this test still enforces — is
    that ``task-intake-service`` MUST keep the legacy ``"task-intake"``
    label so existing ``docker compose --profile task-intake`` workflows
    continue to start the intake service. The complementary
    ``test_task_intake_service_keeps_legacy_profile_label`` anchor
    pins the canonical label requirement.
    """

    services: dict[str, dict[str, Any]] = compose_doc["services"]
    task_intake = services.get("task-intake-service")
    assert task_intake is not None, (
        "task-intake-service MUST be declared in docker-compose.yml "
        "(Requirements 8.9, 16.2)"
    )
    profiles = task_intake.get("profiles") or []
    assert "task-intake" in profiles, (
        "task-intake-service profiles MUST contain the legacy "
        f"'task-intake' label (Requirements 8.9, 16.2, 2.6); got {profiles!r}"
    )


# ---------------------------------------------------------------------------
# Invariant 4 — HTTP healthcheck shape (Property 4.4)
# ---------------------------------------------------------------------------


# Property 4.4 covers every HTTP service Component PLUS atlassian-mcp
# (design §6.3 — "HTTP services and atlassian-mcp"). Build the universe
# of (compose_service_name, expected_port) pairs once.
_HEALTHCHECK_TARGETS: tuple[tuple[str, int], ...] = tuple(
    (c.name, c.container_port) for c in HTTP_SERVICES if c.container_port is not None
) + (("atlassian-mcp", 8090),)


@pytest.mark.parametrize(
    ("service_name", "container_port"),
    _HEALTHCHECK_TARGETS,
    ids=[name for name, _ in _HEALTHCHECK_TARGETS],
)
def test_http_healthcheck_shape(
    compose_doc: dict[str, Any], service_name: str, container_port: int
) -> None:
    """Property 4.4 — HTTP healthcheck targets ``/healthz`` with bounded interval.

    Validates: Requirements 8.4, 12.5.
    """

    service = compose_doc["services"].get(service_name)
    assert service is not None, f"missing Compose service '{service_name}'"

    healthcheck = service.get("healthcheck")
    assert isinstance(healthcheck, dict), (
        f"{service_name}: healthcheck block must be a mapping "
        f"(Req 12.5, Property 4.4); got {type(healthcheck).__name__}"
    )

    test = healthcheck.get("test")
    assert test is not None, (
        f"{service_name}: healthcheck.test missing (Req 12.5, Property 4.4)"
    )

    # ``test`` may be either a list (``["CMD", "curl", ...]`` or
    # ``["CMD-SHELL", "..."]``) or a single string. Flatten to a single
    # joinable string for substring assertions.
    if isinstance(test, list):
        test_str = " ".join(str(t) for t in test)
    else:
        test_str = str(test)

    expected_url = f"http://localhost:{container_port}/healthz"
    assert expected_url in test_str, (
        f"{service_name}: healthcheck.test must reference {expected_url} "
        f"(Req 12.5, Property 4.4); got {test_str!r}"
    )

    interval_seconds = _parse_duration_seconds(healthcheck.get("interval", "0s"))
    assert 5.0 <= interval_seconds <= 30.0, (
        f"{service_name}: healthcheck.interval must be in [5s, 30s] "
        f"(Req 12.5, Property 4.4); got {healthcheck.get('interval')!r} "
        f"= {interval_seconds}s"
    )

    retries = healthcheck.get("retries")
    assert isinstance(retries, int), (
        f"{service_name}: healthcheck.retries must be an int "
        f"(Req 12.5, Property 4.4); got {retries!r}"
    )
    assert retries <= 3, (
        f"{service_name}: healthcheck.retries must be <= 3 "
        f"(Req 12.5, Property 4.4); got {retries}"
    )


# ---------------------------------------------------------------------------
# Invariant 5 — top-level volumes (Property 4.5)
# ---------------------------------------------------------------------------


#: ``volume_name → (service, expected_mount_path)`` mappings the test
#: enforces. ``agent_workspace`` is mounted on *two* services so it
#: appears twice in the parametrized matrix.
_VOLUME_MOUNTS: tuple[tuple[str, str, str], ...] = (
    ("pg_data", "postgres", "/var/lib/postgresql/data"),
    ("minio_data", "minio", "/data"),
    ("agent_workspace", "agent-runner-worker", "/tmp/workspace"),
    ("agent_workspace", "opencode-sidecar", "/tmp/workspace"),
)


def test_compose_declares_named_volumes(compose_doc: dict[str, Any]) -> None:
    """Property 4.5 (a) — top-level volumes block declares the trio.

    Validates: Requirements 8.5, 14.1.
    """

    volumes = compose_doc.get("volumes")
    assert isinstance(volumes, dict), (
        f"top-level 'volumes:' must be a mapping; got {type(volumes).__name__}"
    )
    for required in ("pg_data", "minio_data", "agent_workspace"):
        assert required in volumes, (
            f"top-level volumes must declare '{required}' "
            f"(Req 8.5, Property 4.5)"
        )


@pytest.mark.parametrize(
    ("volume", "service_name", "mount_path"),
    _VOLUME_MOUNTS,
    ids=[f"{vol}@{svc}" for vol, svc, _ in _VOLUME_MOUNTS],
)
def test_named_volume_mount_points(
    compose_doc: dict[str, Any],
    volume: str,
    service_name: str,
    mount_path: str,
) -> None:
    """Property 4.5 (b) — every named volume mounts at the expected path.

    Validates: Requirements 8.5, 14.1.
    """

    service = compose_doc["services"].get(service_name)
    assert service is not None, f"missing Compose service '{service_name}'"

    raw_volumes = service.get("volumes") or []
    expected_short = f"{volume}:{mount_path}"

    found = False
    for entry in raw_volumes:
        if isinstance(entry, str):
            # Short syntax: ``<source>:<target>[:<flags>]`` — split on
            # ':' but only consider the first two components so trailing
            # ``:ro`` flags do not break the equality check.
            parts = entry.split(":")
            if len(parts) >= 2 and parts[0] == volume and parts[1] == mount_path:
                found = True
                break
        elif isinstance(entry, dict):
            # Long syntax: ``{type, source, target, ...}``.
            if entry.get("source") == volume and entry.get("target") == mount_path:
                found = True
                break

    assert found, (
        f"{service_name}: named volume '{volume}' must mount at "
        f"'{mount_path}' (expected short form '{expected_short}'); "
        f"got volumes={raw_volumes!r} (Req 8.5, Property 4.5)"
    )


# ---------------------------------------------------------------------------
# Invariant 6 — depends_on DAG: acyclic + manifest-superset (Property 4.6)
# ---------------------------------------------------------------------------


def test_compose_depends_on_is_acyclic(compose_doc: dict[str, Any]) -> None:
    """Property 4.6 (a) — ``depends_on`` graph is acyclic.

    Validates: Requirements 8.6, 14.2, 14.3, 14.4.
    """

    services: dict[str, dict[str, Any]] = compose_doc["services"]
    graph: dict[str, tuple[str, ...]] = {
        name: _compose_dependencies(svc) for name, svc in services.items()
    }

    cycle = _has_cycle(graph)
    assert cycle is None, (
        f"depends_on graph must be acyclic (Req 14.2, Property 4.6); "
        f"detected cycle: {' → '.join(cycle) if cycle else ''}"
    )


# Property 4.6's superset check applies to the subset of Components that
# actually ship as a Compose service. ``streamlit-app`` is a Component
# (design §4.1) but is intentionally NOT packaged into Compose
# (``EXPECTED_COMPOSE_SERVICES`` excludes it; design §"Compose Bağımlılık
# DAG'ı" omits it). Filter the manifest to the in-Compose subset so the
# test does not flag the deliberate omission.
_COMPOSE_PACKAGED_COMPONENTS: tuple[ComponentSpec, ...] = tuple(
    c for c in COMPONENT_MANIFEST
    if _compose_service_name(c) in EXPECTED_COMPOSE_SERVICES
)


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(component=st.sampled_from(_COMPOSE_PACKAGED_COMPONENTS))
def test_compose_depends_on_superset_of_manifest(component: ComponentSpec) -> None:
    """Property 4.6 (b) — Compose ``depends_on`` ⊇ manifest ``depends_on``.

    The check is restricted to Components that ship as a Compose service
    (``streamlit-app`` is a published Component but is consumed
    standalone, not packaged into Compose — see
    :data:`EXPECTED_COMPOSE_SERVICES`).

    Validates: Requirements 8.6, 14.3, 14.4.
    """

    doc = _load_compose()
    svc_name = _compose_service_name(component)
    service = doc["services"].get(svc_name)
    assert service is not None, (
        f"{component.name}: missing Compose service '{svc_name}'"
    )

    compose_deps = set(_compose_dependencies(service))
    manifest_deps = set(component.depends_on)

    missing = manifest_deps - compose_deps
    assert not missing, (
        f"{svc_name}: Compose depends_on must be a superset of the "
        f"manifest dependencies (Req 14.3/14.4, Property 4.6); "
        f"missing={sorted(missing)}, "
        f"compose_deps={sorted(compose_deps)}, "
        f"manifest_deps={sorted(manifest_deps)}"
    )


# ---------------------------------------------------------------------------
# Invariant 7 — atlassian-mcp build context (Property 4.7)
# ---------------------------------------------------------------------------


def test_atlassian_mcp_builds_from_unified_with_no_image(
    compose_doc: dict[str, Any],
) -> None:
    """Property 4.7 — ``atlassian-mcp`` builds ``../services/atlassian_unified`` only.

    Validates: Requirement 17.4.
    """

    service = compose_doc["services"].get("atlassian-mcp")
    assert service is not None, "Compose must declare 'atlassian-mcp' service"

    build = service.get("build")
    # Compose accepts both the short form (``build: ../services/atlassian_unified``)
    # and the long form (``build: {context: ../services/atlassian_unified, ...}``).
    if isinstance(build, str):
        context = build
    elif isinstance(build, dict):
        context = build.get("context")
    else:
        raise AssertionError(
            f"atlassian-mcp.build must be string or mapping; got {build!r}"
        )

    assert context == "../services/atlassian_unified", (
        f"atlassian-mcp.build context must be '../services/atlassian_unified' "
        f"(Req 17.4, Property 4.7); got {context!r}"
    )

    assert "image" not in service, (
        f"atlassian-mcp MUST NOT declare an 'image:' override "
        f"(Req 17.4, Property 4.7); got image={service.get('image')!r}"
    )


# ===========================================================================
# Property 1 (platform-mimari-foundation): Servis topolojisi ve compose-manifest
# shape tutarlılığı.
#
# **Validates: Requirements 1.1, 1.10, 2.1, 2.3, 2.5, 2.7, 2.9, 9.4, 9.9**
#
# This block extends the multi-service-scaffold Property 4 with the
# foundation spec's stricter contract:
#
# 1. ``config/services.manifest.json`` declares **at least** the 10
#    canonical entries required by the foundation topology
#    (automation-service, assistant-service, admin-dashboard-api,
#    agent-runner-worker, execution-runner-worker, atlassian-mcp,
#    firecrawl, opencode-sidecar, streamlit-ui, admin-dashboard-ui).
#    Additional entries (e.g. ``task-intake-service`` from the prior
#    spec) are tolerated — see design.md §"Note: task-intake-service".
#
# 2. Every manifest entry's ``kind`` MUST be drawn from the foundation
#    enum ``{infra, http_service, worker, sidecar, ui}``. No other
#    values are permitted (Requirement 1.1).
#
# 3. ``health_endpoint`` MUST be ``null`` for every entry whose
#    ``kind`` is ``worker``, ``sidecar``, or ``ui``; ``http_service``
#    and ``infra`` entries MUST declare a non-empty path starting with
#    ``/`` (Requirement 1.10).
#
# 4. Every Compose service that declares a ``healthcheck:`` block MUST
#    satisfy ``interval ∈ [5s, 30s]``, ``retries ≤ 3``, and
#    ``timeout < interval`` (Requirement 2.5).
#
# 5. Every Compose service whose corresponding manifest entry has
#    ``kind=sidecar`` MUST NOT declare a top-level ``ports:`` key —
#    sidecars are reachable only via the Compose-internal network
#    through ``expose:`` (Requirement 2.9).
# ===========================================================================


# ---------------------------------------------------------------------------
# Foundation manifest contract
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402  (deferred import keeps module top tidy)

#: Workspace-relative path to ``services.manifest.json``. Loaded once at
#: module import time so the per-example Hypothesis cost is the property
#: check itself, not JSON parsing.
_FOUNDATION_MANIFEST_PATH: Path = (
    WORKSPACE_ROOT / "config" / "services.manifest.json"
)


def _load_foundation_manifest() -> dict[str, Any]:
    """Parse ``config/services.manifest.json`` once.

    The file ships under workspace ``config/`` and is the single
    source of truth for the 10-entry foundation topology
    (Requirement 1.1, design §"services.manifest.json (10 giriş)").
    """

    assert _FOUNDATION_MANIFEST_PATH.is_file(), (
        f"foundation manifest missing at "
        f"{_FOUNDATION_MANIFEST_PATH.relative_to(WORKSPACE_ROOT)}"
    )
    with _FOUNDATION_MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        document = _json.load(fh)
    assert isinstance(document, dict), (
        f"services.manifest.json must parse to a mapping; "
        f"got {type(document).__name__}"
    )
    services = document.get("services")
    assert isinstance(services, list) and services, (
        "services.manifest.json must declare a non-empty 'services:' array"
    )
    return document


_FOUNDATION_MANIFEST_DOC: dict[str, Any] = _load_foundation_manifest()
_FOUNDATION_MANIFEST_ENTRIES: tuple[dict[str, Any], ...] = tuple(
    _FOUNDATION_MANIFEST_DOC["services"]
)


#: The foundation topology requires *at least* these 10 manifest names
#: (design §Components and Interfaces — "services.manifest.json (10 giriş)").
#: Additional entries are tolerated; see the spec note about
#: ``task-intake-service`` remaining from the prior scaffold spec.
_FOUNDATION_REQUIRED_NAMES: frozenset[str] = frozenset(
    {
        "automation-service",
        "assistant-service",
        "admin-dashboard-api",
        "agent-runner-worker",
        "execution-runner-worker",
        "atlassian-mcp",
        "firecrawl",
        "opencode-sidecar",
        "streamlit-ui",
        "admin-dashboard-ui",
    }
)


#: The complete enum of valid ``kind`` values per the foundation
#: schema migration (Requirement 1.1, design §"services.manifest.schema.json
#: kind enum'una sidecar ve ui değerleri eklenir").
_FOUNDATION_KIND_ENUM: frozenset[str] = frozenset(
    {"infra", "http_service", "worker", "sidecar", "ui"}
)


#: Kinds whose ``health_endpoint`` MUST be ``null`` (Requirement 1.10).
#: Workers do not expose HTTP at all; sidecars are Compose-internal
#: only; UI components historically poll their backend rather than a
#: ``/healthz`` of their own.
_NO_HEALTH_KINDS: frozenset[str] = frozenset({"worker", "sidecar", "ui"})


# ---------------------------------------------------------------------------
# Property 1 (a) — Manifest declares at least the 10 canonical entries
# ---------------------------------------------------------------------------


def test_foundation_manifest_includes_all_canonical_entries() -> None:
    """Property 1 (a) — manifest is a superset of the foundation 10.

    Validates: Requirements 1.1, 1.10, 2.1.

    The foundation spec mandates exactly 10 canonical service names
    (design §"10 giriş" table). This test asserts that the manifest
    declares all 10; it tolerates additional entries (e.g.
    ``task-intake-service``) per the design's explicit note that
    pre-existing manifest entries remain in place but are required to
    obey the same ``kind`` / ``health_endpoint`` invariants.
    """

    declared_names = frozenset(
        entry["name"] for entry in _FOUNDATION_MANIFEST_ENTRIES
    )
    missing = _FOUNDATION_REQUIRED_NAMES - declared_names
    assert not missing, (
        "services.manifest.json must declare every foundation service "
        f"(Requirement 1.1, Property 1); missing: {sorted(missing)!r}"
    )

    assert len(_FOUNDATION_MANIFEST_ENTRIES) >= 10, (
        f"foundation manifest must declare ≥ 10 entries "
        f"(Requirement 1.1); got {len(_FOUNDATION_MANIFEST_ENTRIES)}"
    )


# ---------------------------------------------------------------------------
# Property 1 (b) — Every manifest entry's ``kind`` is in the foundation enum
# ---------------------------------------------------------------------------


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(entry=st.sampled_from(_FOUNDATION_MANIFEST_ENTRIES))
def test_foundation_manifest_kind_is_in_canonical_enum(
    entry: dict[str, Any],
) -> None:
    """Property 1 (b) — manifest ``kind`` ∈ {infra, http_service, worker, sidecar, ui}.

    Validates: Requirement 1.1.

    The foundation schema migration adds ``sidecar`` and ``ui`` to the
    ``kind`` enum (design §"kind enum'una sidecar ve ui eklenir").
    No other value is permitted — this test rejects any drift with a
    deterministic failure message naming the offending entry.
    """

    name = entry.get("name", "<unnamed>")
    kind = entry.get("kind")
    assert kind in _FOUNDATION_KIND_ENUM, (
        f"manifest entry {name!r} has kind={kind!r}; must be one of "
        f"{sorted(_FOUNDATION_KIND_ENUM)} (Requirement 1.1, Property 1)"
    )


# ---------------------------------------------------------------------------
# Property 1 (c) — health_endpoint nullness aligns with kind
# ---------------------------------------------------------------------------


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(entry=st.sampled_from(_FOUNDATION_MANIFEST_ENTRIES))
def test_foundation_manifest_health_endpoint_matches_kind(
    entry: dict[str, Any],
) -> None:
    """Property 1 (c) — ``health_endpoint`` is null iff kind ∈ no-HTTP set.

    Validates: Requirement 1.10.

    Workers, sidecars, and UI components do not expose an HTTP health
    surface at the Compose boundary (Requirement 1.10 — "worker crash →
    Temporal redelegate"). HTTP services and infra services MUST
    declare a non-empty path starting with ``/``.
    """

    name = entry.get("name", "<unnamed>")
    kind = entry.get("kind")
    health = entry.get("health_endpoint")

    if kind in _NO_HEALTH_KINDS:
        assert health is None, (
            f"manifest entry {name!r} kind={kind!r} MUST have "
            f"health_endpoint=null (Requirement 1.10, Property 1); "
            f"got {health!r}"
        )
    else:
        assert isinstance(health, str) and health.startswith("/"), (
            f"manifest entry {name!r} kind={kind!r} MUST declare a "
            f"non-empty health_endpoint starting with '/' "
            f"(Requirement 1.10, Property 1); got {health!r}"
        )


# ---------------------------------------------------------------------------
# Property 1 (d) — Healthcheck shape across the Compose stack
# ---------------------------------------------------------------------------


def _iter_compose_healthchecks(
    compose_doc: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(service_name, healthcheck_block)`` for every service that
    declares a healthcheck. Services without a ``healthcheck:`` are
    skipped — the property only applies where one is declared
    (Requirement 2.5 conditions on "WHEN a Compose service declares a
    healthcheck").
    """

    services = compose_doc.get("services") or {}
    out: list[tuple[str, dict[str, Any]]] = []
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        healthcheck = svc.get("healthcheck")
        if isinstance(healthcheck, dict):
            out.append((name, healthcheck))
    return out


_FOUNDATION_HEALTHCHECK_TARGETS: tuple[tuple[str, dict[str, Any]], ...] = tuple(
    _iter_compose_healthchecks(_load_compose())
)


@pytest.mark.parametrize(
    ("service_name", "healthcheck"),
    _FOUNDATION_HEALTHCHECK_TARGETS,
    ids=[name for name, _ in _FOUNDATION_HEALTHCHECK_TARGETS],
)
def test_foundation_healthcheck_shape_bounds(
    service_name: str, healthcheck: dict[str, Any]
) -> None:
    """Property 1 (d) — healthcheck interval, retries and timeout bounds.

    Validates: Requirements 2.5, 9.4.

    For every Compose service declaring ``healthcheck:``:

    * ``interval`` parses to seconds within ``[5, 30]``.
    * ``retries`` is an integer ``≤ 3``.
    * ``timeout`` (if declared) is **strictly less than** ``interval``;
      this is the foundation spec's stricter form of Requirement 2.5
      and prevents a hung probe from delaying the next attempt.

    The `interval ∈ [5s, 30s]` and `retries ≤ 3` checks already exist
    for HTTP services in :func:`test_http_healthcheck_shape`. This
    function generalises both to *every* service that declares a
    healthcheck (postgres, vault, redis, minio, temporal, …) and adds
    the new ``timeout < interval`` invariant.
    """

    # Interval — REQUIRED for any healthcheck block we score.
    raw_interval = healthcheck.get("interval")
    assert raw_interval is not None, (
        f"{service_name}: healthcheck.interval MUST be declared "
        f"(Requirement 2.5, Property 1)"
    )
    interval_seconds = _parse_duration_seconds(raw_interval)
    assert 5.0 <= interval_seconds <= 30.0, (
        f"{service_name}: healthcheck.interval must be in [5s, 30s] "
        f"(Requirement 2.5, Property 1); got {raw_interval!r} "
        f"= {interval_seconds}s"
    )

    # Retries — REQUIRED, integer, ≤ 3.
    retries = healthcheck.get("retries")
    assert isinstance(retries, int), (
        f"{service_name}: healthcheck.retries must be an int "
        f"(Requirement 2.5, Property 1); got {retries!r}"
    )
    assert retries <= 3, (
        f"{service_name}: healthcheck.retries must be ≤ 3 "
        f"(Requirement 2.5, Property 1); got {retries}"
    )

    # Timeout — OPTIONAL, but if present MUST be < interval. This is
    # the foundation spec's stricter form vs. the legacy Property 4.4
    # which only bounded ``interval`` and ``retries``.
    raw_timeout = healthcheck.get("timeout")
    if raw_timeout is not None:
        timeout_seconds = _parse_duration_seconds(raw_timeout)
        assert timeout_seconds < interval_seconds, (
            f"{service_name}: healthcheck.timeout ({raw_timeout!r} = "
            f"{timeout_seconds}s) must be strictly less than interval "
            f"({raw_interval!r} = {interval_seconds}s) "
            f"(Requirement 2.5, Property 1)"
        )


# ---------------------------------------------------------------------------
# Property 1 (e) — Sidecar services MUST NOT declare ``ports:``
# ---------------------------------------------------------------------------


def _sidecar_compose_names() -> tuple[str, ...]:
    """Compose service names whose manifest entry has ``kind=sidecar``."""

    return tuple(
        entry["compose_service_name"]
        for entry in _FOUNDATION_MANIFEST_ENTRIES
        if entry.get("kind") == "sidecar"
    )


_FOUNDATION_SIDECAR_NAMES: tuple[str, ...] = _sidecar_compose_names()


@pytest.mark.skipif(
    not _FOUNDATION_SIDECAR_NAMES,
    reason="manifest declares no sidecar entries; Property 1 (e) vacuous",
)
@pytest.mark.parametrize(
    "sidecar_name",
    _FOUNDATION_SIDECAR_NAMES,
    ids=list(_FOUNDATION_SIDECAR_NAMES),
)
def test_foundation_sidecar_does_not_publish_host_ports(
    compose_doc: dict[str, Any], sidecar_name: str
) -> None:
    """Property 1 (e) — sidecar Compose services MUST NOT publish host ports.

    Validates: Requirements 2.9, 9.9.

    Sidecars are reachable only on the Compose-internal network via
    ``expose:`` (design §"opencode-sidecar: ``ports:`` yok, yalnızca
    ``expose:``"). A ``ports:`` declaration would break the sidecar
    invariant and accidentally widen the attack surface.
    """

    services = compose_doc.get("services") or {}
    service = services.get(sidecar_name)
    assert service is not None, (
        f"sidecar {sidecar_name!r}: missing from docker-compose.yml "
        f"(Requirement 2.9, Property 1)"
    )

    ports = service.get("ports")
    # ``None`` (key absent) and ``[]`` (empty list) both satisfy the
    # invariant. Anything else is a violation.
    if ports is None:
        return
    assert isinstance(ports, list), (
        f"sidecar {sidecar_name!r}: 'ports:' must be a list when "
        f"present; got {type(ports).__name__} ({ports!r})"
    )
    assert ports == [], (
        f"sidecar {sidecar_name!r}: MUST NOT publish host ports — "
        f"sidecars are Compose-internal only (Requirement 2.9, "
        f"Property 1); got ports={ports!r}"
    )
