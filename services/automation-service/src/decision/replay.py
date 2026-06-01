"""SHA-256 payload-hash replay guard for webhook deduplication.

Prevents duplicate processing of the same webhook delivery by storing
a SHA-256 hash of the canonical payload in ``automation.processed_events``.
Hashes expire after a configurable TTL (default 7 days) and are cleaned
up by a scheduled activity.

Requirements: 2.3, 2.4, 10.5
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import asyncpg


async def check_and_insert(
    db: asyncpg.Pool,
    payload_hash: str,
    ttl: timedelta,
) -> bool:
    """Atomically check and insert a payload hash for deduplication.

    Uses ``INSERT ... ON CONFLICT DO NOTHING RETURNING ...`` to achieve
    atomic dedup without race conditions between concurrent requests.

    Parameters
    ----------
    db:
        asyncpg connection pool connected to the automation database.
    payload_hash:
        The SHA-256 hex digest of the canonical webhook payload.
    ttl:
        Time-to-live for the hash entry (e.g., ``timedelta(days=7)``).

    Returns
    -------
    bool
        ``True`` if the hash was newly inserted (first occurrence);
        ``False`` if it already existed (duplicate delivery).
    """
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO automation.processed_events (event_hash, expires_at)
            VALUES ($1, now() + $2::interval)
            ON CONFLICT (event_hash) DO NOTHING
            RETURNING event_hash
            """,
            payload_hash,
            ttl,
        )
        return row is not None


async def cleanup_expired(db: asyncpg.Pool, now: datetime) -> int:
    """Delete expired entries from ``automation.processed_events``.

    Called by a scheduled activity (daily) to keep the table bounded.

    Parameters
    ----------
    db:
        asyncpg connection pool connected to the automation database.
    now:
        The current timestamp to compare against ``expires_at``.
        Should be passed from the caller (e.g., ``workflow.now()`` in
        Temporal context) to maintain determinism.

    Returns
    -------
    int
        The number of rows deleted.
    """
    async with db.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM automation.processed_events
            WHERE expires_at < $1
            """,
            now,
        )
        # asyncpg execute returns a status string like "DELETE 5"
        return int(result.split()[-1])


def compute_payload_hash(raw_body: bytes) -> str:
    """Compute a deterministic SHA-256 hash of a webhook payload.

    The payload is first decoded to a Python object, then re-serialized
    as canonical JSON (sorted keys, compact separators, no ASCII escaping)
    before hashing. This ensures that semantically identical payloads
    produce the same hash regardless of original formatting.

    Parameters
    ----------
    raw_body:
        The raw request body bytes as received from the webhook.

    Returns
    -------
    str
        The lowercase hex SHA-256 digest of the canonical JSON.
    """
    payload = json.loads(raw_body)
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
