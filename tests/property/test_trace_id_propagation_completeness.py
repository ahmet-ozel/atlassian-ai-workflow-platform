"""Property-based tests for trace ID propagation completeness.

Behavior
--------
For ANY inbound HTTP request that flows through the platform, the same
``trace_id`` value MUST appear at every observable hop:

* The ``X-Trace-Id`` request header on the inbound automation-service
  request (or a freshly generated UUIDv7 when the inbound header is
  absent / malformed).
* The ``X-Trace-Id`` response header echoed back by automation-service
  so retries keep the same trace_id.
* The contextvars-bound :func:`observability.get_trace_id` value
  observed by any code path running inside the request handler
  so workers / activities can surface trace_id on every log line via
  the same contextvars slot.
* The ``X-Trace-Id`` header on every outbound MCP request issued by
  :func:`http_shared.make_mcp_client` while the request handler is
  active.

This test does NOT need a live cluster - it exercises the in-process
middleware + http_shared client + observability ``set_trace_id``
wiring already in place. We drive a
request through ``automation_service.app.create_app()`` test client,
capture the resolved trace_id at the boundary, and assert it shows up
in any outbound MCP request fired off the contextvars context that
the middleware populated.

Strategies
----------
The Hypothesis search space covers three trace_id input regimes that
cover the expected input regimes:

1. **Inbound UUID-shape trace_id** - UUIDv4 / UUIDv7 strings supplied
   in ``X-Trace-Id``. These MUST be preserved end-to-end.
2. **Empty / missing inbound header** - the middleware MUST generate
   a fresh UUIDv7 and propagate it through the chain.
3. **Invalid / malformed inbound header** - the middleware MUST
   discard the malformed value, generate a fresh UUIDv7, and
   propagate that fresh value.

For each regime we then simulate the *next hop* - outbound MCP /
Firecrawl / admin-side calls - by constructing an
``httpx.MockTransport`` and asserting the trace_id captured by the
transport equals the trace_id observed at the inbound boundary.

Layout
------
The test file mirrors the structural conventions used by sibling
property tests (``test_compose_bootstrap_minimal.py``,
``test_webhook_predicates.py``):

* ``sys.path`` is bootstrapped to expose ``automation_service`` and
  the workspace ``observability`` / ``http_shared`` libs without an
  editable install.
* Hypothesis ``@settings`` profiles use a generous ``deadline=None``
  because the in-process FastAPI ``TestClient`` is not particularly
  fast under MockTransport.
* Concrete regression anchors (``pytest.mark.parametrize``) pin the
  three input regimes by example so a wiring bug that empties the
  Hypothesis sample space cannot silently green-out the suite.
"""

from __future__ import annotations

import asyncio
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap - make automation-service / observability / http_shared
# importable without an editable install. Mirrors
# ``test_webhook_predicates.py``.
# ---------------------------------------------------------------------------

_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

_AUTOMATION_ROOT = (
    Path(__file__).resolve().parents[1].parent
    / "services"
    / "automation-service"
)
for _bootstrap in (_AUTOMATION_ROOT, _AUTOMATION_ROOT / "src"):
    _bs = str(_bootstrap)
    if _bs not in sys.path:
        sys.path.insert(0, _bs)

from automation_service.app import create_app  # noqa: E402
from http_shared import make_mcp_client  # noqa: E402
from observability import (  # noqa: E402
    TRACE_HEADER,
    generate_trace_id,
    get_trace_id,
    is_valid_trace_id,
    set_trace_id,
)


# ---------------------------------------------------------------------------
# UUIDv7 layout - matches the regex shipped by ``observability.trace``
# ---------------------------------------------------------------------------

_UUIDV7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# A generic UUID matcher (any version) - the middleware preserves
# inbound UUIDs regardless of version.
_ANY_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Hypothesis strategies - three inbound trace_id regimes
# ---------------------------------------------------------------------------


def _uuid_v4_strategy() -> st.SearchStrategy[str]:
    """Generate canonical UUIDv4 strings (lower-case, dashed)."""
    return st.uuids(version=4).map(lambda u: str(u))


def _uuid_v7_strategy() -> st.SearchStrategy[str]:
    """Generate UUIDv7 strings via the observability lib's generator.

    We don't use ``st.uuids(version=7)`` because that returns a stub on
    older Hypothesis releases; the observability lib's
    :func:`generate_trace_id` always produces a well-formed UUIDv7.
    """
    return st.builds(generate_trace_id)


