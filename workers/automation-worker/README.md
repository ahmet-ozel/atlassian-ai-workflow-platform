# automation-worker

Temporal worker that hosts the **automation-tq** task queue workflows and
activities for the operational layer:

* `AutomationWorkflow` (gateway + capability gate + workflow
  type route).
* `BotBranchRetention` (daily cron, deletes orphan
  `ai/{issue_key}` branches).
* `AuditPruneWorkflow` (daily cron, archives stale
  `audit_events` to MinIO and deletes them; mandatory admin Slack alarm
  on any failure).

Activities for the AuditPruneWorkflow are wired in this worker; the
workflow body in `src/automation_worker/workflows/audit_prune.py`
references them by activity name only and therefore loads cleanly even
before the activity modules are created.

The worker is registered on the Temporal task queue `automation-tq` and
is scheduled to run with the `cron_schedule="0 3 * * *"` daily at 03:00
UTC for the `AuditPruneWorkflow`.

See the worker source for implementation details.
