"""Bot license hard-cap enforcement middleware.

This module owns the workflow-start guard that rejects new
``AutomationWorkflow`` executions when a dept's license tier limit is
already saturated.

Behaviour
=========

For a given ``dept_id`` and a fresh workflow start request, the
:func:`enforce_license_cap` helper:

1. Fetches the cap row for the dept from
   ``automation.bot_license_caps`` (joined via
   ``automation.departments.license_id``). When the dept has no
   ``license_id`` (NULL / department predates license assignment), the
   helper falls back to the *default cap* baked into
   :class:`LicenseCap` - ``max_concurrent_workflows=10``,
   ``max_workflows_per_day=100``,
   ``max_token_usd_per_month=Decimal("1000.00")``.
2. Reads three usage counters in a deterministic order - **concurrent
    daily  monthly_token** - short-circuiting on the *first*
   exceeded limit.
3. If a limit is exceeded, writes a ``bot_license_cap_exceeded``
   audit row (``actor_role="system"``, ``result="denied"``) with
   payload ``{license_id, limit_type, current_value, max_value,
   dept_id, issue_key}`` and raises
   :class:`BotLicenseCapExceededError`. The caller translates the
   exception into HTTP
   429 Too Many Requests with a structured body and a best-effort
   Jira acknowledgement comment.
4. If every limit is below its cap, returns ``None`` - no audit row
   is written on the success path (silent allow).

The audit write is **best-effort**: a failure inside
``audit_logger.write`` does not mask the rejection signal. The
exception is raised regardless so the workflow-start path always
fails closed.

SQL surface
-----------

The helper executes three independent ``COUNT`` / ``SUM`` queries
against:

* ``automation.work_items`` (status filter for concurrent + daily);
* ``shared.cost_tracking`` (monthly token cost; ``cost_tag =
  'production'`` so sandbox / probe traffic is excluded).

All three are read-only and use ``$N`` parametrised placeholders so
the helper is safe against SQL injection in ``dept_id`` and
``license_id`` payloads.

Because ``automation.work_items.license_id`` does not exist (the
foundation schema only carries ``department_id``), the queries scope
license-level usage by joining ``automation.departments.license_id``.
For NULL-license dept rows, the queries fall back to a per-dept
filter so the default-cap path is still usage-bounded.

"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Final, Literal

import asyncpg

__all__ = [
    "BotLicenseCapExceededError",
    "DEFAULT_LICENSE_CAP",
    "LicenseCap",
    "enforce_license_cap",
    "fetch_cap_for_dept",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cap dataclass + defaults
# ---------------------------------------------------------------------------


#: Limit identifiers in the deterministic check order. The literal
#: ordering is part of the public contract: the *first* exceeded limit
#: wins, and the workflow-start path surfaces the value verbatim in the 429
#: response body.
LimitType = Literal["concurrent", "daily", "monthly_token"]

#: Default cap applied when ``automation.departments.license_id`` is
#: NULL (no license assigned). Numbers come from migration ``002``
#: design defaults so config and code agree on a single
#: source of truth.
_DEFAULT_MAX_CONCURRENT: Final[int] = 10
_DEFAULT_MAX_DAILY: Final[int] = 100
_DEFAULT_MAX_MONTHLY_TOKEN_USD: Final[Decimal] = Decimal("1000.00")


@dataclass(frozen=True, slots=True)
class LicenseCap:
    """Resolved hard-cap configuration for a dept.

    The dataclass deliberately mirrors the columns of
    ``automation.bot_license_caps`` (excluding ``id`` and
    ``created_at`` which are operational metadata) so the cap loader
    can hydrate it directly from the row.

    Attributes
    ----------
    license_id:
        Identifier of the license tier (eg. ``"enterprise-2025"``)
        or ``None`` when the dept has no license assigned and the
        default cap is applied. The audit payload records this as
        the literal value (``None`` is serialised to JSON ``null``).
    max_concurrent_workflows:
        Hard cap on simultaneously running ``AutomationWorkflow``
        executions for the license tier. ``status='running'`` rows
        in ``automation.work_items`` count against this limit.
    max_workflows_per_day:
        Hard cap on workflow starts within the current calendar day
        (UTC). All ``automation.work_items`` rows whose
        ``created_at`` falls inside the day window count, regardless
        of final status - this matches the design intent that a
        runaway start storm should be reined in even if executions
        terminate fast.
    max_token_usd_per_month:
        Hard cap on the cumulative LLM cost (``NUMERIC(10,2)`` USD)
        within the current calendar month (UTC). Only
        ``shared.cost_tracking`` rows with ``cost_tag='production'``
        count, so sandbox / probe traffic is excluded.
    """

    license_id: str | None
    max_concurrent_workflows: int
    max_workflows_per_day: int
    max_token_usd_per_month: Decimal


#: Sentinel cap returned by :func:`fetch_cap_for_dept` when a dept
#: has no ``license_id`` assigned. Exposed as a module constant so
#: tests can assert against the exact default values without
#: re-deriving them.
DEFAULT_LICENSE_CAP: Final[LicenseCap] = LicenseCap(
    license_id=None,
    max_concurrent_workflows=_DEFAULT_MAX_CONCURRENT,
    max_workflows_per_day=_DEFAULT_MAX_DAILY,
    max_token_usd_per_month=_DEFAULT_MAX_MONTHLY_TOKEN_USD,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BotLicenseCapExceededError(RuntimeError):
    """Raised when a workflow start would exceed a license hard cap.

    The workflow-start path maps this to HTTP 429 Too
    Many Requests with a structured body
    ``{"error": "bot_license_cap_exceeded", "limit": <type>,
    "current": <int|float>, "max": <int|float>}`` and posts a
    best-effort Jira comment so the end user knows why no bot
    response is forthcoming.

    Attributes
    ----------
    limit_type:
        Which cap was breached (``"concurrent"`` | ``"daily"`` |
        ``"monthly_token"``). Always populated.
    current:
        The observed usage value at check time. Integer for
        ``concurrent`` / ``daily``, :class:`~decimal.Decimal` for
        ``monthly_token`` (USD with 6-digit precision aligned with
        ``shared.cost_tracking.cost_usd``).
    max:
        The cap threshold the usage met or exceeded. Same type as
        :attr:`current`.
    license_id:
        License tier identifier or ``None`` when the dept used the
        default cap.
    dept_id:
        Department identifier the workflow start was for.
    issue_key:
        Jira issue key (eg. ``"PAY-101"``) or ``None`` when the
        start path is not Jira-driven (Slack / email inbound).
    """

    def __init__(
        self,
        *,
        limit_type: LimitType,
        current: int | Decimal,
        max_value: int | Decimal,
        license_id: str | None,
        dept_id: str,
        issue_key: str | None = None,
    ) -> None:
        self.limit_type: LimitType = limit_type
        self.current: int | Decimal = current
        self.max: int | Decimal = max_value
        self.license_id: str | None = license_id
        self.dept_id: str = dept_id
        self.issue_key: str | None = issue_key
        super().__init__(
            f"bot_license_cap_exceeded: limit_type={limit_type!r} "
            f"current={current} max={max_value} "
            f"license_id={license_id!r} dept_id={dept_id!r} "
            f"issue_key={issue_key!r}"
        )


# ---------------------------------------------------------------------------
# Cap loader
# ---------------------------------------------------------------------------


async def fetch_cap_for_dept(db: asyncpg.Pool, dept_id: str) -> LicenseCap:
    """Resolve the :class:`LicenseCap` for a department.

    Parameters
    ----------
    db:
        :class:`asyncpg.Pool` connected to the automation database.
    dept_id:
        Department identifier from ``automation.departments.id``.

    Returns
    -------
    LicenseCap
        Either the row from ``automation.bot_license_caps`` joined via
        ``automation.departments.license_id``, or
        :data:`DEFAULT_LICENSE_CAP` when the dept has no license
        assigned (``license_id IS NULL``) or the dept itself does not
        exist (defensive fallback - workflow start path validates
        dept existence elsewhere).

    Notes
    -----
    A single round-trip (``LEFT JOIN`` on the cap row) keeps the cap
    loader independent of dept-table presence: tests can pre-seed
    the cap row alone and exercise the helper without bringing the
    full departments fixture online.
    """

    sql = """
        SELECT
            d.license_id,
            c.max_concurrent_workflows,
            c.max_workflows_per_day,
            c.max_token_usd_per_month
        FROM automation.departments d
        LEFT JOIN automation.bot_license_caps c
            ON c.license_id = d.license_id
        WHERE d.id = $1
    """
    async with db.acquire() as conn:
        row = await conn.fetchrow(sql, dept_id)

    if row is None:
        # Dept row missing - treat as default cap. The workflow start
        # path is responsible for surfacing a real "unknown dept"
        # error before this guard runs; falling through to the
        # default keeps the helper composable for tests that wire
        # only the cap fixtures.
        return DEFAULT_LICENSE_CAP

    license_id = row["license_id"]
    if license_id is None or row["max_concurrent_workflows"] is None:
        # Either no license assigned, or a license id with no matching
        # cap row (the FK is nullable but we still defend against an
        # orphan reference). Fall back to defaults but propagate the
        # raw license_id so the audit payload reflects what the dept
        # claimed.
        return LicenseCap(
            license_id=license_id,
            max_concurrent_workflows=_DEFAULT_MAX_CONCURRENT,
            max_workflows_per_day=_DEFAULT_MAX_DAILY,
            max_token_usd_per_month=_DEFAULT_MAX_MONTHLY_TOKEN_USD,
        )

    return LicenseCap(
        license_id=license_id,
        max_concurrent_workflows=int(row["max_concurrent_workflows"]),
        max_workflows_per_day=int(row["max_workflows_per_day"]),
        max_token_usd_per_month=Decimal(str(row["max_token_usd_per_month"])),
    )


# ---------------------------------------------------------------------------
# Usage counters
# ---------------------------------------------------------------------------


async def _count_concurrent_workflows(
    db: asyncpg.Pool,
    *,
    license_id: str | None,
    dept_id: str,
) -> int:
    """Count ``status='running'`` workflow executions in scope.

    When ``license_id`` is set, every dept tied to that license tier
    contributes; when ``license_id`` is ``None``, the count scopes to
    the single ``dept_id`` so the default cap is dept-bounded.
    """

    if license_id is None:
        sql = """
            SELECT COUNT(*)::bigint AS n
            FROM automation.work_items
            WHERE department_id = $1
              AND status = 'running'
        """
        params: tuple[Any, ...] = (dept_id,)
    else:
        sql = """
            SELECT COUNT(*)::bigint AS n
            FROM automation.work_items wi
            JOIN automation.departments d ON d.id = wi.department_id
            WHERE d.license_id = $1
              AND wi.status = 'running'
        """
        params = (license_id,)

    async with db.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    return int(row["n"]) if row is not None else 0


async def _count_workflows_today(
    db: asyncpg.Pool,
    *,
    license_id: str | None,
    dept_id: str,
    day_start: datetime,
) -> int:
    """Count workflow starts inside the current UTC calendar day.

    ``day_start`` is the inclusive lower bound; the helper returns
    rows with ``created_at >= day_start``. The caller computes
    ``day_start`` from the injected clock so unit tests pin the
    boundary deterministically.
    """

    if license_id is None:
        sql = """
            SELECT COUNT(*)::bigint AS n
            FROM automation.work_items
            WHERE department_id = $1
              AND created_at >= $2
        """
        params: tuple[Any, ...] = (dept_id, day_start)
    else:
        sql = """
            SELECT COUNT(*)::bigint AS n
            FROM automation.work_items wi
            JOIN automation.departments d ON d.id = wi.department_id
            WHERE d.license_id = $1
              AND wi.created_at >= $2
        """
        params = (license_id, day_start)

    async with db.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    return int(row["n"]) if row is not None else 0


async def _sum_monthly_token_cost(
    db: asyncpg.Pool,
    *,
    license_id: str | None,
    dept_id: str,
    month_start: datetime,
) -> Decimal:
    """Sum production LLM cost (USD) inside the current UTC month.

    Filters ``shared.cost_tracking`` to ``cost_tag = 'production'`` so
    sandbox prompt tests (Q4) and probe runs do not eat into the
    license budget. The aggregate is returned as a
    :class:`~decimal.Decimal` so the caller can compare against the
    cap without float-precision drift.
    """

    if license_id is None:
        sql = """
            SELECT COALESCE(SUM(c.cost_usd), 0)::numeric AS total
            FROM shared.cost_tracking c
            WHERE c.dept_id = $1
              AND c.cost_tag = 'production'
              AND c.created_at >= $2
        """
        params: tuple[Any, ...] = (dept_id, month_start)
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

    async with db.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    if row is None or row["total"] is None:
        return Decimal("0")
    return Decimal(str(row["total"]))


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    """Default clock - UTC ``now()`` with timezone awareness."""

    return datetime.now(timezone.utc)


def _start_of_utc_day(now: datetime) -> datetime:
    """Return the inclusive lower bound of the UTC calendar day."""

    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _start_of_utc_month(now: datetime) -> datetime:
    """Return the inclusive lower bound of the UTC calendar month."""

    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Audit emission
# ---------------------------------------------------------------------------


async def _emit_cap_exceeded_audit(
    audit_logger: Any | None,
    *,
    limit_type: LimitType,
    current: int | Decimal,
    max_value: int | Decimal,
    license_id: str | None,
    dept_id: str,
    issue_key: str | None,
    timestamp: datetime,
) -> None:
    """Best-effort ``bot_license_cap_exceeded`` audit row write.

    A failure inside the audit logger is *swallowed* so the caller's
    rejection signal still reaches the workflow-start path
    untouched. The audit pipeline is monitored separately; sacrificing
    the audit row here is preferable to silently allowing a workflow
    that should be capped.
    """

    if audit_logger is None:
        return

    try:
        from audit_logger import AuditEvent  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - defensive
        return

    # ``current`` / ``max`` are emitted as JSON-friendly primitives so
    # downstream consumers (Loki sink, Postgres jsonb column) receive
    # a stable shape regardless of underlying numeric type.
    if isinstance(current, Decimal):
        current_json: float | int = float(current)
    else:
        current_json = int(current)
    if isinstance(max_value, Decimal):
        max_json: float | int = float(max_value)
    else:
        max_json = int(max_value)

    resource = (
        f"workflow:{issue_key}" if issue_key else f"dept:{dept_id}"
    )

    event = AuditEvent(
        actor_id="automation-service.middleware.license_cap",
        actor_role="system",
        dept_id=dept_id,
        action="bot_license_cap_exceeded",
        resource=resource,
        result="denied",
        timestamp=timestamp,
        payload={
            "license_id": license_id,
            "limit_type": limit_type,
            "current_value": current_json,
            "max_value": max_json,
            "dept_id": dept_id,
            "issue_key": issue_key,
        },
    )

    try:
        await audit_logger.write(event)
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning(
            "bot_license_cap_exceeded audit write failed; "
            "rejection signal preserved",
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def enforce_license_cap(
    *,
    dept_id: str,
    db: asyncpg.Pool,
    audit_logger: Any | None = None,
    issue_key: str | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> None:
    """Reject a workflow start that would breach the dept's license cap.

    Parameters
    ----------
    dept_id:
        Department identifier from ``automation.departments.id``.
    db:
        :class:`asyncpg.Pool` connected to the automation database.
        The helper runs four read-only SQL statements against it
        (cap fetch + three usage counters); transactionality is not
        required because each statement is naturally serialisable
        and a brief race window is acceptable for advisory caps.
    audit_logger:
        Optional :class:`audit_logger.AuditLogger`. If supplied, a
        ``bot_license_cap_exceeded`` row is written before the
        exception is raised. ``None`` skips the audit (used by unit
        tests that do not exercise the audit path).
    issue_key:
        Jira issue key associated with the workflow start, or
        ``None`` for non-Jira inbound channels (Slack / email). The
        value is recorded in the audit payload and the exception so
        downstream tooling can correlate the rejection back to the
        originating event.
    now:
        Injectable clock returning a timezone-aware UTC
        :class:`~datetime.datetime`. Defaults to ``datetime.now(UTC)``;
        tests pass a frozen clock to pin the daily / monthly window
        boundaries deterministically.

    Raises
    ------
    BotLicenseCapExceededError
        When *any* of the three caps (in the deterministic order
        ``concurrent``  ``daily``  ``monthly_token``) is met or
        exceeded by current usage. The exception carries the offending
        limit, the observed current value, the cap threshold, the
        license_id (or ``None``), the dept_id and the issue_key so
        the workflow-start path can render the 429 response and the
        Jira acknowledgement comment without a second round-trip.

    Notes
    -----
    * **Comparison semantics** - limits are checked with ``>=``: a
      workflow that would push usage *to* the cap is also rejected,
      because the start being guarded would tip usage over by 1
      (concurrent / daily) or by an a-priori-unknown amount
      (monthly_token, since the LLM cost is not known at start
      time).
    * **Order matters** - the first exceeded limit wins; subsequent
      limits are not evaluated. This is part of the public contract
      so the 429 response surface stays predictable for clients
      retrying with backoff.
    * **No success audit** - a green pass writes nothing. Audit
      volume is dominated by webhook traffic; adding a per-start row
      here would inflate it without operational benefit.
    """

    cap = await fetch_cap_for_dept(db, dept_id)
    timestamp = now()
    day_start = _start_of_utc_day(timestamp)
    month_start = _start_of_utc_month(timestamp)

    # 1. Concurrent - running workflow executions in scope right now.
    concurrent = await _count_concurrent_workflows(
        db, license_id=cap.license_id, dept_id=dept_id
    )
    if concurrent >= cap.max_concurrent_workflows:
        await _emit_cap_exceeded_audit(
            audit_logger,
            limit_type="concurrent",
            current=concurrent,
            max_value=cap.max_concurrent_workflows,
            license_id=cap.license_id,
            dept_id=dept_id,
            issue_key=issue_key,
            timestamp=timestamp,
        )
        raise BotLicenseCapExceededError(
            limit_type="concurrent",
            current=concurrent,
            max_value=cap.max_concurrent_workflows,
            license_id=cap.license_id,
            dept_id=dept_id,
            issue_key=issue_key,
        )

    # 2. Daily - workflow starts within the current UTC calendar day.
    daily = await _count_workflows_today(
        db,
        license_id=cap.license_id,
        dept_id=dept_id,
        day_start=day_start,
    )
    if daily >= cap.max_workflows_per_day:
        await _emit_cap_exceeded_audit(
            audit_logger,
            limit_type="daily",
            current=daily,
            max_value=cap.max_workflows_per_day,
            license_id=cap.license_id,
            dept_id=dept_id,
            issue_key=issue_key,
            timestamp=timestamp,
        )
        raise BotLicenseCapExceededError(
            limit_type="daily",
            current=daily,
            max_value=cap.max_workflows_per_day,
            license_id=cap.license_id,
            dept_id=dept_id,
            issue_key=issue_key,
        )

    # 3. Monthly token - production LLM cost (USD) since the start of
    # the current UTC calendar month.
    monthly = await _sum_monthly_token_cost(
        db,
        license_id=cap.license_id,
        dept_id=dept_id,
        month_start=month_start,
    )
    if monthly >= cap.max_token_usd_per_month:
        await _emit_cap_exceeded_audit(
            audit_logger,
            limit_type="monthly_token",
            current=monthly,
            max_value=cap.max_token_usd_per_month,
            license_id=cap.license_id,
            dept_id=dept_id,
            issue_key=issue_key,
            timestamp=timestamp,
        )
        raise BotLicenseCapExceededError(
            limit_type="monthly_token",
            current=monthly,
            max_value=cap.max_token_usd_per_month,
            license_id=cap.license_id,
            dept_id=dept_id,
            issue_key=issue_key,
        )

    # All three caps under threshold - silent allow.
    return None


_ = timedelta  # keep timedelta import resolved for forward-compat helpers
