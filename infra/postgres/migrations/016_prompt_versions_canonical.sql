-- 011_prompt_versions.sql
-- Canonical prompt versioning schema migration.
--
-- Defines the canonical schema for the prompt versioning audit trail:
--   1. shared.prompt_versions — Append-only history of prompt file changes
--
-- Each row records one committed change to a prompt file under
-- `platform/prompts/`, written by the Admin Dashboard prompt editor
-- (`POST /api/v1/prompts/{name}/commit`). The row stores the prompt name,
-- the SHA-256 of the new content, the admin user who made the change,
-- and the URL of the Bitbucket draft PR that carries the commit.
--
-- The table is the source of the `prompt_updated` audit event and
-- backs the prompt history view in the Admin Dashboard. Two access patterns
-- are supported:
--   * Newest-first version list for a single prompt
--     → idx_prompt_versions_name_time (prompt_name, created_at DESC)
--   * Idempotent re-commit detection (skip "no-op" PRs)
--     → uq_prompt_versions_name_hash (prompt_name, content_hash)
--
-- Idempotent — uses IF NOT EXISTS guards so re-running on an already-migrated
-- database (including one where 010_prompt_versions.sql has been applied) is
-- a no-op for the table/index and simply ensures the UNIQUE constraint is
-- present.
--

-- =============================================================================
-- 1. prompt_versions — Prompt change audit trail
-- =============================================================================
-- Column notes:
--   prompt_name   — Relative file name under platform/prompts/ (e.g. 'planner.md')
--   content_hash  — SHA-256 of the new prompt content (hex-encoded)
--   changed_by    — Admin user identifier (subject from auth context)
--   pr_url        — Bitbucket draft PR URL; NULL until the PR is created
CREATE TABLE IF NOT EXISTS shared.prompt_versions (
    id            BIGSERIAL    PRIMARY KEY,
    prompt_name   TEXT         NOT NULL,
    content_hash  TEXT         NOT NULL,
    changed_by    TEXT         NOT NULL,
    pr_url        TEXT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Most common lookup: version history for a given prompt, newest first
CREATE INDEX IF NOT EXISTS idx_prompt_versions_name_time
    ON shared.prompt_versions (prompt_name, created_at DESC);

-- Prevent duplicate rows for the same (prompt, content) pair — supports
-- "no-op commit" detection in the prompt editor commit endpoint.
CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_versions_name_hash
    ON shared.prompt_versions (prompt_name, content_hash);
