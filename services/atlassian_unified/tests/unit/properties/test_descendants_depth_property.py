"""Property test P15 — Confluence descendants-tree depth is capped at 10.

Validates Requirement 40.1 / 40.2 / 40.3 and design Property 15:
:meth:`DescendantsMixin.get_page_descendants` silently clamps any
caller-supplied depth into the supported ``[1, 10]`` range before
issuing ``GET /rest/api/content/{page_id}/descendant/page``, and the
server tool ``confluence_get_page_descendants`` exposes a
``capped_depth`` marker in the response envelope *only* when the
caller's requested depth exceeded the cap (Requirement 40.3).

Endpoint / field facts discovered from
``src/mcp_atlassian/confluence/descendants.py`` and
``src/mcp_atlassian/servers/confluence.py`` at the time this test was
written:

* **HTTP endpoint:** ``rest/api/content/{page_id}/descendant/page``
* **Query param carrying the effective depth:** ``depth`` (alongside
  ``limit=25``). The mixin clamps to ``[MIN_DESCENDANTS_DEPTH=1,
  MAX_DESCENDANTS_DEPTH=10]`` so the value forwarded on the wire is
  always in ``[1, 10]``.
* **Response field for the cap marker:** ``capped_depth`` (value:
  ``10``). The marker lives on the server-tool response envelope, not
  on the mixin's raw DC payload — the mixin is intentionally transparent
  and the server tool compares the *caller-supplied* depth against
  ``MAX_DESCENDANTS_DEPTH`` to decide whether to attach the marker.
  When the requested depth is within the cap the server tool omits
  the key entirely (it is not emitted as ``False`` or ``None``).

Test shape
----------
Every property drives the async server tool directly via its
``.fn(...)`` handle (same pattern used elsewhere in the suite, e.g.
``tests/unit/jira/test_votes.py``) while patching
``get_confluence_fetcher`` so the tool delegates into the *real*
:class:`DescendantsMixin` through a tiny SimpleNamespace shim bound to
``mock_requests_session`` — mirroring the shim convention in
``tests/unit/properties/test_cql_order_by_property.py``. This lets a
single test observe both sides of the contract: the HTTP call args
landing on the mock session *and* the JSON envelope returned by the
server tool.

* **Property A (depth is always clamped on the wire).** For any
  ``depth`` in ``[1, 100]``, the ``depth`` parameter forwarded to
  ``self.confluence.get`` is in ``[1, 10]``. Exactly one GET fires per
  invocation.
* **Property B (capped_depth is present when requested > 10).** For
  any ``depth > 10``, the server-tool response envelope contains
  ``capped_depth == 10`` and ``depth == 10`` (the applied depth).
* **Property C (capped_depth is absent or false when requested <= 10).**
  For any ``depth`` in ``[1, 10]``, the server-tool response envelope
  either omits ``capped_depth`` entirely or carries a falsy value, and
  ``depth`` matches the caller-supplied value.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcp_atlassian.confluence.descendants import (
    MAX_DESCENDANTS_DEPTH,
    MIN_DESCENDANTS_DEPTH,
    DescendantsMixin,
)
from mcp_atlassian.servers import confluence as confluence_server
from mcp_atlassian.servers.confluence import get_page_descendants


# ---------------------------------------------------------------------------
# Hypothesis strategy — caller-supplied depths covering both sides of the cap
# ---------------------------------------------------------------------------
#
# Per the task brief: ``st.integers(min_value=1, max_value=100)``. This
# straddles the cap (values 1..10 exercise the uncapped branch, 11..100
# exercise the capped branch) and keeps the cardinality bounded so the
# default Hypothesis budget exhausts both sides.
depth_strategy: st.SearchStrategy[int] = st.integers(min_value=1, max_value=100)


# ---------------------------------------------------------------------------
# Shim helpers — wire the real mixin through a mocked HTTP surface
# ---------------------------------------------------------------------------


def _build_fake_fetcher(session_mock) -> SimpleNamespace:
    """Return a fake ``ConfluenceFetcher`` that delegates to the real mixin.

    The server tool expects a fetcher with a ``get_page_descendants``
    bound method; we satisfy that by building a ``SimpleNamespace`` whose
    method forwards into :meth:`DescendantsMixin.get_page_descendants`
    with a ``SimpleNamespace(confluence=SimpleNamespace(get=...))`` self
    shim — the same pattern used by ``test_cql_order_by_property``. The
    result is that the *real* clamp logic runs and the call lands on
    ``session_mock.get`` so the test can introspect its arguments.
    """

    def _get_page_descendants(page_id: str, *, depth: int = 3) -> dict[str, Any]:
        shim = SimpleNamespace(
            confluence=SimpleNamespace(get=session_mock.get)
        )
        return DescendantsMixin.get_page_descendants(
            shim, page_id, depth=depth
        )

    # ``config`` is not consulted by this read tool (no space filter per
    # Req 43 for descendants), but stub it defensively so any future
    # refactor that reads ``fetcher.config`` still has something to bind.
    return SimpleNamespace(
        get_page_descendants=_get_page_descendants,
        config=SimpleNamespace(spaces_filter=None),
    )


def _invoke_tool(session_mock, monkeypatch, *, depth: int) -> dict[str, Any]:
    """Invoke the server tool ``.fn(...)`` and return the decoded envelope.

    Patches ``get_confluence_fetcher`` on the server module so the tool
    resolves to the fake fetcher built from ``session_mock``. The mocked
    ``session.get`` returns ``{}`` (preseeded by the conftest) which the
    mixin normalises to an empty dict — the envelope's ``tree`` field
    therefore carries an empty mapping in every example.
    """
    fake_fetcher = _build_fake_fetcher(session_mock)

    async def _aget(_ctx: Any) -> SimpleNamespace:
        return fake_fetcher

    monkeypatch.setattr(confluence_server, "get_confluence_fetcher", _aget)

    # Disable read-only mode precheck noise: this is a read tool so the
    # guard returns ``None`` regardless, but clear the env var defensively.
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)

    fake_ctx: Any = SimpleNamespace()
    result_json = asyncio.run(
        get_page_descendants.fn(fake_ctx, page_id="123456", depth=depth)
    )
    return json.loads(result_json)


def _extract_wire_depth(session_mock) -> int:
    """Pull the ``depth`` param out of the most recent ``session.get`` call."""
    assert session_mock.get.call_count == 1, (
        f"expected exactly one GET, got {session_mock.get.call_count}"
    )
    _args, kwargs = session_mock.get.call_args
    params = kwargs.get("params")
    assert isinstance(params, dict), f"expected dict params, got {params!r}"
    assert "depth" in params, f"depth missing from params: {params!r}"
    return int(params["depth"])


# ---------------------------------------------------------------------------
# Property A — the depth forwarded to HTTP is always within [1, 10]
# ---------------------------------------------------------------------------


@given(depth=depth_strategy)
def test_wire_depth_is_always_within_cap(
    mock_requests_session, monkeypatch, depth: int
) -> None:
    """P15.A: ``depth`` query param sent upstream stays inside ``[1, 10]``."""
    # Reset between Hypothesis examples so the "exactly one GET" and
    # ``call_args`` assertions describe *this* example rather than being
    # cumulative across the run.
    mock_requests_session.reset_mock()
    # The mixin returns the raw dict from ``self.confluence.get``; the
    # conftest preseeds ``return_value`` with a ``MagicMock`` response
    # shape that does not round-trip as a dict, so override to an empty
    # dict here — the mixin normalises non-dicts to ``{}`` anyway, but
    # a literal dict keeps the envelope's ``tree`` field clean.
    mock_requests_session.get.return_value = {}

    _invoke_tool(mock_requests_session, monkeypatch, depth=depth)

    wire_depth = _extract_wire_depth(mock_requests_session)
    assert MIN_DESCENDANTS_DEPTH <= wire_depth <= MAX_DESCENDANTS_DEPTH, (
        f"depth forwarded to HTTP ({wire_depth}) is outside "
        f"[{MIN_DESCENDANTS_DEPTH}, {MAX_DESCENDANTS_DEPTH}] "
        f"for caller-supplied depth={depth}"
    )


# ---------------------------------------------------------------------------
# Property B — capped_depth marker is emitted when caller exceeds the cap
# ---------------------------------------------------------------------------


@given(depth=st.integers(min_value=MAX_DESCENDANTS_DEPTH + 1, max_value=100))
def test_capped_depth_present_when_requested_exceeds_cap(
    mock_requests_session, monkeypatch, depth: int
) -> None:
    """P15.B: envelope carries ``capped_depth == 10`` when requested > 10.

    The exact response field name is ``capped_depth`` (not
    ``requested_depth`` / ``effective_depth``) — documented at the top
    of this module and cross-checked against
    ``src/mcp_atlassian/servers/confluence.py`` where the key is set on
    the tool's return payload whenever ``depth > MAX_DESCENDANTS_DEPTH``.
    """
    mock_requests_session.reset_mock()
    mock_requests_session.get.return_value = {}

    envelope = _invoke_tool(mock_requests_session, monkeypatch, depth=depth)

    assert envelope.get("success") is True, (
        f"expected successful envelope, got {envelope!r}"
    )
    assert "capped_depth" in envelope, (
        f"expected 'capped_depth' marker for depth={depth}, "
        f"envelope keys={sorted(envelope)!r}"
    )
    assert envelope["capped_depth"] == MAX_DESCENDANTS_DEPTH, (
        f"expected capped_depth={MAX_DESCENDANTS_DEPTH}, "
        f"got {envelope['capped_depth']!r}"
    )
    # The ``depth`` field on the envelope reports the *applied* (capped)
    # depth, which must equal the cap in this branch.
    assert envelope.get("depth") == MAX_DESCENDANTS_DEPTH, (
        f"expected applied depth={MAX_DESCENDANTS_DEPTH}, "
        f"got {envelope.get('depth')!r}"
    )


# ---------------------------------------------------------------------------
# Property C — capped_depth marker is absent when requested is within cap
# ---------------------------------------------------------------------------


@given(
    depth=st.integers(
        min_value=MIN_DESCENDANTS_DEPTH, max_value=MAX_DESCENDANTS_DEPTH
    )
)
def test_capped_depth_absent_when_requested_within_cap(
    mock_requests_session, monkeypatch, depth: int
) -> None:
    """P15.C: envelope omits (or carries falsy) ``capped_depth`` when <= 10.

    The server tool deliberately does not emit a ``capped_depth`` key at
    all in this branch; the assertion accepts either an outright
    omission or an explicit falsy value to stay robust against a future
    refactor that might prefer ``capped_depth: False`` as a sentinel.
    """
    mock_requests_session.reset_mock()
    mock_requests_session.get.return_value = {}

    envelope = _invoke_tool(mock_requests_session, monkeypatch, depth=depth)

    assert envelope.get("success") is True, (
        f"expected successful envelope, got {envelope!r}"
    )
    # Accept both shapes: key missing, OR key present but falsy.
    if "capped_depth" in envelope:
        assert not envelope["capped_depth"], (
            f"expected falsy capped_depth for depth={depth}, "
            f"got {envelope['capped_depth']!r}"
        )
    # The applied depth echoed back on the envelope must match the
    # caller's request because no cap was applied.
    assert envelope.get("depth") == depth, (
        f"expected applied depth={depth}, got {envelope.get('depth')!r}"
    )
