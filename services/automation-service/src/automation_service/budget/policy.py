"""``BudgetCapPolicy`` - dept / user weekly + monthly USD cap enforcement.

Implements the ``BudgetCapPolicy`` cap enforcement flow.

The policy is the single source of truth for the **HTTP 429** decision
that the workflow start endpoint owes to the user when a department
or a single end-user has burned through their LLM cost budget. It
deliberately keeps three concerns isolated:

1. **Configuration read** - :class:`BudgetCaps` mirrors the
   ``budget_caps`` block of ``config/departments.json``
   (``departments.schema.json`` ``BudgetCaps`` ``$def``). The policy
   pulls the caps through a small :class:`BudgetCapsProvider`
   protocol so production wiring can read either the JSON file or
   ``shared.budget_caps`` (whichever stays in sync) without changing
   the policy.
2. **Usage aggregation** - :meth:`BudgetCapPolicy._usage` runs four
   ``SUM(cost_usd)`` aggregates against ``shared.cost_tracking``,
   each filtered by ``cost_tag = 'production'`` so sandbox prompt
   tests (``cost_tag='sandbox'``) and connectivity probes
   (``cost_tag='probe'``) never count against a real budget. The
   queries use ``$1::interval`` parameters so the time window is
   driven by the SQL planner's ``now() - $1`` constant folding and
   the matching ``idx_cost_dept_time`` / ``idx_cost_user_time``
   indexes (``20_ops.sql``).
3. **Decision + audit** - :meth:`BudgetCapPolicy.enforce` checks the
   four scopes in the order ``dept_weekly  user_weekly
   dept_monthly  user_monthly`` (matching the ordering in
   ``departments.README.md``); on the **first** breach it writes a
   single ``budget_exceeded`` audit event with a payload describing
   the offending scope, the current usage, the configured limit,
   and the optional ``user_id``, then returns
   :meth:`BudgetDecision.deny`. The caller (workflow start handler)
   maps a ``deny`` decision to HTTP 429 and surfaces the scope
   reason in the response body.

The policy is **idempotent** w.r.t. allow / deny per call: it never
mutates ``shared.cost_tracking``, never starts a transaction, and
treats the audit write as the only side-effect (mirrored by Postgres
RLS via the connection's ``with_dept_session`` context).

"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Final, Literal, Mapping, Protocol, Sequence, runtime_checkable

from audit_logger import AuditEvent, AuditLogger

__all__ = [
    "AlarmThreshold",
    "AlarmThresholdStore",
    "BudgetCapPolicy",
    "BudgetCaps",
    "BudgetCapsProvider",
    "BudgetCheckResult",
    "BudgetDecision",
    "BudgetUsage",
    "DenyScope",
    "JiraCommentCallback",
    "NotificationDispatcher",
    "StaticBudgetCapsProvider",
    "UsageQueryRunner",
    "check_budget",
    "configuration_error_response",
    "deny_response_body",
    "get_budget_usage_snapshot",
    "pre_llm_budget_guard",
]

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain dataclasses
# ---------------------------------------------------------------------------


#: One of the four budget scopes the policy can deny on.
DenyScope = Literal["dept_weekly", "user_weekly", "dept_monthly", "user_monthly"]


#: Order in which scopes are checked. The list mirrors
#: ``departments.README.md`` ("dept_weekly  user_weekly
#: dept_monthly  user_monthly"). Exposed as a module constant so tests
#: can assert on the exact ordering instead of duplicating the literal.
SCOPE_ORDER: Final[tuple[DenyScope, ...]] = (
    "dept_weekly",
    "user_weekly",
    "dept_monthly",
    "user_monthly",
)


@dataclass(frozen=True, slots=True)
class BudgetCaps:
    """Frozen mirror of the ``budget_caps`` block in ``departments.json``.

    The four fields are required and ``>= 0`` per
    ``departments.schema.json`` ``BudgetCaps`` ``$def``. The dataclass
    is used as a value object so the policy can be exercised against
    in-memory fixtures without touching the disk-backed configuration
    loader.

    Attributes:
        weekly_usd_dept: Department weekly USD cap.
        weekly_usd_user: Single-user weekly USD cap (within the dept).
        monthly_usd_dept: Department monthly USD cap.
        monthly_usd_user: Single-user monthly USD cap.
    """

    weekly_usd_dept: Decimal
    weekly_usd_user: Decimal
    monthly_usd_dept: Decimal
    monthly_usd_user: Decimal


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """Production-only running cost aggregate.

    Populated by :meth:`BudgetCapPolicy._usage`. ``user_*`` fields are
    ``Decimal("0")`` when the caller does not supply a ``user_id``
    (system-driven workflows have no end-user attribution); user-scope
    checks short-circuit when ``user_id is None`` so the zero values
    are never compared against a configured cap.

    Attributes:
        dept_weekly_usd: ``SUM(cost_usd)`` for the dept over the last
            7 days, ``cost_tag='production'``.
        user_weekly_usd: Same window, scoped to the calling user.
        dept_monthly_usd: ``SUM(cost_usd)`` for the dept over the last
            30 days, ``cost_tag='production'``.
        user_monthly_usd: Same window, scoped to the calling user.
    """

    dept_weekly_usd: Decimal
    user_weekly_usd: Decimal
    dept_monthly_usd: Decimal
    user_monthly_usd: Decimal


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """Outcome of a :meth:`BudgetCapPolicy.enforce` call.

    The pair ``(allowed, deny_scope)`` is intentionally explicit: both
    consumers of the type (the HTTP handler and the audit writer)
    benefit from being able to pattern-match on either field without
    the other.

    Invariants enforced by :meth:`allow` / :meth:`deny`:

    * ``allowed=True`` implies ``deny_scope is None``.
    * ``allowed=False`` implies ``deny_scope`` is one of
      :data:`SCOPE_ORDER`.

    Attributes:
        allowed: Whether the workflow start may proceed.
        deny_scope: Which budget scope was breached, or ``None`` when
            ``allowed`` is ``True``.
    """

    allowed: bool
    deny_scope: DenyScope | None

    @classmethod
    def allow(cls) -> "BudgetDecision":
        """Return a positive decision (no scope was breached)."""

        return cls(allowed=True, deny_scope=None)

    @classmethod
    def deny(cls, scope: DenyScope) -> "BudgetDecision":
        """Return a deny decision with the offending scope label.

        Args:
            scope: One of :data:`SCOPE_ORDER`. The HTTP handler uses
                this value verbatim in the 429 response body's
                ``scope`` field so admins can tell from the wire which
                cap tripped.

        Raises:
            ValueError: If ``scope`` is not one of the four canonical
                values. Catching the typo here is cheaper than letting
                an unknown literal land in the audit payload.
        """

        if scope not in SCOPE_ORDER:
            raise ValueError(
                f"BudgetDecision.deny scope must be one of {SCOPE_ORDER!r}; "
                f"got {scope!r}"
            )
        return cls(allowed=False, deny_scope=scope)


# ---------------------------------------------------------------------------
# Alarm threshold dataclass + store protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlarmThreshold:
    """A single row from ``automation.budget_alarm_thresholds``.

    Represents a configured alarm threshold for a department × period × scope
    combination. When the current usage percentage reaches or exceeds
    ``threshold_pct``, a notification is dispatched (unless already alarmed
    in the same period).

    Attributes:
        id: UUID primary key.
        dept_id: Department identifier.
        period: ``"weekly"`` or ``"monthly"``.
        scope: ``"user"`` or ``"dept"``.
        threshold_pct: Percentage threshold (1-99) at which alarm fires.
        notify_channel: Channel to send the alarm (``"slack"``, ``"email"``,
            or ``"teams"``).
        last_alarmed_at: Timestamp of the last alarm sent for this threshold,
            or ``None`` if never alarmed.
    """

    id: str
    dept_id: str
    period: Literal["weekly", "monthly"]
    scope: Literal["user", "dept"]
    threshold_pct: int
    notify_channel: Literal["slack", "email", "teams"]
    last_alarmed_at: datetime | None


@runtime_checkable
class AlarmThresholdStore(Protocol):
    """Async store for reading/updating ``automation.budget_alarm_thresholds``.

    The policy uses this protocol to:
    1. Fetch all configured thresholds for a department.
    2. Update ``last_alarmed_at`` after successfully dispatching an alarm.

    Production wiring backs this with an asyncpg connection; tests inject
    an in-memory fake.
    """

    async def get_thresholds(self, dept_id: str) -> Sequence[AlarmThreshold]:
        """Return all alarm thresholds configured for ``dept_id``.

        Returns an empty sequence when no thresholds are configured
        (the department has not set up budget alarms).
        """
        ...  # pragma: no cover - protocol

    async def update_last_alarmed_at(
        self, threshold_id: str, alarmed_at: datetime
    ) -> None:
        """Update ``last_alarmed_at`` for the given threshold row.

        Called after a notification is successfully dispatched so the
        same threshold does not re-fire within the same period.
        """
        ...  # pragma: no cover - protocol


@runtime_checkable
class NotificationDispatcher(Protocol):
    """Minimal interface for dispatching budget alarm notifications.

    The policy uses this protocol to send alarm notifications through
    the configured channel (slack/email/teams). Production wiring
    connects this to the ``notification`` lib's
    :class:`NotificationService`; tests inject a recording fake.
    """

    async def send_budget_alarm(
        self,
        *,
        channel: Literal["slack", "email", "teams"],
        dept_id: str,
        period: str,
        scope: str,
        current_usd: Decimal,
        cap_usd: Decimal,
        threshold_pct: int,
        pct_used: Decimal,
    ) -> None:
        """Dispatch a budget alarm notification.

        Args:
            channel: Target notification channel.
            dept_id: Department that breached the threshold.
            period: ``"weekly"`` or ``"monthly"``.
            scope: ``"user"`` or ``"dept"``.
            current_usd: Current spending in USD.
            cap_usd: Configured cap in USD.
            threshold_pct: The threshold percentage that was breached.
            pct_used: Actual percentage of budget used.

        Raises:
            Exception: On transport failure. The caller logs and
                continues (best-effort alarm dispatch).
        """
        ...  # pragma: no cover - protocol


# ---------------------------------------------------------------------------
# Enhanced dataclasses for budget guard checks
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BudgetCheckResult:
    """Outcome of :func:`check_budget` - the enhanced pre-workflow check.

    Extends the original :class:`BudgetDecision` with 90% threshold
    warning information and full usage breakdown so callers can:

    1. Reject with HTTP 429 when ``allowed is False``.
    2. Post a Jira warning comment when ``warning_scopes`` is non-empty
       but ``allowed is True``.
    3. Expose ``current_usage`` to the Admin Dashboard (max 60s delay).

    Attributes:
        allowed: Whether the workflow may proceed.
        exceeded_scope: The first scope that was exceeded (usage >= cap),
            or ``None`` when no scope is exceeded.
        warning_scopes: List of scopes that reached 90% of their cap
            but have not exceeded it. Empty when no scope is at 90%.
        current_usage: Dict mapping scope names to their current USD
            usage values (as strings for JSON serialisation).
    """

    allowed: bool
    exceeded_scope: str | None
    warning_scopes: list[str]
    current_usage: dict[str, str]


#: Threshold ratio at which a warning comment is posted to Jira.
#: Scopes at or above this ratio (but below 1.0) trigger a warning
#: without blocking the workflow.
WARNING_THRESHOLD: Final[Decimal] = Decimal("0.9")


# ---------------------------------------------------------------------------
# Collaborator protocols
# ---------------------------------------------------------------------------


#: Callback type for posting Jira comments. Implementations should
#: post a comment to the given issue_key with the given body text.
#: Best-effort: failures are logged but do not block the workflow.
JiraCommentCallback = Callable[[str, str], Awaitable[None]]


@runtime_checkable
class BudgetCapsProvider(Protocol):
    """Source of :class:`BudgetCaps` values keyed by ``dept_id``.

    The policy is intentionally agnostic to whether caps come from
    the parsed ``departments.json`` document, the
    ``shared.budget_caps`` projection table, or an in-test fixture.
    Implementations MUST raise :class:`KeyError` when ``dept_id`` is
    unknown - the policy converts that into a clear runtime error
    rather than silently allowing an unbudgeted dept.
    """

    def get(self, dept_id: str) -> BudgetCaps:  # pragma: no cover - protocol
        ...


@runtime_checkable
class UsageQueryRunner(Protocol):
    """Async surface used to fetch a single ``Decimal`` usage aggregate.

    Production wiring backs this with an asyncpg connection that has
    already been opened via ``db_shared.with_dept_session(...)`` so
    the running query inherits the caller's RLS GUCs
    (``app.current_dept_id`` / ``app.current_role``). Tests inject a
    list-or-dict fake.

    Implementations MUST return ``Decimal("0")`` (not ``None``) when
    the aggregate is empty so callers do not need to special-case the
    ``SUM`` of an empty set. The default :class:`BudgetCapPolicy`
    implementation handles either, but downstream code is simpler
    when zero is the canonical empty value.
    """

    async def fetchval(
        self, query: str, *args: Any
    ) -> Decimal | None:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# SQL - usage aggregates
# ---------------------------------------------------------------------------

#: Department-scoped weekly / monthly aggregate. ``$1`` is the dept id;
#: ``$2`` is an interval (``'7 days'`` or ``'30 days'``). Filtering on
#: ``cost_tag = 'production'`` is the **mandatory** invariant from
#: sandbox / probe rows must not eat into a real budget. The
#: ``COALESCE`` makes the empty-window case return zero
#: without forcing the caller to special-case ``NULL``.
_SQL_USAGE_DEPT: Final[str] = """
    SELECT COALESCE(SUM(cost_usd), 0)::numeric
      FROM shared.cost_tracking
     WHERE dept_id = $1
       AND cost_tag = 'production'
       AND created_at >= now() - $2::interval
