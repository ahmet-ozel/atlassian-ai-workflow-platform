-- 10_automation.sql
-- P0 Critical Path: automation schema migration
-- Idempotent — safe to run multiple times without side effects.
-- Schema `automation` is created in 00_schemas.sql.

-- =============================================================================
-- 1. departments
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.departments (
    id              TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    default_language TEXT NOT NULL DEFAULT 'tr',
    web_search_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    mode            TEXT NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_departments_mode
        CHECK (mode IN ('active', 'shadow', 'paused', 'decommissioned'))
);

-- =============================================================================
-- 2. department_bots
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.department_bots (
    id              SERIAL PRIMARY KEY,
    department_id   TEXT NOT NULL
                        REFERENCES automation.departments(id) ON DELETE CASCADE,
    service         TEXT NOT NULL,
    credential_ref  TEXT NOT NULL,
    account_id      TEXT,
    username        TEXT,
    deployment      TEXT NOT NULL DEFAULT 'cloud',

    CONSTRAINT chk_department_bots_service
        CHECK (service IN ('jira', 'bitbucket', 'confluence')),
    CONSTRAINT chk_department_bots_deployment
        CHECK (deployment IN ('cloud', 'dc')),
    CONSTRAINT uq_department_bots_dept_service
        UNIQUE (department_id, service)
);

-- =============================================================================
-- 3. department_project_keys
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.department_project_keys (
    id              SERIAL PRIMARY KEY,
    department_id   TEXT NOT NULL
                        REFERENCES automation.departments(id) ON DELETE CASCADE,
    project_key     TEXT NOT NULL UNIQUE
);

-- =============================================================================
-- 4. department_space_keys
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.department_space_keys (
    id              SERIAL PRIMARY KEY,
    department_id   TEXT NOT NULL
                        REFERENCES automation.departments(id) ON DELETE CASCADE,
    space_key       TEXT NOT NULL UNIQUE
);

-- =============================================================================
-- 5. repo_mappings
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.repo_mappings (
    id                  SERIAL PRIMARY KEY,
    department_id       TEXT NOT NULL
                            REFERENCES automation.departments(id) ON DELETE CASCADE,
    bitbucket_workspace TEXT NOT NULL,
    bitbucket_repo      TEXT NOT NULL,
    jira_project_key    TEXT NOT NULL,
    default_branch      TEXT NOT NULL DEFAULT 'develop',

    CONSTRAINT uq_repo_mappings_workspace_repo
        UNIQUE (bitbucket_workspace, bitbucket_repo)
);

-- =============================================================================
-- 6. processed_events
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.processed_events (
    event_hash  TEXT PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL DEFAULT (now() + INTERVAL '7 days')
);

-- =============================================================================
-- 7. work_items
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.work_items (
    id              SERIAL PRIMARY KEY,
    workflow_id     TEXT NOT NULL UNIQUE,
    department_id   TEXT NOT NULL
                        REFERENCES automation.departments(id) ON DELETE CASCADE,
    issue_key       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    workflow_type   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_work_items_status
        CHECK (status IN ('pending', 'running', 'completed', 'failed'))
);

-- =============================================================================
-- Indexes
-- =============================================================================
CREATE INDEX IF NOT EXISTS idx_processed_events_expires_at
    ON automation.processed_events (expires_at);

CREATE INDEX IF NOT EXISTS idx_work_items_issue_key
    ON automation.work_items (issue_key);

CREATE INDEX IF NOT EXISTS idx_work_items_status
    ON automation.work_items (status);

-- =============================================================================
-- Foundation schema additions.
--      "Postgres şeması (yeni / değişen tablolar)"
--
-- This block aligns the existing automation.departments table with the
-- target schema (mirror of departments.json via config_json + RLS) and
-- introduces audit_events and probe_artifacts tables.
--
-- Idempotent — safe to run multiple times.
-- =============================================================================

-- Required for probe_artifacts.id default (gen_random_uuid()).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -----------------------------------------------------------------------------
-- 8. departments — schema alignment (config_json mirror + RLS)
-- -----------------------------------------------------------------------------
-- Design contract:
--   mode CHECK IN ('active','shadow','disabled')
--   config_json JSONB NOT NULL  -- mirror of departments.json entry
--   ENABLE + FORCE ROW LEVEL SECURITY
--   POLICY dept_isolation USING (id = current_setting('app.current_dept_id', true)
--                            OR  current_setting('app.current_role',    true) = 'admin')

