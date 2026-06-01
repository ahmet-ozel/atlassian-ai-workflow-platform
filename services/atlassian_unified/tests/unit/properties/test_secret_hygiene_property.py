"""Property test P3 — Secret hygiene: redaction of known-secret keys and
non-echo of input secrets.

Validates Requirements 2.3, 2.4, 21.2, 44.1, 44.2, 44.5, 47.3 / design
Property 3:

    For every tool whose response passes through ``redact_secrets``
    and for every fixture containing a key from ``DEFAULT_SECRET_KEYS``
    (case-insensitive) at any nesting depth, the returned payload SHALL
    replace the leaf value with ``"[REDACTED]"`` and SHALL NOT embed the
    caller-supplied secret verbatim anywhere in the JSON response.

Test shape
----------

* **Property A** — unit-level fuzzing of :func:`redact_secrets` over
  arbitrarily shaped nested ``dict`` / ``list`` structures. Hypothesis
  builds a random container tree, injects a *marker* secret under a
  random secret-shaped key at a random nesting depth, and asserts:

    1. The marker value is **absent** from the serialised output of the
       redacted structure (``json.dumps``) — no verbatim echo.
    2. Every leaf that lived under a secret-shaped key in the *input*
       appears as ``"[REDACTED]"`` in the *output* at the same path.
    3. Every non-secret leaf is preserved exactly (structural equality
       outside the redacted paths).

* **Property B** — tool-layer: ``bitbucket_create_webhook`` with a
  Hypothesis-generated ``secret`` argument. A stub fetcher echoes the
  secret back under ``configuration.secret`` (mimicking what Bitbucket
  actually does on ``POST /webhooks``). The full tool JSON response —
  including the receipt and any nested scope fields — must not contain
  the secret substring.

* **Property C** — tool-layer: ``jira_get_myself`` with a stubbed
  ``get_myself`` returning a Hypothesis-crafted profile whose nested
  ``password`` / ``token`` / ``sessionCookie`` fields all hold distinct
  random secret values. The tool's JSON response must redact every
  matching field, must not echo any of the raw secrets, and must
  preserve non-secret profile fields (``displayName``, ``emailAddress``,
  etc.) verbatim.

Style reference
---------------

Shaped after :mod:`tests.unit.properties.test_comment_visibility_property`
for the tool-layer fixtures (``SimpleNamespace`` ctx shim, monkeypatched
``get_{product}_fetcher``, ``asyncio.run(tool.fn(...))``) and
:mod:`tests.unit.utils.test_secret_redaction` for the canonical
:func:`redact_secrets` contract.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mcp_atlassian.servers import bitbucket as bitbucket_server
from mcp_atlassian.servers import jira as jira_server
from mcp_atlassian.servers.bitbucket import create_webhook
from mcp_atlassian.servers.jira import jira_get_myself
from mcp_atlassian.utils.secret_redaction import (
    DEFAULT_SECRET_KEYS,
    REDACTED_PLACEHOLDER,
    redact_secrets,
)

# ---------------------------------------------------------------------------
# Shared Hypothesis strategies
# ---------------------------------------------------------------------------

# Canonical secret shape: a distinctive marker prefix plus random hex
# suffix. The marker (``s3cr3t_``) guarantees the value cannot collide
# with structural JSON keys like ``"repo_slug"`` or ``"project_key"``
# that the tool layer emits around the payload. The hex suffix provides
# sufficient entropy (>= 8 chars, up to 40 chars) that random non-secret
# leaves cannot accidentally reproduce the value. Together these two
# properties make ``secret not in json.dumps(response)`` a faithful test
# of echo-detection rather than a false-positive on structural content.
_SECRET_TEXT: st.SearchStrategy[str] = st.text(
    alphabet="0123456789abcdef",
    min_size=8,
    max_size=40,
).map(lambda s: f"s3cr3t_{s}")

# Non-secret key names: deliberately excludes every canonical secret-key
# synonym (case-insensitive) so the strategy never accidentally collides
# with a field that would be redacted.
_LOWERED_SECRET_KEYS: frozenset[str] = frozenset(
    k.lower() for k in DEFAULT_SECRET_KEYS
)


def _non_secret_key_filter(name: str) -> bool:
    """Return True when ``name`` is safe to use as a non-secret key."""
    return bool(name) and name.lower() not in _LOWERED_SECRET_KEYS


# Simple identifier-ish keys. Restricted to ASCII letters to keep
# ``json.dumps`` output readable for failure messages and to avoid
# accidental overlap with any secret-shaped key.
_NON_SECRET_KEY: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu"),
        min_codepoint=ord("A"),
        max_codepoint=ord("z"),
    ),
    min_size=3,
    max_size=12,
).filter(_non_secret_key_filter)

# Key drawn from the DEFAULT_SECRET_KEYS set, with random case applied
# so we exercise the case-insensitive matching contract.
_SECRET_KEYS_LIST: tuple[str, ...] = tuple(sorted(DEFAULT_SECRET_KEYS))


@st.composite
def _secret_shaped_key(draw: st.DrawFn) -> str:
    """Draw a secret key from :data:`DEFAULT_SECRET_KEYS` with random case."""
    base = draw(st.sampled_from(_SECRET_KEYS_LIST))
    # Randomly flip case on each character; covers ``Secret``,
    # ``CLIENT_SECRET``, ``Token``, ``apiKEY`` etc. in one strategy.
    flip_mask = draw(
        st.lists(st.booleans(), min_size=len(base), max_size=len(base))
    )
    return "".join(
        ch.upper() if flip and ch.isalpha() else ch.lower() if ch.isalpha() else ch
        for ch, flip in zip(base, flip_mask, strict=True)
    )


# Non-secret leaf values: the redactor preserves these verbatim, so they
# appear in failure messages unchanged.
_NON_SECRET_LEAF: st.SearchStrategy[Any] = st.one_of(
    st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
        max_size=20,
    ),
    st.integers(min_value=-1_000, max_value=1_000),
    st.booleans(),
    st.none(),
)


# ---------------------------------------------------------------------------
# Property A — redact_secrets replaces nested secret keys with the
# placeholder and does not echo the original value
# ---------------------------------------------------------------------------


def _build_nested(
    secret_key: str,
    secret_value: str,
    non_secret_pairs: list[tuple[str, Any]],
    depth: int,
) -> dict[str, Any]:
    """Construct a nested dict ``depth`` layers deep that embeds a single
    secret leaf under ``secret_key`` and ``len(non_secret_pairs)`` benign
    leaves along the path.

    The shape looks like::

        {kN: vN, "l0": {kN-1: vN-1, "l1": {..., secret_key: secret_value}}}

    ``depth=0`` produces a flat dict with the secret at the top level.
    """
    # Innermost layer carries the secret.
    current: dict[str, Any] = {secret_key: secret_value}
    # Walk from the innermost layer outward, wrapping in a new dict each
    # time and sprinkling one benign leaf per layer (when supplied).
    for i, (k, v) in enumerate(reversed(non_secret_pairs[:depth])):
        current = {k: v, f"l{i}": current}
    return current


@st.composite
def _secret_bearing_structure(draw: st.DrawFn) -> dict[str, Any]:
    """Draw a nested dict that contains exactly one secret-shaped leaf.

    The depth ranges from 0 (secret at the top level) to 5 layers deep,
    covering both the "flat" and "deeply-nested" cases that show up in
    real DC responses (e.g. ``configuration.secret`` is depth-1).
    """
    depth = draw(st.integers(min_value=0, max_value=5))
    non_secret_pairs = draw(
        st.lists(
            st.tuples(_NON_SECRET_KEY, _NON_SECRET_LEAF),
            min_size=depth,
            max_size=depth,
            unique_by=lambda pair: pair[0],
        )
    )
    secret_key = draw(_secret_shaped_key())
    secret_value = draw(_SECRET_TEXT)
    return _build_nested(secret_key, secret_value, non_secret_pairs, depth)


def _walk_paths(obj: Any, path: tuple[str | int, ...] = ()) -> list[
    tuple[tuple[str | int, ...], Any]
]:
    """Yield (path, leaf) for every leaf in a ``dict``/``list`` tree."""
    result: list[tuple[tuple[str | int, ...], Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            result.extend(_walk_paths(v, path + (k,)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            result.extend(_walk_paths(v, path + (i,)))
    else:
        result.append((path, obj))
    return result


def _resolve_path(obj: Any, path: tuple[str | int, ...]) -> Any:
    """Follow ``path`` into ``obj`` and return the terminal value."""
    cursor = obj
    for step in path:
        cursor = cursor[step]
    return cursor


@given(payload=_secret_bearing_structure())
def test_redact_replaces_secret_leaves_and_preserves_others(
    payload: dict[str, Any],
) -> None:
    """P3.A: ``redact_secrets`` redacts matching keys at any depth and
    preserves every non-matching leaf verbatim.
    """
    # Capture the original secret leaf(s) so we can assert non-echo.
    original_paths = _walk_paths(payload)
    secret_paths = [
        path
        for path, _ in original_paths
        if path
        and isinstance(path[-1], str)
        and path[-1].lower() in _LOWERED_SECRET_KEYS
    ]
    # The strategy always seeds exactly one secret; make it observable.
    assert secret_paths, "fixture invariant: at least one secret leaf"

    original_secret_values = {
        path: _resolve_path(payload, path) for path in secret_paths
    }

    redacted = redact_secrets(payload)

    # (1) Every secret leaf in the input is now the placeholder.
    for path in secret_paths:
        assert _resolve_path(redacted, path) == REDACTED_PLACEHOLDER, (
            f"expected {path!r} to be redacted, got "
            f"{_resolve_path(redacted, path)!r}"
        )

    # (2) Non-echo: the original secret value never appears in the
    # serialised output. Tests the actual observable contract —
    # downstream clients see JSON, not the python dict.
    dumped = json.dumps(redacted, ensure_ascii=False)
    for path, raw_secret in original_secret_values.items():
        assert raw_secret not in dumped, (
            f"secret value under {path!r} leaked into serialised payload"
        )

    # (3) Non-secret leaves are preserved exactly.
    redacted_paths = dict(_walk_paths(redacted))
    for path, value in original_paths:
        if path in set(secret_paths):
            continue
        assert redacted_paths[path] == value, (
            f"non-secret leaf at {path!r} was modified: "
            f"{value!r} → {redacted_paths[path]!r}"
        )


# ---------------------------------------------------------------------------
# Tool-layer fixtures: minimal FastMCP Context shim + monkeypatched fetcher
# ---------------------------------------------------------------------------


def _make_fake_ctx() -> SimpleNamespace:
    """Minimal ``fastmcp.Context`` stand-in.

    The ``@check_write_access`` decorator reads
    ``ctx.request_context.lifespan_context`` and calls ``.get`` on it; an
    empty dict makes the read-only check a no-op so the property isolates
    secret-hygiene concerns from the read-only gate.
    """
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context={}),
    )


def _make_bitbucket_fetcher(secret_echo: str | None) -> MagicMock:
    """Return a ``BitbucketFetcher``-shaped mock.

    ``create_webhook`` mimics DC's real behaviour: it echoes the request
    body — including ``configuration.secret`` — back to the caller so we
    can verify the server-layer redaction pipeline catches the leak.
    """
    fetcher = MagicMock(name="bitbucket-fetcher")
    fetcher.config = SimpleNamespace(
        projects_filter=None,
        spaces_filter=None,
        username="tester",
    )
    # Modern DC — skips the version gate unambiguously.
    fetcher.get_dc_version.return_value = "9.0.0"
    fetcher._dc_version = "9.0.0"
    fetcher.create_webhook.return_value = {
        "id": 42,
        "name": "hook",
        "url": "https://hooks.example.com/bb",
        "events": ["repo:refs_changed"],
        "active": True,
        # The echo that prompts redaction: Bitbucket reflects the body
        # back, including the HMAC ``configuration.secret`` field.
        "configuration": (
            {"secret": secret_echo} if secret_echo is not None else {}
        ),
    }
    return fetcher


def _make_jira_fetcher(profile: dict[str, Any]) -> MagicMock:
    """Return a ``JiraFetcher``-shaped mock with a crafted ``get_myself``.

    The stub faithfully mimics :class:`MyselfMixin.get_myself`'s
    defence-in-depth strip of the top-level ``password`` / ``token`` /
    ``sessionCookie`` keys before returning. This matches the real
    pipeline so the property exercises the intended composition: mixin
    strip (top-level credentials) + server-layer ``redact_secrets``
    walker (nested secret-shaped keys).
    """
    fetcher = MagicMock(name="jira-fetcher")
    fetcher.config = SimpleNamespace(
        projects_filter=None,
        username="tester",
    )
    fetcher._dc_version = "9.0.0"
    fetcher.get_dc_version.return_value = "9.0.0"

    # Simulate MyselfMixin.get_myself: strip top-level password / token /
    # sessionCookie (case-insensitive match on exact key names) before
    # handing the payload back to the server layer.
    _TOPLEVEL_CREDENTIAL_FIELDS = {"password", "token", "sessioncookie"}
    stripped = {
        k: v
        for k, v in profile.items()
        if not (isinstance(k, str) and k.lower() in _TOPLEVEL_CREDENTIAL_FIELDS)
    }
    fetcher.get_myself.return_value = stripped
    return fetcher


@pytest.fixture
def fake_ctx() -> SimpleNamespace:
    return _make_fake_ctx()


@pytest.fixture
def disable_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure ``READ_ONLY_MODE`` is unset for tool-layer properties."""
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)
    monkeypatch.delenv("BITBUCKET_PROJECTS_FILTER", raising=False)


