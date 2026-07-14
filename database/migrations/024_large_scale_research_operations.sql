ALTER TABLE research_campaign_jobs
    ADD COLUMN IF NOT EXISTS batch_id BIGINT,
    ADD COLUMN IF NOT EXISTS strategy_family TEXT,
    ADD COLUMN IF NOT EXISTS provider_latency_ms INTEGER,
    ADD COLUMN IF NOT EXISTS database_latency_ms INTEGER;

CREATE TABLE IF NOT EXISTS research_campaign_workers (
    worker_id TEXT PRIMARY KEY,
    process_id TEXT,
    hostname TEXT,
    status TEXT NOT NULL DEFAULT 'starting',
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    stopped_at TIMESTAMPTZ,
    latest_cycle_at TIMESTAMPTZ,
    latest_error TEXT,
    processed_jobs INTEGER NOT NULL DEFAULT 0,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_campaign_workers_status_check CHECK (status IN ('starting', 'running', 'idle', 'stopping', 'stopped', 'stale', 'error')),
    CONSTRAINT research_campaign_workers_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_campaign_workers_status_idx
    ON research_campaign_workers(status, heartbeat_at DESC);

CREATE TABLE IF NOT EXISTS research_campaign_batches (
    id BIGSERIAL PRIMARY KEY,
    campaign_id BIGINT NOT NULL REFERENCES research_campaigns(id) ON DELETE CASCADE,
    batch_key TEXT NOT NULL UNIQUE,
    batch_number INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    job_count INTEGER NOT NULL DEFAULT 0,
    completed_jobs INTEGER NOT NULL DEFAULT 0,
    failed_jobs INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_campaign_batches_status_check CHECK (status IN ('queued', 'running', 'completed', 'failed', 'canceled')),
    CONSTRAINT research_campaign_batches_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_campaign_batches_campaign_idx
    ON research_campaign_batches(campaign_id, batch_number);

ALTER TABLE research_campaign_jobs
    ADD CONSTRAINT research_campaign_jobs_batch_fk
    FOREIGN KEY (batch_id) REFERENCES research_campaign_batches(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS research_campaign_analytics_snapshots (
    id BIGSERIAL PRIMARY KEY,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE CASCADE,
    snapshot_key TEXT NOT NULL UNIQUE,
    analytics JSONB NOT NULL,
    strategy_family_intelligence JSONB NOT NULL DEFAULT '[]'::jsonb,
    asset_intelligence JSONB NOT NULL DEFAULT '[]'::jsonb,
    timeframe_intelligence JSONB NOT NULL DEFAULT '[]'::jsonb,
    heatmaps JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_campaign_analytics_snapshots_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_campaign_analytics_snapshots_campaign_idx
    ON research_campaign_analytics_snapshots(campaign_id, created_at DESC);

CREATE TABLE IF NOT EXISTS research_campaign_reports (
    id BIGSERIAL PRIMARY KEY,
    campaign_id BIGINT NOT NULL REFERENCES research_campaigns(id) ON DELETE CASCADE,
    report_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    summary JSONB NOT NULL,
    recommendations JSONB NOT NULL DEFAULT '[]'::jsonb,
    markdown_report TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_campaign_reports_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_campaign_reports_campaign_idx
    ON research_campaign_reports(campaign_id, created_at DESC);
