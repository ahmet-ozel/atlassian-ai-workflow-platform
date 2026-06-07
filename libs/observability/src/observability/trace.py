"""End-to-end trace ID propagation primitives.

This module implements the :class:`Trace_Propagator` component:
:requirement:`8.1` and :requirement:`8.7`:

* :func:`generate_trace_id` produces a fresh **UUID v7** string. The
  format follows :rfc:`9562` §5.7 (48-bit Unix-epoch milliseconds
  timestamp + 4-bit version + 12 random bits + 2-bit RFC 4122 variant
  + 62 random bits). Time-ordered IDs make trace_id values directly
  sortable by occurrence time, which the downstream Admin Dashboard
  log filter relies on (:requirement:`8.6`).
* :func:`get_trace_id` and :func:`set_trace_id` use :mod:`contextvars`
  so concurrent FastAPI request handlers and Temporal worker
  activities running in the same event loop each see *their own*
  trace_id without explicit threading.
* :class:`TraceMiddleware` is an ASGI middleware that:

  1. Extracts an inbound ``X-Trace-Id`` header. When the header is
     present and well-formed, it is **preserved** (so Atlassian retry
     loops keep the same trace_id - :requirement:`8.7`).
  2. Otherwise, generates a fresh UUID v7 trace_id.
  3. Sets the trace_id into the request-scoped context variable so
     downstream code can call :func:`get_trace_id` without having to
     thread the value through every function.
  4. Mirrors the trace_id back into the response under the same
     ``X-Trace-Id`` header so callers (and reverse proxies) can
     correlate request  response.

The module is intentionally dependency-light: it uses only the Python
standard library. The :class:`TraceMiddleware` class is implemented
against the bare ASGI 3.0 contract (``scope``, ``receive``, ``send``)
so the package does not need to depend on Starlette or FastAPI - the
sibling module :mod:`observability.tracing` follows the same
discipline.

Usage::

    from fastapi import FastAPI
    from observability.trace import TraceMiddleware, get_trace_id

    app = FastAPI()
    app.add_middleware(TraceMiddleware)

    @app.get("/example")
    async def example():
        return {"trace_id": get_trace_id()}

"""

from __future__ import annotations

import logging
import secrets
import time
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Final, MutableMapping

__all__ = [
    "TRACE_HEADER",
    "TraceLogFilter",
    "TraceMiddleware",
    "generate_trace_id",
    "get_trace_id",
    "is_valid_trace_id",
    "set_trace_id",
]

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

#: Canonical HTTP header name used to carry the trace_id across
#: services. The same name is used by the inbound extractor and the
#: outbound response injector (:requirement:`8.4`, :requirement:`8.7`).
TRACE_HEADER: Final[str] = "X-Trace-Id"

#: Lowercase ASGI byte form of :data:`TRACE_HEADER`. ASGI delivers
#: headers as ``(bytes, bytes)`` pairs with a lowercase name; using a
#: pre-computed bytes constant avoids repeated ``.encode()`` calls.
_TRACE_HEADER_BYTES: Final[bytes] = TRACE_HEADER.lower().encode("latin-1")

# ---------------------------------------------------------------------------
# Context variable
# ---------------------------------------------------------------------------

#: Per-async-task trace_id slot. The default empty string mirrors the
#: convention used by ``observability.tracing`` for the OTel
#: trace_id - callers should treat ``""`` as "no trace_id set".
_trace_id_ctx: ContextVar[str] = ContextVar("trace_id", default="")


def get_trace_id() -> str:
    """Return the trace_id bound to the current async task.

    Returns:
        The trace_id previously installed by :func:`set_trace_id` or
        :class:`TraceMiddleware`, or the empty string if no trace_id
        has been set in the current context.
    """

    return _trace_id_ctx.get()


def set_trace_id(trace_id: str) -> None:
    """Bind ``trace_id`` to the current async task's context.

    Args:
        trace_id: The trace_id string. Callers SHOULD use a value
            produced by :func:`generate_trace_id` (UUID v7) but the
            function does *not* validate the format - services may
            choose to round-trip vendor-specific identifiers through
            the same context slot.
    """

    _trace_id_ctx.set(trace_id)


# ---------------------------------------------------------------------------
# UUID v7 generation (RFC 9562 §5.7)
# ---------------------------------------------------------------------------


