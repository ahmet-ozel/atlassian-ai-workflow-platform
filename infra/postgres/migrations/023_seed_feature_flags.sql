-- 023_seed_feature_flags.sql
-- Seed the admin-dashboard feature flag catalogue used by the UI.

INSERT INTO shared.feature_flags (
    name,
    enabled,
    description,
    impact_note,
    default_value,
    updated_by,
    updated_at
)
VALUES
    (
        'FEATURE_FLAG_AI_ENABLED',
        true,
        'Enables AI/LLM driven workflow and assistant decisions.',
        'When disabled, LLM calls and AI workflow starts are stopped.',
        true,
        'system-seed',
        now()
    ),
    (
        'FEATURE_FLAG_EXECUTION_ENABLED',
        true,
        'Enables SSH/Docker execution runner flows.',
        'When disabled, the runner does not start even if task analysis requires execution.',
        true,
        'system-seed',
        now()
    ),
    (
        'FEATURE_FLAG_TASK_INTAKE_ENABLED',
        false,
        'Enables the optional task-intake-service profile.',
        'When enabled, the external task intake pipeline can run.',
        false,
        'system-seed',
        now()
    ),
    (
        'FEATURE_FLAG_FIRECRAWL_ENABLED',
        false,
        'Enables web research and Firecrawl based content collection flows.',
        'When disabled, steps that require internet research are short-circuited.',
        false,
        'system-seed',
        now()
    ),
    (
        'FEATURE_FLAG_PR_AUTO_MERGE_ENABLED',
        false,
        'Enables bot PR auto-merge attempts.',
        'Default is off for production; PR approval remains with an authorized reviewer.',
        false,
        'system-seed',
        now()
    ),
    (
        'FEATURE_FLAG_AUDIT_PRUNE_ENABLED',
        false,
        'Enables archive/cleanup workflow for old audit records.',
        'When enabled, audit pruning can run according to retention policy.',
        false,
        'system-seed',
        now()
    ),
    (
        'FEATURE_FLAG_FORGE_ADDON_ENABLED',
        false,
        'Enables Atlassian Forge task add-on integration.',
        'Keep disabled when the Forge add-on is not installed.',
        false,
        'system-seed',
        now()
    )
ON CONFLICT (name) DO UPDATE SET
    description = EXCLUDED.description,
    impact_note = EXCLUDED.impact_note,
    default_value = EXCLUDED.default_value;
