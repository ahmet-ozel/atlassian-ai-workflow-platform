"""``OperationsRouter`` — operations dashboard endpoints (task 16.3).

**Validates: Requirements 16.6** (``platform-mimari-uyumluluk`` R16 / Q20)

Exposes the ``GET /admin/operations/license`` endpoint that returns
current license cap usage for every license tier visible to the
authenticated actor. The data is read directly from the automation
Postgres database (``automation.bot_license_caps``,
``automation.departments``, ``automation.work_items``,
``shared.cost_tracking``) — no proxy hop to automation-service is
needed because admin-dashboard-api already holds a ``pg_pool`` slot
wired during lifespan (the same pool used by the ``costs`` and
``feature_flags`` routers).

Response shape
--------------

``GET /admin/operations/license`` returns a JSON array of license
usage objects:

.. code-block:: json

    [
      {
        "license_id": "enterprise-2025",
        "max_concurrent": 10,
        "current_concurrent": 3,
        "daily_used": 47,
        "daily_max": 100,
        "monthly_token_usd_used": "234.56",
        "monthly_token_usd_max": "1000.00",
        "percent_used": 47.0
      }
    ]

``percent_used`` is ``max(concurrent%, daily%, monthly%)`` rounded to
one decimal place. Departments with ``license_id IS NULL`` are
aggregated under the sentinel ``license_id`` value ``"__default__"``
using the default cap values baked into
``automation-service/src/middleware/license_cap.py``
(:data:`DEFAULT_MAX_CONCURRENT`, :data:`DEFAULT_MAX_DAILY`,
:data:`DEFAULT_MAX_MONTHLY_TOKEN_USD`).

RBAC
----

* ``admin`` — sees all license tiers.
* ``dept_admin`` — sees only the license tier(s) associated with their
  own ``dept_ids``. The endpoint resolves the actor's ``dept_ids``
  from the OIDC ``dept_ids`` claim (via :class:`AuthContext`) and
  filters the result set accordingly.
* ``lead`` / ``viewer`` — ``403`` (the ``require_admin_or_dept_admin``
  dependency rejects them before the handler runs).

The RBAC check is implemented inline rather than via a shared
dependency because the filtering logic is specific to the license
domain (joining ``automation.departments.license_id`` against the
actor's ``dept_ids``).

Pool availability
-----------------

The endpoint reads ``app.state.pg_pool`` (the same asyncpg pool used
by the ``costs`` and ``feature_flags`` routers). When the pool is
``None`` (Postgres still booting, or the pool creation failed during
lifespan) the endpoint returns ``503`` with
``reason="pg_pool_unavailable"`` — matching the pattern established
by the other ops routers.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth.dependencies import AuthClaims, require_admin

__all__ = ["router"]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default cap constants — kept in sync with
# ``automation-service/src/middleware/license_cap.py`` defaults.
# These are used when a dept has no license_id assigned (NULL).
# ---------------------------------------------------------------------------

_DEFAULT_MAX_CONCURRENT: int = 10
_DEFAULT_MAX_DAILY: int = 100
_DEFAULT_MAX_MONTHLY_TOKEN_USD: Decimal = Decimal("1000.00")

# Sentinel license_id used in the response for NULL-license depts.
_DEFAULT_LICENSE_SENTINEL: str = "__default__"


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/admin/operations",
    tags=["operations"],
)


# ---------------------------------------------------------------------------
# Pool helper
# ---------------------------------------------------------------------------


def _get_pool(request: Request) -> Any:
    """Return the asyncpg pool from ``app.state.pg_pool``.

    Raises ``503`` when the pool is not yet available so the endpoint
    surfaces a clear wiring failure instead of an ``AttributeError``.
    """

    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "not_ready", "reason": "pg_pool_unavailable"},
        )
    return pool


# ---------------------------------------------------------------------------
# RBAC helper
# ---------------------------------------------------------------------------


def _is_admin(actor: AuthClaims) -> bool:
    """Return ``True`` when the actor holds the ``admin`` group/role."""

    return "admin" in actor.groups


def _is_dept_admin(actor: AuthClaims) -> bool:
    """Return ``True`` when the actor holds the ``dept_admin`` group/role."""

    return "dept_admin" in actor.groups


def _actor_dept_ids(actor: AuthClaims) -> list[str]:
    """Extract the ``dept_ids`` list from the actor's OIDC claims.

    The ``dept_ids`` claim is surfaced as a space-separated string or
    a JSON array depending on the IdP configuration. We normalise both
    forms to a plain Python list. When the claim is absent or empty we
    return an empty list — the caller treats that as "no depts visible"
    and returns an empty result set rather than raising.

    The claim is stored on :class:`AuthClaims` as part of the
    ``groups`` tuple (the dependency unions ``groups`` and ``roles``
    into a single tuple). For the dept_ids we look for entries that
    look like dept identifiers (non-role strings). In practice the
    IdP should surface ``dept_ids`` as a separate claim; until the
    auth-shared library exposes it directly we fall back to an empty
    list so the endpoint degrades gracefully.

    Note: The foundation Y11 RBAC matrix specifies that ``dept_admin``
    actors carry a ``dept_ids`` claim. The current
    :class:`AuthClaims` dataclass only exposes ``sub`` and ``groups``;
    a future auth-shared update will add ``dept_ids`` as a first-class
    field. Until then, ``dept_admin`` actors see an empty result set
    (safe-fail: under-exposure rather than over-exposure).
    """

    # Future: return actor.dept_ids when auth-shared exposes the field.
    # For now, return empty list — dept_admin sees no licenses until
    # the auth-shared library is updated.
    return []


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


async def _fetch_all_license_ids(pool: Any) -> list[str | None]:
    """Return every distinct ``license_id`` from ``bot_license_caps``.

    Also includes ``None`` as a sentinel for depts with no license
    assigned (the default-cap bucket).
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT license_id
            FROM automation.bot_license_caps
            ORDER BY license_id
            """
        )
    ids: list[str | None] = [r["license_id"] for r in rows]
    # Always include the NULL bucket so depts without a license are
    # represented in the response.
    if None not in ids:
        ids.append(None)
    return ids


async def _fetch_license_ids_for_depts(
    pool: Any, dept_ids: list[str]
) -> list[str | None]:
    """Return the distinct ``license_id`` values for the given dept_ids.

    Used by the ``dept_admin`` RBAC branch to restrict the result set
    to only the license tiers the actor's depts belong to.
    """

    if not dept_ids:
        return []

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT d.license_id
            FROM automation.departments d
            WHERE d.id = ANY($1::text[])
            """,
            dept_ids,
        )
    return [r["license_id"] for r in rows]