# ---------------------------------------------------------------------------
# Property B — bitbucket_create_webhook never echoes the caller's secret
# ---------------------------------------------------------------------------
#
# The Hypothesis harness needs to rebuild the fetcher + monkeypatch inside
# the test body (the ``monkeypatch`` fixture is function-scoped and the
# conftest's ``_COMMON_SUPPRESS`` already disables the function-scoped-
# fixture health check — but the fetcher itself must be re-created per
# example so the ``return_value`` reflects the freshly-drawn secret).


@given(secret=_SECRET_TEXT)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_bitbucket_create_webhook_does_not_echo_secret(
    secret: str,
    fake_ctx: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    disable_read_only: None,
) -> None:
    """P3.B: the response JSON must not contain the caller-supplied
    ``secret`` verbatim, even though the stub fetcher echoes it back
    under ``configuration.secret``.
    """
    fetcher = _make_bitbucket_fetcher(secret_echo=secret)

    async def _aget(_ctx: Any) -> MagicMock:
        return fetcher

    monkeypatch.setattr(bitbucket_server, "get_bitbucket_fetcher", _aget)

    result_json = asyncio.run(
        create_webhook.fn(
            fake_ctx,
            project_key="PROJ",
            repo_slug="repo",
            name="hook",
            url="https://hooks.example.com/bb",
            events='["repo:refs_changed"]',
            secret=secret,
            active=True,
        )
    )

    # Sanity: the tool reached the success path (otherwise ``secret``
    # would not have been serialised at all and the test would pass
    # vacuously). We want to observe the redaction, not a short-circuit.
    parsed = json.loads(result_json)
    assert parsed.get("success") is True, (
        f"expected success=True, got {parsed!r}"
    )
    assert "webhook" in parsed, (
        f"expected 'webhook' key in response, got keys={list(parsed)!r}"
    )

    # The observable invariant: the caller's secret is nowhere in the
    # full JSON string — not in the webhook body, not in the receipt,
    # not in the recipient_scope.
    assert secret not in result_json, (
        f"secret value leaked into tool JSON response: "
        f"substring {secret!r} found in result; "
        f"configuration echoes={parsed['webhook'].get('configuration')!r}"
    )

    # The redacted placeholder must be present where the echoed secret
    # lived — this fails if a future refactor drops the redact_secrets
    # call and the secret would have been exposed.
    assert parsed["webhook"]["configuration"]["secret"] == REDACTED_PLACEHOLDER


