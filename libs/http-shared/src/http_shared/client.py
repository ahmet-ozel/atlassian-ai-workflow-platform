"""HTTP client factory that stamps every outgoing request with ``X-Client-Source``.

This module is the single point that creates :class:`httpx.AsyncClient`
instances used for MCP and Firecrawl calls across the multi-service
scaffold.  All outgoing traffic carries the caller Component's identity
in the ``X-Client-Source`` header so the observability layer can break
down traffic by origin (see MIMARI §6.6, Requirement 13).

In addition (platform-gap-fill task 7.2 / Requirement 8.4) every
outgoing request is stamped with the ``X-Trace-Id`` header at request
time using the trace_id installed on the calling task's
:mod:`contextvars` context by :class:`observability.TraceMiddleware`
(in services) or :func:`observability.set_trace_id` (in workers).
The injection runs as an :class:`httpx` request event hook so the
header value is resolved *per request* — that way a single long-lived
client constructed in startup wiring still emits a fresh trace_id for
each fan-out activity, instead of pinning the trace_id captured at
construction time.

When :mod:`observability` is not importable (e.g. local development
without the workspace lib), the hook falls back to a no-op so the
factory remains usable.  Callers that pass an explicit
``X-Trace-Id`` header continue to win — the hook never overwrites a
caller-supplied value, mirroring the convention used for other
correlation identifiers.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx

# ``observability`` is a peer workspace lib; the import is wrapped in a
# try / except so a stand-alone ``http-shared`` install (e.g. in
# focused unit-test environments) keeps working.  When the lib is
# absent the trace-id hook degrades to a no-op and only the
# ``X-Client-Source`` header is injected.
try:  # pragma: no cover - exercised by integration tests
    from observability import get_trace_id as _get_trace_id  # type: ignore[import-not-found]
except Exception:  # pragma: no cover - defensive fallback
    def _get_trace_id() -> str:  # type: ignore[misc]
        return ""

#: The Component identifiers that may appear in ``X-Client-Source``
#: (design §3.4). Unknown values are accepted by :func:`make_mcp_client`
#: but are intended to be flagged by the observability layer.
#:
#: ``automation-worker`` was added by platform-gap-fill task 8.2
#: (Requirement 9.3) so the worker hosting the ``automation-tq``
#: Temporal task queue can identify itself when its output_actions
#: activity calls the MCP server.
KNOWN_CLIENT_SOURCES: frozenset[str] = frozenset(
    {
        "automation-service",
        "assistant-service",
        "admin-dashboard-api",
        "agent-runner-worker",
        "automation-worker",
        "execution-runner-worker",
        "streamlit-app",
        "task-intake-service",
    }
)

_CLIENT_SOURCE_HEADER = "X-Client-Source"
_TRACE_ID_HEADER = "X-Trace-Id"


def _build_trace_request_hook(
    trace_id_provider: Callable[[], str],
) -> Callable[[httpx.Request], Any]:
    """Return an httpx async request event hook that injects ``X-Trace-Id``.

    The hook is called by :mod:`httpx` after the request object is
    constructed but before the bytes hit the wire (see ``httpx``
    "Event Hooks" docs).  It looks up the caller's trace_id from the
    supplied ``trace_id_provider`` (typically
    :func:`observability.get_trace_id` which reads a
    :class:`contextvars.ContextVar`), and sets the
    ``X-Trace-Id`` header on the outgoing request.

    The hook is implemented as an ``async def`` because
    :class:`httpx.AsyncClient` ``await``s every event hook returned
    from its ``event_hooks`` mapping — synchronous callables raise
    ``TypeError: object NoneType can't be used in 'await' expression``
    at request time.

    The hook never overwrites an existing ``X-Trace-Id`` value so
    callers that build a request manually with a specific trace_id
    (e.g. test harnesses, retry loops carrying a known parent id)
    keep their value.
    """

    async def _inject_trace_id(request: httpx.Request) -> None:
        # Honour any caller-supplied trace_id.  ``httpx.Headers`` does
        # case-insensitive lookup so ``"X-Trace-Id"`` and
        # ``"x-trace-id"`` are both honoured.
        if _TRACE_ID_HEADER in request.headers:
            return

        try:
            trace_id = trace_id_provider()
        except Exception:  # pragma: no cover - provider must never break a request
            return

        if trace_id:
            request.headers[_TRACE_ID_HEADER] = trace_id

    return _inject_trace_id


def make_mcp_client(
    client_source: str,
    *,
    timeout: float = 30.0,
    **kwargs: Any,
) -> httpx.AsyncClient:
    """Return an :class:`httpx.AsyncClient` pre-configured for MCP/Firecrawl.

    Every request issued by the returned client carries:

    * ``X-Client-Source: <client_source>`` (Requirement 13 — set on the
      client instance so it survives every ``client.post(...)`` /
      ``client.request(...)`` call without per-call boilerplate).
    * ``X-Trace-Id: <get_trace_id()>`` (platform-gap-fill task 7.2 /
      Requirement 8.4 — resolved per-request via a request event
      hook so the header value reflects the trace_id of the calling
      task at the moment the request is sent, not at client
      construction time).

    Any caller-supplied ``headers=`` argument is honoured for *other*
    header keys, but the factory's ``X-Client-Source`` value always
    wins on key collision so callers cannot accidentally spoof
    another Component's identity.  Caller-supplied ``X-Trace-Id``
    headers (set on the request object itself) are preserved by the
    hook — that path is reserved for explicit retry / replay
    scenarios where the operator wants to pin a specific trace.

    Parameters
    ----------
    client_source:
        The Component identity to advertise. Should be one of
        :data:`KNOWN_CLIENT_SOURCES`; unknown values are accepted to keep
        local development frictionless.
    timeout:
        Default request timeout in seconds (forwarded to ``httpx``).
    **kwargs:
        Additional keyword arguments forwarded to
        :class:`httpx.AsyncClient`. The ``headers`` argument, if provided,
        is merged with the factory header (factory wins on collision).
        ``event_hooks`` is also honoured — caller-supplied request /
        response hooks are merged with the factory's trace-id
        injector so callers can layer their own observability
        without losing trace propagation.
    """

    caller_headers = kwargs.pop("headers", None)
    merged_headers: dict[str, str] = {}

    if caller_headers is not None:
        # Accept dict-like, httpx.Headers, or sequence of (key, value) pairs.
        if isinstance(caller_headers, httpx.Headers):
            merged_headers.update(dict(caller_headers))
        elif hasattr(caller_headers, "items"):
            merged_headers.update({str(k): str(v) for k, v in caller_headers.items()})
        else:
            merged_headers.update({str(k): str(v) for k, v in caller_headers})

    # Factory header is applied last so it always wins on key collision,
    # regardless of header-name casing in the caller-supplied mapping.
    for existing_key in list(merged_headers):
        if existing_key.lower() == _CLIENT_SOURCE_HEADER.lower():
            del merged_headers[existing_key]
    merged_headers[_CLIENT_SOURCE_HEADER] = client_source

    # Compose event hooks — preserve any caller-supplied hooks and
    # append the trace-id injector.  ``httpx`` expects a mapping with
    # ``"request"`` / ``"response"`` keys, each mapped to a list of
    # callables.
    caller_hooks = kwargs.pop("event_hooks", None) or {}
    request_hooks = list(caller_hooks.get("request", []))
    response_hooks = list(caller_hooks.get("response", []))
    request_hooks.append(_build_trace_request_hook(_get_trace_id))

    merged_event_hooks = {
        "request": request_hooks,
        "response": response_hooks,
    }

    return httpx.AsyncClient(
        headers=merged_headers,
        timeout=timeout,
        event_hooks=merged_event_hooks,
        **kwargs,
    )
