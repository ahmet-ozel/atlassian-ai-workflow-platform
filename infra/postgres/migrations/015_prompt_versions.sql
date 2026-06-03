-- 010_prompt_versions.sql
-- Prompt versioning schema migration.
--
-- Adds the table used by the prompt versioning feature:
--   1. shared.prompt_versions — Audit trail for prompt file changes
--
-- Each row represents one committed change to a prompt file in
-- `platform/prompts/`, captured by the Admin Dashboard prompt editor
-- (`POST /api/v1/prompts/{name}/commit`). The row records the prompt
-- name, the SHA of the new content, the admin user who made the change,
-- and the URL of the Bitbucket draft PR that carries the commit.
--
-- The table is append-only and serves as the source for the
-- `prompt_updated` audit event. The (prompt_name, created_at DESC)
-- index supports the most common lookup pattern: "give me the version
-- history for prompt X, newest first".
--
-- Idempotent — uses IF NOT EXISTS guards so re-running on an already-migrated
-- database is a no-op.
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
    path          TEXT,
    commit_hash   TEXT,
    body_hash     TEXT,
    seen_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Backward-compatible bridge for databases bootstrapped by
-- infra/postgres/20_ops.sql, which created this table as
-- (path, commit_hash, body_hash, seen_at). The prompt editor needs
-- (id, prompt_name, content_hash, changed_by, pr_url, created_at).
CREATE SEQUENCE IF NOT EXISTS shared.prompt_versions_id_seq;

ALTER TABLE shared.prompt_versions
    ADD COLUMN IF NOT EXISTS id BIGINT;
ALTER TABLE shared.prompt_versions
    ALTER COLUMN id SET DEFAULT nextval('shared.prompt_versions_id_seq');
UPDATE shared.prompt_versions
    SET id = nextval('shared.prompt_versions_id_seq')
    WHERE id IS NULL;
ALTER TABLE shared.prompt_versions
    ALTER COLUMN id SET NOT NULL;

ALTER TABLE shared.prompt_versions
    ADD COLUMN IF NOT EXISTS prompt_name TEXT;
ALTER TABLE shared.prompt_versions
    ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE shared.prompt_versions
    ADD COLUMN IF NOT EXISTS changed_by TEXT;
ALTER TABLE shared.prompt_versions
    ADD COLUMN IF NOT EXISTS pr_url TEXT;
ALTER TABLE shared.prompt_versions
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;

UPDATE shared.prompt_versions
    SET
        prompt_name = COALESCE(prompt_name, path, 'unknown'),
        content_hash = COALESCE(content_hash, body_hash, commit_hash, ''),
        changed_by = COALESCE(changed_by, 'system:prompt-loader'),
        created_at = COALESCE(created_at, seen_at, NOW())
    WHERE prompt_name IS NULL
       OR content_hash IS NULL
       OR changed_by IS NULL
       OR created_at IS NULL;

ALTER TABLE shared.prompt_versions
    ALTER COLUMN prompt_name SET NOT NULL;
ALTER TABLE shared.prompt_versions
    ALTER COLUMN content_hash SET NOT NULL;
ALTER TABLE shared.prompt_versions
    ALTER COLUMN changed_by SET NOT NULL;
ALTER TABLE shared.prompt_versions
    ALTER COLUMN created_at SET DEFAULT NOW();
ALTER TABLE shared.prompt_versions
    ALTER COLUMN created_at SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_versions_id
    ON shared.prompt_versions (id);

-- Most common lookup: version history for a given prompt, newest first
CREATE INDEX IF NOT EXISTS idx_prompt_versions_name_time
    ON shared.prompt_versions (prompt_name, created_at DESC);