def generate_trace_id() -> str:
    """Generate a fresh UUID v7 trace_id string.

    The implementation follows :rfc:`9562` §5.7 (UUIDv7 layout):

    * Bits  0..47   - ``unix_ts_ms``: Unix timestamp in milliseconds,
      big-endian. 48 bits cover dates up to year 10889 so overflow is
      not a practical concern.
    * Bits 48..51   - ``ver``: the 4-bit version field, set to ``0b0111``
      (= ``7``) to identify a UUIDv7.
    * Bits 52..63   - ``rand_a``: 12 bits of cryptographically secure
      randomness.
    * Bits 64..65   - ``var``: the 2-bit RFC 4122 variant, set to
      ``0b10``.
    * Bits 66..127  - ``rand_b``: 62 bits of cryptographically secure
      randomness.

    The function uses :func:`secrets.token_bytes` for the random
    portions so generated IDs are unpredictable enough to use as
    correlation tokens in audit logs without leaking timing-only
    inference.

    Returns:
        The canonical 36-character ``8-4-4-4-12`` lowercase hex
        representation, e.g.
        ``"018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34"``.
    """

    # 48-bit Unix milliseconds timestamp.
    unix_ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF

    # 80 bits (= 10 bytes) of randomness covers rand_a (12 bits) and
    # rand_b (62 bits). Drawing the full 10 bytes in one syscall is
    # both faster and atomic.
    rand_bytes = secrets.token_bytes(10)
    rand_a = ((rand_bytes[0] << 8) | rand_bytes[1]) & 0x0FFF  # 12 bits
    rand_b = int.from_bytes(rand_bytes[2:], "big") & ((1 << 62) - 1)  # 62 bits

    # Assemble the 128-bit integer per RFC 9562 §5.7 layout.
    uuid_int = (
        (unix_ts_ms & 0xFFFFFFFFFFFF) << 80          # bits 0..47
        | (0x7 << 76)                                # bits 48..51 (version)
        | (rand_a << 64)                             # bits 52..63
        | (0x2 << 62)                                # bits 64..65 (variant 10)
        | rand_b                                     # bits 66..127
    )

    return _format_uuid(uuid_int)


def _format_uuid(value: int) -> str:
    """Format a 128-bit integer as a canonical lowercase UUID string."""

    hex_str = f"{value:032x}"
    return f"{hex_str[0:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:32]}"


def is_valid_trace_id(trace_id: str | None) -> bool:
    """Return ``True`` iff ``trace_id`` is a syntactically valid UUID.

    Used by :class:`TraceMiddleware` to decide whether to *preserve* an
    inbound ``X-Trace-Id`` header (:requirement:`8.7`) or to discard it
    and generate a fresh value. The check is lenient on UUID version -
    any ``8-4-4-4-12`` lowercase or uppercase hex string is accepted -
    so services can interoperate with upstream emitters that haven't
    yet migrated to UUIDv7.

    Args:
        trace_id: Candidate trace_id string. ``None`` and empty values
            return ``False``.

    Returns:
        ``True`` if the value matches the canonical UUID textual layout.
    """

    if not trace_id:
        return False

    if len(trace_id) != 36:
        return False

    # Layout: 8-4-4-4-12, dashes at positions 8, 13, 18, 23.
    if (
        trace_id[8] != "-"
        or trace_id[13] != "-"
        or trace_id[18] != "-"
        or trace_id[23] != "-"
    ):
        return False

    hex_only = trace_id.replace("-", "")
    if len(hex_only) != 32:
        return False

    return all(c in "0123456789abcdefABCDEF" for c in hex_only)


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------

# ASGI typing (PEP-style aliases - kept loose to avoid a hard
# starlette / asgiref dependency).
_Scope = MutableMapping[str, Any]
_Message = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[_Message]]
_Send = Callable[[_Message], Awaitable[None]]


