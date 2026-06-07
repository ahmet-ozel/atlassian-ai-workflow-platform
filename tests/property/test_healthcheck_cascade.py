"""invariant 11 - Healthcheck cascade aggregator deterministik.



For every Hypothesis-generated set of ``(per_service_status,
depends_on_dag)`` inputs,:class:`HealthcheckAggregator.aggregate`
satisfies:

(a) Output is the strict superset of the input keys (no service
 silently disappears).
(b) A service whose ``depends_on_services`` includes any unhealthy
 or unknown service is **never** reported as ``healthy`` -
 minimum it can be is ``degraded``.
(c) A service that is itself ``unhealthy`` keeps the ``unhealthy``
 label even when its deps are healthy (deeper failure wins).
(d) The function is deterministic: same input  same output.

The aggregator under test lives at
``platform/services/admin-dashboard-api/src/routers/healthcheck.py``. When that module is unavailable (eg. before
ships) the test skips with a precise reason.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent
_ADMIN_API_SRC = (
    _PLATFORM_ROOT / "services" / "admin-dashboard-api"
)
for path in (_ADMIN_API_SRC, _ADMIN_API_SRC / "src"):
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))


try:  # pragma: no cover - guarded import
    from src.routers.healthcheck import (  # type: ignore[import-not-found]
        HealthcheckAggregator,
        _apply_cascade,
    )
except ModuleNotFoundError as exc:  # pragma: no cover
    HealthcheckAggregator = None  # type: ignore[assignment,misc]
    _apply_cascade = None  # type: ignore[assignment,misc]
    _IMPORT_ERROR: str | None = str(exc)
else:
    _IMPORT_ERROR = None


pytestmark = pytest.mark.skipif(
    HealthcheckAggregator is None,
    reason=(
        "src.routers.healthcheck not yet importable "
        f"(implementation milestone is still [-]); error: {_IMPORT_ERROR!r}"
    ),
)


_STATUSES = st.sampled_from(["healthy", "unhealthy", "unknown"])
_SERVICE_NAMES = st.sampled_from(
    ["postgres", "vault", "temporal", "automation-service", "assistant-service"]
)


@st.composite
def _service_dag(draw):
    names = list(
        dict.fromkeys(draw(st.lists(_SERVICE_NAMES, min_size=1, max_size=5)))
    )
    statuses = {n: draw(_STATUSES) for n in names}
    deps = {
        n: draw(
            st.lists(
                st.sampled_from(names).filter(lambda d, n=n: d != n),
                min_size=0,
                max_size=min(3, len(names) - 1) if len(names) > 1 else 0,
                unique=True,
            )
        )
        for n in names
    }
    return statuses, deps


@settings(max_examples=200, deadline=None, suppress_health_check=(HealthCheck.too_slow,))
@given(_service_dag())
def test_cascade_invariants(payload):
    statuses, deps = payload
    out = _apply_cascade(statuses, deps)

    # (a) superset of keys
    assert set(out) == set(statuses)

    for name, status in out.items():
        # (b) cascade rule
        if any(statuses.get(d, "unknown") != "healthy" for d in deps.get(name, [])):
            assert status != "healthy", (
                f"{name} reported healthy despite unhealthy/unknown dep "
                f"(deps={deps[name]!r}, dep_status={[statuses.get(d) for d in deps[name]]!r})"
            )
        # (c) self-unhealthy survives
        if statuses[name] == "unhealthy":
            assert status == "unhealthy"


@settings(max_examples=80, deadline=None)
@given(_service_dag())
def test_cascade_is_deterministic(payload):
    statuses, deps = payload
    a = _apply_cascade(statuses, deps)
    b = _apply_cascade(statuses, deps)
    assert a == b
