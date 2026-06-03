"""``HealthcheckAggregator`` cascade router.

Aggregates the per-service healthcheck status into a single
``CascadeReport`` consumed by the admin-dashboard ``/services``
panel. The cascade rule:

* When ``services.manifest.json`` declares ``A.depends_on_services``
  contains ``B`` and ``B`` is unhealthy, ``A`` is reported as
  ``degraded`` (even if its own probe came back ``healthy``).
* ``unknown`` (probe failed / timed out) propagates the same as
  ``unhealthy``.

The aggregator does not write audit events itself. It only computes
the cascade and returns the latest snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from fastapi import APIRouter, Depends, Request

from ..auth.dependencies import require_admin

__all__ = ["router", "HealthcheckAggregator", "CascadeReport"]


@dataclass(frozen=True, slots=True)
class CascadeReport:
    """Snapshot returned by :meth:`HealthcheckAggregator.aggregate`.

    Attributes:
        services: Mapping of service name to status string
            (``healthy`` / ``unhealthy`` / ``unknown`` / ``degraded``).
        transitions: List of recent ``(service, from, to, at)``
            transitions surfaced by the audit log.
    """

    services: Mapping[str, str]
    transitions: tuple[Mapping[str, str], ...] = ()


def _apply_cascade(
    statuses: Mapping[str, str],
    depends_on: Mapping[str, Sequence[str]],
) -> dict[str, str]:
    """Compute the cascade-adjusted status map.

    For every service ``A`` whose dependencies include any service
    ``B`` with ``status != "healthy"``, ``A`` is downgraded to
    ``"degraded"`` (unless ``A`` is itself already ``"unhealthy"``,
    in which case the deeper failure wins).
    """

    cascade: dict[str, str] = dict(statuses)
    for name, deps in depends_on.items():
        own = cascade.get(name, "unknown")
        if own == "unhealthy":
            continue
        for dep in deps:
            if cascade.get(dep, "unknown") != "healthy":
                cascade[name] = "degraded"
                break
    return cascade


class HealthcheckAggregator:
    """Compose a :class:`CascadeReport` from the foundation probe state.

    The aggregator is dependency-injected with two collaborators:

    * ``probe_state`` — a callable returning ``Mapping[str, str]``
      from service name to raw status. Production wires this to
      :class:`HealthProbe`; tests inject a dict.
    * ``manifest_loader`` — a callable returning the parsed
      ``services.manifest.json`` dict so the cascade rule can read
      ``depends_on_services``.

    Both are intentionally tiny callables so unit tests can drive
    the aggregator with hand-rolled data.
    """

    def __init__(
        self,
        *,
        probe_state,
        manifest_loader,
        recent_transitions=lambda: (),
    ) -> None:
        self._probe_state = probe_state
        self._manifest_loader = manifest_loader
        self._recent_transitions = recent_transitions

    def aggregate(self) -> CascadeReport:
        statuses = dict(self._probe_state())
        manifest = self._manifest_loader() or {}
        deps_map: dict[str, Sequence[str]] = {}
        for entry in manifest.get("entries", []):
            name = entry.get("name")
            if name:
                deps_map[name] = list(entry.get("depends_on_services", []))
        cascade = _apply_cascade(statuses, deps_map)
        return CascadeReport(
            services=cascade,
            transitions=tuple(self._recent_transitions()),
        )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/admin/healthcheck", tags=["healthcheck"])


def _get_aggregator(request: Request) -> HealthcheckAggregator:
    agg = getattr(request.app.state, "healthcheck_aggregator", None)
    if agg is None:
        raise RuntimeError(
            "HealthcheckAggregator not wired on app.state; lifespan "
            "must populate app.state.healthcheck_aggregator."
        )
    return agg


@router.get("/aggregate", dependencies=[Depends(require_admin)])
async def aggregate_endpoint(request: Request) -> dict:
    agg = _get_aggregator(request)
    report = agg.aggregate()
    return {
        "services": dict(report.services),
        "transitions": [dict(t) for t in report.transitions],
    }
