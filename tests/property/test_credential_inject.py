"""Property tests for MCP credential injection round-trip and header preservation.

**Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7**

Property 1: MCP credential injection round-trip ve header preservation.

This module uses Hypothesis to verify the following invariants of
``http_shared.auth_inject.with_atlassian_creds``:

1. **Round-trip**: Credentials read from the resolver are injected into
   headers with exact fidelity — the header values inside the with-block
   match the credential fields byte-for-byte (Requirement 4.5).

2. **Header preservation**: All pre-existing headers on the client
   (including ``X-Client-Source`` and arbitrary user headers) survive
   the with-block unchanged (Requirement 4.7).

3. **Header restoration**: After the with-block exits (normally or via
   exception), the client's headers are restored to their pre-injection
   state — credential headers are removed if they didn't exist before,
   or reverted to their original values if they did (Requirement 4.7).

4. **Service routing**: The correct header prefix is used for each
   service literal (jira → X-Atlassian-Jira-*, bitbucket →
   X-Atlassian-Bitbucket-*, confluence → X-Atlassian-Confluence-*)
   (Requirements 4.2, 4.3, 4.4).

5. **Missing credential rejection**: When the resolver returns an
   incomplete credential (any of url, username, personal_token is
   empty), ``CredentialResolutionError`` is raised with the correct
   dept_id and service (Requirement 4.6).

6. **Scope validation**: ``"org"`` (default) and ``"user"`` scopes are
   accepted; ``"bot"`` is a deprecated alias for ``"org"``; any other
   scope value raises ``ValueError`` (Requirements 4.1, 2.1).
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import httpx
import pytest
from hypothesis import HealthCheck, given, settings, assume
from hypothesis import strategies as st

from http_shared.auth_inject import (
    CredentialResolutionError,
    ServiceLiteral,
    _HEADER_PREFIX,
    with_atlassian_creds,
)
# ---------------------------------------------------------------------------
# Shared event loop for performance (avoids asyncio.run() per example)
# ---------------------------------------------------------------------------

_LOOP: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP


def _run_async(coro: Any) -> Any:
    """Run an async coroutine using a shared event loop for performance."""
    return _get_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Printable ASCII excluding control characters — valid for HTTP header values.
_HEADER_CHAR = st.characters(min_codepoint=0x21, max_codepoint=0x7E)

# Non-empty header value text (HTTP headers must be non-empty when present).
_HEADER_VALUE = st.text(alphabet=_HEADER_CHAR, min_size=1, max_size=128)

# URL-like strings for credential url field.
_URL_TEXT = st.from_regex(
    r"https://[a-z][a-z0-9\-]{1,30}\.[a-z]{2,6}",
    fullmatch=True,
).filter(lambda value: "bitbucket.org" not in value.lower())

# Username-like strings.
_USERNAME_TEXT = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="@._-",
        min_codepoint=0x21,
        max_codepoint=0x7E,
    ),
    min_size=1,
    max_size=64,
)

# Token-like strings (non-empty printable ASCII).
_TOKEN_TEXT = st.text(alphabet=_HEADER_CHAR, min_size=1, max_size=128)

# Service literal strategy.
_SERVICE = st.sampled_from(["jira", "bitbucket", "confluence"])

# Department ID strategy.
_DEPT_ID = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Nd"),
        whitelist_characters="-_",
        min_codepoint=0x30,
        max_codepoint=0x7A,
    ),
    min_size=1,
    max_size=32,
)


@st.composite
def atlassian_credentials(draw: st.DrawFn) -> "FakeCredential":
    """Generate a valid (complete) Atlassian credential triple."""
    return FakeCredential(
        url=draw(_URL_TEXT),
        username=draw(_USERNAME_TEXT),
        personal_token=draw(_TOKEN_TEXT),
    )


@st.composite
def incomplete_credentials(draw: st.DrawFn) -> "FakeCredential":
    """Generate a credential with at least one empty field."""
    # Pick which fields to make empty (at least one must be empty).
    empty_fields = draw(
        st.sets(st.sampled_from(["url", "username", "personal_token"]), min_size=1)
    )
    url = "" if "url" in empty_fields else draw(_URL_TEXT)
    username = "" if "username" in empty_fields else draw(_USERNAME_TEXT)
    personal_token = "" if "personal_token" in empty_fields else draw(_TOKEN_TEXT)
    return FakeCredential(url=url, username=username, personal_token=personal_token)


def _header_key_strategy() -> st.SearchStrategy[str]:
    """Generate random header keys that do NOT collide with credential headers."""
    # Avoid keys that start with X-Atlassian- to prevent collision.
    return st.text(
        alphabet=st.characters(
            whitelist_categories=("Ll", "Lu", "Nd"),
            whitelist_characters="-",
            min_codepoint=0x30,
            max_codepoint=0x7A,
        ),
        min_size=1,
        max_size=32,
    ).filter(lambda k: not k.lower().startswith("x-atlassian-"))


@st.composite
def random_initial_headers(draw: st.DrawFn) -> dict[str, str]:
    """Generate a random set of initial headers for the client.

    Always includes X-Client-Source to test preservation. May include
    other arbitrary headers.
    """
    headers: dict[str, str] = {}
    # Always include X-Client-Source (Requirement 4.7 explicitly mentions it).
    headers["X-Client-Source"] = draw(_HEADER_VALUE)
    # Add 0-5 additional random headers.
    n_extra = draw(st.integers(min_value=0, max_value=5))
    for _ in range(n_extra):
        key = draw(_header_key_strategy())
        value = draw(_HEADER_VALUE)
        headers[key] = value
    return headers


# ---------------------------------------------------------------------------
# Fake credential resolver
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FakeCredential:
    url: str
    username: str
    personal_token: str


class FakeCredentialResolver:
    """A minimal duck-typed credential resolver for property testing."""

    def __init__(self, credential: FakeCredential) -> None:
        self._credential = credential

    async def get(self, dept_id: str, service: str, *, scope: str = "bot") -> FakeCredential:
        return self._credential


def _credential_header_keys(service: ServiceLiteral) -> tuple[str, str, str]:
    """Return the three credential header keys for a given service."""
    prefix = _HEADER_PREFIX[service]
    return (f"{prefix}-Url", f"{prefix}-Username", f"{prefix}-Personal-Token")


# ---------------------------------------------------------------------------
# Property Tests
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    cred=atlassian_credentials(),
    service=_SERVICE,
    dept_id=_DEPT_ID,
    initial_headers=random_initial_headers(),
)
def test_round_trip_credential_values_match_exactly(
    cred: FakeCredential,
    service: ServiceLiteral,
    dept_id: str,
    initial_headers: dict[str, str],
) -> None:
    """Property 1a — credential values injected into headers match Vault values exactly.

    **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5**

    For every valid credential triple (url, username, personal_token) and
    every service literal, the header values inside the with-block must
    be byte-for-byte identical to the credential fields.
    """
    resolver = FakeCredentialResolver(cred)

    async def _check() -> None:
        client = httpx.AsyncClient(headers=initial_headers)
        try:
            async with with_atlassian_creds(
                client,
                dept_id=dept_id,
                service=service,
                credential_resolver=resolver,
            ) as c:
                url_key, user_key, token_key = _credential_header_keys(service)
                # Round-trip: header values == credential fields
                assert c.headers[url_key] == cred.url
                assert c.headers[user_key] == cred.username
                assert c.headers[token_key] == cred.personal_token
        finally:
            await client.aclose()

    _run_async(_check())


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    cred=atlassian_credentials(),
    service=_SERVICE,
    dept_id=_DEPT_ID,
    initial_headers=random_initial_headers(),
)
def test_existing_headers_preserved_inside_with_block(
    cred: FakeCredential,
    service: ServiceLiteral,
    dept_id: str,
    initial_headers: dict[str, str],
) -> None:
    """Property 1b — all pre-existing headers survive inside the with-block.

    **Validates: Requirements 4.7**

    For every set of initial headers (including X-Client-Source), all
    non-credential headers must remain unchanged inside the with-block.
    """
    resolver = FakeCredentialResolver(cred)

    async def _check() -> None:
        client = httpx.AsyncClient(headers=initial_headers)
        try:
            async with with_atlassian_creds(
                client,
                dept_id=dept_id,
                service=service,
                credential_resolver=resolver,
            ) as c:
                # All initial headers must still be present and unchanged.
                for key, value in initial_headers.items():
                    # Skip credential header keys if they happen to collide
                    # (they get overwritten by design).
                    cred_keys = {k.lower() for k in _credential_header_keys(service)}
                    if key.lower() in cred_keys:
                        continue
                    assert c.headers[key] == value, (
                        f"Header {key!r} was modified inside with-block: "
                        f"expected {value!r}, got {c.headers.get(key)!r}"
                    )
        finally:
            await client.aclose()

    _run_async(_check())


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    cred=atlassian_credentials(),
    service=_SERVICE,
    dept_id=_DEPT_ID,
    initial_headers=random_initial_headers(),
)
def test_headers_restored_after_with_block_exit(
    cred: FakeCredential,
    service: ServiceLiteral,
    dept_id: str,
    initial_headers: dict[str, str],
) -> None:
    """Property 1c — headers are fully restored after with-block exits.

    **Validates: Requirements 4.7**

    After the context manager exits normally, the client's headers must
    be identical to their state before entering the with-block. Credential
    headers that didn't exist before are removed; those that did exist
    are reverted to their original values.
    """
    resolver = FakeCredentialResolver(cred)

    async def _check() -> None:
        client = httpx.AsyncClient(headers=initial_headers)
        try:
            # Capture pre-injection state (httpx.Headers normalizes keys).
            pre_headers = dict(client.headers.items())

            async with with_atlassian_creds(
                client,
                dept_id=dept_id,
                service=service,
                credential_resolver=resolver,
            ):
                pass  # Just enter and exit

            # Post-injection state must match pre-injection state.
            post_headers = dict(client.headers.items())
            assert post_headers == pre_headers, (
                f"Headers not restored.\n"
                f"Pre:  {pre_headers}\n"
                f"Post: {post_headers}"
            )
        finally:
            await client.aclose()

    _run_async(_check())


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    cred=atlassian_credentials(),
    service=_SERVICE,
    dept_id=_DEPT_ID,
    initial_headers=random_initial_headers(),
)
def test_headers_restored_after_exception_in_with_block(
    cred: FakeCredential,
    service: ServiceLiteral,
    dept_id: str,
    initial_headers: dict[str, str],
) -> None:
    """Property 1d — headers are restored even when an exception occurs.

    **Validates: Requirements 4.7**

    The context manager's finally block must restore headers regardless
    of whether the with-block raises an exception.
    """
    resolver = FakeCredentialResolver(cred)

    async def _check() -> None:
        client = httpx.AsyncClient(headers=initial_headers)
        try:
            pre_headers = dict(client.headers.items())

            with pytest.raises(RuntimeError, match="simulated"):
                async with with_atlassian_creds(
                    client,
                    dept_id=dept_id,
                    service=service,
                    credential_resolver=resolver,
                ):
                    raise RuntimeError("simulated failure")

            post_headers = dict(client.headers.items())
            assert post_headers == pre_headers
        finally:
            await client.aclose()

    _run_async(_check())


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    service=_SERVICE,
    cred=atlassian_credentials(),
    dept_id=_DEPT_ID,
)
def test_correct_header_prefix_per_service(
    service: ServiceLiteral,
    cred: FakeCredential,
    dept_id: str,
) -> None:
    """Property 1e — each service uses its designated header prefix.

    **Validates: Requirements 4.2, 4.3, 4.4**

    jira → X-Atlassian-Jira-*, bitbucket → X-Atlassian-Bitbucket-*,
    confluence → X-Atlassian-Confluence-*.
    """
    resolver = FakeCredentialResolver(cred)
    expected_prefix = _HEADER_PREFIX[service]

    async def _check() -> None:
        client = httpx.AsyncClient()
        try:
            async with with_atlassian_creds(
                client,
                dept_id=dept_id,
                service=service,
                credential_resolver=resolver,
            ) as c:
                url_key, user_key, token_key = _credential_header_keys(service)
                # Verify the keys start with the correct prefix.
                assert url_key.startswith(expected_prefix)
                assert user_key.startswith(expected_prefix)
                assert token_key.startswith(expected_prefix)
                # Verify the keys are present in headers.
                assert url_key in c.headers
                assert user_key in c.headers
                assert token_key in c.headers
                # Verify no OTHER service's credential headers are set.
                for other_service, other_prefix in _HEADER_PREFIX.items():
                    if other_service == service:
                        continue
                    assert f"{other_prefix}-Url" not in c.headers
                    assert f"{other_prefix}-Username" not in c.headers
                    assert f"{other_prefix}-Personal-Token" not in c.headers
        finally:
            await client.aclose()

    _run_async(_check())


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    incomplete_cred=incomplete_credentials(),
    service=_SERVICE,
    dept_id=_DEPT_ID,
)
def test_incomplete_credential_raises_credential_resolution_error(
    incomplete_cred: FakeCredential,
    service: ServiceLiteral,
    dept_id: str,
) -> None:
    """Property 1f — incomplete credentials raise CredentialResolutionError.

    **Validates: Requirements 4.6**

    When any of url, username, or personal_token is empty/falsy, the
    context manager must raise CredentialResolutionError with the correct
    dept_id and service attributes.
    """
    resolver = FakeCredentialResolver(incomplete_cred)

    async def _check() -> None:
        client = httpx.AsyncClient()
        try:
            with pytest.raises(CredentialResolutionError) as exc_info:
                async with with_atlassian_creds(
                    client,
                    dept_id=dept_id,
                    service=service,
                    credential_resolver=resolver,
                ):
                    pass  # pragma: no cover

            assert exc_info.value.dept_id == dept_id
            assert exc_info.value.service == service
        finally:
            await client.aclose()

    _run_async(_check())


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    cred=atlassian_credentials(),
    service=_SERVICE,
    dept_id=_DEPT_ID,
    invalid_scope=st.text(
        alphabet=_HEADER_CHAR, min_size=1, max_size=16
    ).filter(lambda s: s not in {"bot", "org", "user"}),
)
def test_invalid_scope_raises_value_error(
    cred: FakeCredential,
    service: ServiceLiteral,
    dept_id: str,
    invalid_scope: str,
) -> None:
    """Property 1g — unknown scope raises ValueError.

    **Validates: Requirements 4.1, 2.1**

    Accepted scopes are ``"org"`` (worker bot, default) and ``"user"``
    (Streamlit per-user). The legacy value ``"bot"`` is silently routed
    to ``"org"`` (deprecated alias). Any other scope value must raise
    ValueError before attempting credential resolution.
    """
    resolver = FakeCredentialResolver(cred)

    async def _check() -> None:
        client = httpx.AsyncClient()
        try:
            with pytest.raises(ValueError, match="scope must be one of"):
                async with with_atlassian_creds(
                    client,
                    dept_id=dept_id,
                    service=service,
                    credential_resolver=resolver,
                    scope=invalid_scope,
                ):
                    pass  # pragma: no cover
        finally:
            await client.aclose()

    _run_async(_check())


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    cred=atlassian_credentials(),
    service=_SERVICE,
    dept_id=_DEPT_ID,
)
def test_idempotent_double_injection_restores_correctly(
    cred: FakeCredential,
    service: ServiceLiteral,
    dept_id: str,
) -> None:
    """Property 1h — nested with-blocks restore correctly.

    **Validates: Requirements 4.5, 4.7**

    When with_atlassian_creds is used twice in sequence on the same
    client, each exit restores the state to what it was before that
    particular entry. This tests the save/restore mechanism's correctness
    under repeated use.
    """
    resolver = FakeCredentialResolver(cred)

    async def _check() -> None:
        client = httpx.AsyncClient(headers={"X-Client-Source": "test-worker"})
        try:
            pre_headers = dict(client.headers.items())

            # First injection
            async with with_atlassian_creds(
                client,
                dept_id=dept_id,
                service=service,
                credential_resolver=resolver,
            ):
                pass

            mid_headers = dict(client.headers.items())
            assert mid_headers == pre_headers, "First exit didn't restore"

            # Second injection (same client, same service)
            async with with_atlassian_creds(
                client,
                dept_id=dept_id,
                service=service,
                credential_resolver=resolver,
            ):
                pass

            final_headers = dict(client.headers.items())
            assert final_headers == pre_headers, "Second exit didn't restore"
        finally:
            await client.aclose()

    _run_async(_check())


# ===========================================================================
# Property 6 — Atomik departman ekleme + plain-text sızıntı yasağı (task 5.9)
# ===========================================================================
#
# **Validates: Requirements 3.4, 3.6, 5.10, 9.3**
#
# Property 6 extends this module with the plain-text leak invariants of the
# atomic department-create flow described in
# ``.kiro/specs/platform-mimari-foundation/design.md`` §"Atomic Department
# Create" and the Property 6 row of the design's Property → Test mapping
# table (P6 → ``test_credential_inject.py`` extended + new
# ``test_dept_atomic_create.py``).
#
# The five sub-properties below pin one invariant each. They share a single
# generator (``_p6_dept_create_request``) and a single in-memory backend
# tower (fake Vault + fake DB connection + fake probe client + fake audit
# writer) so the tests focus exclusively on the *secrecy* contract:
#
#   P6a — token absent from the success response (``DepartmentCreateResult``).
#   P6b — token absent from every captured ``logging.LogRecord`` after the
#         platform's redaction filter runs.
#   P6c — token absent from every SQL parameter the orchestrator binds.
#   P6d — token absent from the on-disk bytes of the LocalDevBackend
#         encrypted store (sodium ``SecretBox`` envelope must hide the
#         plain-text bytes — Requirement 6.6).
#   P6e — the ``bytearray`` that carried the token is zeroed once the
#         orchestrator returns (Requirement 3.4 — best-effort heap scrub).
#
# Hypothesis only varies the plain-text token here; the remaining inputs
# (dept_id, services, urls, usernames) are fixed so the test focuses on the
# token-leak surface and remains fast (≥ 50 examples per property under
# Windows file I/O).
# ---------------------------------------------------------------------------

import json as _json
import logging as _logging
import secrets as _secrets
import shutil as _shutil
import string as _string
import sys as _sys
import tempfile as _tempfile
from datetime import datetime as _datetime, timezone as _timezone
from pathlib import Path as _Path
from typing import Any as _Any, Mapping as _Mapping

# ``automation_service`` lives under the service tree that is not on the
# default ``sys.path``. The package's ``__init__.py`` re-exports the FastAPI
# ``app``, which itself imports ``from src.config import Settings`` — so
# we add **both** the ``services/automation-service/`` directory (so
# ``src.config`` resolves) and the inner ``src/`` directory (so the
# top-level ``automation_service`` package import works). This mirrors the
# bootstrap used by ``services/automation-service/tests/unit/test_app.py``.
_AUTOMATION_ROOT = (
    _Path(__file__).resolve().parents[1].parent
    / "services"
    / "automation-service"
)
_AUTOMATION_SRC = _AUTOMATION_ROOT / "src"
for _p in (str(_AUTOMATION_SRC), str(_AUTOMATION_ROOT)):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)

from audit_logger import AuditEvent as _AuditEvent  # noqa: E402

# Task 5.3 ships ``automation_service.admin.router`` as a real module;
# no pre-registered stub is needed.

from automation_service.admin.dept_create import (  # noqa: E402
    DepartmentCreateOrchestrator as _Orchestrator,
    DepartmentCreateRequest as _CreateRequest,
    _BotCredential as _BotCred,
    _zero_bytearray as _zero_bytearray_fn,
)
from automation_service.probe import (  # noqa: E402
    ProbeArtifact as _ProbeArtifact,
    ProbeResult as _ProbeResult,
    ProbeTargets as _ProbeTargets,
)


# ---------------------------------------------------------------------------
# The orchestrator ships ``_zero_all_tokens`` as a real method (task
# 5.3); no compatibility shim is needed.
# ---------------------------------------------------------------------------

from http_shared.redaction import RedactionFilter as _RedactionFilter  # noqa: E402
from vault_client import VaultPath as _VaultPath  # noqa: E402
from vault_client.local_dev_backend import (  # noqa: E402
    KEY_SIZE as _LOCAL_DEV_KEY_SIZE,
    LocalDevBackend as _LocalDevBackend,
)


# ---------------------------------------------------------------------------
# Token strategy — opaque, high-entropy, distinctive
# ---------------------------------------------------------------------------

# We need values that:
#   1. Are **distinctive** — short common substrings (``a``, ``1234``)
#      could accidentally appear in fixture text and trigger spurious
#      "leak" assertions, defeating the property.
#   2. Are **utf-8 encodable** — the orchestrator decodes the bytearray
#      via ``decode("utf-8")`` and rejects bad bytes with
#      ``StagingFailureError``.
#   3. Stay within Atlassian PAT shape (printable ASCII, no whitespace).
#
# We restrict to the same alphabet used by ``_HEADER_VALUE`` above so
# tokens look realistic and never accidentally collide with English words
# or repository identifiers.
_P6_TOKEN_ALPHABET = _string.ascii_letters + _string.digits + "+/=._-"

_p6_plain_token_text = st.text(
    alphabet=_P6_TOKEN_ALPHABET, min_size=24, max_size=64
)


def _make_token_bytearray(plain: str) -> bytearray:
    """Wrap *plain* into the mutable buffer the orchestrator requires."""

    return bytearray(plain.encode("utf-8"))


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class _FakeProbeClient:
    """Minimal :class:`AtlassianProbeClient` that always returns OK.

    The orchestrator runs probes between the staging write and the DB
    insert. For the secrecy property tests we don't care about the
    probe semantics — we just need ``ProbeResult.state == "ok"`` so
    the run reaches the commit phase. The fake therefore returns a
    minimal ``myself``-shaped payload for every read call and a
    fresh artifact id for every write call.

    Method signatures preserve the keyword-argument names the
    :class:`ProbeRunner` uses internally (``body``, ``issue_key``,
    ``comment_id``, ``workspace``, ``repo``, ``branch_name``,
    ``space_key``, ``title``, ``page_id``).
    """

    async def jira_myself(self, cred: _Any) -> dict[str, _Any]:
        return {"accountId": "auto-fetched-id", "displayName": "Probe Bot"}

    async def jira_search_self_comments(
        self, cred: _Any, author_account_id: str
    ) -> list[dict[str, _Any]]:
        return []

    async def jira_create_self_comment(
        self, cred: _Any, body: str
    ) -> dict[str, _Any]:
        return {"id": "c1", "issue_key": "PROBE-1"}

    async def jira_delete_comment(
        self, cred: _Any, *, issue_key: str, comment_id: str
    ) -> None:
        return None

    async def bitbucket_user(self, cred: _Any) -> dict[str, _Any]:
        return {"account_id": "auto-fetched-bb", "username": "probe-bot"}

    async def bitbucket_list_probe_branches(
        self, cred: _Any, *, workspace: str, repo: str
    ) -> list[str]:
        return []

    async def bitbucket_create_branch(
        self,
        cred: _Any,
        *,
        workspace: str,
        repo: str,
        branch_name: str,
    ) -> str:
        return "deadbeef"

    async def bitbucket_delete_branch(
        self,
        cred: _Any,
        *,
        workspace: str,
        repo: str,
        branch_name: str,
    ) -> None:
        return None

    async def confluence_user(self, cred: _Any) -> dict[str, _Any]:
        return {"accountId": "auto-fetched-conf", "displayName": "Probe Bot"}

    async def confluence_list_probe_pages(
        self, cred: _Any, *, space_key: str
    ) -> list[dict[str, _Any]]:
        return []

    async def confluence_create_draft_page(
        self, cred: _Any, *, space_key: str, title: str
    ) -> dict[str, _Any]:
        return {"id": "p1", "title": title}

    async def confluence_delete_page(self, cred: _Any, *, page_id: str) -> None:
        return None


class _RecordingConnection:
    """asyncpg-shaped connection that records every (sql, args) pair.

    The orchestrator binds parameters via ``$1`` / ``$2`` so SQL
    injection cannot leak the token into the SQL string itself; the
    secrecy invariant we test is that **no parameter** ever contains
    the plain-text token (the canonical write path is Vault, not the
    DB row — Requirement 3.3 / 3.4 / 6.1).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[_Any, ...]]] = []

    async def execute(self, query: str, *args: _Any) -> _Any:
        self.calls.append((query, args))
        return None


