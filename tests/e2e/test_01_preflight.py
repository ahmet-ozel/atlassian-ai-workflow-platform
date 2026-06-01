"""
Pre-flight checks for Docker Desktop, resource allocation, port availability,
and Node.js/Playwright versions.

Validates that the local environment meets all prerequisites before starting
the E2E test suite.

Requirements: R1.1, R1.2, R1.3, R1.4, R1.5, R1.6
"""

import json
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUIRED_PORTS = [3000, 5433, 8082, 8200, 8090, 8501, 7233, 8233]

MIN_DOCKER_VERSION = (27, 0)
MIN_COMPOSE_VERSION = (2, 27)
MIN_NODE_VERSION = (18, 0)
MIN_RAM_GIB = 8
MIN_CPUS = 4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cmd(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a command and return the CompletedProcess result.

    Uses shell=True on Windows to resolve .cmd/.bat wrappers (e.g. npx.cmd).
    """
    import platform

    use_shell = platform.system() == "Windows"
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=use_shell,
    )


def _parse_version(version_str: str) -> tuple[int, ...]:
    """Extract numeric version tuple from a version string like '27.5.1' or 'v2.27.0'."""
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", version_str)
    if not match:
        return (0, 0, 0)
    parts = [int(x) for x in match.groups() if x is not None]
    return tuple(parts)


def _is_port_free(port: int) -> bool:
    """Check if a TCP port is free on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        result = sock.connect_ex(("127.0.0.1", port))
        return result != 0


def _get_pid_on_port(port: int) -> dict[str, Any]:
    """Get PID and process name occupying a port (Windows netstat)."""
    try:
        result = _run_cmd(["netstat", "-ano"], timeout=10)
        if result.returncode != 0:
            return {"port": port, "pid": None, "process": "unknown"}

        for line in result.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = parts[-1] if parts else "unknown"
                # Try to get process name via tasklist
                try:
                    tasklist = _run_cmd(
                        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                        timeout=5,
                    )
                    proc_name = tasklist.stdout.split(",")[0].strip('"') if tasklist.stdout else "unknown"
                except Exception:
                    proc_name = "unknown"
                return {"port": port, "pid": pid, "process": proc_name}
    except Exception:
        pass
    return {"port": port, "pid": None, "process": "unknown"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDockerDesktopValidation:
    """R1.1: Assert docker info exits 0, Server Version >= 27.0, compose v2.27+."""

    def test_docker_info_exits_zero(self):
        """docker info must exit with code 0 (Docker Desktop running)."""
        result = _run_cmd(["docker", "info"])
        assert result.returncode == 0, (
            f"docker info failed with exit code {result.returncode}.\n"
            f"Ensure Docker Desktop is running.\n"
            f"stderr: {result.stderr[:500]}"
        )

    def test_docker_server_version(self):
        """Docker Server Version must be >= 27.0."""
        result = _run_cmd(["docker", "info", "--format", "{{.ServerVersion}}"])
        assert result.returncode == 0, f"docker info --format failed: {result.stderr}"

        version_str = result.stdout.strip()
        version = _parse_version(version_str)
        assert version[:2] >= MIN_DOCKER_VERSION, (
            f"Docker Server Version {version_str} is below minimum {MIN_DOCKER_VERSION[0]}.{MIN_DOCKER_VERSION[1]}. "
            f"Please update Docker Desktop."
        )

    def test_docker_compose_version(self):
        """docker compose version must be >= v2.27."""
        result = _run_cmd(["docker", "compose", "version"])
        assert result.returncode == 0, f"docker compose version failed: {result.stderr}"

        version_str = result.stdout.strip()
        version = _parse_version(version_str)
        assert version[:2] >= MIN_COMPOSE_VERSION, (
            f"Docker Compose version {version_str} is below minimum "
            f"v{MIN_COMPOSE_VERSION[0]}.{MIN_COMPOSE_VERSION[1]}. "
            f"Please update Docker Desktop."
        )


class TestDockerResources:
    """R1.2: Assert Docker Desktop RAM >= 8 GiB, CPUs >= 4."""

    def test_docker_ram_allocation(self):
        """Docker Desktop must have >= 8 GiB RAM allocated."""
        result = _run_cmd(["docker", "info", "--format", "{{.MemTotal}}"])
        assert result.returncode == 0, f"Failed to query Docker memory: {result.stderr}"

        mem_bytes = int(result.stdout.strip())
        mem_gib = mem_bytes / (1024 ** 3)
        assert mem_gib >= MIN_RAM_GIB, (
            f"Docker Desktop RAM is {mem_gib:.1f} GiB, minimum required is {MIN_RAM_GIB} GiB. "
            f"Increase memory allocation in Docker Desktop Settings > Resources."
        )

    def test_docker_cpu_allocation(self):
        """Docker Desktop must have >= 4 CPUs allocated."""
        result = _run_cmd(["docker", "info", "--format", "{{.NCPU}}"])
        assert result.returncode == 0, f"Failed to query Docker CPUs: {result.stderr}"

        cpus = int(result.stdout.strip())
        assert cpus >= MIN_CPUS, (
            f"Docker Desktop CPUs is {cpus}, minimum required is {MIN_CPUS}. "
            f"Increase CPU allocation in Docker Desktop Settings > Resources."
        )


class TestPortAvailability:
    """R1.3, R1.4: Assert required ports are free; log offending PID if occupied."""

    def test_required_ports_are_free(self):
        """All required ports (3000, 5432, 8082, 8200, 8090, 8501, 7233, 8233) must be free."""
        occupied_ports = []

        for port in REQUIRED_PORTS:
            if not _is_port_free(port):
                info = _get_pid_on_port(port)
                occupied_ports.append(info)

        if occupied_ports:
            details = "\n".join(
                f"  - Port {p['port']}: PID={p['pid']}, Process={p['process']}"
                for p in occupied_ports
            )
            pytest.fail(
                f"The following required ports are occupied:\n{details}\n\n"
                f"Remediation: Stop the processes above or change their port bindings "
                f"before running the E2E test suite."
            )


class TestNodePlaywright:
    """R1.5: Assert node --version >= 18.0, npx playwright --version valid."""

    def test_node_version(self):
        """Node.js version must be >= 18.0."""
        result = _run_cmd(["node", "--version"])
        assert result.returncode == 0, (
            f"node --version failed. Ensure Node.js is installed and on PATH.\n"
            f"stderr: {result.stderr}"
        )

        version_str = result.stdout.strip()
        version = _parse_version(version_str)
        assert version[:2] >= MIN_NODE_VERSION, (
            f"Node.js version {version_str} is below minimum v{MIN_NODE_VERSION[0]}.{MIN_NODE_VERSION[1]}. "
            f"Please update Node.js."
        )

    def test_playwright_version(self):
        """npx playwright --version must return a valid version string."""
        result = _run_cmd(["npx", "playwright", "--version"], timeout=60)
        assert result.returncode == 0, (
            f"npx playwright --version failed. Ensure Playwright is installed.\n"
            f"stderr: {result.stderr}"
        )

        version_str = result.stdout.strip()
        version = _parse_version(version_str)
        assert version[0] > 0, (
            f"Playwright version could not be parsed from output: {version_str!r}"
        )


class TestPreflightEvidence:
    """R1.6: Emit e2e-evidence/01-preflight.json with all preflight results."""

    def test_emit_preflight_evidence(self, evidence_collector):
        """Collect all preflight data and emit structured evidence JSON."""
        evidence_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "docker": {},
            "resources": {},
            "ports": {},
            "node_playwright": {},
            "overall_verdict": "pass",
        }

        # Docker version info
        docker_info = _run_cmd(["docker", "info", "--format", "{{.ServerVersion}}"])
        compose_info = _run_cmd(["docker", "compose", "version"])
        evidence_data["docker"] = {
            "server_version": docker_info.stdout.strip() if docker_info.returncode == 0 else "ERROR",
            "compose_version": compose_info.stdout.strip() if compose_info.returncode == 0 else "ERROR",
            "docker_info_exit_code": docker_info.returncode,
        }

        # Resource allocation
        mem_result = _run_cmd(["docker", "info", "--format", "{{.MemTotal}}"])
        cpu_result = _run_cmd(["docker", "info", "--format", "{{.NCPU}}"])
        mem_gib = 0.0
        cpus = 0
        if mem_result.returncode == 0:
            try:
                mem_gib = int(mem_result.stdout.strip()) / (1024 ** 3)
            except ValueError:
                mem_gib = 0.0
        if cpu_result.returncode == 0:
            try:
                cpus = int(cpu_result.stdout.strip())
            except ValueError:
                cpus = 0

        evidence_data["resources"] = {
            "ram_gib": round(mem_gib, 2),
            "ram_meets_minimum": mem_gib >= MIN_RAM_GIB,
            "cpus": cpus,
            "cpus_meets_minimum": cpus >= MIN_CPUS,
        }

        # Port scan
        port_results = {}
        occupied = []
        for port in REQUIRED_PORTS:
            is_free = _is_port_free(port)
            port_results[str(port)] = {
                "free": is_free,
            }
            if not is_free:
                info = _get_pid_on_port(port)
                port_results[str(port)]["pid"] = info["pid"]
                port_results[str(port)]["process"] = info["process"]
                occupied.append(port)

        evidence_data["ports"] = {
            "checked": REQUIRED_PORTS,
            "results": port_results,
            "all_free": len(occupied) == 0,
            "occupied": occupied,
        }

        # Node.js and Playwright
        node_result = _run_cmd(["node", "--version"])
        playwright_result = _run_cmd(["npx", "playwright", "--version"], timeout=60)
        evidence_data["node_playwright"] = {
            "node_version": node_result.stdout.strip() if node_result.returncode == 0 else "NOT_FOUND",
            "node_exit_code": node_result.returncode,
            "playwright_version": playwright_result.stdout.strip() if playwright_result.returncode == 0 else "NOT_FOUND",
            "playwright_exit_code": playwright_result.returncode,
        }

        # Overall verdict
        checks_passed = all([
            docker_info.returncode == 0,
            _parse_version(evidence_data["docker"]["server_version"])[:2] >= MIN_DOCKER_VERSION,
            evidence_data["resources"]["ram_meets_minimum"],
            evidence_data["resources"]["cpus_meets_minimum"],
            evidence_data["ports"]["all_free"],
            node_result.returncode == 0,
            _parse_version(evidence_data["node_playwright"]["node_version"])[:2] >= MIN_NODE_VERSION,
            playwright_result.returncode == 0,
        ])
        evidence_data["overall_verdict"] = "pass" if checks_passed else "fail"

        # Emit evidence
        evidence_collector.emit_json(
            requirement_id="R1.1,R1.2,R1.3,R1.4,R1.5,R1.6",
            filename="01-preflight.json",
            data=evidence_data,
        )

        # This test always passes — it's for evidence collection.
        # The actual assertions are in the other test classes above.
        assert True
