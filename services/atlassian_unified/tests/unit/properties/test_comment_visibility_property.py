"""Property test P11 — comment visibility validation rejects malformed input before POST.

Validates Requirements 27.1, 27.2, 27.4 / design Property 11:
``_parse_visibility`` and its threading through ``jira_add_comment`` /
``jira_edit_comment`` must reject half-specified ``{type, value}`` pairs
with a structured ``invalid_visibility`` error *before* any POST or PUT
is issued against ``/rest/api/2/issue/{key}/comment[/{id}]``.

The test operates at the server-tool layer (the end-to-end level where
the ``zero POST/PUT on malformed input`` invariant is observable). Both
tools are exercised by calling their underlying ``.fn`` through
``asyncio.run`` with a fake ``fastmcp.Context`` and a patched
``get_jira_fetcher`` that returns a ``MagicMock`` fetcher. The property
is asserted by inspecting the fetcher's ``add_comment`` / ``edit_comment``
call counts — these are the methods that would issue the outbound
POST / PUT, so a zero count is equivalent to "zero outbound HTTP".

Test shape
----------
* **Property A (Hypothesis)** — ``type`` without ``value``: the JSON
  payload has a non-empty ``type`` string and ``value`` that is either
  omitted, ``None``, empty, or whitespace-only. Both ``jira_add_comment``
  and ``jira_edit_comment`` must return
  ``{"success": False, "error_code": "invalid_visibility",
  "details": {"reason": "value_missing", ...}}`` and **zero** POST/PUT
  must be issued.
* **Property B (Hypothesis)** — ``value`` without ``type``: the mirror
  case (non-empty ``value``, missing/blank ``type``). Same assertions
  with ``details.reason == "type_missing"``.
* **Property C (parametrized sanity)** — ``visibility=None`` (omitted):
  no validation error; exactly one call to ``fetcher.add_comment`` is
  issued (the POST on the happy path).
* **Property D (parametrized sanity)** — both ``type`` and ``value``
  present and non-empty with a valid type vocabulary
  (``role`` / ``group``): no validation error; exactly one POST is
  issued.

Style reference: :mod:`tests.unit.properties.test_cql_order_by_property`
(``SimpleNamespace`` ``self``-shim pattern, hypothesis fixture reset,
call-counting HTTP mock).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from mcp_atlassian.servers import jira as jira_server
from mcp_atlassian.servers.jira import add_comment, edit_comment


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Non-empty printable ASCII strings — realistic group / role / type values
# without control chars that would confuse json.dumps round-tripping.
_NON_EMPTY_PRINTABLE: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
    min_size=1,
    max_size=30,
).filter(lambda s: s.strip() != "")

# "Missing / blank" alternatives for the paired field: the key may be
# absent entirely (sentinel None → omitted from dict), explicitly None,
# empty string, or whitespace-only. All four are treated as missing by
# ``_parse_visibility``'s ``_is_present`` helper.
_MISSING_SENTINEL: object = object()

_blank_values: st.SearchStrategy[Any] = st.one_of(
    st.just(_MISSING_SENTINEL),  # key absent from dict
    st.none(),
    st.just(""),
    st.text(alphabet=" \t\n\r", min_size=1, max_size=5),  # whitespace-only
)


def _build_visibility_json(
    type_value: Any, value_value: Any
) -> str:
    """Encode a visibility payload, honoring the _MISSING_SENTINEL marker."""
    payload: dict[str, Any] = {}
    if type_value is not _MISSING_SENTINEL:
        payload["type"] = type_value
    if value_value is not _MISSING_SENTINEL:
        payload["value"] = value_value
    return json.dumps(payload)


# Strategy A: type present, value missing/blank.
type_without_value_strategy: st.SearchStrategy[str] = st.builds(
    _build_visibility_json,
    type_value=_NON_EMPTY_PRINTABLE,
    value_value=_blank_values,
)

# Strategy B: value present, type missing/blank.
value_without_type_strategy: st.SearchStrategy[str] = st.builds(
    _build_visibility_json,
    type_value=_blank_values,
    value_value=_NON_EMPTY_PRINTABLE,
)


# ---------------------------------------------------------------------------
# Fixtures — fake ctx / fetcher shim, monkeypatched get_jira_fetcher
# ---------------------------------------------------------------------------


def _make_fake_ctx() -> SimpleNamespace:
    """Minimal ``fastmcp.Context`` stand-in for ``check_write_access``.

    The decorator reads ``ctx.request_context.lifespan_context`` and calls
    ``.get("app_lifespan_context")`` on it; an empty dict makes the
    read-only check a no-op without triggering the read-only branch.
    """
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={}),
    )


def _make_fetcher_mock() -> MagicMock:
    fetcher = MagicMock(name="jira-fetcher")
    # The tools thread through ``jira.add_comment(...)`` /
    # ``jira.edit_comment(...)``. Return minimal happy-path payloads so
    # the sanity checks (Props C/D) have something to json.dumps over.
    fetcher.add_comment.return_value = {
        "id": "10001",
        "body": "ok",
        "author": "tester",
    }
    fetcher.edit_comment.return_value = {
        "id": "10001",
        "body": "ok",
        "author": "tester",
    }
    return fetcher


@pytest.fixture
def fake_ctx() -> SimpleNamespace:
    return _make_fake_ctx()


@pytest.fixture
def fake_fetcher() -> MagicMock:
    return _make_fetcher_mock()


@pytest.fixture
def patch_get_fetcher(monkeypatch, fake_fetcher: MagicMock) -> MagicMock:
    """Patch ``get_jira_fetcher`` to bypass auth + HTTP bootstrap."""

    async def _aget(_ctx: Any) -> MagicMock:
        return fake_fetcher

    monkeypatch.setattr(jira_server, "get_jira_fetcher", _aget)
    return fake_fetcher


@pytest.fixture
def disable_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ``READ_ONLY_MODE`` is unset so the decorator stays transparent."""
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_add_comment(
    ctx: SimpleNamespace, *, issue_key: str, body: str, visibility: str | None
) -> dict[str, Any]:
    result_json = asyncio.run(
        add_comment.fn(
            ctx,
            issue_key=issue_key,
            body=body,
            visibility=visibility,
        )
    )
    return json.loads(result_json)


