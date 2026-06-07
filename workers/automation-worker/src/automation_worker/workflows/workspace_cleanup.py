"""``WorkspaceCleanupSchedulerWorkflow`` - hourly Temporal cron for runner workspace disk auto-prune.

Validates Requirements: **G2** (single-runner canonical contract gereği
tek SSH host = tek darboğaz; ``cleanup_policy=never`` task'ları veya
hata fırlatıp ``on_success``'in atlandığı durumlar disk'i doldurur. Bu
workflow ``RUNNER_DISK_WARN_PCT`` / ``RUNNER_DISK_EVICT_PCT`` eşiklerini
kullanarak en eski ``iter-N`` klasörlerini siler ve admin-dashboard'a
banner gönderir).

Lifecycle (one cron tick)::

    1. ``probe_workspace_disk_usage()``  WorkspaceDiskSnapshot
       (single SSH host, returns total/used/usage_pct + thresholds)
    2. If ``usage_pct < warn_pct``:                        no-op
       If ``warn_pct <= usage_pct < evict_pct``:           emit warn
       If ``usage_pct >= evict_pct``:                      emit warn + evict
    3. ``emit_workspace_disk_warning(snapshot)`` (idempotent - Admin
       Dashboard tracks the last warning timestamp and dedupes within
       a 60-minute window).
    4. ``list_workspace_iter_dirs_oldest_first()``
       list[``WorkspaceIterEntry``] (sorted by mtime ascending)
    5. For each entry while ``usage_pct >= evict_pct``:
         ``prune_workspace_iter(entry.path)``  freed_mb
         update local ``usage_pct`` estimate; emit
         ``workspace_auto_pruned`` audit per directory.
    6. return ``WorkspaceCleanupReport(probed, warned, pruned_paths,
       freed_mb_total, final_usage_pct)``.

    On any exception inside the probe / list / prune activities for a
    single entry: the error is recorded in the report but does NOT
    abort the remaining entries - best-effort, mirroring
    ``WebhookRotationFinalizeWorkflow``.

Determinism contract
--------------------

This workflow body uses **only** Temporal-deterministic primitives:

* ``workflow.now()`` for the current timestamp (never ``datetime.now``,
  ``time.time``, ``datetime.utcnow``).
* ``workflow.execute_activity(...)`` for every side-effecting step
  (no direct SSH / paramiko / asyncpg / httpx calls).
* No ``random.*``, no ``uuid.uuid4()``, no ``os.environ`` reads.
* Activities are referenced **by string name** so the workflow module
  loads cleanly inside the Temporal sandbox even when the
  ``execution-runner-worker`` activity modules (which own the SSH
  ``df`` / ``rm -rf`` plumbing) are not on the import path of the
  ``automation-worker`` process.

Single-runner canonical contract (G1)
-------------------------------------

This workflow assumes **exactly one** SSH runner host - the platform
contract under G1. The probe activity implementation reads the
canonical ``SSH_HOST`` env var (with ``SSH_HOST_1`` accepted as a
deprecated alias) and ``RUNNER_BASE_PATH`` to bound the ``df`` and
``find`` commands. There is no fan-out across runners.

Idempotent run semantics
------------------------

Running the workflow twice within the same hour is a *safe* no-op once
the first tick has driven usage below ``evict_pct``:

* ``probe_workspace_disk_usage`` is read-only and stateless.
* ``emit_workspace_disk_warning`` is idempotent at the Admin Dashboard
  layer (60-minute dedup window for the same dept_id="*" / runner
  scope; mirrors ``disk_quota`` activity's existing dedup contract).
* ``prune_workspace_iter`` is naturally idempotent - re-running
  ``rm -rf`` on a path that no longer exists succeeds at the SSH
  layer with exit_code=0 and ``freed_mb=0``.

Therefore the workflow body itself does **not** need a "did we run
this hour already?" guard.

Cron schedule registration
--------------------------

``automation-worker``'s boot script registers this workflow with::

    await client.create_schedule(
        ...,
        WorkspaceCleanupSchedulerWorkflow.run,
        id="workspace-cleanup-scheduler-cron",
        task_queue="automation-tq",
        cron_schedule="0 * * * *",  # every hour at :00
    )

The constants :data:`WORKSPACE_CLEANUP_SCHEDULER_WORKFLOW_ID`,
:data:`AUTOMATION_TASK_QUEUE`, and
:data:`WORKSPACE_CLEANUP_SCHEDULER_CRON_SCHEDULE` expose the exact
strings so the boot script and tests share a single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

# ---------------------------------------------------------------------------
# Public constants (re-exported via ``__init__``)
# ---------------------------------------------------------------------------

#: Temporal task queue name on which the ``automation-worker`` polls.
#: Shared with other automation workflows (audit_prune, bot_branch_retention,
#: webhook_rotation_finalize).
AUTOMATION_TASK_QUEUE: str = "automation-tq"

#: Stable workflow ID used when scheduling the periodic cron.
#: Temporal uses this ID for cron-schedule book-keeping; reusing the
#: same ID across restarts means a single in-flight cron lineage.
WORKSPACE_CLEANUP_SCHEDULER_WORKFLOW_ID: str = "workspace-cleanup-scheduler-cron"

#: Cron schedule expression - every hour at minute 00.
#: 5-field POSIX cron syntax. Temporal interprets in UTC.
WORKSPACE_CLEANUP_SCHEDULER_CRON_SCHEDULE: str = "0 * * * *"


#: Fallback warn threshold (%) - used only when the probe activity
#: cannot resolve ``RUNNER_DISK_WARN_PCT`` from env. Mirrors the
#: ``.env.example`` default.
DEFAULT_WARN_PCT: int = 80

#: Fallback evict threshold (%) - used only when the probe activity
#: cannot resolve ``RUNNER_DISK_EVICT_PCT`` from env. Mirrors the
#: ``.env.example`` default.
DEFAULT_EVICT_PCT: int = 90


# ---------------------------------------------------------------------------
# Activity name constants (referenced by string only)
# ---------------------------------------------------------------------------

#: Activity that runs ``df`` against the SSH runner's ``RUNNER_BASE_PATH``
#: and returns a :class:`WorkspaceDiskSnapshot`. Implementation lives in
#: the ``execution-runner-worker`` (the only worker with SSH credentials).
_ACT_PROBE_WORKSPACE_DISK_USAGE: str = "probe_workspace_disk_usage"

#: Activity that posts a disk-usage warning to the admin-dashboard with
#: 60-minute dedup. Implementation lives in the ``execution-runner-worker``
#: alongside the existing ``disk_quota`` warning helpers, but the API is
#: deliberately decoupled (``dept_id="*"`` to indicate "host-wide").
_ACT_EMIT_WORKSPACE_DISK_WARNING: str = "emit_workspace_disk_warning"

#: Activity that lists ``iter-*`` directories under ``RUNNER_BASE_PATH``,
#: sorted oldest-first by mtime. Implementation lives in
#: ``execution-runner-worker``.
_ACT_LIST_WORKSPACE_ITER_DIRS: str = "list_workspace_iter_dirs_oldest_first"

#: Activity that ``rm -rf``'s a single ``iter-N`` directory and writes
#: a ``workspace_auto_pruned`` audit event. Implementation lives in
#: ``execution-runner-worker``.
_ACT_PRUNE_WORKSPACE_ITER: str = "prune_workspace_iter"


# ---------------------------------------------------------------------------
# Activity options
# ---------------------------------------------------------------------------

#: Timeout for the disk-usage probe - single ``df`` call over SSH.
#: 30s is generous; the existing ``disk_quota`` activity uses the same
#: budget for ``du -sm``.
_PROBE_TIMEOUT: timedelta = timedelta(seconds=30)

#: Timeout for emitting a single warning to admin-dashboard. One HTTP
#: round-trip; 10s is generous.
_WARN_TIMEOUT: timedelta = timedelta(seconds=10)

#: Timeout for listing ``iter-*`` directories. ``find`` over a large
#: workspace tree may take a few seconds on a saturated host.
_LIST_TIMEOUT: timedelta = timedelta(seconds=60)

#: Timeout for a single ``rm -rf`` of an ``iter-N`` directory. Large
#: workspaces (5+ GB) take time to delete; 5 minutes is generous.
_PRUNE_TIMEOUT: timedelta = timedelta(minutes=5)

#: Retry policy for the probe / list / warning activities. Transient
#: SSH unavailability is the most likely failure mode.
_BEST_EFFORT_RETRY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=3,
)

#: Retry policy for each prune activity invocation. Per-entry failures
#: are isolated so a single ``rm -rf`` hiccup does not block the others.
_PRUNE_RETRY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=2,
)

#: Hard cap on the number of ``iter-N`` directories pruned per cron tick.
#: Prevents an unbounded sweep from consuming activity slots; the next
#: tick (one hour later) picks up where this one left off.
MAX_PRUNES_PER_TICK: int = 50


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspaceDiskSnapshot:
    """Read-only snapshot of the runner workspace disk.

    Returned by the ``probe_workspace_disk_usage`` activity.

    Attributes
    ----------
    runner_base_path:
        The absolute path probed (mirrors ``RUNNER_BASE_PATH``).
    total_mb:
        Total filesystem size in MB (the ``df`` ``1K-blocks`` column
        divided by 1024 and rounded).
    used_mb:
        Used space in MB.
    usage_pct:
        ``used_mb / total_mb * 100`` rounded to one decimal place. The
        workflow rounds again on its end before threshold comparisons.
    warn_pct:
        Effective warning threshold (from ``RUNNER_DISK_WARN_PCT`` env
        with :data:`DEFAULT_WARN_PCT` fallback).
    evict_pct:
        Effective eviction threshold (from ``RUNNER_DISK_EVICT_PCT``
        env with :data:`DEFAULT_EVICT_PCT` fallback).
    error:
        ``None`` on success. Human-readable error string when the
        probe failed (in which case the rest of the fields default to
        ``0`` / fallback thresholds).
    """

    runner_base_path: str
    total_mb: int
    used_mb: int
    usage_pct: float
    warn_pct: int = DEFAULT_WARN_PCT
    evict_pct: int = DEFAULT_EVICT_PCT
    error: str | None = None


@dataclass(frozen=True)
class WorkspaceIterEntry:
    """A single ``iter-N`` directory eligible for pruning.

    Attributes
    ----------
    path:
        Absolute path under ``RUNNER_BASE_PATH`` (e.g.
        ``/var/ai-runner/PAY-4211/iter-3``).
    issue_key:
        The Jira issue key extracted from the parent directory name.
    iter_n:
        The iteration number extracted from the directory name.
    mtime_epoch:
        File mtime as a UNIX epoch (seconds). Used by the workflow
        only for stable sorting; the activity already returns the list
        sorted oldest-first.
    size_mb:
        Estimated directory size in MB, used for the running usage
        estimate during the eviction loop.
    """

    path: str
    issue_key: str
    iter_n: int
    mtime_epoch: int
    size_mb: int


@dataclass(frozen=True)
class WorkspacePruneResult:
    """Outcome of a single ``rm -rf`` activity invocation.

    Attributes
    ----------
    path:
        The directory that was targeted.
    success:
        ``True`` when the activity acknowledged the removal (or the
        directory no longer existed - both are idempotent successes).
    freed_mb:
        Bytes freed by the deletion, in MB. Zero when the directory
        was already gone or the activity did not measure size.
    error:
        ``None`` on success; the error string when the activity raised.
    """

    path: str
    success: bool
    freed_mb: int = 0
    error: str | None = None


@dataclass(frozen=True)
class WorkspaceCleanupReport:
    """Final result of a single ``WorkspaceCleanupSchedulerWorkflow`` cron run.

    Attributes
    ----------
    probed:
        ``True`` when the disk-usage probe returned successfully.
    initial_usage_pct:
        The usage percentage observed by the probe before any
        eviction.
    final_usage_pct:
        Estimated usage percentage after all evictions completed (the
        running estimate maintained by the workflow body, not a fresh
        probe).
    warned:
        ``True`` when the workflow emitted a warning to admin-dashboard.
    pruned_paths:
        List of directory paths successfully removed during this tick.
    pruned_count:
        ``len(pruned_paths)`` - exposed for convenience and parity with
        ``WebhookRotationFinalizeReport``.
    freed_mb_total:
        Sum of ``freed_mb`` across all successful prunes.
    errors:
        Per-entry failures recorded best-effort (probe errors land in
        ``snapshot_error``).
    snapshot_error:
        Human-readable probe error when ``probed=False``; ``None``
        otherwise. Prevents conflating probe failures with prune
        failures.
    """

    probed: bool
    initial_usage_pct: float
    final_usage_pct: float
    warned: bool
    pruned_paths: tuple[str, ...] = ()
    pruned_count: int = 0
    freed_mb_total: int = 0
    errors: tuple[WorkspacePruneResult, ...] = ()
    snapshot_error: str | None = None


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


@workflow.defn(name="WorkspaceCleanupSchedulerWorkflow")
class WorkspaceCleanupSchedulerWorkflow:
    """Hourly cron that probes runner workspace disk usage and prunes
    the oldest ``iter-N`` directories when usage crosses the configured
    eviction threshold.

    See module docstring for the full lifecycle, determinism contract,
    and idempotency notes.
    """

    @workflow.run
    async def run(self) -> WorkspaceCleanupReport:
        # ---------- Step 1: probe ---------------------------------------
        try:
            snapshot_dict: dict = await workflow.execute_activity(
                _ACT_PROBE_WORKSPACE_DISK_USAGE,
                start_to_close_timeout=_PROBE_TIMEOUT,
                retry_policy=_BEST_EFFORT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.error(
                "WorkspaceCleanupSchedulerWorkflow: probe failed: %s", exc
            )
            return WorkspaceCleanupReport(
                probed=False,
                initial_usage_pct=0.0,
                final_usage_pct=0.0,
                warned=False,
                pruned_paths=(),
                pruned_count=0,
                freed_mb_total=0,
                errors=(),
                snapshot_error=f"probe_failed: {exc}",
            )

        snapshot = _coerce_snapshot(snapshot_dict)
        if snapshot.error is not None:
            workflow.logger.warning(
                "WorkspaceCleanupSchedulerWorkflow: probe returned error: %s",
                snapshot.error,
            )
            return WorkspaceCleanupReport(
                probed=False,
                initial_usage_pct=0.0,
                final_usage_pct=0.0,
                warned=False,
                pruned_paths=(),
                pruned_count=0,
                freed_mb_total=0,
                errors=(),
                snapshot_error=snapshot.error,
            )

        initial_usage_pct = snapshot.usage_pct

        # ---------- Step 2: decide ---------------------------------------
        below_warn = snapshot.usage_pct < snapshot.warn_pct
        if below_warn:
            workflow.logger.info(
                "WorkspaceCleanupSchedulerWorkflow: usage %.1f%% below warn "
                "threshold %d%% - no action",
                snapshot.usage_pct,
                snapshot.warn_pct,
            )
            return WorkspaceCleanupReport(
                probed=True,
                initial_usage_pct=initial_usage_pct,
                final_usage_pct=initial_usage_pct,
                warned=False,
                pruned_paths=(),
                pruned_count=0,
                freed_mb_total=0,
                errors=(),
                snapshot_error=None,
            )

        # ---------- Step 3: warn (idempotent at admin-dashboard) ---------
        warned = False
        try:
            await workflow.execute_activity(
                _ACT_EMIT_WORKSPACE_DISK_WARNING,
                args=[
                    {
                        "runner_base_path": snapshot.runner_base_path,
                        "total_mb": snapshot.total_mb,
                        "used_mb": snapshot.used_mb,
                        "usage_pct": snapshot.usage_pct,
                        "warn_pct": snapshot.warn_pct,
                        "evict_pct": snapshot.evict_pct,
                    }
                ],
                start_to_close_timeout=_WARN_TIMEOUT,
                retry_policy=_BEST_EFFORT_RETRY,
            )
            warned = True
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "WorkspaceCleanupSchedulerWorkflow: warn emit failed "
                "(continuing): %s",
                exc,
            )

        below_evict = snapshot.usage_pct < snapshot.evict_pct
        if below_evict:
            # Warn-only branch - no eviction needed yet.
            return WorkspaceCleanupReport(
                probed=True,
                initial_usage_pct=initial_usage_pct,
                final_usage_pct=initial_usage_pct,
                warned=warned,
                pruned_paths=(),
                pruned_count=0,
                freed_mb_total=0,
                errors=(),
                snapshot_error=None,
            )

        # ---------- Step 4: list candidates oldest-first ----------------
        try:
            entries_raw: list[dict] = await workflow.execute_activity(
                _ACT_LIST_WORKSPACE_ITER_DIRS,
                start_to_close_timeout=_LIST_TIMEOUT,
                retry_policy=_BEST_EFFORT_RETRY,
            )
        except Exception as exc:  # noqa: BLE001
            workflow.logger.warning(
                "WorkspaceCleanupSchedulerWorkflow: list-iter-dirs failed "
                "(no eviction this tick): %s",
                exc,
            )
            return WorkspaceCleanupReport(
                probed=True,
                initial_usage_pct=initial_usage_pct,
                final_usage_pct=initial_usage_pct,
                warned=warned,
                pruned_paths=(),
                pruned_count=0,
                freed_mb_total=0,
                errors=(),
                snapshot_error=None,
            )

        entries = tuple(_coerce_entry(d) for d in entries_raw)
        if not entries:
            workflow.logger.info(
                "WorkspaceCleanupSchedulerWorkflow: no iter-N directories "
                "to evict (usage=%.1f%%, evict=%d%%)",
                snapshot.usage_pct,
                snapshot.evict_pct,
            )
            return WorkspaceCleanupReport(
                probed=True,
                initial_usage_pct=initial_usage_pct,
                final_usage_pct=initial_usage_pct,
                warned=warned,
                pruned_paths=(),
                pruned_count=0,
                freed_mb_total=0,
                errors=(),
                snapshot_error=None,
            )

        # ---------- Step 5: prune oldest until below evict threshold ----
        pruned_paths: list[str] = []
        errors: list[WorkspacePruneResult] = []
        freed_mb_total = 0
        running_used_mb = snapshot.used_mb
        total_mb = snapshot.total_mb if snapshot.total_mb > 0 else 1
        running_pct = snapshot.usage_pct
        pruned_count = 0

        for entry in entries:
            if running_pct < snapshot.evict_pct:
                break
            if pruned_count >= MAX_PRUNES_PER_TICK:
                workflow.logger.info(
                    "WorkspaceCleanupSchedulerWorkflow: hit "
                    "MAX_PRUNES_PER_TICK=%d; remaining entries deferred "
                    "to next tick",
                    MAX_PRUNES_PER_TICK,
                )
                break

            try:
                prune_result_raw = await workflow.execute_activity(
                    _ACT_PRUNE_WORKSPACE_ITER,
                    args=[
                        {
                            "path": entry.path,
                            "issue_key": entry.issue_key,
                            "iter_n": entry.iter_n,
                            "expected_size_mb": entry.size_mb,
                        }
                    ],
                    start_to_close_timeout=_PRUNE_TIMEOUT,
                    retry_policy=_PRUNE_RETRY,
                )
                prune_result = _coerce_prune_result(prune_result_raw)
            except Exception as exc:  # noqa: BLE001
                err = WorkspacePruneResult(
                    path=entry.path,
                    success=False,
                    freed_mb=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
                errors.append(err)
                continue

            if prune_result.success:
                pruned_paths.append(prune_result.path)
                pruned_count += 1
                freed_mb_total += prune_result.freed_mb
                running_used_mb = max(0, running_used_mb - prune_result.freed_mb)
                running_pct = round((running_used_mb / total_mb) * 100, 1)
            else:
                errors.append(prune_result)

        return WorkspaceCleanupReport(
            probed=True,
            initial_usage_pct=initial_usage_pct,
            final_usage_pct=running_pct,
            warned=warned,
            pruned_paths=tuple(pruned_paths),
            pruned_count=pruned_count,
            freed_mb_total=freed_mb_total,
            errors=tuple(errors),
            snapshot_error=None,
        )


# ---------------------------------------------------------------------------
# Internal coercion helpers (replay-safe - pure dict  dataclass)
# ---------------------------------------------------------------------------


def _coerce_snapshot(d: dict) -> WorkspaceDiskSnapshot:
    """Coerce a dict returned by the probe activity into the dataclass.

    Defensive against missing fields so the workflow keeps running
    against legacy probe implementations during a rolling upgrade.
    """
    return WorkspaceDiskSnapshot(
        runner_base_path=str(d.get("runner_base_path", "")),
        total_mb=int(d.get("total_mb", 0) or 0),
        used_mb=int(d.get("used_mb", 0) or 0),
        usage_pct=float(d.get("usage_pct", 0.0) or 0.0),
        warn_pct=int(d.get("warn_pct", DEFAULT_WARN_PCT) or DEFAULT_WARN_PCT),
        evict_pct=int(
            d.get("evict_pct", DEFAULT_EVICT_PCT) or DEFAULT_EVICT_PCT
        ),
        error=d.get("error") if d.get("error") else None,
    )


def _coerce_entry(d: dict) -> WorkspaceIterEntry:
    """Coerce a dict returned by the list activity into the dataclass."""
    return WorkspaceIterEntry(
        path=str(d.get("path", "")),
        issue_key=str(d.get("issue_key", "")),
        iter_n=int(d.get("iter_n", 0) or 0),
        mtime_epoch=int(d.get("mtime_epoch", 0) or 0),
        size_mb=int(d.get("size_mb", 0) or 0),
    )


def _coerce_prune_result(d: dict) -> WorkspacePruneResult:
    """Coerce a dict returned by the prune activity into the dataclass."""
    return WorkspacePruneResult(
        path=str(d.get("path", "")),
        success=bool(d.get("success", False)),
        freed_mb=int(d.get("freed_mb", 0) or 0),
        error=d.get("error") if d.get("error") else None,
    )


__all__: tuple[str, ...] = (
    "AUTOMATION_TASK_QUEUE",
    "DEFAULT_EVICT_PCT",
    "DEFAULT_WARN_PCT",
    "MAX_PRUNES_PER_TICK",
    "WORKSPACE_CLEANUP_SCHEDULER_CRON_SCHEDULE",
    "WORKSPACE_CLEANUP_SCHEDULER_WORKFLOW_ID",
    "WorkspaceCleanupReport",
    "WorkspaceCleanupSchedulerWorkflow",
    "WorkspaceDiskSnapshot",
    "WorkspaceIterEntry",
    "WorkspacePruneResult",
)
