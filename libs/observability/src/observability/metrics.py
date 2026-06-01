"""Prometheus metric registry shared by every platform-mimari-ops service.

Validates: Requirement 6.6 (Prometheus metric collection across the
workflow execution duration histogram, MCP latency histogram, LLM token
cost counter, capability_denied counter, healthcheck status gauge and
queue depth gauge), Requirement 6.7 (Grafana dashboards reference these
metrics), Requirement 5.4 (cost tracking ↔ counter), Requirement 5.5
(budget cap enforcement ↔ counter), Requirement 5.1/5.2/5.3
(notification dispatch ↔ counter), Requirement 1.5 (chat PII counter),
Requirement 1.10 (LLM provider fallback counter), Requirement 6.3 / 6.4
(audit prune counters).

Design contract (`.kiro/specs/platform-mimari-ops/design.md`):

* The catalogue of metric **names**, **types** and **label sets** is
  *frozen at import time*. Callers never construct a new collector;
  they look up an attribute on the singleton `metrics` and then call
  `.labels(...).inc()`, `.observe(...)` or `.set(...)`.
* The package owns its own `CollectorRegistry`. Importing
  `observability` does **not** mutate the `prometheus_client` global
  registry, which keeps the `/metrics` exposition of every consuming
  service free of accidental cross-service label collisions and free of
  the default Python process metrics that `prometheus_client` would
  otherwise register.
* `render()` returns a `(body: bytes, content_type: str)` tuple. The
  content type matches the Prometheus exposition format consumed by
  Grafana / Prometheus scrape jobs.

The metric set is the union of:

1. The eight metrics enumerated in `tasks.md` task 14.1
   (`workflow_execution_duration_seconds_bucket`,
   `mcp_latency_seconds_bucket`, `llm_token_cost_usd_total`,
   `capability_denied_total`, `healthcheck_status`, `queue_depth`,
   `chat_pii_matches_total`, `audit_prune_archived_rows_total`,
   `audit_prune_failed_total`).
2. The five additional metrics required by the cost / notification /
   chat-loop design (`chat_messages_total`,
   `llm_provider_fallback_total`, `budget_exceeded_total`,
   `cost_usd_total`, `notification_dispatch_total`).

The two `_seconds_bucket` names from `tasks.md` are exposed by
`prometheus_client`'s Histogram type as `<name>_bucket`, `<name>_sum`
and `<name>_count` time-series. The Python attribute on the registry is
the un-suffixed base name.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

__all__ = [
    "CONTENT_TYPE_LATEST",
    "METRIC_NAMES",
    "Metrics",
    "metrics",
    "registry",
    "render",
]


# ---------------------------------------------------------------------------
# Histogram bucket layouts
# ---------------------------------------------------------------------------

#: Default workflow-duration buckets, in seconds. Covers sub-second
#: webhook → workflow start latency at the low end up to long-running
#: code-change workflows that may take multiple minutes (B15 cost
#: prediction CI uses the same range for its training data).
_WORKFLOW_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    1800.0,
    3600.0,
)

#: Default MCP-tool latency buckets, in seconds. Covers fast read tools
#: (issue lookup, ~50ms) up to slower write probes (Confluence create,
#: a few seconds). MCP timeouts above 30s are deliberately bucketed
#: together as ``+Inf``.
_MCP_LATENCY_BUCKETS: Final[tuple[float, ...]] = (
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)


# ---------------------------------------------------------------------------
# Metric registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Metrics:
    """Frozen container of every collector registered by this package.

    The dataclass is `frozen=True` so callers cannot accidentally
    monkey-patch a metric name to a different collector at runtime; the
    singleton `metrics` is therefore safe to import from any number of
    workers in the same process.

    Each field maps **directly** to a Prometheus collector. The field
    name matches the metric name (without the `_bucket`/`_sum`/`_count`
    Histogram suffixes that `prometheus_client` synthesises on scrape).
    """

    # --- Histograms -------------------------------------------------------

    #: Workflow execution duration in seconds, observed once at the end
    #: of every Temporal workflow (R6.6). Exposed as
    #: ``workflow_execution_duration_seconds_bucket`` etc.
    workflow_execution_duration_seconds: Histogram

    #: Per-tool MCP call latency in seconds (R6.6). Exposed as
    #: ``mcp_latency_seconds_bucket`` etc.
    mcp_latency_seconds: Histogram

    # --- Counters ---------------------------------------------------------

    #: Cumulative LLM token cost in USD, broken down by dept / model /
    #: provider / cost_tag (R5.4, R5.6, R6.6). Increment by `cost_usd`
    #: per LLM activity.
    llm_token_cost_usd_total: Counter

    #: Convenience aggregate of `llm_token_cost_usd_total` collapsed to
    #: the dept × model dimensions for the cost dashboard
    #: (`grafana-dashboards/llm-cost.json`). The two metrics are
    #: increment-by-cost in lockstep at the call site so dashboards do
    #: not have to sum across `provider` and `cost_tag` for the
    #: dept × model panel.
    cost_usd_total: Counter

    #: Cumulative ``capability_denied`` events from the foundation
    #: capability gate (Spec 1 R7) and the workflows-spec gate
    #: (Spec 2 R2). Extended to ops scope by R6.6.
    capability_denied_total: Counter

    #: PII matches detected by `pii_shared.mask` (R1.5). Bumped once
    #: per `PiiMatch` reported by the filter; `pii_kind ∈ {tc_kimlik,
    #: phone_tr, email, credit_card}`.
    chat_pii_matches_total: Counter

    #: One increment per `POST /api/chat/stream` request (R1.1, R1.8).
    #: The `result` label captures the terminal SSE event of the
    #: stream: `ok`, `redirect_to_task_creator`,
    #: `rate_limit_exhausted`, `token_cap_exceeded`, `error`.
    chat_messages_total: Counter

    #: Bumped when `LlmOrchestrator` switches to the fallback provider
    #: (R1.10). `reason ∈ {downtime, rate_limit_exhausted}`.
    llm_provider_fallback_total: Counter

    #: Rows moved by `AuditPruneWorkflow` to MinIO archive (R6.3).
    #: Incremented by the row count at the end of every successful run.
    audit_prune_archived_rows_total: Counter

    #: `AuditPruneWorkflow` failures (R6.4). One increment per failed
    #: run; bumped *before* the admin-Slack alarm activity is invoked.
    audit_prune_failed_total: Counter

    #: `BudgetCapPolicy.enforce` deny outcomes (R5.5). The `scope`
    #: label disambiguates which limit was hit:
    #: `dept_weekly`, `user_weekly`, `dept_monthly`, `user_monthly`.
    budget_exceeded_total: Counter

    #: One increment per `NotificationService.send(...)` outcome
    #: (R5.1, R5.2, R5.3). `result ∈ {sent, failed, deduped}`,
    #: `channel ∈ {slack, email, teams}`.
    notification_dispatch_total: Counter

    # --- Gauges -----------------------------------------------------------

    #: Per-service healthcheck status (R6.6, R4.9, R4.10):
    #: ``1`` healthy, ``0`` unhealthy, ``0.5`` degraded (cascade).
    healthcheck_status: Gauge

    #: Pending work in the per-dept Temporal task queue (R6.6).
    queue_depth: Gauge


def _build_metrics(reg: CollectorRegistry) -> Metrics:
    """Register every metric on `reg` and return the frozen container.

    The function is private and is only invoked once at import time;
    splitting it out from the module body keeps the registration logic
    re-usable from tests that want to construct an isolated registry
    (for example, to assert Prometheus exposition determinism without
    interference from the package-level singleton).
    """

    return Metrics(
        # --- Histograms ---------------------------------------------------
        workflow_execution_duration_seconds=Histogram(
            "workflow_execution_duration_seconds",
            "Workflow execution duration in seconds.",
            labelnames=("workflow_type", "dept_id", "result"),
            buckets=_WORKFLOW_DURATION_BUCKETS,
            registry=reg,
        ),
        mcp_latency_seconds=Histogram(
            "mcp_latency_seconds",
            "Latency of MCP tool invocations in seconds.",
            labelnames=("tool", "result"),
            buckets=_MCP_LATENCY_BUCKETS,
            registry=reg,
        ),
        # --- Counters -----------------------------------------------------
        llm_token_cost_usd_total=Counter(
            "llm_token_cost_usd_total",
            "Cumulative LLM token cost in USD per dept / model / provider / cost_tag.",
            labelnames=("dept_id", "model", "provider", "cost_tag"),
            registry=reg,
        ),
        cost_usd_total=Counter(
            "cost_usd_total",
            (
                "Cumulative LLM cost in USD aggregated to the dept x model "
                "dimensions used by the LLM-cost Grafana dashboard."
            ),
            labelnames=("dept_id", "model"),
            registry=reg,
        ),
        capability_denied_total=Counter(
            "capability_denied_total",
            "Total capability_denied events emitted by the capability gate.",
            labelnames=("dept_id", "workflow_type"),
            registry=reg,
        ),
        chat_pii_matches_total=Counter(
            "chat_pii_matches_total",
            "PII matches detected by the assistant-service PII filter.",
            labelnames=("pii_kind",),
            registry=reg,
        ),
        chat_messages_total=Counter(
            "chat_messages_total",
            "Number of POST /api/chat/stream requests by terminal result.",
            labelnames=("dept_id", "result"),
            registry=reg,
        ),
        llm_provider_fallback_total=Counter(
            "llm_provider_fallback_total",
            "Number of LLM provider fallback transitions.",
            labelnames=("primary", "fallback", "reason"),
            registry=reg,
        ),
        audit_prune_archived_rows_total=Counter(
            "audit_prune_archived_rows_total",
            "Cumulative audit rows moved to MinIO by AuditPruneWorkflow.",
            registry=reg,
        ),
        audit_prune_failed_total=Counter(
            "audit_prune_failed_total",
            "Cumulative AuditPruneWorkflow failures.",
            registry=reg,
        ),
        budget_exceeded_total=Counter(
            "budget_exceeded_total",
            "BudgetCapPolicy.enforce deny outcomes by dept and scope.",
            labelnames=("dept_id", "scope"),
            registry=reg,
        ),
        notification_dispatch_total=Counter(
            "notification_dispatch_total",
            "NotificationService dispatch outcomes by channel / kind / result.",
            labelnames=("channel", "kind", "result"),
            registry=reg,
        ),
        # --- Gauges -------------------------------------------------------
        healthcheck_status=Gauge(
            "healthcheck_status",
            "Per-service healthcheck status (1 healthy, 0 unhealthy, 0.5 degraded).",
            labelnames=("service",),
            registry=reg,
        ),
        queue_depth=Gauge(
            "queue_depth",
            "Pending work in the per-dept Temporal task queue.",
            labelnames=("dept_id", "workflow_type"),
            registry=reg,
        ),
    )


#: The package-private collector registry. Isolated from the
#: `prometheus_client` global registry so importing `observability`
#: does not pull in default process metrics or risk cross-service
#: label collisions.
registry: Final[CollectorRegistry] = CollectorRegistry()


#: Module-level singleton. Every consumer should import this directly
#: instead of constructing a new `Metrics` instance.
metrics: Final[Metrics] = _build_metrics(registry)


#: Canonical, deterministic ordering of every metric name registered by
#: this package. Used by `tests/property/test_observability_metrics.py`
#: and by CI guards that pin the public observability contract.
METRIC_NAMES: Final[tuple[str, ...]] = (
    "workflow_execution_duration_seconds",
    "mcp_latency_seconds",
    "llm_token_cost_usd_total",
    "cost_usd_total",
    "capability_denied_total",
    "chat_pii_matches_total",
    "chat_messages_total",
    "llm_provider_fallback_total",
    "audit_prune_archived_rows_total",
    "audit_prune_failed_total",
    "budget_exceeded_total",
    "notification_dispatch_total",
    "healthcheck_status",
    "queue_depth",
)


def render() -> tuple[bytes, str]:
    """Render the package registry in Prometheus exposition format.

    The return shape matches what FastAPI handlers expect for a
    ``GET /metrics`` endpoint: a `bytes` body and the canonical content
    type from `prometheus_client`. Wiring is intentionally trivial::

        from fastapi import FastAPI, Response
        from observability import render

        app = FastAPI()

        @app.get("/metrics")
        def metrics_endpoint() -> Response:
            body, content_type = render()
            return Response(content=body, media_type=content_type)

    Returns:
        A `(body, content_type)` tuple where `body` is the raw exposition
        bytes and `content_type` is `CONTENT_TYPE_LATEST` from
        `prometheus_client`.
    """

    return generate_latest(registry), CONTENT_TYPE_LATEST
