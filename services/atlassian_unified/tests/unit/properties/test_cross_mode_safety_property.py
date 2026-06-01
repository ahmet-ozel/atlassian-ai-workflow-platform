"""Property test P12 — Cross-mode safety guards apply uniformly.

Validates Requirements 16.1, 16.2, 16.3, 16.4 of the
``bitbucket-cloud-dc-parity`` spec / design Property 12:

    For every Bitbucket tool invocation, the three cross-cutting safety
    guards — ``READ_ONLY_MODE``, ``BITBUCKET_PROJECTS_FILTER``, and
    webhook-secret redaction — behave identically in DC mode and Cloud
    mode. Switching to Cloud does not relax any safety posture.

The property is broken into three sub-properties, each exercised across
the ``is_cloud ∈ {False, True}`` axis:

* **P12.A (Req 16.1)** — with ``READ_ONLY_MODE=true``, every Bitbucket
  write tool returns ``error_code="read_only_mode"`` with zero outbound
  HTTP in BOTH modes. Parametrized over a curated list of Bitbucket
  write tools.
* **P12.B (Req 16.2, 16.3)** — with ``BITBUCKET_PROJECTS_FILTER`` set to
  an allow-list not containing the supplied ``project_key``, every
  project-scoped Bitbucket tool returns ``error_code="filtered_out"``
  with zero outbound HTTP in BOTH modes. Hypothesis generates random
  project keys both outside the filter (negative path) and inside the
  filter (positive sanity path) so we know the guard is not an
  always-deny no-op. Req 16.3 is implicitly covered: the filter is
  mode-agnostic, so a Cloud-mode rejection proves Cloud treats filter
  entries the same way DC does (as opaque slugs).
* **P12.C (Req 16.4)** — for arbitrary Hypothesis-generated ``secret``
  strings, ``bitbucket_create_webhook`` in BOTH modes scrubs the secret
  from the returned JSON. The server-layer ``redact_secrets()`` is
  mode-agnostic, so the test verifies symmetry by running the same
  property against a DC fetcher (``is_cloud=False``) and a Cloud fetcher
  (``is_cloud=True``).

Style reference
---------------

Shaped after :mod:`tests.unit.properties.test_read_only_property` for
the write-tool fixture, :mod:`tests.unit.properties.test_filter_property`
for the filter fixture, and
:mod:`tests.unit.properties.test_secret_hygiene_property` for the
secret-echo invariant. The symmetry-across-modes axis is what makes
this property distinct from those predecessors.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import string
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mcp_atlassian.utils.secret_redaction import REDACTED_PLACEHOLDER

# ---------------------------------------------------------------------------
# Shared fixtures — fake FastMCP Context + Bitbucket fetcher shim
# ---------------------------------------------------------------------------


def _make_fake_ctx() -> SimpleNamespace:
    """Minimal :class:`fastmcp.Context` stand-in.

    The ``@check_write_access`` decorator (used by a handful of
    long-task tools) reads ``ctx.request_context.lifespan_context`` and
    calls ``.get`` on it; an empty dict short-circuits that decorator
    transparently so the inner safety guards handle the gate uniformly.
    """
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={}),
    )


def _make_fetcher_mock(
    *,
    is_cloud: bool,
    projects_filter: str | None = None,
    workspace: str | None = None,
) -> MagicMock:
    """Build a ``MagicMock`` fetcher wired for the chosen mode.

    Config fields exposed on the fetcher mirror the exact subset of
    :class:`BitbucketConfig` that guard code and tool bodies read:

    * ``is_cloud`` — boolean driving the cross-mode routing.
    * ``projects_filter`` — ``None`` disables the filter guard;
      non-empty string activates it.
    * ``workspace`` — populated on Cloud so Cloud branches can resolve
      the workspace without raising.
    * ``username`` — populated so any downstream owner-scoped logic
      (none of the tools exercised here invoke one) has a plausible
      value.

    The :class:`MagicMock` ``bitbucket`` attribute stands in for the
    ``atlassian.Bitbucket`` session. Fresh ``get``/``post``/``put``/
    ``delete`` mocks start at ``call_count == 0`` so zero-HTTP
    assertions are exact.

    ``get_dc_version`` / ``_dc_version`` report a modern DC release so
    that any downstream ``check_dc_version`` guard passes (those guards
    are mode-aware via ``is_cloud`` upstream, but keeping the version
    high avoids cross-talk with the safety-guard assertions under test).
    """
    fetcher = MagicMock(name="bitbucket-fetcher")
    fetcher.is_cloud = is_cloud
    fetcher.config = SimpleNamespace(
        is_cloud=is_cloud,
        url=(
            "https://api.bitbucket.org" if is_cloud else "https://stash.corp.local"
        ),
        workspace=workspace if is_cloud else None,
        projects_filter=projects_filter,
        spaces_filter=None,
        username="tester",
        ssl_verify=True,
    )
    fetcher.bitbucket = MagicMock(name="atlassian.Bitbucket")
    fetcher.bitbucket.get = MagicMock(return_value={})
    fetcher.bitbucket.post = MagicMock(return_value={})
    fetcher.bitbucket.put = MagicMock(return_value={})
    fetcher.bitbucket.delete = MagicMock(return_value=None)
    # Modern DC so ``check_dc_version`` is a no-op where it runs.
    fetcher.get_dc_version = MagicMock(return_value="9.4.0")
    fetcher._dc_version = "9.4.0"
    return fetcher


def _install_fetcher(monkeypatch: pytest.MonkeyPatch, fetcher: MagicMock) -> None:
    """Patch ``get_bitbucket_fetcher`` so tool calls return ``fetcher``."""
    from mcp_atlassian.servers import bitbucket as bb_server

    async def _aget(_ctx: Any) -> MagicMock:
        return fetcher

    monkeypatch.setattr(bb_server, "get_bitbucket_fetcher", _aget)


def _assert_zero_http(fetcher: MagicMock, *, label: str) -> None:
    """Assert no outbound HTTP was issued on the fetcher's session."""
    assert fetcher.bitbucket.get.call_count == 0, (
        f"{label}: leaked {fetcher.bitbucket.get.call_count} GET(s)"
    )
    assert fetcher.bitbucket.post.call_count == 0, (
        f"{label}: leaked {fetcher.bitbucket.post.call_count} POST(s)"
    )
    assert fetcher.bitbucket.put.call_count == 0, (
        f"{label}: leaked {fetcher.bitbucket.put.call_count} PUT(s)"
    )
    assert fetcher.bitbucket.delete.call_count == 0, (
        f"{label}: leaked {fetcher.bitbucket.delete.call_count} DELETE(s)"
    )
    # Guard-denial must land before any mixin dispatch too — we inspect
    # the aggregate ``method_calls`` ledger because individual mixin
    # names vary across tools.
    assert fetcher.method_calls == [], (
        f"{label}: unexpected mixin dispatch — {fetcher.method_calls!r}"
    )


