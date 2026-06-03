"""Property tests for Event Dedup idempotency.

Event dedup idempotency: for any webhook with the same
event_id received N times (N ≥ 1), exactly ONE workflow start SHALL be
triggered; all subsequent deliveries SHALL be dropped with 200 OK.

Invariants tested
-----------------

2a. **First submission always passes.** For any event_id that has not been
    seen before, ``EventDedup.check()`` returns ``StageResult(action="pass")``.

2b. **All subsequent submissions are dropped.** For any event_id that has
    already been submitted once, all N-1 subsequent calls to
    ``EventDedup.check()`` return ``StageResult(action="drop", reason="duplicate")``.

2c. **Exactly one pass per event_id.** For any event_id submitted N times
    (N ≥ 1), the count of "pass" results is exactly 1 and the count of
    "drop" results is exactly N-1.

2d. **Different event_ids are independent.** Two distinct event_ids each
    get their own "first pass" — dedup state for one does not affect the other.

2e. **Determinism.** The dedup decision for a given event_id is deterministic
    given the same DB state — repeated checks with the same state produce
    identical results.

This file uses a fake in-memory DB that simulates the dedup table behavior
(insert with TTL, existence check) to test the ``EventDedup`` class from
``platform/services/automation-service/src/webhooks/dedup.py``.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# sys.path bootstrap — mirrors sibling property tests
# ---------------------------------------------------------------------------

_AUTOMATION_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_AUTOMATION_SRC) not in sys.path:
    sys.path.insert(0, str(_AUTOMATION_SRC))

from webhooks.dedup import EventDedup, StageResult, WebhookPayload  # noqa: E402


# ---------------------------------------------------------------------------
# Fake async DB pool (simulates shared.webhook_dedup table)
# ---------------------------------------------------------------------------


@dataclass
class FakeConnection:
    """Simulates an asyncpg connection with fetchrow and execute methods."""

    _store: dict[str, datetime] = field(default_factory=dict)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        """Simulate SELECT 1 FROM shared.webhook_dedup WHERE event_id = $1."""
        event_id = args[0]
        if event_id in self._store:
            # Check if not expired
            expires_at = self._store[event_id]
            if expires_at > datetime.now(timezone.utc):
                return {"exists": 1}
        return None

    async def execute(self, query: str, *args: Any) -> str:
        """Simulate INSERT INTO shared.webhook_dedup or DELETE."""
        if "INSERT" in query:
            event_id = args[0]
            # Simulate ON CONFLICT DO NOTHING
            if event_id not in self._store:
                # TTL is the 4th arg (timedelta)
                ttl = args[3] if len(args) > 3 else timedelta(hours=24)
                self._store[event_id] = datetime.now(timezone.utc) + ttl
            return "INSERT 0 1"
        elif "DELETE" in query:
            now = args[0]
            expired_keys = [
                k for k, v in self._store.items() if v < now
            ]
            for k in expired_keys:
                del self._store[k]
            return f"DELETE {len(expired_keys)}"
        return "OK"


@dataclass
class FakeDBPool:
    """Simulates asyncpg.Pool with acquire() context manager."""

    _conn: FakeConnection = field(default_factory=FakeConnection)

    @asynccontextmanager
    async def acquire(self):
        """Yield the shared fake connection."""
        yield self._conn


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

#: Event IDs: either Atlassian-style UUIDs or SHA-256 hashes.
_event_ids: st.SearchStrategy[str] = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="-_",
    ),
    min_size=1,
    max_size=64,
)

#: Repetition counts: how many times the same event is submitted.
_repetitions: st.SearchStrategy[int] = st.integers(min_value=1, max_value=20)

#: Webhook event type strings.
_webhook_events: st.SearchStrategy[str] = st.sampled_from([
    "jira:issue_created",
    "jira:issue_updated",
    "jira:issue_assigned",
    "jira:comment_created",
])

#: Issue IDs (numeric strings).
_issue_ids: st.SearchStrategy[str] = st.from_regex(r"[0-9]{4,6}", fullmatch=True)

#: Timestamps (epoch ms).
_timestamps: st.SearchStrategy[int] = st.integers(
    min_value=1700000000000, max_value=1800000000000
)


@st.composite
def _webhook_payload_with_header(draw: st.DrawFn, event_id: str) -> WebhookPayload:
    """Generate a WebhookPayload that uses X-Atlassian-Webhook-Identifier header."""
    webhook_event = draw(_webhook_events)
    issue_id = draw(_issue_ids)
    timestamp = draw(_timestamps)
    body = {
        "webhookEvent": webhook_event,
        "timestamp": timestamp,
        "issue": {"id": issue_id, "key": f"PROJ-{issue_id}"},
    }
    headers = {"x-atlassian-webhook-identifier": event_id}
    return WebhookPayload(
        headers=headers,
        body=body,
        raw_body=b"{}",
    )


@st.composite
def _webhook_payload_without_header(draw: st.DrawFn) -> WebhookPayload:
    """Generate a WebhookPayload without the Atlassian header (uses hash-based dedup)."""
    webhook_event = draw(_webhook_events)
    issue_id = draw(_issue_ids)
    timestamp = draw(_timestamps)
    body = {
        "webhookEvent": webhook_event,
        "timestamp": timestamp,
        "issue": {"id": issue_id, "key": f"PROJ-{issue_id}"},
    }
    headers: dict[str, str] = {}
    return WebhookPayload(
        headers=headers,
        body=body,
        raw_body=b"{}",
    )


# ---------------------------------------------------------------------------
# Hypothesis settings
# ---------------------------------------------------------------------------

_PROFILE = settings(
    max_examples=200,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)


# ---------------------------------------------------------------------------
# Helper: run async test in sync context
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# First submission always passes
# ---------------------------------------------------------------------------


class TestFirstSubmissionPasses:
    """The first time any event_id is seen, it passes through."""

    @_PROFILE
    @given(event_id=_event_ids, data=st.data())
    def test_first_submission_returns_pass(
        self, event_id: str, data: st.DataObject
    ) -> None:
        """For any event_id not previously seen, EventDedup.check() returns
        action="pass".
        """
        payload = data.draw(_webhook_payload_with_header(event_id))
        db = FakeDBPool()
        dedup = EventDedup(db=db)

        result = _run(dedup.check(payload))
        assert result.action == "pass"


# ---------------------------------------------------------------------------
# All subsequent submissions are dropped
# ---------------------------------------------------------------------------


class TestSubsequentSubmissionsDropped:
    """After the first pass, all subsequent submissions are dropped."""

    @_PROFILE
    @given(event_id=_event_ids, n=_repetitions, data=st.data())
    def test_subsequent_submissions_return_drop(
        self, event_id: str, n: int, data: st.DataObject
    ) -> None:
        """For any event_id submitted N times (N >= 2), submissions 2..N
        all return action="drop" with reason="duplicate".
        """
        payload = data.draw(_webhook_payload_with_header(event_id))
        db = FakeDBPool()
        dedup = EventDedup(db=db)

        # First submission passes
        first_result = _run(dedup.check(payload))
        assert first_result.action == "pass"

        # All subsequent submissions are dropped
        for _ in range(n):
            result = _run(dedup.check(payload))
            assert result.action == "drop"
            assert result.reason == "duplicate"


# ---------------------------------------------------------------------------
# Exactly one pass per event_id
# ---------------------------------------------------------------------------


class TestExactlyOnePass:
    """For N submissions of the same event_id, exactly 1 passes."""

    @_PROFILE
    @given(event_id=_event_ids, n=_repetitions, data=st.data())
    def test_exactly_one_pass_n_minus_one_drops(
        self, event_id: str, n: int, data: st.DataObject
    ) -> None:
        """For any event_id submitted N times (N >= 1), the count of "pass"
        results is exactly 1 and the count of "drop" results is exactly N-1.
        """
        payload = data.draw(_webhook_payload_with_header(event_id))
        db = FakeDBPool()
        dedup = EventDedup(db=db)

        results = []
        for _ in range(n):
            result = _run(dedup.check(payload))
            results.append(result.action)

        pass_count = results.count("pass")
        drop_count = results.count("drop")

        assert pass_count == 1, (
            f"Expected exactly 1 pass, got {pass_count} for {n} submissions"
        )
        assert drop_count == n - 1, (
            f"Expected {n - 1} drops, got {drop_count} for {n} submissions"
        )


# ---------------------------------------------------------------------------
# Different event_ids are independent
# ---------------------------------------------------------------------------


class TestIndependentEventIds:
    """Dedup state for one event_id does not affect another."""

    @_PROFILE
    @given(
        event_id_a=_event_ids,
        event_id_b=_event_ids,
        data=st.data(),
    )
    def test_different_event_ids_each_get_first_pass(
        self, event_id_a: str, event_id_b: str, data: st.DataObject
    ) -> None:
        """Two distinct event_ids each get their own independent "first pass".
        Submitting event_id_a does not cause event_id_b to be dropped.
        """
        from hypothesis import assume

        assume(event_id_a != event_id_b)

        payload_a = data.draw(_webhook_payload_with_header(event_id_a))
        payload_b = data.draw(_webhook_payload_with_header(event_id_b))
        db = FakeDBPool()
        dedup = EventDedup(db=db)

        # Submit A first
        result_a = _run(dedup.check(payload_a))
        assert result_a.action == "pass"

        # B should still pass (independent)
        result_b = _run(dedup.check(payload_b))
        assert result_b.action == "pass"

        # A should now be dropped
        result_a2 = _run(dedup.check(payload_a))
        assert result_a2.action == "drop"
        assert result_a2.reason == "duplicate"

        # B should now be dropped
        result_b2 = _run(dedup.check(payload_b))
        assert result_b2.action == "drop"
        assert result_b2.reason == "duplicate"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDedupDeterminism:
    """Dedup decisions are deterministic given the same DB state."""

    @_PROFILE
    @given(event_id=_event_ids, data=st.data())
    def test_fresh_dedup_always_passes_first_time(
        self, event_id: str, data: st.DataObject
    ) -> None:
        """With a fresh DB, the same event_id always passes on first check."""
        payload = data.draw(_webhook_payload_with_header(event_id))

        # Run the same scenario 3 times with fresh DBs
        for _ in range(3):
            db = FakeDBPool()
            dedup = EventDedup(db=db)
            result = _run(dedup.check(payload))
            assert result.action == "pass"

    @_PROFILE
    @given(event_id=_event_ids, data=st.data())
    def test_seen_event_always_drops(
        self, event_id: str, data: st.DataObject
    ) -> None:
        """Once an event_id is in the DB, repeated checks always return "drop"."""
        payload = data.draw(_webhook_payload_with_header(event_id))
        db = FakeDBPool()
        dedup = EventDedup(db=db)

        # First pass
        _run(dedup.check(payload))

        # All subsequent checks are deterministically "drop"
        r1 = _run(dedup.check(payload))
        r2 = _run(dedup.check(payload))
        r3 = _run(dedup.check(payload))

        assert r1.action == r2.action == r3.action == "drop"
        assert r1.reason == r2.reason == r3.reason == "duplicate"


# ---------------------------------------------------------------------------
# Hash-based event IDs keep the same invariants
# ---------------------------------------------------------------------------


class TestHashBasedDedup:
    """Dedup idempotency holds when event_id is derived from payload hash."""

    @_PROFILE
    @given(data=st.data(), n=_repetitions)
    def test_hash_based_dedup_exactly_one_pass(
        self, data: st.DataObject, n: int
    ) -> None:
        """When no X-Atlassian-Webhook-Identifier header is present, the
        event_id is derived from webhookEvent + timestamp + issue.id.
        The same payload submitted N times still yields exactly 1 pass.
        """
        payload = data.draw(_webhook_payload_without_header())
        db = FakeDBPool()
        dedup = EventDedup(db=db)

        results = []
        for _ in range(n):
            result = _run(dedup.check(payload))
            results.append(result.action)

        pass_count = results.count("pass")
        drop_count = results.count("drop")

        assert pass_count == 1
        assert drop_count == n - 1
