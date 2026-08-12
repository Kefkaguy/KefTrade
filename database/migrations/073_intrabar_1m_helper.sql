-- 1m intrabar helper support.
--
-- The 1m layer is not a standalone strategy family.  It is microscope data
-- for higher-timeframe events: after a 15m/30m setup is identified, 1m
-- candles and signed trade flow can describe whether participation is
-- improving, exhausting, or becoming too expensive to execute.

ALTER TABLE intraday_premarket_features
    DROP CONSTRAINT IF EXISTS intraday_premarket_timeframe_check;

ALTER TABLE intraday_premarket_features
    ADD CONSTRAINT intraday_premarket_timeframe_check
    CHECK (timeframe IN ('1m', '15m', '30m'));

ALTER TABLE intraday_trade_flow_features
    DROP CONSTRAINT IF EXISTS intraday_trade_flow_timeframe_check;

ALTER TABLE intraday_trade_flow_features
    ADD CONSTRAINT intraday_trade_flow_timeframe_check
    CHECK (timeframe IN ('1m', '15m', '30m'));

CREATE TABLE IF NOT EXISTS intraday_intrabar_diagnostic_runs (
    id BIGSERIAL PRIMARY KEY,
    parent_dataset_id BIGINT NOT NULL
        REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    parent_timeframe TEXT NOT NULL,
    intrabar_timeframe TEXT NOT NULL,
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    factor_keys JSONB NOT NULL,
    results JSONB NOT NULL,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT intraday_intrabar_parent_timeframe_check
        CHECK (parent_timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_intrabar_timeframe_check
        CHECK (intrabar_timeframe = '1m'),
    CONSTRAINT intraday_intrabar_immutable_check CHECK (immutable = TRUE)
);

CREATE INDEX IF NOT EXISTS intraday_intrabar_diagnostic_runs_lookup_idx
    ON intraday_intrabar_diagnostic_runs(parent_dataset_id, created_at DESC);

DROP TRIGGER IF EXISTS intraday_intrabar_diagnostic_runs_immutable_trigger
    ON intraday_intrabar_diagnostic_runs;

CREATE TRIGGER intraday_intrabar_diagnostic_runs_immutable_trigger
    BEFORE UPDATE OR DELETE ON intraday_intrabar_diagnostic_runs
    FOR EACH ROW EXECUTE FUNCTION prevent_intraday_research_evidence_mutation();
