"""Property tests for replay-dedup idempotence.

**Validates: Requirements 2.3, 2.4, 10.5 (Property 3, foundation)
              Requirements 1.8, 2.4, 2.5, 2.6 (Property 18, workflows
              spec extension — HTTP-layer ``ProcessedEventsRepo``)**

Property 3: SHA-256 replay-dedup idempotence (foundation spec).

Invariants tested:
  3a. compute_payload_hash is deterministic — same input always yields
      the same SHA-256 hex digest (idempotence).
  3b. Distinct payloads produce distinct hashes (injectivity for
      semantically different JSON objects).
  3c. check_and_insert returns True on first insert and False on all
      subsequent inserts of the same hash (dedup idempotence).
  3d. cleanup_expired removes only entries where expires_at < now;
      entries with expires_at >= now survive (post-state invariant).
  3e. cleanup_expired is idempotent — calling it twice with the same
      timestamp produces the same post-state.

Property 18: ``processed_events`` idempotent dedup at HTTP layer
(platform-mimari-workflows spec, task 3.5).

Extends the foundation Property 3 with the new ``delivery_id`` /
``provider`` schema introduced by ``11_workflows.sql`` and consumed
by :class:`automation_service.processed_events.ProcessedEventsRepo`.
The two property families coexist in this file: foundation
properties cover the SHA-256 canonicalization layer; Property 18
covers the HTTP-layer rollback semantics expressed by
``claim`` / ``is_processed`` / ``release``.

Invariants tested (Property 18):
 18a. ``claim(delivery_id, provider)`` returns True on first call
      and False on every subsequent call with the same id; the
      table contains exactly one row per delivery id (R1.8, R2.5).
 18b. After a successful ``claim``, ``is_processed(delivery_id)``
      returns True for all subsequent reads (R2.5).
 18c. ``signalWithStart`` HTTP 503 rollback path: ``release`` after a
      successful ``claim`` removes the row, so the next ``claim``
      with the same id returns True again (retry-safe). Combined
      ``claim → release → claim → True`` round-trip (R2.4).
 18d. Composite invariant with foundation Property 3: dispatching
      the same payload N times yields exactly one Temporal
      ``signalWithStart`` execution because the replay-dedup gate
      drops every replay before it reaches the dispatcher (R2.6).
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

# Ensure the automation-service src is importable for property tests.
_AUTOMATION_SRC = (
    Path(__file__).resolve().parents[1].parent
    / "services"
    / "automation-service"
    / "src"
)
if str(_AUTOMATION_SRC) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_SRC))

from decision.replay import check_and_insert, cleanup_expired, compute_payload_hash


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# JSON-serializable payloads as byte strings.
# We generate Python dicts and serialize them to bytes to ensure valid JSON.
_json_primitives = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1_000_000, max_value=1_000_000),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(min_size=0, max_size=50),
)

_json_values = st.recursive(
    _json_primitives,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(min_size=1, max_size=10), children, max_size=5),
    ),
    max_leaves=20,
)

_json_objects = st.dictionaries(
    st.text(min_size=1, max_size=10),
    _json_values,
    min_size=1,
    max_size=8,
)


def _dict_to_bytes(d: dict) -> bytes:
    """Serialize a dict to JSON bytes (arbitrary formatting)."""
    return json.dumps(d).encode("utf-8")


# Strategy that produces valid JSON byte payloads
_payloads = _json_objects.map(_dict_to_bytes)

# Strategy for payload hashes (64-char hex strings)
_hex_hashes = st.text(
    alphabet=st.sampled_from("0123456789abcdef"),
    min_size=64,
    max_size=64,
)

# Strategy for TTL durations
_ttls = st.timedeltas(
    min_value=timedelta(hours=1),
    max_value=timedelta(days=30),
)

# Strategy for timestamps (timezone-aware via timezones parameter)
_timestamps = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31),
    timezones=st.just(timezone.utc),
)


# ---------------------------------------------------------------------------
# In-memory fake for asyncpg pool to test check_and_insert / cleanup_expired
# ---------------------------------------------------------------------------


class FakeConnection:
    """Simulates asyncpg connection behavior for replay guard operations."""

    def __init__(self, store: dict[str, datetime]) -> None:
        self._store = store

    async def fetchrow(self, query: str, *args) -> dict | None:
        """Simulate INSERT ... ON CONFLICT DO NOTHING RETURNING ..."""
        payload_hash = args[0]
        ttl = args[1]
        if payload_hash not in self._store:
            # Simulate now() + ttl
            expires_at = datetime.now(timezone.utc) + ttl
            self._store[payload_hash] = expires_at
            return {"event_hash": payload_hash}
        return None

    async def execute(self, query: str, *args) -> str:
        """Simulate DELETE FROM ... WHERE expires_at < $1."""
        now = args[0]
        to_delete = [h for h, ea in self._store.items() if ea < now]
        for h in to_delete:
            del self._store[h]
        return f"DELETE {len(to_delete)}"


class FakePool:
    """Simulates asyncpg.Pool with an in-memory store."""

    def __init__(self) -> None:
        self.store: dict[str, datetime] = {}

    def acquire(self):
        return FakeAcquireContext(FakeConnection(self.store))


class FakeAcquireContext:
    """Async context manager for FakePool.acquire()."""

    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self._conn

    async def __aexit__(self, *args) -> None:
        pass


# ---------------------------------------------------------------------------
# Property 3a: compute_payload_hash is deterministic (idempotent)
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(payload=_payloads)
def test_compute_payload_hash_deterministic(payload: bytes) -> None:
    """Property 3a — same payload always produces the same hash.

    compute_payload_hash is a pure function; calling it multiple times
    with the same input must yield identical results.
    """
    h1 = compute_payload_hash(payload)
    h2 = compute_payload_hash(payload)
    assert h1 == h2


# ---------------------------------------------------------------------------
# Property 3a (extended): hash output is valid SHA-256 hex
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(payload=_payloads)
def test_compute_payload_hash_valid_sha256_format(payload: bytes) -> None:
    """Property 3a (format) — output is always a 64-char lowercase hex string."""
    h = compute_payload_hash(payload)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


# ---------------------------------------------------------------------------
# Property 3b: Distinct payloads produce distinct hashes
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(payload_a=_payloads, payload_b=_payloads)
def test_distinct_payloads_distinct_hashes(payload_a: bytes, payload_b: bytes) -> None:
    """Property 3b — semantically different payloads produce different hashes.

    Two JSON payloads that parse to different canonical forms must
    produce different SHA-256 digests (collision resistance).
    """
    # Canonicalize both to compare semantic equality
    canonical_a = json.dumps(
        json.loads(payload_a), sort_keys=True, separators=(",", ":")
    )
    canonical_b = json.dumps(
        json.loads(payload_b), sort_keys=True, separators=(",", ":")
    )
    assume(canonical_a != canonical_b)

    hash_a = compute_payload_hash(payload_a)
    hash_b = compute_payload_hash(payload_b)
    assert hash_a != hash_b


# ---------------------------------------------------------------------------
# Property 3b (extended): Key-order independence
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(obj=_json_objects)
def test_key_order_independence(obj: dict) -> None:
    """Property 3b (canonicalization) — different key orderings yield same hash.

    The canonical JSON normalization (sorted keys) ensures that
    semantically identical payloads with different key orderings
    produce the same hash.
    """
    # Original order
    body1 = json.dumps(obj).encode("utf-8")
    # Reversed key order
    reversed_obj = dict(reversed(list(obj.items())))
    body2 = json.dumps(reversed_obj).encode("utf-8")

    assert compute_payload_hash(body1) == compute_payload_hash(body2)


# ---------------------------------------------------------------------------
# Property 3c: check_and_insert first True, subsequent False
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    hashes=st.lists(_hex_hashes, min_size=1, max_size=10, unique=True),
    ttl=_ttls,
)
@pytest.mark.asyncio
async def test_check_and_insert_first_true_then_false(
    hashes: list[str], ttl: timedelta
) -> None:
    """Property 3c — first insert returns True, all subsequent inserts return False.

    For each unique hash, the first call to check_and_insert must return
    True (newly inserted). Any subsequent call with the same hash must
    return False (duplicate detected).
    """
    pool = FakePool()

    for h in hashes:
        # First insert → True
        result = await check_and_insert(pool, h, ttl)
        assert result is True, f"First insert of {h!r} should return True"

        # Second insert → False (duplicate)
        result = await check_and_insert(pool, h, ttl)
        assert result is False, f"Second insert of {h!r} should return False"

        # Third insert → still False (idempotent)
        result = await check_and_insert(pool, h, ttl)
        assert result is False, f"Third insert of {h!r} should return False"


# ---------------------------------------------------------------------------
# Property 3c (extended): Interleaved inserts maintain correct state
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    hashes=st.lists(_hex_hashes, min_size=2, max_size=8, unique=True),
    ttl=_ttls,
)
@pytest.mark.asyncio
async def test_check_and_insert_interleaved_state(
    hashes: list[str], ttl: timedelta
) -> None:
    """Property 3c (interleaved) — inserting multiple distinct hashes maintains
    independent dedup state for each.

    After inserting all hashes once, re-inserting any of them returns False,
    while the store contains exactly the set of inserted hashes.
    """
    pool = FakePool()

    # Insert all hashes (all should be True)
    for h in hashes:
        result = await check_and_insert(pool, h, ttl)
        assert result is True

    # Re-insert all (all should be False)
    for h in hashes:
        result = await check_and_insert(pool, h, ttl)
        assert result is False

    # Store contains exactly the inserted hashes
    assert set(pool.store.keys()) == set(hashes)


# ---------------------------------------------------------------------------
# Property 3d: cleanup_expired removes only expired entries
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    entries=st.lists(
        st.tuples(_hex_hashes, _timestamps),
        min_size=1,
        max_size=15,
        unique_by=lambda x: x[0],
    ),
    now=_timestamps,
)
@pytest.mark.asyncio
async def test_cleanup_expired_post_state(
    entries: list[tuple[str, datetime]], now: datetime
) -> None:
    """Property 3d — after cleanup_expired(now), only entries with expires_at >= now remain.

    The post-state invariant: {(h, ea) : ea >= now} is exactly the set
    of entries remaining in the store after cleanup.
    """
    pool = FakePool()

    # Populate the store directly
    for h, expires_at in entries:
        pool.store[h] = expires_at

    # Run cleanup
    deleted_count = await cleanup_expired(pool, now)

    # Post-state: only entries with expires_at >= now survive
    expected_survivors = {h for h, ea in entries if ea >= now}
    expected_deleted = {h for h, ea in entries if ea < now}

    assert set(pool.store.keys()) == expected_survivors
    assert deleted_count == len(expected_deleted)


# ---------------------------------------------------------------------------
# Property 3e: cleanup_expired is idempotent
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    entries=st.lists(
        st.tuples(_hex_hashes, _timestamps),
        min_size=0,
        max_size=15,
        unique_by=lambda x: x[0],
    ),
    now=_timestamps,
)
@pytest.mark.asyncio
async def test_cleanup_expired_idempotent(
    entries: list[tuple[str, datetime]], now: datetime
) -> None:
    """Property 3e — calling cleanup_expired twice with the same timestamp
    produces the same post-state (second call deletes 0 rows).

    cleanup_expired(now); cleanup_expired(now) ≡ cleanup_expired(now)
    """
    pool = FakePool()

    # Populate the store
    for h, expires_at in entries:
        pool.store[h] = expires_at

    # First cleanup
    first_deleted = await cleanup_expired(pool, now)
    state_after_first = dict(pool.store)

    # Second cleanup (same timestamp)
    second_deleted = await cleanup_expired(pool, now)
    state_after_second = dict(pool.store)

    # Idempotence: second call deletes nothing, state unchanged
    assert second_deleted == 0
    assert state_after_first == state_after_second


# ===========================================================================
# Property 18: processed_events idempotent dedup at HTTP layer
# (platform-mimari-workflows spec, task 3.5)
# ===========================================================================
#
# Below this banner the file extends the foundation Property 3 surface
# with Property 18: the HTTP-layer ``ProcessedEventsRepo`` contract
# introduced by ``11_workflows.sql`` and consumed by the webhook
# filter chain's ``replay_dedup`` stage. The new properties never
# touch the foundation ``check_and_insert`` / ``cleanup_expired`` /
# ``compute_payload_hash`` surface — they coexist in the same file
# because design.md groups them under the same "replay-dedup
# idempotence" umbrella (foundation Property 3 invariant + workflows
# Property 18 HTTP rollback path = a single composite invariant on
# how the same payload N times yields exactly one Temporal
# execution).
# ---------------------------------------------------------------------------

# Import the system-under-test as a standalone module to avoid the
# heavy ``automation_service`` package ``__init__`` chain (which pulls
# in FastAPI, the configured Vault client, and the legacy ``src.*``
# re-exports). The foundation block already mirrors this pattern by
# loading ``decision.replay`` from the same ``services/automation-service/src``
# tree without going through ``automation_service.__init__``.
#
# We use ``importlib.util.spec_from_file_location`` to load the file
# under a synthetic top-level module name so the SUT is fully
# isolated from the package namespace. The ``Protocol``-typed
# constructor accepts the structural ``acquire()`` surface our fake
# pool already exposes, so no asyncpg dependency leak occurs at
# import time.
import importlib.util as _importlib_util

_PROCESSED_EVENTS_PATH = (
    Path(__file__).resolve().parents[1].parent
    / "services"
    / "automation-service"
    / "src"
    / "automation_service"
    / "processed_events.py"
)
_spec = _importlib_util.spec_from_file_location(
    "_processed_events_sut", _PROCESSED_EVENTS_PATH
)
assert _spec is not None and _spec.loader is not None, (
    f"Failed to load processed_events.py from {_PROCESSED_EVENTS_PATH!s}"
)
_processed_events_module = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(_processed_events_module)
ProcessedEventsRepo = _processed_events_module.ProcessedEventsRepo


# ---------------------------------------------------------------------------
# Strategies — Property 18
# ---------------------------------------------------------------------------

# Webhook delivery ids are opaque provider-assigned tokens. Jira sends
# UUID-shaped strings via ``X-Atlassian-Webhook-Delivery``; Bitbucket
# sends UUID-shaped strings via ``X-Request-UUID``. The schema column
# is ``TEXT`` so we exercise the full character set the webhook
# handler may forward verbatim. We constrain the strategy to the
# printable ASCII subset (Hypothesis ``text`` blacklists control
# characters by default which is exactly what the schema accepts).
_delivery_ids = st.text(
    alphabet=st.characters(
        min_codepoint=33, max_codepoint=126, blacklist_categories=()
    ),
    min_size=1,
    max_size=64,
)

# Provider mirrors the SQL CHECK constraint
# (CHECK (provider IN ('jira','bitbucket'))).
_providers = st.sampled_from(["jira", "bitbucket"])

# A ``(delivery_id, provider)`` pair as it would arrive at
# ``ProcessedEventsRepo.claim``.
_claim_inputs = st.tuples(_delivery_ids, _providers)


# ---------------------------------------------------------------------------
# In-memory fake for asyncpg pool — honours the new schema's PK + CHECK
# ---------------------------------------------------------------------------
#
# The fake intentionally models only the surface ``ProcessedEventsRepo``
# touches: a ``fetchrow`` that interprets the three SQL strings the
# repo emits (claim INSERT, is_processed SELECT, release DELETE) and
# an ``execute`` for the DELETE return-status. This keeps the
# property test self-contained — no real Postgres in the loop — while
# still enforcing the unique PK constraint that Property 18 (a)
# relies on.


class _ProcessedEventsFakeConnection:
    """asyncpg-shaped connection fake honouring the new PK + CHECK constraints.

    The store is a plain ``dict[str, str]`` mapping ``delivery_id`` →
    ``provider``, which is the minimal projection of the
    ``automation.processed_events`` row needed to validate the
    Property 18 invariants. ``received_at`` is intentionally not
    modelled — neither the repo nor the property reads it back.
    """

    _ALLOWED_PROVIDERS = frozenset({"jira", "bitbucket"})

    def __init__(self, store: dict[str, str]) -> None:
        self._store = store

    async def fetchrow(self, query: str, *args):
        # The repo emits exactly three queries; we dispatch on a
        # canonical-substring match rather than a full string compare
        # so whitespace / formatting drift in ``processed_events.py``
        # does not break this fake.
        normalized = " ".join(query.split())
        if "INSERT INTO automation.processed_events" in normalized:
            delivery_id, provider = args[0], args[1]
            # Mirror the SQL CHECK constraint so a property attempting
            # an unsupported provider surfaces the same error class
            # the real DB would raise. We use ValueError to keep the
            # fake free of asyncpg internals; tests do not exercise
            # this path because ``_providers`` is closed.
            if provider not in self._ALLOWED_PROVIDERS:
                raise ValueError(
                    f"chk_processed_events_provider violated: {provider!r}"
                )
            if delivery_id in self._store:
                # ON CONFLICT DO NOTHING → no row returned.
                return None
            self._store[delivery_id] = provider
            return {"delivery_id": delivery_id}
        if "SELECT 1" in normalized and "FROM automation.processed_events" in normalized:
            delivery_id = args[0]
            return {"?column?": 1} if delivery_id in self._store else None
        raise NotImplementedError(
            f"_ProcessedEventsFakeConnection.fetchrow: unsupported query "
            f"{query!r}"
        )

    async def execute(self, query: str, *args) -> str:
        normalized = " ".join(query.split())
        if "DELETE FROM automation.processed_events" in normalized:
            delivery_id = args[0]
            removed = self._store.pop(delivery_id, None) is not None
            return f"DELETE {1 if removed else 0}"
        raise NotImplementedError(
            f"_ProcessedEventsFakeConnection.execute: unsupported query "
            f"{query!r}"
        )


class _ProcessedEventsFakeAcquireContext:
    """Async context-manager wrapper around ``_ProcessedEventsFakeConnection``."""

    def __init__(self, conn: _ProcessedEventsFakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _ProcessedEventsFakeConnection:
        return self._conn

    async def __aexit__(self, *args) -> None:
        return None


class _ProcessedEventsFakePool:
    """In-memory ``asyncpg.Pool`` substitute for Property 18.

    Each ``acquire()`` yields a connection bound to the same shared
    store, which is the behaviour ``ProcessedEventsRepo`` assumes
    when the PK constraint provides linearisability across
    concurrent calls. The store is exposed as a public attribute so
    properties can inspect the post-state directly (row count,
    membership, provider value) without going through the SQL
    fake.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def acquire(self) -> _ProcessedEventsFakeAcquireContext:
        return _ProcessedEventsFakeAcquireContext(
            _ProcessedEventsFakeConnection(self.store)
        )


