-- 019_ssh_runner_base_path.sql
-- Adds the remote workspace root for each SSH runner.

ALTER TABLE infrastructure.ssh_runners
    ADD COLUMN IF NOT EXISTS base_path TEXT NOT NULL DEFAULT '/var/ai-runner';

ALTER TABLE infrastructure.ssh_runners
    DROP CONSTRAINT IF EXISTS ssh_runners_base_path_abs;

ALTER TABLE infrastructure.ssh_runners
    ADD CONSTRAINT ssh_runners_base_path_abs
    CHECK (base_path LIKE '/%' AND base_path NOT LIKE '%/../%' AND base_path NOT LIKE '%/..');
