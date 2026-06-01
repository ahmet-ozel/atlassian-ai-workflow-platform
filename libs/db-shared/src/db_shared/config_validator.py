"""Department config validation utilities.

Provides programmatic validation of ``config/departments.json`` against
``config/departments.schema.json`` and additional semantic checks that
go beyond what JSON Schema can express.

The primary entry point is :func:`validate_departments_config` which:

1. Validates the JSON document against the JSON Schema (Draft 2020-12).
2. Runs semantic checks on the new platform-completion fields:
   - ``approval_required_paths`` regex patterns are compilable.
   - ``approvers`` list is non-empty when ``approval_required_paths`` is non-empty.
   - ``docker_defaults.default_timeout_seconds`` <= ``docker_defaults.max_timeout_seconds``.
   - ``teams_webhook_ref`` follows the ``vault:`` pattern when ``notify_channels`` includes "teams".
   - ``status_mapping`` keys are from the supported set.

Usage::

    from db_shared.config_validator import (
        validate_departments_config,
        load_and_validate_departments,
        ValidationError,
    )

    # Validate a dict directly
    errors = validate_departments_config(config_dict)
    if errors:
        for err in errors:
            print(f"  - {err}")

    # Load from disk and validate
    config, errors = load_and_validate_departments()
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Sequence

__all__ = [
    "ConfigValidationError",
    "load_and_validate_departments",
    "validate_departments_config",
    "validate_department_entry",
]


#: Supported logical status keys for status_mapping.
SUPPORTED_LOGICAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"todo", "in_progress", "review", "done", "out_of_scope"}
)

#: Valid notification channels.
VALID_NOTIFY_CHANNELS: Final[frozenset[str]] = frozenset(
    {"slack", "email", "teams"}
)

#: Valid Docker cleanup policies.
VALID_CLEANUP_POLICIES: Final[frozenset[str]] = frozenset(
    {"on_success", "always", "never"}
)

#: Vault reference pattern.
_VAULT_REF_RE: Final[re.Pattern[str]] = re.compile(r"^vault:[a-zA-Z0-9/_-]+$")

#: Default path to departments.json (relative to this file's location).
_DEFAULT_CONFIG_PATH: Final[Path] = (
    Path(__file__).resolve().parents[4] / "config" / "departments.json"
)

#: Default path to departments.schema.json.
_DEFAULT_SCHEMA_PATH: Final[Path] = (
    Path(__file__).resolve().parents[4] / "config" / "departments.schema.json"
)


@dataclass(frozen=True, slots=True)
class ConfigValidationError:
    """A single validation error with path and message.

    Attributes:
        path: JSON path to the offending field (e.g. "departments[0].docker_defaults.cpu_limit").
        message: Human-readable error description.
        severity: "error" for hard failures, "warning" for advisory issues.
    """

    path: str
    message: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"[{self.severity}] {self.path}: {self.message}"


def validate_departments_config(
    config: dict[str, Any],
    *,
    schema: dict[str, Any] | None = None,
    schema_path: Path | None = None,
) -> list[ConfigValidationError]:
    """Validate a departments config dict against schema + semantic rules.

    Parameters
    ----------
    config:
        The parsed departments.json content.
    schema:
        Optional pre-loaded schema dict. If not provided, loads from
        ``schema_path`` or the default location.
    schema_path:
        Path to the schema file. Defaults to
        ``config/departments.schema.json``.

    Returns
    -------
    list[ConfigValidationError]
        Empty list means the config is valid. Non-empty means there
        are validation errors.
    """
    errors: list[ConfigValidationError] = []

    # --- JSON Schema validation ---
    schema_errors = _validate_json_schema(config, schema=schema, schema_path=schema_path)
    errors.extend(schema_errors)

    # If schema validation fails badly, semantic checks may not make sense
    # but we still try to provide as much feedback as possible.

    # --- Semantic validation ---
    departments = config.get("departments", [])
    if not isinstance(departments, list):
        return errors

    for idx, dept in enumerate(departments):
        if not isinstance(dept, dict):
            continue
        dept_id = dept.get("id", f"<index:{idx}>")
        prefix = f"departments[{idx}]"
        dept_errors = validate_department_entry(dept, path_prefix=prefix)
        errors.extend(dept_errors)

    return errors


def validate_department_entry(
    dept: dict[str, Any],
    *,
    path_prefix: str = "",
) -> list[ConfigValidationError]:
    """Run semantic validation on a single department entry.

    Checks that go beyond JSON Schema structural validation:
    - Regex patterns in approval_required_paths are compilable.
    - docker_defaults timeout consistency.
    - teams_webhook_ref presence when teams channel is configured.
    - status_mapping key validation.

    Parameters
    ----------
    dept:
        A single department dict from the departments array.
    path_prefix:
        Path prefix for error messages (e.g. "departments[0]").

    Returns
    -------
    list[ConfigValidationError]
        Semantic validation errors found.
    """
    errors: list[ConfigValidationError] = []

    # --- approval_required_paths regex validation ---
    approval_paths = dept.get("approval_required_paths", [])
    if isinstance(approval_paths, list):
        for i, pattern in enumerate(approval_paths):
            if isinstance(pattern, str):
                try:
                    re.compile(pattern)
                except re.error as exc:
                    errors.append(ConfigValidationError(
                        path=f"{path_prefix}.approval_required_paths[{i}]",
                        message=f"Invalid regex pattern: {exc}",
                    ))

    # --- approvers advisory check ---
    approvers = dept.get("approvers", [])
    if approval_paths and not approvers:
        errors.append(ConfigValidationError(
            path=f"{path_prefix}.approvers",
            message=(
                "approval_required_paths is non-empty but approvers list is empty; "
                "no user will be able to approve commits matching these paths"
            ),
            severity="warning",
        ))

    # --- docker_defaults validation ---
    docker_defaults = dept.get("docker_defaults")
    if isinstance(docker_defaults, dict):
        default_timeout = docker_defaults.get("default_timeout_seconds")
        max_timeout = docker_defaults.get("max_timeout_seconds")
        if (
            isinstance(default_timeout, (int, float))
            and isinstance(max_timeout, (int, float))
            and default_timeout > max_timeout
        ):
            errors.append(ConfigValidationError(
                path=f"{path_prefix}.docker_defaults",
                message=(
                    f"default_timeout_seconds ({default_timeout}) must not exceed "
                    f"max_timeout_seconds ({max_timeout})"
                ),
            ))

        cleanup_policy = docker_defaults.get("cleanup_policy")
        if cleanup_policy is not None and cleanup_policy not in VALID_CLEANUP_POLICIES:
            errors.append(ConfigValidationError(
                path=f"{path_prefix}.docker_defaults.cleanup_policy",
                message=(
                    f"Invalid cleanup_policy '{cleanup_policy}'; "
                    f"must be one of: {sorted(VALID_CLEANUP_POLICIES)}"
                ),
            ))

    # --- notify_channels + teams_webhook_ref consistency ---
    notify_channels = dept.get("notify_channels", [])
    teams_webhook_ref = dept.get("teams_webhook_ref")
    if isinstance(notify_channels, list) and "teams" in notify_channels:
        if not teams_webhook_ref:
            errors.append(ConfigValidationError(
                path=f"{path_prefix}.teams_webhook_ref",
                message=(
                    "notify_channels includes 'teams' but teams_webhook_ref "
                    "is null/empty; Teams notifications will fail at runtime"
                ),
                severity="warning",
            ))

    if teams_webhook_ref and isinstance(teams_webhook_ref, str):
        if not _VAULT_REF_RE.match(teams_webhook_ref):
            errors.append(ConfigValidationError(
                path=f"{path_prefix}.teams_webhook_ref",
                message=(
                    f"teams_webhook_ref must match vault: pattern; "
                    f"got '{teams_webhook_ref}'"
                ),
            ))

    # --- status_mapping key validation ---
    status_mapping = dept.get("status_mapping")
    if isinstance(status_mapping, dict):
        invalid_keys = set(status_mapping.keys()) - SUPPORTED_LOGICAL_STATUSES
        if invalid_keys:
            errors.append(ConfigValidationError(
                path=f"{path_prefix}.status_mapping",
                message=(
                    f"Unsupported logical status keys: {sorted(invalid_keys)}; "
                    f"supported: {sorted(SUPPORTED_LOGICAL_STATUSES)}"
                ),
            ))

    # --- branch_pattern_rules validation ---
    branch_rules = dept.get("branch_pattern_rules", [])
    if isinstance(branch_rules, list):
        for i, rule in enumerate(branch_rules):
            if isinstance(rule, dict):
                # Check that glob is non-empty
                glob_val = rule.get("glob", "")
                if not glob_val:
                    errors.append(ConfigValidationError(
                        path=f"{path_prefix}.branch_pattern_rules[{i}].glob",
                        message="glob pattern must not be empty",
                    ))

    return errors


def load_and_validate_departments(
    *,
    config_path: Path | None = None,
    schema_path: Path | None = None,
) -> tuple[dict[str, Any] | None, list[ConfigValidationError]]:
    """Load departments.json from disk and validate it.

    Parameters
    ----------
    config_path:
        Path to departments.json. Defaults to the standard location.
    schema_path:
        Path to departments.schema.json. Defaults to the standard location.

    Returns
    -------
    tuple[dict | None, list[ConfigValidationError]]
        The loaded config (or None on load failure) and any validation errors.
    """
    config_path = config_path or _DEFAULT_CONFIG_PATH
    schema_path = schema_path or _DEFAULT_SCHEMA_PATH

    # Load config
    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except FileNotFoundError:
        return None, [ConfigValidationError(
            path="<file>",
            message=f"departments.json not found at {config_path}",
        )]
    except json.JSONDecodeError as exc:
        return None, [ConfigValidationError(
            path="<file>",
            message=f"departments.json is not valid JSON: {exc}",
        )]

    # Load schema
    schema: dict[str, Any] | None = None
    try:
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        # Schema load failure is non-fatal; we still run semantic checks
        pass

    errors = validate_departments_config(config, schema=schema)
    return config, errors


def _validate_json_schema(
    config: dict[str, Any],
    *,
    schema: dict[str, Any] | None = None,
    schema_path: Path | None = None,
) -> list[ConfigValidationError]:
    """Validate config against JSON Schema.

    Returns errors as ConfigValidationError instances. If jsonschema
    is not installed or schema cannot be loaded, returns a single
    warning and skips schema validation.
    """
    errors: list[ConfigValidationError] = []

    if schema is None:
        schema_path = schema_path or _DEFAULT_SCHEMA_PATH
        try:
            with open(schema_path, encoding="utf-8") as f:
                schema = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            errors.append(ConfigValidationError(
                path="<schema>",
                message=f"Could not load schema for validation: {exc}",
                severity="warning",
            ))
            return errors

    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        errors.append(ConfigValidationError(
            path="<schema>",
            message="jsonschema package not installed; skipping schema validation",
            severity="warning",
        ))
        return errors

    try:
        validator = Draft202012Validator(schema)
        schema_errors = sorted(
            validator.iter_errors(config),
            key=lambda e: list(e.absolute_path),
        )
        for err in schema_errors:
            json_path = ".".join(str(p) for p in err.absolute_path) or "<root>"
            errors.append(ConfigValidationError(
                path=json_path,
                message=err.message,
            ))
    except Exception as exc:
        errors.append(ConfigValidationError(
            path="<schema>",
            message=f"Schema validation failed unexpectedly: {exc}",
        ))

    return errors
