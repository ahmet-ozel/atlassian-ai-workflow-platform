"""Unit test - atlassian_mcp_bitbucket MCP server's inline ``TraceMiddleware``.

The MCP server is the central hub for every Atlassian call from every
worker and service; it must propagate the ``X-Trace-Id`` header
end-to-end so the Admin Dashboard log filter can correlate workflow logs
with the Atlassian-side request log.

The MCP server ships a vendored virtualenv and does not import the
workspace ``observability`` library - instead it carries an inline
:class:`TraceMiddleware` implementation that mirrors the contract.
This test exercises that inline class directly against an ASGI
recorder so the wire shape is verifiable without a live FastMCP /
Starlette stack.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

# Make the in-tree atlassian_mcp_bitbucket ``src`` directory importable
# without depending on the vendored ``.venv``.  The MCP server's
# ``servers/__init__.py`` pulls in heavyweight FastMCP and Atlassian
# dependencies; the imports work because each of those packages is
# also on the user's site-packages.
_ATLASSIAN_SRC = (
    Path(__file__).resolve().parents[2]
    / "services"
    / "atlassian_mcp_bitbucket"
    / "src"
).resolve()
if str(_ATLASSIAN_SRC) not in sys.path:
    sys.path.insert(0, str(_ATLASSIAN_SRC))

try:
    from mcp_atlassian.servers.main import (  # noqa: E402
        TraceMiddleware,
        _generate_trace_id,
        _is_valid_trace_id,
    )
except Exception as exc:  # pragma: no cover - skip if MCP deps missing
    pytest.skip(f"atlassian_mcp_bitbucket deps unavailable: {exc}", allow_module_level=True)


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# ---------------------------------------------------------------------------
# UUIDv7 generator + validator
# ---------------------------------------------------------------------------


def test_generate_trace_id_matches_uuidv7_layout() -> None:
    trace_id = _generate_trace_id()
    assert _UUID_RE.match(trace_id), trace_id


def test_is_valid_trace_id_accepts_canonical_layout() -> None:
    assert _is_valid_trace_id("018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34")


def test_is_valid_trace_id_rejects_malformed() -> None:
    assert not _is_valid_trace_id("not-a-uuid")
    assert not _is_valid_trace_id("")
    assert not _is_valid_trace_id("018f7d4d_5f8c_7c4d_92ab_1f6f5a4d9b34")  # underscores
    assert not _is_valid_trace_id("zzz" * 12)


# ---------------------------------------------------------------------------
# TraceMiddleware ASGI behaviour
# ---------------------------------------------------------------------------


def _build_scope(headers: list[tuple[bytes, bytes]] | None = None) -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers or [],
    }


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(dict(message))


def _identity_app(scope, receive, send):
    """ASGI app that emits a 200 response with no body."""

    async def runner() -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    return runner()


def test_middleware_generates_trace_id_when_header_absent() -> None:
    middleware = TraceMiddleware(_identity_app)
    scope = _build_scope()
    recorder = _Recorder()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(middleware(scope, receive, recorder))

    start = next(m for m in recorder.messages if m["type"] == "http.response.start")
    headers = dict(start["headers"])
    assert b"X-Trace-Id" in headers or b"x-trace-id" in headers
    trace = headers.get(b"X-Trace-Id") or headers.get(b"x-trace-id")
    assert trace is not None
    assert _UUID_RE.match(trace.decode("latin-1"))

    # Scope state was populated for downstream middlewares.
    assert scope["state"]["trace_id"] == trace.decode("latin-1")


def test_middleware_preserves_inbound_trace_id() -> None:
    inbound = "018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34"
    middleware = TraceMiddleware(_identity_app)
    scope = _build_scope([(b"x-trace-id", inbound.encode("latin-1"))])
    recorder = _Recorder()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(middleware(scope, receive, recorder))

    start = next(m for m in recorder.messages if m["type"] == "http.response.start")
    headers = dict(start["headers"])
    trace = headers.get(b"X-Trace-Id") or headers.get(b"x-trace-id")
    assert trace is not None
    assert trace.decode("latin-1") == inbound
    assert scope["state"]["trace_id"] == inbound


def test_middleware_overrides_invalid_inbound_trace_id() -> None:
    middleware = TraceMiddleware(_identity_app)
    scope = _build_scope([(b"x-trace-id", b"not-a-uuid")])
    recorder = _Recorder()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(middleware(scope, receive, recorder))

    start = next(m for m in recorder.messages if m["type"] == "http.response.start")
    headers = dict(start["headers"])
    trace = headers.get(b"X-Trace-Id") or headers.get(b"x-trace-id")
    assert trace is not None
    decoded = trace.decode("latin-1")
    assert _UUID_RE.match(decoded)
    assert decoded != "not-a-uuid"


def test_middleware_passes_through_non_http_scope() -> None:
    """Lifespan / WebSocket scopes must pass through untouched."""

    seen = {}

    def downstream(scope, receive, send):
        seen["called"] = True

        async def _run():
            return None

        return _run()

    middleware = TraceMiddleware(downstream)
    scope = {"type": "lifespan", "headers": []}

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(_msg):
        return None

    asyncio.run(middleware(scope, receive, send))

    assert seen.get("called") is True
    # The middleware must not have stamped trace_id on a non-http scope.
    assert "state" not in scope
