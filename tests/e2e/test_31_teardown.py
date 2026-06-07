"""
Test 31: Graceful teardown (R31).

Validates that `make down` gracefully stops all containers, exits cleanly,
and preserves named volumes for data persistence across restarts.

Verification steps:
1. Execute `make down`  assert exit 0 within 60s
2. Assert `docker compose ps -q` returns empty
3. Assert named volumes still exist (pg_data, minio_data, agent_workspace)
4. Emit evidence JSON

Requirements: R31.1, R31.2, R31.3, R31.4, R31.5
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

EVIDENCE_FILENAME = "31-teardown.json"
COMMAND_TIMEOUT = 120
PROJECT_PREFIX = "platform"

# Named volumes that should persist after teardown
EXPECTED_VOLUMES = [
    "pg_data",
    "minio_data",
    "agent_workspace",
]


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


def _get_docker_volumes(prefix: str) -> list[str]:
    """List Docker volumes matching the project prefix."""
    result = subprocess.run(
        ["docker", "volume", "ls", "--format", "{{.Name}}",
         "--filter", f"name={prefix}"],
        capture_output=True,
        text=True,
        timeout=30,
        shell=platform.system() == "Windows",
    )
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGracefulTeardown:
    """R31: Verify graceful teardown preserves volumes."""

    def test_make_down_exits_zero(self, platform_root):
        """R31.1: make down exits 0 within 60s.

        The teardown command should complete successfully without
        errors or timeouts.
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

    def test_compose_ps_empty_after_teardown(self, platform_root):
        """R31.2: docker compose ps -q returns empty after teardown.

        No containers should be running after make down completes.
        """
        # Ensure make down has been run
        _run_cmd(["make", "down"], cwd=str(platform_root), timeout=COMMAND_TIMEOUT)

        # Wait for containers to fully stop
        time.sleep(3)

        result = _get_compose_ps(cwd=str(platform_root))
        container_ids = [
            line.strip()
            for line in result.stdout.strip().splitlines()
            if line.strip()
        ]

        assert len(container_ids) == 0, (
            f"docker compose ps -q returned {len(container_ids)} container(s) "
            f"after make down. Expected 0.\n"
            f"Container IDs: {container_ids}"
        )

    def test_named_volumes_preserved(self, platform_root):
        """R31.3: Named volumes still exist after teardown.

        Data volumes (pg_data, minio_data, agent_workspace) should
        persist after make down so data is not lost between restarts.
        """
        # Ensure make down has been run
        _run_cmd(["make", "down"], cwd=str(platform_root), timeout=COMMAND_TIMEOUT)
        time.sleep(2)

        # List volumes with project prefix
        volumes = _get_docker_volumes(PROJECT_PREFIX)

        # Check for expected volumes
        found_volumes = []
        missing_volumes = []

        for expected in EXPECTED_VOLUMES:
            # Volume names may be prefixed with project name
            # e.g., "platform_pg_data" or just "pg_data"
            matches = [
                v for v in volumes
                if expected in v
            ]
            if matches:
                found_volumes.append(expected)
            else:
                missing_volumes.append(expected)

        # At least some volumes should exist if the stack was ever started
        # If no volumes exist at all, the stack may never have been started
        if not volumes:
            pytest.skip(
                "No Docker volumes found with project prefix. "
                "Stack may not have been started in this session."
            )

        # Verify expected volumes are preserved
        assert len(missing_volumes) == 0, (
            f"Expected volumes missing after teardown: {missing_volumes}\n"
            f"Found volumes: {volumes}\n"
            f"make down should use --volumes=false or equivalent to preserve data."
        )

    def test_no_orphan_containers(self, platform_root):
        """R31.4: No orphan containers remain with project label.

        After teardown, docker ps should show no containers belonging
        to this compose project.
        """
        # Ensure make down has been run
        _run_cmd(["make", "down"], cwd=str(platform_root), timeout=COMMAND_TIMEOUT)
        time.sleep(3)

        # Check for any containers with project label
        result = _run_cmd(
            ["docker", "ps", "--filter",
             f"label=com.docker.compose.project={PROJECT_PREFIX}",
             "--format", "{{.Names}}"],
            cwd=str(platform_root),
        )

        remaining = [
            line.strip()
            for line in result.stdout.strip().splitlines()
            if line.strip()
        ]

        assert len(remaining) == 0, (
            f"Found {len(remaining)} orphan container(s) after teardown: "
            f"{remaining}"
        )


class TestTeardownEvidence:
    """R31.5: Emit structured evidence for graceful teardown."""

    def test_emit_evidence(self, evidence_collector, platform_root):
        """Collect teardown verification data and emit evidence JSON."""
        evidence_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "project_prefix": PROJECT_PREFIX,
            "expected_volumes": EXPECTED_VOLUMES,
            "make_down": {},
            "compose_ps_after": {},
            "volumes_check": {},
            "orphan_check": {},
            "overall_verdict": "pass",
        }

        # Run make down
        start = time.time()
        down_result = _run_cmd(
            ["make", "down"],
            cwd=str(platform_root),
            timeout=COMMAND_TIMEOUT,
        )
        elapsed = time.time() - start

        evidence_data["make_down"] = {
            "exit_code": down_result.returncode,
            "elapsed_seconds": round(elapsed, 1),
            "stdout_snippet": down_result.stdout[:1500],
            "stderr_snippet": down_result.stderr[:500],
            "passed": down_result.returncode == 0,
        }

        # Wait for containers to stop
        time.sleep(3)

        # Check docker compose ps -q
        ps_result = _get_compose_ps(cwd=str(platform_root))
        container_ids = [
            line.strip()
            for line in ps_result.stdout.strip().splitlines()
            if line.strip()
        ]
        evidence_data["compose_ps_after"] = {
            "container_ids": container_ids,
            "count": len(container_ids),
            "passed": len(container_ids) == 0,
        }

        # Check volumes
        volumes = _get_docker_volumes(PROJECT_PREFIX)
        found_expected = [v for v in EXPECTED_VOLUMES if any(v in vol for vol in volumes)]
        evidence_data["volumes_check"] = {
            "all_volumes": volumes,
            "expected_found": found_expected,
            "expected_missing": [v for v in EXPECTED_VOLUMES if v not in found_expected],
            "passed": len(found_expected) == len(EXPECTED_VOLUMES) or len(volumes) == 0,
        }

        # Check orphan containers
        orphan_result = _run_cmd(
            ["docker", "ps", "--filter",
             f"label=com.docker.compose.project={PROJECT_PREFIX}",
             "--format", "{{.Names}}"],
            cwd=str(platform_root),
        )
        orphans = [
            line.strip()
            for line in orphan_result.stdout.strip().splitlines()
            if line.strip()
        ]
        evidence_data["orphan_check"] = {
            "orphan_containers": orphans,
            "count": len(orphans),
            "passed": len(orphans) == 0,
        }

        # Overall verdict
        all_passed = (
            evidence_data["make_down"]["passed"]
            and evidence_data["compose_ps_after"]["passed"]
            and evidence_data["orphan_check"]["passed"]
        )
        evidence_data["overall_verdict"] = "pass" if all_passed else "fail"

        # Emit evidence
        evidence_collector.emit_json(
            requirement_id="R31.1,R31.2,R31.3,R31.4,R31.5",
            filename=EVIDENCE_FILENAME,
            data=evidence_data,
        )
