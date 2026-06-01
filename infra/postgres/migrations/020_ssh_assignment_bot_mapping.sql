-- 020_ssh_assignment_bot_mapping.sql
-- Adds optional bot identity metadata to department SSH assignments.
--
-- Existing deployments may already have applied 018_ssh_runner_pool.sql, so
-- this must live in a new migration instead of relying on edits to 018.

ALTER TABLE infrastructure.dept_ssh_assignments
    ADD COLUMN IF NOT EXISTS bot_service TEXT NULL;

ALTER TABLE infrastructure.dept_ssh_assignments
    ADD COLUMN IF NOT EXISTS bot_account_id TEXT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'chk_dept_ssh_assignments_bot_service'
    ) THEN
        ALTER TABLE infrastructure.dept_ssh_assignments
            ADD CONSTRAINT chk_dept_ssh_assignments_bot_service
            CHECK (
                bot_service IS NULL
                OR bot_service IN ('jira', 'bitbucket', 'confluence')
            );
    END IF;
END $$;
