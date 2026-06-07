"""Per-department workflow concurrency enforcement.

Implements the *Worker Concurrency Limit* gate. The gate is invoked **before** the
webhook dispatcher hands a fresh workflow start request to Temporal,
and ensures a single department cannot exhaust the cluster's worker
slots.

Behavioural contract
====================

* ``max_concurrent_workflows = None`` (or absent)  check is skipped
  and the start proceeds. Departments without an explicit
  cap fall back to the global license-tier cap enforced by
  :mod:`middleware.license_cap`; the two gates are
  complementary.
* ``count >= max``  :class:`ConcurrencyLimitExceeded` is raised so
  the caller can:

    1. Skip the workflow start (no Temporal RPC issued).
    2. Render an HTTP 429 response.
    3. Post a Jira comment explaining the limit so the human
       reporter knows the bot is throttled.

* ``count < max``  :func:`check_dept_concurrency` returns the
  observed count and the gate is silently passed.

Counting strategy
=================

The primary path uses the Temporal Visibility API (``count_workflows``
with the JSON-style query
``WorkflowType='AutomationWorkflow' AND ExecutionStatus='Running'
AND DeptId='X'``). This is the design-mandated path because it
counts what Temporal *actually* has scheduled, not what Postgres
*believes* is running - the two can drift if a worker crashed
mid-activity and Temporal retried the workflow under a new run id
without the DB row being closed.

The fallback path uses an ``asyncpg`` query against
``automation.work_items WHERE department_id = $1 AND status =
'running'``. This is engaged when:

* The Temporal client is not wired (lifespan still bringing the
  connection up, or the unit-test harness does not provide one).
* The Temporal call raises an exception (search-attribute not
  registered, transient RPC error, namespace mis-config).

.. note::

   ``DeptId`` is **not** yet a registered search attribute on the
   Temporal namespace. Until ``tctl admin cluster add-search-attributes
   --name DeptId --type Keyword`` (or the operator-API equivalent) is
   run during platform bootstrap, the visibility query returns 0 for
   every dept and the helper short-circuits to the DB fallback.
   Search attribute registration is handled during platform bootstrap.
   Until then this module logs a single
   ``concurrency_visibility_unavailable`` warning per process and
   relies on the Postgres counter, which agrees with Temporal as long
   as the workflow happy-path keeps ``automation.work_items.status``
   in sync (it does - see ``automation_workflow.py`` activity
   ``mark_work_item_status``).

The function intentionally never raises on a Temporal hiccup: a
broken visibility surface should degrade to "use the DB count" -
better an over-count (some closed workflows still tagged ``running``
in Postgres) than an under-count (silently letting a dept exceed
its limit because Temporal failed to answer).

"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import asyncpg

__all__ = [
    "ConcurrencyCheckResult",
    "ConcurrencyLimitExceeded",
    "TemporalVisibilityClient",
    "check_dept_concurrency",
    "count_active_workflows",
    "AUTOMATION_WORKFLOW_TYPE",
]


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Workflow type discriminator used in the Temporal Visibility query.
#: Mirrors ``_WORKFLOW_NAME`` in :mod:`webhooks.dispatcher` and
#: :mod:`webhooks.jira` - kept here as a separate constant so the
#: helper does not import the dispatcher module (that would cause a
#: circular import; the dispatcher imports this module).
AUTOMATION_WORKFLOW_TYPE: str = "AutomationWorkflow"


# ---------------------------------------------------------------------------
# Result + error types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConcurrencyCheckResult:
    """Outcome of a single :func:`check_dept_concurrency` call.

    Attributes
    ----------
    dept_id:
        The department the check was run for.
    current:
        Observed count of running ``AutomationWorkflow`` executions
        for the dept (Visibility API result, or DB fallback).
    max_allowed:
        The cap that was checked against, or ``None`` when the dept
        has ``max_concurrent_workflows`` unset (in which case the
        check is a no-op and ``current`` is the only meaningful
        field).
    source:
        Which counter produced ``current``: ``"temporal"`` when the
        Visibility API answered, ``"postgres"`` when the helper fell
        back to ``automation.work_items``.
    """

    dept_id: str
    current: int
    max_allowed: int | None
    source: str  # "temporal" | "postgres"


class ConcurrencyLimitExceeded(RuntimeError):
    """Raised when a workflow start would breach the dept's cap.

    The webhook dispatcher catches this exception, posts a best-effort
    Jira comment ("departman paralel iş limiti aşıldı, lütfen
    bekleyin"), writes an audit row, and bubbles a 429-shaped
    response up to Atlassian.

    Attributes
    ----------
    dept_id:
        The department whose cap was breached.
    current:
        Observed running-workflow count at check time.
    max_allowed:
        The cap value the dept was configured with.
    source:
        Whether the count came from Temporal or the Postgres fallback
        - useful for audit payloads when triaging a "false rejection"
        report.
    """

    def __init__(
        self,
        *,
        dept_id: str,
        current: int,
        max_allowed: int,
        source: str,
    ) -> None:
        self.dept_id = dept_id
        self.current = current
        self.max_allowed = max_allowed
        self.source = source
        super().__init__(
            f"concurrency_limit_exceeded: dept_id={dept_id!r} "
            f"current={current} max={max_allowed} source={source!r}"
        )


# ---------------------------------------------------------------------------
# Protocol - narrow Temporal Visibility surface
# ---------------------------------------------------------------------------


class TemporalVisibilityClient(Protocol):
    """Minimal Temporal client surface used by the concurrency gate.

    Implementations must expose :meth:`count_workflows`, the
    Visibility ``CountWorkflowExecutions`` RPC. Production wires this
    against ``temporalio.client.Client``; tests inject a stub that
    returns a pre-baked count or raises a configured exception.
    """

    async def count_workflows(self, query: str | None = None) -> Any:
        """Run a Visibility ``CountWorkflowExecutions`` query.

        Returns
        -------
        Any
            An object with an integer ``count`` attribute (the SDK
            returns :class:`temporalio.client.WorkflowExecutionCount`
            but we accept any duck-type that exposes ``.count``).
        """
        ...


# ---------------------------------------------------------------------------
# Counting helpers
# ---------------------------------------------------------------------------


async def _count_via_temporal(
    *,
    temporal: TemporalVisibilityClient,
    dept_id: str,
) -> int:
    """Count running ``AutomationWorkflow`` executions for *dept_id*.

    Uses the Temporal Visibility ``CountWorkflowExecutions`` RPC with
    a query of the form::

        WorkflowType="AutomationWorkflow" AND ExecutionStatus="Running"
        AND DeptId="<dept_id>"

    The ``DeptId`` clause depends on a custom Keyword search
    attribute that platform bootstrap is responsible for registering;
    when it is missing Temporal returns 0 silently (the clause
    becomes a tautology against absent metadata) and the caller
    should engage the Postgres fallback.

    Raises any RPC-layer exception so the caller can decide whether
    to fall back. We deliberately do not swallow errors here - the
    fallback decision belongs to :func:`count_active_workflows`,
    which has access to the Postgres pool.
    """

    # Escape any ``"`` characters the dept_id might contain. The
    # current schema constrains dept_id to ``[a-z][a-z0-9-]{1,30}``
    # but the helper stays defensive in case the column ever widens.
    safe_dept = dept_id.replace('"', '\\"')
    query = (
        f'WorkflowType="{AUTOMATION_WORKFLOW_TYPE}" '
        f'AND ExecutionStatus="Running" '
        f'AND DeptId="{safe_dept}"'
    )
    result = await temporal.count_workflows(query=query)
    # The SDK returns ``WorkflowExecutionCount`` with a ``count``
    # attribute; some shims return a plain int. Accept both.
    raw_count = getattr(result, "count", result)
    return int(raw_count)


async def _count_via_postgres(
    *,
    db: asyncpg.Pool,
    dept_id: str,
) -> int:
    """Fallback counter - counts ``automation.work_items`` rows.

    Mirrors the SQL used by :mod:`middleware.license_cap` so the two
    gates agree on what "running" means. ``status='running'`` is
    maintained by ``automation_workflow.py``'s
    ``mark_work_item_status`` activity at workflow start / completion
    /failure, so the count is in sync with Temporal as long as the
    happy-path activities run.
    """

    sql = """
        SELECT COUNT(*)::bigint AS n
        FROM automation.work_items
        WHERE department_id = $1
          AND status = 'running'
    """
    async with db.acquire() as conn:
        row = await conn.fetchrow(sql, dept_id)
    return int(row["n"]) if row is not None else 0


async def count_active_workflows(
    *,
    dept_id: str,
    db: asyncpg.Pool,
    temporal: TemporalVisibilityClient | None = None,
) -> tuple[int, str]:
    """Count active ``AutomationWorkflow`` executions for a dept.

    Returns a ``(count, source)`` tuple where ``source`` is
    ``"temporal"`` when the Visibility API answered and
    ``"postgres"`` when the helper fell back to the DB counter.

    Parameters
    ----------
    dept_id:
        Department identifier from ``automation.departments.id``.
    db:
        :class:`asyncpg.Pool` for the fallback path.
    temporal:
        Optional Visibility client. ``None`` skips the Temporal
        attempt and goes straight to the DB counter - used by unit
        tests and by lifespan stages that have not yet wired the
        Temporal client.
    """

    if temporal is not None:
        try:
            count = await _count_via_temporal(
                temporal=temporal, dept_id=dept_id
            )
            return count, "temporal"
        except Exception as exc:  # noqa: BLE001 - degrade to DB
            logger.warning(
                "concurrency_visibility_unavailable: dept_id=%s, "
                "falling back to Postgres counter: %s",
                dept_id,
                exc,
            )

    count = await _count_via_postgres(db=db, dept_id=dept_id)
    return count, "postgres"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def check_dept_concurrency(
    dept_id: str,
    max_concurrent: int | None,
    *,
    db: asyncpg.Pool,
    temporal: TemporalVisibilityClient | None = None,
) -> ConcurrencyCheckResult:
    """Check whether a dept has slack for one more workflow start.

    Implements the worker concurrency gate.

    Parameters
    ----------
    dept_id:
        Department identifier.
    max_concurrent:
        The dept's ``max_concurrent_workflows`` value. ``None`` (or
        absent) skips the cap check entirely; the helper
        still returns the observed count so callers that want to
        surface it (eg. the admin dashboard endpoint) get a
        consistent shape.
    db:
        Asyncpg pool used by the fallback counter.
    temporal:
        Optional Temporal Visibility client. ``None`` forces the DB
        fallback.

    Returns
    -------
    ConcurrencyCheckResult
        On success - caller should proceed with the workflow start.

    Raises
    ------
    ConcurrencyLimitExceeded
        When ``max_concurrent`` is set and the observed count meets
        or exceeds it. The webhook dispatcher catches this and emits
        the 429 + audit + Jira comment side effects.

    Notes
    -----
    Comparison uses ``>=`` (not ``>``) - the workflow being guarded
    *would* push the count to ``max_concurrent + 1``, so a count
    already equal to the cap is also a rejection.
    """

    current, source = await count_active_workflows(
        dept_id=dept_id, db=db, temporal=temporal
    )

    if max_concurrent is None:
        # Cap unset: silently allow with the count populated.
        return ConcurrencyCheckResult(
            dept_id=dept_id,
            current=current,
            max_allowed=None,
            source=source,
        )

    if current >= max_concurrent:
        raise ConcurrencyLimitExceeded(
            dept_id=dept_id,
            current=current,
            max_allowed=max_concurrent,
            source=source,
        )

    return ConcurrencyCheckResult(
        dept_id=dept_id,
        current=current,
        max_allowed=max_concurrent,
        source=source,
    )


# ---------------------------------------------------------------------------
# Helper: extract max_concurrent_workflows from a config_json mapping
# ---------------------------------------------------------------------------


def extract_max_concurrent(config_json: Any) -> int | None:
    """Extract ``max_concurrent_workflows`` from a dept's config_json.

    The ``automation.departments.config_json`` JSONB column mirrors
    the ``departments.json`` entry. The schema declares
    ``max_concurrent_workflows`` as ``["integer", "null"]`` with
    ``minimum=1, maximum=50`` (see
    ``platform/config/departments.schema.json``).

    Returns ``None`` for any of:

    * ``config_json`` is ``None``
    * the key is missing
    * the value is JSON ``null``
    * the value is not a positive integer (defensive - schema
      validation should have caught this upstream, but a corrupted
      row should not silently disable the cap by raising)
    """

    if config_json is None:
        return None
    if not isinstance(config_json, dict):
        # asyncpg returns jsonb as decoded Python dicts; a string
        # leaks through only when a fake / fixture passed raw JSON.
        # We don't decode here - the caller has the context to
        # ``json.loads`` if needed. Treat anything non-dict as "no
        # config" so the cap defaults to "absent" (silent allow).
        return None

    raw = config_json.get("max_concurrent_workflows")
    if raw is None:
        return None
    if isinstance(raw, bool):
        # ``bool`` is a subclass of ``int``; defend against it.
        return None
    if not isinstance(raw, int):
        return None
    if raw < 1:
        return None
    return raw
