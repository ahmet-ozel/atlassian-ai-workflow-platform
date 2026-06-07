"""Unit tests for ``src.decision.credential_resolver``.

Tests exercise the CredentialResolver against in-memory fakes for both
Vault and asyncpg, verifying:
- Successful credential resolution (happy path)
- Caching behaviour of list_dept_bots
- CredentialResolutionError on missing bot registration
- CredentialResolutionError on Vault 404
- CredentialResolutionError on incomplete secret
- ValueError on invalid scope
- Cross-scope read isolation (uyumluluk Q7 / R2)
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Path setup - allow importing from the service src directory
# ---------------------------------------------------------------------------
_SERVICE_ROOT = Path(__file__).resolve().parents[2] / "src"
_LIBS_ROOT = Path(__file__).resolve().parents[4] / "libs" / "http-shared" / "src"
sys.path.insert(0, str(_SERVICE_ROOT))
sys.path.insert(0, str(_LIBS_ROOT))

from decision.credential_resolver import (  # noqa: E402
    AtlassianCredential,
    CredentialResolver,
    CredentialScopeViolationError,
    DeptBotRow,
)
from http_shared.auth_inject import CredentialResolutionError  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeVaultClient:
    """In-memory Vault stub for testing."""

    secrets: dict[str, dict[str, str]] = field(default_factory=dict)

    async def read_secret(self, path: str) -> dict[str, str] | None:
        return self.secrets.get(path)


class _FakeConnection:
    """Mimics asyncpg connection for fetchrow/fetch."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        return self._rows

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        dept_id = args[0] if len(args) > 0 else None
        service = args[1] if len(args) > 1 else None
        for row in self._rows:
            if row.get("department_id") == dept_id and row.get("service") == service:
                return row
        return None