def _call_edit_comment(
    ctx: SimpleNamespace,
    *,
    issue_key: str,
    comment_id: str,
    body: str,
    visibility: str | None,
) -> dict[str, Any]:
    result_json = asyncio.run(
        edit_comment.fn(
            ctx,
            issue_key=issue_key,
            comment_id=comment_id,
            body=body,
            visibility=visibility,
        )
    )
    return json.loads(result_json)


# ---------------------------------------------------------------------------
# Property A — ``type`` without ``value`` ⇒ invalid_visibility, zero POST/PUT
# ---------------------------------------------------------------------------


@given(visibility_json=type_without_value_strategy)
def test_type_without_value_rejected_before_any_post(
    visibility_json: str,
    fake_ctx: SimpleNamespace,
    fake_fetcher: MagicMock,
    patch_get_fetcher: MagicMock,
    disable_read_only: None,
) -> None:
    """P11.A: ``{type: <str>, value: missing/blank}`` ⇒ invalid_visibility."""
    # Reset between Hypothesis examples so the "zero calls" assertion
    # scopes to this example, not cumulative across the run.
    fake_fetcher.reset_mock()
    fake_fetcher.add_comment.return_value = {"id": "1", "body": "x"}
    fake_fetcher.edit_comment.return_value = {"id": "1", "body": "x"}

    # jira_add_comment — the POST path.
    add_payload = _call_add_comment(
        fake_ctx,
        issue_key="PROJ-1",
        body="test body",
        visibility=visibility_json,
    )
    assert add_payload["success"] is False
    assert add_payload["error_code"] == "invalid_visibility"
    assert add_payload["details"]["reason"] == "value_missing"
    assert add_payload["details"]["field"] == "visibility"
    # The critical safety property: no POST reached the fetcher.
    assert fake_fetcher.add_comment.call_count == 0, (
        f"expected zero POSTs for malformed visibility {visibility_json!r}, "
        f"got {fake_fetcher.add_comment.call_count}"
    )

    # jira_edit_comment — the PUT path. Same invariant.
    edit_payload = _call_edit_comment(
        fake_ctx,
        issue_key="PROJ-1",
        comment_id="10001",
        body="updated body",
        visibility=visibility_json,
    )
    assert edit_payload["success"] is False
    assert edit_payload["error_code"] == "invalid_visibility"
    assert edit_payload["details"]["reason"] == "value_missing"
    assert fake_fetcher.edit_comment.call_count == 0, (
        f"expected zero PUTs for malformed visibility {visibility_json!r}, "
        f"got {fake_fetcher.edit_comment.call_count}"
    )


