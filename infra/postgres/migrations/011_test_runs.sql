-- 011_test_runs.sql
-- Spec: gereksinim.txt G9 / Requirement 7.x — E4 iyileştirmesi.
--
-- Adds:
--   automation.test_runs — persistent service test-run history.
--
-- The admin dashboard's "Servis testleri" panel runs a service's test
-- command via test_runner.py. Before this migration the run results
-- lived only in an in-memory dict (test_results.py `_test_history`) and
-- were lost on every admin-dashboard-api restart. This table gives the
-- panel a durable pass/fail trend.
--
-- Idempotent — IF NOT EXISTS guards make re-runs a no-op.

CREATE SCHEMA IF NOT EXISTS automation;

-- =============================================================================
-- automation.test_runs
-- =============================================================================
CREATE TABLE IF NOT EXISTS automation.test_runs (
    id            BIGSERIAL    PRIMARY KEY,
    service_name  TEXT         NOT NULL,
    exit_code     INTEGER      NOT NULL,
    status        TEXT         NOT NULL,            -- 'pass' | 'fail'
    total_tests   INTEGER      NOT NULL DEFAULT 0,
    passed        INTEGER      NOT NULL DEFAULT 0,
    failed        INTEGER      NOT NULL DEFAULT 0,
    duration_ms   INTEGER,
    output_tail   TEXT,                            -- last ~4 KB of stdout
    triggered_by  TEXT         NOT NULL DEFAULT 'system',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Most queries are "latest N runs for service X, newest first".
CREATE INDEX IF NOT EXISTS idx_test_runs_service_created
    ON automation.test_runs (service_name, created_at DESC);
