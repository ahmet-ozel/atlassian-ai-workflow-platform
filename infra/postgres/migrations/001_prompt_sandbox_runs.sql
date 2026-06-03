-- 001_prompt_sandbox_runs.sql
-- Prompt promote endpoint + sandbox run table.
-- Stores every sandbox-test run so /admin/prompts/{path}/promote can verify
-- that a previously-tested draft actually passed before opening a PR.
--
-- Idempotent — safe to apply multiple times. The schema `automation` is created
-- in 00_schemas.sql; `pgcrypto` is required for gen_random_uuid() and is also
-- enabled in 10_automation.sql, but we re-issue CREATE EXTENSION IF NOT EXISTS
-- here so this migration can be applied standalone (e.g. via testcontainer
-- bootstrap in tests/integration/test_prompt_sandbox_runs_migration.py).
--

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS automation.prompt_sandbox_runs (
    id                  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_path         TEXT         NOT NULL,
    draft_branch        TEXT         NOT NULL,
    sample_input        TEXT,
    prompt_body_hash    TEXT,
    response_text       TEXT,
    token_in            INT,
    token_out           INT,
    cost_usd            NUMERIC(10, 4),
    passed              BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    actor_id            TEXT
);

-- Hot path: list recent sandbox runs for a given prompt (newest first) to
-- back the promote endpoint's sandbox_run_id lookup and the prompts UI history.
CREATE INDEX IF NOT EXISTS idx_prompt_sandbox_runs_path_created
    ON automation.prompt_sandbox_runs (prompt_path, created_at DESC);