# ---------------------------------------------------------------------------
# Property 18 (a): claim() first True, subsequent False; exactly one row
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    delivery_id=_delivery_ids,
    provider=_providers,
    extra_attempts=st.integers(min_value=1, max_value=10),
)
@pytest.mark.asyncio
async def test_claim_first_true_then_false_exactly_one_row(
    delivery_id: str, provider: str, extra_attempts: int
) -> None:
    """Property 18 (a) — ``claim()`` first call True, all replays False; exactly one row.

    **Validates: Requirements 1.8, 2.5**

    For any ``(delivery_id, provider)`` pair, the first call to
    :meth:`ProcessedEventsRepo.claim` returns True and inserts
    exactly one row into ``automation.processed_events``. Every
    subsequent call with the same ``delivery_id`` returns False
    (replay dedup) and the row count stays at one — this is the
    ``ON CONFLICT DO NOTHING`` invariant the SQL PK provides.
    """

    pool = _ProcessedEventsFakePool()
    repo = ProcessedEventsRepo(pool)

    # First claim → True (inserted)
    first = await repo.claim(delivery_id, provider)
    assert first is True, "first claim must return True"
    assert pool.store == {delivery_id: provider}, (
        "first claim must leave exactly one row in the store"
    )

    # Replays → False (idempotent no-op)
    for _ in range(extra_attempts):
        replay = await repo.claim(delivery_id, provider)
        assert replay is False, "replay claim must return False"
        # Row count never deviates from one for this delivery id.
        assert pool.store == {delivery_id: provider}, (
            "replay claim must leave the store untouched"
        )


