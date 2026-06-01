-- 004_audit_log_purge_vault_action.sql
-- Spec: platform-mimari-uyumluluk — Task 15.1 (R14 / Q16 — stop + purge_vault profile guard)
--
-- Widens the ``shared.audit_log.action`` CHECK constraint to include
-- ``purge_vault_blocked_in_production`` so the lifecycle stop endpoint
-- can record an attempt to pass ``purge_vault=true`` while the runtime
-- ``DEPLOYMENT_PROFILE`` resolves to ``"production"`` (Requirement
-- 14.2).
--
-- Builds on top of migration ``003_audit_log_feature_flag_action.sql``
-- which introduced the named constraint ``audit_log_action_check_v2``.
-- We drop ``audit_log_action_check_v2`` if present and add
-- ``audit_log_action_check_v3`` with the wider value set; the
-- inline-named original constraint (introduced by ``50_shared.sql``)
-- is also handled in case migration 003 has not yet been applied.
--
-- Idempotent — re-running on a DB that already has v3 in place is a
-- no-op. New CHECK names are added with the suffix ``_v3`` so future
-- migrations can chain off them without grovelling through
-- ``pg_constraint`` again.

DO $$
DECLARE
    old_constraint_name TEXT;
BEGIN
    -- Locate any existing CHECK constraint on shared.audit_log.action
    -- that does NOT yet allow ``purge_vault_blocked_in_production``.
    -- This catches both the v2 (audit_log_action_check_v2) constraint
    -- introduced by migration 003 and the original auto-named
    -- constraint from 50_shared.sql when migration 003 has not run.
    SELECT conname
      INTO old_constraint_name
      FROM pg_constraint
      JOIN pg_class ON pg_class.oid = pg_constraint.conrelid
      JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
     WHERE pg_namespace.nspname = 'shared'
       AND pg_class.relname = 'audit_log'
       AND pg_constraint.contype = 'c'
       AND pg_get_constraintdef(pg_constraint.oid) LIKE '%action%'
       AND pg_get_constraintdef(pg_constraint.oid) NOT LIKE '%purge_vault_blocked_in_production%'
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
           AND pg_constraint.conname = 'audit_log_action_check_v3'
    ) THEN
        ALTER TABLE shared.audit_log
            ADD CONSTRAINT audit_log_action_check_v3
            CHECK (action IN (
                'start',
                'stop',
                'restart',
                'run_tests',
                'health_streak_alert',
                'service_start_blocked_feature_flag',
                'purge_vault_blocked_in_production'
            ));
    END IF;
END $$;
