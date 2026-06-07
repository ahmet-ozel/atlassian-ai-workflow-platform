"""``WebhookRotationFinalizeWorkflow`` - periodic Temporal cron for auto-finalizing webhook secret rotations.

Validates Requirements: **R9.2** - *"``WEBHOOK_ROTATION_OVERLAP_S`` env'i
(default 3600) süre sonunda ``secret_previous``'ı ``null``'a çekecek
background job'ı zamanlar."*

This workflow runs every 10 minutes and scans all dept × provider
webhook entries. For each entry that has an active ``secret_previous``
slot whose ``overlap_until`` timestamp has expired, the workflow
invokes the ``finalize_webhook_rotation`` activity to clear the
previous slot - ensuring that stale overlap windows are automatically
closed even if the operator forgets to click "Finalize" in the UI.

Lifecycle (one cron tick)::

    1. ``list_webhook_entries_with_overlap()``
       → list of ``WebhookOverlapEntry`` (dept_id, provider, overlap_until)
    2. For each entry where ``overlap_until <= workflow.now()``:
         ``finalize_webhook_rotation(dept_id, provider)``
         → clears ``secret_previous`` slot in Vault
         → writes ``webhook_secret_auto_finalized`` audit event
    3. return ``WebhookRotationFinalizeReport(scanned, finalized, errors)``

    On any exception inside the finalize activity for a single entry:
        The error is recorded in the report but does NOT abort the
        remaining entries - the workflow continues best-effort so a
        single Vault hiccup does not block all pending finalizations.

Determinism contract
--------------------

This workflow body uses **only** Temporal-deterministic primitives:

* ``workflow.now()`` for the wallclock comparison (never
  ``datetime.now``, ``time.time``, ``datetime.utcnow``).
* ``workflow.execute_activity(...)`` for every side-effecting step
  (no direct Vault / DB calls).
* No ``random.*``, no ``uuid.uuid4()``, no ``os.environ`` reads.
* Activities are referenced **by string name** so the workflow module
  loads cleanly inside the Temporal sandbox.

Idempotent run semantics
------------------------

Running the workflow multiple times within the same 10-minute window
is a *safe* no-op:

* ``list_webhook_entries_with_overlap`` returns only entries with a
  non-null ``secret_previous`` slot. Once finalized, the entry no
  longer appears in subsequent scans.
* ``finalize_webhook_rotation`` is idempotent: clearing an already-
  empty ``previous`` slot is a no-op at the Vault layer (the
  ``VaultClient.delete`` contract guarantees this).

Cron schedule registration
--------------------------

``automation-worker``'s boot script registers this workflow with::

    await client.start_workflow(
        WebhookRotationFinalizeWorkflow.run,
        id="webhook-rotation-finalize-cron",
        task_queue="automation-tq",
        cron_schedule="*/10 * * * *",
    )

The constants :data:`WEBHOOK_ROTATION_FINALIZE_WORKFLOW_ID`,
:data:`AUTOMATION_TASK_QUEUE`, and
:data:`WEBHOOK_ROTATION_FINALIZE_CRON_SCHEDULE` expose the exact
strings so the boot script and tests share a single source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

# ---------------------------------------------------------------------------
# Public constants (re-exported via ``__init__``)
# ---------------------------------------------------------------------------

#: Temporal task queue name on which the ``automation-worker`` polls.
#: Shared with other automation workflows (audit_prune, bot_branch_retention).
AUTOMATION_TASK_QUEUE: str = "automation-tq"

#: Stable workflow ID used when scheduling the periodic cron.
#: Temporal uses this ID for cron-schedule book-keeping; reusing the
#: same ID across restarts means a single in-flight cron lineage.
WEBHOOK_ROTATION_FINALIZE_WORKFLOW_ID: str = "webhook-rotation-finalize-cron"

#: Cron schedule expression - every 10 minutes.
#: 5-field POSIX cron syntax. Temporal interprets in UTC.
WEBHOOK_ROTATION_FINALIZE_CRON_SCHEDULE: str = "*/10 * * * *"


# ---------------------------------------------------------------------------
# Activity name constants (referenced by string only)
# ---------------------------------------------------------------------------

#: Activity that scans all dept × provider entries and returns those
#: with an active overlap window (non-null ``secret_previous`` slot).
_ACT_LIST_WEBHOOK_ENTRIES_WITH_OVERLAP: str = "list_webhook_entries_with_overlap"

#: Activity that finalizes a single dept × provider rotation by
#: clearing the ``secret_previous`` Vault slot and writing an audit
#: event (``webhook_secret_auto_finalized``).
_ACT_FINALIZE_WEBHOOK_ROTATION: str = "finalize_webhook_rotation"


# ---------------------------------------------------------------------------
# Activity options
# ---------------------------------------------------------------------------

#: Timeout for listing webhook entries - reads Vault metadata for each
#: dept × provider pair. 60s is generous for a typical deployment with
#: a handful of departments.
_LIST_TIMEOUT: timedelta = timedelta(seconds=60)

#: Timeout for a single finalize operation - one Vault delete + one
#: audit write. 30s is generous.
_FINALIZE_TIMEOUT: timedelta = timedelta(seconds=30)

#: Retry policy for the list activity. Transient Vault unavailability
#: is the most likely failure mode; a short exponential backoff with
#: 3 attempts handles it gracefully.
_LIST_RETRY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=15),
    maximum_attempts=3,
)

#: Retry policy for each finalize activity invocation. Per-entry
#: failures are isolated so a single Vault hiccup does not block
#: other entries.
_FINALIZE_RETRY: RetryPolicy = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=3,
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WebhookOverlapEntry:
    """A single dept × provider entry with an active overlap window.

    Attributes
    ----------
    dept_id:
        The department identifier.
    provider:
        One of ``"jira"``, ``"bitbucket"``, or ``"confluence"``.
    overlap_until:
        UTC ISO-8601 timestamp indicating when the overlap window
        expires. The workflow compares this against ``workflow.now()``
        to decide whether to finalize.
    """

    dept_id: str
    provider: str
    overlap_until: str


@dataclass(frozen=True)
class WebhookFinalizeError:
    """Records a per-entry finalize failure without aborting the run.

    Attributes
    ----------
    dept_id:
        The department identifier of the failed entry.
    provider:
        The provider of the failed entry.
    error:
        Human-readable error description.
    """

    dept_id: str
    provider: str
    error: str


@dataclass(frozen=True)
class WebhookRotationFinalizeReport:
    """Final result of a single ``WebhookRotationFinalizeWorkflow`` cron run.

    Attributes
    ----------
    scanned:
        Number of entries with an active overlap window that were
        evaluated.
    finalized:
        Number of entries successfully auto-finalized (overlap window
        expired and ``secret_previous`` cleared).
    skipped:
        Number of entries whose overlap window has not yet expired
        (still within the ``WEBHOOK_ROTATION_OVERLAP_S`` window).
    errors:
        List of per-entry errors encountered during finalization.
        These do not abort the workflow - remaining entries are still
        processed.
    """

    scanned: int
    finalized: int
    skipped: int
    errors: list[WebhookFinalizeError] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------


@workflow.defn(name="WebhookRotationFinalizeWorkflow")
class WebhookRotationFinalizeWorkflow:
    """Periodic Temporal cron that auto-finalizes expired webhook secret overlap windows.

    Runs every 10 minutes. For each dept × provider entry whose
    ``overlap_until`` timestamp has passed, invokes the finalize
    activity to clear the ``secret_previous`` Vault slot.

    See module docstring for the full lifecycle, determinism contract,
    and idempotence guarantees.
    """

    @workflow.run
    async def run(self) -> WebhookRotationFinalizeReport:
        """Execute one tick of the webhook rotation finalize cron."""

        # 1. List all entries that currently have an active overlap
        #    window (non-null secret_previous slot).
        entries: list[WebhookOverlapEntry] = await workflow.execute_activity(
            _ACT_LIST_WEBHOOK_ENTRIES_WITH_OVERLAP,
            start_to_close_timeout=_LIST_TIMEOUT,
            retry_policy=_LIST_RETRY,
        )

        # 2. For each entry, check if the overlap window has expired.
        now: datetime = workflow.now()
        finalized = 0
        skipped = 0
        errors: list[WebhookFinalizeError] = []

        for entry in entries:
            # Parse the overlap_until timestamp. If parsing fails,
            # treat it as expired (defensive - clear stale data).
            try:
                overlap_until = datetime.fromisoformat(entry.overlap_until)
                # Ensure timezone-aware comparison
                if overlap_until.tzinfo is None:
                    from datetime import timezone

                    overlap_until = overlap_until.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                # Malformed timestamp - treat as expired to clean up
                overlap_until = now

            if overlap_until > now:
                # Overlap window still active - skip this entry
                skipped += 1
                continue

            # 3. Overlap expired - finalize this entry
            try:
                await workflow.execute_activity(
                    _ACT_FINALIZE_WEBHOOK_ROTATION,
                    args=[entry.dept_id, entry.provider],
                    start_to_close_timeout=_FINALIZE_TIMEOUT,
                    retry_policy=_FINALIZE_RETRY,
                )
                finalized += 1
            except Exception as exc:  # noqa: BLE001
                # Per-entry failure - record and continue with
                # remaining entries. The next cron tick will retry.
                errors.append(
                    WebhookFinalizeError(
                        dept_id=entry.dept_id,
                        provider=entry.provider,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )

        return WebhookRotationFinalizeReport(
            scanned=len(entries),
            finalized=finalized,
            skipped=skipped,
            errors=errors,
        )


__all__: tuple[str, ...] = (
    "AUTOMATION_TASK_QUEUE",
    "WEBHOOK_ROTATION_FINALIZE_CRON_SCHEDULE",
    "WEBHOOK_ROTATION_FINALIZE_WORKFLOW_ID",
    "WebhookFinalizeError",
    "WebhookOverlapEntry",
    "WebhookRotationFinalizeReport",
    "WebhookRotationFinalizeWorkflow",
)