class _FakePool:
    """Mimics asyncpg.Pool with acquire() context manager."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._conn = _FakeConnection(rows)

    def acquire(self) -> "_FakePoolAcquire":
        return _FakePoolAcquire(self._conn)


class _FakePoolAcquire:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_DEPT_BOT_ROWS = [
    {
        "id": 1,
        "department_id": "payment",
        "service": "jira",
        "credential_ref": "secret/data/bots/payment/jira",
        "account_id": "bot-001",
        "username": "payment-bot",
        "deployment": "cloud",
    },
    {
        "id": 2,
        "department_id": "payment",
        "service": "bitbucket",
        "credential_ref": "secret/data/bots/payment/bitbucket",
        "account_id": "bot-002",
        "username": "payment-bot-bb",
        "deployment": "cloud",
    },
]


def _make_resolver(
    vault_secrets: dict[str, dict[str, str]] | None = None,
    db_rows: list[dict[str, Any]] | None = None,
) -> CredentialResolver:
    vault = _FakeVaultClient(secrets=vault_secrets or {})
    db = _FakePool(rows=db_rows or _DEPT_BOT_ROWS)
    return CredentialResolver(vault=vault, db=db)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_happy_path() -> None:
    """Successful credential resolution returns AtlassianCredential."""
    resolver = _make_resolver(
        vault_secrets={
            "secret/data/bots/payment/jira": {
                "url": "https://mycompany.atlassian.net",
                "username": "bot@company.com",
                "personal_token": "ATATT3xFfGF0...",
            }
        }
    )

    cred = await resolver.get("payment", "jira")

    assert isinstance(cred, AtlassianCredential)
    assert cred.url == "https://mycompany.atlassian.net"
    assert cred.username == "bot@company.com"
    assert cred.personal_token == "ATATT3xFfGF0..."


@pytest.mark.asyncio
async def test_get_missing_bot_registration() -> None:
    """CredentialResolutionError when no bot row exists for dept+service."""
    resolver = _make_resolver(db_rows=[])

    with pytest.raises(CredentialResolutionError) as exc_info:
        await resolver.get("nonexistent", "jira")

    assert "nonexistent" in str(exc_info.value)
    assert exc_info.value.dept_id == "nonexistent"
    assert exc_info.value.service == "jira"


@pytest.mark.asyncio
async def test_get_vault_404() -> None:
    """CredentialResolutionError when Vault path returns None (404)."""
    resolver = _make_resolver(vault_secrets={})  # no secrets at all

    with pytest.raises(CredentialResolutionError) as exc_info:
        await resolver.get("payment", "jira")

    assert "not found" in str(exc_info.value).lower()
    assert exc_info.value.dept_id == "payment"
    assert exc_info.value.service == "jira"


@pytest.mark.asyncio
async def test_get_incomplete_secret() -> None:
    """CredentialResolutionError when secret is missing required fields."""
    resolver = _make_resolver(
        vault_secrets={
            "secret/data/bots/payment/jira": {
                "url": "https://mycompany.atlassian.net",
                "username": "",  # empty  incomplete
                "personal_token": "token123",
            }
        }
    )

    with pytest.raises(CredentialResolutionError) as exc_info:
        await resolver.get("payment", "jira")

    assert "incomplete" in str(exc_info.value).lower()
    assert "username" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_get_invalid_scope() -> None:
    """ValueError when scope is not one of {'org', 'user', 'bot' (deprecated)}."""
    resolver = _make_resolver()

    with pytest.raises(ValueError, match="scope must be 'org' or 'user'"):
        await resolver.get("payment", "jira", scope="superuser")


@pytest.mark.asyncio
async def test_get_user_scope_requires_session_id() -> None:
    """``scope='user'`` rejects an empty / missing session_id (uyumluluk Q7)."""
    resolver = _make_resolver()

    with pytest.raises(ValueError, match="session_id"):
        await resolver.get("payment", "jira", scope="user")

    with pytest.raises(ValueError, match="session_id"):
        await resolver.get("payment", "jira", scope="user", session_id="")


@pytest.mark.asyncio
async def test_get_user_scope_reads_session_path_only() -> None:
    """``scope='user'`` reads ``secret/atlassian/_user_session/...`` only.

    The resolver MUST NOT consult ``automation.department_bots`` (which
    holds org-shaped credential_refs) when resolving a user scope, even
    if a row exists for the (dept_id, service) pair. The DB rows from
    ``_DEPT_BOT_ROWS`` would point at ``secret/data/bots/payment/jira``
    - a non-user path - and reading from there would be a cross-scope
    leak (uyumluluk Q7 R2.3).
    """
    user_path = "secret/atlassian/_user_session/sess-abc/jira"
    resolver = _make_resolver(
        vault_secrets={
            user_path: {
                "url": "https://mycompany.atlassian.net",
                "username": "alice@example.com",
                "personal_token": "user-pat",
            }
        }
    )

    cred = await resolver.get(
        "payment", "jira", scope="user", session_id="sess-abc"
    )

    assert cred.url == "https://mycompany.atlassian.net"
    assert cred.username == "alice@example.com"
    assert cred.personal_token == "user-pat"


@pytest.mark.asyncio
async def test_get_user_scope_does_not_touch_org_path() -> None:
    """``scope='user'`` never reads from the org-default Vault prefix.

    We seed an org-shaped path (``secret/data/bots/payment/jira``)
    matching ``_DEPT_BOT_ROWS[0].credential_ref`` and verify that a
    user-scope ``get`` call ignores it: it must look up the user
    session path, fail with :class:`CredentialResolutionError` (404),
    and never observe the org payload.
    """
    org_path = "secret/data/bots/payment/jira"
    resolver = _make_resolver(
        vault_secrets={
            # Org-shaped seed; resolver MUST NOT touch it for scope=user.
            org_path: {
                "url": "https://org.atlassian.net",
                "username": "bot",
                "personal_token": "org-token",
            }
        }
    )

    with pytest.raises(CredentialResolutionError) as exc_info:
        await resolver.get(
            "payment", "jira", scope="user", session_id="sess-xyz"
        )

    # The error message names the user-session path, never the org one.
    assert "_user_session/sess-xyz" in str(exc_info.value)
    assert org_path not in str(exc_info.value)


@pytest.mark.asyncio
async def test_get_org_scope_rejects_leaked_user_credential_ref() -> None:
    """A leaked ``_user_session`` credential_ref triggers a violation.

    If ``automation.department_bots.credential_ref`` somehow points at
    a per-user path (data corruption, faulty migration), the resolver
    MUST refuse the read and raise
    :class:`CredentialScopeViolationError` rather than silently
    serving user-scoped material to a worker bot caller.
    """
    leaked_rows = [
        {
            "id": 99,
            "department_id": "payment",
            "service": "jira",
            # Leaked: should never appear in this column.
            "credential_ref": "secret/atlassian/_user_session/sess-xyz/jira",
            "account_id": "bot-99",
            "username": "bot",
            "deployment": "cloud",
        }
    ]
    resolver = _make_resolver(db_rows=leaked_rows)

    with pytest.raises(CredentialScopeViolationError) as exc_info:
        await resolver.get("payment", "jira", scope="org")

    assert exc_info.value.scope == "org"
    assert "_user_session" in exc_info.value.attempted_path
    assert exc_info.value.dept_id == "payment"
    assert exc_info.value.service == "jira"


@pytest.mark.asyncio
async def test_get_bot_scope_alias_routes_to_org() -> None:
    """``scope='bot'`` is silently routed to ``'org'`` (deprecated alias)."""
    resolver = _make_resolver(
        vault_secrets={
            "secret/data/bots/payment/jira": {
                "url": "https://mycompany.atlassian.net",
                "username": "bot",
                "personal_token": "org-token",
            }
        }
    )

    cred = await resolver.get("payment", "jira", scope="bot")

    assert cred.personal_token == "org-token"


@pytest.mark.asyncio
async def test_scope_violation_writes_audit_when_logger_provided() -> None:
    """Cross-scope reads emit ``credential_scope_violation_attempt``."""

    class _CapturingAudit:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def write(self, event: Any) -> None:
            self.events.append(event)

    audit = _CapturingAudit()
    leaked_rows = [
        {
            "id": 99,
            "department_id": "payment",
            "service": "jira",
            "credential_ref": "secret/atlassian/_user_session/leaked/jira",
            "account_id": None,
            "username": None,
            "deployment": "cloud",
        }
    ]
    vault = _FakeVaultClient(secrets={})
    db = _FakePool(rows=leaked_rows)
    resolver = CredentialResolver(vault=vault, db=db, audit_logger=audit)  # type: ignore[arg-type]

    with pytest.raises(CredentialScopeViolationError):
        await resolver.get("payment", "jira", scope="org")

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.action == "credential_scope_violation_attempt"
    assert event.actor_role == "system"
    assert event.result == "denied"
    assert event.payload is not None
    assert event.payload["scope"] == "org"
    assert event.payload["service"] == "jira"
    assert "_user_session" in event.payload["attempted_path"]


@pytest.mark.asyncio
async def test_list_dept_bots() -> None:
    """list_dept_bots returns all rows as DeptBotRow dataclasses."""
    resolver = _make_resolver()

    bots = await resolver.list_dept_bots()

    assert len(bots) == 2
    assert all(isinstance(b, DeptBotRow) for b in bots)
    assert bots[0].department_id == "payment"
    assert bots[0].service == "jira"
    assert bots[1].service == "bitbucket"


@pytest.mark.asyncio
async def test_list_dept_bots_caching() -> None:
    """list_dept_bots caches after first call (same list returned)."""
    resolver = _make_resolver()

    first = await resolver.list_dept_bots()
    second = await resolver.list_dept_bots()

    assert first is second  # same object reference  cached


@pytest.mark.asyncio
async def test_get_missing_personal_token_field() -> None:
    """CredentialResolutionError when personal_token key is absent."""
    resolver = _make_resolver(
        vault_secrets={
            "secret/data/bots/payment/jira": {
                "url": "https://mycompany.atlassian.net",
                "username": "bot@company.com",
                # personal_token missing entirely
            }
        }
    )

    with pytest.raises(CredentialResolutionError) as exc_info:
        await resolver.get("payment", "jira")

    assert "personal_token" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# TTL cache + drift detection (uyumluluk R15 / Q17)
# ---------------------------------------------------------------------------


from datetime import datetime, timedelta, timezone  # noqa: E402

from decision.credential_resolver import (  # noqa: E402
    CachedEntry,
    _CACHE_TTL,
)


@dataclass
class _MetadataVaultClient:
    """Vault stub that surfaces ``read_with_metadata`` for drift tests.

    Exposes a mutable ``versions`` map keyed by Vault path so tests
    can simulate a rotation by bumping the ``created_time`` between
    resolver calls. Tracks every read for assertion.
    """

    versions: dict[str, tuple[dict[str, str], str]] = field(default_factory=dict)
    read_calls: list[str] = field(default_factory=list)

    async def read_secret(self, path: str) -> dict[str, str] | None:
        self.read_calls.append(path)
        entry = self.versions.get(path)
        return entry[0] if entry is not None else None

    async def read_with_metadata(
        self, path: str
    ) -> tuple[dict[str, str], dict[str, Any]] | None:
        self.read_calls.append(path)
        entry = self.versions.get(path)
        if entry is None:
            return None
        data, created_time = entry
        return data, {"created_time": created_time, "version": 1}


class _CapturingAuditLogger:
    """Minimal audit logger that records every event written."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def write(self, event: Any) -> None:
        self.events.append(event)