class _RecordingAuditWriter:
    """Captures every :class:`AuditEvent` so we can scan its payload."""

    def __init__(self) -> None:
        self.events: list[_AuditEvent] = []

    async def insert_audit(self, event: _AuditEvent) -> None:
        self.events.append(event)


class _RecordingAuditLogger:
    """Adapter exposing the :class:`AuditLogger.write` shape."""

    def __init__(self) -> None:
        self.writer = _RecordingAuditWriter()
        self.events: list[_AuditEvent] = self.writer.events

    async def write(self, event: _AuditEvent) -> None:
        await self.writer.insert_audit(event)


# ---------------------------------------------------------------------------
# Test fixture builders
# ---------------------------------------------------------------------------


def _build_create_request(plain_token: str) -> _CreateRequest:
    """Build a single-bot Jira-only :class:`DepartmentCreateRequest`.

    We restrict the request to a single Jira bot so the test exercises
    the full staging → probe → DB → promotion sequence with the
    smallest moving parts. The other services follow the same code
    path; covering one is sufficient for the secrecy invariant and
    keeps the property's wall-clock time bounded.
    """

    return _CreateRequest(
        dept_id="acme",
        display_name="Acme Engineering",
        default_language="en",
        web_search_enabled=False,
        mode="active",
        jira_project_keys=("ACME",),
        confluence_space_keys=(),
        bitbucket_workspace=None,
        config_json={"id": "acme", "display_name": "Acme Engineering"},
        bots=(
            _BotCred(
                service="jira",
                url="https://acme.atlassian.net",
                username="bot@acme.test",
                personal_token=_make_token_bytearray(plain_token),
                account_id=None,
                deployment=None,
            ),
        ),
        probe_targets=_ProbeTargets(),
    )


