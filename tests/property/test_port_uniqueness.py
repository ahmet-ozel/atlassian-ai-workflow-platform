"""Property test for host-port uniqueness across published Components.

Validates: Requirements 2.1, 2.2, 2.3, 4.1, 4.7, 17.3
Property 3: Host-port uniqueness across published Components.

For any two distinct Components ``c1, c2`` in
:data:`COMPONENT_MANIFEST` whose ``host_port`` is not ``None``, the
equality ``c1.host_port == c2.host_port`` SHALL imply
``c1.name == c2.name`` (i.e. ports are unique modulo identity).

The same uniqueness SHALL hold across the union of:

* Component-published host ports (from ``COMPONENT_MANIFEST``).
* Infrastructure-published host ports (from
  :data:`INFRA_PUBLISHED_PORTS`): ``postgres:5432``, ``redis:6379``,
  ``vault:8200``, ``temporal:7233``, ``temporal-ui:8233``,
  ``minio:9000``, ``minio:9001``, ``firecrawl:3002``,
  ``atlassian-mcp:8090``.

This property is the source-of-truth invariant behind every per-host
Compose stack: if it fails, ``docker compose up`` will collide on the
overlapping port and one of the two services will refuse to bind.

The third assertion cross-checks the live ``infra/docker-compose.yml``:
its parsed ``ports:`` blocks (host side of every ``"H:C"`` mapping)
must match the manifest + infra-published-ports model exactly and
must contain no duplicates.

Strategy
--------

Hypothesis draws an ordered pair ``(c1, c2)`` from
``st.sampled_from(COMPONENT_MANIFEST)`` × ``st.sampled_from(...)``.
For each draw with both ``host_port`` values populated, the test
asserts ``c1.host_port == c2.host_port → c1.name == c2.name``. The
example space is finite (8 × 8 = 64 ordered pairs) so the property
exhaustively covers the manifest under the default Hypothesis budget.
The complementary deterministic checks (full-set distinctness,
Compose cross-check) run unconditionally as part of the same module.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up; pytest registers it as an
# importable module, but we add ``tests/`` to ``sys.path`` defensively
# so this file works under direct ``python -m pytest tests/property``
# invocations too.
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import (  # noqa: E402
    COMPONENT_MANIFEST,
    EXPECTED_COMPOSE_SERVICES,
    INFRA_PUBLISHED_PORTS,
    WORKSPACE_ROOT,
    ComponentSpec,
)


# ---------------------------------------------------------------------------
# Property test — pair-wise uniqueness over COMPONENT_MANIFEST
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    c1=st.sampled_from(COMPONENT_MANIFEST),
    c2=st.sampled_from(COMPONENT_MANIFEST),
)
def test_host_port_pairwise_uniqueness(
    c1: ComponentSpec, c2: ComponentSpec
) -> None:
    """Property 3 (pair-wise) — colliding host ports imply same Component.

    Validates: Requirements 2.1, 2.2, 2.3, 4.1, 4.7, 17.3.

    For any pair ``(c1, c2)`` drawn from ``COMPONENT_MANIFEST`` with
    both ``host_port`` populated, ``c1.host_port == c2.host_port``
    SHALL imply ``c1.name == c2.name``. Pairs where either side has
    ``host_port is None`` (Temporal workers) are vacuously satisfied
    and skipped, mirroring the design's "published Components" scope.
    """

    if c1.host_port is None or c2.host_port is None:
        # Temporal workers expose no host port — out of Property 3 scope.
        return

    if c1.host_port == c2.host_port:
        assert c1.name == c2.name, (
            "Host-port collision: "
            f"{c1.name!r}({c1.host_port}) vs {c2.name!r}({c2.host_port}). "
            "Two distinct published Components must not share a host port."
        )


# ---------------------------------------------------------------------------
# Deterministic checks — full (Component ∪ infra) port universe
# ---------------------------------------------------------------------------


def _collect_universe() -> list[tuple[str, int]]:
    """Build the canonical ``(owner, host_port)`` list.

    The owner is ``ComponentSpec.name`` for application Components and
    the infra service name (e.g. ``"postgres"``) for infrastructure
    services. ``minio`` contributes two entries (``9000`` and ``9001``)
    as declared by ``INFRA_PUBLISHED_PORTS``.
    """

    universe: list[tuple[str, int]] = []
    for component in COMPONENT_MANIFEST:
        if component.host_port is not None:
            universe.append((component.name, component.host_port))
    for infra_name, ports in INFRA_PUBLISHED_PORTS.items():
        for port in ports:
            universe.append((infra_name, port))
    return universe


def test_component_plus_infra_ports_are_globally_unique() -> None:
    """Property 3 (universe) — all host ports distinct across the stack.

    Validates: Requirements 2.1, 2.2, 2.3, 4.1, 4.7, 17.3.

    Build the full ``(owner, port)`` list from ``COMPONENT_MANIFEST``
    plus ``INFRA_PUBLISHED_PORTS`` and assert:

    1. The number of unique ports equals the number of entries — i.e.
       no two owners share a host port.
    2. Each ``(owner, port)`` entry is itself unique (the manifest +
       infra map should never declare the same row twice).

    This is the deterministic complement to the Hypothesis pair-wise
    property and exhaustively covers the cross-product
    Components × infra services, including the multi-port ``minio``
    entry that the @given strategy alone cannot reach.
    """

    universe = _collect_universe()

    # (1) ports are pair-wise unique across the entire stack.
    ports = [port for _owner, port in universe]
    port_counts = Counter(ports)
    duplicates = {p: n for p, n in port_counts.items() if n > 1}
    assert not duplicates, (
        f"Host-port collision across Component + infra universe: {duplicates}. "
        f"Universe = {sorted(universe, key=lambda t: t[1])}"
    )
    assert len(set(ports)) == len(universe), (
        f"Port set size {len(set(ports))} != entry count {len(universe)}; "
        "duplicates exist somewhere in COMPONENT_MANIFEST ∪ INFRA_PUBLISHED_PORTS."
    )

    # (2) ``(owner, port)`` rows are themselves unique.
    row_counts = Counter(universe)
    dup_rows = {row: n for row, n in row_counts.items() if n > 1}
    assert not dup_rows, (
        f"Duplicate (owner, port) rows in the manifest+infra universe: {dup_rows}"
    )


# ---------------------------------------------------------------------------
# Cross-check against ``infra/docker-compose.yml``
# ---------------------------------------------------------------------------


# Compose port mapping shapes accepted by docker compose:
#   - "H:C"
#   - "H:C/proto"
#   - "127.0.0.1:H:C"
#   - {published: H, target: C}
# Property 4.x covers full Compose structure; here we only need the
# published (host) side, so a small parser keyed on the textual form
# plus the long-form mapping suffices.
_PORT_RE = re.compile(
    r"""
    ^
    (?:[\w\.\-:]+:)?      # optional bind address (e.g. 127.0.0.1:)
    (?P<host>\d+)         # host port
    :
    (?P<container>\d+)    # container port
    (?:/[a-zA-Z]+)?       # optional /tcp /udp suffix
    $
    """,
    re.VERBOSE,
)


def _published_host_port(entry: object) -> int | None:
    """Return the host port from a Compose ``ports:`` list entry.

    Supports both the short ``"H:C"`` string form and the long
    mapping form ``{published: H, target: C}``. Returns ``None`` for
    container-only ``expose:``-style numerics that Compose still
    accepts under ``ports:`` (where the host port is ephemeral).
    """

    if isinstance(entry, str):
        match = _PORT_RE.match(entry.strip())
        if match is None:
            return None
        return int(match.group("host"))
    if isinstance(entry, dict):
        published = entry.get("published")
        if published is None:
            return None
        # Compose accepts strings or ints for ``published``.
        return int(published)
    return None


def test_compose_published_ports_match_universe() -> None:
    """Property 3 (Compose cross-check) — compose ports ⊆ manifest+infra.

    Validates: Requirements 2.1, 2.2, 2.3, 4.1, 4.7, 17.3.

    Parse ``infra/docker-compose.yml`` and assert:

    1. There are no duplicate host ports across the compose file
       itself (defence in depth — a YAML edit that breaks this would
       also break ``docker compose up`` at runtime).
    2. Every ``services[*].ports`` entry that publishes a host port
       maps to exactly one owner in the canonical universe (Component
       manifest + infra-published ports).
    3. For every Component in ``EXPECTED_COMPOSE_SERVICES`` that the
       manifest publishes, the host port declared in compose matches
       the manifest. (Components like ``streamlit-app`` whose Compose
       service is intentionally omitted from the base stack are
       excluded from this check, mirroring the design's
       ``EXPECTED_COMPOSE_SERVICES`` set.)
    4. Every infra-published port in ``INFRA_PUBLISHED_PORTS`` is
       actually published by its Compose service with the expected
       host port.

    The compose service ``admin-dashboard-ui`` corresponds to the
    Component ``admin-dashboard`` (design §"Compose Bağımlılık DAG'ı"),
    so the owner-mapping uses the manifest's port even though the
    service name differs.
    """

    compose_path = WORKSPACE_ROOT / "infra" / "docker-compose.yml"
    if not compose_path.is_file():
        pytest.skip(f"{compose_path} missing; skipping compose cross-check.")

    with compose_path.open("r", encoding="utf-8") as fh:
        compose = yaml.safe_load(fh) or {}
    services = compose.get("services") or {}
    assert isinstance(services, dict), (
        f"{compose_path}: top-level 'services' must be a mapping, "
        f"got {type(services).__name__}"
    )

    # Collect (compose_service, host_port) for every published port.
    compose_ports: list[tuple[str, int]] = []
    for svc_name, svc_def in services.items():
        if not isinstance(svc_def, dict):
            continue
        ports_block = svc_def.get("ports") or []
        if not isinstance(ports_block, list):
            continue
        for entry in ports_block:
            host_port = _published_host_port(entry)
            if host_port is not None:
                compose_ports.append((svc_name, host_port))

    # (1) no duplicate host ports inside the compose file itself.
    host_ports_only = [hp for _svc, hp in compose_ports]
    dup = {p: n for p, n in Counter(host_ports_only).items() if n > 1}
    assert not dup, (
        f"{compose_path}: duplicate host ports across services: {dup}; "
        f"published ports = {sorted(compose_ports, key=lambda t: t[1])}"
    )

    # Build the expected (host_port → owner_set) map from manifest +
    # infra. ``admin-dashboard`` Component is published as Compose
    # service ``admin-dashboard-ui``; ``streamlit-app`` is published as
    # ``streamlit-ui`` per foundation task 10.1.
    component_to_compose = {
        "admin-dashboard": "admin-dashboard-ui",
        "streamlit-app": "streamlit-ui",
    }
    expected_owners: dict[int, set[str]] = {}
    expected_compose_for_component: dict[str, int] = {}
    for component in COMPONENT_MANIFEST:
        if component.host_port is None:
            continue
        compose_name = component_to_compose.get(component.name, component.name)
        expected_owners.setdefault(component.host_port, set()).update(
            {component.name, compose_name}
        )
        # Only enforce compose-side assertion when the Component's
        # Compose service is part of the base stack.
        if compose_name in EXPECTED_COMPOSE_SERVICES:
            expected_compose_for_component[compose_name] = component.host_port
    for infra_name, ports in INFRA_PUBLISHED_PORTS.items():
        for port in ports:
            expected_owners.setdefault(port, set()).add(infra_name)

    # (2) each compose (service, port) pair has a known owner.
    for svc_name, host_port in compose_ports:
        owners = expected_owners.get(host_port, set())
        assert svc_name in owners, (
            f"{compose_path}: service {svc_name!r} publishes host port "
            f"{host_port}, but expected owner(s) for that port are "
            f"{sorted(owners) or '∅'}"
        )

    # (3) every Component-backed Compose service publishes the
    # manifest-declared host port.
    compose_ports_by_service: dict[str, set[int]] = {}
    for svc_name, host_port in compose_ports:
        compose_ports_by_service.setdefault(svc_name, set()).add(host_port)
    for compose_name, expected_port in expected_compose_for_component.items():
        actual = compose_ports_by_service.get(compose_name, set())
        assert expected_port in actual, (
            f"{compose_path}: service {compose_name!r} must publish host "
            f"port {expected_port} (per manifest), got {sorted(actual)}."
        )

    # (4) every infra-published port is wired in compose.
    for infra_name, ports in INFRA_PUBLISHED_PORTS.items():
        actual = compose_ports_by_service.get(infra_name, set())
        for port in ports:
            assert port in actual, (
                f"{compose_path}: infra service {infra_name!r} must "
                f"publish host port {port}, got {sorted(actual)}."
            )


# ===========================================================================
# Property 1 (platform-mimari-foundation): Servis topolojisi ve compose-manifest
# shape tutarlılığı — port uniqueness across the 10-entry topology.
#
# **Validates: Requirements 1.1, 1.10, 2.1, 2.3, 2.5, 2.7, 2.9, 9.4, 9.9**
#
# This block extends the existing pair-wise / universe / Compose
# uniqueness checks with two foundation-specific invariants:
#
# 1. Every foundation Compose service that publishes host ports
#    publishes them on globally unique values — re-asserted across
#    the dynamically-loaded Compose document so newly-added services
#    cannot quietly reuse a port already taken by an existing one.
#
# 2. Every foundation service of ``kind=sidecar`` MUST publish
#    **zero** host ports (mirrors Property 1 (e) in
#    ``test_compose_structure.py``; reproduced here so the port-
#    centric file is self-contained).
# ===========================================================================


# ---------------------------------------------------------------------------
# Foundation manifest discovery (kept lightweight — no extra deps).
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402  (deferred import keeps top tidy)

#: Workspace-relative path to ``services.manifest.json``. Loaded once.
_FOUNDATION_MANIFEST_PATH: Path = (
    WORKSPACE_ROOT / "config" / "services.manifest.json"
)


def _load_foundation_manifest_entries() -> tuple[dict[str, object], ...]:
    """Parse ``config/services.manifest.json`` once and return entries.

    The single read at module import time keeps each Hypothesis
    example's cost limited to the property check itself.
    """

    assert _FOUNDATION_MANIFEST_PATH.is_file(), (
        f"foundation manifest missing at "
        f"{_FOUNDATION_MANIFEST_PATH.relative_to(WORKSPACE_ROOT)}"
    )
    with _FOUNDATION_MANIFEST_PATH.open("r", encoding="utf-8") as fh:
        document = _json.load(fh)
    services = document.get("services") or []
    return tuple(services)


_FOUNDATION_MANIFEST_ENTRIES_PORT: tuple[dict[str, object], ...] = (
    _load_foundation_manifest_entries()
)


# ---------------------------------------------------------------------------
# Property 1 — All published Compose host ports are globally unique
# ---------------------------------------------------------------------------


def test_foundation_compose_host_ports_globally_unique() -> None:
    """Property 1 — every host port in Compose appears at most once.

    Validates: Requirements 2.7, 9.9.

    Re-reads ``infra/docker-compose.yml`` and asserts that the
    multiset of ``(service, host_port)`` pairs has no duplicate
    ``host_port`` value across services. This is a stronger,
    Compose-document-driven version of
    :func:`test_component_plus_infra_ports_are_globally_unique`,
    which models the universe from the conftest manifest. Together
    the two tests ensure that whichever side a regression lands on,
    it gets caught.
    """

    compose_path = WORKSPACE_ROOT / "infra" / "docker-compose.yml"
    if not compose_path.is_file():
        pytest.skip(f"{compose_path} missing; skipping foundation cross-check.")

    with compose_path.open("r", encoding="utf-8") as fh:
        compose = yaml.safe_load(fh) or {}
    services = compose.get("services") or {}

    pairs: list[tuple[str, int]] = []
    for svc_name, svc_def in services.items():
        if not isinstance(svc_def, dict):
            continue
        ports_block = svc_def.get("ports") or []
        if not isinstance(ports_block, list):
            continue
        for entry in ports_block:
            host_port = _published_host_port(entry)
            if host_port is not None:
                pairs.append((svc_name, host_port))

    counts = Counter(hp for _svc, hp in pairs)
    duplicates = {p: n for p, n in counts.items() if n > 1}
    assert not duplicates, (
        "Compose host-port collision detected (Requirement 2.7, "
        f"Property 1): {duplicates}; full pairs = "
        f"{sorted(pairs, key=lambda t: t[1])}"
    )


# ---------------------------------------------------------------------------
# Property 1 — Sidecar entries publish zero host ports
# ---------------------------------------------------------------------------


def _foundation_sidecar_compose_names() -> tuple[str, ...]:
    """Compose service names of every manifest entry with ``kind=sidecar``."""

    return tuple(
        str(entry["compose_service_name"])
        for entry in _FOUNDATION_MANIFEST_ENTRIES_PORT
        if entry.get("kind") == "sidecar"
    )


_FOUNDATION_SIDECAR_PORT_NAMES: tuple[str, ...] = _foundation_sidecar_compose_names()


@pytest.mark.skipif(
    not _FOUNDATION_SIDECAR_PORT_NAMES,
    reason="manifest declares no sidecar entries; Property 1 vacuous",
)
@pytest.mark.parametrize(
    "sidecar_name",
    _FOUNDATION_SIDECAR_PORT_NAMES,
    ids=list(_FOUNDATION_SIDECAR_PORT_NAMES),
)
def test_foundation_sidecar_publishes_no_host_ports(sidecar_name: str) -> None:
    """Property 1 — sidecar Compose services publish no host ports.

    Validates: Requirements 2.9, 9.9.

    Mirrors the assertion in ``test_compose_structure.py``'s
    Property 1 (e), reproduced here because the port-uniqueness
    invariant is logically port-centric: a sidecar that suddenly
    started publishing ports would also be a port-allocation bug.
    """

    compose_path = WORKSPACE_ROOT / "infra" / "docker-compose.yml"
    if not compose_path.is_file():
        pytest.skip(f"{compose_path} missing; skipping sidecar port check.")

    with compose_path.open("r", encoding="utf-8") as fh:
        compose = yaml.safe_load(fh) or {}
    services = compose.get("services") or {}
    service = services.get(sidecar_name)
    assert service is not None, (
        f"sidecar {sidecar_name!r}: missing from docker-compose.yml "
        f"(Requirement 2.9, Property 1)"
    )

    ports = service.get("ports")
    if ports is None:
        return
    assert isinstance(ports, list), (
        f"sidecar {sidecar_name!r}: 'ports:' must be a list when "
        f"present; got {type(ports).__name__}"
    )
    # Count actual host-port publications (long form ``{published: H}``
    # and short form ``"H:C"`` both count). An entry without a host
    # port (e.g. a bare ``"4096"`` ephemeral binding) is rejected too:
    # sidecars MUST use ``expose:`` instead.
    published = [_published_host_port(p) for p in ports]
    nonempty = [p for p in published if p is not None]
    assert not nonempty and not ports, (
        f"sidecar {sidecar_name!r}: MUST NOT declare 'ports:' — "
        f"sidecars use Compose-internal ``expose:`` only "
        f"(Requirement 2.9, Property 1); got ports={ports!r}"
    )
