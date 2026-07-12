CREATE TABLE IF NOT EXISTS evidence_alerts (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    verdict TEXT NOT NULL,
    evidence_summary TEXT NOT NULL,
    matched_rules JSONB NOT NULL DEFAULT '[]'::jsonb,
    failed_rules JSONB NOT NULL DEFAULT '[]'::jsonb,
    profit_factor NUMERIC,
    expectancy NUMERIC,
    trade_count INTEGER,
    max_drawdown NUMERIC,
    regime TEXT,
    candle_timestamp TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ,
    simulation_only BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT evidence_alerts_simulation_only_check CHECK (simulation_only = TRUE),
    CONSTRAINT evidence_alerts_type_check CHECK (
        alert_type IN (
            'entry_setup_review',
            'exit_risk_review',
            'avoid_condition',
            'stale_data_warning',
            'scheduler_error',
            'duplicate_candle_skip'
        )
    ),
    CONSTRAINT evidence_alerts_severity_check CHECK (severity IN ('info', 'warning', 'critical'))
);

CREATE INDEX IF NOT EXISTS evidence_alerts_created_idx ON evidence_alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS evidence_alerts_symbol_timeframe_idx ON evidence_alerts(symbol, timeframe, created_at DESC);
CREATE INDEX IF NOT EXISTS evidence_alerts_unacknowledged_idx ON evidence_alerts(acknowledged_at) WHERE acknowledged_at IS NULL;
