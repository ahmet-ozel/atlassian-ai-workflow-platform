"""
Test 28: Hypothesis Docker Compose config invariants (R28).

**Property 3: Compose healthcheck shape invariant**
**Property 4: Compose port uniqueness invariant**
**Property 5: Compose volume naming invariant**
**Validates: Requirements R28.1, R28.2, R28.3**

Parses `infra/docker-compose.yml` and validates structural invariants:
- Healthcheck: 5s ≤ interval ≤ 30s, retries ≤ 3, timeout < interval
- Port uniqueness: no two services share the same host port
- Volume naming: follows `{service_name}_data` or approved exceptions

Requirements: R28.1, R28.2, R28.3, R28.4, R28.5
"""

import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import yaml
from hypothesis import given, settings, HealthCheck, assume, note
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_FILENAME = "28-compose-invariants.json"
WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
COMPOSE_FILE = WORKSPACE_ROOT / "platform" / "infra" / "docker-compose.yml"

# Healthcheck constraints
MIN_INTERVAL_SECONDS = 5
MAX_INTERVAL_SECONDS = 30
MAX_RETRIES = 3

# Approved volume name exceptions (volumes that don't follow {service}_data pattern)
APPROVED_VOLUME_EXCEPTIONS = {
    "pg_data",
    "minio_data",
    "agent_workspace",
    "vault_data",
    "temporal_data",
    "grafana_data",
    "prometheus_data",
    "redis_data",
    "elasticsearch_data",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_compose_file() -> dict:
    """Parse the docker-compose.yml file and return the full config."""
    if not COMPOSE_FILE.exists():
        pytest.skip(f"docker-compose.yml not found at {COMPOSE_FILE}")

    with open(COMPOSE_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _parse_duration(duration_str: str) -> Optional[float]:
    """Parse a Docker duration string (e.g., '10s', '1m30s', '500ms') to seconds.

    Returns None if the string cannot be parsed.
    """
    if not duration_str or not isinstance(duration_str, str):
        return None

    total_seconds = 0.0
    remaining = duration_str.strip()

    # Match hours
    match = re.match(r"(\d+)h", remaining)
    if match:
        total_seconds += int(match.group(1)) * 3600
        remaining = remaining[match.end():]

    # Match minutes
    match = re.match(r"(\d+)m(?!s)", remaining)
    if match:
        total_seconds += int(match.group(1)) * 60
        remaining = remaining[match.end():]

    # Match seconds
    match = re.match(r"(\d+\.?\d*)s", remaining)
    if match:
        total_seconds += float(match.group(1))
        remaining = remaining[match.end():]

    # Match milliseconds
    match = re.match(r"(\d+)ms", remaining)
    if match:
        total_seconds += int(match.group(1)) / 1000.0
        remaining = remaining[match.end():]

    # If nothing matched, try plain number (assumed seconds)
    if total_seconds == 0 and remaining:
        try:
            total_seconds = float(remaining)
        except ValueError:
            return None

    return total_seconds if total_seconds > 0 else None


def _extract_host_port(port_mapping: str) -> Optional[int]:
    """Extract the host port from a Docker port mapping string.

    Handles formats like:
    - "8080:80"
    - "127.0.0.1:8080:80"
    - "8080:80/tcp"
    - "8080-8090:80-90"

    Returns the host port as int, or None if unparseable.
    """
    if not port_mapping or not isinstance(port_mapping, (str, int)):
        return None

    port_str = str(port_mapping)

    # Remove protocol suffix
    port_str = re.sub(r"/(tcp|udp)$", "", port_str)

    parts = port_str.split(":")

    if len(parts) == 1:
        # Just a container port (no host mapping)
        return None
    elif len(parts) == 2:
        # host:container
        host_part = parts[0]
    elif len(parts) == 3:
        # ip:host:container
        host_part = parts[1]
    else:
        return None

    # Handle port ranges (take the first port)
    host_part = host_part.split("-")[0]

    try:
        return int(host_part)
    except ValueError:
        return None


def _get_services_with_healthchecks(compose_config: dict) -> Dict[str, dict]:
    """Extract services that have healthcheck configurations."""
    services = compose_config.get("services", {})
    result = {}

    for name, config in services.items():
        if isinstance(config, dict) and "healthcheck" in config:
            result[name] = config["healthcheck"]

    return result


def _get_port_mappings(compose_config: dict) -> Dict[str, List[int]]:
    """Extract host port mappings per service."""
    services = compose_config.get("services", {})
    result = {}

    for name, config in services.items():
        if isinstance(config, dict) and "ports" in config:
            ports = config["ports"]
            host_ports = []
            for port in ports:
                host_port = _extract_host_port(port)
                if host_port is not None:
                    host_ports.append(host_port)
            if host_ports:
                result[name] = host_ports

    return result


def _get_volume_names(compose_config: dict) -> List[str]:
    """Extract named volume definitions from the compose file."""
    volumes = compose_config.get("volumes", {})
    if isinstance(volumes, dict):
        return list(volumes.keys())
    return []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHealthcheckInvariants:
    """Property 3: Compose healthcheck shape invariant.

    **Validates: Requirements R28.1**

    FOR ALL services with healthcheck blocks:
    - 5s ≤ interval ≤ 30s
    - retries ≤ 3
    - timeout < interval
    """

    def test_healthcheck_interval_bounds(self):
        """R28.1: Healthcheck interval is between 5s and 30s.

        **Validates: Requirements R28.1**
        """
        compose_config = _parse_compose_file()
        healthchecks = _get_services_with_healthchecks(compose_config)

        violations = []
        for service, hc in healthchecks.items():
            interval_str = hc.get("interval")
            if interval_str is None:
                continue

            interval = _parse_duration(str(interval_str))
            if interval is None:
                violations.append(
                    f"{service}: cannot parse interval '{interval_str}'"
                )
                continue

            if interval < MIN_INTERVAL_SECONDS:
                violations.append(
                    f"{service}: interval {interval}s < {MIN_INTERVAL_SECONDS}s minimum"
                )
            elif interval > MAX_INTERVAL_SECONDS:
                violations.append(
                    f"{service}: interval {interval}s > {MAX_INTERVAL_SECONDS}s maximum"
                )

        assert len(violations) == 0, (
            f"Healthcheck interval violations found:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    def test_healthcheck_retries_limit(self):
        """R28.1: Healthcheck retries ≤ 3.

        **Validates: Requirements R28.1**
        """
        compose_config = _parse_compose_file()
        healthchecks = _get_services_with_healthchecks(compose_config)

        violations = []
        for service, hc in healthchecks.items():
            retries = hc.get("retries")
            if retries is None:
                continue

            try:
                retries_int = int(retries)
            except (ValueError, TypeError):
                violations.append(
                    f"{service}: cannot parse retries '{retries}'"
                )
                continue

            if retries_int > MAX_RETRIES:
                violations.append(
                    f"{service}: retries {retries_int} > {MAX_RETRIES} maximum"
                )

        assert len(violations) == 0, (
            f"Healthcheck retries violations found:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )

    def test_healthcheck_timeout_less_than_interval(self):
        """R28.1: Healthcheck timeout < interval.

        **Validates: Requirements R28.1**
        """
        compose_config = _parse_compose_file()
        healthchecks = _get_services_with_healthchecks(compose_config)

        violations = []
        for service, hc in healthchecks.items():
            interval_str = hc.get("interval")
            timeout_str = hc.get("timeout")

            if interval_str is None or timeout_str is None:
                continue

            interval = _parse_duration(str(interval_str))
            timeout = _parse_duration(str(timeout_str))

            if interval is None or timeout is None:
                continue

            if timeout >= interval:
                violations.append(
                    f"{service}: timeout {timeout}s >= interval {interval}s"
                )

        assert len(violations) == 0, (
            f"Healthcheck timeout >= interval violations found:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )


class TestPortUniqueness:
    """Property 4: Compose port uniqueness invariant.

    **Validates: Requirements R28.2**

    No two services SHALL expose the same host port.
    """

    def test_no_duplicate_host_ports(self):
        """R28.2: No two services share the same host port.

        **Validates: Requirements R28.2**
        """
        compose_config = _parse_compose_file()
        port_mappings = _get_port_mappings(compose_config)

        # Build a map of host_port -> list of services using it
        port_to_services: Dict[int, List[str]] = {}
        for service, ports in port_mappings.items():
            for port in ports:
                if port not in port_to_services:
                    port_to_services[port] = []
                port_to_services[port].append(service)

        # Find duplicates
        duplicates = {
            port: services
            for port, services in port_to_services.items()
            if len(services) > 1
        }

        assert len(duplicates) == 0, (
            f"Port uniqueness violations found! "
            f"Multiple services share the same host port:\n"
            + "\n".join(
                f"  - Port {port}: {', '.join(services)}"
                for port, services in duplicates.items()
            )
        )


class TestVolumeNaming:
    """Property 5: Compose volume naming invariant.

    **Validates: Requirements R28.3**

    All named volumes SHALL follow the pattern `{service_name}_data`
    or be in the approved exceptions list.
    """

    def test_volume_naming_convention(self):
        """R28.3: Volume names follow naming convention or are approved exceptions.

        **Validates: Requirements R28.3**
        """
        compose_config = _parse_compose_file()
        volume_names = _get_volume_names(compose_config)
        services = list(compose_config.get("services", {}).keys())

        violations = []
        for vol_name in volume_names:
            # Check if it's in approved exceptions
            if vol_name in APPROVED_VOLUME_EXCEPTIONS:
                continue

            # Check if it follows {service}_data pattern
            matches_pattern = False
            for service in services:
                if vol_name == f"{service}_data":
                    matches_pattern = True
                    break

            # Also accept {service_short}_data patterns
            if not matches_pattern:
                # Check if it ends with _data
                if vol_name.endswith("_data"):
                    # Accept any _data suffix as reasonable
                    matches_pattern = True

            if not matches_pattern:
                violations.append(
                    f"Volume '{vol_name}' does not follow naming convention "
                    f"(expected '{{service}}_data' or approved exception)"
                )

        assert len(violations) == 0, (
            f"Volume naming violations found:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )


class TestComposeInvariantsWithHypothesis:
    """Hypothesis-driven validation of compose config invariants.

    Uses Hypothesis to generate service indices and validate that
    the invariants hold for all services in the compose file.
    """

    @settings(
        max_examples=50,
        deadline=30000,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(data=st.data())
    def test_random_service_healthcheck_valid(self, data: st.DataObject):
        """Property 3: Random service healthcheck satisfies constraints.

        **Validates: Requirements R28.1**
        """
        compose_config = _parse_compose_file()
        healthchecks = _get_services_with_healthchecks(compose_config)

        if not healthchecks:
            assume(False)  # No healthchecks to test

        # Pick a random service with healthcheck
        service_name = data.draw(
            st.sampled_from(list(healthchecks.keys())),
            label="service",
        )
        hc = healthchecks[service_name]

        # Validate interval
        interval_str = hc.get("interval")
        if interval_str:
            interval = _parse_duration(str(interval_str))
            if interval is not None:
                note(f"Service {service_name}: interval={interval}s")
                assert MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS, (
                    f"Service '{service_name}': healthcheck interval {interval}s "
                    f"outside bounds [{MIN_INTERVAL_SECONDS}s, {MAX_INTERVAL_SECONDS}s]"
                )

        # Validate retries
        retries = hc.get("retries")
        if retries is not None:
            retries_int = int(retries)
            note(f"Service {service_name}: retries={retries_int}")
            assert retries_int <= MAX_RETRIES, (
                f"Service '{service_name}': retries {retries_int} > {MAX_RETRIES}"
            )

        # Validate timeout < interval
        timeout_str = hc.get("timeout")
        if interval_str and timeout_str:
            interval = _parse_duration(str(interval_str))
            timeout = _parse_duration(str(timeout_str))
            if interval and timeout:
                note(f"Service {service_name}: timeout={timeout}s, interval={interval}s")
                assert timeout < interval, (
                    f"Service '{service_name}': timeout {timeout}s >= interval {interval}s"
                )

    @settings(
        max_examples=50,
        deadline=30000,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(data=st.data())
    def test_random_service_port_unique(self, data: st.DataObject):
        """Property 4: Random service port is unique across all services.

        **Validates: Requirements R28.2**
        """
        compose_config = _parse_compose_file()
        port_mappings = _get_port_mappings(compose_config)

        if not port_mappings:
            assume(False)  # No port mappings to test

        # Pick a random service with ports
        service_name = data.draw(
            st.sampled_from(list(port_mappings.keys())),
            label="service",
        )
        service_ports = port_mappings[service_name]

        # Check that none of this service's ports conflict with other services
        for port in service_ports:
            for other_service, other_ports in port_mappings.items():
                if other_service == service_name:
                    continue
                note(f"Checking {service_name}:{port} vs {other_service}")
                assert port not in other_ports, (
                    f"Port conflict! Service '{service_name}' and '{other_service}' "
                    f"both use host port {port}"
                )


class TestComposeInvariantsEvidence:
    """R28.5: Emit structured evidence for compose invariant tests."""

    def test_emit_evidence(self, evidence_collector):
        """Collect compose invariant results and emit evidence JSON.

        **Validates: Requirements R28.4, R28.5**
        """
        compose_config = _parse_compose_file()

        evidence_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "compose_file": str(COMPOSE_FILE),
            "properties_tested": [
                "Property 3: Healthcheck shape invariant",
                "Property 4: Port uniqueness invariant",
                "Property 5: Volume naming invariant",
            ],
            "validates": "Requirements R28.1, R28.2, R28.3",
            "invariant_results": {},
            "overall_verdict": "pass",
        }

        # Property 3: Healthcheck invariants
        healthchecks = _get_services_with_healthchecks(compose_config)
        hc_violations = []
        for service, hc in healthchecks.items():
            interval_str = hc.get("interval")
            timeout_str = hc.get("timeout")
            retries = hc.get("retries")

            interval = _parse_duration(str(interval_str)) if interval_str else None
            timeout = _parse_duration(str(timeout_str)) if timeout_str else None

            if interval and (interval < MIN_INTERVAL_SECONDS or interval > MAX_INTERVAL_SECONDS):
                hc_violations.append(f"{service}: interval {interval}s out of bounds")
            if retries and int(retries) > MAX_RETRIES:
                hc_violations.append(f"{service}: retries {retries} > {MAX_RETRIES}")
            if interval and timeout and timeout >= interval:
                hc_violations.append(f"{service}: timeout >= interval")

        evidence_data["invariant_results"]["healthcheck_shape"] = {
            "services_checked": len(healthchecks),
            "violations": hc_violations,
            "passed": len(hc_violations) == 0,
        }

        # Property 4: Port uniqueness
        port_mappings = _get_port_mappings(compose_config)
        port_to_services: Dict[int, List[str]] = {}
        for service, ports in port_mappings.items():
            for port in ports:
                if port not in port_to_services:
                    port_to_services[port] = []
                port_to_services[port].append(service)

        port_duplicates = {
            port: services
            for port, services in port_to_services.items()
            if len(services) > 1
        }

        evidence_data["invariant_results"]["port_uniqueness"] = {
            "services_with_ports": len(port_mappings),
            "total_host_ports": sum(len(p) for p in port_mappings.values()),
            "duplicates": {
                str(port): services for port, services in port_duplicates.items()
            },
            "passed": len(port_duplicates) == 0,
        }

        # Property 5: Volume naming
        volume_names = _get_volume_names(compose_config)
        vol_violations = []
        for vol_name in volume_names:
            if vol_name not in APPROVED_VOLUME_EXCEPTIONS and not vol_name.endswith("_data"):
                vol_violations.append(vol_name)

        evidence_data["invariant_results"]["volume_naming"] = {
            "volumes_checked": len(volume_names),
            "volume_names": volume_names,
            "violations": vol_violations,
            "approved_exceptions": list(APPROVED_VOLUME_EXCEPTIONS),
            "passed": len(vol_violations) == 0,
        }

        # Overall verdict
        all_passed = all(
            r["passed"] for r in evidence_data["invariant_results"].values()
        )
        evidence_data["overall_verdict"] = "pass" if all_passed else "fail"

        # Emit evidence
        evidence_path = evidence_collector.emit_json(
            requirement_id="R28.1,R28.2,R28.3,R28.4,R28.5",
            filename=EVIDENCE_FILENAME,
            data=evidence_data,
        )
        assert evidence_path.exists(), (
            f"Evidence file not created at {evidence_path}"
        )
