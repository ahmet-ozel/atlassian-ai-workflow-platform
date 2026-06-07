"""``CostsRouter`` (`operations surface` cost router wiring).


Surfaces the dept / model / trend cost views that the admin
dashboard ``/costs`` page renders. Reads from the
``shared.cost_tracking`` table via an asyncpg pool stashed on
``app.state.pg_pool``; falls back to an empty result when the
pool is missing so the panel renders a soft warning instead of a
500.

Three endpoints:

* ``GET /admin/costs/dept/{dept_id}`` - total + per-user breakdown
  for the configured dept over the last 30 days.
* ``GET /admin/costs/model`` - per-model totals across all depts
  the caller has admin RBAC for.
* ``GET /admin/costs/trend`` - daily series for the last 30 days.

Every aggregate filters on ``cost_tag = 'production'`` so sandbox /
probe rows never inflate the numbers (invariant 7 / rule 5.5).
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth.dependencies import require_admin

__all__ = ["router"]


router = APIRouter(prefix="/admin/costs", tags=["costs"])


def _get_pool(request: Request) -> Any:
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "reason": "pg_pool_unavailable"},
        )
    return pool


@router.get("/dept/{dept_id}", dependencies=[Depends(require_admin)])
async def costs_by_dept(dept_id: str, request: Request) -> dict:
    pool = _get_pool(request)
    async with pool.acquire() as conn:
        total_row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(cost_usd), 0)::numeric AS total_usd,
                   COUNT(*)::bigint AS rows
              FROM shared.cost_tracking
             WHERE dept_id = $1
               AND cost_tag = 'production'
               AND created_at >= now() - interval '30 days'
            """,
            dept_id,
        )
        user_rows = await conn.fetch(
            """
            SELECT user_id,
                   COALESCE(SUM(cost_usd), 0)::numeric AS user_usd
              FROM shared.cost_tracking
             WHERE dept_id = $1
               AND cost_tag = 'production'
               AND created_at >= now() - interval '30 days'
             GROUP BY user_id
             ORDER BY user_usd DESC
            """,
            dept_id,
        )
    return {
        "dept_id": dept_id,
        "window": "30d",
        "total_usd": str(total_row["total_usd"]),
        "row_count": int(total_row["rows"]),
        "by_user": [
            {"user_id": r["user_id"], "usd": str(r["user_usd"])}
            for r in user_rows
        ],
    }