# ===========================================================================
# Sub-property P12.A — READ_ONLY_MODE blocks writes in BOTH modes (Req 16.1)
# ===========================================================================
#
# A curated registry of Bitbucket write tools — each entry is (attr,
# kwargs). These are the tools whose response to a read-only invocation
# must be ``error_code="read_only_mode"`` with zero outbound HTTP,
# regardless of whether the fetcher reports ``is_cloud=True`` or
# ``is_cloud=False``. Tools that are DC-only (e.g. create_project,
# fork_repository) are NOT included here because their CloudMode
# response is ``not_supported_on_cloud`` (task 17), not
# ``read_only_mode`` — those are covered by P9 (task 21.7). This
# registry contains only write tools that are supported in BOTH modes
# (i.e. their mixin has a Cloud branch per tasks 7–15).


_WRITE_TOOLS: list[tuple[str, dict[str, Any]]] = [
    # bitbucket_webhooks — create / update / delete (Cloud branches in task 14).
    (
        "create_webhook",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "name": "hook",
            "url": "https://ci.example.com/hook",
            "events": '["repo:refs_changed"]',
        },
    ),
    (
        "update_webhook",
        {"project_key": "PROJ", "repo_slug": "repo", "webhook_id": 1},
    ),
    (
        "delete_webhook",
        {"project_key": "PROJ", "repo_slug": "repo", "webhook_id": 1},
    ),
    # bitbucket_commits — commit-comment CRUD (Cloud branches in task 11).
    (
        "add_commit_comment",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "commit_id": "abc123",
            "text": "hello",
        },
    ),
    (
        "update_commit_comment",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "commit_id": "abc123",
            "comment_id": 1,
            "text": "updated",
            "version": 0,
        },
    ),
    (
        "delete_commit_comment",
        {
            "project_key": "PROJ",
            "repo_slug": "repo",
            "commit_id": "abc123",
            "comment_id": 1,
            "version": 0,
        },
    ),
    # bitbucket_repositories — watch / unwatch (Cloud branches in task 15).
    (
        "watch_repository",
        {"project_key": "PROJ", "repo_slug": "repo"},
    ),
    (
        "unwatch_repository",
        {"project_key": "PROJ", "repo_slug": "repo"},
    ),
    # bitbucket_pull_requests — watch / unwatch PR (Cloud branches in task 15).
    (
        "watch_pull_request",
        {"project_key": "PROJ", "repo_slug": "repo", "pr_id": 1},
    ),
    (
        "unwatch_pull_request",
        {"project_key": "PROJ", "repo_slug": "repo", "pr_id": 1},
    ),
]


