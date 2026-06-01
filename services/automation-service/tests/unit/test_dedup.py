"""Unit tests for webhooks.dedup module (EventDedup pipeline stage).

Validates event_id derivation, dedup check logic, cleanup job, and
at-least-once semantics on DB failure.

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure the automation-service src is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from webhooks.dedup import EventDedup, StageResult, WebhookPayload


# =============================================================================
# Fixtures
# =============================================================================


def _make_payload(
    *,
    headers: dict[str, str] | None = None,
    body: dict | None = None,
    raw_body: bytes | None = None,
) -> WebhookPayload:
    """Helper to create a WebhookPayload with sensible defaults."""
    if body is None:
        body = {
            "webhookEvent": "jira:issue_created",
            "timestamp": 1700000000000,
            "issue": {"id": "12345", "key": "PROJ-100"},
        }
    if headers is None:
        headers = {}
    if raw_body is None:
        import json

        raw_body = json.dumps(body).encode("utf-8")
    # Lowercase header keys for consistency
    normalized_headers = {k.lower(): v for k, v in headers.items()}
    return WebhookPayload(headers=normalized_headers, body=body, raw_body=raw_body)


@pytest.fixture
def mock_pool() -> AsyncMock:
    """Create a mock asyncpg pool with acquire() context manager."""
    pool = AsyncMock(spec=["acquire"])
    conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = ctx
    pool._conn = conn
    return pool


@pytest.fixture
def dedup(mock_pool: AsyncMock) -> EventDedup:
    """Create an EventDedup instance with a mocked DB pool."""
    return EventDedup(db=mock_pool)


# =============================================================================
# WebhookPayload accessor tests
# =============================================================================


class TestWebhookPayload:
    """Tests for WebhookPayload data model accessors."""

    def test_webhook_event(self) -> None:
        payload = _make_payload()
        assert payload.webhook_event == "jira:issue_created"

    def test_timestamp(self) -> None:
        payload = _make_payload()
        assert payload.timestamp == 1700000000000

    def test_issue_id(self) -> None:
        payload = _make_payload()
        assert payload.issue_id == "12345"

    def test_issue_key(self) -> None:
        payload = _make_payload()
        assert payload.issue_key == "PROJ-100"

    def test_atlassian_webhook_identifier_present(self) -> None:
        payload = _make_payload(
            headers={"X-Atlassian-Webhook-Identifier": "abc-123-def"}
        )
        assert payload.atlassian_webhook_identifier == "abc-123-def"

    def test_atlassian_webhook_identifier_absent(self) -> None:
        payload = _make_payload(headers={})
        assert payload.atlassian_webhook_identifier is None

    def test_missing_issue(self) -> None:
        payload = _make_payload(body={"webhookEvent": "test", "timestamp": 1})
        assert payload.issue_id is None
        assert payload.issue_key is None

    def test_missing_webhook_event(self) -> None:
        payload = _make_payload(body={"timestamp": 1})
        assert payload.webhook_event is None


# =============================================================================
# EventDedup._derive_event_id tests
# =============================================================================


class TestDeriveEventId:
    """Tests for event_id derivation logic (Requirement 3.1)."""

    def test_uses_atlassian_header_when_present(self, dedup: EventDedup) -> None:
        """R3.1: X-Atlassian-Webhook-Identifier header is preferred."""
        payload = _make_payload(
            headers={"X-Atlassian-Webhook-Identifier": "unique-delivery-id"}
        )
        event_id = dedup._derive_event_id(payload)
        assert event_id == "unique-delivery-id"

    def test_falls_back_to_hash_when_no_header(self, dedup: EventDedup) -> None:
        """R3.1: Falls back to hash(event+timestamp+issue_id)."""
        payload = _make_payload(headers={})
        event_id = dedup._derive_event_id(payload)
        # Should be a SHA-256 hex digest
        assert len(event_id) == 64
        assert all(c in "0123456789abcdef" for c in event_id)

    def test_hash_is_deterministic(self, dedup: EventDedup) -> None:
        """Same payload produces same hash."""
        payload = _make_payload(headers={})
        assert dedup._derive_event_id(payload) == dedup._derive_event_id(payload)

    def test_different_events_produce_different_hashes(self, dedup: EventDedup) -> None:
        """Different payloads produce different event_ids."""
        p1 = _make_payload(
            body={
                "webhookEvent": "jira:issue_created",
                "timestamp": 1700000000000,
                "issue": {"id": "111", "key": "A-1"},
            }
        )
        p2 = _make_payload(
            body={
                "webhookEvent": "jira:issue_updated",
                "timestamp": 1700000000000,
                "issue": {"id": "111", "key": "A-1"},
            }
        )
        assert dedup._derive_event_id(p1) != dedup._derive_event_id(p2)

    def test_hash_composition(self, dedup: EventDedup) -> None:
        """Verify the hash is computed from event:timestamp:issue_id."""
        payload = _make_payload(
            headers={},
            body={
                "webhookEvent": "jira:issue_created",
                "timestamp": 1700000000000,
                "issue": {"id": "99", "key": "X-1"},
            },
        )
        expected = hashlib.sha256(
            "jira:issue_created:1700000000000:99".encode("utf-8")
        ).hexdigest()
        assert dedup._derive_event_id(payload) == expected

    def test_missing_fields_handled_gracefully(self, dedup: EventDedup) -> None:
        """Missing fields default to empty string in hash."""
        payload = _make_payload(body={})
        event_id = dedup._derive_event_id(payload)
        expected = hashlib.sha256("::".encode("utf-8")).hexdigest()
        assert event_id == expected


# =============================================================================
# EventDedup.check tests
# =============================================================================


class TestCheck:
    """Tests for the check() method (Requirements 3.2, 3.3, 3.5)."""

    @pytest.mark.asyncio
    async def test_new_event_passes(
        self, dedup: EventDedup, mock_pool: AsyncMock
    ) -> None:
        """R3.3: New event_id → insert + pass to next stage."""
        # _exists returns None (not found)
        mock_pool._conn.fetchrow.return_value = None
        # _insert succeeds
        mock_pool._conn.execute.return_value = "INSERT 0 1"

        payload = _make_payload()
        result = await dedup.check(payload)

        assert result.action == "pass"
        assert result.reason is None

    @pytest.mark.asyncio
    async def test_duplicate_event_drops(
        self, dedup: EventDedup, mock_pool: AsyncMock
    ) -> None:
        """R3.2: Existing event_id → drop with reason 'duplicate'."""
        # _exists returns a row (found)
        mock_pool._conn.fetchrow.return_value = {"?column?": 1}

        payload = _make_payload()
        result = await dedup.check(payload)

        assert result.action == "drop"
        assert result.reason == "duplicate"

    @pytest.mark.asyncio
    async def test_db_write_failure_passes_through(
        self, dedup: EventDedup, mock_pool: AsyncMock
    ) -> None:
        """R3.5: DB write failure → pass through (at-least-once semantics)."""
        # _exists returns None (not found)
        mock_pool._conn.fetchrow.return_value = None
        # _insert raises an exception
        mock_pool._conn.execute.side_effect = ConnectionError("DB connection lost")

        payload = _make_payload()
        result = await dedup.check(payload)

        assert result.action == "pass"
        assert result.reason is None

    @pytest.mark.asyncio
    async def test_db_read_failure_passes_through(
        self, dedup: EventDedup, mock_pool: AsyncMock
    ) -> None:
        """R3.5: DB read failure → pass through (at-least-once semantics)."""
        # _exists raises an exception
        mock_pool._conn.fetchrow.side_effect = ConnectionError("DB connection lost")

        payload = _make_payload()
        result = await dedup.check(payload)

        assert result.action == "pass"
        assert result.reason is None


# =============================================================================
# EventDedup.cleanup_expired tests
# =============================================================================


class TestCleanupExpired:
    """Tests for the cleanup_expired() method (Requirement 3.4)."""

    @pytest.mark.asyncio
    async def test_returns_deleted_count(
        self, dedup: EventDedup, mock_pool: AsyncMock
    ) -> None:
        """Cleanup returns the number of expired rows deleted."""
        mock_pool._conn.execute.return_value = "DELETE 42"
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = await dedup.cleanup_expired(now)
        assert result == 42

    @pytest.mark.asyncio
    async def test_returns_zero_when_nothing_expired(
        self, dedup: EventDedup, mock_pool: AsyncMock
    ) -> None:
        """Cleanup returns 0 when no rows are expired."""
        mock_pool._conn.execute.return_value = "DELETE 0"
        now = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        result = await dedup.cleanup_expired(now)
        assert result == 0

    @pytest.mark.asyncio
    async def test_uses_current_time_when_none(
        self, dedup: EventDedup, mock_pool: AsyncMock
    ) -> None:
        """When now=None, uses datetime.now(utc)."""
        mock_pool._conn.execute.return_value = "DELETE 0"
        result = await dedup.cleanup_expired(None)
        assert result == 0
        # Verify execute was called with a datetime argument
        call_args = mock_pool._conn.execute.call_args[0]
        assert isinstance(call_args[1], datetime)

    @pytest.mark.asyncio
    async def test_passes_timestamp_to_query(
        self, dedup: EventDedup, mock_pool: AsyncMock
    ) -> None:
        """Verify the SQL query receives the correct timestamp."""
        mock_pool._conn.execute.return_value = "DELETE 0"
        now = datetime(2024, 3, 15, 8, 30, 0, tzinfo=timezone.utc)
        await dedup.cleanup_expired(now)
        call_args = mock_pool._conn.execute.call_args[0]
        assert now in call_args


# =============================================================================
# StageResult tests
# =============================================================================


class TestStageResult:
    """Tests for the StageResult data model."""

    def test_pass_result(self) -> None:
        result = StageResult(action="pass")
        assert result.action == "pass"
        assert result.reason is None

    def test_drop_result(self) -> None:
        result = StageResult(action="drop", reason="duplicate")
        assert result.action == "drop"
        assert result.reason == "duplicate"

    def test_frozen(self) -> None:
        result = StageResult(action="pass")
        with pytest.raises(Exception):
            result.action = "drop"  # type: ignore[misc]
