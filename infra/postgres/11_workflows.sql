-- 11_workflows.sql
-- Workflow schema additions.
-- Workflow-scope additions on top of foundation 10_automation.sql.
-- Idempotent — safe to run multiple times without side effects.
--
-- Boot order (alphabetic under /docker-entrypoint-initdb.d):
--   00_schemas.sql    → creates `automation`, `assistant`, `shared`,
--                       `temporal` schemas.
--   10_automation.sql → foundation: `automation.departments`,
--                       `automation.audit_events`,
--                       `automation.probe_artifacts`, …
--   11_workflows.sql  → THIS FILE: cross-dept idempotency / cache
--                       tables consumed by the webhook filter chain
--                       and the AgentRunnerWorkflow iteration loop.
--   20_ops.sql        → ops scope (cost / budget / notification).
--
-- Schema choice: tables live in the `automation` schema, matching
-- design.md prose (`automation.diff_summary_cache`, ...). They are
-- *cross-dept idempotency keys* — the workflow_id / page_id / pr_id
-- column already encodes the dept boundary — and therefore do NOT
-- carry a `dept_id` column and are NOT subject to RLS.
--
-- Defines workflow idempotency, deduplication, and cache tables used by
-- webhook and review flows.
-- Design ref: design.md "Postgres şeması — yeni / değişen tablolar"


-- ===========================================================================
-- 1. automation.processed_events — webhook delivery_id replay-dedup
-- ===========================================================================
-- Replay dedup keeps duplicate deliveries from starting extra workflows.
-- If signalWithStart returns 503, the row is rolled back so retry can
-- re-claim it; repeated deliveries still converge to one Temporal execution.
--
-- Schema migration vs foundation 10_automation.sql:
--   The initial table used `automation.processed_events` keyed
--   on `event_hash` (sha256 of canonical payload) with a 7-day TTL.
--   The workflow migration replaces that scheme with `delivery_id` (the
--   provider-assigned webhook delivery id) — this is the column the
--   webhook filter chain and `WebhookFilterChain.evaluate()` rely on
--   per design.md (`delivery_id` → `processed_events.delivery_id` PK
--   with one-to-one mapping). The old shape is incompatible with the
--   new PK, so this block drops the legacy table (and its TTL index)
--   before recreating it with the new contract.
--
--   `DROP TABLE IF EXISTS` is acceptable here because the legacy
--   table is a transient idempotency cache (no audit-grade history,
--   no FKs from other tables, 7-day TTL by design). On a fresh
--   container boot the foundation table never gets created (alphabetic
--   ordering runs 10_automation.sql first, then this file); on an
--   upgrade boot the worst case is that a window of in-flight webhook
--   deliveries replay once before the new table accepts them, which
--   the workflow_id-based `signalWithStart` idempotency absorbs
--   without side effects.
DROP INDEX IF EXISTS automation.idx_processed_events_expires_at;
DROP TABLE IF EXISTS automation.processed_events;

CREATE TABLE IF NOT EXISTS automation.processed_events (
    delivery_id  TEXT PRIMARY KEY,
    provider     TEXT NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_processed_events_provider
        CHECK (provider IN ('jira', 'bitbucket'))
);

-- Hot-path index for ad-hoc operational queries ("recent webhook
-- deliveries") and for the future TTL pruner if/when the workflows
-- one is added. Kept narrow (received_at only) so the PK index
-- carries the dedup lookup load without contention.
CREATE INDEX IF NOT EXISTS idx_processed_events_received_at
    ON automation.processed_events (received_at DESC);


-- ===========================================================================
-- 2. automation.confluence_section_hashes — section-level dedup (V10)
-- ===========================================================================
-- Confluence section hash dedup stores tuples that make
-- `should_skip_section_update(workflow_id, page_id, section_path, content_hash)`
-- return True when the same section content was already written; skipped writes
-- emit `confluence_section_dedup_skip`.
--
-- The composite PK encodes the design contract verbatim:
--   "aynı `(workflow_id, page_id, section_path, content_hash)`
--    kombinasyonu varsa update'i skip eder"
-- so `INSERT ... ON CONFLICT DO NOTHING` is the natural idempotent
-- write path from `ConfluenceSectionHashRepo`.
--
-- No `dept_id` and no RLS: `workflow_id` already encodes the dept
-- boundary (workflow_id format is `automation-jira-{PROJECT_KEY}-…`
-- per identifiers.py). Cross-dept reads of this table are an
-- acceptable invariant — the contents are content hashes, not
-- payloads.
CREATE TABLE IF NOT EXISTS automation.confluence_section_hashes (
    workflow_id   TEXT NOT NULL,
    page_id       TEXT NOT NULL,
    section_path  TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    written_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (workflow_id, page_id, section_path, content_hash)
);

-- Lookup index for the "all sections written for this page" query
-- used by the Confluence overwrite-protection check.
CREATE INDEX IF NOT EXISTS idx_confluence_section_hashes_page
    ON automation.confluence_section_hashes (page_id, written_at DESC);


-- ===========================================================================
-- 3. automation.diff_summary_cache - LLM diff summary cache
-- ===========================================================================
-- Caches LLM diff summaries so `compute_diff_summary(diff_hash, cache, llm)`
-- avoids another LLM call on cache hits. Commit-only review comments can reuse
-- the same summary for identical diff hashes.
--
-- `diff_hash` is sha256 of the unified diff body; once an LLM
-- summary is computed, every subsequent request for the same hash
-- (Orphan Branches widget refresh, repeated commit-only iters, …)
-- is served from this cache. Effective cost gate for V7.
CREATE TABLE IF NOT EXISTS automation.diff_summary_cache (
    diff_hash     TEXT PRIMARY KEY,
    summary       TEXT NOT NULL,
    generated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ===========================================================================
-- 4. automation.pr_supersede_log — multi-iter PR supersede ledger (Y9)
-- ===========================================================================
-- Records each iter-N supersede transition by tagging the previous PR with
-- `superseded-by-pr-{new_id}` and inserting one ledger row. The primary key
-- keeps `iter_advance` idempotent on retries.
--
-- PK = (workflow_id, old_pr_id) so that:
--   • re-running `iter_advance(state, new_pr_id)` for the same
--     (workflow_id, old_pr_id) is a no-op insert (ON CONFLICT
--     DO NOTHING),
--   • the log records every supersede transition exactly once,
--     suitable for the PO Review Inbox audit trail.
CREATE TABLE IF NOT EXISTS automation.pr_supersede_log (
    workflow_id    TEXT NOT NULL,
    old_pr_id      BIGINT NOT NULL,
    new_pr_id      BIGINT NOT NULL,
    superseded_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (workflow_id, old_pr_id)
);

-- Lookup index for "what was superseded by PR #X" queries from the
-- PO Review Inbox endpoint.
CREATE INDEX IF NOT EXISTS idx_pr_supersede_log_new_pr
    ON automation.pr_supersede_log (new_pr_id);