@router.get("/model", dependencies=[Depends(require_admin)])
async def costs_by_model(request: Request) -> dict:
    pool = _get_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT model,
                   COALESCE(SUM(cost_usd), 0)::numeric AS usd,
                   COUNT(*)::bigint AS rows
              FROM shared.cost_tracking
             WHERE cost_tag = 'production'
               AND created_at >= now() - interval '30 days'
             GROUP BY model
             ORDER BY usd DESC
            """,
        )
    return {
        "window": "30d",
        "by_model": [
            {
                "model": r["model"],
                "usd": str(r["usd"]),
                "row_count": int(r["rows"]),
            }
            for r in rows
        ],
    }


@router.get("/trend", dependencies=[Depends(require_admin)])
async def costs_trend(request: Request) -> dict:
    pool = _get_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT date_trunc('day', created_at)::date AS day,
                   COALESCE(SUM(cost_usd), 0)::numeric AS usd
              FROM shared.cost_tracking
             WHERE cost_tag = 'production'
               AND created_at >= now() - interval '30 days'
             GROUP BY day
             ORDER BY day
            """,
        )
    return {
        "window": "30d",
        "trend": [
            {"day": r["day"].isoformat(), "usd": str(r["usd"])}
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Feature 10: Budget Alarm Threshold Configuration (legacy file-based)
# ---------------------------------------------------------------------------

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from ..auth.dependencies import AuthClaims

logger = logging.getLogger(__name__)

def _resolve_config_path(filename: str) -> Path:
    """Return ``config/<filename>`` across repo and container layouts."""

    resolved = Path(__file__).resolve()
    for parent in resolved.parents:
        config_dir = parent / "config"
        if config_dir.is_dir():
            return config_dir / filename
    return resolved.parents[min(2, len(resolved.parents) - 1)] / "config" / filename


#: Path to budget alarm config file.
_ALARM_CONFIG_PATH = _resolve_config_path("budget_alarm.json")

#: Default threshold percentage.
_DEFAULT_THRESHOLD_PCT = 80


class BudgetAlarmConfig(BaseModel):
    """Budget alarm threshold configuration."""

    budget_alarm_threshold_pct: int = Field(
        default=_DEFAULT_THRESHOLD_PCT,
        ge=1,
        le=100,
        description="Percentage of budget at which alarm triggers",
    )
    slack_channel: str = Field(
        default="#cost-alerts",
        description="Slack channel for budget alarm notifications",
    )


def _load_alarm_config() -> dict:
    """Load budget alarm config from disk."""
    try:
        with open(_ALARM_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {
            "budget_alarm_threshold_pct": _DEFAULT_THRESHOLD_PCT,
            "slack_channel": "#cost-alerts",
        }


def _save_alarm_config(config: dict) -> None:
    """Persist budget alarm config to disk."""
    _ALARM_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_ALARM_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


async def _emit_cost_audit(
    request: Request,
    action: str,
    actor_id: str,
    payload: dict | None = None,
) -> None:
    """Emit an audit event for cost-related actions."""
    from audit_logger import AuditEvent

    sink = getattr(request.app.state, "audit_sink", None)
    if sink is None:
        return
    event = AuditEvent(
        actor_id=actor_id,
        actor_role="admin",
        dept_id=None,
        action=action,
        resource="costs:alarm-config",
        result="ok",
        timestamp=datetime.now(timezone.utc),
        payload=payload,
    )
    await sink.write(event)


@router.get("/alarm-config", dependencies=[Depends(require_admin)])
async def get_alarm_config(request: Request) -> dict:
    """Return the current budget alarm threshold configuration.

    Returns the threshold percentage and Slack channel for budget alarms.
    """
    config = _load_alarm_config()
    return {
        "budget_alarm_threshold_pct": config.get(
            "budget_alarm_threshold_pct", _DEFAULT_THRESHOLD_PCT
        ),
        "slack_channel": config.get("slack_channel", "#cost-alerts"),
    }


@router.put("/alarm-config", dependencies=[Depends(require_admin)])
async def update_alarm_config(
    body: BudgetAlarmConfig,
    request: Request,
    claims: AuthClaims = Depends(require_admin),
) -> dict:
    """Update the budget alarm threshold configuration.

    Audit event: ``budget_alarm_threshold_updated``.
    """
    new_config = {
        "budget_alarm_threshold_pct": body.budget_alarm_threshold_pct,
        "slack_channel": body.slack_channel,
    }
    _save_alarm_config(new_config)

    await _emit_cost_audit(
        request,
        action="budget_alarm_threshold_updated",
        actor_id=claims.sub,
        payload=new_config,
    )

    return {"status": "updated", **new_config}


async def check_budget_threshold_after_insert(
    request: Request,
    dept_id: str,
    budget_cap_usd: float,
) -> bool:
    """Check if current spend exceeds the alarm threshold after a cost insert.

    Called by the cost tracking insert path. On first threshold crossing,
    emits a ``budget_alarm_triggered`` audit event and sends a Slack alarm.

    Returns:
        True if the threshold was crossed (alarm triggered), False otherwise.
    """
    pool = getattr(request.app.state, "pg_pool", None)
    if pool is None:
        return False

    config = _load_alarm_config()
    threshold_pct = config.get("budget_alarm_threshold_pct", _DEFAULT_THRESHOLD_PCT)
    slack_channel = config.get("slack_channel", "#cost-alerts")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(cost_usd), 0)::numeric AS total_usd
              FROM shared.cost_tracking
             WHERE dept_id = $1
               AND cost_tag = 'production'
               AND created_at >= date_trunc('month', now())
            """,
            dept_id,
        )

    if row is None:
        return False

    total_usd = float(row["total_usd"])
    usage_pct = (total_usd / budget_cap_usd * 100) if budget_cap_usd > 0 else 0

    if usage_pct >= threshold_pct:
        # Check if alarm was already triggered this month
        alarm_state_key = f"_budget_alarm_triggered_{dept_id}"
        already_triggered = getattr(request.app.state, alarm_state_key, False)

        if not already_triggered:
            setattr(request.app.state, alarm_state_key, True)

            # Emit audit event
            from audit_logger import AuditEvent

            sink = getattr(request.app.state, "audit_sink", None)
            if sink is not None:
                event = AuditEvent(
                    actor_id="system",
                    actor_role="system",
                    dept_id=dept_id,
                    action="budget_alarm_triggered",
                    resource=f"costs:dept:{dept_id}",
                    result="ok",
                    timestamp=datetime.now(timezone.utc),
                    payload={
                        "usage_pct": round(usage_pct, 2),
                        "threshold_pct": threshold_pct,
                        "total_usd": str(total_usd),
                        "budget_cap_usd": str(budget_cap_usd),
                        "slack_channel": slack_channel,
                    },
                )
                await sink.write(event)

            # Send Slack alarm (best-effort)
            slack_client = getattr(request.app.state, "slack_client", None)
            if slack_client is not None:
                try:
                    await slack_client.send_message(
                        channel=slack_channel,
                        text=(
                            f" Budget alarm: Department *{dept_id}* has reached "
                            f"{usage_pct:.1f}% of its monthly budget "
                            f"(${total_usd:.2f} / ${budget_cap_usd:.2f}). "
                            f"Threshold: {threshold_pct}%."
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to send Slack budget alarm for dept %s: %s",
                        dept_id,
                        exc,
                    )

            return True

    return False


# ---------------------------------------------------------------------------
# Feature 13.2: Budget Alarm Threshold Endpoints (DB-backed, per-dept matrix)
# ---------------------------------------------------------------------------
# Covers 13.2, 13.3
#
# These endpoints operate on the ``automation.budget_alarm_thresholds``
# Postgres table (migration 008). They provide a per-department matrix
# of alarm thresholds (period × scope) with RBAC enforcement:
# - ``admin`` role: full access to any department.
# - ``dept_admin`` role: self-service access to own department(s) only.
# ---------------------------------------------------------------------------


class AlarmThresholdRow(BaseModel):
    """A single row in the budget alarm threshold matrix."""

    period: Literal["weekly", "monthly"] = Field(
        ..., description="Budget period: weekly or monthly"
    )
    scope: Literal["user", "dept"] = Field(
        ..., description="Alarm scope: per-user or per-department"
    )
    threshold_pct: int = Field(
        default=70,
        ge=1,
        le=99,
        description="Percentage of budget at which alarm triggers (1-99)",
    )
    notify_channel: Literal["slack", "email", "teams"] = Field(
        default="slack",
        description="Notification channel for the alarm",
    )


class AlarmThresholdUpsertRequest(BaseModel):
    """Request body for upserting alarm threshold rows."""

    thresholds: list[AlarmThresholdRow] = Field(
        ...,
        min_length=1,
        description="List of threshold rows to upsert",
    )


def _require_admin_or_dept_admin(claims: AuthClaims, dept_id: str) -> None:
    """Enforce RBAC: admin has full access, dept_admin only for own dept.

    Raises:
        HTTPException(403): When the caller is neither an admin nor a
            dept_admin for the specified department.
    """
    if "admin" in claims.groups:
        return
    if f"dept_admin:{dept_id}" in claims.groups:
        return
    if "dept_admin" in claims.groups:
        # Generic dept_admin - allowed for self-service on own dept
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"admin or dept_admin access required for department '{dept_id}'",
    )


@router.get("/alarm-thresholds", dependencies=[Depends(require_admin)])
async def get_alarm_thresholds(
    request: Request,
    dept_id: str,
    claims: AuthClaims = Depends(require_admin),
) -> dict:
    """Return the budget alarm threshold matrix for a department.

    Query Parameters:
        dept_id: Department ID to fetch thresholds for.

    Returns:
        A dict with ``dept_id`` and ``thresholds`` list containing
        each configured threshold row (period, scope, threshold_pct,
        notify_channel, last_alarmed_at).

    RBAC: admin or dept_admin (self-service for own department).
    """
    _require_admin_or_dept_admin(claims, dept_id)

    pool = _get_pool(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT period, scope, threshold_pct, notify_channel, last_alarmed_at
              FROM automation.budget_alarm_thresholds
             WHERE dept_id = $1
             ORDER BY period, scope
            """,
            dept_id,
        )

    return {
        "dept_id": dept_id,
        "thresholds": [
            {
                "period": r["period"],
                "scope": r["scope"],
                "threshold_pct": int(r["threshold_pct"]),
                "notify_channel": r["notify_channel"],
                "last_alarmed_at": (
                    r["last_alarmed_at"].isoformat()
                    if r["last_alarmed_at"]
                    else None
                ),
            }
            for r in rows
        ],
    }


@router.put("/alarm-thresholds/{dept_id}", dependencies=[Depends(require_admin)])
async def upsert_alarm_thresholds(
    dept_id: str,
    body: AlarmThresholdUpsertRequest,
    request: Request,
    claims: AuthClaims = Depends(require_admin),
) -> dict:
    """Upsert budget alarm threshold rows for a department.

    Performs an INSERT ... ON CONFLICT UPDATE for each row in the
    request body. The unique constraint is (dept_id, period, scope),
    so existing rows are updated in place.

    RBAC: admin or dept_admin (self-service for own department).
    Audit: ``budget_threshold_updated``.

    Returns:
        A dict with ``status``, ``dept_id``, and ``upserted_count``.
    """
    _require_admin_or_dept_admin(claims, dept_id)

    pool = _get_pool(request)
    upserted_count = 0

    async with pool.acquire() as conn:
        async with conn.transaction():
            for row in body.thresholds:
                await conn.execute(
                    """
                    INSERT INTO automation.budget_alarm_thresholds
                        (dept_id, period, scope, threshold_pct, notify_channel, updated_at)
                    VALUES ($1, $2, $3, $4, $5, NOW())
                    ON CONFLICT (dept_id, period, scope)
                    DO UPDATE SET
                        threshold_pct = EXCLUDED.threshold_pct,
                        notify_channel = EXCLUDED.notify_channel,
                        updated_at = NOW()
                    """,
                    dept_id,
                    row.period,
                    row.scope,
                    row.threshold_pct,
                    row.notify_channel,
                )
                upserted_count += 1

    # Emit audit event
    await _emit_cost_audit(
        request,
        action="budget_threshold_updated",
        actor_id=claims.sub,
        payload={
            "dept_id": dept_id,
            "upserted_count": upserted_count,
            "thresholds": [t.model_dump() for t in body.thresholds],
        },
    )

    return {
        "status": "updated",
        "dept_id": dept_id,
        "upserted_count": upserted_count,
    }
