-- TODO: shared schema-specific tables

-- admin-dashboard-control-plane / Requirement 11
-- shared.audit_log: Lifecycle_Action audit trail (audit-or-rollback semantics).
-- The shared. schema is created by 00_schemas.sql (scaffold Requirement 6.1).
-- IF NOT EXISTS guards keep boot idempotent; existing pg_data volume is preserved.
CREATE TABLE IF NOT EXISTS shared.audit_log (
    id UUID PRIMARY KEY,
    actor TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    service_name TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('start','stop','restart','run_tests','health_streak_alert','service_start_blocked_feature_flag')),
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    correlation_id UUID NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('success','failed','pending')),
    details_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_audit_log_correlation
    ON shared.audit_log (correlation_id);

CREATE INDEX IF NOT EXISTS idx_audit_log_service_action_ts
    ON shared.audit_log (service_name, action, timestamp DESC);
