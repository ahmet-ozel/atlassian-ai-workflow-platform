-- 009_workflow_iterations.sql
-- Spec: platform-gap-fill — Task 11.2 (Iteration Re-Run Schema)
--
-- Adds the table required by the iteration manager (R12.x):
--   1. shared.workflow_iterations — Per-issue iteration tracking
--
-- Each row represents one iter-N attempt for a Jira issue. New iterations
-- triggered by `[iterate]` comments increment iteration_number and reference
-- the previous iteration's branch / PR / workspace so the iteration manager
-- can decide whether to commit to the same PR or open a new branch.
--
-- The (issue_key, iteration_number) pair is unique; an additional index on
-- issue_key supports the most common lookup pattern: "give me all iterations
-- for issue X, latest first".
--
-- Idempotent — uses IF NOT EXISTS guards so re-running on an already-migrated
-- database is a no-op.
--
-- Requirements: 12.7

-- =============================================================================
-- 1. workflow_iterations — Iteration tracking for [iterate] re-run flow
-- =============================================================================
-- Status values (application-enforced):
--   'pending'      — Iteration row created, workflow not yet started
--   'in_progress'  — Workflow currently executing
--   'completed'    — Workflow finished successfully
--   'failed'       — Workflow terminated with error
CREATE TABLE IF NOT EXISTS shared.workflow_iterations (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    issue_key         TEXT         NOT NULL,
    iteration_number  INTEGER      NOT NULL,
    workflow_id       TEXT         NOT NULL,
    previous_branch   TEXT,
    previous_pr_id    INTEGER,
    workspace_path    TEXT         NOT NULL,
    status            TEXT         NOT NULL,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_workflow_iterations_issue_iter
        UNIQUE (issue_key, iteration_number)
);

-- Most common lookup: all iterations for a given issue
CREATE INDEX IF NOT EXISTS idx_workflow_iterations_issue_key
    ON shared.workflow_iterations (issue_key);
