"""Property test: Worker Vault TTL Cache + Drift Detection.

For any clock advance sequence ``dt = (t0, t1, ..., tN)`` and Vault
change event sequence, ``CredentialResolver.get(...)`` behaviour:

- **TTL cache invariant**: Within ``(now - cached_at) < 300s``, no
  Vault ``read`` call is made for the same cache key.
- **TTL expiration**: After ``(now - cached_at) >= 300s``, the first
  call triggers exactly one ``read_with_metadata`` call; cache is
  refreshed.
- **Drift detection**: On refresh, if ``fresh.created_time >
  cached.vault_created_time``, a ``vault_credential_refreshed`` audit
  is emitted.

The injectable ``clock`` parameter on ``CredentialResolver.__init__``
is used to advance time deterministically — no freezegun/time-machine
dependency required.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap
# ---------------------------------------------------------------------------

_AUTOMATION_ROOT = Path(__file__).resolve().parents[2]
if str(_AUTOMATION_ROOT) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_ROOT))

from src.decision.credential_resolver import (  # noqa: E402
    AtlassianCredential,
    CredentialResolver,
    _CACHE_TTL,
)

# ---------------------------------------------------------------------------
# Hypothesis profile
# ---------------------------------------------------------------------------

_PROFILE = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TTL_SECONDS: int = int(_CACHE_TTL.total_seconds())  # 300

_T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

_SERVICES = ("jira", "confluence", "bitbucket")
_DEPT_IDS = ("payment", "platform", "hr")
_SESSION_IDS = ("sess-aaa", "sess-bbb", "sess-ccc")

# ---------------------------------------------------------------------------
# Controllable clock
# ---------------------------------------------------------------------------


class _AdvancingClock:
    """A clock whose current time can be set explicitly.

    Passed as the ``clock`` parameter to ``CredentialResolver`` so
    tests can advance time without any monkey-patching.
    """

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def set(self, t: datetime) -> None:
        self._now = t

# ---------------------------------------------------------------------------
# Fake Vault client
# ---------------------------------------------------------------------------


@dataclass
class _VaultEntry:
    """A single Vault secret version."""

    url: str
    username: str
    personal_token: str
    created_time: str  # ISO-8601 string


class _FakeVaultClient:
    """In-memory Vault KV-v2 fake.

    Supports ``read_with_metadata`` (returns data + metadata dict) and
    ``read_secret`` (returns data only). Tracks call counts per path so
    tests can assert Vault was or was not consulted.
    """

    def __init__(self) -> None:
        # path → current entry
        self._store: dict[str, _VaultEntry] = {}
        # path → number of read_with_metadata calls
        self.read_with_metadata_calls: dict[str, int] = {}
        # path → number of read_secret calls
        self.read_secret_calls: dict[str, int] = {}

    def set_secret(self, path: str, entry: _VaultEntry) -> None:
        self._store[path] = entry

    def total_reads(self, path: str) -> int:
        return self.read_with_metadata_calls.get(
            path, 0
        ) + self.read_secret_calls.get(path, 0)

    async def read_with_metadata(
        self, path: str
    ) -> tuple[dict[str, str], dict[str, Any]] | None:
        self.read_with_metadata_calls[path] = (
            self.read_with_metadata_calls.get(path, 0) + 1
        )
        entry = self._store.get(path)
        if entry is None:
            return None
        data = {
            "url": entry.url,
            "username": entry.username,
            "personal_token": entry.personal_token,
        }
        metadata = {"created_time": entry.created_time}
        return data, metadata

    async def read_secret(self, path: str) -> dict[str, str] | None:
        self.read_secret_calls[path] = self.read_secret_calls.get(path, 0) + 1
        entry = self._store.get(path)
        if entry is None:
            return None
        return {
            "url": entry.url,
            "username": entry.username,
            "personal_token": entry.personal_token,
        }

# ---------------------------------------------------------------------------
# Fake asyncpg pool (org scope needs a DB lookup for credential_ref)
# ---------------------------------------------------------------------------


class _FakeConnection:
    def __init__(self, dept_id: str, service: str, vault_path: str) -> None:
        self._dept_id = dept_id
        self._service = service
        self._vault_path = vault_path

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        q = " ".join(query.split()).lower()
        if "credential_ref" in q and "department_bots" in q:
            dept_id, service = args[0], args[1]
            if dept_id == self._dept_id and service == self._service:
                return {"credential_ref": self._vault_path}
        return None


class _FakeAcquireCtx:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *_: object) -> bool:
        return False


class _FakePool:
    def __init__(self, dept_id: str, service: str, vault_path: str) -> None:
        self._conn = _FakeConnection(dept_id, service, vault_path)

    def acquire(self) -> _FakeAcquireCtx:
        return _FakeAcquireCtx(self._conn)


# ---------------------------------------------------------------------------
# Fake audit logger
# ---------------------------------------------------------------------------


@dataclass
class _AuditRecord:
    action: str
    payload: dict[str, Any]


class _FakeAuditLogger:
    def __init__(self) -> None:
        self.events: list[_AuditRecord] = []

    async def write(self, event: Any) -> None:
        self.events.append(
            _AuditRecord(action=event.action, payload=event.payload or {})
        )

    def refreshed_events(self) -> list[_AuditRecord]:
        return [e for e in self.events if e.action == "vault_credential_refreshed"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_created_time(base: datetime, delta_seconds: int = 0) -> str:
    """Return an ISO-8601 UTC string offset from *base* by *delta_seconds*."""
    t = base + timedelta(seconds=delta_seconds)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")


def _build_org_resolver(
    *,
    dept_id: str,
    service: str,
    vault: _FakeVaultClient,
    clock: _AdvancingClock,
    audit: _FakeAuditLogger | None = None,
) -> tuple[CredentialResolver, str]:
    """Return a resolver wired for org scope + the expected Vault path."""
    vault_path = f"secret/atlassian/{dept_id}/{service}"
    pool = _FakePool(dept_id, service, vault_path)
    resolver = CredentialResolver(vault, pool, audit, clock=clock)
    return resolver, vault_path


def _build_user_resolver(
    *,
    session_id: str,
    service: str,
    vault: _FakeVaultClient,
    clock: _AdvancingClock,
    audit: _FakeAuditLogger | None = None,
) -> tuple[CredentialResolver, str]:
    """Return a resolver wired for user scope + the expected Vault path."""
    vault_path = f"secret/atlassian/_user_session/{session_id}/{service}"
    # user scope bypasses DB; pool is never consulted — pass a dummy pool
    pool = _FakePool("_unused", "_unused", "_unused")
    resolver = CredentialResolver(vault, pool, audit, clock=clock)
    return resolver, vault_path


def _seed_vault(
    vault: _FakeVaultClient,
    path: str,
    *,
    created_offset: int = 0,
    base: datetime = _T0,
) -> _VaultEntry:
    entry = _VaultEntry(
        url="https://example.atlassian.net",
        username="bot-user",
        personal_token="tok-abc123",
        created_time=_make_created_time(base, created_offset),
    )
    vault.set_secret(path, entry)
    return entry

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

# Advance within TTL: [1, TTL-1] seconds
_within_ttl = st.integers(min_value=1, max_value=_TTL_SECONDS - 1)

# Advance past TTL: [TTL, TTL*3] seconds
_past_ttl = st.integers(min_value=_TTL_SECONDS, max_value=_TTL_SECONDS * 3)

# Number of repeated calls within TTL window
_call_count = st.integers(min_value=2, max_value=10)

# Vault created_time delta (seconds) for rotation simulation
_rotation_delta = st.integers(min_value=1, max_value=3600)

_dept_id_st = st.sampled_from(_DEPT_IDS)
_service_st = st.sampled_from(_SERVICES)
_session_id_st = st.sampled_from(_SESSION_IDS)
_scope_st = st.sampled_from(["org", "user"])

# ---------------------------------------------------------------------------
# TTL cache invariant (no Vault hit within TTL)
# ---------------------------------------------------------------------------


class TestTTLCacheInvariant:
    """TTL cache invariant: no Vault read within the TTL window."""

    @_PROFILE
    @given(
        dept_id=_dept_id_st,
        service=_service_st,
        advance_seconds=_within_ttl,
        extra_calls=_call_count,
    )
    @pytest.mark.asyncio
    async def test_no_vault_read_within_ttl_org_scope(
        self,
        dept_id: str,
        service: str,
        advance_seconds: int,
        extra_calls: int,
    ) -> None:
        """Org-scope: repeated calls within TTL never hit Vault again."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        resolver, path = _build_org_resolver(
            dept_id=dept_id, service=service, vault=vault, clock=clock
        )
        _seed_vault(vault, path)

        # First call — populates cache (1 Vault read expected)
        await resolver.get(dept_id, service, "org")
        reads_after_first = vault.total_reads(path)
        assert reads_after_first == 1, (
            f"Expected exactly 1 Vault read after first call, got {reads_after_first}"
        )

        # Advance time within TTL
        clock.advance(advance_seconds)

        # Subsequent calls — must NOT hit Vault
        for _ in range(extra_calls):
            await resolver.get(dept_id, service, "org")

        total_reads = vault.total_reads(path)
        assert total_reads == 1, (
            f"Expected 1 total Vault read (cache hit), got {total_reads}. "
            f"advance_seconds={advance_seconds}, extra_calls={extra_calls}"
        )

    @_PROFILE
    @given(
        session_id=_session_id_st,
        service=_service_st,
        advance_seconds=_within_ttl,
        extra_calls=_call_count,
    )
    @pytest.mark.asyncio
    async def test_no_vault_read_within_ttl_user_scope(
        self,
        session_id: str,
        service: str,
        advance_seconds: int,
        extra_calls: int,
    ) -> None:
        """User-scope: repeated calls within TTL never hit Vault again."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        resolver, path = _build_user_resolver(
            session_id=session_id, service=service, vault=vault, clock=clock
        )
        _seed_vault(vault, path)

        # First call — populates cache
        await resolver.get("_any_dept", service, "user", session_id=session_id)
        assert vault.total_reads(path) == 1

        # Advance within TTL
        clock.advance(advance_seconds)

        for _ in range(extra_calls):
            await resolver.get("_any_dept", service, "user", session_id=session_id)

        assert vault.total_reads(path) == 1, (
            f"Cache should have served {extra_calls} calls without Vault reads. "
            f"advance_seconds={advance_seconds}"
        )

    @_PROFILE
    @given(
        dept_id=_dept_id_st,
        service=_service_st,
        advance_seconds=_within_ttl,
    )
    @pytest.mark.asyncio
    async def test_cached_credential_matches_vault_value(
        self,
        dept_id: str,
        service: str,
        advance_seconds: int,
    ) -> None:
        """Cached credential is identical to the value originally read from Vault."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        resolver, path = _build_org_resolver(
            dept_id=dept_id, service=service, vault=vault, clock=clock
        )
        entry = _seed_vault(vault, path)

        first = await resolver.get(dept_id, service, "org")
        clock.advance(advance_seconds)
        second = await resolver.get(dept_id, service, "org")

        assert first == second, "Cached credential must equal the first resolved value"
        assert first.url == entry.url
        assert first.username == entry.username
        assert first.personal_token == entry.personal_token

