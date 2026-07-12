CREATE TABLE IF NOT EXISTS signal_reviews (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT REFERENCES paper_accounts(id) ON DELETE SET NULL,
    deployment_id BIGINT REFERENCES strategy_deployments(id) ON DELETE SET NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    status TEXT NOT NULL,
    verdict TEXT NOT NULL,
    regime TEXT,
    evidence_score TEXT NOT NULL DEFAULT '0/0',
    matched_rules JSONB NOT NULL DEFAULT '[]'::jsonb,
    failed_rules JSONB NOT NULL DEFAULT '[]'::jsonb,
    profit_factor NUMERIC,
    expectancy NUMERIC,
    trade_count INTEGER,
    max_drawdown NUMERIC,
    latest_candle_timestamp TIMESTAMPTZ,
    data_freshness TEXT NOT NULL,
    possible_entry_price NUMERIC,
    invalidation_level NUMERIC,
    risk_target NUMERIC,
    exit_zone NUMERIC,
    risk_per_share NUMERIC,
    reward_per_share NUMERIC,
    risk_reward_ratio NUMERIC,
    max_holding_bars INTEGER,
    note TEXT,
    reviewed_at TIMESTAMPTZ,
    ignored_at TIMESTAMPTZ,
    sent_to_paper_simulation_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT signal_reviews_simulation_only_check CHECK (simulation_only = TRUE),
    CONSTRAINT signal_reviews_status_check CHECK (
        status IN (
            'No Setup',
            'Setup Forming',
            'Setup Worth Reviewing',
            'In Paper Position',
            'Exit Risk Worth Reviewing',
            'Invalidated',
            'Stale Data Blocked'
        )
    ),
    CONSTRAINT signal_reviews_verdict_check CHECK (
        verdict IN (
            'No Setup',
            'Setup Worth Reviewing',
            'Exit Risk Worth Reviewing',
            'Stale Data Blocked',
            'Invalidated'
        )
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS signal_reviews_unique_candle_idx
    ON signal_reviews(deployment_id, latest_candle_timestamp);

CREATE INDEX IF NOT EXISTS signal_reviews_created_idx ON signal_reviews(created_at DESC);
CREATE INDEX IF NOT EXISTS signal_reviews_symbol_timeframe_idx ON signal_reviews(symbol, timeframe, created_at DESC);
