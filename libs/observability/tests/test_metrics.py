"""Unit tests for the observability metric registry.

These are *example-based* sanity checks that lock in the public
contract of the package: which collectors exist, which label sets they
declare, and that `render()` emits the canonical exposition format.

Validates: Requirement 6.6 (platform-mimari-ops) — Prometheus metric
catalogue.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram

from observability import METRIC_NAMES, Metrics, metrics, registry, render


# ---------------------------------------------------------------------------
# Singleton + registration shape
# ---------------------------------------------------------------------------


def test_metrics_is_frozen_dataclass() -> None:
    """`metrics` cannot be monkey-patched at runtime — locks the catalogue."""
    try:
        metrics.workflow_execution_duration_seconds = "nope"  # type: ignore[misc]
    except (AttributeError, Exception):
        return
    raise AssertionError("Metrics container must be frozen")


def test_metric_names_match_dataclass_fields() -> None:
    """Every name in METRIC_NAMES is an attribute on the singleton."""
    for name in METRIC_NAMES:
        assert hasattr(metrics, name), f"missing collector attribute: {name}"


def test_metric_names_are_unique_and_ordered() -> None:
    """METRIC_NAMES is the canonical ordering and contains no duplicates."""
    assert len(METRIC_NAMES) == len(set(METRIC_NAMES))
    # Histograms first, then counters, then gauges — matches the design
    # narrative in metrics.py docstring.
    assert METRIC_NAMES[0] == "workflow_execution_duration_seconds"
    assert METRIC_NAMES[1] == "mcp_latency_seconds"
    assert METRIC_NAMES[-2] == "healthcheck_status"
    assert METRIC_NAMES[-1] == "queue_depth"


# ---------------------------------------------------------------------------
# Per-metric type + label assertions
# ---------------------------------------------------------------------------


def test_workflow_execution_duration_seconds_is_histogram() -> None:
    h = metrics.workflow_execution_duration_seconds
    assert isinstance(h, Histogram)
    # `_labelnames` is the public-ish attribute used by prometheus_client
    # to track the declared labelset.
    assert h._labelnames == ("workflow_type", "dept_id", "result")


def test_mcp_latency_seconds_is_histogram() -> None:
    h = metrics.mcp_latency_seconds
    assert isinstance(h, Histogram)
    assert h._labelnames == ("tool", "result")


def test_llm_token_cost_usd_total_is_counter_with_full_labels() -> None:
    c = metrics.llm_token_cost_usd_total
    assert isinstance(c, Counter)
    assert c._labelnames == ("dept_id", "model", "provider", "cost_tag")


def test_cost_usd_total_is_counter_with_dept_and_model_only() -> None:
    c = metrics.cost_usd_total
    assert isinstance(c, Counter)
    assert c._labelnames == ("dept_id", "model")


def test_capability_denied_total_is_counter() -> None:
    c = metrics.capability_denied_total
    assert isinstance(c, Counter)
    assert c._labelnames == ("dept_id", "workflow_type")


def test_chat_pii_matches_total_is_counter() -> None:
    c = metrics.chat_pii_matches_total
    assert isinstance(c, Counter)
    assert c._labelnames == ("pii_kind",)


def test_chat_messages_total_is_counter() -> None:
    c = metrics.chat_messages_total
    assert isinstance(c, Counter)
    assert c._labelnames == ("dept_id", "result")


def test_llm_provider_fallback_total_is_counter() -> None:
    c = metrics.llm_provider_fallback_total
    assert isinstance(c, Counter)
    assert c._labelnames == ("primary", "fallback", "reason")


def test_audit_prune_counters_have_no_labels() -> None:
    archived = metrics.audit_prune_archived_rows_total
    failed = metrics.audit_prune_failed_total
    assert isinstance(archived, Counter)
    assert isinstance(failed, Counter)
    assert archived._labelnames == ()
    assert failed._labelnames == ()


def test_budget_exceeded_total_is_counter() -> None:
    c = metrics.budget_exceeded_total
    assert isinstance(c, Counter)
    assert c._labelnames == ("dept_id", "scope")


def test_notification_dispatch_total_is_counter() -> None:
    c = metrics.notification_dispatch_total
    assert isinstance(c, Counter)
    assert c._labelnames == ("channel", "kind", "result")


def test_healthcheck_status_is_gauge() -> None:
    g = metrics.healthcheck_status
    assert isinstance(g, Gauge)
    assert g._labelnames == ("service",)


def test_queue_depth_is_gauge() -> None:
    g = metrics.queue_depth
    assert isinstance(g, Gauge)
    assert g._labelnames == ("dept_id", "workflow_type")


# ---------------------------------------------------------------------------
# Behaviour: increments, observations, gauge sets
# ---------------------------------------------------------------------------


def _scrape_text() -> str:
    body, content_type = render()
    assert content_type == CONTENT_TYPE_LATEST
    return body.decode()


def test_render_returns_bytes_and_canonical_content_type() -> None:
    body, content_type = render()
    assert isinstance(body, bytes)
    assert content_type == CONTENT_TYPE_LATEST
    assert b"# HELP" in body or b"# TYPE" in body


def test_counter_increment_is_visible_in_render() -> None:
    metrics.audit_prune_failed_total.inc()
    metrics.audit_prune_failed_total.inc()
    text = _scrape_text()
    # Counters are exposed as `<name>_total`; prometheus_client adds the
    # `_total` suffix automatically when missing, so we asked for
    # `audit_prune_failed_total` (already suffixed) and the rendered
    # name is `audit_prune_failed_total`.
    assert "audit_prune_failed_total" in text


def test_histogram_observation_is_visible_in_render() -> None:
    metrics.workflow_execution_duration_seconds.labels(
        workflow_type="code_change",
        dept_id="dept_eng",
        result="completed",
    ).observe(1.5)
    text = _scrape_text()
    assert "workflow_execution_duration_seconds_bucket" in text
    assert "workflow_execution_duration_seconds_count" in text
    assert "workflow_execution_duration_seconds_sum" in text


def test_gauge_set_is_visible_in_render() -> None:
    metrics.healthcheck_status.labels(service="automation-service").set(1)
    metrics.healthcheck_status.labels(service="postgres").set(0)
    text = _scrape_text()
    assert 'healthcheck_status{service="automation-service"} 1.0' in text
    assert 'healthcheck_status{service="postgres"} 0.0' in text


def test_labelled_counter_increment_renders_with_label_set() -> None:
    metrics.notification_dispatch_total.labels(
        channel="slack",
        kind="workflow_failed",
        result="sent",
    ).inc()
    text = _scrape_text()
    assert (
        'notification_dispatch_total{channel="slack",kind="workflow_failed",result="sent"}'
        in text
    )


def test_budget_exceeded_total_records_scope_label() -> None:
    metrics.budget_exceeded_total.labels(
        dept_id="dept_eng",
        scope="dept_weekly",
    ).inc()
    text = _scrape_text()
    assert (
        'budget_exceeded_total{dept_id="dept_eng",scope="dept_weekly"}' in text
    )


def test_chat_pii_matches_total_increments_per_kind() -> None:
    metrics.chat_pii_matches_total.labels(pii_kind="email").inc(2)
    metrics.chat_pii_matches_total.labels(pii_kind="tc_kimlik").inc()
    text = _scrape_text()
    assert 'chat_pii_matches_total{pii_kind="email"} 2.0' in text
    assert 'chat_pii_matches_total{pii_kind="tc_kimlik"} 1.0' in text


def test_llm_provider_fallback_total_increments() -> None:
    metrics.llm_provider_fallback_total.labels(
        primary="vllm",
        fallback="openai",
        reason="downtime",
    ).inc()
    text = _scrape_text()
    assert (
        'llm_provider_fallback_total{fallback="openai",primary="vllm",reason="downtime"}'
        in text
    )


# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------


def test_registry_does_not_expose_default_python_process_metrics() -> None:
    """The package registry is isolated from the prometheus_client default.

    The default registry exposes `process_*` and `python_*` metrics that
    are noisy when scraped from many services. The package registry
    must omit them.
    """
    text = _scrape_text()
    assert "process_cpu_seconds_total" not in text
    assert "python_info" not in text


def test_registry_singleton_is_shared() -> None:
    """`metrics` is the same object on every import of the package."""
    from observability import metrics as metrics_again
    from observability.metrics import metrics as metrics_inner

    assert metrics is metrics_again
    assert metrics is metrics_inner


def test_metrics_container_is_metrics_dataclass() -> None:
    assert isinstance(metrics, Metrics)
    # The singleton's registry is the package-level `registry`.
    # We can't check identity directly (Counter/Histogram/Gauge don't
    # expose the registry), but we can check that scraping the package
    # registry produces the metrics we just touched.
    assert "audit_prune_failed_total" in _scrape_text()


def test_render_is_idempotent() -> None:
    """Calling render twice without mutating any metric yields the same bytes."""
    a, _ = render()
    b, _ = render()
    assert a == b


def test_registry_is_collector_registry_instance() -> None:
    from prometheus_client import CollectorRegistry

    assert isinstance(registry, CollectorRegistry)
