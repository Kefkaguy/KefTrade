-- Return-blind signed-trade-imbalance calibration.
--
-- The original order-flow declaration used an absolute 0.30 cutoff chosen
-- before any trade-flow distribution existed.  This migration creates an
-- immutable, predictor-only calibration record.  It deliberately contains no
-- candles, forward prices, returns, P&L, or factor outcomes.

ALTER TABLE intraday_trade_flow_features
    ADD COLUMN IF NOT EXISTS classification_method TEXT,
    ADD COLUMN IF NOT EXISTS classified_volume NUMERIC,
    ADD COLUMN IF NOT EXISTS trade_size_squared_sum NUMERIC,
    ADD COLUMN IF NOT EXISTS effective_trade_count NUMERIC;

ALTER TABLE research_dataset_trade_flow_features
    ADD COLUMN IF NOT EXISTS classification_method TEXT,
    ADD COLUMN IF NOT EXISTS classified_volume NUMERIC,
    ADD COLUMN IF NOT EXISTS trade_size_squared_sum NUMERIC,
    ADD COLUMN IF NOT EXISTS effective_trade_count NUMERIC;

CREATE TABLE IF NOT EXISTS intraday_trade_imbalance_calibrations (
    id BIGSERIAL PRIMARY KEY,
    timeframe TEXT NOT NULL,
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    symbols JSONB NOT NULL,
    dataset_hash TEXT NOT NULL,
    specification_hash TEXT NOT NULL,
    threshold_mode TEXT NOT NULL,
    calibrated_threshold NUMERIC,
    ready_for_declaration BOOLEAN NOT NULL,
    report JSONB NOT NULL,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT trade_imbalance_calibration_timeframe_check CHECK (timeframe = '30m'),
    CONSTRAINT trade_imbalance_calibration_mode_check
        CHECK (threshold_mode IN ('global', 'time_liquidity_bucket')),
    CONSTRAINT trade_imbalance_calibration_window_check CHECK (window_end >= window_start),
    CONSTRAINT trade_imbalance_calibration_immutable_check CHECK (immutable = TRUE),
    UNIQUE (dataset_hash, specification_hash)
);

CREATE TABLE IF NOT EXISTS intraday_trade_imbalance_calibration_rows (
    calibration_id BIGINT NOT NULL
        REFERENCES intraday_trade_imbalance_calibrations(id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    session_date DATE NOT NULL,
    time_slot TEXT NOT NULL,
    liquidity_bucket TEXT NOT NULL,
    trade_count INTEGER NOT NULL,
    total_volume NUMERIC NOT NULL,
    classified_volume NUMERIC NOT NULL,
    trade_size_squared_sum NUMERIC NOT NULL,
    effective_trade_count NUMERIC NOT NULL,
    signed_trade_imbalance NUMERIC NOT NULL,
    unclassified_share NUMERIC NOT NULL,
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (calibration_id, symbol, timestamp),
    CONSTRAINT trade_imbalance_calibration_row_immutable_check CHECK (immutable = TRUE),
    CONSTRAINT trade_imbalance_calibration_row_range_check
        CHECK (signed_trade_imbalance BETWEEN -1 AND 1),
    CONSTRAINT trade_imbalance_calibration_row_volume_check
        CHECK (
            total_volume > 0 AND classified_volume > 0
            AND trade_size_squared_sum > 0 AND effective_trade_count > 0
        )
);

CREATE INDEX IF NOT EXISTS intraday_trade_imbalance_calibration_lookup_idx
    ON intraday_trade_imbalance_calibrations(created_at DESC);

DO $$
DECLARE
    target TEXT;
    trigger_name TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'intraday_trade_imbalance_calibrations',
        'intraday_trade_imbalance_calibration_rows'
    ]
    LOOP
        trigger_name := target || '_immutable_trigger';
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', trigger_name, target);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I ' ||
            'FOR EACH ROW EXECUTE FUNCTION prevent_intraday_research_evidence_mutation()',
            trigger_name,
            target
        );
    END LOOP;
END;
$$;