# ---------------------------------------------------------------------------
# TTL expiration triggers exactly one Vault read
# ---------------------------------------------------------------------------


class TestTTLExpiration:
    """TTL expiration: Vault is re-read after TTL expires."""

    @_PROFILE
    @given(
        dept_id=_dept_id_st,
        service=_service_st,
        advance_seconds=_past_ttl,
    )
    @pytest.mark.asyncio
    async def test_vault_read_after_ttl_expiry_org_scope(
        self,
        dept_id: str,
        service: str,
        advance_seconds: int,
    ) -> None:
        """Org-scope: exactly one Vault read occurs after TTL expires."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        resolver, path = _build_org_resolver(
            dept_id=dept_id, service=service, vault=vault, clock=clock
        )
        _seed_vault(vault, path)

        # First call — populates cache
        await resolver.get(dept_id, service, "org")
        assert vault.total_reads(path) == 1

        # Advance past TTL
        clock.advance(advance_seconds)

        # Call after TTL — must trigger exactly one more Vault read
        await resolver.get(dept_id, service, "org")
        assert vault.total_reads(path) == 2, (
            f"Expected 2 total Vault reads (initial + post-TTL refresh), "
            f"got {vault.total_reads(path)}. advance_seconds={advance_seconds}"
        )

    @_PROFILE
    @given(
        session_id=_session_id_st,
        service=_service_st,
        advance_seconds=_past_ttl,
    )
    @pytest.mark.asyncio
    async def test_vault_read_after_ttl_expiry_user_scope(
        self,
        session_id: str,
        service: str,
        advance_seconds: int,
    ) -> None:
        """User-scope: exactly one Vault read occurs after TTL expires."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        resolver, path = _build_user_resolver(
            session_id=session_id, service=service, vault=vault, clock=clock
        )
        _seed_vault(vault, path)

        await resolver.get("_dept", service, "user", session_id=session_id)
        assert vault.total_reads(path) == 1

        clock.advance(advance_seconds)

        await resolver.get("_dept", service, "user", session_id=session_id)
        assert vault.total_reads(path) == 2, (
            f"Expected 2 Vault reads after TTL expiry, got {vault.total_reads(path)}"
        )

    @_PROFILE
    @given(
        dept_id=_dept_id_st,
        service=_service_st,
        advance_seconds=_past_ttl,
        extra_calls=_call_count,
    )
    @pytest.mark.asyncio
    async def test_no_extra_vault_reads_after_refresh(
        self,
        dept_id: str,
        service: str,
        advance_seconds: int,
        extra_calls: int,
    ) -> None:
        """After TTL refresh, subsequent calls within new TTL window don't hit Vault."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        resolver, path = _build_org_resolver(
            dept_id=dept_id, service=service, vault=vault, clock=clock
        )
        _seed_vault(vault, path)

        # Populate cache
        await resolver.get(dept_id, service, "org")
        # Expire TTL
        clock.advance(advance_seconds)
        # Refresh
        await resolver.get(dept_id, service, "org")
        reads_after_refresh = vault.total_reads(path)
        assert reads_after_refresh == 2

        # Advance a small amount within the new TTL window
        clock.advance(1)

        # These calls should all be cache hits
        for _ in range(extra_calls):
            await resolver.get(dept_id, service, "org")

        assert vault.total_reads(path) == 2, (
            f"Expected no additional Vault reads after refresh within new TTL. "
            f"extra_calls={extra_calls}"
        )

# ---------------------------------------------------------------------------
# Drift detection: audit emitted when created_time advances
# ---------------------------------------------------------------------------


class TestDriftDetection:
    """Drift detection: audit emitted on credential rotation."""

    @_PROFILE
    @given(
        dept_id=_dept_id_st,
        service=_service_st,
        advance_seconds=_past_ttl,
        rotation_delta=_rotation_delta,
    )
    @pytest.mark.asyncio
    async def test_audit_emitted_when_vault_created_time_advances(
        self,
        dept_id: str,
        service: str,
        advance_seconds: int,
        rotation_delta: int,
    ) -> None:
        """vault_credential_refreshed audit is emitted when created_time increases."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        audit = _FakeAuditLogger()
        resolver, path = _build_org_resolver(
            dept_id=dept_id, service=service, vault=vault, clock=clock, audit=audit
        )

        # Seed initial version
        initial_entry = _seed_vault(vault, path, created_offset=0)

        # First call — populates cache, no drift yet
        await resolver.get(dept_id, service, "org")
        assert len(audit.refreshed_events()) == 0, (
            "No drift audit expected on first (cold) cache population"
        )

        # Simulate Vault rotation: update secret with newer created_time
        rotated_entry = _VaultEntry(
            url=initial_entry.url,
            username=initial_entry.username,
            personal_token="tok-rotated-xyz",
            created_time=_make_created_time(_T0, rotation_delta),
        )
        vault.set_secret(path, rotated_entry)

        # Advance past TTL so cache expires
        clock.advance(advance_seconds)

        # Refresh call — should detect drift and emit audit
        await resolver.get(dept_id, service, "org")

        refreshed = audit.refreshed_events()
        assert len(refreshed) == 1, (
            f"Expected exactly 1 vault_credential_refreshed audit, "
            f"got {len(refreshed)}. advance_seconds={advance_seconds}, "
            f"rotation_delta={rotation_delta}"
        )

        ev = refreshed[0]
        assert ev.payload["scope"] == "org"
        assert ev.payload["dept_id"] == dept_id
        assert ev.payload["service"] == service
        assert ev.payload["prev_created_time"] == initial_entry.created_time
        assert ev.payload["new_created_time"] == rotated_entry.created_time

    @_PROFILE
    @given(
        dept_id=_dept_id_st,
        service=_service_st,
        advance_seconds=_past_ttl,
    )
    @pytest.mark.asyncio
    async def test_no_audit_when_created_time_unchanged(
        self,
        dept_id: str,
        service: str,
        advance_seconds: int,
    ) -> None:
        """No drift audit when Vault secret has not been rotated."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        audit = _FakeAuditLogger()
        resolver, path = _build_org_resolver(
            dept_id=dept_id, service=service, vault=vault, clock=clock, audit=audit
        )

        _seed_vault(vault, path, created_offset=0)

        # First call
        await resolver.get(dept_id, service, "org")

        # Advance past TTL — but Vault secret is unchanged
        clock.advance(advance_seconds)

        # Refresh call — same created_time, no drift
        await resolver.get(dept_id, service, "org")

        assert len(audit.refreshed_events()) == 0, (
            "No drift audit expected when Vault created_time is unchanged"
        )

    @_PROFILE
    @given(
        dept_id=_dept_id_st,
        service=_service_st,
        advance_seconds=_past_ttl,
    )
    @pytest.mark.asyncio
    async def test_no_audit_on_first_cold_cache_population(
        self,
        dept_id: str,
        service: str,
        advance_seconds: int,
    ) -> None:
        """No drift audit on the very first Vault read (no prior cached entry)."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        audit = _FakeAuditLogger()
        resolver, path = _build_org_resolver(
            dept_id=dept_id, service=service, vault=vault, clock=clock, audit=audit
        )
        _seed_vault(vault, path, created_offset=0)

        # Cold start — no prior cache entry
        await resolver.get(dept_id, service, "org")

        assert len(audit.refreshed_events()) == 0, (
            "No drift audit expected on cold cache population (no prior entry)"
        )

    @_PROFILE
    @given(
        session_id=_session_id_st,
        service=_service_st,
        advance_seconds=_past_ttl,
        rotation_delta=_rotation_delta,
    )
    @pytest.mark.asyncio
    async def test_drift_detection_user_scope(
        self,
        session_id: str,
        service: str,
        advance_seconds: int,
        rotation_delta: int,
    ) -> None:
        """User-scope drift detection also emits audit on created_time advance."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        audit = _FakeAuditLogger()
        resolver, path = _build_user_resolver(
            session_id=session_id, service=service, vault=vault, clock=clock,
            audit=audit,
        )

        initial_entry = _seed_vault(vault, path, created_offset=0)
        await resolver.get("_dept", service, "user", session_id=session_id)
        assert len(audit.refreshed_events()) == 0

        # Rotate
        rotated_entry = _VaultEntry(
            url=initial_entry.url,
            username=initial_entry.username,
            personal_token="tok-user-rotated",
            created_time=_make_created_time(_T0, rotation_delta),
        )
        vault.set_secret(path, rotated_entry)

        clock.advance(advance_seconds)
        await resolver.get("_dept", service, "user", session_id=session_id)

        refreshed = audit.refreshed_events()
        assert len(refreshed) == 1, (
            f"Expected 1 drift audit for user scope, got {len(refreshed)}"
        )
        assert refreshed[0].payload["scope"] == "user"
        assert refreshed[0].payload["service"] == service

# ---------------------------------------------------------------------------
# Cache key isolation: different keys don't interfere
# ---------------------------------------------------------------------------


class TestCacheKeyIsolation:
    """Cache key isolation: different (scope, id, service) are independent."""

    @_PROFILE
    @given(
        dept_id_a=st.sampled_from(["payment", "platform"]),
        dept_id_b=st.just("hr"),
        service=_service_st,
        advance_seconds=_within_ttl,
    )
    @pytest.mark.asyncio
    async def test_different_dept_ids_have_independent_caches(
        self,
        dept_id_a: str,
        dept_id_b: str,
        service: str,
        advance_seconds: int,
    ) -> None:
        """Two different dept_ids have independent cache entries."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()

        path_a = f"secret/atlassian/{dept_id_a}/{service}"
        path_b = f"secret/atlassian/{dept_id_b}/{service}"
        _seed_vault(vault, path_a)
        _seed_vault(vault, path_b)

        # Build a resolver that can serve both dept_ids
        # We need two separate resolvers (each has its own DB pool)
        pool_a = _FakePool(dept_id_a, service, path_a)
        pool_b = _FakePool(dept_id_b, service, path_b)
        resolver_a = CredentialResolver(vault, pool_a, clock=clock)
        resolver_b = CredentialResolver(vault, pool_b, clock=clock)

        # Populate cache for dept_a
        await resolver_a.get(dept_id_a, service, "org")
        assert vault.total_reads(path_a) == 1
        assert vault.total_reads(path_b) == 0

        # Advance within TTL
        clock.advance(advance_seconds)

        # dept_b first call should still hit Vault (cold cache)
        await resolver_b.get(dept_id_b, service, "org")
        assert vault.total_reads(path_b) == 1

        # dept_a should still be cached
        await resolver_a.get(dept_id_a, service, "org")
        assert vault.total_reads(path_a) == 1, (
            "dept_a cache should not have been invalidated by dept_b access"
        )

    @_PROFILE
    @given(
        dept_id=_dept_id_st,
        service_a=st.sampled_from(["jira", "confluence"]),
        service_b=st.just("bitbucket"),
        advance_seconds=_within_ttl,
    )
    @pytest.mark.asyncio
    async def test_different_services_have_independent_caches(
        self,
        dept_id: str,
        service_a: str,
        service_b: str,
        advance_seconds: int,
    ) -> None:
        """Two different services for the same dept have independent cache entries."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()

        path_a = f"secret/atlassian/{dept_id}/{service_a}"
        path_b = f"secret/atlassian/{dept_id}/{service_b}"
        _seed_vault(vault, path_a)
        _seed_vault(vault, path_b)

        pool_a = _FakePool(dept_id, service_a, path_a)
        pool_b = _FakePool(dept_id, service_b, path_b)
        resolver_a = CredentialResolver(vault, pool_a, clock=clock)
        resolver_b = CredentialResolver(vault, pool_b, clock=clock)

        await resolver_a.get(dept_id, service_a, "org")
        assert vault.total_reads(path_a) == 1
        assert vault.total_reads(path_b) == 0

        clock.advance(advance_seconds)

        await resolver_b.get(dept_id, service_b, "org")
        assert vault.total_reads(path_b) == 1

        # service_a still cached
        await resolver_a.get(dept_id, service_a, "org")
        assert vault.total_reads(path_a) == 1

# ---------------------------------------------------------------------------
# TTL boundary: exactly at TTL boundary
# ---------------------------------------------------------------------------


class TestTTLBoundary:
    """TTL boundary: behavior at exactly TTL seconds."""

    @pytest.mark.asyncio
    async def test_at_exactly_ttl_boundary_triggers_refresh(self) -> None:
        """At exactly TTL seconds elapsed, the cache is considered stale."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        dept_id, service = "payment", "jira"
        resolver, path = _build_org_resolver(
            dept_id=dept_id, service=service, vault=vault, clock=clock
        )
        _seed_vault(vault, path)

        await resolver.get(dept_id, service, "org")
        assert vault.total_reads(path) == 1

        # Advance to exactly TTL — cache is stale (now - cached_at == TTL, not < TTL)
        clock.advance(_TTL_SECONDS)

        await resolver.get(dept_id, service, "org")
        assert vault.total_reads(path) == 2, (
            "At exactly TTL seconds, cache should be stale and Vault re-read"
        )

    @pytest.mark.asyncio
    async def test_one_second_before_ttl_no_refresh(self) -> None:
        """One second before TTL expiry, cache is still valid."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        dept_id, service = "payment", "jira"
        resolver, path = _build_org_resolver(
            dept_id=dept_id, service=service, vault=vault, clock=clock
        )
        _seed_vault(vault, path)

        await resolver.get(dept_id, service, "org")
        assert vault.total_reads(path) == 1

        # One second before TTL
        clock.advance(_TTL_SECONDS - 1)

        await resolver.get(dept_id, service, "org")
        assert vault.total_reads(path) == 1, (
            "One second before TTL, cache should still be valid"
        )

    @pytest.mark.asyncio
    async def test_multiple_ttl_cycles_each_triggers_one_refresh(self) -> None:
        """Each TTL cycle triggers exactly one Vault read."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        dept_id, service = "payment", "jira"
        resolver, path = _build_org_resolver(
            dept_id=dept_id, service=service, vault=vault, clock=clock
        )
        _seed_vault(vault, path)

        n_cycles = 3
        for cycle in range(n_cycles):
            # First call in cycle — should hit Vault (cold or stale)
            await resolver.get(dept_id, service, "org")
            expected_reads = cycle + 1
            assert vault.total_reads(path) == expected_reads, (
                f"Cycle {cycle}: expected {expected_reads} reads, "
                f"got {vault.total_reads(path)}"
            )
            # Advance past TTL to expire cache for next cycle
            clock.advance(_TTL_SECONDS + 1)

