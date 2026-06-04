-- 024_llm_provider_tuning.sql
-- Adds optional model-tuning knobs to automation.llm_providers.
--
--   reasoning_effort — minimal | low | medium | high  (reasoning-capable models)
--   verbosity        — low | medium | high             (gpt-5 family)
--
-- Both columns are nullable: a NULL value means "leave the upstream
-- default" and the runtime omits the parameter from the request. Only
-- models that advertise support surface these inputs in the UI.
--
-- Idempotent — uses IF NOT EXISTS guards so re-running is a no-op.

ALTER TABLE automation.llm_providers
    ADD COLUMN IF NOT EXISTS reasoning_effort TEXT,
    ADD COLUMN IF NOT EXISTS verbosity        TEXT;

-- Constrain to the documented enums when a value is present; NULL stays
-- valid (no tuning requested).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'llm_providers_reasoning_effort_check'
    ) THEN
        ALTER TABLE automation.llm_providers
            ADD CONSTRAINT llm_providers_reasoning_effort_check CHECK (
                reasoning_effort IS NULL
                OR reasoning_effort IN ('minimal', 'low', 'medium', 'high')
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'llm_providers_verbosity_check'
    ) THEN
        ALTER TABLE automation.llm_providers
            ADD CONSTRAINT llm_providers_verbosity_check CHECK (
                verbosity IS NULL
                OR verbosity IN ('low', 'medium', 'high')
            );
    END IF;
END$$;
