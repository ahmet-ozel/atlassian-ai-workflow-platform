"""Sanity check that config/services.manifest.schema.json behaves as expected.

The checker also verifies that config/services.manifest.json is schema-valid
and contains the baseline service topology used by the platform.

* ``depends_on_services`` (array of string, default ``[]``) - names of
  services this entry depends on for cascade healthcheck and auto-start
  orchestration. May reference other manifest entries by
  ``name`` OR external Boot_Bundle / infra components (postgres, vault,
  temporal, atlassian_mcp_bitbucket) that do not appear as manifest entries.
* ``feature_flag_dependency`` (array of string, default ``[]``) - names
  of feature flags whose enabled state must be ``true`` before the
  Control_Plane will start the service.
* Cycle detection (DFS) over the intra-manifest portion of the
  ``depends_on_services`` graph, since JSON Schema 2020-12 cannot
  express cross-array constraints. External dependency names that are
  not manifest entries are treated as edge sinks and skipped.

Exits non-zero on the first surprise. Not part of the manifest loader
(that lives in services/admin-dashboard-api/src/manifest.py) -- this is
just a smoke test to validate the schema file and manifest contents.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
schema = json.loads((ROOT / "config" / "services.manifest.schema.json").read_text("utf-8"))

# Schema must itself be a valid Draft 2020-12 schema.
Draft202012Validator.check_schema(schema)
v = Draft202012Validator(schema)

# Baseline service topology. The manifest is allowed to carry additional
# entries, but every name in this set MUST be present.
FOUNDATION_REQUIRED_NAMES: frozenset[str] = frozenset(
    {
        "atlassian-mcp",
        "firecrawl",
        "automation-service",
        "assistant-service",
        "agent-runner-worker",
        "execution-runner-worker",
        "streamlit-ui",
        "opencode-sidecar",
        "admin-dashboard-api",
        "admin-dashboard-ui",
    }
)

# Each foundation entry has a fixed kind.
FOUNDATION_REQUIRED_KIND: dict[str, str] = {
    "atlassian-mcp": "infra",
    "firecrawl": "infra",
    "automation-service": "http_service",
    "assistant-service": "http_service",
    "admin-dashboard-api": "http_service",
    "agent-runner-worker": "worker",
    "execution-runner-worker": "worker",
    "opencode-sidecar": "sidecar",
    "streamlit-ui": "ui",
    "admin-dashboard-ui": "ui",
}


def assert_valid(doc: dict, label: str) -> None:
    errs = sorted(v.iter_errors(doc), key=lambda e: e.path)
    if errs:
        print(f"FAIL valid case [{label}]:")
        for e in errs:
            print(f"  {list(e.path)}: {e.message}")
        sys.exit(1)
    print(f"OK valid   [{label}]")


def assert_invalid(doc: dict, label: str) -> None:
    errs = list(v.iter_errors(doc))
    if not errs:
        print(f"FAIL invalid case [{label}] was accepted")
        sys.exit(1)
    print(f"OK invalid [{label}] -> {errs[0].message[:80]}")


# Valid: HTTP service with health endpoint and test command.
assert_valid(
    {
        "version": 1,
        "services": [
            {
                "name": "automation-service",
                "kind": "http_service",
                "compose_service_name": "automation-service",
                "compose_profile": "automation-service",
                "env_example_path": "services/automation-service/.env.example",
                "health_endpoint": "/healthz",
                "test_command": "docker compose -f infra/docker-compose.yml exec automation-service pytest tests/integration/ -v",
            }
        ],
    },
    "http_service with /healthz",
)

# Valid: worker with health_endpoint null and test_command null.
assert_valid(
    {
        "version": 1,
        "services": [
            {
                "name": "agent-runner-worker",
                "kind": "worker",
                "compose_service_name": "agent-runner-worker",
                "compose_profile": "agent-runner-worker",
                "env_example_path": "workers/agent-runner-worker/.env.example",
                "health_endpoint": None,
                "test_command": None,
            }
        ],
    },
    "worker with nulls",
)

# Valid: empty services array.
assert_valid({"version": 1, "services": []}, "empty services")

# Valid: sidecar kind with health_endpoint null (foundation: opencode-sidecar).
assert_valid(
    {
        "version": 1,
        "services": [
            {
                "name": "opencode-sidecar",
                "kind": "sidecar",
                "compose_service_name": "opencode-sidecar",
                "compose_profile": "opencode-sidecar",
                "env_example_path": "services/opencode-sidecar/.env.example",
                "health_endpoint": None,
                "test_command": None,
            }
        ],
    },
    "sidecar with null health",
)

# Valid: ui kind with health_endpoint null (foundation: streamlit-ui, admin-dashboard-ui).
assert_valid(
    {
        "version": 1,
        "services": [
            {
                "name": "streamlit-ui",
                "kind": "ui",
                "compose_service_name": "streamlit-ui",
                "compose_profile": "streamlit-ui",
                "env_example_path": "ui/streamlit-app/.env.example",
                "health_endpoint": None,
                "test_command": None,
            }
        ],
    },
    "ui with null health",
)

# Valid: depends_on_services and feature_flag_dependency populated.
assert_valid(
    {
        "version": 1,
        "services": [
            {
                "name": "automation-service",
                "kind": "http_service",
                "compose_service_name": "automation-service",
                "compose_profile": "automation-service",
                "env_example_path": "services/automation-service/.env.example",
                "health_endpoint": "/healthz",
                "test_command": None,
                "depends_on_services": ["postgres", "vault", "temporal", "atlassian_mcp_bitbucket"],
                "feature_flag_dependency": ["BUDGET_CAPS_ENFORCED"],
            }
        ],
    },
    "depends_on_services + feature_flag_dependency populated",
)

# Valid: depends_on_services / feature_flag_dependency empty arrays.
assert_valid(
    {
        "version": 1,
        "services": [
            {
                "name": "ok",
                "kind": "infra",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": None,
                "test_command": None,
                "depends_on_services": [],
                "feature_flag_dependency": [],
            }
        ],
    },
    "dependency arrays empty",
)

# Valid: depends_on_services / feature_flag_dependency omitted (default []).
assert_valid(
    {
        "version": 1,
        "services": [
            {
                "name": "ok",
                "kind": "infra",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": None,
                "test_command": None,
            }
        ],
    },
    "dependency arrays omitted (default [])",
)

# Invalid: version != 1.
assert_invalid({"version": 2, "services": []}, "version=2 rejected")

# Invalid: top-level extra property.
assert_invalid(
    {"version": 1, "services": [], "extra": True}, "top-level additionalProperties"
)

# Invalid: name with uppercase.
assert_invalid(
    {
        "version": 1,
        "services": [
            {
                "name": "BadName",
                "kind": "infra",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": None,
                "test_command": None,
            }
        ],
    },
    "name uppercase",
)

# Invalid: name too short (<2 chars).
assert_invalid(
    {
        "version": 1,
        "services": [
            {
                "name": "a",
                "kind": "infra",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": None,
                "test_command": None,
            }
        ],
    },
    "name single char",
)

# Invalid: name too long (>41 chars).
assert_invalid(
    {
        "version": 1,
        "services": [
            {
                "name": "a" + "b" * 41,
                "kind": "infra",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": None,
                "test_command": None,
            }
        ],
    },
    "name 42 chars",
)

# Invalid: kind not in enum.
assert_invalid(
    {
        "version": 1,
        "services": [
            {
                "name": "ok",
                "kind": "daemon",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": None,
                "test_command": None,
            }
        ],
    },
    "kind=daemon",
)

# Invalid: health_endpoint string without leading slash.
assert_invalid(
    {
        "version": 1,
        "services": [
            {
                "name": "ok",
                "kind": "http_service",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": "healthz",
                "test_command": None,
            }
        ],
    },
    "health_endpoint missing slash",
)

# Invalid: test_command empty string.
assert_invalid(
    {
        "version": 1,
        "services": [
            {
                "name": "ok",
                "kind": "http_service",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": "/healthz",
                "test_command": "",
            }
        ],
    },
    "test_command empty string",
)

# Invalid: missing required field.
assert_invalid(
    {
        "version": 1,
        "services": [
            {
                "name": "ok",
                "kind": "http_service",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": "/healthz",
                # test_command missing
            }
        ],
    },
    "missing test_command",
)

# Invalid: ManagedService extra property.
assert_invalid(
    {
        "version": 1,
        "services": [
            {
                "name": "ok",
                "kind": "http_service",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": "/healthz",
                "test_command": None,
                "extra": "nope",
            }
        ],
    },
    "service additionalProperties",
)

# Invalid: depends_on_services not an array.
assert_invalid(
    {
        "version": 1,
        "services": [
            {
                "name": "ok",
                "kind": "http_service",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": "/healthz",
                "test_command": None,
                "depends_on_services": "postgres",
            }
        ],
    },
    "depends_on_services string instead of array",
)

# Invalid: depends_on_services contains a non-string item.
assert_invalid(
    {
        "version": 1,
        "services": [
            {
                "name": "ok",
                "kind": "http_service",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": "/healthz",
                "test_command": None,
                "depends_on_services": ["postgres", 42],
            }
        ],
    },
    "depends_on_services non-string element",
)

# Invalid: depends_on_services contains an entry with uppercase or invalid chars.
assert_invalid(
    {
        "version": 1,
        "services": [
            {
                "name": "ok",
                "kind": "http_service",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": "/healthz",
                "test_command": None,
                "depends_on_services": ["Postgres"],
            }
        ],
    },
    "depends_on_services uppercase entry",
)

# Invalid: depends_on_services contains duplicate entries.
assert_invalid(
    {
        "version": 1,
        "services": [
            {
                "name": "ok",
                "kind": "http_service",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": "/healthz",
                "test_command": None,
                "depends_on_services": ["postgres", "postgres"],
            }
        ],
    },
    "depends_on_services duplicate entries",
)

# Invalid: feature_flag_dependency contains lowercase entry.
assert_invalid(
    {
        "version": 1,
        "services": [
            {
                "name": "ok",
                "kind": "http_service",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": "/healthz",
                "test_command": None,
                "feature_flag_dependency": ["budget_caps_enforced"],
            }
        ],
    },
    "feature_flag_dependency lowercase entry",
)

# Invalid: feature_flag_dependency duplicates.
assert_invalid(
    {
        "version": 1,
        "services": [
            {
                "name": "ok",
                "kind": "http_service",
                "compose_service_name": "x",
                "compose_profile": "x",
                "env_example_path": "x/.env.example",
                "health_endpoint": "/healthz",
                "test_command": None,
                "feature_flag_dependency": ["FEATURE_X", "FEATURE_X"],
            }
        ],
    },
    "feature_flag_dependency duplicate entries",
)

print("\nAll schema cases passed.")

# ---------------------------------------------------------------------------
# Validate the actual config/services.manifest.json file itself.
# ---------------------------------------------------------------------------
manifest_path = ROOT / "config" / "services.manifest.json"
manifest = json.loads(manifest_path.read_text("utf-8"))

# 1) Schema validation.
manifest_errs = sorted(v.iter_errors(manifest), key=lambda e: e.path)
if manifest_errs:
    print("\nFAIL: config/services.manifest.json does not match the schema:")
    for e in manifest_errs:
        print(f"  {list(e.path)}: {e.message}")
    sys.exit(1)
print("\nOK   config/services.manifest.json is schema-valid.")

# 2) Foundation 10-entry topology presence.
manifest_names = {entry["name"] for entry in manifest["services"]}
missing = FOUNDATION_REQUIRED_NAMES - manifest_names
if missing:
    print(
        "\nFAIL: config/services.manifest.json is missing foundation "
        f"required entries: {sorted(missing)}"
    )
    sys.exit(1)
print(
    f"OK   foundation 10-entry topology present "
    f"({len(FOUNDATION_REQUIRED_NAMES)} required, "
    f"{len(manifest_names)} total entries in manifest)."
)

# 3) Foundation kind invariants.
by_name = {entry["name"]: entry for entry in manifest["services"]}
kind_errs: list[str] = []
for name, expected_kind in FOUNDATION_REQUIRED_KIND.items():
    actual = by_name[name]["kind"]
    if actual != expected_kind:
        kind_errs.append(f"  {name}: expected kind={expected_kind}, got {actual}")
if kind_errs:
    print("\nFAIL: foundation kind invariants violated:")
    for line in kind_errs:
        print(line)
    sys.exit(1)
print("OK   foundation kind invariants hold.")

# 4) Required field consistency: each foundation entry's compose_profile
# SHALL contain its compose_service_name.
profile_errs: list[str] = []
for name in FOUNDATION_REQUIRED_NAMES:
    entry = by_name[name]
    if entry["compose_service_name"] not in entry["compose_profile"]:
        profile_errs.append(
            f"  {name}: compose_profile={entry['compose_profile']!r} does not "
            f"contain compose_service_name={entry['compose_service_name']!r}"
        )
if profile_errs:
    print("\nFAIL: compose_profile/compose_service_name invariant violated:")
    for line in profile_errs:
        print(line)
    sys.exit(1)
print("OK   compose_profile contains compose_service_name for all foundation entries.")

# 5) HTTP-service entries SHALL define a non-null health_endpoint;
# worker/sidecar/ui foundation entries SHALL have health_endpoint=null
# (workers don't open HTTP, sidecars are Compose-internal-only, UIs
# serve via their own framework).
health_errs: list[str] = []
for name in FOUNDATION_REQUIRED_NAMES:
    entry = by_name[name]
    kind = entry["kind"]
    health = entry["health_endpoint"]
    if kind == "http_service" and health is None:
        health_errs.append(f"  {name}: http_service must have non-null health_endpoint")
    elif kind in {"worker", "sidecar", "ui"} and health is not None:
        health_errs.append(
            f"  {name}: {kind} foundation entry must have health_endpoint=null, "
            f"got {health!r}"
        )
if health_errs:
    print("\nFAIL: foundation health_endpoint invariant violated:")
    for line in health_errs:
        print(line)
    sys.exit(1)
print("OK   foundation health_endpoint invariants hold.")

# ---------------------------------------------------------------------------
# 6) Cycle detection (DFS) over depends_on_services edges, restricted to
# intra-manifest references. External dependency names that are not
# manifest entry names are skipped - they cannot form a cycle since
# they are not nodes in the graph.
# ---------------------------------------------------------------------------


def _detect_dependency_cycle(services: list[dict]) -> list[str] | None:
    """Return the first cycle path found, or None if the graph is acyclic.

    Uses iterative DFS with WHITE/GRAY/BLACK colouring. Roots are
    iterated in sorted order so the diagnostic output is deterministic.
    """

    names = {entry["name"] for entry in services}
    adjacency: dict[str, list[str]] = {name: [] for name in names}
    for entry in services:
        for dep in entry.get("depends_on_services", []) or []:
            if dep in names:
                adjacency[entry["name"]].append(dep)

    WHITE, GRAY, BLACK = 0, 1, 2
    colour = {n: WHITE for n in names}
    parent: dict[str, str | None] = {n: None for n in names}

    def _extract_cycle(start: str, end: str) -> list[str]:
        path = [end]
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
                return _extract_cycle(start=node, end=neighbour)
            if colour[neighbour] == WHITE:
                colour[neighbour] = GRAY
                parent[neighbour] = node
                stack.append((neighbour, list(adjacency[neighbour])))
    return None


# Self-test the cycle detector with synthetic fixtures before applying it
# to the real manifest, so a regression in the algorithm fails this
# script loudly rather than silently passing the production check.

# Acyclic chain: a -> b -> c
_acyclic = [
    {"name": "a", "depends_on_services": ["b"]},
    {"name": "b", "depends_on_services": ["c"]},
    {"name": "c", "depends_on_services": []},
]
if _detect_dependency_cycle(_acyclic) is not None:
    print("\nFAIL: cycle detector reported a cycle on an acyclic chain a->b->c")
    sys.exit(1)
print("OK   cycle detector accepts acyclic chain a->b->c.")

# Direct cycle: a -> b -> a
_direct_cycle = [
    {"name": "a", "depends_on_services": ["b"]},
    {"name": "b", "depends_on_services": ["a"]},
]
if _detect_dependency_cycle(_direct_cycle) is None:
    print("\nFAIL: cycle detector missed direct cycle a->b->a")
    sys.exit(1)
print("OK   cycle detector flags direct cycle a->b->a.")

# Indirect cycle: a -> b -> c -> a
_indirect_cycle = [
    {"name": "a", "depends_on_services": ["b"]},
    {"name": "b", "depends_on_services": ["c"]},
    {"name": "c", "depends_on_services": ["a"]},
]
if _detect_dependency_cycle(_indirect_cycle) is None:
    print("\nFAIL: cycle detector missed indirect cycle a->b->c->a")
    sys.exit(1)
print("OK   cycle detector flags indirect cycle a->b->c->a.")

# Self-loop: a -> a
_self_loop = [
    {"name": "a", "depends_on_services": ["a"]},
]
if _detect_dependency_cycle(_self_loop) is None:
    print("\nFAIL: cycle detector missed self-loop a->a")
    sys.exit(1)
print("OK   cycle detector flags self-loop a->a.")

# External-only edges must NOT count as cycles, even if the same name
# appears on both sides (postgres, vault, ... are not manifest entries).
_external_only = [
    {"name": "a", "depends_on_services": ["postgres", "vault", "temporal"]},
    {"name": "b", "depends_on_services": ["postgres"]},
]
if _detect_dependency_cycle(_external_only) is not None:
    print(
        "\nFAIL: cycle detector flagged external-only edges that "
        "reference Boot_Bundle / infra components"
    )
    sys.exit(1)
print("OK   cycle detector ignores external (non-manifest) edges.")

# 7) Apply cycle detection to the real manifest.
real_cycle = _detect_dependency_cycle(manifest["services"])
if real_cycle is not None:
    rendered = " -> ".join(real_cycle)
    print(
        "\nFAIL: config/services.manifest.json depends_on_services graph "
        f"contains a cycle: {rendered}"
    )
    sys.exit(1)
print("OK   config/services.manifest.json depends_on_services graph is acyclic.")

print("\nAll manifest content checks passed.")
