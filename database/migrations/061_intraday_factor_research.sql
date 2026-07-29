-- Chronologically split, backend-only intraday factor research.
-- Confirmation is a separate immutable run linked to a frozen discovery run.

CREATE TABLE IF NOT EXISTS intraday_factor_diagnostic_runs (
    id BIGSERIAL PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    dataset_id BIGINT NOT NULL REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    source_run_id BIGINT REFERENCES intraday_factor_diagnostic_runs(id) ON DELETE RESTRICT,
    timeframe TEXT NOT NULL,
    factor_keys JSONB NOT NULL,
    symbols JSONB NOT NULL,
    split_boundaries JSONB NOT NULL,
    cost_model JSONB NOT NULL,
    results JSONB NOT NULL DEFAULT '{}'::jsonb,
    frozen_spec_hash TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT intraday_factor_mode_check CHECK (mode IN ('discovery', 'confirmation')),
    CONSTRAINT intraday_factor_status_check CHECK (status IN ('running', 'completed', 'failed')),
    CONSTRAINT intraday_factor_timeframe_check CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_factor_source_check CHECK (
        (mode = 'discovery' AND source_run_id IS NULL)
        OR (mode = 'confirmation' AND source_run_id IS NOT NULL)
    ),
    CONSTRAINT intraday_factor_hash_check CHECK (length(frozen_spec_hash) = 64)
);

CREATE INDEX IF NOT EXISTS intraday_factor_diagnostic_runs_lookup_idx
    ON intraday_factor_diagnostic_runs(dataset_id, timeframe, created_at DESC);

CREATE TABLE IF NOT EXISTS intraday_microstructure_features (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    quote_count INTEGER NOT NULL,
    median_spread_bps NUMERIC,
    p90_spread_bps NUMERIC,
    mean_depth NUMERIC,
    order_flow_imbalance NUMERIC,
    normalized_order_flow_imbalance NUMERIC,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(symbol, timeframe, timestamp, provider, feed),
    CONSTRAINT intraday_microstructure_timeframe_check CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_microstructure_count_check CHECK (quote_count > 0)
);

CREATE INDEX IF NOT EXISTS intraday_microstructure_features_lookup_idx
    ON intraday_microstructure_features(symbol, timeframe, timestamp);
