-- 009_bootstrap_tokens.sql
-- Bootstrap admin token migration.
--
-- Adds:
--   auth.bootstrap_tokens — one-time bootstrap token storage for initial admin
--   setup. Only the SHA-256 hash of the token is stored; the plain token is
--   never persisted (printed to stdout once at generation time).
--
-- Idempotent — uses IF NOT EXISTS guards so re-running on an already-migrated
-- database is a no-op.

-- pgcrypto provides gen_random_uuid(); already created by 10_automation.sql,
-- but we re-declare for migration self-sufficiency.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Ensure the auth schema exists (also declared in 00_schemas.sql, but we
-- guard here for standalone migration applicability).
CREATE SCHEMA IF NOT EXISTS auth;

-- =============================================================================
-- auth.bootstrap_tokens
-- =============================================================================
CREATE TABLE IF NOT EXISTS auth.bootstrap_tokens (
    id                   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash           TEXT         NOT NULL,          -- SHA-256 hash (plain token asla saklanmaz)
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at           TIMESTAMPTZ  NOT NULL,          -- created_at + 1 hour
    consumed_at          TIMESTAMPTZ,                    -- NULL = henüz kullanılmamış
    consumed_by_user_id  UUID                            -- FK → admin user oluşturulduğunda set edilir
);

-- Index for quick lookup by token hash during validation.
CREATE INDEX IF NOT EXISTS idx_bootstrap_tokens_hash
    ON auth.bootstrap_tokens (token_hash);
