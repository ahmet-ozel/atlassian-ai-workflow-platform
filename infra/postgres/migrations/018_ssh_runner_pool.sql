-- 013_ssh_runner_pool.sql
-- Spec: platform-quick-fixes — Task 7.1 (SSH Runner Pool Migration)
--
-- Creates the infrastructure schema and tables required by the multi-SSH
-- host pool feature (R4 / G5):
--   1. infrastructure schema (new)
--   2. infrastructure.ssh_runners — SSH runner host definitions
--   3. infrastructure.dept_ssh_assignments — Department ↔ Runner many-to-many
--   4. idx_dept_ssh_assignments_runner — Index for runner_resolver least-busy lookup
--
-- The multi-runner pool replaces the single SSH_HOST env variable with a
-- database-backed runner registry. Departments are assigned to one or more
-- runners, and the runner_resolver activity selects the least-busy runner
-- at workflow start time.
--
-- Idempotent — uses IF NOT EXISTS guards so re-running on an already-migrated
-- database is a no-op.
--
-- Requirements: 4.1, 4.2

-- =============================================================================
-- 1. Schema: infrastructure
-- =============================================================================
CREATE SCHEMA IF NOT EXISTS infrastructure;

-- =============================================================================
-- 2. infrastructure.ssh_runners — SSH runner host definitions
-- =============================================================================
-- Status values:
--   'active'      — Runner is available for workflow assignment
--   'disabled'    — Runner manually disabled by operator
--   'quarantine'  — Runner auto-quarantined due to repeated failures
CREATE TABLE IF NOT EXISTS infrastructure.ssh_runners (
    runner_id   TEXT PRIMARY KEY,
    host        TEXT NOT NULL,
    port        INT NOT NULL DEFAULT 22,
    username    TEXT NOT NULL,
    vault_path  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'disabled', 'quarantine')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- =============================================================================
-- 3. infrastructure.dept_ssh_assignments — Department ↔ Runner many-to-many
-- =============================================================================
-- Priority: lower value = higher precedence (used as tiebreaker when
-- multiple runners have equal active workflow counts).
-- ON DELETE CASCADE on dept_id: removing a department cleans up assignments.
-- ON DELETE RESTRICT on runner_id: cannot delete a runner that still has
-- department assignments (operator must unassign first).
CREATE TABLE IF NOT EXISTS infrastructure.dept_ssh_assignments (
    dept_id     TEXT NOT NULL REFERENCES automation.departments(id) ON DELETE CASCADE,
    runner_id   TEXT NOT NULL REFERENCES infrastructure.ssh_runners(runner_id) ON DELETE RESTRICT,
    priority    INT NOT NULL DEFAULT 100,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (dept_id, runner_id)
);

-- =============================================================================
-- 4. Index for runner_resolver least-busy lookup
-- =============================================================================
-- Supports the runner_resolver activity's query that joins on runner_id
-- to count active workflows per runner and select the least-busy one.
CREATE INDEX IF NOT EXISTS idx_dept_ssh_assignments_runner
    ON infrastructure.dept_ssh_assignments(runner_id);
