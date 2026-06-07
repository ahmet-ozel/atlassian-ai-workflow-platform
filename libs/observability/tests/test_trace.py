"""Unit tests for the trace_id propagator.

Checks UUIDv7 trace_id generation and preservation of inbound
``X-Trace-Id`` headers across hops.

The tests cover the three pieces of the public API:

1. :func:`generate_trace_id` - RFC 9562 §5.7 layout, time-ordering,
   uniqueness across many draws.
2. :func:`get_trace_id` / :func:`set_trace_id` - context isolation
   between concurrent async tasks.
3. :class:`TraceMiddleware` - preserve-vs-generate decision, response
   header injection, scope.state propagation, non-HTTP pass-through.

The async-flavoured tests use :func:`asyncio.run` directly rather than
``pytest-asyncio`` so the ``observability`` library doesn't need to add
a test-only dependency.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from contextvars import copy_context

import pytest

from observability import (
    TRACE_HEADER,
    TraceMiddleware,
    generate_trace_id,
    get_trace_id,
    is_valid_trace_id,
    set_trace_id,
)

# ---------------------------------------------------------------------------
# generate_trace_id - RFC 9562 §5.7 conformance
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def test_generate_trace_id_matches_canonical_uuid_layout() -> None:
    trace_id = generate_trace_id()
    assert _UUID_RE.match(trace_id), trace_id


def test_generate_trace_id_is_lowercase() -> None:
    trace_id = generate_trace_id()
    assert trace_id == trace_id.lower()


def test_generate_trace_id_has_version_7_nibble() -> None:
    """Bits 48..51 must be 0b0111 (= '7' in canonical position 14)."""

    trace_id = generate_trace_id()
    # Canonical layout: xxxxxxxx-xxxx-Mxxx-Nxxx-xxxxxxxxxxxx
    # The 'M' nibble is at index 14 in the dashed string.
    assert trace_id[14] == "7", trace_id


def test_generate_trace_id_has_rfc4122_variant_bits() -> None:
    """Bits 64..65 must be 0b10 - the 'N' nibble is one of {8, 9, a, b}."""

    trace_id = generate_trace_id()
    # The 'N' nibble is at index 19.
    assert trace_id[19] in "89ab", trace_id


def test_generate_trace_id_embeds_current_unix_ms() -> None:
    """The first 48 bits encode milliseconds since the Unix epoch."""

    before_ms = int(time.time() * 1000)
    trace_id = generate_trace_id()
    after_ms = int(time.time() * 1000)

    # Reconstruct the 48-bit millisecond timestamp from the first 12
    # hex characters (8 + 4) of the canonical form.
    ts_hex = trace_id[0:8] + trace_id[9:13]
    ts_ms = int(ts_hex, 16)

    assert before_ms - 5 <= ts_ms <= after_ms + 5, (
        before_ms,
        ts_ms,
        after_ms,
    )


def test_generate_trace_id_round_trips_through_uuid_module() -> None:
    """The output must parse cleanly via :class:`uuid.UUID`."""

    trace_id = generate_trace_id()
    parsed = uuid.UUID(trace_id)
    assert parsed.version == 7
    # RFC 4122 / 9562 variant: top two bits of clock_seq_hi_and_reserved
    # are 0b10 → variant string is ``RFC_4122``.
    assert parsed.variant == uuid.RFC_4122


def test_generate_trace_id_produces_unique_values() -> None:
    ids = {generate_trace_id() for _ in range(5_000)}
    assert len(ids) == 5_000


def test_generate_trace_id_is_monotonically_orderable_by_ms() -> None:
    """Two trace_ids generated in order have non-decreasing timestamps."""

    t1 = generate_trace_id()
    time.sleep(0.002)  # ensure the millisecond clock advances
    t2 = generate_trace_id()

    ts1 = int((t1[0:8] + t1[9:13]), 16)
    ts2 = int((t2[0:8] + t2[9:13]), 16)
    assert ts2 >= ts1


# ---------------------------------------------------------------------------
# is_valid_trace_id
# ---------------------------------------------------------------------------


def test_is_valid_trace_id_accepts_generated_value() -> None:
    assert is_valid_trace_id(generate_trace_id())


def test_is_valid_trace_id_accepts_arbitrary_uuid_versions() -> None:
    # Standard UUIDv4 must also be accepted (8.7 leniency).
    assert is_valid_trace_id(str(uuid.uuid4()))


def test_is_valid_trace_id_rejects_empty_or_none() -> None:
    assert not is_valid_trace_id(None)
    assert not is_valid_trace_id("")


def test_is_valid_trace_id_rejects_wrong_length() -> None:
    assert not is_valid_trace_id("abc")
    assert not is_valid_trace_id("0" * 35)
    assert not is_valid_trace_id("0" * 37)


def test_is_valid_trace_id_rejects_misplaced_dashes() -> None:
    # 36 chars but dashes in the wrong positions.
    assert not is_valid_trace_id("0123456789-0-0-0-0123456789abcdef0123")


def test_is_valid_trace_id_rejects_non_hex_chars() -> None:
    assert not is_valid_trace_id("zzzzzzzz-zzzz-7zzz-8zzz-zzzzzzzzzzzz")


# ---------------------------------------------------------------------------
# Context variable isolation
# ---------------------------------------------------------------------------


def test_get_trace_id_default_is_empty_string() -> None:
    # Run inside a fresh context so we don't leak prior test state.
    ctx = copy_context()
    assert ctx.run(get_trace_id) == ""


def test_set_trace_id_round_trips() -> None:
    def _round_trip() -> str:
        set_trace_id("018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34")
        return get_trace_id()

    ctx = copy_context()
    assert ctx.run(_round_trip) == "018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34"


def test_concurrent_tasks_have_isolated_trace_ids() -> None:
    """Two coroutines started under different contexts MUST NOT share state."""

    seen: dict[str, str] = {}

    async def runner(name: str, value: str) -> None:
        set_trace_id(value)
        # Yield to let the other coroutine run between set and get.
        await asyncio.sleep(0)
        seen[name] = get_trace_id()

    async def driver() -> None:
        # Each Task is started with its own copied context, so the
        # set_trace_id() inside ``runner`` cannot leak into siblings.
        loop = asyncio.get_running_loop()
        ctx_a = copy_context()
        ctx_b = copy_context()
        task_a = loop.create_task(
            runner("a", "11111111-1111-7111-8111-111111111111"),
            context=ctx_a,
        )
        task_b = loop.create_task(
            runner("b", "22222222-2222-7222-8222-222222222222"),
            context=ctx_b,
        )
        await asyncio.gather(task_a, task_b)

    asyncio.run(driver())

    assert seen["a"] == "11111111-1111-7111-8111-111111111111"
    assert seen["b"] == "22222222-2222-7222-8222-222222222222"


# ---------------------------------------------------------------------------
# TraceMiddleware - ASGI behaviour
# ---------------------------------------------------------------------------


def _build_scope(headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/example",
        "headers": list(headers or []),
    }


async def _identity_app(scope, receive, send):  # type: ignore[no-untyped-def]
    """Minimal ASGI app: emits a 200 response, capturing trace_id."""

    # Capture the per-request trace_id so the test can assert on it.
    captured = scope.setdefault("__captured__", {})
    captured["trace_id"] = get_trace_id()
    if isinstance(scope.get("state"), dict):
        captured["state_trace_id"] = scope["state"].get("trace_id")

    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


class _Recorder:
    """Helper that captures messages emitted by an ASGI app."""

    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)

    async def receive(self) -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    @property
    def response_headers(self) -> dict[str, str]:
        for msg in self.messages:
            if msg.get("type") == "http.response.start":
                return {
                    k.decode("latin-1").lower(): v.decode("latin-1")
                    for k, v in msg.get("headers", [])
                }
        return {}


def _drive(middleware, scope, recorder):  # type: ignore[no-untyped-def]
    """Run the middleware to completion under a fresh event loop."""

    asyncio.run(middleware(scope, recorder.receive, recorder))


def test_middleware_generates_trace_id_when_header_absent() -> None:
    middleware = TraceMiddleware(_identity_app)
    scope = _build_scope()
    recorder = _Recorder()

    _drive(middleware, scope, recorder)

    captured = scope["__captured__"]
    assert is_valid_trace_id(captured["trace_id"])
    assert captured["state_trace_id"] == captured["trace_id"]
    assert recorder.response_headers["x-trace-id"] == captured["trace_id"]


def test_middleware_preserves_valid_inbound_trace_id() -> None:
    """Preserve trace_id on retry."""

    inbound = "018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34"
    middleware = TraceMiddleware(_identity_app)
    scope = _build_scope([(b"x-trace-id", inbound.encode("latin-1"))])
    recorder = _Recorder()

    _drive(middleware, scope, recorder)

    assert scope["__captured__"]["trace_id"] == inbound
    assert recorder.response_headers["x-trace-id"] == inbound


def test_middleware_overrides_invalid_inbound_trace_id() -> None:
    middleware = TraceMiddleware(_identity_app)
    scope = _build_scope([(b"x-trace-id", b"not-a-uuid")])
    recorder = _Recorder()

    _drive(middleware, scope, recorder)

    captured = scope["__captured__"]["trace_id"]
    assert is_valid_trace_id(captured)
    assert captured != "not-a-uuid"
    assert recorder.response_headers["x-trace-id"] == captured


def test_middleware_replaces_app_emitted_trace_header() -> None:
    """The middleware MUST be the single source of truth for the response header."""

    rogue_value = b"00000000-0000-0000-0000-000000000000"

    async def rogue_app(scope, receive, send):  # type: ignore[no-untyped-def]
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"x-trace-id", rogue_value)],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    inbound = "018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34"
    middleware = TraceMiddleware(rogue_app)
    scope = _build_scope([(b"x-trace-id", inbound.encode("latin-1"))])
    recorder = _Recorder()

    _drive(middleware, scope, recorder)

    headers = recorder.response_headers
    assert headers["x-trace-id"] == inbound
    # And there must be exactly one trace header on the wire (the
    # rogue downstream value must have been filtered out).
    raw = next(
        msg["headers"]
        for msg in recorder.messages
        if msg["type"] == "http.response.start"
    )
    occurrences = sum(1 for name, _ in raw if name.lower() == b"x-trace-id")
    assert occurrences == 1


def test_middleware_passes_through_non_http_scopes() -> None:
    """WebSocket / lifespan scopes must not be mutated."""

    seen: dict[str, object] = {}

    async def downstream(scope, receive, send):  # type: ignore[no-untyped-def]
        seen["scope_type"] = scope.get("type")
        seen["trace_id"] = get_trace_id()

    middleware = TraceMiddleware(downstream)
    scope = {"type": "lifespan", "headers": []}

    asyncio.run(middleware(scope, _Recorder().receive, _Recorder()))

    # No trace_id should have been injected into context for non-HTTP scopes.
    assert seen["trace_id"] == ""
    assert seen["scope_type"] == "lifespan"
    assert "state" not in scope


def test_middleware_resets_context_after_request() -> None:
    """The trace_id MUST NOT leak past the lifecycle of a single request."""

    captured_inside: list[str] = []

    async def capture_app(scope, receive, send):  # type: ignore[no-untyped-def]
        captured_inside.append(get_trace_id())
        await _identity_app(scope, receive, send)

    middleware = TraceMiddleware(capture_app)
    scope = _build_scope()
    recorder = _Recorder()

    async def driver() -> str:
        await middleware(scope, recorder.receive, recorder)
        return get_trace_id()

    outer_after = asyncio.run(driver())

    assert is_valid_trace_id(captured_inside[0])
    # The outer scope should NOT see the inside-request value.
    assert outer_after == ""


def test_middleware_uses_custom_header_name() -> None:
    middleware = TraceMiddleware(_identity_app, header_name="X-Correlation-Id")
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
    }
    recorder = _Recorder()
    _drive(middleware, scope, recorder)

    assert "x-correlation-id" in recorder.response_headers
    assert "x-trace-id" not in recorder.response_headers


def test_middleware_emits_trace_header_in_canonical_case() -> None:
    """Response header should preserve the original ``X-Trace-Id`` casing."""

    middleware = TraceMiddleware(_identity_app)
    scope = _build_scope()
    recorder = _Recorder()
    _drive(middleware, scope, recorder)

    # Walk raw headers - case is preserved on the wire.
    raw = next(
        msg["headers"]
        for msg in recorder.messages
        if msg["type"] == "http.response.start"
    )
    names = [name.decode("latin-1") for name, _ in raw]
    assert TRACE_HEADER in names


@pytest.mark.parametrize(
    "inbound",
    [
        # Valid UUID strings of various versions.
        "11111111-1111-1111-8111-111111111111",  # v1
        "22222222-2222-4222-8222-222222222222",  # v4
        "33333333-3333-7333-9333-333333333333",  # v7 (lowercase)
        "44444444-4444-7444-A444-444444444444",  # v7 (mixed case)
    ],
)
def test_middleware_preserves_uuid_regardless_of_version(inbound: str) -> None:
    middleware = TraceMiddleware(_identity_app)
    scope = _build_scope([(b"x-trace-id", inbound.encode("latin-1"))])
    recorder = _Recorder()

    _drive(middleware, scope, recorder)

    assert scope["__captured__"]["trace_id"] == inbound
    assert recorder.response_headers["x-trace-id"] == inbound



# ---------------------------------------------------------------------------
# TraceLogFilter - log-record enrichment
# ---------------------------------------------------------------------------


def test_trace_log_filter_sets_trace_id_attribute_on_record() -> None:
    """Filter must populate ``record.trace_id`` from the contextvars value.

    Worker activities surface trace_id on every log line; the filter is the bridge
    between the contextvars-bound trace_id and the structured-log
    record.
    """

    from observability import TraceLogFilter

    set_trace_id("018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34")
    filt = TraceLogFilter()
    record = logging.makeLogRecord({"msg": "test"})

    keep = filt.filter(record)

    assert keep is True  # filter never drops records
    assert record.trace_id == "018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34"
    # camelCase alias for OTel/JSON-friendly emitters.
    assert record.traceId == "018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34"


def test_trace_log_filter_emits_empty_string_when_no_trace_id_set() -> None:
    """Filter still populates the attribute with the empty default.

    The downstream log formatter typically uses ``%(trace_id)s`` -
    if the attribute were missing the formatter would raise
    ``KeyError``.  Always setting the attribute (even to ``""``)
    keeps the format string resolvable in the no-context branch.
    """

    from observability import TraceLogFilter

    # Reset the contextvars slot - empty default per
    # ``observability.trace._trace_id_ctx``.
    set_trace_id("")
    filt = TraceLogFilter()
    record = logging.makeLogRecord({"msg": "test"})

    filt.filter(record)

    assert record.trace_id == ""
    assert record.traceId == ""


def test_trace_log_filter_is_idempotent() -> None:
    """Re-applying the filter does not overwrite an already-set value.

    Pipelines may install both the framework-level filter and a
    per-handler one; the filter must be safe under double-application
    so the per-handler value (typically more specific) is preserved.
    """

    from observability import TraceLogFilter

    set_trace_id("018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34")
    filt = TraceLogFilter()
    record = logging.makeLogRecord({"msg": "test"})
    record.trace_id = "pre-existing-value"

    filt.filter(record)

    # The pre-existing value wins - ``hasattr`` short-circuits the
    # assignment.
    assert record.trace_id == "pre-existing-value"


# ``logging`` is referenced above; import here to keep the
# observability lib's stdlib-only dependency contract.
import logging  # noqa: E402
