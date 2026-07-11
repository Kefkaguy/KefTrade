ALTER TABLE strategy_deployments
    ADD COLUMN IF NOT EXISTS last_scanned_candle_timestamp TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS strategy_deployments_last_scanned_candle_idx
    ON strategy_deployments(id, last_scanned_candle_timestamp);
