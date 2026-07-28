-- 058_signal_diagnostics_jobs.sql
-- Background job queue for signal diagnostics.
--
-- Why this exists: the signal-diagnostics endpoint was doing its entire
-- measurement synchronously inside the HTTP request -- reading candles for
-- every (family, variant, symbol) combination and running decide() over every
-- bar -- while holding the request's database transaction open the whole
-- time. On a single-worker API process that starves the async event loop of
-- GIL time long enough for nginx to return 502s on completely unrelated
-- endpoints, and the DB connection sits in a transaction for 100+ seconds.
--
-- This queue lets the API just enqueue a row and return immediately. A
-- separate worker process (app/workers/signal_diagnostics_runner.py, the same
-- shape as campaign_runner.py) claims queued jobs one at a time using its own
-- short-lived connections and writes results to research_signal_diagnostics
-- (migration 057) as it goes.
--
-- Additive and idempotent: the migrate job re-applies every file on every
-- deploy. Nothing here alters an existing table, changes any gate, or
-- promotes anything.

CREATE TABLE IF NOT EXISTS research_signal_diagnostics_jobs (
    id BIGSERIAL PRIMARY KEY,
    timeframe TEXT NOT NULL,
    dataset_id BIGINT,
    architectures JSONB,
    max_variants INTEGER NOT NULL DEFAULT 3,
    max_symbols INTEGER NOT NULL DEFAULT 4,
    status TEXT NOT NULL DEFAULT 'queued',
    result JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT research_signal_diagnostics_jobs_status_check
        CHECK (status IN ('queued', 'running', 'completed', 'failed'))
);

CREATE INDEX IF NOT EXISTS research_signal_diagnostics_jobs_status_idx
    ON research_signal_diagnostics_jobs(status, id);