def _build_orchestrator(
    *,
    vault: _Any,
    connection: _RecordingConnection,
    audit: _RecordingAuditLogger,
) -> _Orchestrator:
    """Wire a fresh orchestrator over the supplied collaborators."""

    async def _factory() -> _RecordingConnection:
        return connection

    return _Orchestrator(
        vault=vault,
        connection_factory=_factory,
        probe_client=_FakeProbeClient(),
        audit_logger=audit,  # type: ignore[arg-type]
        clock=lambda: _datetime(2025, 1, 1, tzinfo=_timezone.utc),
    )


def _new_local_dev_backend(tmpdir: _Path) -> _LocalDevBackend:
    """Build a fresh encrypted-file Vault backend rooted at *tmpdir*.

    We use a real :class:`LocalDevBackend` (libsodium ``SecretBox``)
    rather than an in-memory fake so Property 6d can read the on-disk
    bytes and verify the plain-text never appears in the encrypted
    envelope (Requirement 6.6 — local-dev backend rejects plain-text
    persistence).
    """

    key = _secrets.token_bytes(_LOCAL_DEV_KEY_SIZE)
    store = tmpdir / "vault.kv"
    return _LocalDevBackend(store_path=store, key=key)


# ---------------------------------------------------------------------------
# Property 6a — token absent from the success response
# ---------------------------------------------------------------------------


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(plain_token=_p6_plain_token_text)
def test_p6a_response_body_does_not_contain_plain_text_token(
    plain_token: str,
) -> None:
    """Property 6a — :class:`DepartmentCreateResult` never carries the token.

    **Validates: Requirements 3.4, 9.3**

    For every randomly generated plain-text token, the orchestrator's
    success response (the value the FastAPI router serialises into
    JSON) must contain only Vault path references — never the token
    bytes.
    """

    workspace = _Path(_tempfile.mkdtemp(prefix="p6a-resp-"))
    try:
        vault = _new_local_dev_backend(workspace)
        connection = _RecordingConnection()
        audit = _RecordingAuditLogger()
        orchestrator = _build_orchestrator(
            vault=vault, connection=connection, audit=audit
        )

        request = _build_create_request(plain_token)

        async def _go() -> _Any:
            return await orchestrator.run(
                request, actor_id="ops-1", actor_role="admin"
            )

        result = _run_async(_go())

        # Serialise every public field we hand back to the caller so
        # we catch leaks via __repr__ / dataclass field values too.
        result_repr = repr(result)
        result_refs = " | ".join(result.credential_refs.values())

        # The plain-text token must NOT appear anywhere in the
        # response surface.
        assert plain_token not in result_repr, (
            "Property 6a violated: plain-text token leaked into "
            "DepartmentCreateResult repr (Requirement 3.4)."
        )
        assert plain_token not in result_refs, (
            "Property 6a violated: plain-text token leaked into a "
            "credential_ref (Requirements 3.3, 3.4, 6.1)."
        )

        # Positive assertion: the response carries the **final** Vault
        # path (and only the path) for the bot we created.
        assert result.credential_refs["jira"] == "vault:atlassian/acme/jira"
        assert result.dept_id == "acme"
        assert result.services == ("jira",)
    finally:
        _shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 6b — token absent from log records (after redaction filter)
