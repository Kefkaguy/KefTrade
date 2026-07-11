ALTER TABLE strategy_deployments
    ADD COLUMN IF NOT EXISTS last_scan_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_signal TEXT,
    ADD COLUMN IF NOT EXISTS last_check_result TEXT,
    ADD COLUMN IF NOT EXISTS last_scan_payload JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS strategy_deployments_symbol_timeframe_idx ON strategy_deployments(symbol, timeframe, status);
