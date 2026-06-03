-- 008_capability_probes.sql
-- Capability probe schema migration.
--
-- Adds the table required by the capability probe matrix:
--   1. shared.capability_probes — Per-department/per-service probe result cache
--
-- Only the most recent probe result per (dept_id, service) pair is stored,
-- updated via UPSERT (ON CONFLICT (dept_id, service) DO UPDATE). The table
-- backs the GET /api/v1/departments/capabilities matrix endpoint and the
-- POST /api/v1/departments/{dept_id}/probe/{service} single-probe endpoint.
--
-- Idempotent — uses IF NOT EXISTS guards so re-running on an already-migrated
-- database is a no-op.
--

-- =============================================================================
-- 1. capability_probes — Capability probe results cache
-- =============================================================================
-- Status values:
--   'ok'              — Probe succeeded (service reachable + auth valid)
--   'error'           — Probe failed (network, auth, or service error)
--   'not_configured'  — Service not configured for this department
--
-- Service values (non-exhaustive, application-defined):
--   'jira', 'bitbucket', 'confluence', 'llm', 'ssh', 'docker'
CREATE TABLE IF NOT EXISTS shared.capability_probes (
    dept_id     TEXT         NOT NULL,
    service     TEXT         NOT NULL,
    status      TEXT         NOT NULL,
    error       TEXT,
    latency_ms  INTEGER,
    probed_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (dept_id, service)
);

-- Time-based pruning index — supports cleanup of stale probe results
CREATE INDEX IF NOT EXISTS idx_capability_probes_probed_at
    ON shared.capability_probes (probed_at);
