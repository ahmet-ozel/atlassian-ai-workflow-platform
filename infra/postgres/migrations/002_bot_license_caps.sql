-- 002_bot_license_caps.sql
-- Spec: platform-mimari-uyumluluk — Task 1.2 (R16 / Q20 — Bot license hard-cap)
--
-- Adds:
--   1. automation.bot_license_caps — per-license hard-cap configuration
--      (max concurrent workflows, daily workflow count, monthly token spend).
--   2. automation.departments.license_id — nullable FK to bot_license_caps,
--      so each department can opt into a license tier.
--
-- Idempotent — uses IF NOT EXISTS guards so re-running on an already-migrated
-- database is a no-op (Requirements: 16.1, 16.2).

-- pgcrypto provides gen_random_uuid(); already created by 10_automation.sql,
-- but we re-declare for migration self-sufficiency.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =============================================================================
-- 1. bot_license_caps
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.bot_license_caps (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    license_id                  TEXT NOT NULL UNIQUE,
    max_concurrent_workflows    INT NOT NULL DEFAULT 10,
    max_workflows_per_day       INT NOT NULL DEFAULT 100,
    max_token_usd_per_month     NUMERIC(10, 2) NOT NULL DEFAULT 1000.00,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =============================================================================
-- 2. departments.license_id  (nullable FK → bot_license_caps.license_id)
-- =============================================================================
ALTER TABLE automation.departments
    ADD COLUMN IF NOT EXISTS license_id TEXT
        REFERENCES automation.bot_license_caps(license_id);
