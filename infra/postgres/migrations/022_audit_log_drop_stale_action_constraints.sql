-- 022_audit_log_drop_stale_action_constraints.sql
-- Drop older shared.audit_log action CHECK constraints that can coexist
-- with v5 and still reject external provider probe audit rows.

DO $$
DECLARE
    stale_constraint RECORD;
BEGIN
    FOR stale_constraint IN
        SELECT conname
          FROM pg_constraint
          JOIN pg_class ON pg_class.oid = pg_constraint.conrelid
          JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
         WHERE pg_namespace.nspname = 'shared'
           AND pg_class.relname = 'audit_log'
           AND pg_constraint.contype = 'c'
           AND pg_constraint.conname LIKE 'audit_log_action_check%'
           AND pg_get_constraintdef(pg_constraint.oid)
               NOT LIKE '%external_provider_probe_failed%'
    LOOP
        EXECUTE format(
            'ALTER TABLE shared.audit_log DROP CONSTRAINT %I',
            stale_constraint.conname
        );
    END LOOP;

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
