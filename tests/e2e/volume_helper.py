"""
Volume prefix helper for Docker Compose.

Implements R25 fix: determines the correct volume prefix based on
the Docker Compose project name derivation rules.

Docker Compose project name resolution order:
1. COMPOSE_PROJECT_NAME environment variable
2. Top-level `name:` in compose file
3. Directory name of the first compose file specified with -f
4. Current working directory name (if no -f specified)

For this platform, compose is invoked as:
  docker compose -f infra/docker-compose.yml ...
from the platform/ directory, so the project name is "infra".

Volumes are therefore prefixed as: infra_pg_data, infra_minio_data, etc.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List, Optional


def get_compose_project_name(platform_root: Path) -> str:
    """Determine the Docker Compose project name.

    Returns the project name that Docker Compose would use when invoked
    from the platform/ directory with -f infra/docker-compose.yml.
    """
    # Check COMPOSE_PROJECT_NAME env var first
    env_name = os.environ.get("COMPOSE_PROJECT_NAME")
    if env_name:
        return env_name

    # The compose file is at infra/docker-compose.yml
    # When using -f, Compose uses the directory of the first -f file
    # as the project name basis
    compose_dir = platform_root / "infra"
    return compose_dir.name  # "infra"


def get_volume_prefix(platform_root: Path) -> str:
    """Get the correct volume name prefix for filtering.

    Returns the prefix string that Docker uses for named volumes
    (project_name + underscore).
    """
    project_name = get_compose_project_name(platform_root)
    return f"{project_name}_"


def list_platform_volumes(platform_root: Path) -> List[str]:
    """List all Docker volumes belonging to this platform stack.

    Uses the correct prefix derived from the compose project name.
    """
    prefix = get_volume_prefix(platform_root)
    try:
        result = subprocess.run(
            ["docker", "volume", "ls", "--filter", f"name={prefix}", "--format", "{{.Name}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return [v.strip() for v in result.stdout.strip().split("\n") if v.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return []


# Expected volume names (without prefix)
EXPECTED_VOLUMES = ["pg_data", "minio_data", "agent_workspace"]


def verify_volumes_exist(platform_root: Path) -> dict[str, bool]:
    """Check which expected volumes exist with the correct prefix.

    Returns a dict mapping volume_name -> exists.
    """
    prefix = get_volume_prefix(platform_root)
    existing = list_platform_volumes(platform_root)

    result = {}
    for vol in EXPECTED_VOLUMES:
        full_name = f"{prefix}{vol}"
        result[vol] = full_name in existing
    return result