def _valid_inbound_trace_strategy() -> st.SearchStrategy[str]:
    """Union of UUIDv4 / UUIDv7 - any UUID-shape value is preserved."""
    return st.one_of(_uuid_v4_strategy(), _uuid_v7_strategy())


def _invalid_inbound_trace_strategy() -> st.SearchStrategy[str]:
    """Generate strings that are NOT valid UUIDs.

    These trigger the middleware's ``is_valid_trace_id`` rejection
    branch - the inbound value is discarded and a fresh UUIDv7 is
    generated.  ``filter`` keeps the search space honest by
    discarding accidental UUID-shape draws.
    """
    return st.text(
        alphabet=st.characters(
            min_codepoint=33,
            max_codepoint=126,
            blacklist_categories=("Cs",),
        ),
        min_size=1,
        max_size=64,
    ).filter(lambda s: not is_valid_trace_id(s))


def _empty_or_missing_strategy() -> st.SearchStrategy[str | None]:
    """Generate the "no inbound trace_id" regime.

    ``None`` means the header is omitted entirely.  ``""`` means the
    header is present but empty - both branches MUST end up with a
    freshly-generated UUIDv7.
    """
    return st.sampled_from([None, ""])


# ---------------------------------------------------------------------------
# Helpers - drive a request and capture the trace_id at every hop
# ---------------------------------------------------------------------------


def _run_with_inbound_trace(
    inbound: str | None,
) -> tuple[str, str | None, str | None]:
    """Drive a single request through the chain and return observed trace_ids.

    Returns
    -------
    tuple
        ``(response_header, contextvars_inside_handler, mcp_outbound_header)``

        * ``response_header`` - the ``X-Trace-Id`` value echoed back
          on the response (always non-empty).
        * ``contextvars_inside_handler`` - the value
          :func:`observability.get_trace_id` returns from inside the
          request handler.  ``None`` when the route did not capture
          it (always set in this test, but we surface ``None`` so
          assertion failures are precise about which hop diverged).
        * ``mcp_outbound_header`` - the ``X-Trace-Id`` header captured
          by the ``httpx.MockTransport`` for an outbound MCP call
          fired *from inside the request handler*.  ``None`` when no
          outbound call was issued.
    """

    captured: dict[str, Any] = {
        "context_trace": None,
        "outbound_trace": None,
    }

    app = create_app()

    @app.get("/__trace_probe__")
    async def _trace_probe() -> dict[str, str]:
        # 1. Capture the contextvars-bound trace_id observed inside
        # the request handler.  TraceMiddleware MUST have
        # populated this slot before we got here.
        captured["context_trace"] = get_trace_id()

        # 2. Issue an outbound "MCP call" through the canonical
        # factory and capture the X-Trace-Id header on the wire.
        # The factory's request hook reads from the same
        # contextvars slot - the captured value MUST equal the
        # one we read directly above.
        outbound_captured: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            outbound_captured.append(request)
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(_handler)
        client = make_mcp_client(
            client_source="automation-service",
            transport=transport,
        )
        try:
            await client.post(
                "http://atlassian-mcp:8090/mcp",
                json={},
            )
        finally:
            await client.aclose()

        if outbound_captured:
            outbound_request = outbound_captured[0]
            captured["outbound_trace"] = outbound_request.headers.get(
                TRACE_HEADER
            )

        return {"status": "probed"}

    headers: dict[str, str] = {}
    if inbound is not None:
        headers[TRACE_HEADER] = inbound

    with TestClient(app) as client:
        resp = client.get("/__trace_probe__", headers=headers)

    assert resp.status_code == 200, resp.text
    response_trace = resp.headers.get(TRACE_HEADER)
    assert response_trace is not None, (
        f"automation-service did not echo {TRACE_HEADER} on the response - "
        f"TraceMiddleware not wired? response.headers={dict(resp.headers)!r}"
    )

    return (
        response_trace,
        captured["context_trace"],
        captured["outbound_trace"],
    )


# ---------------------------------------------------------------------------
# Hypothesis-driven coverage for the three input regimes
# ---------------------------------------------------------------------------


_PROFILE = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)