# ---------------------------------------------------------------------------
# Property 18 (a-extended): independent dedup state across distinct ids
# ---------------------------------------------------------------------------


@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    inputs=st.lists(
        _claim_inputs,
        min_size=2,
        max_size=10,
        unique_by=lambda pair: pair[0],
    )
)
@pytest.mark.asyncio
async def test_claim_independent_state_across_delivery_ids(
    inputs: list[tuple[str, str]],
) -> None:
    """Property 18 (a-ext) — distinct delivery ids maintain independent dedup state.

    **Validates: Requirements 1.8, 2.5**

    After claiming each id once, the store contains exactly one row
    per id with the matching provider. A second pass over the same
    ids returns False for every entry — the dedup state of one
    delivery does not contaminate another.
    """

    pool = _ProcessedEventsFakePool()
    repo = ProcessedEventsRepo(pool)

    # First pass: every claim is fresh.
    for delivery_id, provider in inputs:
        assert await repo.claim(delivery_id, provider) is True

    # Store contains exactly the inputs, with the right provider.
    assert pool.store == {did: prov for did, prov in inputs}

    # Second pass: every claim is a replay.
    for delivery_id, provider in inputs:
        assert await repo.claim(delivery_id, provider) is False

    # Store unchanged after the replay pass.
    assert pool.store == {did: prov for did, prov in inputs}


