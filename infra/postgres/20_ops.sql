-- 20_ops.sql
-- Ops schema additions.
-- Ops-scope additions on top of foundation 10_automation.sql.
-- Idempotent — safe to run multiple times without side effects.
--
-- Boot order (alphabetic under /docker-entrypoint-initdb.d):
--   00_schemas.sql  → creates `automation`, `assistant`, `shared`,
--                     `temporal` schemas.
--   10_automation.sql → creates `automation.departments`,
--                       `automation.audit_events`, etc.
--   20_ops.sql      → THIS FILE: cost/budget/notification/prompt
--                     versions/feature flags ops tables.
--
-- Schema choice: tables live in the `shared` schema.
--   - LLM activity cost records are stored in `shared.cost_tracking`.
--   - These tables are cross-cutting ops/analytics state (cost
--     tracking, notification deliveries, prompt cache, runtime
--     toggles), not core automation domain state, and therefore
--     belong alongside `shared.audit_log` (50_shared.sql).
--   - FKs to `automation.departments(id)` are cross-schema, which
--     is supported by Postgres without restriction.
--
-- RLS pattern mirrors foundation 10_automation.sql:
--   ENABLE + FORCE ROW LEVEL SECURITY, dept_isolation policy that
--   compares `dept_id` to `current_setting('app.current_dept_id', true)`
--   with an admin escape hatch via `current_setting('app.current_role',
--   true) = 'admin'`. The `db-shared` helper is
--   responsible for setting these GUCs per request.
--
-- Defines the ops tables used for prompt auditing, feature flags, cost tracking,
-- budget enforcement, and notifications.
-- Design ref: design.md "Postgres Şema Eklemeleri (`infra/postgres/init/20_ops.sql`)"


-- ===========================================================================
-- 1. shared.cost_tracking — per-LLM-activity cost record (idempotent insert)
-- ===========================================================================
-- Cost_Tracker writes token_in/out, model, provider, and cost_usd to
-- shared.cost_tracking on every LLM activity. Budget cap enforcement reads
-- this table, and rows are retained as audit-grade records.
-- Idempotency: `activity_id` is the Temporal activity id, which is
-- globally unique per workflow execution. The UNIQUE constraint plus
-- `INSERT ... ON CONFLICT (activity_id) DO NOTHING` from
-- `libs/cost-tracking/CostTracker.record(...)` makes the write safely
-- replayable on activity retry.
-- `cost_tag` partitions production usage from sandbox prompt tests
-- and probe-time LLM calls so neither contaminates dept
-- budget queries (BudgetCapPolicy filters on `cost_tag='production'`).
CREATE TABLE IF NOT EXISTS shared.cost_tracking (
    id              BIGSERIAL PRIMARY KEY,
    activity_id     TEXT NOT NULL UNIQUE,
    workflow_id     TEXT NULL,
    dept_id         TEXT NOT NULL
                        REFERENCES automation.departments(id)
                        ON DELETE CASCADE,
    user_id         TEXT NULL,
    model           TEXT NOT NULL,
    provider        TEXT NOT NULL,
    token_in        INTEGER NOT NULL,
    token_out       INTEGER NOT NULL,
    cost_usd        NUMERIC(12, 6) NOT NULL,
    cost_tag        TEXT NOT NULL DEFAULT 'production',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_cost_tracking_provider
        CHECK (provider IN ('vllm', 'openai', 'anthropic')),
    CONSTRAINT chk_cost_tracking_token_in
        CHECK (token_in >= 0),
    CONSTRAINT chk_cost_tracking_token_out
        CHECK (token_out >= 0),
    CONSTRAINT chk_cost_tracking_cost_usd
        CHECK (cost_usd >= 0),
    CONSTRAINT chk_cost_tracking_cost_tag
        CHECK (cost_tag IN ('production', 'sandbox', 'probe'))
);

-- Hot-path indexes for budget/cost panel queries.
-- `idx_cost_dept_time`: `BudgetCapPolicy._usage(...)` weekly/monthly
-- aggregation by dept; admin /costs panel dept-bazlı dağılım.
CREATE INDEX IF NOT EXISTS idx_cost_dept_time
    ON shared.cost_tracking (dept_id, created_at DESC);

-- `idx_cost_user_time`: per-user weekly/monthly cap enforcement
-- for user-level limits and Streamlit "kendi cost widget" reads.
-- Partial index — `user_id` is NULL for system/automation events
-- (e.g. automation-driven workflows without an attributed end-user)
-- and indexing those rows would only inflate the index without
-- helping any per-user query.
CREATE INDEX IF NOT EXISTS idx_cost_user_time
    ON shared.cost_tracking (user_id, created_at DESC)
    WHERE user_id IS NOT NULL;

ALTER TABLE shared.cost_tracking ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared.cost_tracking FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS cost_dept_isolation ON shared.cost_tracking;
CREATE POLICY cost_dept_isolation ON shared.cost_tracking
    USING (
        dept_id = current_setting('app.current_dept_id', true)
        OR current_setting('app.current_role', true) = 'admin'
    );


