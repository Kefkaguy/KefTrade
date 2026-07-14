CREATE TABLE IF NOT EXISTS research_universes (
    id BIGSERIAL PRIMARY KEY,
    universe_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    assets JSONB NOT NULL,
    default_timeframes JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_universes_assets_array_check CHECK (jsonb_typeof(assets) = 'array'),
    CONSTRAINT research_universes_timeframes_array_check CHECK (jsonb_typeof(default_timeframes) = 'array'),
    CONSTRAINT research_universes_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE TABLE IF NOT EXISTS research_campaigns (
    id BIGSERIAL PRIMARY KEY,
    campaign_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    universe_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    requested_candidates INTEGER NOT NULL,
    queued_jobs INTEGER NOT NULL DEFAULT 0,
    completed_jobs INTEGER NOT NULL DEFAULT 0,
    failed_jobs INTEGER NOT NULL DEFAULT 0,
    rejected_candidates INTEGER NOT NULL DEFAULT 0,
    promoted_candidates INTEGER NOT NULL DEFAULT 0,
    analytics JSONB NOT NULL DEFAULT '{}'::jsonb,
    controls JSONB NOT NULL DEFAULT '{}'::jsonb,
    safety_statement TEXT NOT NULL,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    canceled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_campaigns_status_check CHECK (status IN ('queued', 'running', 'paused', 'completed', 'canceled', 'failed')),
    CONSTRAINT research_campaigns_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_campaigns_status_idx
    ON research_campaigns(status, created_at DESC);

CREATE TABLE IF NOT EXISTS research_campaign_jobs (
    id BIGSERIAL PRIMARY KEY,
    campaign_id BIGINT NOT NULL REFERENCES research_campaigns(id) ON DELETE CASCADE,
    job_key TEXT NOT NULL UNIQUE,
    candidate_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    candidate JSONB NOT NULL,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    validation_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    consistency_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    failure_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    attempts INTEGER NOT NULL DEFAULT 0,
    latest_error TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT research_campaign_jobs_status_check CHECK (status IN ('queued', 'running', 'completed', 'rejected', 'promoted', 'failed', 'canceled')),
    CONSTRAINT research_campaign_jobs_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS research_campaign_jobs_campaign_status_idx
    ON research_campaign_jobs(campaign_id, status, id);

CREATE INDEX IF NOT EXISTS research_campaign_jobs_candidate_idx
    ON research_campaign_jobs(candidate_id, campaign_id);

CREATE TABLE IF NOT EXISTS elite_research_candidates (
    id BIGSERIAL PRIMARY KEY,
    campaign_id BIGINT REFERENCES research_campaigns(id) ON DELETE SET NULL,
    candidate_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    research_score DOUBLE PRECISION NOT NULL,
    profit_factor DOUBLE PRECISION NOT NULL DEFAULT 0,
    expectancy DOUBLE PRECISION NOT NULL DEFAULT 0,
    max_drawdown DOUBLE PRECISION NOT NULL DEFAULT 0,
    trade_count INTEGER NOT NULL DEFAULT 0,
    stability DOUBLE PRECISION NOT NULL DEFAULT 0,
    assets_passed INTEGER NOT NULL DEFAULT 0,
    timeframes_passed INTEGER NOT NULL DEFAULT 0,
    regimes_passed INTEGER NOT NULL DEFAULT 0,
    validation_history JSONB NOT NULL,
    paper_performance JSONB NOT NULL DEFAULT '{}'::jsonb,
    promoted_to_paper_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT elite_research_candidates_unique UNIQUE(candidate_id, campaign_id),
    CONSTRAINT elite_research_candidates_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS elite_research_candidates_score_idx
    ON elite_research_candidates(research_score DESC, created_at DESC);