class TraceMiddleware:
    """ASGI middleware that enforces trace_id propagation.

    For every inbound HTTP request the middleware:

    1. Reads the ``X-Trace-Id`` request header.
    2. If the header is present and is a syntactically valid UUID,
       reuses it (Atlassian webhook retries and cross-service relays
       MUST keep the same trace - :requirement:`8.7`).
    3. Otherwise, generates a fresh UUIDv7 via :func:`generate_trace_id`.
    4. Stores the resolved trace_id on the per-request context variable
       so handlers, dependencies and downstream activity log lines can
       call :func:`get_trace_id` to retrieve it.
    5. Mirrors the resolved trace_id into the response under the same
       ``X-Trace-Id`` header so reverse proxies and clients can
       correlate the request  response pair.
    6. Stores the trace_id under ``scope["state"]["trace_id"]`` to
       parallel the shape used by :class:`observability.tracing.TracingMiddleware`.

    The middleware is intentionally synchronous in its trace_id
    handling - UUID generation is a few microseconds and has no I/O -
    so it does not add measurable latency to the request hot path.

    Non-HTTP scopes (``lifespan``, ``websocket``) are passed through
    untouched.

    Args:
        app: The downstream ASGI application (injected by Starlette
            when used via ``app.add_middleware(TraceMiddleware)``).
        header_name: HTTP header to read/write. Defaults to
            :data:`TRACE_HEADER` (``"X-Trace-Id"``). The header lookup
            is case-insensitive (ASGI normalises header names to
            lowercase bytes for us).
    """

    def __init__(
        self,
        app: Any,
        *,
        header_name: str = TRACE_HEADER,
    ) -> None:
        self.app = app
        self._header_name = header_name
        self._header_lookup_bytes = header_name.lower().encode("latin-1")
        self._header_emit_bytes = header_name.encode("latin-1")

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        # Only HTTP requests carry trace_ids. WebSockets and lifespan
        # events bypass the middleware entirely.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        trace_id = self._resolve_trace_id(scope)

        # Bind to context var (for get_trace_id() lookups) and to
        # scope["state"] (for direct attribute access via Starlette's
        # request.state).
        token = _trace_id_ctx.set(trace_id)
        state = scope.setdefault("state", {})
        # ``scope["state"]`` is a plain dict in Starlette; ensure the
        # key exists even when other middlewares have not initialised
        # it yet.
        if isinstance(state, dict):
            state["trace_id"] = trace_id

        emit_bytes = self._header_emit_bytes
        trace_bytes = trace_id.encode("latin-1")

        async def send_with_trace_header(message: _Message) -> None:
            """Wrap ``send`` so we inject the trace_id response header."""

            if message.get("type") == "http.response.start":
                # ``message["headers"]`` is a list of (bytes, bytes) tuples.
                # Replace any pre-existing trace header (single source of
                # truth: the middleware) and append the canonical value.
                raw_headers = list(message.get("headers", []))
                filtered: list[tuple[bytes, bytes]] = [
                    (k, v)
                    for (k, v) in raw_headers
                    if k.lower() != self._header_lookup_bytes
                ]
                filtered.append((emit_bytes, trace_bytes))
                message = dict(message)
                message["headers"] = filtered
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace_header)
        finally:
            # Restore the previous context value so we don't leak the
            # trace_id into unrelated tasks reusing this event loop.
            _trace_id_ctx.reset(token)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_trace_id(self, scope: _Scope) -> str:
        """Decide whether to reuse the inbound trace_id or generate one."""

        inbound = self._extract_header(scope)

        if inbound and is_valid_trace_id(inbound):
            return inbound

        if inbound:
            # An inbound value was supplied but is malformed. Log at
            # debug level - this is not a security event, just a
            # misbehaving upstream - and overwrite with a fresh value
            # so downstream logs still have a coherent ID.
            _LOG.debug(
                "trace_middleware_invalid_inbound: header=%s value=%r",
                self._header_name,
                inbound,
            )

        return generate_trace_id()

    def _extract_header(self, scope: _Scope) -> str | None:
        """Pull the trace header value from the ASGI scope."""

        for raw_name, raw_value in scope.get("headers", []) or ():
            if raw_name.lower() == self._header_lookup_bytes:
                try:
                    return raw_value.decode("latin-1").strip()
                except (UnicodeDecodeError, AttributeError):
                    return None
        return None


# ---------------------------------------------------------------------------
# Logging filter - surface trace_id on every log record
# ---------------------------------------------------------------------------


class TraceLogFilter(logging.Filter):
    """Logging filter that stamps the current ``trace_id`` onto records.

    Designed for Temporal worker activities: the worker process installs this filter on the
    root logger once during startup, then each activity calls
    :func:`set_trace_id(input.trace_id)` at entry.  Every log record
    emitted between the ``set_trace_id`` call and the activity return
    inherits the trace_id via the per-task :class:`contextvars.ContextVar`
    - without the activity body having to plumb the value through every
    log call manually.

    The filter writes to two record attributes so structured-log
    formatters (JSON, Loki, OTel) can pick whichever convention the
    pipeline expects:

    * ``record.trace_id`` - the canonical attribute name used across
      the platform (mirrors the ``trace_id`` field consumed by the
      Admin Dashboard log filter).
    * ``record.traceId`` - camelCase alias for OpenTelemetry-style
      log emitters that prefer JSON-friendly keys.

    The filter never *drops* records (always returns ``True``) - its
    sole purpose is enrichment.

    Usage::

        import logging
        from observability import TraceLogFilter

        root = logging.getLogger()
        root.addFilter(TraceLogFilter())

        # ... later, inside an activity ...
        from observability import set_trace_id
        set_trace_id(input.trace_id)
        logging.info("processing")  # log record now has .trace_id set
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        trace_id = _trace_id_ctx.get()
        # Use ``setattr`` rather than direct dict access so the filter
        # works correctly with custom :class:`LogRecord` subclasses
        # (some pipelines use slots).
        if not hasattr(record, "trace_id"):
            record.trace_id = trace_id
        if not hasattr(record, "traceId"):
            record.traceId = trace_id
        return True