# ---------------------------------------------------------------------------
# Property 18 (b): is_processed True after claim; stays True
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    delivery_id=_delivery_ids,
    provider=_providers,
    read_attempts=st.integers(min_value=1, max_value=10),
)
@pytest.mark.asyncio
async def test_is_processed_true_after_claim_and_stable(
    delivery_id: str, provider: str, read_attempts: int
) -> None:
    """Property 18 (b) — ``is_processed`` True after claim; stable across reads.

    **Validates: Requirements 2.5**

    Pre-claim the predicate is False; immediately after a
    successful claim it transitions to True and stays True for any
    number of subsequent reads. This is the read-side contract the
    webhook filter chain's ``replay_dedup`` callback depends on.
    """

    pool = _ProcessedEventsFakePool()
    repo = ProcessedEventsRepo(pool)

    # Pre-claim: predicate is False.
    assert await repo.is_processed(delivery_id) is False

    # Claim transitions the predicate to True.
    assert await repo.claim(delivery_id, provider) is True

    # Stable: every subsequent read still observes True.
    for _ in range(read_attempts):
        assert await repo.is_processed(delivery_id) is True


# ---------------------------------------------------------------------------
# Property 18 (c): signalWithStart 503 rollback — claim → release → claim → True
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    delivery_id=_delivery_ids,
    provider=_providers,
    rollback_cycles=st.integers(min_value=1, max_value=5),
)
@pytest.mark.asyncio
async def test_claim_release_claim_round_trip_after_503(
    delivery_id: str, provider: str, rollback_cycles: int
) -> None:
    """Property 18 (c) — ``release`` after ``claim`` lets the retry re-claim.

    **Validates: Requirements 2.4**

    Models the ``signalWithStart`` HTTP 503 rollback path: when the
    Temporal dispatcher fails after a successful ``claim``, the
    webhook handler calls :meth:`ProcessedEventsRepo.release` to
    remove the row so the webhook provider's retry observes the
    delivery as un-claimed and can re-enter the workflow-start
    path. Repeated rollback cycles must remain idempotent — every
    cycle ends with ``is_processed == False`` and the store empty
    of the rolled-back id.
    """

    pool = _ProcessedEventsFakePool()
    repo = ProcessedEventsRepo(pool)

    for cycle in range(rollback_cycles):
        # claim → True (fresh row inserted)
        assert await repo.claim(delivery_id, provider) is True, (
            f"cycle {cycle}: post-rollback claim must return True"
        )
        assert await repo.is_processed(delivery_id) is True

        # release → True (row removed)
        assert await repo.release(delivery_id) is True, (
            f"cycle {cycle}: release after claim must return True"
        )
        # Post-release: predicate flips back to False, store empty
        # of this delivery id.
        assert await repo.is_processed(delivery_id) is False
        assert delivery_id not in pool.store

        # release again is a safe no-op (idempotent rollback).
        assert await repo.release(delivery_id) is False


