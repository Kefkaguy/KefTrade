-- Historical elite replay evidence. Replay rows are deliberately separated from
-- forward strategy_evaluations and shadow_executions so research cannot be
-- mistaken for a naturally observed broker decision.

CREATE TABLE IF NOT EXISTS elite_shadow_replay_runs (
    id BIGSERIAL PRIMARY KEY,
    replay_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    requested_external_deployment_id BIGINT REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    requested_candle_limit INTEGER NOT NULL,
    configuration JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    broker_mutation BOOLEAN NOT NULL DEFAULT FALSE,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT elite_shadow_replay_runs_status_check CHECK (status IN ('running', 'complete', 'failed')),
    CONSTRAINT elite_shadow_replay_runs_limit_check CHECK (requested_candle_limit > 0),
    CONSTRAINT elite_shadow_replay_runs_no_broker_mutation CHECK (broker_mutation = FALSE)
);

CREATE INDEX IF NOT EXISTS elite_shadow_replay_runs_started_idx
    ON elite_shadow_replay_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS elite_shadow_replay_decisions (
    id BIGSERIAL PRIMARY KEY,
    replay_run_id BIGINT NOT NULL REFERENCES elite_shadow_replay_runs(id) ON DELETE RESTRICT,
    internal_deployment_id BIGINT NOT NULL REFERENCES strategy_deployments(id) ON DELETE RESTRICT,
    external_deployment_id BIGINT NOT NULL REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    elite_candidate_id BIGINT NOT NULL REFERENCES elite_research_candidates(id) ON DELETE RESTRICT,
    candidate_id TEXT NOT NULL,
    configuration_fingerprint TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    completed_bar_timestamp TIMESTAMPTZ NOT NULL,
    signal_type TEXT NOT NULL,
    gates JSONB NOT NULL DEFAULT '[]'::jsonb,
    regime JSONB NOT NULL DEFAULT '{}'::jsonb,
    stop_price NUMERIC,
    target_price NUMERIC,
    reference_price NUMERIC NOT NULL,
    simulated_quantity INTEGER NOT NULL DEFAULT 0,
    simulated_expected_risk NUMERIC NOT NULL DEFAULT 0,
    simulated_risk_pct NUMERIC NOT NULL DEFAULT 0,
    model_bound_applied BOOLEAN NOT NULL DEFAULT TRUE,
    portfolio_bound_applied BOOLEAN NOT NULL DEFAULT TRUE,
    would_submit BOOLEAN NOT NULL DEFAULT FALSE,
    rejection_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    decision JSONB NOT NULL DEFAULT '{}'::jsonb,
    broker_mutation BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(replay_run_id, internal_deployment_id, completed_bar_timestamp),
    CONSTRAINT elite_shadow_replay_signal_check CHECK (signal_type IN ('setup', 'watchlist', 'avoid', 'skipped')),
    CONSTRAINT elite_shadow_replay_quantity_check CHECK (simulated_quantity >= 0),
    CONSTRAINT elite_shadow_replay_risk_check CHECK (simulated_expected_risk >= 0 AND simulated_risk_pct >= 0),
    CONSTRAINT elite_shadow_replay_no_broker_mutation CHECK (broker_mutation = FALSE)
);

CREATE INDEX IF NOT EXISTS elite_shadow_replay_decisions_run_idx
    ON elite_shadow_replay_decisions(replay_run_id, external_deployment_id, completed_bar_timestamp);
CREATE INDEX IF NOT EXISTS elite_shadow_replay_decisions_setup_idx
    ON elite_shadow_replay_decisions(signal_type, would_submit, completed_bar_timestamp DESC);