# ---------------------------------------------------------------------------
# Property B — ``value`` without ``type`` ⇒ invalid_visibility, zero POST/PUT
# ---------------------------------------------------------------------------


@given(visibility_json=value_without_type_strategy)
def test_value_without_type_rejected_before_any_post(
    visibility_json: str,
    fake_ctx: SimpleNamespace,
    fake_fetcher: MagicMock,
    patch_get_fetcher: MagicMock,
    disable_read_only: None,
) -> None:
    """P11.B: ``{value: <str>, type: missing/blank}`` ⇒ invalid_visibility."""
    fake_fetcher.reset_mock()
    fake_fetcher.add_comment.return_value = {"id": "1", "body": "x"}
    fake_fetcher.edit_comment.return_value = {"id": "1", "body": "x"}

    add_payload = _call_add_comment(
        fake_ctx,
        issue_key="PROJ-1",
        body="test body",
        visibility=visibility_json,
    )
    assert add_payload["success"] is False
    assert add_payload["error_code"] == "invalid_visibility"
    assert add_payload["details"]["reason"] == "type_missing"
    assert add_payload["details"]["field"] == "visibility"
    assert fake_fetcher.add_comment.call_count == 0, (
        f"expected zero POSTs for malformed visibility {visibility_json!r}, "
        f"got {fake_fetcher.add_comment.call_count}"
    )

    edit_payload = _call_edit_comment(
        fake_ctx,
        issue_key="PROJ-1",
        comment_id="10001",
        body="updated body",
        visibility=visibility_json,
    )
    assert edit_payload["success"] is False
    assert edit_payload["error_code"] == "invalid_visibility"
    assert edit_payload["details"]["reason"] == "type_missing"
    assert fake_fetcher.edit_comment.call_count == 0, (
        f"expected zero PUTs for malformed visibility {visibility_json!r}, "
        f"got {fake_fetcher.edit_comment.call_count}"
    )


# ---------------------------------------------------------------------------
# Property C (parametrized sanity) — omitted visibility ⇒ exactly one POST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "issue_key,body",
    [
        ("PROJ-1", "public comment"),
        ("ACV2-642", "another comment"),
    ],
)
def test_omitted_visibility_issues_exactly_one_post(
    issue_key: str,
    body: str,
    fake_ctx: SimpleNamespace,
    fake_fetcher: MagicMock,
    patch_get_fetcher: MagicMock,
    disable_read_only: None,
) -> None:
    """P11.C: ``visibility=None`` (public comment, Req 27.3) ⇒ one POST."""
    fake_fetcher.reset_mock()
    fake_fetcher.add_comment.return_value = {
        "id": "10001",
        "body": body,
        "author": "tester",
    }

    payload = _call_add_comment(
        fake_ctx,
        issue_key=issue_key,
        body=body,
        visibility=None,
    )

    # The tool returns the raw fetcher payload (not wrapped in success=...).
    assert payload["id"] == "10001"
    assert payload["body"] == body

    # Exactly one POST — the visibility validator is transparent for None.
    assert fake_fetcher.add_comment.call_count == 1
    fake_fetcher.add_comment.assert_called_once_with(
        issue_key, body, None, public=None
    )


# ---------------------------------------------------------------------------
# Property D (parametrized sanity) — well-formed pair ⇒ exactly one POST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vis_type,vis_value",
    [
        ("group", "jira-users"),
        ("role", "Administrators"),
        ("group", "developers"),
    ],
)
def test_well_formed_visibility_issues_exactly_one_post(
    vis_type: str,
    vis_value: str,
    fake_ctx: SimpleNamespace,
    fake_fetcher: MagicMock,
    patch_get_fetcher: MagicMock,
    disable_read_only: None,
) -> None:
    """P11.D: ``{type, value}`` both present + valid type vocab ⇒ one POST."""
    fake_fetcher.reset_mock()
    fake_fetcher.add_comment.return_value = {
        "id": "10001",
        "body": "restricted",
        "author": "tester",
    }

    visibility_json = json.dumps({"type": vis_type, "value": vis_value})
    payload = _call_add_comment(
        fake_ctx,
        issue_key="PROJ-1",
        body="restricted",
        visibility=visibility_json,
    )

    assert payload["id"] == "10001"

    # Exactly one POST; the helper surfaced the dict as-is to the fetcher.
    assert fake_fetcher.add_comment.call_count == 1
    fake_fetcher.add_comment.assert_called_once_with(
        "PROJ-1",
        "restricted",
        {"type": vis_type, "value": vis_value},
        public=None,
    )