-- ===========================================================================
-- 2. shared.budget_caps — dept- and user-level weekly/monthly limits
-- ===========================================================================
-- Budget caps are the source for HTTP 429 enforcement.
-- Mirror of `departments.json` `budget_caps` block (design.md
-- "departments.json Şema Eklemeleri") — a query-friendly projection
-- so `BudgetCapPolicy.enforce(...)` can join with
-- `shared.cost_tracking` aggregates in a single SQL round-trip
-- instead of re-parsing JSON.
-- Source of truth remains `config/departments.json`; a small
-- reconciliation helper keeps this table in sync on
-- config reload.
CREATE TABLE IF NOT EXISTS shared.budget_caps (
    dept_id             TEXT PRIMARY KEY
                            REFERENCES automation.departments(id)
                            ON DELETE CASCADE,
    weekly_usd_dept     NUMERIC(12, 2) NOT NULL,
    weekly_usd_user     NUMERIC(12, 2) NOT NULL,
    monthly_usd_dept    NUMERIC(12, 2) NOT NULL,
    monthly_usd_user    NUMERIC(12, 2) NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_budget_caps_weekly_dept
        CHECK (weekly_usd_dept  >= 0),
    CONSTRAINT chk_budget_caps_weekly_user
        CHECK (weekly_usd_user  >= 0),
    CONSTRAINT chk_budget_caps_monthly_dept
        CHECK (monthly_usd_dept >= 0),
    CONSTRAINT chk_budget_caps_monthly_user
        CHECK (monthly_usd_user >= 0)
);

ALTER TABLE shared.budget_caps ENABLE ROW LEVEL SECURITY;
ALTER TABLE shared.budget_caps FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS budget_dept_isolation ON shared.budget_caps;
CREATE POLICY budget_dept_isolation ON shared.budget_caps
    USING (
        dept_id = current_setting('app.current_dept_id', true)
        OR current_setting('app.current_role', true) = 'admin'
    );


-- ===========================================================================
-- 3. shared.notification_log — Slack/email send history (audit + retry idempotency)
-- ===========================================================================
-- Records Slack/email adapter dispatch, success notifications when
-- notify_on_success=true, mandatory failure notifications regardless of dept
-- config, and audit_prune_failed admin alarm rows.
-- Idempotency: `dedup_key` is sha256 of (workflow_id, channel, kind)
-- so a retried notify call cannot double-deliver. `target` stores
-- a hashed webhook URL or a redacted email; raw recipients never
-- land in this table so log redaction behavior stays consistent.
-- This table intentionally has no `dept_id` column and no RLS:
-- send history is system-internal and read only by admins via the
-- `/notifications` panel. Body content lives off-table; only the
-- sha256 `body_hash` is kept for forensic correlation.
CREATE TABLE IF NOT EXISTS shared.notification_log (
    id              BIGSERIAL PRIMARY KEY,
    dedup_key       TEXT NOT NULL UNIQUE,
    channel         TEXT NOT NULL,
    kind            TEXT NOT NULL,
    target          TEXT NOT NULL,
    body_hash       TEXT NOT NULL,
    status          TEXT NOT NULL,
    error           TEXT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_notification_log_channel
        CHECK (channel IN ('slack', 'email', 'teams')),
    CONSTRAINT chk_notification_log_status
        CHECK (status IN ('sent', 'failed', 'retrying'))
);


-- ===========================================================================
-- 4. shared.prompt_versions — cache of (path, commit_hash, body_hash) for audit
-- ===========================================================================
-- Records the `prompt_version` git short hash for audit events and
-- drill-down across iterations.
-- Authoritative source remains git (`platform/prompts/`,
-- `services/<svc>/prompts/`, `workers/<svc>/prompts/`); this table
-- is a queryable projection populated by
-- `libs/prompts.PromptVersionRecorder.record(...)` on every hot-reload
-- (30s mtime poll). The composite primary key `(path, commit_hash)`
-- makes upserts on identical reloads no-ops — `seen_at` is updated
-- only on a true commit-hash change, so the table is small and
-- monotonically growing per prompt revision.
CREATE TABLE IF NOT EXISTS shared.prompt_versions (
    path            TEXT NOT NULL,
    commit_hash     TEXT NOT NULL,
    body_hash       TEXT NOT NULL,
    seen_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (path, commit_hash)
);


-- ===========================================================================
-- 5. shared.feature_flags — runtime toggles (audit-tracked)
-- ===========================================================================
-- Supports the admin-dashboard `/feature-flags` panel:
--            on/off, description, default, "açıldığında ne değişir"
--            note; toggle aksiyonları audit'e yazılır).
-- This is the canonical store for runtime toggles consumed by every
-- service. Per-dept overrides live in `departments.json`
-- `feature_flag_overrides`; this table holds the
-- platform-wide default and the human-facing copy admins see in
-- the toggle panel. `updated_by` carries the admin actor_id so that
-- the panel can render "last changed by" without joining
-- `automation.audit_events`.
CREATE TABLE IF NOT EXISTS shared.feature_flags (
    name            TEXT PRIMARY KEY,
    enabled         BOOLEAN NOT NULL,
    description     TEXT NOT NULL,
    impact_note     TEXT NOT NULL,
    default_value   BOOLEAN NOT NULL,
    updated_by      TEXT NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
