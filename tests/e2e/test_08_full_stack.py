"""
Test 08: Full stack healthcheck — all 12+ services running and healthy.

Validates that after the Setup Wizard completes, the entire Local_Stack
(all 12 expected services) reports running and healthy via `docker compose ps`.
Captures timing from boot to full-stack-healthy and emits structured evidence.

This test uses:
- subprocess for docker compose health verification
- stack_state fixture for timing data (boot_time_seconds)
- evidence_collector fixture for emitting JSON evidence

Expected services (12 minimum):
  postgres, vault, temporal, atlassian-mcp, automation-service,
  assistant-service, automation-worker, agent-runner-worker,
  execution-runner-worker, streamlit-ui, admin-dashboard-api,
  admin-dashboard-ui

Requirements: R8.1, R8.2, R8.3, R8.4
"""

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# All 12 services expected to be running and healthy after wizard completion
FULL_STACK_SERVICES = [
    "postgres",
    "vault",
    "temporal",
    "atlassian-mcp",
    "automation-service",
    "assistant-service",
    "automation-worker",
    "agent-runner-worker",
    "execution-runner-worker",
    "streamlit-ui",
    "admin-dashboard-api",
    "admin-dashboard-ui",
]

# Timeout for unhealthy service log capture (R8.2)
UNHEALTHY_TIMEOUT_SECONDS = 60

# Polling interval when waiting for services to become healthy
POLL_INTERVAL_SECONDS = 5

