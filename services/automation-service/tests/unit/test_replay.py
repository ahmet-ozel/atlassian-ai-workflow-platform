"""Unit tests for decision.replay module.

Validates SHA-256 payload hash computation, canonical JSON normalization,
and the check_and_insert / cleanup_expired contract.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure the automation-service src is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from datetime import datetime, timedelta, timezone

from decision.replay import check_and_insert, cleanup_expired, compute_payload_hash


# =============================================================================
# compute_payload_hash tests
# =============================================================================


class TestComputePayloadHash:
    """Tests for the compute_payload_hash() function."""

    def test_deterministic_same_input(self) -> None:
        body = b'{"event": "jira:issue_created", "user": "alice"}'
        assert compute_payload_hash(body) == compute_payload_hash(body)

    def test_key_order_independent(self) -> None:
        """Semantically identical JSON with different key order  same hash."""
        body1 = b'{"b": 2, "a": 1}'
        body2 = b'{"a": 1, "b": 2}'
        assert compute_payload_hash(body1) == compute_payload_hash(body2)

    def test_whitespace_independent(self) -> None:
        """Different whitespace formatting  same hash."""
        body_compact = b'{"key":"value"}'
        body_pretty = b'{\n  "key": "value"\n}'
        assert compute_payload_hash(body_compact) == compute_payload_hash(body_pretty)

    def test_different_payloads_produce_different_hashes(self) -> None:
        body1 = b'{"event": "jira:issue_created"}'
        body2 = b'{"event": "jira:issue_updated"}'
        assert compute_payload_hash(body1) != compute_payload_hash(body2)

    def test_returns_valid_sha256_hex(self) -> None:
        body = b'{"test": true}'
        result = compute_payload_hash(body)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_canonical_json_format(self) -> None:
        """Verify the canonical form: sorted keys, compact separators, no ASCII escape."""
        body = b'{"z": 1, "a": "\\u00e9"}'  # é character
        payload = json.loads(body)
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        expected = hashlib.sha256(canonical).hexdigest()
        assert compute_payload_hash(body) == expected

    def test_unicode_preserved(self) -> None:
        """Non-ASCII characters are preserved (ensure_ascii=False)."""
        body = '{"message": "Görev alındı"}'.encode("utf-8")
        result = compute_payload_hash(body)
        # Verify it's a valid hash (no encoding errors)
        assert len(result) == 64

    def test_nested_objects_sorted(self) -> None:
        """Nested objects also have their keys sorted."""
        body1 = b'{"outer": {"z": 1, "a": 2}}'
        body2 = b'{"outer": {"a": 2, "z": 1}}'
        assert compute_payload_hash(body1) == compute_payload_hash(body2)

    def test_empty_object(self) -> None:
        body = b"{}"
        result = compute_payload_hash(body)
        expected = hashlib.sha256(b"{}").hexdigest()
        assert result == expected


# =============================================================================
# check_and_insert tests (with mocked asyncpg pool)
# =============================================================================


class TestCheckAndInsert:
    """Tests for the check_and_insert() function using mocked asyncpg."""

    @pytest.fixture
    def mock_pool(self) -> AsyncMock:
        pool = AsyncMock(spec=["acquire"])
        conn = AsyncMock()
        # Make pool.acquire() work as async context manager
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire.return_value = ctx
        pool._conn = conn  # expose for assertions
        return pool

    @pytest.mark.asyncio
    async def test_new_hash_returns_true(self, mock_pool: AsyncMock) -> None:
        """First insertion of a hash returns True."""
        mock_pool._conn.fetchrow.return_value = {"event_hash": "abc123"}
        result = await check_and_insert(
            mock_pool, "abc123", timedelta(days=7)
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_duplicate_hash_returns_false(self, mock_pool: AsyncMock) -> None:
        """Duplicate hash (ON CONFLICT DO NOTHING) returns False."""
        mock_pool._conn.fetchrow.return_value = None
        result = await check_and_insert(
            mock_pool, "abc123", timedelta(days=7)
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_passes_correct_parameters(self, mock_pool: AsyncMock) -> None:
        """Verify the SQL query receives the correct hash and TTL."""
        mock_pool._conn.fetchrow.return_value = {"event_hash": "deadbeef"}
        ttl = timedelta(days=7)
        await check_and_insert(mock_pool, "deadbeef", ttl)

        call_args = mock_pool._conn.fetchrow.call_args
        assert "deadbeef" in call_args[0]
        assert ttl in call_args[0]


# =============================================================================
# cleanup_expired tests (with mocked asyncpg pool)
# =============================================================================


class TestCleanupExpired:
    """Tests for the cleanup_expired() function using mocked asyncpg."""

    @pytest.fixture
    def mock_pool(self) -> AsyncMock:
        pool = AsyncMock(spec=["acquire"])
        conn = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool.acquire.return_value = ctx
        pool._conn = conn
        return pool

    @pytest.mark.asyncio
    async def test_returns_deleted_count(self, mock_pool: AsyncMock) -> None:
        """Returns the number of rows deleted."""
        mock_pool._conn.execute.return_value = "DELETE 5"
        now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = await cleanup_expired(mock_pool, now)
        assert result == 5

    @pytest.mark.asyncio
    async def test_returns_zero_when_nothing_expired(
        self, mock_pool: AsyncMock
    ) -> None:
        """Returns 0 when no rows are expired."""
        mock_pool._conn.execute.return_value = "DELETE 0"
        now = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = await cleanup_expired(mock_pool, now)
        assert result == 0

    @pytest.mark.asyncio
    async def test_passes_now_parameter(self, mock_pool: AsyncMock) -> None:
        """Verify the SQL query receives the correct timestamp."""
        mock_pool._conn.execute.return_value = "DELETE 0"
        now = datetime(2024, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
        await cleanup_expired(mock_pool, now)

        call_args = mock_pool._conn.execute.call_args
        assert now in call_args[0]
