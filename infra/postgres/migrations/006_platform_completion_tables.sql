-- 006_platform_completion_tables.sql
-- Platform completion database schema.
--
-- Adds tables used by platform completion workflows:
--   1. automation.workflow_steps — Multi-Step Orchestrator step tracking
--   2. automation.output_action_log — Output action execution history
--   3. automation.ssh_healthcheck_log — SSH healthcheck results
--   4. automation.firecrawl_allowlist — Firecrawl egress domain allowlist
--   5. automation.setup_wizard_state — Admin setup wizard progress
--   6. automation.service_test_results — Service test run history
--   7. automation.approval_events — Approval gate audit events
--   8. automation.disk_quota_warnings — Disk quota warning dedup
--
-- Idempotent — uses IF NOT EXISTS guards so re-running on an already-migrated
-- database is a no-op.

-- pgcrypto provides gen_random_uuid(); already created by 10_automation.sql,
-- but we re-declare for migration self-sufficiency.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- =============================================================================
-- 1. workflow_steps — Multi-Step Orchestrator step tracking
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.workflow_steps (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id       TEXT         NOT NULL,
    step_index        INTEGER      NOT NULL,
    step_name         TEXT         NOT NULL,
    status            TEXT         NOT NULL DEFAULT 'pending',
    start_time        TIMESTAMPTZ,
    end_time          TIMESTAMPTZ,
    duration_seconds  FLOAT,
    output_summary    TEXT,
    error             TEXT,
    retry_count       INTEGER      DEFAULT 0,
    input_hash        TEXT,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE(workflow_id, step_index)
);

CREATE INDEX IF NOT EXISTS idx_workflow_steps_workflow_id
    ON automation.workflow_steps (workflow_id);

-- =============================================================================
-- 2. output_action_log — Output action execution history
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.output_action_log (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id     TEXT         NOT NULL,
    issue_key       TEXT         NOT NULL,
    action_type     TEXT         NOT NULL,
    action_index    INTEGER      NOT NULL,
    status          TEXT         NOT NULL,
    error           TEXT,
    executed_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_output_action_log_workflow_id
    ON automation.output_action_log (workflow_id);

CREATE INDEX IF NOT EXISTS idx_output_action_log_issue_key
    ON automation.output_action_log (issue_key);

-- =============================================================================
-- 3. ssh_healthcheck_log — SSH healthcheck results
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.ssh_healthcheck_log (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    host        TEXT         NOT NULL,
    port        INTEGER      NOT NULL,
    healthy     BOOLEAN      NOT NULL,
    error       TEXT,
    checked_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ssh_healthcheck_log_checked_at
    ON automation.ssh_healthcheck_log (checked_at DESC);

-- =============================================================================
-- 4. firecrawl_allowlist — Firecrawl egress domain allowlist
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.firecrawl_allowlist (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    domain      TEXT         NOT NULL UNIQUE,
    description TEXT,
    added_by    TEXT         NOT NULL,
    added_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- =============================================================================
-- 5. setup_wizard_state — Admin setup wizard progress
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.setup_wizard_state (
    step_name    TEXT         PRIMARY KEY,
    status       TEXT         NOT NULL DEFAULT 'pending',
    config_data  JSONB,
    completed_at TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- =============================================================================
-- 6. service_test_results — Service test run history
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.service_test_results (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    service_name    TEXT         NOT NULL,
    run_number      INTEGER      NOT NULL,
    total_tests     INTEGER      NOT NULL,
    passed          INTEGER      NOT NULL,
    failed          INTEGER      NOT NULL,
    duration_ms     INTEGER,
    raw_output      TEXT,
    parsed_results  JSONB,
    executed_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- =============================================================================
-- 7. approval_events — Approval gate audit events
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.approval_events (
    id                   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id          TEXT         NOT NULL,
    issue_key            TEXT         NOT NULL,
    event_type           TEXT         NOT NULL,
    matched_paths        TEXT[],
    approver_account_id  TEXT,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_approval_events_workflow_id
    ON automation.approval_events (workflow_id);

CREATE INDEX IF NOT EXISTS idx_approval_events_issue_key
    ON automation.approval_events (issue_key);

-- =============================================================================
-- 8. disk_quota_warnings — Disk quota warning dedup
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.disk_quota_warnings (
    dept_id    TEXT         NOT NULL,
    warned_at  TIMESTAMPTZ  NOT NULL,
    usage_mb   FLOAT        NOT NULL,
    quota_mb   FLOAT        NOT NULL,
    PRIMARY KEY (dept_id, warned_at)
);

CREATE INDEX IF NOT EXISTS idx_disk_quota_warnings_dept_id
    ON automation.disk_quota_warnings (dept_id);
