-- =============================================================================
-- Migration 008: budget_alarm_thresholds
-- =============================================================================
-- Requirement: R13.1 (platform-real-usage-gaps)
--
-- Creates the automation.budget_alarm_thresholds table for per-department
-- budget alarm configuration. Each row defines a threshold percentage at which
-- a notification is fired (slack/email/teams) for a given period and scope.
--
-- Idempotent: uses IF NOT EXISTS / IF NOT EXISTS guards.
-- =============================================================================

CREATE TABLE IF NOT EXISTS automation.budget_alarm_thresholds (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  dept_id         TEXT NOT NULL REFERENCES automation.departments(id) ON DELETE CASCADE,
  period          TEXT NOT NULL CHECK (period IN ('weekly', 'monthly')),
  scope           TEXT NOT NULL CHECK (scope IN ('user', 'dept')),
  threshold_pct   INTEGER NOT NULL CHECK (threshold_pct BETWEEN 1 AND 99) DEFAULT 70,
  notify_channel  TEXT NOT NULL CHECK (notify_channel IN ('slack', 'email', 'teams')),
  last_alarmed_at TIMESTAMPTZ,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (dept_id, period, scope)
);

CREATE INDEX IF NOT EXISTS idx_budget_alarms_dept
  ON automation.budget_alarm_thresholds(dept_id);