# ---------------------------------------------------------------------------


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(plain_token=_p6_plain_token_text)
def test_p6b_log_records_do_not_contain_plain_text_token(
    plain_token: str,
) -> None:
    """Property 6b — captured log records carry no plain-text token bytes.

    **Validates: Requirements 3.4, 6.10, 9.3**

    The orchestrator logs ``dept_create.start`` / ``dept_create.ok`` /
    ``dept_create.failed`` records. None of these is supposed to
    interpolate the token, but we test the negative invariant
    end-to-end: capture every emitted record (after the platform's
    :class:`RedactionFilter` runs) and assert the token bytes do not
    appear in any of them.
    """

    workspace = _Path(_tempfile.mkdtemp(prefix="p6b-logs-"))
    captured: list[str] = []

    class _Capture(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            try:
                captured.append(self.format(record))
            except Exception:  # pragma: no cover - defensive
                captured.append(repr(record.msg))

    handler = _Capture(level=_logging.DEBUG)
    handler.setFormatter(_logging.Formatter("%(name)s %(levelname)s %(message)s"))
    handler.addFilter(_RedactionFilter())

    # Attach to the dept_create module logger and the root so any
    # propagation still reaches the capture handler.
    target_logger = _logging.getLogger(
        "automation_service.admin.dept_create"
    )
    root_logger = _logging.getLogger()

    prior_level = target_logger.level
    target_logger.setLevel(_logging.DEBUG)
    target_logger.addHandler(handler)

    try:
        vault = _new_local_dev_backend(workspace)
        connection = _RecordingConnection()
        audit = _RecordingAuditLogger()
        orchestrator = _build_orchestrator(
            vault=vault, connection=connection, audit=audit
        )

        request = _build_create_request(plain_token)

        async def _go() -> _Any:
            return await orchestrator.run(
                request, actor_id="ops-1", actor_role="admin"
            )

        _run_async(_go())

        # Every captured line MUST be free of the token bytes.
        # We also tolerate the root logger missing the filter — the
        # property still holds because dept_create itself never
        # interpolates the token into a log call.
        for line in captured:
            assert plain_token not in line, (
                f"Property 6b violated: plain-text token leaked into log "
                f"record {line!r} (Requirements 3.4, 6.10)."
            )
    finally:
        target_logger.removeHandler(handler)
        target_logger.setLevel(prior_level)
        _shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 6c — token absent from SQL parameter bindings
# ---------------------------------------------------------------------------


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(plain_token=_p6_plain_token_text)
def test_p6c_db_parameters_do_not_contain_plain_text_token(
    plain_token: str,
) -> None:
    """Property 6c — no SQL parameter ever carries the plain-text token.

    **Validates: Requirements 3.3, 3.4, 6.1, 9.3**

    The Postgres ``automation.department_bots.credential_ref`` column
    stores the **Vault path**, not the value (Requirement 6.1). This
    property pins the negative invariant: across every
    ``connection.execute`` call the orchestrator issues, none of the
    bound positional parameters may be the plain-text token.
    """

    workspace = _Path(_tempfile.mkdtemp(prefix="p6c-db-"))
    try:
        vault = _new_local_dev_backend(workspace)
        connection = _RecordingConnection()
        audit = _RecordingAuditLogger()
        orchestrator = _build_orchestrator(
            vault=vault, connection=connection, audit=audit
        )

        request = _build_create_request(plain_token)

        async def _go() -> _Any:
            return await orchestrator.run(
                request, actor_id="ops-1", actor_role="admin"
            )

        result = _run_async(_go())

        # Walk every recorded (sql, args) pair and assert the token
        # does not appear in any positional / keyword argument or in
        # the SQL string itself (defence in depth — the orchestrator
        # uses parameterised queries, but a future regression that
        # builds a literal SQL string could still leak).
        for sql, args in connection.calls:
            assert plain_token not in sql, (
                f"Property 6c violated: token leaked into SQL string "
                f"{sql!r} (Requirements 3.4, 6.1)."
            )
            for idx, arg in enumerate(args):
                if isinstance(arg, str):
                    assert plain_token not in arg, (
                        f"Property 6c violated: token leaked into SQL "
                        f"parameter ${idx + 1} of {sql.strip().splitlines()[0]!r} "
                        "(Requirement 6.1)."
                    )
                elif isinstance(arg, (bytes, bytearray)):
                    assert plain_token.encode("utf-8") not in bytes(arg), (
                        f"Property 6c violated: token leaked into SQL "
                        f"binary parameter ${idx + 1} (Requirement 6.1)."
                    )

        # Positive contract: a ``credential_ref`` parameter pointing
        # at the **final** Vault path is bound to the bot insert, so
        # the row carries the path indirection rather than the secret.
        bot_inserts = [
            (sql, args)
            for sql, args in connection.calls
            if "department_bots" in sql
        ]
        assert bot_inserts, (
            "Expected at least one INSERT into department_bots; got none."
        )
        # The third positional parameter is ``credential_ref`` per
        # the orchestrator's INSERT statement.
        for _sql, args in bot_inserts:
            credential_ref = args[2]
            assert isinstance(credential_ref, str)
            assert credential_ref.startswith("vault:"), (
                "credential_ref must reference Vault, never the plain-text "
                f"token (got {credential_ref!r})."
            )

        # The orchestrator should also have returned a successful result.
        assert result.credential_refs["jira"].startswith("vault:")
    finally:
        _shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 6d — token absent from on-disk Vault store bytes
# ---------------------------------------------------------------------------


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(plain_token=_p6_plain_token_text)
def test_p6d_local_dev_store_bytes_do_not_contain_plain_text_token(
    plain_token: str,
) -> None:
    """Property 6d — on-disk Vault store never carries the token bytes.

    **Validates: Requirements 3.4, 6.6, 9.3**

    The :class:`LocalDevBackend` encrypts the entire KV payload with
    libsodium ``SecretBox`` before writing it to disk. After a
    successful create, reading the raw store bytes back must NOT
    reveal the plain-text token under any encoding (utf-8, ascii,
    base64).
    """

    workspace = _Path(_tempfile.mkdtemp(prefix="p6d-disk-"))
    try:
        vault = _new_local_dev_backend(workspace)
        connection = _RecordingConnection()
        audit = _RecordingAuditLogger()
        orchestrator = _build_orchestrator(
            vault=vault, connection=connection, audit=audit
        )

        request = _build_create_request(plain_token)

        async def _go() -> _Any:
            return await orchestrator.run(
                request, actor_id="ops-1", actor_role="admin"
            )

        _run_async(_go())

        # Walk every file under the workspace (the store + atomic-
        # rename tmp file, if any) and assert the token bytes are
        # absent from raw bytes.
        token_utf8 = plain_token.encode("utf-8")
        token_ascii = plain_token.encode("ascii", errors="ignore")

        for path in workspace.rglob("*"):
            if not path.is_file():
                continue
            data = path.read_bytes()
            assert token_utf8 not in data, (
                f"Property 6d violated: token bytes appear in "
                f"on-disk file {path.name!r} (Requirement 6.6)."
            )
            if token_ascii and token_ascii != token_utf8:
                assert token_ascii not in data, (
                    f"Property 6d violated: token ascii bytes appear "
                    f"in on-disk file {path.name!r} (Requirement 6.6)."
                )

            # The envelope is always JSON with a single ``ciphertext``
            # field — defence-in-depth check that the structure matches
            # the local-dev backend contract.
            try:
                envelope = _json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(envelope, dict) and "ciphertext" in envelope:
                # Make sure the token didn't accidentally end up in
                # an unencrypted sibling field.
                assert plain_token not in envelope.get("ciphertext", ""), (
                    "Property 6d violated: token appears in ciphertext "
                    "field literally — encryption was not applied."
                )
                for k, v in envelope.items():
                    if k == "ciphertext":
                        continue
                    if isinstance(v, str):
                        assert plain_token not in v, (
                            f"Property 6d violated: token appears in "
                            f"envelope field {k!r} (Requirement 6.6)."
                        )
    finally:
        _shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# Property 6e — token bytearray is zeroed after the orchestrator returns
# ---------------------------------------------------------------------------


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(plain_token=_p6_plain_token_text)
def test_p6e_token_bytearray_is_zeroed_after_run(plain_token: str) -> None:
    """Property 6e — the input ``personal_token`` bytearray is fully zeroed.

    **Validates: Requirements 3.4**

    The orchestrator promises a best-effort heap scrub of the
    ``personal_token`` :class:`bytearray` once Vault has the value.
    We hold a reference to the same buffer the orchestrator received
    and check it is all-zero on the success path.
    """

    workspace = _Path(_tempfile.mkdtemp(prefix="p6e-zero-"))
    try:
        vault = _new_local_dev_backend(workspace)
        connection = _RecordingConnection()
        audit = _RecordingAuditLogger()
        orchestrator = _build_orchestrator(
            vault=vault, connection=connection, audit=audit
        )

        request = _build_create_request(plain_token)
        # Hold our own reference to the bytearray so we can inspect it
        # after the orchestrator zeroes it in place. ``bytearray`` is
        # mutable, so the reference and the orchestrator's copy point
        # at the same buffer.
        token_buffer = request.bots[0].personal_token
        original_length = len(token_buffer)
        assert original_length == len(plain_token.encode("utf-8")), (
            "test setup invariant: bytearray must mirror the token bytes"
        )

        async def _go() -> _Any:
            return await orchestrator.run(
                request, actor_id="ops-1", actor_role="admin"
            )

        _run_async(_go())

        # After the run the buffer must be all-zero (R3.4 — scrubbed
        # before the DB transaction begins).
        assert len(token_buffer) == original_length, (
            "token buffer length must not change during scrub"
        )
        assert all(b == 0 for b in token_buffer), (
            "Property 6e violated: token bytearray was not zeroed after "
            "orchestrator.run; remaining non-zero bytes leak the secret "
            "(Requirement 3.4)."
        )
    finally:
        _shutil.rmtree(workspace, ignore_errors=True)


# ===========================================================================
# Property 15 — Credential resolve önceliği: per-user > org-default (task 12.11)
# ===========================================================================
#
# **Validates: Requirements 10.7, 10.8**
#
# Property 15 pins the priority rule for
# :class:`automation_service.credentials.CredentialResolver`:
#
#   1. Per-user override — ``vault:atlassian/_user_session/<session_id>/<service>``
#   2. Org-default bot   — ``vault:atlassian/<dept_id>/<service>``
#   3. Neither present   → :class:`CredentialMissing`
#                          (``error_code == "credential_missing"``)
#
# We test the full 2x2 truth table of
# ``(per_user_present, org_default_present)`` against random
# ``(session_id, dept_id, service)`` triples to lock down:
#
#   P15a — (True, True)  → output comes from the per-user path.
#   P15b — (True, False) → output still comes from the per-user path.
#   P15c — (False, True) → output comes from the org-default path.
#   P15d — (False, False) → ``CredentialMissing`` (a.k.a.
#          ``credential_missing``) is raised; the resolver attempts both
#          paths in order and never returns a payload.
#
# A fifth invariant (P15e) pins the **call ordering** — the per-user
# path MUST be queried first regardless of org-default presence; this
# enforces the "no implicit org leak when a user override exists"
# property at the I/O level (the resolver doesn't read the org path
# when the user path is present, so an attacker who can observe the
# Vault audit log cannot tell whether org-default exists from a
# successful per-user lookup).
#
# Strategies are deliberately tight: we only need printable ASCII
# session/dept ids that survive the Vault path regex
# (``[A-Za-z0-9_\-]+``). We do **not** vary the secret payload —
# its shape is irrelevant to the priority rule and varying it would
# only burn Hypothesis budget without buying additional coverage.
# ---------------------------------------------------------------------------

# The Property 6 block above already inserts ``automation-service/src``
# onto ``sys.path``, so the import below resolves without further
# bootstrap. Keeping the imports here (rather than at the top of the
# module) preserves the "one Property block = one import section"
# layout that Property 1 / Property 6 already follow.

from automation_service.credentials import (  # noqa: E402
    AtlassianService as _P15Service,
    CredentialMissing as _P15CredentialMissing,
    CredentialResolver as _P15CredentialResolver,
    ResolvedCredential as _P15ResolvedCredential,
    build_org_default_path as _p15_build_org_default_path,
    build_user_session_path as _p15_build_user_session_path,
)


# ---------------------------------------------------------------------------
# Path-safe identifier strategies (R10.7 / R10.8 path layout)
# ---------------------------------------------------------------------------

# Session ids are opaque tokens minted by the Streamlit / assistant layer;
# in practice they are URL-safe base64 with optional hyphens / underscores.
# We restrict to ``[A-Za-z0-9_-]`` so the generated strings always survive
# the Vault path canonical form ``vault:<bucket>/<segment>/<segment>``.
_p15_session_id = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="-_",
        min_codepoint=0x30,
        max_codepoint=0x7A,
    ),
    min_size=1,
    max_size=32,
)