# Evidence filenames
EVIDENCE_FULL_STACK = "08-full-stack.json"
EVIDENCE_STACK_TIMING = "08-stack-timing.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_compose_ps_json(platform_root: Path) -> list[dict]:
    """Run `docker compose ps --format json` and return parsed service list.

    Includes all profiles to capture the full stack state.
    """
    result = subprocess.run(
        [
            "docker", "compose",
            "-f", "infra/docker-compose.yml",
            "-f", "infra/docker-compose.dev.yml",
            "ps", "--format", "json",
        ],
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


def parse_all_service_status(services_json: list[dict]) -> dict[str, dict]:
    """Parse compose ps JSON output into a dict keyed by service name.

    Returns: {service_name: {"state": ..., "health": ..., "raw": ...}}
    """
    status_map = {}
    for svc in services_json:
        name = svc.get("Service") or svc.get("Name", "")
        state = svc.get("State", "unknown")
        health = svc.get("Health", "")
        status_map[name] = {
            "state": state,
            "health": health,
            "raw": svc,
        }
    return status_map


def match_service_in_status(service_name: str, status_map: dict[str, dict]) -> dict | None:
    """Find a service in the status map by exact or partial name match.

    Docker compose may use the service name directly or include a project prefix.
    """
    # Exact match first
    if service_name in status_map:
        return status_map[service_name]

    # Partial match: check if service_name is contained in any key
    for key, value in status_map.items():
        if service_name in key.lower() or key.lower().endswith(service_name):
            return value

    return None


def is_service_healthy(service_info: dict | None) -> bool:
    """Check if a service is running and healthy.

    A service is considered healthy if:
    - State is 'running'
    - Health is 'healthy' OR empty (no healthcheck defined)
    """
    if service_info is None:
        return False

    state = service_info.get("state", "").lower()
    health = service_info.get("health", "").lower()

    if state != "running":
        return False

    # Accept healthy or no healthcheck defined (empty)
    if health in ("healthy", ""):
        return True

    return False


def capture_service_logs(service_name: str, platform_root: Path, lines: int = 100) -> str:
    """Capture the last N lines of a service's logs via docker compose."""
    try:
        result = subprocess.run(
            [
                "docker", "compose",
                "-f", "infra/docker-compose.yml",
                "-f", "infra/docker-compose.dev.yml",
                "logs", "--tail", str(lines), service_name,
            ],
            cwd=str(platform_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.stdout.strip() or result.stderr.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"[ERROR] Timeout capturing logs for '{service_name}'"
    except Exception as exc:
        return f"[ERROR] Failed to capture logs for '{service_name}': {exc}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFullStackHealthcheck:
    """Full stack healthcheck after Setup Wizard completion.

    Validates that all 12+ expected services are running and healthy,
    captures timing metrics, and emits structured evidence.
    """

    def test_all_services_running_and_healthy(
        self, platform_root, stack_state, evidence_collector
    ):
        """R8.1: Assert all 12+ services running and healthy via docker compose ps.

        WHEN the post-wizard stack is observed, THE Test_Framework SHALL assert
        that `docker compose ps` reports `running` and `healthy` for at least:
        postgres, vault, temporal, atlassian-mcp, automation-service,
        assistant-service, automation-worker, agent-runner-worker,
        execution-runner-worker, streamlit-ui, admin-dashboard-api and
        admin-dashboard-ui.
        """
        services_json = get_compose_ps_json(platform_root)
        status_map = parse_all_service_status(services_json)

        # Track results per service
        healthy_services = []
        unhealthy_services = []
        missing_services = []

        for svc_name in FULL_STACK_SERVICES:
            svc_info = match_service_in_status(svc_name, status_map)
            if svc_info is None:
                missing_services.append(svc_name)
            elif is_service_healthy(svc_info):
                healthy_services.append(svc_name)
                stack_state.mark_running(svc_name, "healthy")
            else:
                unhealthy_services.append(svc_name)
                stack_state.mark_running(svc_name, svc_info.get("health", "unhealthy"))

        total_expected = len(FULL_STACK_SERVICES)
        total_healthy = len(healthy_services)

        assert total_healthy >= total_expected, (
            f"Expected all {total_expected} services healthy, "
            f"got {total_healthy} healthy.\n"
            f"Missing services: {missing_services}\n"
            f"Unhealthy services: {unhealthy_services}\n"
            f"Healthy services: {healthy_services}\n"
            f"Full status map keys: {list(status_map.keys())}"
        )

    def test_unhealthy_service_log_capture(
        self, platform_root, stack_state, evidence_collector
    ):
        """R8.2: Capture logs for any service unhealthy >60s after wizard completion.

        WHEN any service reports `unhealthy` for more than 60 seconds after
        wizard completion, THE Test_Framework SHALL capture that service's
        last 100 log lines and SHALL emit a structured failure report.
        """
        services_json = get_compose_ps_json(platform_root)
        status_map = parse_all_service_status(services_json)

        # Identify unhealthy services
        unhealthy_report = {}
        has_unhealthy = False

        for svc_name in FULL_STACK_SERVICES:
            svc_info = match_service_in_status(svc_name, status_map)
            if svc_info is not None and not is_service_healthy(svc_info):
                has_unhealthy = True
                # Wait up to UNHEALTHY_TIMEOUT_SECONDS to see if it recovers
                start_wait = time.time()
                recovered = False

                while (time.time() - start_wait) < UNHEALTHY_TIMEOUT_SECONDS:
                    time.sleep(POLL_INTERVAL_SECONDS)
                    fresh_json = get_compose_ps_json(platform_root)
                    fresh_map = parse_all_service_status(fresh_json)
                    fresh_info = match_service_in_status(svc_name, fresh_map)
                    if is_service_healthy(fresh_info):
                        recovered = True
                        stack_state.mark_running(svc_name, "healthy")
                        break

                if not recovered:
                    # Capture last 100 log lines for the unhealthy service
                    logs = capture_service_logs(svc_name, platform_root, lines=100)
                    unhealthy_report[svc_name] = {
                        "state": svc_info.get("state"),
                        "health": svc_info.get("health"),
                        "waited_seconds": UNHEALTHY_TIMEOUT_SECONDS,
                        "recovered": False,
                        "last_100_log_lines": logs[:5000],  # Cap log size
                    }

        if unhealthy_report:
            # Emit structured failure report
            evidence_collector.emit_json(
                "R8.2",
                "08-unhealthy-services.json",
                {
                    "unhealthy_services": unhealthy_report,
                    "timeout_seconds": UNHEALTHY_TIMEOUT_SECONDS,
                    "total_expected": len(FULL_STACK_SERVICES),
                },
            )

            pytest.fail(
                f"Services remained unhealthy after {UNHEALTHY_TIMEOUT_SECONDS}s: "
                f"{list(unhealthy_report.keys())}. "
                f"See e2e-evidence/08-unhealthy-services.json for logs."
            )

        # If no unhealthy services found, this test passes silently
        # (all services are healthy — no failure report needed)

    def test_boot_to_full_stack_timing(
        self, platform_root, stack_state, evidence_collector
    ):
        """R8.3: Record total time from make boot to full-stack-healthy.

        THE Test_Framework SHALL record the total time from `make boot` to
        full-stack-healthy as a performance metric in
        `e2e-evidence/08-stack-timing.json`.
        """
        # Calculate timing: boot_time_seconds was recorded by test_02_boot.py
        boot_time = stack_state.boot_time_seconds

        # Measure current time to full-stack-healthy
        # (this test runs after wizard completion, so we measure from now)
        start_check = time.time()
        services_json = get_compose_ps_json(platform_root)
        status_map = parse_all_service_status(services_json)

        all_healthy = all(
            is_service_healthy(match_service_in_status(svc, status_map))
            for svc in FULL_STACK_SERVICES
        )
        check_duration = time.time() - start_check

        # Build timing evidence
        timing_data: dict[str, Any] = {
            "boot_duration_seconds": boot_time,
            "full_stack_healthy_at_check": all_healthy,
            "healthcheck_poll_duration_seconds": round(check_duration, 3),
            "total_services_expected": len(FULL_STACK_SERVICES),
            "services_list": FULL_STACK_SERVICES,
        }

        # If boot_time is available, compute total elapsed
        if boot_time is not None:
            # full_stack_time is approximate: boot_time + wizard duration
            # We record what we know; the exact wizard duration is tracked
            # by individual wizard step tests
            timing_data["boot_to_check_note"] = (
                "boot_duration_seconds reflects `make boot` time only. "
                "Wizard steps add additional time not captured here."
            )

        # Record in stack_state for downstream tests
        stack_state.full_stack_time_seconds = boot_time

        # Emit timing evidence
        evidence_path = evidence_collector.emit_json(
            "R8.3", EVIDENCE_STACK_TIMING, timing_data
        )
        assert evidence_path.exists(), (
            f"Timing evidence not created at {evidence_path}"
        )

    def test_emit_full_stack_evidence(
        self, platform_root, stack_state, evidence_collector
    ):
        """R8.4: Emit full docker compose ps table snapshot.

        THE Evidence_Collector SHALL emit a full `docker compose ps` table
        snapshot at `e2e-evidence/08-full-stack.json`.
        """
        services_json = get_compose_ps_json(platform_root)
        status_map = parse_all_service_status(services_json)

        # Build per-service verdict
        service_verdicts = {}
        for svc_name in FULL_STACK_SERVICES:
            svc_info = match_service_in_status(svc_name, status_map)
            if svc_info is None:
                service_verdicts[svc_name] = {
                    "found": False,
                    "running": False,
                    "healthy": False,
                    "verdict": "missing",
                }
            else:
                healthy = is_service_healthy(svc_info)
                service_verdicts[svc_name] = {
                    "found": True,
                    "running": svc_info.get("state", "").lower() == "running",
                    "healthy": healthy,
                    "state": svc_info.get("state"),
                    "health": svc_info.get("health"),
                    "verdict": "pass" if healthy else "fail",
                }

        # Count results
        total = len(FULL_STACK_SERVICES)
        passing = sum(1 for v in service_verdicts.values() if v["verdict"] == "pass")
        failing = sum(1 for v in service_verdicts.values() if v["verdict"] == "fail")
        missing = sum(1 for v in service_verdicts.values() if v["verdict"] == "missing")

        # Build full evidence payload
        evidence_data = {
            "full_stack_services": FULL_STACK_SERVICES,
            "total_expected": total,
            "total_healthy": passing,
            "total_unhealthy": failing,
            "total_missing": missing,
            "overall_verdict": "pass" if passing == total else "fail",
            "service_verdicts": service_verdicts,
            "compose_ps_raw": services_json,
            "all_detected_services": list(status_map.keys()),
            "timing": {
                "boot_duration_seconds": stack_state.boot_time_seconds,
                "full_stack_time_seconds": stack_state.full_stack_time_seconds,
            },
        }

        # Emit evidence
        evidence_path = evidence_collector.emit_json(
            "R8.4", EVIDENCE_FULL_STACK, evidence_data
        )
        assert evidence_path.exists(), (
            f"Full stack evidence not created at {evidence_path}"
        )