# ---------------------------------------------------------------------------
# Drift audit payload completeness
# ---------------------------------------------------------------------------


class TestDriftAuditPayload:
    """Drift audit payload includes all required fields."""

    @_PROFILE
    @given(
        dept_id=_dept_id_st,
        service=_service_st,
        advance_seconds=_past_ttl,
        rotation_delta=_rotation_delta,
    )
    @pytest.mark.asyncio
    async def test_drift_audit_payload_has_all_required_fields(
        self,
        dept_id: str,
        service: str,
        advance_seconds: int,
        rotation_delta: int,
    ) -> None:
        """vault_credential_refreshed audit payload contains all required fields."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        audit = _FakeAuditLogger()
        resolver, path = _build_org_resolver(
            dept_id=dept_id, service=service, vault=vault, clock=clock, audit=audit
        )

        initial_entry = _seed_vault(vault, path, created_offset=0)
        await resolver.get(dept_id, service, "org")

        # Rotate secret
        rotated_entry = _VaultEntry(
            url=initial_entry.url,
            username=initial_entry.username,
            personal_token="tok-new",
            created_time=_make_created_time(_T0, rotation_delta),
        )
        vault.set_secret(path, rotated_entry)

        clock.advance(advance_seconds)
        await resolver.get(dept_id, service, "org")

        events = audit.refreshed_events()
        assert len(events) == 1
        payload = events[0].payload

        # Required drift audit fields
        required_fields = {"scope", "dept_id", "service", "prev_created_time", "new_created_time"}
        missing = required_fields - set(payload.keys())
        assert not missing, f"Drift audit payload missing fields: {missing}"

        assert payload["scope"] == "org"
        assert payload["dept_id"] == dept_id
        assert payload["service"] == service
        assert payload["prev_created_time"] == initial_entry.created_time
        assert payload["new_created_time"] == rotated_entry.created_time
        # new must be strictly greater than prev
        assert payload["new_created_time"] > payload["prev_created_time"], (
            "new_created_time must be strictly greater than prev_created_time"
        )

    @_PROFILE
    @given(
        dept_id=_dept_id_st,
        service=_service_st,
        advance_seconds=_past_ttl,
        rotation_delta=_rotation_delta,
    )
    @pytest.mark.asyncio
    async def test_refreshed_credential_reflects_new_vault_value(
        self,
        dept_id: str,
        service: str,
        advance_seconds: int,
        rotation_delta: int,
    ) -> None:
        """After TTL expiry + rotation, resolver returns the new credential."""
        clock = _AdvancingClock(_T0)
        vault = _FakeVaultClient()
        resolver, path = _build_org_resolver(
            dept_id=dept_id, service=service, vault=vault, clock=clock
        )

        _seed_vault(vault, path, created_offset=0)
        first = await resolver.get(dept_id, service, "org")
        assert first.personal_token == "tok-abc123"

        # Rotate
        rotated = _VaultEntry(
            url="https://example.atlassian.net",
            username="bot-user",
            personal_token="tok-rotated-new",
            created_time=_make_created_time(_T0, rotation_delta),
        )
        vault.set_secret(path, rotated)

        clock.advance(advance_seconds)
        second = await resolver.get(dept_id, service, "org")

        assert second.personal_token == "tok-rotated-new", (
            "After TTL expiry, resolver must return the rotated credential"
        )
        assert second != first