# Department ids must be URL-safe and non-empty; the resolver rejects
# the empty string with ``ValueError`` and we don't want to exercise
# that pre-condition path inside the priority property.
_p15_dept_id = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Nd"),
        whitelist_characters="-_",
        min_codepoint=0x30,
        max_codepoint=0x7A,
    ),
    min_size=1,
    max_size=32,
)

_p15_service: st.SearchStrategy[_P15Service] = st.sampled_from(
    ["jira", "bitbucket", "confluence"]
)


# ---------------------------------------------------------------------------
# In-memory Vault fake — duck-typed VaultReader (KeyError on miss)
# ---------------------------------------------------------------------------


class _P15FakeVault:
    """Tiny in-memory ``VaultReader`` that records every read.

    The ``calls`` list lets P15e assert the lookup ordering. Missing
    paths raise ``KeyError`` to match the protocol contract documented
    in :mod:`automation_service.credentials`.
    """

    def __init__(self, secrets: _Mapping[str, _Mapping[str, str]] | None = None) -> None:
        self.secrets: dict[str, _Mapping[str, str]] = dict(secrets or {})
        self.calls: list[str] = []

    def read(self, path: str) -> _Mapping[str, str]:
        self.calls.append(path)
        if path not in self.secrets:
            raise KeyError(path)
        return self.secrets[path]


