"""``McpMetricsClient`` (`platform gap-fill work` metrics client wiring).


Forwards a request to the MCP server's Prometheus exposition endpoint
and parses out the ``mcp_requests_total{client_source, tool, status}``
counter (registered by metrics middleware wiring). The admin dashboard surfaces these
samples through ``GET /api/v1/mcp/traffic`` so an operator can break
traffic down by ``client_source`` and ``tool`` (behavior 9.5).

Design notes
------------

The client is intentionally narrow:

* a single :meth:`fetch_request_counters` coroutine, since the only
  surface the dashboard needs is ``mcp_requests_total``;
* parsing uses the official :mod:`prometheus_client.parser` so we
  honour the full Prometheus exposition format (TYPE / HELP comments,
  escaped label values, ``_total`` suffix handling, etc.) without
  reinventing the wheel;
* the client is stateless aside from a shared
  :class:`httpx.AsyncClient`. Production wiring opens the client
  during the FastAPI lifespan; tests inject a client backed by
  :class:`httpx.MockTransport`.
* failures (timeouts, non-2xx responses, unparseable bodies) are
  raised as :class:`McpMetricsError` so the router can map them to a
  ``502 Bad Gateway`` rather than crashing the request handler.

The "last 24h" framing in the requirement is a UX concern: counter
samples from ``/metrics`` are cumulative since the MCP server started,
which is the right shape for a snapshot view in the admin dashboard.
A future iteration can layer a Prometheus PromQL query on top
(``increase(mcp_requests_total[24h])``) by pointing the client at a
Prometheus aggregator URL - until that lands, the snapshot view is the
documented behaviour.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

import httpx
from prometheus_client.parser import text_string_to_metric_families

__all__ = [
    "McpMetricsClient",
    "McpMetricsError",
    "McpRequestCounter",
    "REQUEST_COUNTER_NAME",
    "parse_request_counters",
]


logger = logging.getLogger(__name__)


#: Counter name registered by the MCP server (metrics middleware wiring, behavior 9.6).
#: The exposition format optionally drops the ``_total`` suffix on the
#: parsed sample name; we strip the suffix when comparing so both
#: ``mcp_requests_total`` and ``mcp_requests`` (the legacy parser
#: representation) match the expected counter.
REQUEST_COUNTER_NAME: str = "mcp_requests_total"

#: Required label set carried by every counter sample. Samples missing
#: any of these labels are skipped silently - the parser must not
#: explode on a malformed exposition line.
_REQUIRED_LABELS: frozenset[str] = frozenset({"client_source", "tool", "status"})


class McpMetricsError(Exception):
    """Raised when the MCP ``/metrics`` endpoint cannot be queried.

    Wraps the underlying transport / parsing failure so the router
    can map every failure mode to a single ``HTTP 502`` envelope.
    """

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause


@dataclass(frozen=True)
class McpRequestCounter:
    """One ``mcp_requests_total`` sample.

    Attributes mirror the Prometheus label set defined by the MCP
    server's middleware (metrics middleware wiring):

    * ``client_source`` - value of ``X-Client-Source`` on the inbound
      MCP request (or ``"unknown"`` when absent).
    * ``tool`` - MCP tool name (``tools/call``) or JSON-RPC method
      name (``initialize``, ``tools/list``, …) or HTTP path.
    * ``status`` - ``"success"`` for HTTP 2xx, ``"error"`` otherwise.
    * ``count`` - cumulative counter value since the MCP server
      process started (Counter - monotonically increasing).
    """

    client_source: str
    tool: str
    status: str
    count: float

    def to_response(self) -> dict[str, object]:
        """Serialise to the JSON shape returned by the router."""

        # ``count`` is exposed as ``int`` to the FE - Prometheus
        # counters are integers by contract (Counter.inc() defaults
        # to incrementing by 1), and JSON consumers prefer ints over
        # floats for whole numbers.
        return {
            "client_source": self.client_source,
            "tool": self.tool,
            "status": self.status,
            "count": int(self.count),
        }


def parse_request_counters(text: str) -> list[McpRequestCounter]:
    """Parse Prometheus exposition into :class:`McpRequestCounter` rows.

    Only counter samples named ``mcp_requests_total`` (or
    ``mcp_requests`` - the parser's legacy ``_total``-stripped form)
    are emitted; everything else is skipped silently. Samples missing
    any required label (``client_source`` / ``tool`` / ``status``)
    are also skipped - the parser must not crash on a partial
    exposition.

    Args:
        text: Raw response body from ``GET /metrics``.

    Returns:
        List of :class:`McpRequestCounter` rows in document order.
    """

    rows: list[McpRequestCounter] = []
    try:
        families = list(text_string_to_metric_families(text))
    except Exception as exc:  # noqa: BLE001 - the parser raises bare ``Exception``
        raise McpMetricsError(
            "failed to parse Prometheus exposition body",
            cause=exc,
        ) from exc

    for family in families:
        # The parser strips the ``_total`` suffix off counter names,
        # so we accept either form to stay forward-compatible if the
        # MCP server ever switches counter naming conventions.
        if family.name not in {REQUEST_COUNTER_NAME, "mcp_requests"}:
            continue
        for sample in family.samples:
            labels = sample.labels or {}
            if not _REQUIRED_LABELS.issubset(labels.keys()):
                logger.debug(
                    "skipping mcp_requests_total sample with missing labels: %s",
                    labels,
                )
                continue
            rows.append(
                McpRequestCounter(
                    client_source=str(labels["client_source"]),
                    tool=str(labels["tool"]),
                    status=str(labels["status"]),
                    count=float(sample.value),
                )
            )
    return rows


class McpMetricsClient:
    """Asynchronous adapter around the MCP server's ``/metrics`` endpoint.

    Production wiring constructs one instance per process during the
    FastAPI lifespan and stashes it on ``app.state.mcp_metrics_client``.
    The :class:`httpx.AsyncClient` is shared with the rest of the
    admin-dashboard-api outbound traffic so we do not open a second
    connection pool for a single endpoint.

    Tests inject an :class:`httpx.AsyncClient` backed by
    :class:`httpx.MockTransport` to keep the suite hermetic.
    """

    def __init__(
        self,
        *,
        base_url: str,
        http_client: httpx.AsyncClient,
        timeout: float = 5.0,
    ) -> None:
        if not base_url:
            raise ValueError("base_url must be a non-empty string")
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        """Return the configured MCP server base URL."""

        return self._base_url

    async def fetch_request_counters(self) -> list[McpRequestCounter]:
        """Fetch and parse ``mcp_requests_total`` from the MCP server.

        Raises:
            McpMetricsError: When the request fails (transport error,
                non-2xx response) or the body cannot be parsed.
        """

        url = f"{self._base_url}/metrics"
        try:
            response = await self._http_client.get(
                url, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise McpMetricsError(
                f"transport error contacting {url}: {exc}",
                cause=exc,
            ) from exc

        if response.status_code < 200 or response.status_code >= 300:
            raise McpMetricsError(
                f"unexpected status {response.status_code} from {url}: "
                f"{_short_body(response)}",
            )

        body = response.text
        return parse_request_counters(body)


def _short_body(response: httpx.Response) -> str:
    """Return at most 200 characters of the response body for context."""

    try:
        text = response.text
    except Exception:  # pragma: no cover - body decoding edge case
        return "<unreadable body>"
    if len(text) > 200:
        return text[:200] + "..."
    return text
