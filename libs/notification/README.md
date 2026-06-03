# `libs/notification`

Slack + email notification dispatcher shared across services and workers.

This package implements the **NotificationService** described in
the notification and cost component design,
satisfying:

- Requirement **5.1** — Slack webhook + email (SMTP) adapters; templates loaded
  from `prompts/notifications/<template_name>.md`.
- Requirement **5.2** — success notification only when
  `dept.notify_on_success == True`.
- Requirement **5.3** — failure notification **mandatory** regardless of dept
  config.
- Requirement **6.4** — admin Slack alarm on `AuditPruneWorkflow` failure
  (separate `notify_audit_prune_failed` entrypoint).

## Layout

```
src/notification/
  __init__.py        # public surface
  types.py           # frozen dataclasses (WorkflowResult, DeptConfig, …)
  adapters.py        # SlackAdapter / EmailAdapter / NotificationLogStore protocols
  service.py         # NotificationService.notify_workflow_completion
```

## Tasks

- **8.1** (in progress) — concrete `aiohttp` `SlackAdapter`, `aiosmtplib`
  `EmailAdapter`, token-bucket rate limiter, `vault:notifications/smtp/credential`
  resolver. The protocols in `adapters.py` are stable; 8.1 fills in the
  implementations.
- **8.2** (this PR) — `NotificationService.notify_workflow_completion`:
  failure-mandatory, success-gated, idempotent retry via
  `shared.notification_log.dedup_key UNIQUE`, body rendered through
  `PromptLoader`.
- **8.3** — `notify_audit_prune_failed` mandatory admin alarm (this PR):
  posts the rendered `notifications/audit_prune_failed` body to the admin
  Slack channel via `SlackAdapter.send_admin_channel(...,
  alert_type="audit_prune_failed")`. The destination webhook is fixed by
  the adapter to `vault:notifications/slack/admin` and is **not**
  configurable per call; the alarm cannot be
  silenced by dept config). Reuses the workflow-completion `dedup_key`
  shape (`sha256("<run_id>:slack:audit_prune_failed")`) so retries inside
  one cron run dedupe via `shared.notification_log.UNIQUE(dedup_key)`.
- **8.4** — `prompts/notifications/*.md` template bodies.
