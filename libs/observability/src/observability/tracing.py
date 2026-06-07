"""OpenTelemetry tracing integration (Feature 7).

Provides a shared tracing setup that all platform services use to
participate in distributed traces. Each service extracts the
``traceparent`` header from incoming requests, creates spans for its
operations, and exports them via OTLP to Tempo/Jaeger.

The ``correlation_id`` used throughout the platform is derived from
the trace_id (last 16 hex characters) so existing audit logs can be
cross-referenced with trace views.

Usage in a FastAPI service::

    from observability.tracing import setup_tracing, TracingMiddleware

    app = FastAPI()
    setup_tracing(service_name="automation-service")
    app.add_middleware(TracingMiddleware)

Usage in a Temporal worker::

    from observability.tracing import setup_tracing, trace_activity

    setup_tracing(service_name="agent-runner-worker")

    @trace_activity("llm_analyze_task")
    async def llm_analyze_task(...):
        ...

Environment variables:
- OTEL_EXPORTER_OTLP_ENDPOINT: OTLP collector endpoint (default: http://tempo:4317)
- OTEL_SERVICE_NAME: Service name for spans (auto-set by setup_tracing)
- OTEL_RESOURCE_ATTRIBUTES: Additional resource attributes
- OTEL_ENABLED: Set to "false" to disable tracing (default: "true")
"""

from __future__ import annotations

import functools
import logging
import os
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

__all__ = [
    "correlation_id_from_trace",
    "extract_trace_context",
    "get_current_trace_id",
    "setup_tracing",
    "trace_activity",
    "TracingMiddleware",
]

F = TypeVar("F", bound=Callable[..., Any])

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_OTLP_ENDPOINT = "http://tempo:4317"
_TRACEPARENT_HEADER = "traceparent"
_tracing_initialized = False


def _is_tracing_enabled() -> bool:
    """Check if OTel tracing is enabled via environment."""
    return os.environ.get("OTEL_ENABLED", "true").lower() not in ("false", "0", "no")


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def setup_tracing(
    service_name: str,
    *,
    otlp_endpoint: str | None = None,
) -> None:
    """Initialize OpenTelemetry tracing for the current process.

    Configures:
    - A TracerProvider with the service name as a resource attribute.
    - An OTLP gRPC exporter pointing at the configured endpoint.
    - A BatchSpanProcessor for efficient export.

    Safe to call multiple times - subsequent calls are no-ops.

    Parameters
    ----------
    service_name:
        The service identifier (e.g. "automation-service").
    otlp_endpoint:
        OTLP collector endpoint. Defaults to ``OTEL_EXPORTER_OTLP_ENDPOINT``
        env var or ``http://tempo:4317``.
    """
    global _tracing_initialized  # noqa: PLW0603

    if _tracing_initialized:
        return

    if not _is_tracing_enabled():
        logger.info("OpenTelemetry tracing disabled (OTEL_ENABLED=false)")
        _tracing_initialized = True
        return

    endpoint = (
        otlp_endpoint
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or _DEFAULT_OTLP_ENDPOINT
    )

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": service_name,
                "deployment.environment": os.environ.get(
                    "DEPLOYMENT_PROFILE", "development"
                ),
            }
        )

        provider = TracerProvider(resource=resource)
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        logger.info(
            "OpenTelemetry tracing initialized: service=%s endpoint=%s",
            service_name,
            endpoint,
        )
    except ImportError:
        logger.warning(
            "OpenTelemetry SDK not installed - tracing disabled. "
            "Install: pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to initialize OpenTelemetry tracing: %s", exc)

    _tracing_initialized = True


# ---------------------------------------------------------------------------
# Trace context extraction
# ---------------------------------------------------------------------------


def extract_trace_context(headers: dict[str, str] | Any) -> dict[str, str]:
    """Extract W3C trace context from HTTP headers.

    Returns a dict with ``trace_id`` and ``span_id`` if a valid
    ``traceparent`` header is found, otherwise empty dict.

    The ``traceparent`` format is:
    ``{version}-{trace_id}-{parent_span_id}-{trace_flags}``
    e.g. ``00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01``
    """
    if hasattr(headers, "get"):
        traceparent = headers.get(_TRACEPARENT_HEADER, "") or headers.get(
            _TRACEPARENT_HEADER.title(), ""
        )
    else:
        traceparent = ""

    if not traceparent:
        return {}

    parts = traceparent.split("-")
    if len(parts) != 4:
        return {}

    return {
        "trace_id": parts[1],
        "span_id": parts[2],
        "trace_flags": parts[3],
    }


def get_current_trace_id() -> str | None:
    """Return the current span's trace_id as a hex string, or None."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            return format(ctx.trace_id, "032x")
    except (ImportError, Exception):  # noqa: BLE001
        pass
    return None


def correlation_id_from_trace(trace_id: str | None) -> str | None:
    """Derive a correlation_id from a trace_id.

    Returns the last 16 hex characters of the trace_id, which can be
    used to cross-reference audit logs with trace views.

    Parameters
    ----------
    trace_id:
        32-character hex trace ID. Returns None if input is None or
        too short.
    """
    if not trace_id or len(trace_id) < 16:
        return None
    return trace_id[-16:]


# ---------------------------------------------------------------------------
# Activity decorator
# ---------------------------------------------------------------------------


def trace_activity(name: str) -> Callable[[F], F]:
    """Decorator that wraps a Temporal activity in an OTel span.

    Usage::

        @trace_activity("ssh_run_test")
        async def ssh_run_test(...):
            ...
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _is_tracing_enabled():
                return await func(*args, **kwargs)
            try:
                from opentelemetry import trace

                tracer = trace.get_tracer(__name__)
                with tracer.start_as_current_span(
                    name,
                    attributes={"activity.name": name},
                ):
                    return await func(*args, **kwargs)
            except ImportError:
                return await func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# ASGI Middleware
# ---------------------------------------------------------------------------


class TracingMiddleware:
    """ASGI middleware that extracts traceparent and creates a server span.

    For each incoming HTTP request:
    1. Extracts the ``traceparent`` header.
    2. Creates a span with the request method + path as the span name.
    3. Propagates the trace context to downstream calls.
    4. Adds ``trace_id`` to the ASGI scope state for downstream access.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not _is_tracing_enabled():
            await self.app(scope, receive, send)
            return

        try:
            from opentelemetry import trace
            from opentelemetry.propagate import extract

            # Build a carrier dict from ASGI headers.
            headers_dict: dict[str, str] = {}
            for key, value in scope.get("headers", []):
                headers_dict[key.decode("latin-1").lower()] = value.decode("latin-1")

            ctx = extract(headers_dict)
            tracer = trace.get_tracer(__name__)
            method = scope.get("method", "?")
            path = scope.get("path", "?")

            with tracer.start_as_current_span(
                f"{method} {path}",
                context=ctx,
                attributes={
                    "http.method": method,
                    "http.target": path,
                },
            ) as span:
                # Store trace_id in scope state for downstream.
                if "state" not in scope:
                    scope["state"] = {}
                span_ctx = span.get_span_context()
                if span_ctx and span_ctx.trace_id:
                    scope["state"]["trace_id"] = format(span_ctx.trace_id, "032x")

                await self.app(scope, receive, send)

        except ImportError:
            await self.app(scope, receive, send)
        except Exception:  # noqa: BLE001
            # Tracing failure must never break the request.
            await self.app(scope, receive, send)
