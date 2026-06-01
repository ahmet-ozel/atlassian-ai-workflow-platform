"""In-process counters for the egress allowlist enforcement layer.

The wrapper is intentionally lightweight — there is no Prometheus client
dependency yet. Counters are exposed via ``GET /metrics`` in plain text so
they are scrape-able by any monitoring stack (Prometheus, OTel collector,
or a simple cron job).

The two named counters are part of the public observability contract for
Requirement 10.3:

* ``firecrawl_egress_allowed_total`` — number of requests whose host
  passed the allowlist.
* ``firecrawl_egress_denied_total`` — number of requests rejected with
  HTTP 403 ``egress_denied``.
"""

from __future__ import annotations

from threading import Lock

__all__ = ["EgressMetrics", "metrics"]


class EgressMetrics:
    """Thread-safe in-process counter pair for the egress decision path.

    The lock is uncontended on the FastAPI default worker (single-threaded
    asyncio) but kept for future-proofing against multi-thread test fixtures
    and for the platform property test that asserts the count after each
    Hypothesis example.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._allowed = 0
        self._denied = 0

    @property
    def allowed(self) -> int:
        with self._lock:
            return self._allowed

    @property
    def denied(self) -> int:
        with self._lock:
            return self._denied

    def record_allowed(self) -> None:
        with self._lock:
            self._allowed += 1

    def record_denied(self) -> None:
        with self._lock:
            self._denied += 1

    def reset(self) -> None:
        """Zero the counters. Test-only helper; never called from app code."""
        with self._lock:
            self._allowed = 0
            self._denied = 0

    def render(self) -> str:
        """Plain-text metric snapshot in Prometheus exposition format."""
        with self._lock:
            allowed = self._allowed
            denied = self._denied
        return (
            "# HELP firecrawl_egress_allowed_total Requests whose target host passed the allowlist.\n"
            "# TYPE firecrawl_egress_allowed_total counter\n"
            f"firecrawl_egress_allowed_total {allowed}\n"
            "# HELP firecrawl_egress_denied_total Requests rejected with HTTP 403 egress_denied.\n"
            "# TYPE firecrawl_egress_denied_total counter\n"
            f"firecrawl_egress_denied_total {denied}\n"
        )


#: Module-level singleton used by the FastAPI app. Tests can call
#: ``metrics.reset()`` inside a fixture to isolate counter assertions.
metrics = EgressMetrics()
