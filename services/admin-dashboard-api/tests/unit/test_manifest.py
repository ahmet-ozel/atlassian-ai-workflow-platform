"""Unit tests for ``src.manifest``.

These tests validate the public manifest contract:
* ``ManagedServiceEntry`` is frozen and exposes the seven manifest fields.
* ``load_manifest`` returns an immutable tuple on a valid manifest.
* ``ManifestLoadError`` is raised on missing files, malformed JSON, schema
  violations, and duplicate ``compose_service_name`` values."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.manifest import (
    MANIFEST_RELATIVE_PATH,
    SCHEMA_RELATIVE_PATH,
    ManagedServiceEntry,
    ManifestLoadError,
    load_manifest,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


WORKSPACE_ROOT = Path(__file__).resolve().parents[4]


def _read_real_schema() -> dict:
    return json.loads((WORKSPACE_ROOT / SCHEMA_RELATIVE_PATH).read_text(encoding="utf-8"))


def _valid_entry(**overrides: object) -> dict:
    base: dict[str, object] = {
        "name": "automation-service",
        "kind": "http_service",
        "compose_service_name": "automation-service",
        "compose_profile": "automation-service",
        "env_example_path": "services/automation-service/.env.example",
        "health_endpoint": "/healthz",
        "test_command": "docker compose -f infra/docker-compose.yml exec automation-service pytest tests/integration/ -v",
    }
    base.update(overrides)
    return base


def _write_manifest_pair(root: Path, manifest_payload: object) -> None:
    """Write the schema (real one shipped in repo) and a custom manifest."""

    config_dir = root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (root / SCHEMA_RELATIVE_PATH).write_text(
        json.dumps(_read_real_schema()), encoding="utf-8"
    )
    (root / MANIFEST_RELATIVE_PATH).write_text(
        json.dumps(manifest_payload), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# ManagedServiceEntry
# ---------------------------------------------------------------------------


def test_managed_service_entry_is_frozen() -> None:
    entry = ManagedServiceEntry(
        name="x",
        kind="http_service",
        compose_service_name="x",
        compose_profile="x",
        env_example_path="x/.env.example",
        health_endpoint="/healthz",
        test_command=None,
    )
    with pytest.raises(FrozenInstanceError):
        entry.name = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Happy path: load the real workspace manifest
# ---------------------------------------------------------------------------


def test_load_manifest_real_workspace_returns_immutable_tuple() -> None:
    entries = load_manifest(WORKSPACE_ROOT)

    assert isinstance(entries, tuple)
    assert len(entries) >= 1

    # Every entry is a frozen dataclass instance.
    for e in entries:
        assert isinstance(e, ManagedServiceEntry)

    # compose_service_name is unique - sanity check on the
    # repo-shipped manifest as well.
    names = [e.compose_service_name for e in entries]
    assert len(set(names)) == len(names)


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_load_manifest_missing_file_raises(tmp_path: Path) -> None:
    # Schema present, manifest absent.
    (tmp_path / "config").mkdir()
    (tmp_path / SCHEMA_RELATIVE_PATH).write_text(
        json.dumps(_read_real_schema()), encoding="utf-8"
    )

    with pytest.raises(ManifestLoadError, match="services.manifest.json not found"):
        load_manifest(tmp_path)


def test_load_manifest_malformed_json_raises(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / SCHEMA_RELATIVE_PATH).write_text(
        json.dumps(_read_real_schema()), encoding="utf-8"
    )
    (tmp_path / MANIFEST_RELATIVE_PATH).write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(ManifestLoadError, match="not valid JSON"):
        load_manifest(tmp_path)


def test_load_manifest_schema_violation_raises(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "services": [
            {
                # Missing required "compose_service_name", "compose_profile", ...
                "name": "automation-service",
                "kind": "http_service",
            }
        ],
    }
    _write_manifest_pair(tmp_path, bad)

    with pytest.raises(
        ManifestLoadError, match="failed JSON Schema 2020-12 validation"
    ):
        load_manifest(tmp_path)


def test_load_manifest_invalid_kind_raises(tmp_path: Path) -> None:
    bad = {
        "version": 1,
        "services": [_valid_entry(kind="bogus_kind")],
    }
    _write_manifest_pair(tmp_path, bad)

    with pytest.raises(ManifestLoadError, match="JSON Schema"):
        load_manifest(tmp_path)


def test_load_manifest_duplicate_compose_service_name_raises(tmp_path: Path) -> None:
    """"""

    dup = {
        "version": 1,
        "services": [
            _valid_entry(name="alpha", compose_service_name="shared-name"),
            _valid_entry(name="beta", compose_service_name="shared-name"),
        ],
    }
    _write_manifest_pair(tmp_path, dup)

    with pytest.raises(ManifestLoadError, match="duplicate compose_service_name"):
        load_manifest(tmp_path)


def test_load_manifest_accepts_valid_minimal_entry(tmp_path: Path) -> None:
    payload = {
        "version": 1,
        "services": [
            _valid_entry(
                name="agent-runner-worker",
                kind="worker",
                compose_service_name="agent-runner-worker",
                compose_profile="agent-runner-worker",
                env_example_path="workers/agent-runner-worker/.env.example",
                health_endpoint=None,
                test_command=None,
            ),
        ],
    }
    _write_manifest_pair(tmp_path, payload)

    entries = load_manifest(tmp_path)
    assert len(entries) == 1
    e = entries[0]
    assert e.kind == "worker"
    assert e.health_endpoint is None
    assert e.test_command is None
