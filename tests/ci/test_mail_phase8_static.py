"""Phase 8 static checks for Mail Chat / Mail MCP integration."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from jsonschema.validators import Draft202012Validator


def test_mail_services_are_schema_valid_manifest_entries(repo_root: Path) -> None:
    schema = json.loads(
        (repo_root / "config" / "services.manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    manifest = json.loads(
        (repo_root / "config" / "services.manifest.json").read_text(encoding="utf-8")
    )
    errors = list(Draft202012Validator(schema).iter_errors(manifest))
    assert not errors, [error.message for error in errors]

    by_name = {entry["name"]: entry for entry in manifest["services"]}
    for name, port in (("gmail-mcp", "8110"), ("outlook-mcp", "8120")):
        assert name in by_name
        entry = by_name[name]
        assert entry["compose_service_name"] == name
        assert entry["compose_profile"] == name
        assert entry["health_endpoint"] == "/healthz"
        assert port in (entry.get("test_command") or "")


def test_compose_config_contains_mail_mcp_routes(repo_root: Path) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI is not available")

    result = subprocess.run(
        ["docker", "compose", "-f", "infra/docker-compose.yml", "config", "--quiet"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout

    compose_source = (repo_root / "infra" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )
    assert "gmail-mcp:" in compose_source
    assert "outlook-mcp:" in compose_source
    assert "GMAIL_MCP_BASE_URL" in compose_source
    assert "OUTLOOK_MCP_BASE_URL" in compose_source
    assert "http://gmail-mcp:8110" in compose_source
    assert "http://outlook-mcp:8120" in compose_source

