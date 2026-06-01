-- 007_audit_log_external_provider_actions.sql
-- Spec: platform-real-usage-gaps — Task 10.4 (R10 — External Provider Downtime Widget audit)
--
-- Widens the ``shared.audit_log.action`` CHECK constraint to include
-- ``external_provider_probe_failed`` (emitted on every failed probe)
-- and ``external_provider_streak_alert`` (emitted once when a provider
-- accumulates 3 consecutive failures, mirroring the existing
-- ``health_streak_alert`` pattern from Requirement 12.5).
--
-- Builds on top of migration ``005_audit_log_vault_purge_actions.sql``
-- which introduced ``audit_log_action_check_v4``. We drop v4 if
-- present and add ``audit_log_action_check_v5`` with the wider value
-- set.
--
-- Idempotent — re-running on a DB that already has v5 in place is a
-- no-op.

DO $$
DECLARE
    old_constraint_name TEXT;
BEGIN
    -- Locate any existing CHECK constraint on shared.audit_log.action
    -- that does NOT yet allow ``external_provider_probe_failed``.
    SELECT conname
      INTO old_constraint_name
      FROM pg_constraint
      JOIN pg_class ON pg_class.oid = pg_constraint.conrelid
      JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
     WHERE pg_namespace.nspname = 'shared'
       AND pg_class.relname = 'audit_log'
       AND pg_constraint.contype = 'c'
       AND pg_get_constraintdef(pg_constraint.oid) LIKE '%action%'
       AND pg_get_constraintdef(pg_constraint.oid) NOT LIKE '%external_provider_probe_failed%'
     LIMIT 1;

    IF old_constraint_name IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE shared.audit_log DROP CONSTRAINT %I',
            old_constraint_name
        );
    END IF;
END $$;

-- Add the widened constraint with a stable name so subsequent
-- migrations can locate it deterministically.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
          JOIN pg_class ON pg_class.oid = pg_constraint.conrelid
          JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
         WHERE pg_namespace.nspname = 'shared'
           AND pg_class.relname = 'audit_log'
           AND pg_constraint.conname = 'audit_log_action_check_v5'
    ) THEN
        ALTER TABLE shared.audit_log
            ADD CONSTRAINT audit_log_action_check_v5
            CHECK (action IN (
                'start',
                'stop',
                'restart',
                'run_tests',
                'health_streak_alert',
                'service_start_blocked_feature_flag',
                'purge_vault_blocked_in_production',
                'vault_overrides_purged',
                'vault_purge_partial_failure',
                'external_provider_probe_failed',
                'external_provider_streak_alert'
            ));
    END IF;
END $$;
