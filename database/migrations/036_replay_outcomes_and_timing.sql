-- Outcome evidence for historical shadow replay. Append-only and simulation-only.

ALTER TABLE elite_shadow_replay_runs
    ADD COLUMN IF NOT EXISTS outcome_summary JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS elite_shadow_replay_outcomes (
    id BIGSERIAL PRIMARY KEY,
    replay_run_id BIGINT NOT NULL REFERENCES elite_shadow_replay_runs(id) ON DELETE RESTRICT,
    replay_decision_id BIGINT NOT NULL UNIQUE REFERENCES elite_shadow_replay_decisions(id) ON DELETE RESTRICT,
    external_deployment_id BIGINT NOT NULL REFERENCES external_paper_deployments(id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL,
    entry_time TIMESTAMPTZ,
    exit_time TIMESTAMPTZ,
    entry_price NUMERIC,
    exit_price NUMERIC,
    quantity INTEGER NOT NULL DEFAULT 0,
    stop_price NUMERIC,
    target_price NUMERIC,
    gross_pnl NUMERIC NOT NULL DEFAULT 0,
    fees NUMERIC NOT NULL DEFAULT 0,
    net_pnl NUMERIC NOT NULL DEFAULT 0,
    net_return_on_allocated_capital NUMERIC NOT NULL DEFAULT 0,
    exit_reason TEXT,
    holding_bars INTEGER NOT NULL DEFAULT 0,
    holding_hours DOUBLE PRECISION NOT NULL DEFAULT 0,
    regime TEXT NOT NULL DEFAULT 'unknown',
    assumptions JSONB NOT NULL DEFAULT '{}'::jsonb,
    broker_mutation BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT elite_shadow_replay_outcome_status_check CHECK (status IN ('completed','unresolved','no_next_bar','invalid_geometry','skipped_overlap')),
    CONSTRAINT elite_shadow_replay_outcome_quantity_check CHECK (quantity >= 0),
    CONSTRAINT elite_shadow_replay_outcome_no_broker_mutation CHECK (broker_mutation = FALSE)
);

CREATE INDEX IF NOT EXISTS elite_shadow_replay_outcomes_run_idx
    ON elite_shadow_replay_outcomes(replay_run_id, external_deployment_id, entry_time);
CREATE INDEX IF NOT EXISTS elite_shadow_replay_outcomes_regime_idx
    ON elite_shadow_replay_outcomes(replay_run_id, regime, status);