def _write_tool_ids() -> list[str]:
    """Stable parametrisation ids of the write-tool registry."""
    return [f"bitbucket_{attr}" for attr, _ in _WRITE_TOOLS]


@pytest.mark.parametrize("is_cloud", [False, True], ids=["dc", "cloud"])
@pytest.mark.parametrize(
    "entry",
    _WRITE_TOOLS,
    ids=_write_tool_ids(),
)
def test_read_only_mode_blocks_write_tool_in_both_modes(
    entry: tuple[str, dict[str, Any]],
    is_cloud: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P12.A: ``READ_ONLY_MODE=true`` blocks every write tool, in both modes.

    The registered write tool returns ``success=False`` with
    ``error_code="read_only_mode"`` and the mocked fetcher records zero
    outbound HTTP calls. The same invariant is checked with
    ``is_cloud=False`` (DC) and ``is_cloud=True`` (Cloud) — Req 16.1's
    "regardless of CloudMode or DCMode".

    Validates: Requirement 16.1.
    """
    attr, kwargs = entry
    bb_server = importlib.import_module("mcp_atlassian.servers.bitbucket")
    tool = getattr(bb_server, attr)

    # Verify the tool actually carries the ``write`` tag — if a
    # curated entry was accidentally mistyped to a read tool, the
    # ``read_only_mode`` assertion below would fail vacuously.
    assert "write" in getattr(tool, "tags", set()), (
        f"{attr} must carry 'write' tag to participate in the P12.A "
        f"registry; got {getattr(tool, 'tags', None)!r}"
    )

    # 1. Activate read-only mode. The guard reads ``READ_ONLY_MODE`` on
    # every call (no caching).
    monkeypatch.setenv("READ_ONLY_MODE", "true")
    # Ensure no stray filter bleeds in from developer env.
    monkeypatch.delenv("BITBUCKET_PROJECTS_FILTER", raising=False)

    # 2. Install a mode-appropriate fetcher.
    fetcher = _make_fetcher_mock(
        is_cloud=is_cloud,
        workspace=("my-team" if is_cloud else None),
    )
    _install_fetcher(monkeypatch, fetcher)

    # 3. Invoke the tool.
    ctx = _make_fake_ctx()
    result_json = asyncio.run(tool.fn(ctx, **kwargs))
    payload = json.loads(result_json)

    # Structured-error contract (Req 16.1).
    mode_label = "cloud" if is_cloud else "dc"
    assert payload.get("success") is False, (
        f"{attr} [{mode_label}]: expected success=False under "
        f"READ_ONLY_MODE=true, got {payload!r}"
    )
    assert payload.get("error_code") == "read_only_mode", (
        f"{attr} [{mode_label}]: expected error_code='read_only_mode', "
        f"got {payload.get('error_code')!r}; payload={payload!r}"
    )

    # Zero-HTTP contract.
    _assert_zero_http(fetcher, label=f"{attr} [{mode_label}]")


# ===========================================================================
# Sub-property P12.B — PROJECTS_FILTER blocks out-of-scope in BOTH modes
# (Req 16.2, 16.3)
# ===========================================================================
#
# The project-filter guard is mode-agnostic by construction — it reads
# ``fetcher.config.projects_filter`` and uppercases both sides before
# comparing. Req 16.3 says Cloud interprets entries as workspace slugs,
# but the matching algorithm is identical. We prove Req 16.2/16.3 with
# Hypothesis-generated project keys that are deliberately drawn OUTSIDE
# the filter allow-list; the guard must short-circuit in both modes.

# Project/workspace-slug alphabet: uppercase ASCII letters + digits.
# Matches both DC project-key conventions (``PROJ``, ``TEAM42``) and
# the shape of Cloud workspace slugs before they are lowercased. The
# filter uppercases on both sides, so case is immaterial to the
# correctness of the assertion.
_KEY_ALPHABET = string.ascii_uppercase + string.digits


# Hypothesis strategy for non-empty filter allow-list tokens. Each
# token is a short identifier; the filter env value is a comma-
# separated list of 1–4 tokens.
_filter_tokens: st.SearchStrategy[str] = st.text(
    alphabet=_KEY_ALPHABET, min_size=2, max_size=8
)


@st.composite
def _filter_setups(
    draw: st.DrawFn,
) -> tuple[str, list[str], str]:
    """Draw a ``(filter_env_value, allow_list, out_of_scope_key)`` triple.

    The ``allow_list`` is 1–4 distinct tokens that form
    ``filter_env_value`` (comma-joined). The ``out_of_scope_key`` is a
    freshly drawn token that is **not** a case-insensitive member of
    the allow-list, so the filter guard is guaranteed to reject it.
    """
    tokens = draw(
        st.lists(_filter_tokens, min_size=1, max_size=4, unique_by=lambda s: s.upper())
    )
    allow_list = [t.upper() for t in tokens]
    filter_env = ",".join(tokens)
    # Draw an out-of-scope key that differs case-insensitively from
    # every allow-list entry. Using ``filter()`` keeps Hypothesis'
    # shrinker happy (no rejection sampling inside the composite).
    out_of_scope = draw(
        _filter_tokens.filter(lambda k: k.upper() not in set(allow_list))
    )
    return filter_env, allow_list, out_of_scope


@given(fixture=_filter_setups(), is_cloud=st.booleans())
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_projects_filter_blocks_out_of_scope_in_both_modes(
    fixture: tuple[str, list[str], str],
    is_cloud: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P12.B: the filter blocks out-of-scope ``project_key`` in both modes.

    For every Hypothesis-drawn ``(filter_env, out_of_scope_key)``, a
    project-scoped Bitbucket tool returns ``error_code="filtered_out"``
    with zero outbound HTTP in both DCMode and CloudMode. The choice of
    ``bitbucket_list_webhooks`` as the representative tool is
    deliberate: it runs the canonical
    ``check_read_only → check_project_filter → check_dc_version``
    prelude and has a Cloud branch (task 14), so the test exercises the
    full mode-agnostic filter path end-to-end.

    Validates: Requirements 16.2, 16.3.
    """
    filter_env, allow_list, out_of_scope = fixture

    bb_server = importlib.import_module("mcp_atlassian.servers.bitbucket")
    tool = bb_server.list_webhooks

    # Read-only mode is irrelevant for this property; clear any stray
    # developer-machine value so ``check_read_only`` is a no-op and the
    # filter gate fires on its own.
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)
    # Set the env var too, matching how operators configure the filter
    # (the guard itself reads ``fetcher.config.projects_filter``).
    monkeypatch.setenv("BITBUCKET_PROJECTS_FILTER", filter_env)

    fetcher = _make_fetcher_mock(
        is_cloud=is_cloud,
        projects_filter=filter_env,
        workspace=("my-team" if is_cloud else None),
    )
    _install_fetcher(monkeypatch, fetcher)

    ctx = _make_fake_ctx()
    result_json = asyncio.run(
        tool.fn(ctx, project_key=out_of_scope, repo_slug="repo")
    )
    payload = json.loads(result_json)

    mode_label = "cloud" if is_cloud else "dc"
    assert payload.get("success") is False, (
        f"[{mode_label}] filter={filter_env!r} out_of_scope={out_of_scope!r}: "
        f"expected success=False, got {payload!r}"
    )
    assert payload.get("error_code") == "filtered_out", (
        f"[{mode_label}] filter={filter_env!r} out_of_scope={out_of_scope!r}: "
        f"expected error_code='filtered_out', got {payload.get('error_code')!r}"
    )
    # Details carry the allow-list so operators can compare; pin them
    # to guard against a future refactor that drops the field.
    details = payload.get("details") or {}
    assert set(details.get("allowed") or []) == set(allow_list), (
        f"[{mode_label}] allowed set mismatch: "
        f"details={details!r}, expected={sorted(allow_list)!r}"
    )

    _assert_zero_http(fetcher, label=f"list_webhooks [{mode_label}]")


