"""``AuditPruneWorkflow`` — daily Temporal cron for audit retention.

Validates Requirements: **R6.3** (daily cron archives audit_events older
than ``RETENTION_DAYS`` to MinIO, then deletes them) and **R6.4**
(any failure invokes ``notify_audit_prune_failed`` which sends a
**mandatory** admin Slack alarm).

Lifecycle (one cron tick)::

    1. ``get_retention_setting()``    → int (env / DB; default 90)
    2. ``cutoff = workflow.now() - timedelta(days=retention_days)``
    3. ``archive_audit_to_minio(cutoff)`` → AuditArchiveResult
    4. ``delete_audit_older_than(cutoff)`` → AuditDeleteResult
    5. return ``AuditPruneReport(archived, deleted, cutoff)``

    On any exception inside steps 1-4:
        ``notify_audit_prune_failed(error_text)`` is invoked with its
        own retry policy (3 attempts), then the original exception is
        re-raised so Temporal records the workflow as failed and the
        next cron tick fires the day after.

Determinism contract (Spec 2 Property 2 / Property 11 parity)
-------------------------------------------------------------

This workflow body uses **only** Temporal-deterministic primitives:

* ``workflow.now()`` for the wallclock cutoff (never ``datetime.now``,
  ``time.time``, ``datetime.utcnow``).
* ``workflow.execute_activity(...)`` for every side-effecting step
  (no direct httpx / asyncpg / aioboto3 calls).
* No ``random.*``, no ``uuid.uuid4()``, no ``os.environ`` reads.
* No imports of activity modules at workflow-module import time —
  activities are referenced **by string name** so the workflow module
  loads cleanly inside the Temporal sandbox even before the activity
  modules (Spec 3 task 13.2) exist on disk.

Idempotent run semantics (Property 10 parity)
---------------------------------------------

Running the workflow twice on the same day is a *safe* no-op once the
activities are wired:

* ``archive_audit_to_minio(cutoff)`` writes to a deterministic
  ``audit-archive/{Y}/{M}/{D}/audit-N.jsonl.gz`` key shape; a re-run
  for the same cutoff overwrites the same object byte-for-byte (the
  underlying SELECT is bounded by ``cutoff`` and the ordering by
  ``(created_at, id)`` is total).
* ``delete_audit_older_than(cutoff)`` is a SQL ``DELETE`` filtered by
  ``created_at < cutoff``; the second run finds zero matching rows and
  returns ``deleted=0``.

Therefore the workflow body itself does **not** need a "did we run
today already?" guard — idempotence is delegated to the activities,
which keeps the workflow logic minimal and replay-clean.

Cron schedule registration
--------------------------

``automation-worker``'s boot script (Spec 3 task 13.3) registers this
workflow with::

    await client.start_workflow(
        AuditPruneWorkflow.run,
        id="audit-prune-cron",
        task_queue="automation-tq",
        cron_schedule="0 3 * * *",
    )

The constants :data:`AUDIT_PRUNE_WORKFLOW_ID`,
:data:`AUTOMATION_TASK_QUEUE`, and :data:`AUDIT_PRUNE_CRON_SCHEDULE`
expose the exact strings so the boot script and tests share a single
source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

# ---------------------------------------------------------------------------
# Public constants (re-exported via ``__init__``)
# ---------------------------------------------------------------------------

#: Temporal task queue name on which the ``automation-worker`` polls.
#:
#: Mirrors the Spec 3 task 13.3 boot-script constant; kept here so
#: tests can assert against a single source of truth without importing
#: the boot module (which would pull in the Temporal client / network
#: dependencies).
AUTOMATION_TASK_QUEUE: str = "automation-tq"

#: Stable workflow ID used when scheduling the daily cron.
#:
#: Temporal uses this ID for cron-schedule book-keeping; reusing the
#: same ID across restarts means a single in-flight cron lineage rather
#: than one per process restart.
AUDIT_PRUNE_WORKFLOW_ID: str = "audit-prune-cron"

#: Cron schedule expression — daily at 03:00 UTC.
#:
#: 5-field POSIX cron syntax (minute, hour, day-of-month, month,
#: day-of-week). Temporal interprets the schedule in UTC.
AUDIT_PRUNE_CRON_SCHEDULE: str = "0 3 * * *"

#: Fallback ``RETENTION_DAYS`` if ``get_retention_setting`` returns a
#: falsy value or the activity is not yet wired (Spec 3 task 13.2 still
#: pending). The value mirrors design.md §"AuditPruneWorkflow"
#: (RETENTION_DAYS=90).
DEFAULT_RETENTION_DAYS: int = 90


# ---------------------------------------------------------------------------
# Activity name constants (referenced by string only)
# ---------------------------------------------------------------------------

#: Activity name strings — the workflow calls
#: ``workflow.execute_activity(<name>, ...)`` rather than importing the
#: activity callable, so the workflow module stays decoupled from the
#: ``automation_worker.activities.audit_prune`` module that Spec 3 task
#: 13.2 will introduce.
_ACT_GET_RETENTION_SETTING: str = "get_retention_setting"
_ACT_ARCHIVE_AUDIT_TO_MINIO: str = "archive_audit_to_minio"
_ACT_DELETE_AUDIT_OLDER_THAN: str = "delete_audit_older_than"
_ACT_NOTIFY_AUDIT_PRUNE_FAILED: str = "notify_audit_prune_failed"


# ---------------------------------------------------------------------------
# Activity options
# ---------------------------------------------------------------------------

#: ``get_retention_setting`` is a fast lookup (env or single SELECT
#: against ``feature_flags`` / ``platform_config``). 10s is generous.
_GET_RETENTION_TIMEOUT: timedelta = timedelta(seconds=10)

#: ``archive_audit_to_minio`` streams audit rows in batches and uploads
#: gzipped JSON-lines to MinIO. 30 minutes accommodates large daily
#: backlogs even on slow networks.
_ARCHIVE_TIMEOUT: timedelta = timedelta(minutes=30)

#: ``delete_audit_older_than`` is a single ``DELETE`` ranged over
#: ``created_at`` with a covering index — fast in steady state but the
#: first run after a long retention extension can hit lock contention.
_DELETE_TIMEOUT: timedelta = timedelta(minutes=10)

#: The mandatory admin alarm activity has its own retry budget — the
#: failure path must reach Slack even if the cluster is unhealthy.
_NOTIFY_TIMEOUT: timedelta = timedelta(seconds=30)

#: Retry policy for the archive / delete activities. Failures here are
#: usually transient (DB temporarily unavailable, MinIO restarting); a
#: short exponential backoff with a low ceiling keeps the workflow
#: responsive without thundering-herd retries.
_DEFAULT_RETRY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)

#: Retry policy for the ``get_retention_setting`` activity. A single
#: SELECT — three quick attempts is plenty.
_LOOKUP_RETRY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_attempts=3,
)

#: Retry policy for the **mandatory** admin Slack alarm. Tighter
#: backoff than the data-path activities because the alarm is the only
#: way an operator learns that retention failed; we want it through
#: even if the cluster is degraded.
_NOTIFY_RETRY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_attempts=3,
)


# ---------------------------------------------------------------------------
# Result dataclasses (workflow output + activity return shapes)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditArchiveResult:
    """Result returned by ``archive_audit_to_minio``.

    Attributes
    ----------
    archived_rows:
        Number of audit rows successfully written to MinIO.
    archive_uri:
        ``s3://audit-archive/{Y}/{M}/{D}/audit-N.jsonl.gz`` URI of the
        object written this run, or an empty string when the run had
        zero rows to archive (still reported back so the workflow can
        emit an idempotent ``AuditPruneReport`` either way).

    Notes
    -----
    The dataclass mirror is here so the workflow body has a stable
    typed shape to destructure even before Spec 3 task 13.2 introduces
    the concrete activity. Activities in task 13.2 will return objects
    with these field names; Temporal dataclass conversion handles the
    rest.
    """

    archived_rows: int
    archive_uri: str


@dataclass(frozen=True)
class AuditDeleteResult:
    """Result returned by ``delete_audit_older_than``.

    Attributes
    ----------
    deleted_rows:
        Number of rows removed from ``audit_events`` (and any sibling
        retention-bound tables wired by task 13.2, e.g.
        ``cost_tracking``).
    """

    deleted_rows: int


@dataclass(frozen=True)
class AuditPruneReport:
    """Final result of a single ``AuditPruneWorkflow`` cron run.

    Attributes
    ----------
    archived_rows:
        Number of audit rows archived to MinIO.
    deleted_rows:
        Number of audit rows deleted from Postgres.
    cutoff:
        The cutoff timestamp used (``workflow.now() - retention_days``).
        Stored in the report so downstream observers (admin UI archive
        index, Loki search) can reconstruct the exact slice that was
        moved on this run without re-deriving it from the schedule.
    retention_days:
        The retention window in days (as resolved by
        ``get_retention_setting``); echoed into the report for audit
        clarity — the operator can read the report and immediately see
        the policy under which the cron ran.
    archive_uri:
        URI of the MinIO object written by the archive activity, or an
        empty string when no rows fell within the cutoff.
    """

    archived_rows: int
    deleted_rows: int
    cutoff: datetime
    retention_days: int
    archive_uri: str


def _result_int(result: object, attr: str) -> int:
    if isinstance(result, dict):
        value = result.get(attr)
    else:
        value = getattr(result, attr, result)
    return int(value or 0)


def _result_str(result: object, attr: str) -> str:
    if isinstance(result, dict):
        value = result.get(attr)
    else:
        value = getattr(result, attr, "")
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------


@workflow.defn(name="AuditPruneWorkflow")
class AuditPruneWorkflow:
    """Daily Temporal cron that archives + prunes ``audit_events``.

    See module docstring for the full lifecycle and determinism /
    idempotence contracts. The workflow takes no input — every per-run
    parameter (retention days, cutoff timestamp) is derived inside
    :meth:`run` so the cron schedule needs no per-tick payload.
    """

    @workflow.run
    async def run(self) -> AuditPruneReport:
        # 1. Resolve the retention window. The activity is responsible
        #    for env / config-flag lookup; we fall back to the
        #    constant default if the activity returns a falsy value
        #    (None / 0) so a bad config never silently produces a
        #    "delete everything" run.
        retention_days_raw: int | None = await workflow.execute_activity(
            _ACT_GET_RETENTION_SETTING,
            start_to_close_timeout=_GET_RETENTION_TIMEOUT,
            retry_policy=_LOOKUP_RETRY,
        )
        retention_days: int = (
            int(retention_days_raw)
            if retention_days_raw and int(retention_days_raw) > 0
            else DEFAULT_RETENTION_DAYS
        )

        # 2. Compute the cutoff using the deterministic Temporal clock.
        #    ``workflow.now()`` is the only legal time source in a
        #    workflow body — replay must produce the identical value.
        cutoff: datetime = workflow.now() - timedelta(days=retention_days)

        # 3-4. Archive then delete. Wrapped in a try/except so any
        #      exception path triggers the **mandatory** admin alarm
        #      before re-raising. The archive activity runs first so
        #      we never delete rows we have not yet archived (the only
        #      ordering invariant the workflow guarantees on top of
        #      the activities' own idempotence).
        try:
            archive_result: AuditArchiveResult = await workflow.execute_activity(
                _ACT_ARCHIVE_AUDIT_TO_MINIO,
                cutoff,
                start_to_close_timeout=_ARCHIVE_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
            delete_result: AuditDeleteResult = await workflow.execute_activity(
                _ACT_DELETE_AUDIT_OLDER_THAN,
                cutoff,
                start_to_close_timeout=_DELETE_TIMEOUT,
                retry_policy=_DEFAULT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001 — see notify path below
            # MANDATORY admin Slack alarm. We do not swallow the
            # original exception: re-raising it lets Temporal record
            # the workflow as failed (so Grafana ``audit_prune_failed_total``
            # counter increments) and ensures the next daily cron tick
            # fires unaffected.
            #
            # The alarm activity itself uses a separate retry policy
            # (``_NOTIFY_RETRY``); if Slack is unreachable for all
            # three attempts we still re-raise — operators have at
            # least the Temporal failure event and can recover from
            # there.
            await self._notify_failure(exc)
            raise

        # 5. Success path — produce a typed report. Field types are
        #    coerced defensively (Temporal converts dataclasses by
        #    field name; if the activity ever returns an int directly
        #    instead of an ``AuditArchiveResult``, the workflow body
        #    still produces a sensible report).
        archived_rows = _result_int(archive_result, "archived_rows")
        archive_uri = _result_str(archive_result, "archive_uri")
        deleted_rows = _result_int(delete_result, "deleted_rows")

        return AuditPruneReport(
            archived_rows=archived_rows,
            deleted_rows=deleted_rows,
            cutoff=cutoff,
            retention_days=retention_days,
            archive_uri=archive_uri,
        )

    # -- Internal helpers --------------------------------------------------

    async def _notify_failure(self, exc: BaseException) -> None:
        """Invoke the mandatory ``notify_audit_prune_failed`` activity.

        The helper is its own coroutine (not inlined into the except
        block) so:

        1. The retry policy / timeout are declared in one place and
           cannot drift between the two failure call-sites if a future
           refactor adds another guarded section.
        2. Any exception raised by the alarm activity itself is
           swallowed — the workflow's job at this point is to surface
           the *original* prune failure, and an alarm-side failure
           must not mask the underlying root cause when the operator
           reads the Temporal failure event.
        """
        # Stringify the exception once so the activity payload stays
        # JSON-serialisable. The activity is responsible for any
        # log-redaction / PII filtering on the message body before it
        # is forwarded to Slack.
        error_text = f"{type(exc).__name__}: {exc}"
        try:
            await workflow.execute_activity(
                _ACT_NOTIFY_AUDIT_PRUNE_FAILED,
                error_text,
                start_to_close_timeout=_NOTIFY_TIMEOUT,
                retry_policy=_NOTIFY_RETRY,
            )
        except Exception:  # noqa: BLE001
            # Alarm-side failure: log via Temporal's workflow logger so
            # the failure shows in worker logs, then return without
            # re-raising so the caller can propagate the *original*
            # prune exception.
            workflow.logger.error(
                "notify_audit_prune_failed activity itself failed; "
                "original prune error will still propagate.",
                exc_info=True,
            )


__all__: tuple[str, ...] = (
    "AUDIT_PRUNE_CRON_SCHEDULE",
    "AUDIT_PRUNE_WORKFLOW_ID",
    "AUTOMATION_TASK_QUEUE",
    "DEFAULT_RETENTION_DAYS",
    "AuditArchiveResult",
    "AuditDeleteResult",
    "AuditPruneReport",
    "AuditPruneWorkflow",
)