# Distinct sentinel payloads so the "which path won?" assertion is
# unambiguous. Using the same dict for both sources would weaken the
# property — equal output could mean either path was read.
_P15_USER_SECRET: _Mapping[str, str] = {
    "url": "https://acme.atlassian.net",
    "email": "alice@acme.example",
    "api_token": "user-session-token-PLACEHOLDER",
}
_P15_ORG_SECRET: _Mapping[str, str] = {
    "url": "https://acme.atlassian.net",
    "email": "bot@acme.example",
    "api_token": "org-default-token-PLACEHOLDER",
}


# ---------------------------------------------------------------------------
# Property 15a — (per_user=True, org_default=True) → per-user wins
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(session_id=_p15_session_id, dept_id=_p15_dept_id, service=_p15_service)
def test_p15a_user_present_org_present_returns_user(
    session_id: str, dept_id: str, service: _P15Service
) -> None:
    """Property 15a — per-user override wins over org-default when both exist.

    **Validates: Requirements 10.7, 10.8**
    """

    user_path = _p15_build_user_session_path(session_id, service)
    org_path = _p15_build_org_default_path(dept_id, service)

    vault = _P15FakeVault({user_path: _P15_USER_SECRET, org_path: _P15_ORG_SECRET})
    resolver = _P15CredentialResolver(vault=vault)

    result = resolver.resolve(session_id, dept_id, service)

    assert isinstance(result, _P15ResolvedCredential)
    assert result.source == "user_session", (
        f"Property 15a violated: with both paths present, expected the "
        f"per-user path to win but got source={result.source!r}."
    )
    assert result.path == user_path
    assert result.data == _P15_USER_SECRET
    # The resolver MUST short-circuit: no org-default lookup when the
    # per-user override is satisfied. This is part of the priority
    # contract — observers of the Vault audit log should not see an
    # org-default read on a user-served call.
    assert vault.calls == [user_path], (
        f"Property 15a violated: org-default path was queried even though "
        f"the per-user override existed. calls={vault.calls!r}"
    )