# Small positive-path sanity: when the supplied key IS in scope, the
# filter does NOT reject — proves the guard is not an always-deny no-op
# that would make the negative property pass vacuously. Parametrised
# over both modes for symmetry.


@pytest.mark.parametrize("is_cloud", [False, True], ids=["dc", "cloud"])
def test_projects_filter_allows_in_scope_key_in_both_modes(
    is_cloud: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive counterpart of P12.B: in-scope keys are NOT blocked.

    Shields the parametric P12.B property from a future regression
    where the filter guard flips to always-deny. In that world the
    negative test would pass vacuously; this positive case catches it.
    """
    bb_server = importlib.import_module("mcp_atlassian.servers.bitbucket")
    tool = bb_server.list_webhooks

    monkeypatch.delenv("READ_ONLY_MODE", raising=False)
    monkeypatch.setenv("BITBUCKET_PROJECTS_FILTER", "PROJ")

    fetcher = _make_fetcher_mock(
        is_cloud=is_cloud,
        projects_filter="PROJ",
        workspace=("my-team" if is_cloud else None),
    )
    # Stub the mixin so the happy path does not crash when the filter
    # allows the call through.
    fetcher.list_webhooks = MagicMock(return_value=[])
    _install_fetcher(monkeypatch, fetcher)

    ctx = _make_fake_ctx()
    result_json = asyncio.run(
        tool.fn(ctx, project_key="PROJ", repo_slug="repo")
    )
    payload = json.loads(result_json)

    mode_label = "cloud" if is_cloud else "dc"
    assert payload.get("error_code") != "filtered_out", (
        f"[{mode_label}] in-scope key incorrectly rejected; payload={payload!r}"
    )
    # ... and the mixin method was dispatched (proving the guard is
    # not always-deny).
    assert fetcher.list_webhooks.called, (
        f"[{mode_label}] expected list_webhooks mixin dispatch, "
        f"got method_calls={fetcher.method_calls!r}"
    )


# ===========================================================================
# Sub-property P12.C — webhook secret is redacted and never echoed
# (Req 16.4)
# ===========================================================================
#
# ``bitbucket_create_webhook`` forwards ``secret`` to Bitbucket in the
# outbound request body and Bitbucket echoes it back in the response
# (DC embeds it under ``configuration.secret``, Cloud exposes it at the
# top level). Either way, the server-layer ``redact_secrets()`` helper
# must strip it before the JSON returns to the agent — and the
# caller-supplied string must NEVER appear anywhere in the serialised
# response.
#
# Hypothesis draws a random secret with a distinctive marker prefix so
# the non-echo assertion cannot collide with structural JSON content
# (field names, URL scheme, etc.).


# Secret strategy — ``s3cr3t_`` prefix + hex suffix gives unambiguous
# substring search semantics.
_SECRET_TEXT: st.SearchStrategy[str] = st.text(
    alphabet="0123456789abcdef", min_size=8, max_size=40
).map(lambda s: f"s3cr3t_{s}")


@given(secret=_SECRET_TEXT, is_cloud=st.booleans())
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_create_webhook_redacts_secret_in_both_modes(
    secret: str,
    is_cloud: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P12.C: webhook response has ``secret`` redacted and never echoed.

    In both DC mode and Cloud mode:

    * The response JSON is ``success=True`` (the tool reached the happy
      path — a short-circuit would make the test vacuous).
    * The ``configuration.secret`` field in the returned webhook
      payload equals the ``"[REDACTED]"`` placeholder.
    * The caller-supplied secret **substring** does not appear anywhere
      in the full JSON response — not in the webhook body, not in the
      receipt, not in the ``recipient_scope``.

    Validates: Requirement 16.4.
    """
    bb_server = importlib.import_module("mcp_atlassian.servers.bitbucket")

    monkeypatch.delenv("READ_ONLY_MODE", raising=False)
    monkeypatch.delenv("BITBUCKET_PROJECTS_FILTER", raising=False)

    fetcher = _make_fetcher_mock(
        is_cloud=is_cloud,
        workspace=("my-team" if is_cloud else None),
    )
    # Stub ``create_webhook`` to echo the secret back in the DC-shaped
    # envelope so ``redact_secrets()`` has something concrete to scrub.
    # The DC shape is used in both branches because
    # ``normalize_webhook`` on the Cloud branch already passes the
    # payload through unchanged, and the mixin-level redaction
    # guarantees the server-layer recursively walks either shape.
    fetcher.create_webhook = MagicMock(
        return_value={
            "id": 99,
            "name": "hook",
            "url": "https://ci.example.com/hook",
            "events": ["repo:refs_changed"],
            "active": True,
            "configuration": {"secret": secret},
        }
    )
    _install_fetcher(monkeypatch, fetcher)

    ctx = _make_fake_ctx()
    result_json = asyncio.run(
        bb_server.create_webhook.fn(
            ctx,
            project_key="PROJ",
            repo_slug="repo",
            name="hook",
            url="https://ci.example.com/hook",
            events='["repo:refs_changed"]',
            secret=secret,
            active=True,
        )
    )
    payload = json.loads(result_json)

    mode_label = "cloud" if is_cloud else "dc"

    # 1. The tool reached the success path — we're observing redaction,
    # not a short-circuit. If this ever regresses to success=False, the
    # non-echo assertion below would pass vacuously.
    assert payload.get("success") is True, (
        f"[{mode_label}] expected success=True, got {payload!r}"
    )
    assert "webhook" in payload, (
        f"[{mode_label}] expected 'webhook' in response keys, "
        f"got {list(payload)!r}"
    )

    # 2. The ``configuration.secret`` slot is the redaction placeholder.
    assert (
        payload["webhook"]["configuration"]["secret"]
        == REDACTED_PLACEHOLDER
    ), (
        f"[{mode_label}] expected configuration.secret=={REDACTED_PLACEHOLDER!r}, "
        f"got {payload['webhook']['configuration']['secret']!r}"
    )

    # 3. The raw secret substring appears NOWHERE in the serialised
    # JSON. This is the strongest assertion because it survives any
    # future shape change of the response envelope.
    assert secret not in result_json, (
        f"[{mode_label}] secret value leaked into JSON response: "
        f"substring {secret!r} present in result_json"
    )
