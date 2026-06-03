"""Behavioral tests for readiness probe aggregation.

For any set of dependency probe results (each either reachable or
unreachable), the readiness endpoint SHALL return HTTP 200 with
``{"status": "ready"}`` if and only if ALL dependencies are reachable.
If ANY dependency is unreachable, it SHALL return HTTP 503 with
``failed_dependencies`` containing exactly the names of all unreachable
dependencies (no more, no less).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

# Ensure the readiness module is importable from the service source tree.
_SERVICE_SRC = (
    Path(__file__).resolve().parents[2]
    / "services"
    / "admin-dashboard-api"
    / "src"
)
if str(_SERVICE_SRC) not in sys.path:
    sys.path.insert(0, str(_SERVICE_SRC))

from lifecycle.readiness import DependencyProbeResult, check_readiness


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate a dependency name from a realistic set of infrastructure names.
_DEPENDENCY_NAMES = st.sampled_from(
    ["postgres", "redis", "temporal", "vault", "minio", "elasticsearch", "rabbitmq"]
)

# Generate a single DependencyProbeResult with random reachable state.
_PROBE_RESULT = st.builds(
    DependencyProbeResult,
    name=_DEPENDENCY_NAMES,
    reachable=st.booleans(),
    latency_ms=st.one_of(st.none(), st.floats(min_value=0.1, max_value=3000.0)),
)

# Generate a non-empty list of probe results with unique names.
_PROBE_RESULTS_LIST = st.lists(
    _PROBE_RESULT,
    min_size=1,
    max_size=7,
    unique_by=lambda r: r.name,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_probe_callable(result: DependencyProbeResult):
    """Wrap a DependencyProbeResult in a zero-arg async callable."""

    async def _probe() -> DependencyProbeResult:
        return result

    return _probe


# ---------------------------------------------------------------------------
# Readiness aggregation behavior
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(probe_results=_PROBE_RESULTS_LIST)
def test_readiness_probe_aggregation(probe_results: list[DependencyProbeResult]) -> None:
    """For any set of dependency probe results, check_readiness returns
    (True, {"status": "ready"}) iff ALL are reachable. Otherwise it
    returns (False, {"status": "not_ready", "failed_dependencies": [...]})
    with exactly the names of unreachable dependencies.
    """
    # Build list of async callables from the generated probe results
    dependencies = [_make_probe_callable(r) for r in probe_results]

    # Run the aggregation
    all_ready, details = asyncio.run(check_readiness(dependencies))

    # Compute expected outcome
    expected_failed = [r.name for r in probe_results if not r.reachable]
    all_reachable = len(expected_failed) == 0

    # all_ready iff every dependency is reachable
    assert all_ready == all_reachable, (
        f"Expected all_ready={all_reachable}, got all_ready={all_ready}. "
        f"Probe results: {probe_results!r}"
    )

    if all_reachable:
        # HTTP 200 case: status must be "ready"
        assert details == {"status": "ready"}, (
            f"Expected {{'status': 'ready'}}, got {details!r}"
        )
    else:
        # HTTP 503 case: status must be "not_ready" with exact failed list
        assert details["status"] == "not_ready", (
            f"Expected status='not_ready', got {details.get('status')!r}"
        )
        assert "failed_dependencies" in details, (
            f"Missing 'failed_dependencies' key in details: {details!r}"
        )
        # The failed list must contain exactly the unreachable dependency names
        assert sorted(details["failed_dependencies"]) == sorted(expected_failed), (
            f"Expected failed_dependencies={sorted(expected_failed)}, "
            f"got {sorted(details['failed_dependencies'])}. "
            f"Probe results: {probe_results!r}"
        )