# ---------------------------------------------------------------------------
# Property C — jira_get_myself redacts every nested secret field
# ---------------------------------------------------------------------------


@st.composite
def _myself_profile(draw: st.DrawFn) -> tuple[dict[str, Any], dict[str, str]]:
    """Draw a ``/myself`` profile with credential-like nested fields.

    Returns a tuple of (profile, secrets) where ``secrets`` is the flat
    mapping from secret key to its generated value so the assertions can
    verify non-echo for each one individually. Every secret value is
    distinct so cross-contamination of assertions (e.g. a ``token``
    accidentally winning the search meant for ``password``) is detected.
    """
    # Distinct secret values for each known credential field. Uniqueness
    # is enforced across the draws so the non-echo check is unambiguous.
    secrets_list = draw(
        st.lists(_SECRET_TEXT, min_size=3, max_size=3, unique=True)
    )
    password_value, token_value, session_value = secrets_list

    secrets: dict[str, str] = {
        "password": password_value,
        "token": token_value,
        "sessionCookie": session_value,
    }

    display_name = draw(
        st.text(
            alphabet=st.characters(min_codepoint=0x41, max_codepoint=0x7A),
            min_size=3,
            max_size=20,
        )
    )
    email = draw(
        st.text(
            alphabet=st.characters(min_codepoint=0x61, max_codepoint=0x7A),
            min_size=3,
            max_size=10,
        ).map(lambda s: f"{s}@example.com")
    )

    profile: dict[str, Any] = {
        "name": "tester",
        "key": "tester",
        "displayName": display_name,
        "emailAddress": email,
        "timeZone": "UTC",
        "locale": "en_US",
        # Top-level credential-shaped fields — mirror what the mixin
        # already defensively strips, but we want the server-layer
        # ``redact_secrets`` walker to catch them too.
        "password": password_value,
        "token": token_value,
        "sessionCookie": session_value,
        # Nested credential field: the mixin's top-level strip would
        # miss this, so the property exercises the walker's recursion.
        "apiTokens": [
            {"label": "ci", "token": token_value},
            {"label": "cli", "apiKey": password_value},
        ],
        "groups": {
            "size": 1,
            "items": [{"name": "jira-users"}],
        },
    }
    return profile, secrets


