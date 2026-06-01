"""Property 11 — every MCP request log carries a non-empty ``client_source``.

**Validates: Requirements 9.1, 9.2** (platform-gap-fill spec)

* **R9.1** — every inbound MCP request log entry SHALL contain a
  ``client_source`` field.
* **R9.2** — when the ``X-Client-Source`` header is absent, empty, or
  whitespace-only, the recorded ``client_source`` SHALL be the literal
  string ``"unknown"``.

This test drives :class:`mcp_atlassian.servers.main.ClientSourceLoggingMiddleware`
directly through the ASGI protocol with Hypothesis-generated request
sequences. The middleware is the single source of truth for the
``client_source`` field, so a property over it covers every code path
that produces an MCP request log line.

The Hypothesis strategy generates a random *sequence* of requests
(rather than a single request per example) so we exercise the
property: *for any random sequence of MCP requests, every recorded
request log entry has a non-empty ``client_source``*. Each request in
the sequence draws one of the following header shapes uniformly:

1. **Absent** — no ``X-Client-Source`` header at all.
2. **Empty** — header present but value is ``b""``.
3. **Whitespace-only** — value is ASCII / Unicode whitespace bytes.
4. **Known** — value drawn from :data:`KNOWN_CLIENT_SOURCES`.
5. **Random non-empty** — arbitrary printable ASCII bytes.

For every emitted log record we assert:

* ``record.client_source`` is a non-empty :class:`str` (R9.1).
* The ``scope.state`` mirror is a non-empty :class:`str`.
* When the input header was absent / empty / whitespace-only the
  recorded value is exactly ``"unknown"`` (R9.2).
* When the input header was a known non-whitespace value, the
  recorded value equals the *stripped* input.

The ``KNOWN_CLIENT_SOURCES`` set is duplicated locally so the test
does not depend on the ``http_shared`` workspace lib being importable
inside the ``mcp-atlassian`` virtualenv (the lib lives in a separate
``uv`` workspace and is not pulled in by ``pyproject.toml``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mcp_atlassian.servers.main import (
    ClientSourceLoggingMiddleware,
    _CLIENT_SOURCE_UNKNOWN,
)

# ---------------------------------------------------------------------------
# Hypothesis profile — keep examples bounded so the suite finishes fast in
# CI but still provides solid coverage of the input space.
# ---------------------------------------------------------------------------

_PROFILE = settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)


# ---------------------------------------------------------------------------
# Inlined component identifiers (mirrors ``http_shared.KNOWN_CLIENT_SOURCES``
# verbatim — kept local so the MCP server's vendored virtualenv does not
# need a workspace path dependency on ``http-shared``).
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Header-value strategies
# ---------------------------------------------------------------------------

#: Whitespace bytes the middleware must treat as "no client_source".
#: Mirrors the alphabet used by the empty-query property test
#: (``test_mention_empty_query_property``).
_WHITESPACE_BYTES_ALPHABET = " \t\n\r\x0b\x0c"

_whitespace_only_bytes: st.SearchStrategy[bytes] = st.text(
    alphabet=_WHITESPACE_BYTES_ALPHABET,
    min_size=0,
    max_size=8,
).map(lambda s: s.encode("latin-1"))

#: Non-empty, non-whitespace identifiers — printable ASCII excluding
#: whitespace and control characters. Both ``KNOWN_CLIENT_SOURCES`` and
#: arbitrary "unknown" identifiers are valid here so the property covers
#: identifiers the platform has not yet registered.
_arbitrary_identifier_bytes: st.SearchStrategy[bytes] = st.text(
    alphabet=st.characters(
        min_codepoint=ord("!"),  # 0x21
        max_codepoint=ord("~"),  # 0x7E
        # Exclude whitespace explicitly (already excluded by the codepoint
        # range, but spell it out for clarity).
        blacklist_characters=" \t",
    ),
    min_size=1,
    max_size=32,
).map(lambda s: s.encode("latin-1"))

_known_source_bytes: st.SearchStrategy[bytes] = st.sampled_from(
    sorted(KNOWN_CLIENT_SOURCES)
).map(lambda s: s.encode("latin-1"))


@st.composite
def _header_shape(
    draw: st.DrawFn,
) -> tuple[str, bytes | None]:
    """Draw a header shape for one request.

    Returns a ``(category, value)`` tuple where ``category`` is one of:

    * ``"absent"``    — no ``X-Client-Source`` header at all (value is ``None``).
    * ``"empty"``     — header present, value is ``b""``.
    * ``"whitespace"`` — value is whitespace-only bytes.
    * ``"known"``     — value is a known component identifier.
    * ``"unknown_id"`` — value is a non-empty, non-whitespace identifier
      that is *not* in :data:`KNOWN_CLIENT_SOURCES`. The middleware must
      still preserve this value verbatim (R9.1 — only the empty / missing
      case maps to ``"unknown"``).
    """
    category = draw(
        st.sampled_from(["absent", "empty", "whitespace", "known", "unknown_id"])
    )
    if category == "absent":
        return ("absent", None)
    if category == "empty":
        return ("empty", b"")
    if category == "whitespace":
        return ("whitespace", draw(_whitespace_only_bytes))
    if category == "known":
        return ("known", draw(_known_source_bytes))
    # ``unknown_id`` — random non-empty identifier; reject draws that
    # happen to land inside KNOWN_CLIENT_SOURCES so the category label
    # stays accurate.
    raw = draw(_arbitrary_identifier_bytes)
    while raw.decode("latin-1") in KNOWN_CLIENT_SOURCES:
        raw = draw(_arbitrary_identifier_bytes)
    return ("unknown_id", raw)


#: Sequence of 1..8 requests. Sequences (rather than a single request)
#: exercise the property: the middleware must emit a well-formed log
#: record on *every* request, regardless of the prior request's shape.
_request_sequences: st.SearchStrategy[list[tuple[str, bytes | None]]] = st.lists(
    _header_shape(), min_size=1, max_size=8
)


# ---------------------------------------------------------------------------
# Minimal ASGI harness — mirrors ``tests/unit/servers/test_client_source_middleware.py``.
# ---------------------------------------------------------------------------


def _build_scope(*, header_value: bytes | None) -> dict[str, Any]:
    headers: list[tuple[bytes, bytes]] = []
    if header_value is not None:
        headers.append((b"x-client-source", header_value))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": headers,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
    }


class _MockApp:
    """Minimal ASGI 3 app — drains the body and returns ``200 OK``.

    Drains the (possibly wrapped) ``receive`` callable so the middleware
    must replay the buffered body, mirroring the contract exercised by
    the unit tests.
    """

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        # Drain body so the middleware's body buffering / replay path runs.
        more = True
        while more:
            msg = await receive()
            if msg["type"] != "http.request":
                break
            more = msg.get("more_body", False)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b'{"ok":true}'})


def _make_receive(body: bytes) -> Any:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _noop_send_factory() -> Any:
    async def send(_msg: dict[str, Any]) -> None:
        return None

    return send


# Single tools/call body reused across every request — the ``client_source``
# log field is independent of the request body, so a constant payload
# keeps the property focused on the header behaviour.
_REQUEST_BODY: bytes = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "jira_get_issue", "arguments": {}},
    }
).encode("utf-8")


# ---------------------------------------------------------------------------
# Property — Property 11
# ---------------------------------------------------------------------------


class TestClientSourceTaggingProperty:
    """Property 11 — every MCP request log entry carries a non-empty
    ``client_source``; missing / empty / whitespace headers map to
    ``"unknown"``.

    **Validates: Requirements 9.1, 9.2**
    """

    @_PROFILE
    @given(sequence=_request_sequences)
    def test_every_request_log_record_has_non_empty_client_source(
        self, sequence: list[tuple[str, bytes | None]]
    ) -> None:
        """For ANY random sequence of MCP requests, every log record
        emitted by :class:`ClientSourceLoggingMiddleware` SHALL contain
        a non-empty ``client_source`` field, and absent / empty /
        whitespace inputs SHALL map to ``"unknown"``.
        """
        # Hypothesis' generated examples re-run inside a single test
        # function instance, so we attach a fresh log handler per
        # example to capture exactly the records emitted by *this*
        # iteration (caplog from pytest is unreliable under @given
        # because the fixture's cleanup runs only once at the test
        # boundary, not between examples).
        captured: list[logging.LogRecord] = []

        class _ListHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _ListHandler(level=logging.DEBUG)
        target_logger = logging.getLogger("mcp-atlassian.client_source")
        prior_level = target_logger.level
        target_logger.addHandler(handler)
        target_logger.setLevel(logging.DEBUG)
        try:
            asyncio.run(self._drive_sequence(sequence))
        finally:
            target_logger.removeHandler(handler)
            target_logger.setLevel(prior_level)

        # One log record per request — the middleware emits exactly one
        # ``mcp_request`` line per inbound HTTP request.
        assert len(captured) == len(sequence), (
            f"Expected {len(sequence)} log records, got {len(captured)}: "
            f"{[r.getMessage() for r in captured]!r}"
        )

        for (category, raw_value), record in zip(sequence, captured, strict=True):
            # ----------------------------------------------------------
            # R9.1 — the log record must always carry a ``client_source``
            # field, and its value must be a non-empty string.
            # ----------------------------------------------------------
            assert hasattr(record, "client_source"), (
                f"Log record missing 'client_source' attribute "
                f"(category={category!r}, raw={raw_value!r}): "
                f"{record.__dict__!r}"
            )
            client_source = record.client_source
            assert isinstance(client_source, str), (
                f"client_source must be str, got {type(client_source).__name__} "
                f"(category={category!r}, value={client_source!r})"
            )
            assert client_source != "", (
                f"client_source must be non-empty (category={category!r}, "
                f"raw={raw_value!r})"
            )

            # ----------------------------------------------------------
            # R9.2 — absent / empty / whitespace header → "unknown".
            # ----------------------------------------------------------
            if category in {"absent", "empty", "whitespace"}:
                assert client_source == _CLIENT_SOURCE_UNKNOWN, (
                    f"Expected client_source={_CLIENT_SOURCE_UNKNOWN!r} for "
                    f"category={category!r} (raw={raw_value!r}), "
                    f"got {client_source!r}"
                )
            else:
                # ``known`` and ``unknown_id`` categories must preserve
                # the (stripped) header value so observability dashboards
                # can group traffic by the originating component.
                assert raw_value is not None  # type-narrowing for mypy
                expected = raw_value.decode("latin-1").strip()
                assert client_source == expected, (
                    f"Expected client_source={expected!r} for "
                    f"category={category!r} (raw={raw_value!r}), "
                    f"got {client_source!r}"
                )
                # Sanity: ``known`` draws must come from the registry.
                if category == "known":
                    assert client_source in KNOWN_CLIENT_SOURCES, (
                        f"Known-source draw {client_source!r} is not in "
                        f"KNOWN_CLIENT_SOURCES"
                    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _drive_sequence(
        self, sequence: list[tuple[str, bytes | None]]
    ) -> None:
        """Run every request in ``sequence`` through a fresh middleware
        instance.

        Each request gets its own middleware + scope so state from a
        prior request cannot mask a missing-log-record bug on the
        next request.
        """
        for _category, header_value in sequence:
            mw = ClientSourceLoggingMiddleware(_MockApp())
            scope = _build_scope(header_value=header_value)
            await mw(scope, _make_receive(_REQUEST_BODY), _noop_send_factory())

            # Defensive cross-check: the middleware also mirrors the
            # resolved value onto ``scope.state`` for downstream
            # handlers. This is part of R9.1 — a non-empty value must
            # always be observable post-middleware.
            assert "state" in scope, "middleware must populate scope['state']"
            assert "client_source" in scope["state"], (
                "middleware must store client_source on scope.state"
            )
            stored = scope["state"]["client_source"]
            assert isinstance(stored, str) and stored != "", (
                f"scope.state['client_source'] must be a non-empty str, "
                f"got {stored!r}"
            )