@_PROFILE
@given(inbound=_valid_inbound_trace_strategy())
def test_property10_valid_inbound_trace_id_propagates_unchanged(
    inbound: str,
) -> None:
    """UUID-shape trace_id is preserved end-to-end.

    For any UUID-shape inbound ``X-Trace-Id`` header, the SAME value
    MUST appear on:

    * the response header,
    * the contextvars slot observed inside the request handler,
    * every outbound MCP request issued from the handler.
    """

    response_trace, context_trace, outbound_trace = _run_with_inbound_trace(
        inbound
    )

    assert response_trace == inbound, (
        f"automation-service did not "
        f"preserve inbound {TRACE_HEADER}; "
        f"inbound={inbound!r}, response={response_trace!r}"
    )
    assert context_trace == inbound, (
        f"TraceMiddleware did not bind "
        f"the inbound trace_id onto the contextvars slot; "
        f"inbound={inbound!r}, context={context_trace!r}"
    )
    assert outbound_trace == inbound, (
        f"outbound MCP request did not "
        f"carry the inbound trace_id; "
        f"inbound={inbound!r}, outbound={outbound_trace!r}"
    )

    # All three observation points agree by transitivity - pin that
    # explicitly so a regression in any one hop reports a precise diff.
    assert response_trace == context_trace == outbound_trace


@_PROFILE
@given(inbound=_empty_or_missing_strategy())
def test_property10_empty_inbound_triggers_fresh_uuidv7_chain(
    inbound: str | None,
) -> None:
    """Missing or empty inbound value produces a fresh UUIDv7.

    When the inbound ``X-Trace-Id`` header is absent or empty, the
    middleware generates a fresh UUIDv7 and the SAME generated value
    MUST appear on the response, on the contextvars slot, and on
    every outbound MCP request.
    """

    response_trace, context_trace, outbound_trace = _run_with_inbound_trace(
        inbound
    )

    # The generated value must match the canonical UUIDv7 layout.
    assert _UUIDV7_RE.match(response_trace), (
        f"generated trace_id is not "
        f"a UUIDv7; got response_trace={response_trace!r}"
    )
    assert context_trace == response_trace, (
        f"contextvars trace_id diverged "
        f"from response trace_id; "
        f"context={context_trace!r}, response={response_trace!r}"
    )
    assert outbound_trace == response_trace, (
        f"outbound MCP request did not "
        f"carry the freshly-generated trace_id; "
        f"outbound={outbound_trace!r}, response={response_trace!r}"
    )


@_PROFILE
@given(inbound=_invalid_inbound_trace_strategy())
def test_property10_invalid_inbound_triggers_fresh_uuidv7_chain(
    inbound: str,
) -> None:
    """Invalid inbound value is discarded and replaced by a fresh UUIDv7.

    When the inbound ``X-Trace-Id`` header is non-empty but malformed
    (e.g. not a UUID), the middleware discards it, generates a fresh
    UUIDv7, and propagates THAT value through every hop.  The
    malformed inbound value MUST NOT appear on any downstream hop.
    """

    response_trace, context_trace, outbound_trace = _run_with_inbound_trace(
        inbound
    )

    # The generated value must be a fresh UUIDv7 - distinct from the
    # malformed inbound by construction.
    assert response_trace != inbound, (
        "TraceMiddleware leaked a malformed inbound trace_id onto "
        f"the response; inbound={inbound!r}, response={response_trace!r}"
    )
    assert _UUIDV7_RE.match(response_trace), (
        "fresh trace_id is not UUIDv7; "
        f"got response={response_trace!r}"
    )
    assert context_trace == response_trace, (
        f"contextvars trace_id ({context_trace!r}) diverged from "
        f"the response trace_id ({response_trace!r}) when the "
        f"inbound value ({inbound!r}) was malformed"
    )
    assert outbound_trace == response_trace, (
        f"outbound MCP request carried a different trace_id than "
        f"the one the middleware installed; "
        f"outbound={outbound_trace!r}, response={response_trace!r}"
    )


# ---------------------------------------------------------------------------
# Concrete regression anchors - pin the three input regimes by example
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "inbound,description",
    [
        (
            "018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34",
            "uuidv7-canonical",
        ),
        (
            "11111111-2222-4333-8444-555555555555",
            "uuidv4-canonical",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) and len(v) < 30 else None,
)
def test_property10_anchor_valid_uuid_propagates(
    inbound: str, description: str
) -> None:
    """Concrete anchor - a known-good UUID flows unchanged through every hop."""

    response_trace, context_trace, outbound_trace = _run_with_inbound_trace(
        inbound
    )

    assert response_trace == inbound, description
    assert context_trace == inbound, description
    assert outbound_trace == inbound, description


