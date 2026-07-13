CREATE TABLE IF NOT EXISTS strategy_discovery_runs (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    requested_candidates INTEGER NOT NULL,
    discovery_version TEXT NOT NULL,
    safety_statement TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT strategy_discovery_runs_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS strategy_discovery_runs_symbol_timeframe_idx
    ON strategy_discovery_runs(symbol, timeframe, created_at DESC);

CREATE TABLE IF NOT EXISTS strategy_discovery_strategies (
    id BIGSERIAL PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    family_id TEXT NOT NULL,
    parent_candidate_id TEXT,
    discovery_run_id BIGINT REFERENCES strategy_discovery_runs(id) ON DELETE SET NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    generation INTEGER NOT NULL,
    blocks JSONB NOT NULL,
    parameters JSONB NOT NULL,
    complexity INTEGER NOT NULL,
    metrics JSONB NOT NULL,
    validation_metrics JSONB NOT NULL,
    walk_forward_metrics JSONB NOT NULL,
    out_of_sample_metrics JSONB NOT NULL,
    regime_analysis JSONB NOT NULL,
    feature_correlations JSONB NOT NULL,
    paper_readiness JSONB NOT NULL,
    research_score DOUBLE PRECISION NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    failure_reasons JSONB NOT NULL,
    explanation TEXT NOT NULL,
    discovery_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_at TIMESTAMPTZ,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT strategy_discovery_status_check CHECK (status IN ('generated', 'rejected', 'promoted', 'retired')),
    CONSTRAINT strategy_discovery_strategies_simulation_only_check CHECK (simulation_only = TRUE),
    CONSTRAINT strategy_discovery_unique_candidate_asset UNIQUE(candidate_id, symbol, timeframe)
);

CREATE INDEX IF NOT EXISTS strategy_discovery_strategies_status_score_idx
    ON strategy_discovery_strategies(status, research_score DESC, created_at DESC);

CREATE INDEX IF NOT EXISTS strategy_discovery_strategies_family_idx
    ON strategy_discovery_strategies(family_id, generation, created_at DESC);

CREATE TABLE IF NOT EXISTS strategy_discovery_events (
    id BIGSERIAL PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    parent_candidate_id TEXT,
    event_type TEXT NOT NULL,
    details JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT strategy_discovery_events_simulation_only_check CHECK (simulation_only = TRUE)
);

CREATE INDEX IF NOT EXISTS strategy_discovery_events_candidate_idx
    ON strategy_discovery_events(candidate_id, created_at DESC);
