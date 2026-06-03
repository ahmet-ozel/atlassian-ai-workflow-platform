"""observability: shared Prometheus metric registry.

Re-exports the public API of the package so callers can simply do::

    from observability import metrics, render, CONTENT_TYPE_LATEST
    from observability import (
        TraceMiddleware,
        generate_trace_id,
        get_trace_id,
        set_trace_id,
    )

Provides Prometheus metric registration across services and workers,
UUIDv7 trace_id generation, and end-to-end propagation via
:class:`TraceMiddleware`.
"""

from .metrics import (
    CONTENT_TYPE_LATEST,
    METRIC_NAMES,
    Metrics,
    metrics,
    registry,
    render,
)
from .trace import (
    TRACE_HEADER,
    TraceLogFilter,
    TraceMiddleware,
    generate_trace_id,
    get_trace_id,
    is_valid_trace_id,
    set_trace_id,
)

__all__ = [
    "CONTENT_TYPE_LATEST",
    "METRIC_NAMES",
    "Metrics",
    "TRACE_HEADER",
    "TraceLogFilter",
    "TraceMiddleware",
    "generate_trace_id",
    "get_trace_id",
    "is_valid_trace_id",
    "metrics",
    "registry",
    "render",
    "set_trace_id",
]
