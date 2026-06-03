-- 007_webhook_pipeline_tables.sql
-- Webhook pipeline schema migration.
--
-- Adds tables required by the webhook processing pipeline:
--   1. shared.webhook_dedup — Webhook event deduplication (24h TTL)
--   2. shared.loop_guard_drops — Loop guard drop audit trail
--   3. shared.loop_guard_blocks — Loop guard storm blocks
--
-- Idempotent — uses IF NOT EXISTS guards so re-running on an already-migrated
-- database is a no-op.
--

-- =============================================================================
-- 1. webhook_dedup — Webhook event deduplication
-- =============================================================================
CREATE TABLE IF NOT EXISTS shared.webhook_dedup (
    event_id     TEXT         PRIMARY KEY,
    issue_key    TEXT,
    event_type   TEXT,
    received_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW() + INTERVAL '24 hours'
);

CREATE INDEX IF NOT EXISTS idx_webhook_dedup_expires
    ON shared.webhook_dedup (expires_at);

-- =============================================================================
-- 2. loop_guard_drops — Loop guard drop audit trail
-- =============================================================================
CREATE TABLE IF NOT EXISTS shared.loop_guard_drops (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    issue_key         TEXT         NOT NULL,
    dept_id           TEXT,
    event_type        TEXT         NOT NULL,
    actor_account_id  TEXT         NOT NULL,
    dropped_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_loop_guard_issue_time
    ON shared.loop_guard_drops (issue_key, dropped_at);

-- =============================================================================
-- 3. loop_guard_blocks — Loop guard storm blocks
-- =============================================================================
CREATE TABLE IF NOT EXISTS shared.loop_guard_blocks (
    issue_key      TEXT         PRIMARY KEY,
    blocked_until  TIMESTAMPTZ  NOT NULL,
    reason         TEXT,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