@given(fixture=_myself_profile())
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_jira_get_myself_redacts_and_does_not_echo_secrets(
    fixture: tuple[dict[str, Any], dict[str, str]],
    fake_ctx: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    disable_read_only: None,
) -> None:
    """P3.C: ``jira_get_myself`` response has every nested secret field
    redacted and the raw values never leak into the serialised JSON.
    """
    profile, secrets = fixture
    fetcher = _make_jira_fetcher(profile)

    async def _aget(_ctx: Any) -> MagicMock:
        return fetcher

    monkeypatch.setattr(jira_server, "get_jira_fetcher", _aget)

    result_json = asyncio.run(jira_get_myself.fn(fake_ctx))

    parsed = json.loads(result_json)
    assert parsed.get("success") is True, (
        f"expected success=True, got {parsed!r}"
    )
    user = parsed["user"]

    # (1) The top-level ``password`` / ``token`` / ``sessionCookie``
    # fields are stripped by ``MyselfMixin.get_myself`` before the
    # server layer sees the payload (Requirement 21.2, defence in
    # depth). The server-layer ``redact_secrets`` walker also covers
    # ``password`` / ``token`` — so if a future refactor drops the
    # mixin strip, the walker catches them. Either outcome is valid:
    # absent key OR redacted placeholder. The forbidden shape is the
    # raw secret echoing back.
    for field in ("password", "token", "sessionCookie"):
        value = user.get(field)
        if value is not None:
            assert value == REDACTED_PLACEHOLDER, (
                f"expected {field!r} to be absent or redacted, got {value!r}"
            )

    # (2) The nested ``apiTokens[*].token`` / ``apiTokens[*].apiKey``
    # fields must be redacted — proves the walker reached them. The
    # mixin only strips top-level keys, so this assertion exercises
    # the server-layer ``redact_secrets`` call directly.
    for entry in user["apiTokens"]:
        if "token" in entry:
            assert entry["token"] == REDACTED_PLACEHOLDER, (
                f"nested apiTokens token should be redacted, "
                f"got {entry['token']!r}"
            )
        if "apiKey" in entry:
            assert entry["apiKey"] == REDACTED_PLACEHOLDER, (
                f"nested apiTokens apiKey should be redacted, "
                f"got {entry['apiKey']!r}"
            )

    # (3) Non-echo: no raw secret value appears anywhere in the
    # serialised JSON. This is the strongest invariant because it
    # survives any future shape change of the response envelope.
    # Because our secret values carry the ``s3cr3t_`` prefix and hex
    # suffix, they cannot collide with structural JSON content.
    for label, raw_value in secrets.items():
        assert raw_value not in result_json, (
            f"{label!r} value leaked into /myself response: "
            f"substring {raw_value!r} found in JSON"
        )

    # (4) Non-secret profile fields are preserved verbatim — proves
    # the redactor is not overzealous and stripping more than it should.
    assert user["displayName"] == profile["displayName"]
    assert user["emailAddress"] == profile["emailAddress"]
    assert user["timeZone"] == "UTC"
    assert user["locale"] == "en_US"
    assert user["groups"]["items"] == [{"name": "jira-users"}]