# ---------------------------------------------------------------------------
# Property 18 (c-extended): release without prior claim is a no-op
# ---------------------------------------------------------------------------


@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(delivery_id=_delivery_ids)
@pytest.mark.asyncio
async def test_release_without_prior_claim_is_noop(delivery_id: str) -> None:
    """Property 18 (c-ext) — ``release`` is a safe no-op when no row exists.

    **Validates: Requirements 2.4**

    Releasing a ``delivery_id`` that was never claimed (or already
    released) returns False and leaves the store untouched. This is
    the contract the webhook handler relies on inside its 503
    except-block: the rollback is unconditional, no pre-check
    needed.
    """

    pool = _ProcessedEventsFakePool()
    repo = ProcessedEventsRepo(pool)

    # No claim → release is a no-op.
    assert await repo.release(delivery_id) is False
    assert pool.store == {}

    # Subsequent claim still observes the id as fresh.
    assert await repo.claim(delivery_id, "jira") is True


# ---------------------------------------------------------------------------
# Property 18 (d): composite invariant — N webhook replays → 1 dispatch
# ---------------------------------------------------------------------------


@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    delivery_id=_delivery_ids,
    provider=_providers,
    replay_count=st.integers(min_value=1, max_value=20),
)
@pytest.mark.asyncio
async def test_n_replays_yield_exactly_one_dispatch(
    delivery_id: str, provider: str, replay_count: int
) -> None:
    """Property 18 (d) — composite invariant: N replays → exactly one dispatch.

    **Validates: Requirements 2.6** (composite with foundation
    Property 3 — same payload SHA-256 + same delivery id ⇒ one
    Temporal execution)

    Models the full webhook dispatcher loop without invoking
    Temporal: every webhook delivery first consults
    :meth:`ProcessedEventsRepo.claim`; only the call that observes
    True proceeds to the dispatcher. Across N replays of the same
    delivery id the dispatcher counter reaches exactly one. This
    closes the loop on R2.6 (``test_temporal_idempotency.py`` —
    aynı event payload'ı 1, 5, 100 kez peş peşe gönderildiğinde tek
    bir Temporal execution oluşur).
    """

    pool = _ProcessedEventsFakePool()
    repo = ProcessedEventsRepo(pool)

    dispatch_count = 0

    for _ in range(replay_count):
        if await repo.claim(delivery_id, provider):
            # First-and-only path that survives the replay-dedup gate.
            dispatch_count += 1

    assert dispatch_count == 1, (
        f"N={replay_count} replays must yield exactly one dispatch, "
        f"got {dispatch_count}"
    )
    # Final state: exactly one row, regardless of how many replays
    # arrived.
    assert pool.store == {delivery_id: provider}


