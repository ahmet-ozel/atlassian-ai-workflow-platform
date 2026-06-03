-- 012_bot_identity_unique.sql
-- Bot account ID uniqueness enforcement.
--
-- Adds the database-layer leg of bot identity uniqueness:
--   * UNIQUE(service, account_id) on the department-bot identity table
--   * Admin Dashboard API CRUD-time conflict checks
--   * Boot-time uniqueness validation when ``departments.json`` is loaded
--
-- The actual table that holds bot identities is
-- ``automation.department_bots`` (created by
-- ``infra/postgres/10_automation.sql``). Earlier notes referenced a
-- ``shared.department_bot_identity`` table, but no such object exists
-- in this codebase. Every reader (loop guard, webhook dispatcher,
-- credential resolver, capability prober, bootstrap probe) keys off
-- ``automation.department_bots`` already, so that is the table this
-- constraint binds to.
--
-- Two design choices worth flagging:
--
--   1. Partial UNIQUE INDEX (instead of plain CONSTRAINT) so the
--      bundled ``departments.json`` can keep shipping placeholder rows
--      with ``account_id = ""`` / ``account_id IS NULL`` for departments
--      whose Vault probe has not run yet. Only *non-empty* account_ids are
--      routing keys, so only those need to be globally unique.
--      Partial indexes also match how the admin dashboard CRUD layer
--      thinks about conflicts (see _extract_bot_identities) so the
--      DB and API layers agree on the same notion of "real" id.
--   2. The migration is idempotent: ``CREATE UNIQUE INDEX IF NOT
--      EXISTS`` lets it re-run on an already-migrated database
--      without raising ``42P07``. The DO-block guard runs *before*
--      the index creation so a fresh apply against a database that
--      already has duplicates fails with an actionable error rather
--      than the bare ``23505`` Postgres surfaces from the index
--      build.
--
-- Idempotent — safe to re-run.

-- =============================================================================
-- 1. Pre-flight: refuse to apply when the table already has duplicates
-- =============================================================================
-- A duplicate ``(service, account_id)`` pair means two departments
-- claim the same bot identity, which produces an ambiguous routing
-- decision the next time that bot acts. We surface this as a clear
-- migration error (with the offending pairs listed) so an operator
-- can resolve the data conflict before the constraint is added,
-- rather than the build failing with the cryptic
-- ``could not create unique index ... Key (service, account_id) is
-- duplicated`` message.
DO $$
DECLARE
    duplicate_pairs TEXT;
    duplicate_count INTEGER;
BEGIN
    SELECT
        STRING_AGG(
            FORMAT(
                'service=%L account_id=%L count=%s',
                service,
                account_id,
                row_count
            ),
            E'\n  - '
        ),
        COUNT(*)
    INTO duplicate_pairs, duplicate_count
    FROM (
        SELECT
            service,
            account_id,
            COUNT(*) AS row_count
        FROM automation.department_bots
        WHERE account_id IS NOT NULL
          AND account_id <> ''
        GROUP BY service, account_id
        HAVING COUNT(*) > 1
    ) AS dups;

    IF duplicate_count > 0 THEN
        RAISE EXCEPTION
            E'cannot apply migration 012_bot_identity_unique: '
            '% duplicate (service, account_id) pair(s) already exist '
            'in automation.department_bots. Resolve these conflicts '
            'before retrying:\n  - %',
            duplicate_count, duplicate_pairs
            USING HINT =
                'Each (service, account_id) pair must map to exactly '
                'one department row. Identify which department legitimately '
                'owns the bot identity and either delete or re-key the '
                'losing row(s) before re-running this migration.';
    END IF;
END
$$;

-- =============================================================================
-- 2. UNIQUE INDEX — (service, account_id) for non-empty account_ids
-- =============================================================================
-- Partial index: empty / NULL account_id rows are placeholders for
-- departments whose Vault probe has not yet populated the column.
-- They are *not* routing keys and must not collide with each other.
CREATE UNIQUE INDEX IF NOT EXISTS uq_department_bots_service_account_id
    ON automation.department_bots (service, account_id)
    WHERE account_id IS NOT NULL
      AND account_id <> '';

COMMENT ON INDEX automation.uq_department_bots_service_account_id IS
    'globally unique (service, account_id) for non-empty bot '
    'account_ids; empty/NULL placeholders are excluded.';