def _build_clock(start: datetime) -> tuple[Any, Any]:
    """Return ``(clock, advance)`` - clock callable + helper to advance it."""
    state = {"now": start}

    def clock() -> datetime:
        return state["now"]

    def advance(delta: timedelta) -> None:
        state["now"] = state["now"] + delta

    return clock, advance


@pytest.mark.asyncio
async def test_cache_hit_within_ttl_skips_vault() -> None:
    """A second ``get(...)`` within TTL must NOT re-read Vault."""
    org_path = "secret/data/bots/payment/jira"
    vault = _MetadataVaultClient(
        versions={
            org_path: (
                {
                    "url": "https://mycompany.atlassian.net",
                    "username": "bot",
                    "personal_token": "v1-token",
                },
                "2025-01-01T12:00:00.000000Z",
            )
        }
    )
    db = _FakePool(rows=_DEPT_BOT_ROWS)
    clock, advance = _build_clock(
        datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    )
    resolver = CredentialResolver(
        vault=vault, db=db, clock=clock  # type: ignore[arg-type]
    )

    first = await resolver.get("payment", "jira")
    advance(_CACHE_TTL - timedelta(seconds=1))  # one second before expiry
    second = await resolver.get("payment", "jira")

    assert first == second
    # Vault was read exactly once; the second call hit the cache.
    assert vault.read_calls == [org_path]