# ---------------------------------------------------------------------------
# Property 18 (d-extended): N replays interleaved with rollback
# ---------------------------------------------------------------------------


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    delivery_id=_delivery_ids,
    provider=_providers,
    # A list of booleans modelling whether each delivery's
    # ``signalWithStart`` succeeded (True) or failed with 503 (False).
    # The dispatcher counter only advances on a successful dispatch
    # (success=True); on failure the row is released and the next
    # delivery re-enters the claim path.
    dispatch_outcomes=st.lists(
        st.booleans(), min_size=1, max_size=15
    ),
)
@pytest.mark.asyncio
async def test_n_replays_with_503_rollback_yield_exactly_one_success(
    delivery_id: str,
    provider: str,
    dispatch_outcomes: list[bool],
) -> None:
    """Property 18 (d-ext) — replays + 503 rollback → at most one successful dispatch.

    **Validates: Requirements 2.4, 2.6**

    Composite of (c) and (d): each delivery either succeeds (counter
    advances, row stays) or fails with 503 (row is rolled back). The
    invariant: the total number of successful dispatches across the
    whole sequence is at most one — once a dispatch succeeds, every
    subsequent replay observes the row as already claimed and is
    dropped before reaching the dispatcher. If every dispatch
    failed, the counter is zero and the store ends empty.
    """

    pool = _ProcessedEventsFakePool()
    repo = ProcessedEventsRepo(pool)

    successful_dispatches = 0
    sealed = False  # set once a successful dispatch claims the slot

    for success in dispatch_outcomes:
        claimed = await repo.claim(delivery_id, provider)
        if not claimed:
            # Replay-dedup dropped the delivery before dispatch.
            continue
        # The handler now invokes signalWithStart; outcome is the
        # property's randomised input.
        if success:
            successful_dispatches += 1
            sealed = True
        else:
            # 503 path: roll the claim back so the next replay can
            # re-enter the workflow-start path.
            assert await repo.release(delivery_id) is True

    assert successful_dispatches <= 1, (
        f"at most one successful dispatch may occur across replays, "
        f"got {successful_dispatches}"
    )
    if sealed:
        # A successful dispatch sealed the slot: the row remains in
        # the store and is_processed reports True for the rest of
        # time.
        assert pool.store == {delivery_id: provider}
        assert await repo.is_processed(delivery_id) is True
    else:
        # Every dispatch failed and was rolled back: the store is
        # empty and the next replay would re-enter as fresh.
        assert pool.store == {}
        assert await repo.is_processed(delivery_id) is False
