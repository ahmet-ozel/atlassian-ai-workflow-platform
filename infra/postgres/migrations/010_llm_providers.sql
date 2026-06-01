-- 010_llm_providers.sql
-- Spec: llm-provider-management — Task 1.1 (Requirements 1.8, 2.6, 2.7, 3.2, 9.1, 10.1)
--
-- Adds:
--   automation.llm_providers — operator-managed LLM provider configurations.
--   automation.dept_llm_provider_overrides — per-department provider pinning.
--
-- Credential material lives EXCLUSIVELY in Vault KV-v2 at
--   secret/data/llm-providers/{provider_id}/credentials
-- The Postgres table carries no api_key / token / secret / credential columns
-- (Requirement 3.2; design "Data Models › Postgres").
--
-- Idempotent — uses IF NOT EXISTS guards so re-running on an already-migrated
-- database is a no-op.

-- pgcrypto provides gen_random_uuid() — already created by 10_automation.sql
-- but re-declared here for standalone-migration self-sufficiency.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Ensure the automation schema exists (also declared in 00_schemas.sql).
CREATE SCHEMA IF NOT EXISTS automation;

-- =============================================================================
-- automation.llm_providers
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.llm_providers (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_type    TEXT         NOT NULL,
    name             TEXT         NOT NULL,
    model            TEXT         NOT NULL,
    context_length   INTEGER      NOT NULL,
    base_url         TEXT,                         -- vLLM only; NULL for SaaS providers
    vault_path       TEXT         NOT NULL,        -- canonical Vault path for credential material
    status           TEXT         NOT NULL DEFAULT 'active',
    last_tested_at   TIMESTAMPTZ,
    last_test_error  TEXT,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT llm_providers_name_unique UNIQUE (name),
    CONSTRAINT llm_providers_provider_type_check CHECK (
        provider_type IN ('vllm', 'openai', 'anthropic', 'gemini')
    ),
    CONSTRAINT llm_providers_status_check CHECK (
        status IN ('active', 'inactive')
    ),
    CONSTRAINT llm_providers_context_length_check CHECK (context_length > 0)
);

CREATE INDEX IF NOT EXISTS idx_llm_providers_status
    ON automation.llm_providers (status);

CREATE INDEX IF NOT EXISTS idx_llm_providers_created_at
    ON automation.llm_providers (created_at DESC);

-- =============================================================================
-- automation.dept_llm_provider_overrides
-- =============================================================================
-- Pins a single LLM provider to a department. ON DELETE RESTRICT on the
-- provider FK means a referenced provider cannot be deleted while any
-- override row points at it (Requirement 1.7 — provider_in_use surface).
CREATE TABLE IF NOT EXISTS automation.dept_llm_provider_overrides (
    dept_id      TEXT         PRIMARY KEY
        REFERENCES automation.departments(id) ON DELETE CASCADE,
    provider_id  UUID         NOT NULL
        REFERENCES automation.llm_providers(id) ON DELETE RESTRICT,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dept_llm_provider_overrides_provider
    ON automation.dept_llm_provider_overrides (provider_id);
