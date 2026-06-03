"""CI test 1.4 — services.manifest schema + manifest content checks.


Scope
-----
This file covers the static, schema-level invariants introduced by
``compliance work`` the implementation (, , ):

1. ``connectivity_probe_command`` is declared as an **optional** field
 with default ``null`` on every entry, accepts both ``string`` and
 ``null`` values, and validates a manifest that omits the field
 entirely .
2. ``depends_on_services`` values populated in
 ``config/services.manifest.json`` cover at least the design's
 minimum set (, :

 - ``automation-service`` → ``{"postgres", "vault", "temporal", "atlassian-mcp"}``
 - ``admin-dashboard-api`` → ``{"postgres", "vault", "automation-service"}``
 - ``agent-runner-worker`` → ``{"temporal", "atlassian-mcp", "vault"}``
 - ``execution-runner-worker`` → ``{"temporal", "vault"}``
3. ``feature_flag_dependency`` for ``task-intake-service`` covers the
 design minimum ``{"FEATURE_FLAG_TASK_INTAKE_ENABLED"}``
 .

These are pure file/JSON-Schema assertions and need no Docker; they run
in the workspace fast lane.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema.validators import Draft202012Validator


# ---------------------------------------------------------------------------
# Constants — design's minimum sets 
# ---------------------------------------------------------------------------

#: Minimum required entries in ``depends_on_services`` service. The
#: manifest may declare additional entries; this test only asserts that
#: the design baseline is present.
MIN_DEPENDS_ON_SERVICES: dict[str, set[str]] = {
    "automation-service": {"postgres", "vault", "temporal", "atlassian-mcp"},
    "admin-dashboard-api": {"postgres", "vault", "automation-service"},
    "agent-runner-worker": {"temporal", "atlassian-mcp", "vault"},
    "execution-runner-worker": {"temporal", "vault"},
}

#: Minimum required entries in ``feature_flag_dependency`` service.
MIN_FEATURE_FLAG_DEPENDENCY: dict[str, set[str]] = {
    "task-intake-service": {"FEATURE_FLAG_TASK_INTAKE_ENABLED"},
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def schema_path(repo_root: Path) -> Path:
    path = repo_root / "config" / "services.manifest.schema.json"
    assert path.is_file(), f"Schema not found at {path}"
    return path


@pytest.fixture(scope="module")
def manifest_path(repo_root: Path) -> Path:
    path = repo_root / "config" / "services.manifest.json"
    assert path.is_file(), f"Manifest not found at {path}"
    return path


@pytest.fixture(scope="module")
def schema(schema_path: Path) -> dict[str, Any]:
    return json.loads(schema_path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest(manifest_path: Path) -> dict[str, Any]:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest_by_name(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["name"]: entry for entry in manifest["services"]}


# ---------------------------------------------------------------------------
# Schema-level: connectivity_probe_command optional
# ---------------------------------------------------------------------------


class TestConnectivityProbeCommandIsOptional:
    """Validates optional ``connectivity_probe_command``."""

    def test_field_declared_in_schema(self, schema: dict[str, Any]) -> None:
        """``connectivity_probe_command`` must be declared on ManagedService."""

        managed = schema["$defs"]["ManagedService"]
        assert "connectivity_probe_command" in managed["properties"], (
            "connectivity_probe_command is missing from "
            "$defs.ManagedService.properties — the implementation contract violated."
        )

    def test_field_is_not_required(self, schema: dict[str, Any]) -> None:
        """The field is opt-in — it must not be in the ``required`` array."""

        managed = schema["$defs"]["ManagedService"]
        required = managed.get("required", [])
        assert "connectivity_probe_command" not in required, (
            "connectivity_probe_command must not appear in "
            "$defs.ManagedService.required (it is optional with default null)."
        )

    def test_field_default_is_null(self, schema: dict[str, Any]) -> None:
        """The schema declares ``default: null`` for the field."""

        managed = schema["$defs"]["ManagedService"]
        prop = managed["properties"]["connectivity_probe_command"]
        assert "default" in prop, (
            "connectivity_probe_command must declare a default value (null)."
        )
        assert prop["default"] is None, (
            f"connectivity_probe_command.default expected null; "
            f"got {prop['default']!r}"
        )

    def test_field_accepts_string_or_null(self, schema: dict[str, Any]) -> None:
        """The field allows non-empty string OR null (no other types)."""

        managed = schema["$defs"]["ManagedService"]
        prop = managed["properties"]["connectivity_probe_command"]
        assert "anyOf" in prop, (
            "connectivity_probe_command should constrain types via anyOf "
            "(string|null); schema shape unexpected."
        )
        type_set = set()
        min_length: int | None = None
        for branch in prop["anyOf"]:
            t = branch.get("type")
            if t is not None:
                type_set.add(t)
            if t == "string" and "minLength" in branch:
                min_length = branch["minLength"]

        assert type_set == {"string", "null"}, (
            f"connectivity_probe_command anyOf types expected {{string,null}}; "
            f"got {type_set!r}"
        )
        # Non-empty string requirement (the design forbids empty argv).
        assert min_length is not None and min_length >= 1, (
            "connectivity_probe_command string branch must enforce minLength≥1 "
            "to forbid empty argv strings."
        )

    def test_validator_accepts_manifest_without_probe_command(
        self, schema: dict[str, Any]
    ) -> None:
        """A manifest entry omitting the field passes JSON Schema validation."""

        validator = Draft202012Validator(schema)
        payload = {
            "version": 1,
            "services": [
                {
                    "name": "infra-only",
                    "kind": "infra",
                    "compose_service_name": "infra-only",
                    "compose_profile": "infra-only",
                    "env_example_path": "services/infra-only/.env.example",
                    "health_endpoint": None,
                    "test_command": None,
                    # connectivity_probe_command intentionally absent.
                },
            ],
        }
        errors = list(validator.iter_errors(payload))
        assert not errors, (
            "Manifest entry without connectivity_probe_command must validate; "
            f"got errors: {[e.message for e in errors]!r}"
        )

    def test_validator_accepts_manifest_with_null_probe_command(
        self, schema: dict[str, Any]
    ) -> None:
        """Explicit ``null`` value is accepted."""

        validator = Draft202012Validator(schema)
        payload = {
            "version": 1,
            "services": [
                {
                    "name": "infra-null",
                    "kind": "infra",
                    "compose_service_name": "infra-null",
                    "compose_profile": "infra-null",
                    "env_example_path": "services/infra-null/.env.example",
                    "health_endpoint": None,
                    "test_command": None,
                    "connectivity_probe_command": None,
                },
            ],
        }
        errors = list(validator.iter_errors(payload))
        assert not errors, (
            "Manifest entry with explicit null connectivity_probe_command must "
            f"validate; got errors: {[e.message for e in errors]!r}"
        )

    def test_validator_accepts_manifest_with_string_probe_command(
        self, schema: dict[str, Any]
    ) -> None:
        """A non-empty string value is accepted."""

        validator = Draft202012Validator(schema)
        payload = {
            "version": 1,
            "services": [
                {
                    "name": "automation-service",
                    "kind": "http_service",
                    "compose_service_name": "automation-service",
                    "compose_profile": "automation-service",
                    "env_example_path": "services/automation-service/.env.example",
                    "health_endpoint": "/healthz",
                    "test_command": "pytest -q",
                    "connectivity_probe_command": "python -m src.scripts.probe_atlassian",
                },
            ],
        }
        errors = list(validator.iter_errors(payload))
        assert not errors, (
            "Manifest entry with string connectivity_probe_command must "
            f"validate; got errors: {[e.message for e in errors]!r}"
        )

    def test_validator_rejects_empty_string_probe_command(
        self, schema: dict[str, Any]
    ) -> None:
        """Empty argv strings are rejected (minLength≥1)."""

        validator = Draft202012Validator(schema)
        payload = {
            "version": 1,
            "services": [
                {
                    "name": "infra-empty",
                    "kind": "infra",
                    "compose_service_name": "infra-empty",
                    "compose_profile": "infra-empty",
                    "env_example_path": "services/infra-empty/.env.example",
                    "health_endpoint": None,
                    "test_command": None,
                    "connectivity_probe_command": "",
                },
            ],
        }
        errors = list(validator.iter_errors(payload))
        assert errors, (
            "Empty connectivity_probe_command must be rejected; validator "
            "accepted it."
        )

    def test_validator_rejects_non_string_non_null_probe_command(
        self, schema: dict[str, Any]
    ) -> None:
        """Numbers, booleans, arrays, objects must all be rejected."""

        validator = Draft202012Validator(schema)
        for bad_value in (42, True, ["a", "b"], {"k": "v"}):
            payload = {
                "version": 1,
                "services": [
                    {
                        "name": "infra-bad",
                        "kind": "infra",
                        "compose_service_name": "infra-bad",
                        "compose_profile": "infra-bad",
                        "env_example_path": "services/infra-bad/.env.example",
                        "health_endpoint": None,
                        "test_command": None,
                        "connectivity_probe_command": bad_value,
                    },
                ],
            }
            errors = list(validator.iter_errors(payload))
            assert errors, (
                f"connectivity_probe_command={bad_value!r} should be rejected"
            )


# ---------------------------------------------------------------------------
# Real manifest content: depends_on_services minimum set
# ---------------------------------------------------------------------------


class TestDependsOnServicesMinimumSet:
    """Validates depends_on_services minimum coverage."""

    @pytest.mark.parametrize(
        "service_name,required_deps",
        list(MIN_DEPENDS_ON_SERVICES.items()),
        ids=list(MIN_DEPENDS_ON_SERVICES.keys()),
    )
    def test_service_covers_required_dependencies(
        self,
        manifest_by_name: dict[str, dict[str, Any]],
        service_name: str,
        required_deps: set[str],
    ) -> None:
        """The named service must declare all required dependencies."""

        assert service_name in manifest_by_name, (
            f"Service {service_name!r} missing from services.manifest.json; "
            "the implementation expected it to be present."
        )
        entry = manifest_by_name[service_name]
        actual = set(entry.get("depends_on_services", []))

        missing = required_deps - actual
        assert not missing, (
            f"Service {service_name!r} is missing required entries in "
            f"depends_on_services: {sorted(missing)!r}. "
            f"Expected at least {sorted(required_deps)!r}; got {sorted(actual)!r}."
        )


# ---------------------------------------------------------------------------
# Real manifest content: feature_flag_dependency minimum set
# ---------------------------------------------------------------------------


class TestFeatureFlagDependencyMinimumSet:
    """Validates feature_flag_dependency baseline."""

    @pytest.mark.parametrize(
        "service_name,required_flags",
        list(MIN_FEATURE_FLAG_DEPENDENCY.items()),
        ids=list(MIN_FEATURE_FLAG_DEPENDENCY.keys()),
    )
    def test_service_covers_required_flags(
        self,
        manifest_by_name: dict[str, dict[str, Any]],
        service_name: str,
        required_flags: set[str],
    ) -> None:
        """The named service must list every required feature flag."""

        assert service_name in manifest_by_name, (
            f"Service {service_name!r} missing from services.manifest.json; "
            "the implementation expected it to be present."
        )
        entry = manifest_by_name[service_name]
        actual = set(entry.get("feature_flag_dependency", []))

        missing = required_flags - actual
        assert not missing, (
            f"Service {service_name!r} is missing required entries in "
            f"feature_flag_dependency: {sorted(missing)!r}. "
            f"Expected at least {sorted(required_flags)!r}; got {sorted(actual)!r}."
        )


# ---------------------------------------------------------------------------
# Sanity: the real manifest still validates against the schema
# ---------------------------------------------------------------------------


def test_real_manifest_validates_against_schema(
    schema: dict[str, Any], manifest: dict[str, Any]
) -> None:
    """The shipped manifest must validate cleanly under the active schema."""

    validator = Draft202012Validator(schema)
    errors = list(validator.iter_errors(manifest))
    assert not errors, (
        "config/services.manifest.json failed JSON Schema validation: "
        f"{[e.message for e in errors]!r}"
    )


def test_schema_is_a_valid_meta_schema(schema: dict[str, Any]) -> None:
    """The schema itself must be a valid Draft 2020-12 schema."""

    # Defensive check; mirrors the loader's call in services/admin-dashboard-api.
    Draft202012Validator.check_schema(copy.deepcopy(schema))