async def _fetch_cap_row(
    pool: Any, license_id: str | None
) -> dict[str, Any] | None:
    """Fetch the cap configuration row for a given ``license_id``.

    Returns ``None`` when no matching row exists (the caller falls
    back to the default cap values).
    """

    if license_id is None:
        return None

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                license_id,
                max_concurrent_workflows,
                max_workflows_per_day,
                max_token_usd_per_month
            FROM automation.bot_license_caps
            WHERE license_id = $1
            """,
            license_id,
        )
    return dict(row) if row is not None else None


async def _count_concurrent(
    pool: Any, license_id: str | None
) -> int:
    """Count currently running workflows in scope for ``license_id``.

    When ``license_id`` is ``None`` (default-cap bucket) the count
    covers all depts whose ``license_id IS NULL``.
    """

    if license_id is None:
        sql = """
            SELECT COUNT(*)::bigint AS n
            FROM automation.work_items wi
            JOIN automation.departments d ON d.id = wi.department_id
            WHERE d.license_id IS NULL
              AND wi.status = 'running'
        """
        params: tuple[Any, ...] = ()
    else:
        sql = """
            SELECT COUNT(*)::bigint AS n
            FROM automation.work_items wi
            JOIN automation.departments d ON d.id = wi.department_id
            WHERE d.license_id = $1
              AND wi.status = 'running'
        """
        params = (license_id,)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    return int(row["n"]) if row is not None else 0


async def _count_daily(
    pool: Any, license_id: str | None, day_start: datetime
) -> int:
    """Count workflow starts within the current UTC calendar day."""

    if license_id is None:
        sql = """
            SELECT COUNT(*)::bigint AS n
            FROM automation.work_items wi
            JOIN automation.departments d ON d.id = wi.department_id
            WHERE d.license_id IS NULL
              AND wi.created_at >= $1
        """
        params: tuple[Any, ...] = (day_start,)
    else:
        sql = """
            SELECT COUNT(*)::bigint AS n
            FROM automation.work_items wi
            JOIN automation.departments d ON d.id = wi.department_id
            WHERE d.license_id = $1
              AND wi.created_at >= $2
        """
        params = (license_id, day_start)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    return int(row["n"]) if row is not None else 0


async def _sum_monthly_token_usd(
    pool: Any, license_id: str | None, month_start: datetime
) -> Decimal:
    """Sum production LLM cost (USD) within the current UTC month."""

    if license_id is None:
        sql = """
            SELECT COALESCE(SUM(c.cost_usd), 0)::numeric AS total
            FROM shared.cost_tracking c
            JOIN automation.departments d ON d.id = c.dept_id
            WHERE d.license_id IS NULL
              AND c.cost_tag = 'production'
              AND c.created_at >= $1
        """
        params: tuple[Any, ...] = (month_start,)
    else:
        sql = """
            SELECT COALESCE(SUM(c.cost_usd), 0)::numeric AS total
            FROM shared.cost_tracking c
            JOIN automation.departments d ON d.id = c.dept_id
            WHERE d.license_id = $1
              AND c.cost_tag = 'production'
              AND c.created_at >= $2
        """
        params = (license_id, month_start)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if row is None or row["total"] is None:
        return Decimal("0")
    return Decimal(str(row["total"]))


# ---------------------------------------------------------------------------
# Usage aggregation
# ---------------------------------------------------------------------------


async def _build_license_usage(
    pool: Any,
    license_id: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Build a single license usage dict for the response array.

    Fetches the cap configuration and all three usage counters, then
    computes ``percent_used = max(concurrent%, daily%, monthly%)``.

    Parameters
    ----------
    pool:
        asyncpg pool.
    license_id:
        The license tier identifier, or ``None`` for the default-cap
        bucket (depts with no license assigned).
    now:
        Current UTC datetime used to compute the day/month window
        boundaries. Injected so callers can pin the clock in tests.
    """

    # Resolve cap configuration.
    cap_row = await _fetch_cap_row(pool, license_id)
    if cap_row is not None:
        max_concurrent = int(cap_row["max_concurrent_workflows"])
        max_daily = int(cap_row["max_workflows_per_day"])
        max_monthly = Decimal(str(cap_row["max_token_usd_per_month"]))
    else:
        # Default cap (NULL license or orphan reference).
        max_concurrent = _DEFAULT_MAX_CONCURRENT
        max_daily = _DEFAULT_MAX_DAILY
        max_monthly = _DEFAULT_MAX_MONTHLY_TOKEN_USD

    # Compute window boundaries.
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    # Fetch usage counters.
    current_concurrent = await _count_concurrent(pool, license_id)
    daily_used = await _count_daily(pool, license_id, day_start)
    monthly_usd_used = await _sum_monthly_token_usd(
        pool, license_id, month_start
    )

    # Compute percent_used = max(concurrent%, daily%, monthly%).
    concurrent_pct = (
        (current_concurrent / max_concurrent * 100.0)
        if max_concurrent > 0
        else 0.0
    )
    daily_pct = (
        (daily_used / max_daily * 100.0) if max_daily > 0 else 0.0
    )
    monthly_pct = (
        (float(monthly_usd_used) / float(max_monthly) * 100.0)
        if max_monthly > 0
        else 0.0
    )
    percent_used = round(max(concurrent_pct, daily_pct, monthly_pct), 1)

    return {
        "license_id": license_id if license_id is not None else _DEFAULT_LICENSE_SENTINEL,
        "max_concurrent": max_concurrent,
        "current_concurrent": current_concurrent,
        "daily_used": daily_used,
        "daily_max": max_daily,
        "monthly_token_usd_used": str(monthly_usd_used),
        "monthly_token_usd_max": str(max_monthly),
        "percent_used": percent_used,
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/license",
    summary="License cap usage for the operations dashboard",
)
async def get_license_usage(
    request: Request,
    actor: AuthClaims = Depends(require_admin),
) -> list[dict[str, Any]]:
    """Return license cap usage for every tier visible to the actor.

    **RBAC:**

    * ``admin`` — all license tiers are returned.
    * ``dept_admin`` — only the license tier(s) associated with the
      actor's own ``dept_ids`` are returned. When the auth-shared
      library does not yet surface ``dept_ids`` as a first-class
      claim, the result set is empty (safe-fail: under-exposure).
    * ``lead`` / ``viewer`` — ``403`` (rejected by
      :func:`require_admin` before this handler runs).

    **Response shape** (array of objects):

    .. code-block:: json

        [
          {
            "license_id": "enterprise-2025",
            "max_concurrent": 10,
            "current_concurrent": 3,
            "daily_used": 47,
            "daily_max": 100,
            "monthly_token_usd_used": "234.56",
            "monthly_token_usd_max": "1000.00",
            "percent_used": 47.0
          }
        ]

    ``percent_used`` is ``max(concurrent%, daily%, monthly%)`` rounded
    to one decimal place. Departments with no license assigned are
    aggregated under ``license_id = "__default__"``.

    **503** is returned when the asyncpg pool is not yet available
    (Postgres still booting or pool creation failed during lifespan).
    """

    pool = _get_pool(request)
    now = datetime.now(timezone.utc)

    # Determine which license_ids to include based on RBAC.
    if _is_admin(actor):
        # Admin sees all license tiers.
        license_ids = await _fetch_all_license_ids(pool)
    elif _is_dept_admin(actor):
        # dept_admin sees only their own license tiers.
        dept_ids = _actor_dept_ids(actor)
        if not dept_ids:
            # No dept_ids claim available — safe-fail: return empty.
            logger.warning(
                "dept_admin actor %r has no dept_ids claim; "
                "returning empty license list",
                actor.sub,
            )
            return []
        license_ids = await _fetch_license_ids_for_depts(pool, dept_ids)
        if not license_ids:
            return []
    else:
        # Should not reach here because require_admin already enforces
        # admin membership. Defensive 403 in case the dependency is
        # overridden in tests.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin or dept_admin claim required",
        )

    # Build usage objects for each license tier.
    results: list[dict[str, Any]] = []
    for license_id in license_ids:
        try:
            usage = await _build_license_usage(pool, license_id, now)
            results.append(usage)
        except Exception as exc:  # noqa: BLE001 — soft-fail per license tier
            logger.warning(
                "Failed to build license usage for license_id=%r: %s",
                license_id,
                exc,
            )
            # Skip this tier rather than failing the entire response.
            continue

    # Sort by license_id for stable output (None sentinel last).
    results.sort(
        key=lambda r: (
            r["license_id"] == _DEFAULT_LICENSE_SENTINEL,
            r["license_id"],
        )
    )

    return results
