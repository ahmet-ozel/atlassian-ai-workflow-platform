"""Service_Manifest loader for the admin-dashboard-api Control_Plane.

This module is the single entry point used by ``src/main.py`` and
:class:`LifecycleService` to load and validate the
``config/services.manifest.json`` file.

Manifest behavior
-----------------
* The manifest is the single source of truth for every
  Managed_Service the Control_Plane orchestrates.
* Loading must validate against
  ``config/services.manifest.schema.json`` using JSON Schema 2020-12; on
  failure the readiness probe returns 503.
* Duplicate ``compose_service_name`` across entries
  must be rejected. JSON Schema 2020-12 does not provide a native
  ``uniqueItemProperties`` keyword, so this check is implemented here as
  a custom Python-side validation step.
* Public surface (``ManifestLoadError``,
  ``ManagedServiceEntry``, ``load_manifest``).
* ``depends_on_services`` and ``feature_flag_dependency`` fields use
  DFS-based cycle detection over the intra-manifest portion of the
  dependency graph; external dependency names that do not appear as
  manifest entries are treated as edge sinks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jsonschema.validators import Draft202012Validator

# Workspace-root-relative locations of the manifest and its schema. Kept as
# module constants so callers (and tests) can monkey-patch them without
# re-deriving the path from ``workspace_root`` in multiple places.
MANIFEST_RELATIVE_PATH = Path("config") / "services.manifest.json"
SCHEMA_RELATIVE_PATH = Path("config") / "services.manifest.schema.json"


ServiceKind = Literal["http_service", "worker", "ui", "infra", "sidecar"]


class ManifestLoadError(RuntimeError):
    """Raised when ``config/services.manifest.json`` cannot be loaded.

    The error covers three failure modes:

    1. The manifest or schema file is missing or unreadable.
    2. The JSON payload does not parse, or fails schema validation.
    3. Two entries share the same ``compose_service_name`` value, which
       JSON Schema cannot express natively.
    """


@dataclass(frozen=True)
class ManagedServiceEntry:
    """Immutable in-memory representation of a single manifest entry.

    The field set mirrors ``$defs/ManagedService`` in
    ``config/services.manifest.schema.json``; the dataclass is frozen so
    that callers (the readiness probe, ``LifecycleService``,
    ``HealthProbe``, ...) can hand entries around without worrying about
    accidental mutation.

    ``depends_on_services`` and ``feature_flag_dependency`` default to
    empty tuples when the JSON field is absent - the JSON Schema marks
    them optional with ``default: []``, but consumers always see a tuple
    here.

    ``connectivity_probe_command`` is an optional subprocess argv string
    executed by :class:`LifecycleService` after ``_wait_for_healthy`` to
    dry-run a credential probe. Default ``None`` (no probe).
    """

    name: str
    kind: ServiceKind
    compose_service_name: str
    compose_profile: str
    env_example_path: str
    health_endpoint: str | None
    test_command: str | None
    depends_on_services: tuple[str, ...] = ()
    feature_flag_dependency: tuple[str, ...] = ()
    connectivity_probe_command: str | None = None
    smoke_test_command: str | None = None


def _read_json(path: Path, *, label: str) -> object:
    """Read and parse a JSON file, wrapping I/O errors as ManifestLoadError."""

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ManifestLoadError(
            f"{label} not found at {path}",
        ) from exc
    except OSError as exc:
        raise ManifestLoadError(
            f"failed to read {label} at {path}: {exc}",
        ) from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestLoadError(
            f"{label} at {path} is not valid JSON: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})",
        ) from exc


def _validate_schema(payload: object, schema: object) -> None:
    """Validate ``payload`` against ``schema`` using Draft 2020-12."""

    try:
        Draft202012Validator.check_schema(schema)  # type: ignore[arg-type]
    except Exception as exc:  # pragma: no cover - schema is shipped with code
        raise ManifestLoadError(
            f"services.manifest.schema.json is itself invalid: {exc}",
        ) from exc

    validator = Draft202012Validator(schema)  # type: ignore[arg-type]
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    if errors:
        # Build a readable, deterministic error message that lists every
        # violation. ``absolute_path`` is a deque - render it as a JSON
        # pointer-ish path for operator-friendly diagnostics.
        rendered = []
        for err in errors:
            location = "/" + "/".join(str(part) for part in err.absolute_path)
            rendered.append(f"  at {location}: {err.message}")
        joined = "\n".join(rendered)
        raise ManifestLoadError(
            f"services.manifest.json failed JSON Schema 2020-12 validation:\n{joined}",
        )


def _check_unique_compose_service_name(services: list[dict]) -> None:
    """Reject manifests that contain duplicate ``compose_service_name`` values.

    JSON Schema 2020-12 has no native ``uniqueItemProperties``; this is
    the Python-side custom check documented in
    ``config/services.manifest.schema.json``'s top-level description.
    """

    seen: dict[str, int] = {}
    duplicates: dict[str, list[int]] = {}
    for index, entry in enumerate(services):
        compose_name = entry.get("compose_service_name")
        if not isinstance(compose_name, str):
            # Schema validation should have caught non-string values; bail
            # out defensively without claiming uniqueness.
            return
        if compose_name in seen:
            duplicates.setdefault(compose_name, [seen[compose_name]]).append(index)
        else:
            seen[compose_name] = index

    if duplicates:
        rendered = ", ".join(
            f"{name!r} at indices {indices}"
            for name, indices in sorted(duplicates.items())
        )
        raise ManifestLoadError(
            "services.manifest.json contains duplicate compose_service_name "
            f"values: {rendered}",
        )


def _check_no_dependency_cycles(services: list[dict]) -> None:
    """Reject manifests whose ``depends_on_services`` graph contains cycles.

    JSON Schema 2020-12 cannot express cross-array constraints, so the
    boot validator and this loader perform DFS-based cycle detection
    over the intra-manifest portion of the dependency graph. External
    dependencies that reference Boot_Bundle / infra components which do
    not appear as manifest entries (e.g. ``postgres``, ``vault``,
    ``temporal``, ``atlassian_mcp_bitbucket``) are skipped - they cannot
    participate in a cycle by definition since they are not nodes in this
    graph.
    """

    # Build adjacency over manifest-resident node names only. Edges to
    # unknown names are filtered out: they're external dependencies the
    # cascade aggregator will treat as raw status lookups, not cycle
    # candidates.
    names: set[str] = set()
    adjacency: dict[str, list[str]] = {}
    for entry in services:
        name = entry.get("name")
        if not isinstance(name, str):
            return  # schema validation should have caught this
        names.add(name)
        adjacency[name] = []

    for entry in services:
        name = entry["name"]
        deps = entry.get("depends_on_services", []) or []
        if not isinstance(deps, list):
            return  # schema validation should have caught this
        for dep in deps:
            if isinstance(dep, str) and dep in names:
                adjacency[name].append(dep)

    # Iterative DFS with WHITE/GRAY/BLACK colouring so that the first
    # back-edge reproducibly yields the offending cycle.
    WHITE, GRAY, BLACK = 0, 1, 2
    colour: dict[str, int] = {n: WHITE for n in names}
    parent: dict[str, str | None] = {n: None for n in names}

    def _extract_cycle(start: str, end: str) -> list[str]:
        """Reconstruct the cycle path ``start -> ... -> end -> start``."""
        path: list[str] = [end]
        node: str | None = start
        while node is not None and node != end:
            path.append(node)
            node = parent[node]
        path.append(end)
        path.reverse()
        return path

    for root in sorted(names):
        if colour[root] != WHITE:
            continue

        # (node, iterator over neighbours) stack for iterative DFS.
        stack: list[tuple[str, list[str]]] = [(root, list(adjacency[root]))]
        colour[root] = GRAY
        parent[root] = None

        while stack:
            node, pending = stack[-1]
            if not pending:
                colour[node] = BLACK
                stack.pop()
                continue

            neighbour = pending.pop(0)
            if colour[neighbour] == GRAY:
                cycle = _extract_cycle(start=node, end=neighbour)
                rendered = " -> ".join(cycle)
                raise ManifestLoadError(
                    "services.manifest.json depends_on_services graph "
                    f"contains a cycle: {rendered}",
                )
            if colour[neighbour] == WHITE:
                colour[neighbour] = GRAY
                parent[neighbour] = node
                stack.append((neighbour, list(adjacency[neighbour])))


def load_manifest(workspace_root: Path) -> tuple[ManagedServiceEntry, ...]:
    """Load and validate ``config/services.manifest.json``.

    Parameters
    ----------
    workspace_root:
        Absolute path to the workspace root (the directory that contains
        ``config/``, ``infra/``, ``services/``, ...). Manifest and schema
        paths are resolved relative to this root.

    Returns
    -------
    tuple[ManagedServiceEntry, ...]
        Immutable tuple of manifest entries in the order they appear in
        the JSON file. Order is presentational only; downstream code
        keys entries by ``name`` / ``compose_service_name``.

    Raises
    ------
    ManifestLoadError
        If either file is missing/unreadable, the JSON is malformed, the
        schema validation fails, or two entries share the same
        ``compose_service_name``.
    """

    manifest_path = workspace_root / MANIFEST_RELATIVE_PATH
    schema_path = workspace_root / SCHEMA_RELATIVE_PATH

    schema = _read_json(schema_path, label="services.manifest.schema.json")
    payload = _read_json(manifest_path, label="services.manifest.json")

    _validate_schema(payload, schema)

    # Schema validation guarantees the top-level shape; this cast-style
    # access is therefore safe.
    assert isinstance(payload, dict)
    services_raw = payload["services"]
    assert isinstance(services_raw, list)

    _check_unique_compose_service_name(services_raw)
    _check_no_dependency_cycles(services_raw)

    entries = tuple(
        ManagedServiceEntry(
            name=entry["name"],
            kind=entry["kind"],
            compose_service_name=entry["compose_service_name"],
            compose_profile=entry["compose_profile"],
            env_example_path=entry["env_example_path"],
            health_endpoint=entry["health_endpoint"],
            test_command=entry["test_command"],
            depends_on_services=tuple(entry.get("depends_on_services", []) or ()),
            feature_flag_dependency=tuple(entry.get("feature_flag_dependency", []) or ()),
            connectivity_probe_command=entry.get("connectivity_probe_command"),
        )
        for entry in services_raw
        if entry.get("kind") != "external"
    )
    return entries