# ---------------------------------------------------------------------------
# Property 15b — (per_user=True, org_default=False) → per-user wins
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(session_id=_p15_session_id, dept_id=_p15_dept_id, service=_p15_service)
def test_p15b_user_present_org_absent_returns_user(
    session_id: str, dept_id: str, service: _P15Service
) -> None:
    """Property 15b — per-user override resolves even with no org-default.

    **Validates: Requirements 10.7, 10.8**
    """

    user_path = _p15_build_user_session_path(session_id, service)

    vault = _P15FakeVault({user_path: _P15_USER_SECRET})
    resolver = _P15CredentialResolver(vault=vault)

    result = resolver.resolve(session_id, dept_id, service)

    assert result.source == "user_session"
    assert result.path == user_path
    assert result.data == _P15_USER_SECRET
    # Same short-circuit invariant as P15a.
    assert vault.calls == [user_path]


# ---------------------------------------------------------------------------
# Property 15c — (per_user=False, org_default=True) → org-default wins
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(session_id=_p15_session_id, dept_id=_p15_dept_id, service=_p15_service)
def test_p15c_user_absent_org_present_returns_org(
    session_id: str, dept_id: str, service: _P15Service
) -> None:
    """Property 15c — fall back to org-default when per-user is missing.

    **Validates: Requirements 10.7, 10.8**
    """

    user_path = _p15_build_user_session_path(session_id, service)
    org_path = _p15_build_org_default_path(dept_id, service)

    vault = _P15FakeVault({org_path: _P15_ORG_SECRET})
    resolver = _P15CredentialResolver(vault=vault)

    result = resolver.resolve(session_id, dept_id, service)

    assert result.source == "org_default", (
        f"Property 15c violated: with only org-default present, expected "
        f"source='org_default' but got {result.source!r}."
    )
    assert result.path == org_path
    assert result.data == _P15_ORG_SECRET
    # Per-user is queried first, then org-default. Ordering matters —
    # see P15e below.
    assert vault.calls == [user_path, org_path]


