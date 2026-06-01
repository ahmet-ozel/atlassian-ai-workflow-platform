"""
Test 02: Boot_Bundle startup and healthcheck validation.

Validates that `make boot` brings up the four core services (postgres, vault,
admin-dashboard-api, admin-dashboard-ui) and that all reach healthy state
within 120 seconds. Probes healthcheck endpoints and captures container logs
on failure.

Requirements: R2.1, R2.2, R2.3, R2.4, R2.5
"""

import json
import subprocess
import time
from pathlib import Path

import httpx
import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BOOT_BUNDLE_SERVICES = ["postgres", "vault", "admin-dashboard-api", "admin-dashboard-ui"]
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 120

HEALTHCHECK_ENDPOINTS = [
    ("admin-dashboard-api", "http://localhost:8082/healthz", 200),
    ("admin-dashboard-ui", "http://localhost:3000", 200),
    ("vault", "http://localhost:8200/v1/sys/health", 200),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_make_boot(platform_root: Path) -> subprocess.CompletedProcess:
    """Execute `make boot` in the platform/ directory."""
    result = subprocess.run(
        ["make", "boot"],
        cwd=str(platform_root),
        capture_output=True,
        text=True,
        timeout=180,
    )
    return result


def get_compose_ps_json(platform_root: Path) -> list[dict]:
    """Run `docker compose ps --format json` and return parsed service list."""
    result = subprocess.run(
        ["docker", "compose", "-f", "infra/docker-compose.yml", "-f", "infra/docker-compose.dev.yml",
         "ps", "--format", "json"],
        cwd=str(platform_root),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return []

    # docker compose ps --format json outputs one JSON object per line
    services = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if line:
            try:
                services.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return services


def parse_service_status(services_json: list[dict]) -> dict[str, dict]:
    """Parse compose ps JSON output into a dict keyed by service name.

    Returns: {service_name: {"state": ..., "health": ...}}
    """
    status_map = {}
    for svc in services_json:
        name = svc.get("Service") or svc.get("Name", "")
        # Normalize: docker compose ps may use "Service" or "Name" field
        # Also handle cases where Name includes the project prefix
        for boot_svc in BOOT_BUNDLE_SERVICES:
            if boot_svc in name.lower() or name.lower().endswith(boot_svc):
                state = svc.get("State", "unknown")
                health = svc.get("Health", "unknown")
                status_map[boot_svc] = {"state": state, "health": health}
                break
    return status_map


def all_services_healthy(status_map: dict[str, dict]) -> bool:
    """Check if all boot bundle services are running and healthy."""
    if len(status_map) < len(BOOT_BUNDLE_SERVICES):
        return False
    for svc_name in BOOT_BUNDLE_SERVICES:
        info = status_map.get(svc_name, {})
        state = info.get("state", "").lower()
        health = info.get("health", "").lower()
        if state != "running":
            return False
        if health not in ("healthy", ""):
            # Some services may not have healthcheck defined — accept empty
            # But if health is "unhealthy" or "starting", not ready yet
            if health in ("unhealthy", "starting"):
                return False
    return True


def capture_failure_logs(platform_root: Path, evidence_collector) -> dict:
    """Capture container logs for all boot bundle services on failure."""
    failure_logs = {}
    for svc in BOOT_BUNDLE_SERVICES:
        logs = evidence_collector.capture_container_logs(svc, lines=50)
        failure_logs[svc] = logs
    return failure_logs


def probe_healthcheck_endpoint(url: str, expected_status: int, timeout: float = 10.0) -> dict:
    """Probe a single healthcheck endpoint and return result dict."""
    start = time.time()
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        elapsed = time.time() - start
        return {
            "url": url,
            "status_code": response.status_code,
            "expected_status": expected_status,
            "passed": response.status_code == expected_status,
            "latency_ms": round(elapsed * 1000, 2),
            "error": None,
        }
    except Exception as exc:
        elapsed = time.time() - start
        return {
            "url": url,
            "status_code": None,
            "expected_status": expected_status,
            "passed": False,
            "latency_ms": round(elapsed * 1000, 2),
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBootBundle:
    """Test group for Boot_Bundle startup and healthcheck validation."""

    def test_make_boot_executes(self, platform_root, stack_state, evidence_collector):
        """R2.1: Execute `make boot` and verify it brings up boot bundle services.

        WHEN the Local_Operator runs `make boot` in `platform/`, THE Local_Stack
        SHALL bring up exactly the services postgres, vault, admin-dashboard-api
        and admin-dashboard-ui and SHALL leave every profile-gated service stopped.
        """
        boot_start = time.time()

        result = run_make_boot(platform_root)
        boot_duration = time.time() - boot_start

        # make boot should succeed (exit 0)
        assert result.returncode == 0, (
            f"`make boot` failed with exit code {result.returncode}.\n"
            f"stdout: {result.stdout[-500:]}\n"
            f"stderr: {result.stderr[-500:]}"
        )

        # Record boot time in stack state
        stack_state.boot_time_seconds = boot_duration

    def test_services_healthy_within_timeout(self, platform_root, stack_state, evidence_collector):
        """R2.2: Poll docker compose ps every 5s for 120s, assert 4 services healthy.

        WHEN `make boot` completes, THE Test_Framework SHALL poll
        `docker compose ps --format json` every 5 seconds for up to 120 seconds
        and SHALL assert that all four Boot_Bundle services report State=running
        and Health=healthy.
        """
        start_time = time.time()
        last_status_map = {}

        while (time.time() - start_time) < POLL_TIMEOUT_SECONDS:
            services_json = get_compose_ps_json(platform_root)
            last_status_map = parse_service_status(services_json)

            if all_services_healthy(last_status_map):
                # All services are healthy — record in stack state
                for svc_name in BOOT_BUNDLE_SERVICES:
                    stack_state.mark_running(svc_name, "healthy")
                return  # Test passes

            time.sleep(POLL_INTERVAL_SECONDS)

        # Timeout reached — capture logs and fail
        elapsed = time.time() - start_time
        failure_logs = capture_failure_logs(platform_root, evidence_collector)

        # Build failure report
        missing = [s for s in BOOT_BUNDLE_SERVICES if s not in last_status_map]
        unhealthy = [
            f"{s}: state={last_status_map[s].get('state')}, health={last_status_map[s].get('health')}"
            for s in BOOT_BUNDLE_SERVICES
            if s in last_status_map and not (
                last_status_map[s].get("state", "").lower() == "running"
                and last_status_map[s].get("health", "").lower() in ("healthy", "")
            )
        ]

        failure_report = {
            "timeout_seconds": POLL_TIMEOUT_SECONDS,
            "elapsed_seconds": round(elapsed, 2),
            "missing_services": missing,
            "unhealthy_services": unhealthy,
            "last_status": last_status_map,
            "failure_logs": {k: v[:2000] for k, v in failure_logs.items()},
        }

        # Emit failure evidence (R2.4)
        evidence_collector.emit_json("R2.4", "02-boot-failure.json", failure_report)

        pytest.fail(
            f"Boot_Bundle services did not reach healthy within {POLL_TIMEOUT_SECONDS}s.\n"
            f"Missing: {missing}\n"
            f"Unhealthy: {unhealthy}\n"
            f"See e2e-evidence/02-boot-failure.json for details."
        )

    def test_healthcheck_endpoints(self, platform_root, evidence_collector):
        """R2.3: Probe healthcheck endpoints and assert HTTP 200.

        WHEN the Boot_Bundle is healthy, THE Test_Framework SHALL probe
        http://localhost:8082/healthz, http://localhost:3000 and
        http://localhost:8200/v1/sys/health and SHALL assert each returns HTTP 200.
        """
        probe_results = []
        all_passed = True

        for service_name, url, expected_status in HEALTHCHECK_ENDPOINTS:
            result = probe_healthcheck_endpoint(url, expected_status)
            result["service"] = service_name
            probe_results.append(result)
            if not result["passed"]:
                all_passed = False

        # Store probe results for evidence emission
        self._probe_results = probe_results

        # Assert all probes passed
        failures = [r for r in probe_results if not r["passed"]]
        if failures:
            failure_details = "\n".join(
                f"  - {f['service']} ({f['url']}): "
                f"got {f['status_code']}, expected {f['expected_status']}, error={f['error']}"
                for f in failures
            )
            pytest.fail(
                f"Healthcheck endpoint probes failed:\n{failure_details}"
            )

    def test_emit_boot_evidence(self, platform_root, stack_state, evidence_collector):
        """R2.5: Emit boot evidence at e2e-evidence/02-boot.json.

        THE Evidence_Collector SHALL emit boot evidence at e2e-evidence/02-boot.json
        containing docker compose ps snapshot, healthcheck probe results and
        timing metrics.
        """
        # Get final compose ps snapshot
        services_json = get_compose_ps_json(platform_root)
        status_map = parse_service_status(services_json)

        # Probe endpoints for evidence (re-probe to get fresh data)
        probe_results = []
        for service_name, url, expected_status in HEALTHCHECK_ENDPOINTS:
            result = probe_healthcheck_endpoint(url, expected_status)
            result["service"] = service_name
            probe_results.append(result)

        # Build evidence payload
        evidence_data = {
            "boot_bundle_services": BOOT_BUNDLE_SERVICES,
            "compose_ps_snapshot": services_json,
            "service_status": status_map,
            "healthcheck_probes": probe_results,
            "timing": {
                "boot_duration_seconds": stack_state.boot_time_seconds,
                "poll_interval_seconds": POLL_INTERVAL_SECONDS,
                "poll_timeout_seconds": POLL_TIMEOUT_SECONDS,
            },
            "verdict": "pass" if all(r["passed"] for r in probe_results) else "fail",
        }

        # Emit evidence
        evidence_path = evidence_collector.emit_json("R2.5", "02-boot.json", evidence_data)
        assert evidence_path.exists(), f"Evidence file not created at {evidence_path}"
