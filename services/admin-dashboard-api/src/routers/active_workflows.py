"""``ActiveWorkflowsRouter`` (`platform gap-fill work` active workflow wiring).

**Covers 19.4** (and exposes data backing rule 19.1, rule 19.2).

Read-only endpoint that returns the active (``status='running'``)
workflow count for a single department, plus the configured cap.
The admin dashboard UI uses this to render a "8/10 paralel iş"
badge on the operations panel so operators can see at a glance how
saturated each dept is.

Endpoint
--------

``GET /api/v1/departments/{dept_id}/active-workflows``

Response (HTTP 200)::

    {
        "dept_id": "payment",
        "active": 3,
        "max_concurrent_workflows": 10,
        "saturation": 0.30,
        "source": "postgres"
    }

* ``active`` — current count of ``status='running'`` rows in
  ``automation.work_items`` for the dept.
* ``max_concurrent_workflows`` — the cap from
  ``departments.json``-mirrored ``config_json.max_concurrent_workflows``.
  ``null`` when the dept has no per-dept cap (the global license-tier
  cap from rule 16 still applies; this endpoint does not surface that).
* ``saturation`` — ``active / max_concurrent_workflows`` as a float
  in ``[0, 1]``, or ``null`` when the cap is unset. Rounded to 2
  decimals.
* ``source`` — currently always ``"postgres"`` because the Temporal
  Visibility API requires a ``DeptId`` search attribute that is not
  yet registered on the namespace. When that lands the helper will
  switch to ``"temporal"`` automatically; the field is exposed so
  the UI can reason about freshness without parsing logs.

The endpoint is gated by :func:`require_admin` because the count
combined with the cap is operational data we don't want exposed to
unauthenticated callers.

503 semantics
-------------

When ``app.state.pg_pool`` is ``None`` (ops pool wiring failed
during lifespan) the endpoint returns ``HTTP 503`` with
``reason="pg_pool_unavailable"`` — the same shape used by other
admin operations routers (costs, feature_flags). The caller can
retry once the operator restores the Postgres pool.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..auth.dependencies import AuthClaims, require_admin

__all__ = ["router"]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Path to ``departments.json``. Matches the resolution used by
#: :mod:`src.routers.departments` and :mod:`src.routers.capabilities`.
def _resolve_config_path(filename: str) -> Path:
    """Return ``config/<filename>`` across repo and container layouts."""

    resolved = Path(__file__).resolve()
    for parent in resolved.parents:
        config_dir = parent / "config"
        if config_dir.is_dir():
            return config_dir / filename
    return resolved.parents[min(2, len(resolved.parents) - 1)] / "config" / filename


_DEPARTMENTS_CONFIG_PATH = _resolve_config_path("departments.json")


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class ActiveWorkflowsResponse(BaseModel):
    """Payload returned by ``GET .../active-workflows``."""

    dept_id: str = Field(..., description="Department identifier.")
    active: int = Field(
        ..., ge=0, description="Currently running workflow count."
    )
    max_concurrent_workflows: int | None = Field(
        default=None,
        description=(
            "Per-dept cap from departments.json. ``null`` when "
            "unconfigured (global license-tier cap still applies)."
        ),
    )
    saturation: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "active / max_concurrent_workflows when both are set, "
            "otherwise null."
        ),
    )
    source: str = Field(
        ...,
        description=(
            "Counter source — \"postgres\" today; will become "
            "\"temporal\" once DeptId search attribute is registered."
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_dept_max_concurrent(dept_id: str) -> int | None:
    """Return the dept's ``max_concurrent_workflows`` from config.

    Reads ``departments.json`` directly. Returns ``None`` when the
    dept does not declare a cap or when the config file cannot be
    parsed (we degrade gracefully rather than 500'ing — the count
    is still useful even without a cap).
    """

    try:
        with open(_DEPARTMENTS_CONFIG_PATH, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "active_workflows: failed to read departments.json: %s", exc
        )
        return None

    departments = doc.get("departments", []) if isinstance(doc, dict) else []
    if not isinstance(departments, list):
        return None

    for dept in departments:
        if not isinstance(dept, dict):
            continue
        if dept.get("id") != dept_id:
            continue
        raw = dept.get("max_concurrent_workflows")
        if raw is None or isinstance(raw, bool):
            return None
        if not isinstance(raw, int):
            return None
        return raw if raw >= 1 else None
    return None


def _get_pool(request: Request) -> Any:
    """Resolve the asyncpg pool, raising 503 when missing."""

    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "reason": "pg_pool_unavailable",
            },
        )
    return pool


async def _count_running_workflows(pool: Any, dept_id: str) -> int:
    """Count ``automation.work_items`` rows in ``running`` status.

    Mirrors the SQL used by
    :mod:`automation_service.middleware.license_cap` and
    :mod:`automation_service.concurrency` so the three surfaces
    agree on what "active" means.
    """

    sql = """
        SELECT COUNT(*)::bigint AS n
        FROM automation.work_items
        WHERE department_id = $1
          AND status = 'running'
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, dept_id)
    return int(row["n"]) if row is not None else 0


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter(
    prefix="/api/v1/departments",
    tags=["active-workflows"],
)


@router.get(
    "/{dept_id}/active-workflows",
    response_model=ActiveWorkflowsResponse,
    summary="Active workflow count + concurrency cap for a department",
    dependencies=[Depends(require_admin)],
)
async def get_active_workflows(
    dept_id: str,
    request: Request,
    actor: AuthClaims = Depends(require_admin),
) -> ActiveWorkflowsResponse:
    """Return ``{active, max_concurrent_workflows, saturation}``.

    The dashboard polls this endpoint to render the per-dept
    concurrency badge (behavior 19.4).
    """

    pool = _get_pool(request)
    active = await _count_running_workflows(pool, dept_id)
    max_concurrent = _load_dept_max_concurrent(dept_id)

    saturation: float | None = None
    if max_concurrent is not None and max_concurrent > 0:
        saturation = round(active / max_concurrent, 2)

    return ActiveWorkflowsResponse(
        dept_id=dept_id,
        active=active,
        max_concurrent_workflows=max_concurrent,
        saturation=saturation,
        source="postgres",
    )