def test_property10_anchor_missing_header_triggers_uuidv7() -> None:
    """Concrete anchor - no inbound header  fresh UUIDv7 throughout."""

    response_trace, context_trace, outbound_trace = _run_with_inbound_trace(
        None
    )

    assert _UUIDV7_RE.match(response_trace), response_trace
    assert context_trace == response_trace
    assert outbound_trace == response_trace


def test_property10_anchor_empty_header_triggers_uuidv7() -> None:
    """Concrete anchor - empty inbound header  fresh UUIDv7 throughout.

    The middleware treats ``X-Trace-Id: `` (empty value) the same as
    a missing header - both fall through to the
    :func:`generate_trace_id` branch.
    """

    response_trace, context_trace, outbound_trace = _run_with_inbound_trace(
        ""
    )

    assert _UUIDV7_RE.match(response_trace), response_trace
    assert context_trace == response_trace
    assert outbound_trace == response_trace


def test_property10_anchor_invalid_header_discarded() -> None:
    """Concrete anchor - malformed inbound header is discarded.

    ``"not-a-uuid"`` fails :func:`is_valid_trace_id` so the
    middleware falls through to the :func:`generate_trace_id`
    branch - the response carries a UUIDv7, NOT the malformed input.
    """

    response_trace, context_trace, outbound_trace = _run_with_inbound_trace(
        "not-a-uuid"
    )

    assert response_trace != "not-a-uuid"
    assert _UUIDV7_RE.match(response_trace), response_trace
    assert context_trace == response_trace
    assert outbound_trace == response_trace


# ---------------------------------------------------------------------------
# Negation property - outside the request scope the contextvars slot
# does NOT leak the request's trace_id (per-request
# isolation).
# ---------------------------------------------------------------------------


def test_property10_context_does_not_leak_outside_request() -> None:
    """The trace_id MUST NOT leak past the request lifecycle.

    After the request completes, a fresh observation of
    :func:`observability.get_trace_id` from the test thread MUST NOT
    return the request-scoped trace_id.  This is the negation half
    of the isolation check - without it, two concurrent requests would
    cross-contaminate trace_ids.
    """

    # Reset the context before the request so the assertion below is
    # observing a clean baseline.
    set_trace_id("")

    response_trace, _, _ = _run_with_inbound_trace(
        "018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34"
    )
    assert response_trace == "018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34"

    # Outside the request scope the contextvars slot is back to the
    # default empty string - TraceMiddleware uses
    # :meth:`ContextVar.reset` to undo its set in a ``finally`` block.
    assert get_trace_id() == "", (
        f"TraceMiddleware leaked the request trace_id past the "
        f"request lifecycle; get_trace_id()={get_trace_id()!r}"
    )


# ---------------------------------------------------------------------------
# Outbound MCP propagation - fresh client per request
# ---------------------------------------------------------------------------


def test_property10_outbound_trace_reflects_per_request_value() -> None:
    """A long-lived MCP client emits the *current* request's trace_id, not the construction-time value.

    The trace-id event hook resolves the value per-request via
    :func:`observability.get_trace_id`, not at client construction time.

    Construct a single :func:`make_mcp_client` *outside* any request
    scope, then drive two requests with different inbound trace_ids
    and observe that each outbound MCP call carries the per-request
    trace_id - never the construction-time empty value or the
    previous request's value.
    """

    # Build the long-lived client OUTSIDE any contextvars scope so its
    # trace-id hook starts from the empty default.  The hook reads
    # :func:`get_trace_id` per request, so the construction-time
    # context value should not be observable downstream.
    set_trace_id("")
    captured: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers.get(TRACE_HEADER, ""))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(_handler)
    client = make_mcp_client(
        client_source="automation-service",
        transport=transport,
    )

    async def drive_with_trace(value: str) -> None:
        set_trace_id(value)
        try:
            await client.post("http://atlassian-mcp:8090/mcp", json={})
        finally:
            set_trace_id("")

    try:
        first = "018f7d4d-5f8c-7c4d-92ab-1f6f5a4d9b34"
        second = "018f7d4d-5f8c-7c4d-92ab-aaaaaaaaaaaa"
        asyncio.run(drive_with_trace(first))
        asyncio.run(drive_with_trace(second))
    finally:
        asyncio.run(client.aclose())

    assert captured == [first, second], (
        f"per-request trace_id propagation broken; expected "
        f"[{first!r}, {second!r}], got {captured!r}"
    )