-- Add config_json column if missing. Existing rows (if any) get '{}'::jsonb so
-- the NOT NULL constraint can be applied without a destructive backfill.
ALTER TABLE automation.departments
    ADD COLUMN IF NOT EXISTS config_json JSONB NOT NULL DEFAULT '{}'::jsonb;

-- Replace the legacy mode CHECK constraint with the current enum.
-- Existing rows with legacy modes ('paused','decommissioned') must be migrated
-- to ('disabled') by a separate data migration; this block only swaps the
-- constraint shape so new writes match the foundation schema.
ALTER TABLE automation.departments
    DROP CONSTRAINT IF EXISTS chk_departments_mode;

ALTER TABLE automation.departments
    ADD CONSTRAINT chk_departments_mode
        CHECK (mode IN ('active', 'shadow', 'disabled'));

ALTER TABLE automation.departments ENABLE ROW LEVEL SECURITY;
ALTER TABLE automation.departments FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS dept_isolation ON automation.departments;
CREATE POLICY dept_isolation ON automation.departments
    USING (
        id = current_setting('app.current_dept_id', true)
        OR current_setting('app.current_role', true) = 'admin'
    );

-- -----------------------------------------------------------------------------
-- 9. audit_events - RBAC audit trail with mandatory actor_role
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS automation.audit_events (
    id          BIGSERIAL PRIMARY KEY,
    actor_id    TEXT NOT NULL,
    actor_role  TEXT NOT NULL,
    dept_id     TEXT NULL,
    action      TEXT NOT NULL,
    resource    TEXT NOT NULL,
    result      TEXT NOT NULL,
    payload     JSONB NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- actor_role MUST be present and one of the four RBAC roles
    -- plus the synthetic 'system' role for unattended events.
    CONSTRAINT chk_audit_events_actor_role
        CHECK (actor_role IS NOT NULL
               AND actor_role IN ('viewer','lead','admin','dept_admin','system')),
    CONSTRAINT chk_audit_events_result
        CHECK (result IN ('ok','denied','error'))
);

CREATE INDEX IF NOT EXISTS idx_audit_events_dept_created
    ON automation.audit_events (dept_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_events_action_created
    ON automation.audit_events (action, created_at DESC);

ALTER TABLE automation.audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE automation.audit_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS audit_dept_isolation ON automation.audit_events;
CREATE POLICY audit_dept_isolation ON automation.audit_events
    USING (
        dept_id IS NULL                                -- system events visible
        OR dept_id = current_setting('app.current_dept_id', true)
        OR current_setting('app.current_role', true) = 'admin'
    );

-- -----------------------------------------------------------------------------
-- 10. probe_artifacts - partial-orphan tracking for failed probe cleanup
-- -----------------------------------------------------------------------------
-- Stores _AI_PROBE_<unix_ts>_DELETE_ME artifacts that the probe runner
-- could not clean up (e.g. Confluence draft delete failure). Admins manage
-- these via /admin/probe-artifacts.
CREATE TABLE IF NOT EXISTS automation.probe_artifacts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dept_id         TEXT NOT NULL
                        REFERENCES automation.departments(id) ON DELETE CASCADE,
    service         TEXT NOT NULL,
    artifact_type   TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    title_or_name   TEXT NOT NULL,                    -- "_AI_PROBE_<ts>_DELETE_ME"
    state           TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    cleared_at      TIMESTAMPTZ NULL,

    CONSTRAINT chk_probe_artifacts_service
        CHECK (service IN ('jira','bitbucket','confluence')),
    CONSTRAINT chk_probe_artifacts_artifact_type
        CHECK (artifact_type IN ('confluence_page','bitbucket_branch','jira_comment')),
    CONSTRAINT chk_probe_artifacts_state
        CHECK (state IN ('partial_orphan','cleared'))
);

CREATE INDEX IF NOT EXISTS idx_probe_artifacts_dept_state
    ON automation.probe_artifacts (dept_id, state);