@pytest.mark.asyncio
async def test_cache_miss_after_ttl_re_reads_vault() -> None:
    """Once TTL expires, the resolver re-reads Vault."""
    org_path = "secret/data/bots/payment/jira"
    vault = _MetadataVaultClient(
        versions={
            org_path: (
                {
                    "url": "https://mycompany.atlassian.net",
                    "username": "bot",
                    "personal_token": "v1-token",
                },
                "2025-01-01T12:00:00.000000Z",
            )
        }
    )
    db = _FakePool(rows=_DEPT_BOT_ROWS)
    clock, advance = _build_clock(
        datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    )
    resolver = CredentialResolver(
        vault=vault, db=db, clock=clock  # type: ignore[arg-type]
    )

    await resolver.get("payment", "jira")
    advance(_CACHE_TTL + timedelta(seconds=1))  # past TTL
    await resolver.get("payment", "jira")

    assert vault.read_calls == [org_path, org_path]


@pytest.mark.asyncio
async def test_drift_detection_emits_audit_on_rotation() -> None:
    """Refresh with newer ``created_time`` writes ``vault_credential_refreshed``."""
    org_path = "secret/data/bots/payment/jira"
    secret_v1 = {
        "url": "https://mycompany.atlassian.net",
        "username": "bot",
        "personal_token": "v1-token",
    }
    secret_v2 = {**secret_v1, "personal_token": "v2-token"}
    vault = _MetadataVaultClient(
        versions={org_path: (secret_v1, "2025-01-01T12:00:00.000000Z")}
    )
    db = _FakePool(rows=_DEPT_BOT_ROWS)
    audit = _CapturingAuditLogger()
    clock, advance = _build_clock(
        datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    )
    resolver = CredentialResolver(
        vault=vault,  # type: ignore[arg-type]
        db=db,
        audit_logger=audit,
        clock=clock,
    )

    # First read populates cache with v1 metadata.
    first = await resolver.get("payment", "jira")
    assert first.personal_token == "v1-token"
    assert audit.events == []

    # Simulate an out-of-band rotation: bump the Vault created_time.
    vault.versions[org_path] = (secret_v2, "2025-01-01T13:00:00.000000Z")
    advance(_CACHE_TTL + timedelta(seconds=1))

    # Refresh: drift detected  audit emitted.
    second = await resolver.get("payment", "jira")
    assert second.personal_token == "v2-token"
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.action == "vault_credential_refreshed"
    assert event.actor_role == "system"
    assert event.result == "ok"
    assert event.payload["scope"] == "org"
    assert event.payload["dept_id"] == "payment"
    assert event.payload["service"] == "jira"
    assert event.payload["prev_created_time"] == "2025-01-01T12:00:00.000000Z"
    assert event.payload["new_created_time"] == "2025-01-01T13:00:00.000000Z"


