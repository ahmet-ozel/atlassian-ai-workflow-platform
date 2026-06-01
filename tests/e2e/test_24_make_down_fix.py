"""
Test 24: Verify make down fix (R24).

Validates that the `make down` command properly stops ALL containers,
including profile-gated services that were previously left running.

Verification steps:
1. Run `make down` from platform/
2. Assert `docker compose ps -q` returns empty (no running containers)
3. Assert no containers with the project prefix remain via docker ps
4. Emit evidence JSON

Requirements: R24.3, R24.4, R24.5
"""

import platform
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_FILENAME = "24-make-down-fix.json"
COMMAND_TIMEOUT = 120
# Docker Compose derives project name from the working directory when
# no explicit `name:` or COMPOSE_PROJECT_NAME is set. The Makefile runs
# from platform/ so the project name is "platform".
PROJECT_PREFIX = "platform"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cmd(cmd: list[str], cwd: str, timeout: int = COMMAND_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a command and return the CompletedProcess result."""
    use_shell = platform.system() == "Windows"
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        shell=use_shell,
    )


def _get_compose_ps(cwd: str) -> subprocess.CompletedProcess:
    """Run docker compose ps -q to list running container IDs."""
    return _run_cmd(
        ["docker", "compose", "-f", "infra/docker-compose.yml",
         "-f", "infra/docker-compose.dev.yml", "ps", "-q"],
        cwd=cwd,
    )


def _get_docker_ps_with_prefix(cwd: str) -> subprocess.CompletedProcess:
    """Run docker ps filtering by compose project label to find leftover containers."""
    return _run_cmd(
        ["docker", "ps", "--filter",
         f"label=com.docker.compose.project={PROJECT_PREFIX}",
         "--format", "{{.Names}}"],
        cwd=cwd,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMakeDownFix:
    """R24: Verify make down stops ALL containers including profile-gated ones."""

    def test_make_down_exits_zero(self, platform_root):
        """make down must exit 0 within the timeout.

        After the R24 fix, the down target includes all profile flags
        so that profile-gated services are also stopped.
        """
        result = _run_cmd(
            ["make", "down"],
            cwd=str(platform_root),
            timeout=COMMAND_TIMEOUT,
        )

        assert result.returncode == 0, (
            f"make down failed with exit code {result.returncode}.\n"
            f"stdout: {result.stdout[:1500]}\n"
            f"stderr: {result.stderr[:1000]}"
        )

    def test_compose_ps_empty_after_down(self, platform_root):
        """docker compose ps -q must return empty after make down.

        R24.3: After make down, no containers should be listed by
        docker compose ps -q (which shows only running container IDs).
        """
        # First ensure make down has been run
        _run_cmd(["make", "down"], cwd=str(platform_root), timeout=COMMAND_TIMEOUT)

        # Small delay to allow containers to fully stop
        time.sleep(2)

        result = _get_compose_ps(cwd=str(platform_root))

        # ps -q returns container IDs, one per line. Empty = no containers.
        container_ids = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]

        assert len(container_ids) == 0, (
            f"docker compose ps -q returned {len(container_ids)} container(s) "
            f"after make down. Expected 0.\n"
            f"Container IDs still running: {container_ids}\n"
            f"The R24 fix should stop ALL containers including profile-gated ones."
        )

    def test_no_project_containers_remain(self, platform_root):
        """No containers with the project prefix should remain after make down.

        R24.4: Uses docker ps with label filter to verify no containers
        belonging to this compose project are still running.
        """
        # First ensure make down has been run
        _run_cmd(["make", "down"], cwd=str(platform_root), timeout=COMMAND_TIMEOUT)

        # Small delay to allow containers to fully stop
        time.sleep(2)

        result = _get_docker_ps_with_prefix(cwd=str(platform_root))

        remaining = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]

        assert len(remaining) == 0, (
            f"Found {len(remaining)} container(s) with project prefix "
            f"'{PROJECT_PREFIX}' still running after make down.\n"
            f"Remaining containers: {remaining}\n"
            f"The R24 fix should ensure ALL profile-gated services are stopped."
        )


class TestMakeDownFixEvidence:
    """R24.5: Emit structured evidence for the make down fix."""

    def test_emit_evidence(self, evidence_collector, platform_root):
        """Collect make down verification data and emit evidence JSON."""
        evidence_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "project_prefix": PROJECT_PREFIX,
            "make_down": {},
            "compose_ps_after": {},
            "docker_ps_after": {},
            "overall_verdict": "pass",
        }

        # Run make down
        down_result = _run_cmd(
            ["make", "down"],
            cwd=str(platform_root),
            timeout=COMMAND_TIMEOUT,
        )
        evidence_data["make_down"] = {
            "exit_code": down_result.returncode,
            "stdout_snippet": down_result.stdout[:2000],
            "stderr_snippet": down_result.stderr[:1000],
            "passed": down_result.returncode == 0,
        }

        # Wait for containers to stop
        time.sleep(2)

        # Check docker compose ps -q
        ps_result = _get_compose_ps(cwd=str(platform_root))
        container_ids = [
            line.strip()
            for line in ps_result.stdout.strip().splitlines()
            if line.strip()
        ]
        evidence_data["compose_ps_after"] = {
            "exit_code": ps_result.returncode,
            "container_ids": container_ids,
            "count": len(container_ids),
            "passed": len(container_ids) == 0,
        }

        # Check docker ps with project label filter
        docker_ps_result = _get_docker_ps_with_prefix(cwd=str(platform_root))
        remaining_containers = [
            line.strip()
            for line in docker_ps_result.stdout.strip().splitlines()
            if line.strip()
        ]
        evidence_data["docker_ps_after"] = {
            "exit_code": docker_ps_result.returncode,
            "remaining_containers": remaining_containers,
            "count": len(remaining_containers),
            "passed": len(remaining_containers) == 0,
        }

        # Overall verdict
        all_passed = (
            evidence_data["make_down"]["passed"]
            and evidence_data["compose_ps_after"]["passed"]
            and evidence_data["docker_ps_after"]["passed"]
        )
        evidence_data["overall_verdict"] = "pass" if all_passed else "fail"

        # Emit evidence
        evidence_collector.emit_json(
            requirement_id="R24.3,R24.4,R24.5",
            filename=EVIDENCE_FILENAME,
            data=evidence_data,
        )
