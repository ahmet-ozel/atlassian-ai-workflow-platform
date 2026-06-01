-- 021_cost_tracking_token_usage.sql
--
-- Adds the production token/cost ledger expected by the admin cost
-- dashboard and the ops budget views. Existing code reads
-- shared.cost_tracking; shared.token_usage is a compatibility view
-- using the naming requested by the operational dashboard spec.

CREATE TABLE IF NOT EXISTS shared.cost_tracking (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dept_id TEXT,
    user_id TEXT NOT NULL DEFAULT 'unknown',
    model TEXT NOT NULL DEFAULT 'unknown',
    provider TEXT NOT NULL DEFAULT 'unknown',
    activity_id TEXT,
    prompt_path TEXT,
    prompt_version TEXT,
    token_in INTEGER NOT NULL DEFAULT 0 CHECK (token_in >= 0),
    token_out INTEGER NOT NULL DEFAULT 0 CHECK (token_out >= 0),
    cost_usd NUMERIC(12, 6) NOT NULL DEFAULT 0 CHECK (cost_usd >= 0),
    cost_tag TEXT NOT NULL DEFAULT 'production',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE shared.cost_tracking
    ADD COLUMN IF NOT EXISTS prompt_path TEXT,
    ADD COLUMN IF NOT EXISTS prompt_version TEXT,
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE UNIQUE INDEX IF NOT EXISTS idx_cost_tracking_activity_id
    ON shared.cost_tracking (activity_id)
    WHERE activity_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cost_tracking_dept_created
    ON shared.cost_tracking (dept_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_cost_tracking_model_created
    ON shared.cost_tracking (model, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_cost_tracking_tag_created
    ON shared.cost_tracking (cost_tag, created_at DESC);

CREATE OR REPLACE VIEW shared.token_usage AS
SELECT
    id,
    dept_id,
    user_id,
    provider,
    model,
    activity_id,
    prompt_path,
    prompt_version,
    token_in AS prompt_tokens,
    token_out AS completion_tokens,
    (token_in + token_out) AS total_tokens,
    cost_usd,
    cost_tag,
    metadata,
    created_at
FROM shared.cost_tracking;
