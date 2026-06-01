# Feature: vps-e2e-deployment-test, Property 4: Compose healthcheck shape
"""Property test for Compose healthcheck shape invariants.

**Validates: Requirements 9.2**

Every Compose service in ``infra/docker-compose.yml`` that declares a
custom ``healthcheck:`` block MUST satisfy the following invariants:

- ``interval`` parses to a seconds value in ``[5, 30]``
- ``retries ≤ 3``
- ``timeout < interval`` (timeout defaults to 30s if absent)

These constraints ensure that healthchecks are responsive enough to
detect failures quickly but not so aggressive that they overwhelm
services during startup or under load.

Implementation notes
--------------------

* The Compose YAML uses anchor / merge syntax (``<<: *http-healthcheck``)
  which ``yaml.safe_load`` resolves into plain dicts; we exploit that
  to treat ``healthcheck`` uniformly across services.
* Duration strings like ``10s``, ``1m30s``, ``500ms`` are parsed via
  a regex-based accumulator matching Compose's duration format.
* Services without a ``healthcheck:`` block are skipped — only custom
  healthcheck configurations are validated.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path bootstrapping
# ---------------------------------------------------------------------------

_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from conftest import WORKSPACE_ROOT  # noqa: E402

# ---------------------------------------------------------------------------
# Compose document loading
# ---------------------------------------------------------------------------

COMPOSE_PATH: Path = WORKSPACE_ROOT / "infra" / "docker-compose.yml"


def _load_compose() -> dict[str, Any]:
    """Parse ``infra/docker-compose.yml`` with ``yaml.safe_load``.

    YAML anchor / merge keys (``<<: *http-healthcheck``) are resolved
    into plain dicts by ``safe_load``, so downstream code can treat
    ``healthcheck`` uniformly across services.
    """
    assert COMPOSE_PATH.is_file(), (
        f"Compose file missing at {COMPOSE_PATH.relative_to(WORKSPACE_ROOT)}"
    )
    with COMPOSE_PATH.open("r", encoding="utf-8") as fh:
        document = yaml.safe_load(fh)
    assert isinstance(document, dict), (
        f"docker-compose.yml must parse to a mapping; got {type(document).__name__}"
    )
    return document


# ---------------------------------------------------------------------------
# Duration parsing helper
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)(?P<unit>ms|s|m|h)", re.IGNORECASE
)
_DURATION_UNIT_SECONDS: dict[str, float] = {
    "ms": 1e-3,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
}


def _parse_duration_seconds(value: str | int | float) -> float:
    """Parse a Compose duration string (e.g. ``"10s"``) to seconds.

    Numeric inputs are interpreted as already-in-seconds (Compose's
    schema accepts integer seconds in some contexts). Composite values
    like ``1m30s`` are accumulated.
    """
    if isinstance(value, (int, float)):
        return float(value)

    matches = list(_DURATION_RE.finditer(str(value)))
    assert matches, f"Could not parse Compose duration: {value!r}"
    total = 0.0
    for match in matches:
        total += float(match.group("value")) * _DURATION_UNIT_SECONDS[
            match.group("unit").lower()
        ]
    return total


# ---------------------------------------------------------------------------
# Service discovery — collect services with custom healthcheck blocks
# ---------------------------------------------------------------------------


def _services_with_healthcheck() -> list[str]:
    """Return names of Compose services that declare a custom healthcheck."""
    doc = _load_compose()
    services = doc.get("services") or {}
    result = []
    for name, svc in services.items():
        if isinstance(svc, dict) and isinstance(svc.get("healthcheck"), dict):
            result.append(name)
    return sorted(result)


# Build the list once at module import time for Hypothesis sampling
_HEALTHCHECK_SERVICES: list[str] = _services_with_healthcheck()


# ---------------------------------------------------------------------------
# Property 4: Compose healthcheck shape
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(service_name=st.sampled_from(_HEALTHCHECK_SERVICES))
def test_healthcheck_interval_in_bounds(service_name: str) -> None:
    """Property 4 (a) — healthcheck interval ∈ [5s, 30s].

    **Validates: Requirements 9.2**

    Every Compose service with a custom ``healthcheck:`` block MUST
    have an ``interval`` that parses to a value in [5, 30] seconds.
    """
    doc = _load_compose()
    service = doc["services"][service_name]
    healthcheck = service["healthcheck"]

    interval_raw = healthcheck.get("interval")
    assert interval_raw is not None, (
        f"{service_name}: healthcheck.interval is missing "
        f"(Req 9.2, Property 4)"
    )

    interval_sec = _parse_duration_seconds(interval_raw)
    assert 5.0 <= interval_sec <= 30.0, (
        f"{service_name}: healthcheck.interval must be in [5s, 30s] "
        f"(Req 9.2, Property 4); got {interval_raw!r} = {interval_sec}s"
    )


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(service_name=st.sampled_from(_HEALTHCHECK_SERVICES))
def test_healthcheck_retries_bounded(service_name: str) -> None:
    """Property 4 (b) — healthcheck retries ≤ 3.

    **Validates: Requirements 9.2**

    Every Compose service with a custom ``healthcheck:`` block MUST
    have ``retries ≤ 3``.
    """
    doc = _load_compose()
    service = doc["services"][service_name]
    healthcheck = service["healthcheck"]

    retries = healthcheck.get("retries")
    assert retries is not None, (
        f"{service_name}: healthcheck.retries is missing "
        f"(Req 9.2, Property 4)"
    )
    assert isinstance(retries, int), (
        f"{service_name}: healthcheck.retries must be an int "
        f"(Req 9.2, Property 4); got {type(retries).__name__}"
    )
    assert retries <= 3, (
        f"{service_name}: healthcheck.retries must be ≤ 3 "
        f"(Req 9.2, Property 4); got {retries}"
    )


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(service_name=st.sampled_from(_HEALTHCHECK_SERVICES))
def test_healthcheck_timeout_less_than_interval(service_name: str) -> None:
    """Property 4 (c) — healthcheck timeout < interval.

    **Validates: Requirements 9.2**

    Every Compose service with a custom ``healthcheck:`` block MUST
    have ``timeout < interval``. If ``timeout`` is not explicitly set,
    Docker's default of 30s is assumed.
    """
    doc = _load_compose()
    service = doc["services"][service_name]
    healthcheck = service["healthcheck"]

    interval_raw = healthcheck.get("interval")
    assert interval_raw is not None, (
        f"{service_name}: healthcheck.interval is missing "
        f"(Req 9.2, Property 4)"
    )
    interval_sec = _parse_duration_seconds(interval_raw)

    # Docker default timeout is 30s if not specified
    timeout_raw = healthcheck.get("timeout", "30s")
    timeout_sec = _parse_duration_seconds(timeout_raw)

    assert timeout_sec < interval_sec, (
        f"{service_name}: healthcheck.timeout ({timeout_raw} = {timeout_sec}s) "
        f"must be less than interval ({interval_raw} = {interval_sec}s) "
        f"(Req 9.2, Property 4)"
    )


# ---------------------------------------------------------------------------
# Parametrized exhaustive check (covers all services deterministically)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("service_name", _HEALTHCHECK_SERVICES)
def test_healthcheck_shape_all_invariants(service_name: str) -> None:
    """Property 4 (combined) — all healthcheck invariants in one pass.

    **Validates: Requirements 9.2**

    Deterministic parametrized test ensuring every service with a
    custom healthcheck satisfies all three invariants:
    - 5 ≤ interval ≤ 30 seconds
    - retries ≤ 3
    - timeout < interval
    """
    doc = _load_compose()
    service = doc["services"][service_name]
    healthcheck = service["healthcheck"]

    # --- interval ∈ [5s, 30s] ---
    interval_raw = healthcheck.get("interval")
    assert interval_raw is not None, (
        f"{service_name}: healthcheck.interval is missing"
    )
    interval_sec = _parse_duration_seconds(interval_raw)
    assert 5.0 <= interval_sec <= 30.0, (
        f"{service_name}: healthcheck.interval must be in [5s, 30s]; "
        f"got {interval_raw!r} = {interval_sec}s"
    )

    # --- retries ≤ 3 ---
    retries = healthcheck.get("retries")
    assert retries is not None, (
        f"{service_name}: healthcheck.retries is missing"
    )
    assert isinstance(retries, int), (
        f"{service_name}: healthcheck.retries must be int; "
        f"got {type(retries).__name__}"
    )
    assert retries <= 3, (
        f"{service_name}: healthcheck.retries must be ≤ 3; got {retries}"
    )

    # --- timeout < interval ---
    timeout_raw = healthcheck.get("timeout", "30s")
    timeout_sec = _parse_duration_seconds(timeout_raw)
    assert timeout_sec < interval_sec, (
        f"{service_name}: healthcheck.timeout ({timeout_raw} = {timeout_sec}s) "
        f"must be < interval ({interval_raw} = {interval_sec}s)"
    )