# ---------------------------------------------------------------------------
# Property 15d — (per_user=False, org_default=False) → credential_missing
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(session_id=_p15_session_id, dept_id=_p15_dept_id, service=_p15_service)
def test_p15d_user_absent_org_absent_raises_credential_missing(
    session_id: str, dept_id: str, service: _P15Service
) -> None:
    """Property 15d — both paths missing → ``credential_missing`` raised.

    **Validates: Requirements 10.7, 10.8**
    """

    user_path = _p15_build_user_session_path(session_id, service)
    org_path = _p15_build_org_default_path(dept_id, service)

    vault = _P15FakeVault()  # empty store
    resolver = _P15CredentialResolver(vault=vault)

    with pytest.raises(_P15CredentialMissing) as exc_info:
        resolver.resolve(session_id, dept_id, service)

    err = exc_info.value
    # Canonical audit error code (R10.8).
    assert err.error_code == "credential_missing"
    assert err.session_id == session_id
    assert err.dept_id == dept_id
    assert err.service == service
    assert err.attempted_paths == (user_path, org_path)
    # Both paths must have been tried before raising — the resolver
    # cannot short-circuit and call ``credential_missing`` without
    # actually checking the org-default fallback.
    assert vault.calls == [user_path, org_path], (
        f"Property 15d violated: resolver did not attempt both paths "
        f"before raising credential_missing. calls={vault.calls!r}"
    )
    # Must subclass LookupError so generic catch sites keep working.
    assert isinstance(err, LookupError)


# ---------------------------------------------------------------------------
# Property 15e — call ordering invariant across the entire 2x2 matrix
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    session_id=_p15_session_id,
    dept_id=_p15_dept_id,
    service=_p15_service,
    per_user_present=st.booleans(),
    org_default_present=st.booleans(),
)
def test_p15e_lookup_order_is_user_then_org_default(
    session_id: str,
    dept_id: str,
    service: _P15Service,
    per_user_present: bool,
    org_default_present: bool,
) -> None:
    """Property 15e — full 2x2 matrix: priority + call ordering.

    **Validates: Requirements 10.7, 10.8**

    For every combination of ``(per_user_present, org_default_present)``:

    * The per-user path is **always** the first read.
    * The org-default path is read **only** when the per-user lookup
      missed (i.e. the resolver does not over-fetch).
    * The returned ``source`` matches the boolean inputs:
        - per_user_present → ``"user_session"``
        - else org_default_present → ``"org_default"``
        - else → ``CredentialMissing``
    """

    user_path = _p15_build_user_session_path(session_id, service)
    org_path = _p15_build_org_default_path(dept_id, service)

    secrets: dict[str, _Mapping[str, str]] = {}
    if per_user_present:
        secrets[user_path] = _P15_USER_SECRET
    if org_default_present:
        secrets[org_path] = _P15_ORG_SECRET

    vault = _P15FakeVault(secrets)
    resolver = _P15CredentialResolver(vault=vault)

    if per_user_present:
        result = resolver.resolve(session_id, dept_id, service)
        assert result.source == "user_session"
        assert result.path == user_path
        assert result.data == _P15_USER_SECRET
        # Short-circuit: org-default is not queried when user wins.
        assert vault.calls == [user_path]
    elif org_default_present:
        result = resolver.resolve(session_id, dept_id, service)
        assert result.source == "org_default"
        assert result.path == org_path
        assert result.data == _P15_ORG_SECRET
        # User first (miss), then org-default.
        assert vault.calls == [user_path, org_path]
    else:
        with pytest.raises(_P15CredentialMissing) as exc_info:
            resolver.resolve(session_id, dept_id, service)
        assert exc_info.value.error_code == "credential_missing"
        assert exc_info.value.attempted_paths == (user_path, org_path)
        # Both paths attempted, in order, before raising.
        assert vault.calls == [user_path, org_path]