""".strip()

#: User-scoped variant of :data:`_SQL_USAGE_DEPT`. The extra
#: ``user_id = $3`` filter narrows the aggregate to the single
#: end-user identified by the request. ``user_id`` is never
#: ``NULL`` here - the caller short-circuits the user-scope check
#: when ``user_id is None`` so this query is only issued for an
#: explicitly attributed end-user.
_SQL_USAGE_USER: Final[str] = """
    SELECT COALESCE(SUM(cost_usd), 0)::numeric
      FROM shared.cost_tracking
     WHERE dept_id = $1
       AND cost_tag = 'production'
       AND user_id = $3
       AND created_at >= now() - $2::interval
""".strip()


# ---------------------------------------------------------------------------
# BudgetCapPolicy
# ---------------------------------------------------------------------------


class BudgetCapPolicy:
    """Enforce dept + user weekly / monthly USD caps on workflow start.

    Args:
        caps_provider: A :class:`BudgetCapsProvider` keyed by
            ``dept_id``. Production wiring resolves this from the
            ``departments.json`` config loader; tests inject a dict.
        usage_query: An :class:`UsageQueryRunner` (asyncpg connection
            shape). Must already carry the caller's RLS context;
            this class never calls ``BEGIN``/``COMMIT`` itself.
        audit_logger: A :class:`AuditLogger` used to write a
            ``budget_exceeded`` event on the first scope breach. The
            logger validates ``actor_role`` so the policy MUST
            populate it - see :meth:`enforce` for the value chosen.
        clock: Optional ``Callable[[], datetime]`` returning a
            timezone-aware UTC ``datetime``. Defaults to
            ``datetime.now(timezone.utc)``. Tests inject a fake clock
            so the audit ``timestamp`` is deterministic.
        alarm_threshold_store: Optional :class:`AlarmThresholdStore`
            for reading configured alarm thresholds from
            ``automation.budget_alarm_thresholds``. When provided
            (together with ``notification_dispatcher``), the policy
            checks thresholds on every ``enforce`` call and dispatches
            alarm notifications when breached.
        notification_dispatcher: Optional :class:`NotificationDispatcher`
            for sending budget alarm notifications. Required alongside
            ``alarm_threshold_store`` for threshold alarm functionality.

    Notes:
        The class holds no mutable state; a single instance can be
        wired into the FastAPI app at boot and reused across requests.
    """

    # Window definitions are class attributes so subclasses or tests
    # can shrink them (e.g. to 1 day for fast property tests) without
    # patching SQL strings.
    WEEKLY_INTERVAL: Final[timedelta] = timedelta(days=7)
    MONTHLY_INTERVAL: Final[timedelta] = timedelta(days=30)

    def __init__(
        self,
        *,
        caps_provider: BudgetCapsProvider,
        usage_query: UsageQueryRunner,
        audit_logger: AuditLogger,
        clock: Callable[[], datetime] | None = None,
        alarm_threshold_store: AlarmThresholdStore | None = None,
        notification_dispatcher: NotificationDispatcher | None = None,
    ) -> None:
        self._caps_provider = caps_provider
        self._usage_query = usage_query
        self._audit_logger = audit_logger
        self._clock: Callable[[], datetime] = clock or _default_clock
        self._alarm_threshold_store = alarm_threshold_store
        self._notification_dispatcher = notification_dispatcher

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def enforce(
        self,
        *,
        dept_id: str,
        user_id: str | None,
    ) -> BudgetDecision:
        """Allow or deny a workflow start request based on running cost.

        Args:
            dept_id: Department identifier matching ``departments.json``
                ``id``. Required (empty values raise ``ValueError``).
            user_id: Optional end-user identifier for per-user scope
                enforcement. ``None`` means "system / unattributed
                workflow"; user-scope checks (``user_weekly``,
                ``user_monthly``) are **skipped** in that case so a
                system workflow cannot be 429'd on a user budget.

        Returns:
            :class:`BudgetDecision`. ``allow()`` if every scope is
            below its cap; ``deny(scope)`` for the first scope that
            equals or exceeds its cap.

        Raises:
            ValueError: If ``dept_id`` is empty.
            KeyError: If ``dept_id`` is not present in the
                :class:`BudgetCapsProvider` (propagated unchanged so
                the calling endpoint can map it to HTTP 404).

        Side effects:
            On a deny, writes exactly **one** ``budget_exceeded`` audit
            event with payload ``{"scope", "limit", "usage", "user_id"
            (when applicable)}``. No event is written on allow.
        """

        if not isinstance(dept_id, str) or not dept_id:
            raise ValueError("dept_id must be a non-empty string")
        # ``user_id`` may be ``None``; only validate the non-None case.
        if user_id is not None and not isinstance(user_id, str):
            raise ValueError(
                f"user_id must be a string or None; got {type(user_id).__name__}"
            )

        caps = self._caps_provider.get(dept_id)
        usage = await self._usage(dept_id=dept_id, user_id=user_id)

        # Scope ordering is part of the public contract (mirrored in
        # the README. Each branch mirrors the equivalent policy block.
        # We use ``>=`` (not ``>``) so a usage that exactly
        # matches the limit also denies - this matches the README's
        # "cap reached" wording and prevents off-by-one boundary slips.
        if usage.dept_weekly_usd >= caps.weekly_usd_dept:
            await self._emit_denied(
                dept_id=dept_id,
                user_id=user_id,
                scope="dept_weekly",
                limit=caps.weekly_usd_dept,
                usage_value=usage.dept_weekly_usd,
            )
            return BudgetDecision.deny("dept_weekly")

        if user_id is not None and usage.user_weekly_usd >= caps.weekly_usd_user:
            await self._emit_denied(
                dept_id=dept_id,
                user_id=user_id,
                scope="user_weekly",
                limit=caps.weekly_usd_user,
                usage_value=usage.user_weekly_usd,
            )
            return BudgetDecision.deny("user_weekly")

        if usage.dept_monthly_usd >= caps.monthly_usd_dept:
            await self._emit_denied(
                dept_id=dept_id,
                user_id=user_id,
                scope="dept_monthly",
                limit=caps.monthly_usd_dept,
                usage_value=usage.dept_monthly_usd,
            )
            return BudgetDecision.deny("dept_monthly")

        if user_id is not None and usage.user_monthly_usd >= caps.monthly_usd_user:
            await self._emit_denied(
                dept_id=dept_id,
                user_id=user_id,
                scope="user_monthly",
                limit=caps.monthly_usd_user,
                usage_value=usage.user_monthly_usd,
            )
            return BudgetDecision.deny("user_monthly")

        # ------------------------------------------------------------------
        # Threshold alarm check
        # # When the workflow is allowed (no scope exceeded), check whether
        # any configured alarm threshold has been breached. If so, and
        # the alarm has not already been sent in the current period,
        # dispatch a notification and update last_alarmed_at.
        # ------------------------------------------------------------------
        if self._alarm_threshold_store is not None and self._notification_dispatcher is not None:
            await self._check_alarm_thresholds(
                dept_id=dept_id,
                user_id=user_id,
                caps=caps,
                usage=usage,
            )

        return BudgetDecision.allow()

    # ------------------------------------------------------------------
    # Helpers (kept on the class so subclasses can swap them out)
    # ------------------------------------------------------------------

    async def _usage(
        self,
        *,
        dept_id: str,
        user_id: str | None,
    ) -> BudgetUsage:
        """Aggregate the running production cost for the four scopes.

        Each branch issues a single ``SUM(cost_usd)`` against
        ``shared.cost_tracking`` filtered by ``cost_tag='production'``
        (sandbox / probe rows are excluded). The
        user-scope branches are skipped when ``user_id is None`` and
        return ``Decimal("0")`` so the caller's monotone comparison
        ``usage >= cap`` cannot trip on an unattributed workflow.
        """

        weekly_iv = _interval_str(self.WEEKLY_INTERVAL)
        monthly_iv = _interval_str(self.MONTHLY_INTERVAL)

        dept_weekly = _to_decimal(
            await self._usage_query.fetchval(_SQL_USAGE_DEPT, dept_id, weekly_iv)
        )
        dept_monthly = _to_decimal(
            await self._usage_query.fetchval(_SQL_USAGE_DEPT, dept_id, monthly_iv)
        )

        if user_id is None:
            return BudgetUsage(
                dept_weekly_usd=dept_weekly,
                user_weekly_usd=Decimal("0"),
                dept_monthly_usd=dept_monthly,
                user_monthly_usd=Decimal("0"),
            )

        user_weekly = _to_decimal(
            await self._usage_query.fetchval(
                _SQL_USAGE_USER, dept_id, weekly_iv, user_id
            )
        )
        user_monthly = _to_decimal(
            await self._usage_query.fetchval(
                _SQL_USAGE_USER, dept_id, monthly_iv, user_id
            )
        )

        return BudgetUsage(
            dept_weekly_usd=dept_weekly,
            user_weekly_usd=user_weekly,
            dept_monthly_usd=dept_monthly,
            user_monthly_usd=user_monthly,
        )

    async def _check_alarm_thresholds(
        self,
        *,
        dept_id: str,
        user_id: str | None,
        caps: BudgetCaps,
        usage: BudgetUsage,
    ) -> None:
        """Check configured alarm thresholds and dispatch notifications.

        For each threshold configured in ``automation.budget_alarm_thresholds``,
        computes the current usage percentage against the corresponding cap.
        If the percentage meets or exceeds ``threshold_pct`` and the alarm
        has not already been sent in the current period, dispatches a
        notification and updates ``last_alarmed_at``.

        This method is best-effort: notification failures are logged but
        do not affect the allow/deny decision (which has already been made
        by the caller).
        """

        assert self._alarm_threshold_store is not None
        assert self._notification_dispatcher is not None

        try:
            thresholds = await self._alarm_threshold_store.get_thresholds(dept_id)
        except Exception as exc:  # noqa: BLE001 - best-effort
            _LOG.warning(
                "budget alarm threshold check: failed to fetch thresholds "
                "for dept=%s: %s",
                dept_id,
                exc,
            )
            return

        now = self._clock()

        for threshold in thresholds:
            # Determine the usage and cap for this threshold's period + scope
            usage_val, cap_val = self._resolve_threshold_usage_and_cap(
                threshold=threshold,
                caps=caps,
                usage=usage,
                user_id=user_id,
            )

            # Skip user-scope thresholds when no user_id is provided
            if usage_val is None or cap_val is None:
                continue

            # Skip if cap is zero (avoid division by zero)
            if cap_val <= Decimal("0"):
                continue

            # Compute percentage used
            pct_used = (usage_val / cap_val) * Decimal("100")

            # Check if threshold is breached
            if pct_used < Decimal(str(threshold.threshold_pct)):
                continue

            # Check if already alarmed in the same period
            if self._already_alarmed_in_period(
                last_alarmed_at=threshold.last_alarmed_at,
                period=threshold.period,
                now=now,
            ):
                continue

            # Dispatch the alarm notification (best-effort)
            try:
                await self._notification_dispatcher.send_budget_alarm(
                    channel=threshold.notify_channel,
                    dept_id=dept_id,
                    period=threshold.period,
                    scope=threshold.scope,
                    current_usd=usage_val,
                    cap_usd=cap_val,
                    threshold_pct=threshold.threshold_pct,
                    pct_used=pct_used,
                )
            except Exception as exc:  # noqa: BLE001 - best-effort
                _LOG.warning(
                    "budget alarm dispatch failed for dept=%s period=%s "
                    "scope=%s channel=%s: %s",
                    dept_id,
                    threshold.period,
                    threshold.scope,
                    threshold.notify_channel,
                    exc,
                )
                continue

            # Update last_alarmed_at (best-effort)
            try:
                await self._alarm_threshold_store.update_last_alarmed_at(
                    threshold_id=threshold.id,
                    alarmed_at=now,
                )
            except Exception as exc:  # noqa: BLE001 - best-effort
                _LOG.warning(
                    "budget alarm: failed to update last_alarmed_at for "
                    "threshold=%s: %s",
                    threshold.id,
                    exc,
                )

            # Write audit event for the alarm
            try:
                await self._audit_logger.write(
                    AuditEvent(
                        actor_id="system",
                        actor_role="system",
                        dept_id=dept_id,
                        action="budget_alarm_triggered",
                        resource=f"department:{dept_id}",
                        result="success",
                        timestamp=now,
                        payload={
                            "period": threshold.period,
                            "scope": threshold.scope,
                            "threshold_pct": threshold.threshold_pct,
                            "current_usd": _decimal_to_str(usage_val),
                            "cap_usd": _decimal_to_str(cap_val),
                            "pct_used": _decimal_to_str(pct_used),
                            "notify_channel": threshold.notify_channel,
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001 - best-effort
                _LOG.warning(
                    "budget alarm: failed to write audit for threshold=%s: %s",
                    threshold.id,
                    exc,
                )

    def _resolve_threshold_usage_and_cap(
        self,
        *,
        threshold: AlarmThreshold,
        caps: BudgetCaps,
        usage: BudgetUsage,
        user_id: str | None,
    ) -> tuple[Decimal | None, Decimal | None]:
        """Map a threshold's period + scope to the corresponding usage and cap values.

        Returns ``(None, None)`` when the threshold targets a user scope
        but no ``user_id`` is available (system workflows skip user-scope
        alarm checks).
        """

        if threshold.period == "weekly" and threshold.scope == "dept":
            return usage.dept_weekly_usd, caps.weekly_usd_dept
        elif threshold.period == "weekly" and threshold.scope == "user":
            if user_id is None:
                return None, None
            return usage.user_weekly_usd, caps.weekly_usd_user
        elif threshold.period == "monthly" and threshold.scope == "dept":
            return usage.dept_monthly_usd, caps.monthly_usd_dept
        elif threshold.period == "monthly" and threshold.scope == "user":
            if user_id is None:
                return None, None
            return usage.user_monthly_usd, caps.monthly_usd_user
        else:
            _LOG.warning(
                "budget alarm: unknown period=%s scope=%s combination",
                threshold.period,
                threshold.scope,
            )
            return None, None

    @staticmethod
    def _already_alarmed_in_period(
        *,
        last_alarmed_at: datetime | None,
        period: Literal["weekly", "monthly"],
        now: datetime,
    ) -> bool:
        """Check if an alarm was already sent in the current period.

        For weekly periods, the period boundary is 7 days ago.
        For monthly periods, the period boundary is 30 days ago.

        Returns ``True`` if ``last_alarmed_at`` falls within the current
        period window (alarm should NOT be re-sent).
        """

        if last_alarmed_at is None:
            return False

        if period == "weekly":
            period_start = now - timedelta(days=7)
        else:  # monthly
            period_start = now - timedelta(days=30)

        return last_alarmed_at >= period_start

    async def _emit_denied(
        self,
        *,
        dept_id: str,
        user_id: str | None,
        scope: DenyScope,
        limit: Decimal,
        usage_value: Decimal,
    ) -> None:
        """Write the mandatory ``budget_exceeded`` audit event.

        The event matches the schema referenced by
        ``test_audit_log_integrity_ops.py``:

        * ``action="budget_exceeded"``
        * ``actor_role="system"`` - the policy enforces caps as a
          background gate; the human actor is recorded separately on
          the surrounding workflow start audit row.
        * ``result="denied"`` - Postgres ``CHECK`` accepts this value
          (see ``audit_logger.event.AUDIT_RESULTS``).
        * ``payload`` carries the four diagnostic fields the
          ``/costs`` panel renders so admins can tell **which** cap
          tripped without having to re-query usage.
        """

        payload: dict[str, Any] = {
            "scope": scope,
            "limit": _decimal_to_str(limit),
            "usage": _decimal_to_str(usage_value),
        }
        if user_id is not None:
            payload["user_id"] = user_id

        await self._audit_logger.write(
            AuditEvent(
                actor_id=user_id or "system",
                actor_role="system",
                dept_id=dept_id,
                action="budget_exceeded",
                resource=f"department:{dept_id}",
                result="denied",
                timestamp=self._clock(),
                payload=payload,
            )
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _default_clock() -> datetime:
    """Return ``datetime.now(timezone.utc)`` - overridable for tests."""

    return datetime.now(timezone.utc)


def _interval_str(td: timedelta) -> str:
    """Format ``td`` as a Postgres ``INTERVAL`` literal.

    Postgres' ``$2::interval`` cast accepts both ``'7 days'`` and
    ISO-8601 ``'P7D'``. We prefer the ``N days`` form because the
    surrounding SQL string is human-read at log-debug time; the
    days-only granularity matches the weekly / monthly windows we
    care about and avoids any surprise from sub-day rounding.
    """

    days = int(td.total_seconds() // 86400)
    return f"{days} days"


def _to_decimal(value: Any) -> Decimal:
    """Coerce an asyncpg result to :class:`Decimal`, treating ``None`` as zero.

    asyncpg returns :class:`decimal.Decimal` for ``NUMERIC`` columns,
    but the in-memory test fakes commonly hand back plain ``int`` or
    ``float`` values. This helper normalises both paths and maps
    ``None`` (the literal ``NULL`` from an empty ``SUM``) to
    ``Decimal("0")`` so the caller's comparison logic does not need
    to special-case it.
    """

    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    # ``str`` round-trip avoids ``float`` precision drift when the
    # fake hands back ``500.0`` instead of ``Decimal("500")``.
    return Decimal(str(value))


def _decimal_to_str(value: Decimal) -> str:
    """Serialise a :class:`Decimal` for an audit JSON payload.

    JSON has no canonical ``Decimal`` representation; downstream
    consumers (the ``/costs`` panel, Loki search) expect a string
    rather than a float so precision is preserved end-to-end.
    """

    return format(value, "f")


# ---------------------------------------------------------------------------
# Lightweight in-memory caps provider (config-driven wiring helper)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StaticBudgetCapsProvider:
    """Trivial :class:`BudgetCapsProvider` backed by an immutable mapping.

    Suitable for production wiring that reads ``config/departments.json``
    once at boot (the file is the source of truth per
    ``departments.README.md``) and for tests that need a deterministic
    cap table. Hot-reload of caps lives outside this class - callers
    rebuild the provider on config refresh and swap it on the
    long-lived :class:`BudgetCapPolicy` instance.

    Args:
        caps: Mapping of ``dept_id`` to :class:`BudgetCaps`.
    """

    caps: Mapping[str, BudgetCaps]

    def get(self, dept_id: str) -> BudgetCaps:
        try:
            return self.caps[dept_id]
        except KeyError as exc:  # pragma: no cover - re-raised as-is
            raise KeyError(
                f"no budget_caps registered for dept_id={dept_id!r}; "
                "verify the entry exists in config/departments.json and "
                "that the caps_provider was rebuilt on the most recent "
                "config reload"
            ) from exc


# ---------------------------------------------------------------------------
# Optional helper: map a deny decision to an HTTP 429 response body
# ---------------------------------------------------------------------------


def deny_response_body(decision: BudgetDecision, *, dept_id: str) -> dict[str, Any]:
    """Render a JSON body for the HTTP 429 response on deny.

    Centralised here so the workflow start endpoint and any future
    bulk-start surface emit the same shape. Callers attach the body
    to an HTTP 429 response; the ``Retry-After`` header (if any) is
    decided by the caller based on the offending scope.

    Args:
        decision: A deny decision from :meth:`BudgetCapPolicy.enforce`.
        dept_id: The department identifier the caller passed in.

    Returns:
        A mapping ready to be serialised to JSON.

    Raises:
        ValueError: If ``decision.allowed`` is ``True``.
    """

    if decision.allowed:
        raise ValueError(
            "deny_response_body called on an allow decision; "
            "the workflow start handler should not be emitting 429 here."
        )
    assert decision.deny_scope is not None  # narrowing for type-checkers
    return {
        "error": "budget_exceeded",
        "dept_id": dept_id,
        "scope": decision.deny_scope,
    }


# ---------------------------------------------------------------------------
# Configuration error response helper
# ---------------------------------------------------------------------------


def configuration_error_response(*, dept_id: str) -> dict[str, Any]:
    """Render a JSON body for a configuration error when dept_id is undefined.

    Called when the ``dept_id`` is not found in the
    :class:`BudgetCapsProvider`. The workflow start handler maps this
    to an appropriate HTTP error response (e.g. 422 or 400).

    Args:
        dept_id: The department identifier that was not found.

    Returns:
        A mapping ready to be serialised to JSON.
    """

    return {
        "error": "configuration_error",
        "message": (
            f"dept_id '{dept_id}' is not defined in budget_caps configuration; "
            "verify the department exists in config/departments.json"
        ),
        "dept_id": dept_id,
    }


# ---------------------------------------------------------------------------
# Enhanced check_budget - pre-workflow budget check with 90% warnings
# ---------------------------------------------------------------------------


async def check_budget(
    dept_id: str,
    user_id: str | None,
    issue_key: str,
    *,
    policy: BudgetCapPolicy,
    jira_comment_callback: JiraCommentCallback | None = None,
) -> BudgetCheckResult:
    """Pre-workflow budget check with 90% threshold warnings.

    This entry point wraps :meth:`BudgetCapPolicy.enforce` with
    additional logic:

    1. **Undefined dept_id**: If the dept is not in the caps provider,
       returns a denied result with a configuration error scope.
    2. **Limit exceeded** (usage >= cap): Returns denied with the
       exceeded scope. The caller should respond with HTTP 429.
       A ``budget_exceeded`` audit record is written by the underlying
       policy. A Jira comment is posted (best-effort) identifying
       the exceeded scope.
    3. **90% threshold**: If any scope reaches 90% of its cap but
       does not exceed it, the workflow is allowed to proceed but a
       warning comment is posted to Jira identifying which scope(s)
       reached 90%.
    4. **Below 90%**: Workflow proceeds with no warnings.

    Args:
        dept_id: Department identifier.
        user_id: Optional end-user identifier. ``None`` for system
            workflows (user-scope checks are skipped).
        issue_key: Jira issue key for posting warning/denial comments.
        policy: The :class:`BudgetCapPolicy` instance to use.
        jira_comment_callback: Optional async callback for posting
            Jira comments. Signature: ``async (issue_key, body) -> None``.
            Best-effort - failures are logged but do not block.

    Returns:
        :class:`BudgetCheckResult` with the decision and usage data.
    """

    if not isinstance(dept_id, str) or not dept_id:
        raise ValueError("dept_id must be a non-empty string")

    # --- Undefined dept_id check ---
    try:
        caps = policy._caps_provider.get(dept_id)
    except KeyError:
        _LOG.warning(
            "check_budget: dept_id=%r not found in budget_caps configuration",
            dept_id,
        )
        return BudgetCheckResult(
            allowed=False,
            exceeded_scope="configuration_error",
            warning_scopes=[],
            current_usage={},
        )

    # --- Fetch current usage ---
    usage = await policy._usage(dept_id=dept_id, user_id=user_id)

    current_usage: dict[str, str] = {
        "dept_weekly": _decimal_to_str(usage.dept_weekly_usd),
        "dept_monthly": _decimal_to_str(usage.dept_monthly_usd),
    }
    if user_id is not None:
        current_usage["user_weekly"] = _decimal_to_str(usage.user_weekly_usd)
        current_usage["user_monthly"] = _decimal_to_str(usage.user_monthly_usd)

    # --- Check for limit exceeded (usage >= cap) ---
    exceeded_scope: str | None = None

    scope_checks: list[tuple[DenyScope, Decimal, Decimal]] = [
        ("dept_weekly", usage.dept_weekly_usd, caps.weekly_usd_dept),
        ("dept_monthly", usage.dept_monthly_usd, caps.monthly_usd_dept),
    ]
    if user_id is not None:
        scope_checks.append(
            ("user_weekly", usage.user_weekly_usd, caps.weekly_usd_user)
        )
        scope_checks.append(
            ("user_monthly", usage.user_monthly_usd, caps.monthly_usd_user)
        )

    for scope, usage_val, cap_val in scope_checks:
        if cap_val > Decimal("0") and usage_val >= cap_val:
            exceeded_scope = scope
            break

    if exceeded_scope is not None:
        # Write audit record via the existing policy mechanism
        await policy._emit_denied(
            dept_id=dept_id,
            user_id=user_id,
            scope=exceeded_scope,  # type: ignore[arg-type]
            limit=_get_cap_for_scope(caps, exceeded_scope),
            usage_value=_get_usage_for_scope(usage, exceeded_scope),
        )

        # Post Jira comment (best-effort)
        if jira_comment_callback is not None:
            comment_body = (
                f" Bütçe limiti aşıldı - iş akışı reddedildi.\n\n"
                f"Aşılan kapsam: **{exceeded_scope}**\n"
                f"Mevcut kullanım: ${_decimal_to_str(_get_usage_for_scope(usage, exceeded_scope))}\n"
                f"Limit: ${_decimal_to_str(_get_cap_for_scope(caps, exceeded_scope))}"
            )
            try:
                await jira_comment_callback(issue_key, comment_body)
            except Exception as exc:  # noqa: BLE001 - best-effort
                _LOG.warning(
                    "check_budget: failed to post denial comment to %s: %s",
                    issue_key,
                    exc,
                )

        return BudgetCheckResult(
            allowed=False,
            exceeded_scope=exceeded_scope,
            warning_scopes=[],
            current_usage=current_usage,
        )

    # --- Check for 90% threshold warnings ---
    warning_scopes: list[str] = []

    for scope, usage_val, cap_val in scope_checks:
        if cap_val > Decimal("0") and usage_val >= cap_val * WARNING_THRESHOLD:
            warning_scopes.append(scope)

    # Post warning comment to Jira (best-effort) if any scope at 90%
    if warning_scopes and jira_comment_callback is not None:
        scopes_str = ", ".join(warning_scopes)
        comment_body = (
            f" Bütçe uyarısı - aşağıdaki kapsam(lar) %90 eşiğine ulaştı:\n\n"
            f"**{scopes_str}**\n\n"
            f"İş akışı başlatıldı ancak bütçe limitine yaklaşılıyor."
        )
        try:
            await jira_comment_callback(issue_key, comment_body)
        except Exception as exc:  # noqa: BLE001 - best-effort
            _LOG.warning(
                "check_budget: failed to post warning comment to %s: %s",
                issue_key,
                exc,
            )

    return BudgetCheckResult(
        allowed=True,
        exceeded_scope=None,
        warning_scopes=warning_scopes,
        current_usage=current_usage,
    )


# ---------------------------------------------------------------------------
# Pre-LLM-call inline budget guard
# ---------------------------------------------------------------------------


async def pre_llm_budget_guard(
    dept_id: str,
    user_id: str | None,
    *,
    policy: BudgetCapPolicy,
) -> bool:
    """Recheck budget immediately before an LLM call.

    This is the inline guard that runs
    inside a workflow just before issuing an LLM request. It rechecks
    the current spending from the ``cost_tracking`` table and blocks
    the call if any limit is exceeded.

    Unlike :func:`check_budget`, this function:
    - Does NOT post Jira comments (the workflow is already running).
    - Does NOT write audit records (the workflow stop handler does).
    - Returns a simple boolean for fast inline decision-making.

    Args:
        dept_id: Department identifier.
        user_id: Optional end-user identifier.
        policy: The :class:`BudgetCapPolicy` instance to use.

    Returns:
        ``True`` if the LLM call is allowed (all scopes below cap).
        ``False`` if any scope is exceeded (caller should block the
        LLM call and stop the workflow).
    """

    if not isinstance(dept_id, str) or not dept_id:
        _LOG.error("pre_llm_budget_guard: dept_id is empty or invalid")
        return False

    # Undefined dept_id  block (fail-closed)
    try:
        caps = policy._caps_provider.get(dept_id)
    except KeyError:
        _LOG.error(
            "pre_llm_budget_guard: dept_id=%r not found in caps provider",
            dept_id,
        )
        return False

    usage = await policy._usage(dept_id=dept_id, user_id=user_id)

    # Check each scope - block on first exceeded
    if usage.dept_weekly_usd >= caps.weekly_usd_dept:
        _LOG.warning(
            "pre_llm_budget_guard: dept_weekly exceeded for dept=%s "
            "(usage=%s, cap=%s)",
            dept_id,
            usage.dept_weekly_usd,
            caps.weekly_usd_dept,
        )
        return False

    if user_id is not None and usage.user_weekly_usd >= caps.weekly_usd_user:
        _LOG.warning(
            "pre_llm_budget_guard: user_weekly exceeded for dept=%s user=%s "
            "(usage=%s, cap=%s)",
            dept_id,
            user_id,
            usage.user_weekly_usd,
            caps.weekly_usd_user,
        )
        return False

    if usage.dept_monthly_usd >= caps.monthly_usd_dept:
        _LOG.warning(
            "pre_llm_budget_guard: dept_monthly exceeded for dept=%s "
            "(usage=%s, cap=%s)",
            dept_id,
            usage.dept_monthly_usd,
            caps.monthly_usd_dept,
        )
        return False

    if user_id is not None and usage.user_monthly_usd >= caps.monthly_usd_user:
        _LOG.warning(
            "pre_llm_budget_guard: user_monthly exceeded for dept=%s user=%s "
            "(usage=%s, cap=%s)",
            dept_id,
            user_id,
            usage.user_monthly_usd,
            caps.monthly_usd_user,
        )
        return False

    return True


# ---------------------------------------------------------------------------
# Admin Dashboard budget usage snapshot
# ---------------------------------------------------------------------------


async def get_budget_usage_snapshot(
    dept_id: str,
    user_id: str | None,
    *,
    policy: BudgetCapPolicy,
) -> dict[str, Any]:
    """Return budget usage data for the Admin Dashboard.

    Exposes current usage and cap information with max 60s delay
    (the delay is bounded by the caller's cache/poll interval, not
    by this function which always queries live data).

    Args:
        dept_id: Department identifier.
        user_id: Optional user to include user-scoped data.
        policy: The :class:`BudgetCapPolicy` instance.

    Returns:
        Dict with ``caps``, ``usage``, ``percentages``, and
        ``warning_scopes`` suitable for JSON serialisation.

    Raises:
        KeyError: If ``dept_id`` is not in the caps provider.
    """

    caps = policy._caps_provider.get(dept_id)
    usage = await policy._usage(dept_id=dept_id, user_id=user_id)

    def _pct(usage_val: Decimal, cap_val: Decimal) -> str:
        """Compute usage percentage as a string (e.g. '85.2')."""
        if cap_val == Decimal("0"):
            return "0"
        return _decimal_to_str((usage_val / cap_val) * Decimal("100"))

    percentages: dict[str, str] = {
        "dept_weekly": _pct(usage.dept_weekly_usd, caps.weekly_usd_dept),
        "dept_monthly": _pct(usage.dept_monthly_usd, caps.monthly_usd_dept),
    }
    if user_id is not None:
        percentages["user_weekly"] = _pct(
            usage.user_weekly_usd, caps.weekly_usd_user
        )
        percentages["user_monthly"] = _pct(
            usage.user_monthly_usd, caps.monthly_usd_user
        )

    # Determine which scopes are at warning level
    warning_scopes: list[str] = []
    scope_pairs: list[tuple[str, Decimal, Decimal]] = [
        ("dept_weekly", usage.dept_weekly_usd, caps.weekly_usd_dept),
        ("dept_monthly", usage.dept_monthly_usd, caps.monthly_usd_dept),
    ]
    if user_id is not None:
        scope_pairs.append(
            ("user_weekly", usage.user_weekly_usd, caps.weekly_usd_user)
        )
        scope_pairs.append(
            ("user_monthly", usage.user_monthly_usd, caps.monthly_usd_user)
        )

    for scope, usage_val, cap_val in scope_pairs:
        if cap_val > Decimal("0") and usage_val >= cap_val * WARNING_THRESHOLD:
            warning_scopes.append(scope)

    return {
        "dept_id": dept_id,
        "caps": {
            "dept_weekly": _decimal_to_str(caps.weekly_usd_dept),
            "dept_monthly": _decimal_to_str(caps.monthly_usd_dept),
            "user_weekly": _decimal_to_str(caps.weekly_usd_user),
            "user_monthly": _decimal_to_str(caps.monthly_usd_user),
        },
        "usage": {
            "dept_weekly": _decimal_to_str(usage.dept_weekly_usd),
            "dept_monthly": _decimal_to_str(usage.dept_monthly_usd),
            "user_weekly": _decimal_to_str(usage.user_weekly_usd),
            "user_monthly": _decimal_to_str(usage.user_monthly_usd),
        },
        "percentages": percentages,
        "warning_scopes": warning_scopes,
    }


# ---------------------------------------------------------------------------
# Internal helpers for the enhanced functions
# ---------------------------------------------------------------------------


def _get_cap_for_scope(caps: BudgetCaps, scope: str) -> Decimal:
    """Return the cap value for a given scope name."""

    mapping: dict[str, Decimal] = {
        "dept_weekly": caps.weekly_usd_dept,
        "user_weekly": caps.weekly_usd_user,
        "dept_monthly": caps.monthly_usd_dept,
        "user_monthly": caps.monthly_usd_user,
    }
    return mapping[scope]


def _get_usage_for_scope(usage: BudgetUsage, scope: str) -> Decimal:
    """Return the usage value for a given scope name."""

    mapping: dict[str, Decimal] = {
        "dept_weekly": usage.dept_weekly_usd,
        "user_weekly": usage.user_weekly_usd,
        "dept_monthly": usage.dept_monthly_usd,
        "user_monthly": usage.user_monthly_usd,
    }
    return mapping[scope]
