-- 005_audit_log_vault_purge_actions.sql
-- Vault purge audit actions for service stop.
--
-- Widens the ``shared.audit_log.action`` CHECK constraint to include
-- ``vault_overrides_purged`` (success path) and
-- ``vault_purge_partial_failure`` (best-effort fallback) so
-- :meth:`LifecycleService.stop` can record the post-stop Vault purge
-- outcome.
--
-- Builds on top of migration ``004_audit_log_purge_vault_action.sql``
-- which introduced the named constraint ``audit_log_action_check_v3``.
-- We drop ``audit_log_action_check_v3`` if present and add
-- ``audit_log_action_check_v4`` with the wider value set; the
-- inline-named original constraint (introduced by ``50_shared.sql``)
-- and earlier v2/v3 checks are also handled defensively in case an
-- intermediate migration has not yet been applied.
--
-- Idempotent — re-running on a DB that already has v4 in place is a
-- no-op. New CHECK names are added with the suffix ``_v4`` so future
-- migrations can chain off them without grovelling through
-- ``pg_constraint`` again.

DO $$
DECLARE
    old_constraint_name TEXT;
BEGIN
    -- Locate any existing CHECK constraint on shared.audit_log.action
    -- that does NOT yet allow ``vault_overrides_purged``. This catches
    -- any of v3 (audit_log_action_check_v3), v2, or the original
    -- auto-named constraint from 50_shared.sql.
    SELECT conname
      INTO old_constraint_name
      FROM pg_constraint
      JOIN pg_class ON pg_class.oid = pg_constraint.conrelid
      JOIN pg_namespace ON pg_namespace.oid = pg_class.relnamespace
     WHERE pg_namespace.nspname = 'shared'
       AND pg_class.relname = 'audit_log'
       AND pg_constraint.contype = 'c'
       AND pg_get_constraintdef(pg_constraint.oid) LIKE '%action%'
       AND pg_get_constraintdef(pg_constraint.oid) NOT LIKE '%vault_overrides_purged%'
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
           AND pg_constraint.conname = 'audit_log_action_check_v4'
    ) THEN
        ALTER TABLE shared.audit_log
            ADD CONSTRAINT audit_log_action_check_v4
            CHECK (action IN (
                'start',
                'stop',
                'restart',
                'run_tests',
                'health_streak_alert',
                'service_start_blocked_feature_flag',
                'purge_vault_blocked_in_production',
                'vault_overrides_purged',
                'vault_purge_partial_failure'
            ));
    END IF;
END $$;
