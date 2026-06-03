# observability

Shared Prometheus metric registry used by platform services and workers
(`assistant-service`, `automation-service`,
`admin-dashboard-api`, `agent-runner-worker`, `execution-runner-worker`,
`automation-worker`).

The package owns the **single source of truth** for the metric *names,
*types* and *label sets*, and exposes a single
`render()` helper that produces the Prometheus exposition format
consumed by `GET /metrics` endpoints.

## Public API

```python
from observability import metrics, render, CONTENT_TYPE_LATEST

# --- counters & histograms -------------------------------------------------
metrics.workflow_execution_duration_seconds.labels(
    workflow_type="code_change",
    dept_id="dept_eng",
    result="completed",
).observe(12.4)

metrics.mcp_latency_seconds.labels(
    tool="bitbucket_get_pull_request",
    result="ok",
).observe(0.31)

metrics.llm_token_cost_usd_total.labels(
    dept_id="dept_eng",
    model="gpt-4o-mini",
    provider="openai",
    cost_tag="production",
).inc(0.0042)

metrics.capability_denied_total.labels(
    dept_id="dept_eng",
    workflow_type="code_change",
).inc()

metrics.healthcheck_status.labels(service="automation-service").set(1)
metrics.queue_depth.labels(dept_id="dept_eng", workflow_type="research").set(3)
metrics.chat_pii_matches_total.labels(pii_kind="email").inc()

metrics.chat_messages_total.labels(
    dept_id="dept_eng",
    result="ok",
).inc()

metrics.llm_provider_fallback_total.labels(
    primary="vllm",
    fallback="openai",
    reason="downtime",
).inc()

metrics.audit_prune_archived_rows_total.inc(742)
metrics.audit_prune_failed_total.inc()
metrics.budget_exceeded_total.labels(
    dept_id="dept_eng",
    scope="dept_weekly",
).inc()

metrics.cost_usd_total.labels(dept_id="dept_eng", model="gpt-4o-mini").inc(0.0042)

metrics.notification_dispatch_total.labels(
    channel="slack",
    kind="workflow_failed",
    result="sent",
).inc()

# --- /metrics endpoint -----------------------------------------------------
body, content_type = render()
# body: bytes (Prometheus exposition format)
# content_type: "text/plain; version=0.0.4; charset=utf-8"
```

`render()` is a thin wrapper around `prometheus_client.generate_latest`
applied to the package-private `CollectorRegistry`. The registry is
*isolated* (no default Python process metrics, no cross-package label
collisions); every metric in the table below is registered once at
import time and is safe to import from multiple workers in the same
process.

## Metric Catalogue

The names and label sets cover workflow execution duration, MCP latency,
LLM token cost, capability denials, healthcheck status, queue depth, chat
traffic, audit pruning, budget enforcement, and notification dispatch.

| Metric | Type | Labels | Notes |
| --- | --- | --- | --- |
| `workflow_execution_duration_seconds` | Histogram | `workflow_type`, `dept_id`, `result` | Buckets exposed as `_bucket`/`_sum`/`_count` by `prometheus_client`. |
| `mcp_latency_seconds` | Histogram | `tool`, `result` | One observation per MCP tool invocation. |
| `llm_token_cost_usd_total` | Counter | `dept_id`, `model`, `provider`, `cost_tag` | Increment with `cost_usd` per LLM activity. `cost_tag ∈ {production, sandbox, probe}`. |
| `cost_usd_total` | Counter | `dept_id`, `model` | Convenience aggregate — same activity may also bump `llm_token_cost_usd_total`; this metric drops the provider/tag dimensions for the dept-by-model dashboard. |
| `capability_denied_total` | Counter | `dept_id`, `workflow_type` | Tracks capability gate denials across operational workflows. |
| `healthcheck_status` | Gauge | `service` | `1` healthy, `0` unhealthy, `0.5` degraded. |
| `queue_depth` | Gauge | `dept_id`, `workflow_type` | Pending work in the per-dept Temporal task queue. |
| `chat_pii_matches_total` | Counter | `pii_kind` | One increment per `PiiMatch` reported by `pii_shared.mask`. |
| `chat_messages_total` | Counter | `dept_id`, `result` | One per `POST /api/chat/stream` request. `result ∈ {ok, redirect_to_task_creator, rate_limit_exhausted, token_cap_exceeded, error}`. |
| `llm_provider_fallback_total` | Counter | `primary`, `fallback`, `reason` | Bumped when `LlmOrchestrator` switches to the fallback provider. |
| `audit_prune_archived_rows_total` | Counter | *(none)* | Cumulative rows moved to MinIO by `AuditPruneWorkflow`. |
| `audit_prune_failed_total` | Counter | *(none)* | Cumulative `AuditPruneWorkflow` failures — drives admin Slack alarm. |
| `budget_exceeded_total` | Counter | `dept_id`, `scope` | Bumped by `BudgetCapPolicy.enforce` on deny. `scope ∈ {dept_weekly, user_weekly, dept_monthly, user_monthly}`. |
| `notification_dispatch_total` | Counter | `channel`, `kind`, `result` | One per `NotificationService.send(...)` outcome. `result ∈ {sent, failed, deduped}`. |

## Determinism guarantee

`metrics` is a process-local singleton; importing the package always
returns the same `Metrics` instance. The catalogue is *frozen* at
import time — `metrics.<attr>` lookup never registers a new collector
behind the caller's back. This keeps the ``/metrics`` exposition
deterministic and auditable across services.

## Standalone build & run

```bash
cd platform/libs/observability
python -m venv .venv
. .venv/bin/activate          # on Windows: .venv\Scripts\activate
pip install -e .

python -c "from observability import metrics, render; metrics.audit_prune_failed_total.inc(); print(render()[0].decode())"
```

Runtime dependencies: `prometheus-client>=0.20,<1`.