@pytest.mark.asyncio
async def test_drift_detection_silent_when_created_time_unchanged() -> None:
    """No audit when ``created_time`` is identical across refreshes."""
    org_path = "secret/data/bots/payment/jira"
    vault = _MetadataVaultClient(
        versions={
            org_path: (
                {
                    "url": "https://mycompany.atlassian.net",
                    "username": "bot",
                    "personal_token": "v1-token",
                },
                "2025-01-01T12:00:00.000000Z",
            )
        }
    )
    db = _FakePool(rows=_DEPT_BOT_ROWS)
    audit = _CapturingAuditLogger()
    clock, advance = _build_clock(
        datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    )
    resolver = CredentialResolver(
        vault=vault,  # type: ignore[arg-type]
        db=db,
        audit_logger=audit,
        clock=clock,
    )

    await resolver.get("payment", "jira")
    advance(_CACHE_TTL + timedelta(seconds=1))
    await resolver.get("payment", "jira")

    # Same created_time  no rotation event.
    assert audit.events == []


@pytest.mark.asyncio
async def test_cache_keys_separate_org_and_user_scopes() -> None:
    """``(scope, id, service)`` triples never collide across scopes."""
    org_path = "secret/data/bots/payment/jira"
    user_path = "secret/atlassian/_user_session/sess-abc/jira"
    vault = _MetadataVaultClient(
        versions={
            org_path: (
                {
                    "url": "https://org.atlassian.net",
                    "username": "bot",
                    "personal_token": "org-token",
                },
                "2025-01-01T12:00:00.000000Z",
            ),
            user_path: (
                {
                    "url": "https://user.atlassian.net",
                    "username": "alice",
                    "personal_token": "user-token",
                },
                "2025-01-01T12:00:00.000000Z",
            ),
        }
    )
    db = _FakePool(rows=_DEPT_BOT_ROWS)
    clock, _ = _build_clock(
        datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    )
    resolver = CredentialResolver(
        vault=vault, db=db, clock=clock  # type: ignore[arg-type]
    )

    org_cred = await resolver.get("payment", "jira", scope="org")
    user_cred = await resolver.get(
        "payment", "jira", scope="user", session_id="sess-abc"
    )

    assert org_cred.personal_token == "org-token"
    assert user_cred.personal_token == "user-token"
    # Both reads happened exactly once at distinct paths.
    assert sorted(vault.read_calls) == sorted([org_path, user_path])


@pytest.mark.asyncio
async def test_cached_entry_dataclass_shape() -> None:
    """``CachedEntry`` exposes the three public fields the design lists."""
    entry = CachedEntry(
        credential=AtlassianCredential(
            url="u", username="n", personal_token="t"
        ),
        cached_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        vault_created_time="2025-01-01T00:00:00Z",
    )

    assert entry.credential.personal_token == "t"
    assert entry.cached_at.year == 2025
    assert entry.vault_created_time == "2025-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_legacy_vault_without_metadata_disables_drift_audit() -> None:
    """Backends without ``read_with_metadata`` still cache but skip drift."""
    # _FakeVaultClient defined at the top of this file does NOT
    # expose ``read_with_metadata`` - it mirrors the legacy
    # protocol surface.
    vault = _FakeVaultClient(
        secrets={
            "secret/data/bots/payment/jira": {
                "url": "https://mycompany.atlassian.net",
                "username": "bot",
                "personal_token": "v1-token",
            }
        }
    )
    db = _FakePool(rows=_DEPT_BOT_ROWS)
    audit = _CapturingAuditLogger()
    clock, advance = _build_clock(
        datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    )
    resolver = CredentialResolver(
        vault=vault,  # type: ignore[arg-type]
        db=db,
        audit_logger=audit,
        clock=clock,
    )

    # First read populates cache with no metadata.
    await resolver.get("payment", "jira")
    advance(_CACHE_TTL + timedelta(seconds=1))
    # Refresh succeeds; no drift audit because metadata is unavailable.
    await resolver.get("payment", "jira")

    assert audit.events == []
