-- Order-flow and premarket datasets.
--
-- The bounded candle-only gap experiment produced no survivor and its factor
-- versions are retired.  Continuing to reshape OHLC is what the protocol
-- forbids; the next hypotheses must rest on data the bars do not contain.
--
-- Two of the three families here need no new ingestion.  Premarket price
-- discovery is already sitting in the extended-hours bars the SIP feed
-- returns and that regular-session research deliberately excludes, and
-- sector-relative flow is derivable from candles plus the sector map.  Only
-- signed trade imbalance requires fetching trades.

CREATE TABLE IF NOT EXISTS intraday_premarket_features (
    symbol TEXT NOT NULL,
    session_date DATE NOT NULL,
    timeframe TEXT NOT NULL,
    source TEXT NOT NULL,
    premarket_bars INTEGER NOT NULL,
    premarket_volume NUMERIC NOT NULL,
    premarket_relative_volume NUMERIC,
    premarket_return NUMERIC,
    premarket_range NUMERIC,
    premarket_high NUMERIC,
    premarket_low NUMERIC,
    last_premarket_price NUMERIC,
    prior_regular_close NUMERIC,
    -- Prior regular close to the last premarket print.
    premarket_gap NUMERIC,
    -- Prior regular close to the 09:30 open: the gap that actually happened.
    opening_gap NUMERIC,
    -- How much of the eventual opening gap the premarket session had already
    -- discovered by 09:30.  A gap the premarket never priced is a different
    -- event from one it fully anticipated.
    gap_discovered_premarket NUMERIC,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, session_date, timeframe, source),
    CONSTRAINT intraday_premarket_timeframe_check CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_premarket_bars_check CHECK (premarket_bars > 0),
    CONSTRAINT intraday_premarket_volume_check CHECK (premarket_volume >= 0)
);

CREATE INDEX IF NOT EXISTS intraday_premarket_features_lookup_idx
    ON intraday_premarket_features(symbol, session_date);

CREATE TABLE IF NOT EXISTS intraday_trade_flow_features (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    trade_count INTEGER NOT NULL,
    total_volume NUMERIC NOT NULL,
    buy_volume NUMERIC NOT NULL,
    sell_volume NUMERIC NOT NULL,
    -- (buy - sell) / total.  Signed by the side that crossed the spread, so
    -- it measures which side was demanding liquidity rather than net volume.
    signed_trade_imbalance NUMERIC,
    signed_trade_count_imbalance NUMERIC,
    large_trade_share NUMERIC,
    unclassified_share NUMERIC,
    trade_vwap NUMERIC,
    effective_spread_bps NUMERIC,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, timeframe, timestamp, provider, feed),
    CONSTRAINT intraday_trade_flow_timeframe_check CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_trade_flow_count_check CHECK (trade_count > 0),
    CONSTRAINT intraday_trade_flow_volume_check CHECK (
        total_volume >= 0 AND buy_volume >= 0 AND sell_volume >= 0
    )
);

CREATE INDEX IF NOT EXISTS intraday_trade_flow_features_lookup_idx
    ON intraday_trade_flow_features(symbol, timeframe, timestamp);

-- Trade ingestion is orders of magnitude larger than candle ingestion, so it
-- checkpoints per symbol-session the same way the backfill does. Operational
-- progress, therefore mutable.
CREATE TABLE IF NOT EXISTS intraday_trade_ingest_checkpoints (
    symbol TEXT NOT NULL,
    session_date DATE NOT NULL,
    feed TEXT NOT NULL,
    status TEXT NOT NULL,
    trades_fetched INTEGER NOT NULL DEFAULT 0,
    bars_written INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    ingest_version TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, session_date, feed),
    CONSTRAINT intraday_trade_ingest_status_check
        CHECK (status IN ('running', 'completed', 'failed'))
);

CREATE INDEX IF NOT EXISTS intraday_trade_ingest_checkpoints_status_idx
    ON intraday_trade_ingest_checkpoints(status, updated_at DESC);

-- Snapshot companions, so a frozen dataset carries its order-flow evidence
-- with the same immutability as its candles.
CREATE TABLE IF NOT EXISTS research_dataset_premarket_features (
    dataset_id BIGINT NOT NULL REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL,
    session_date DATE NOT NULL,
    timeframe TEXT NOT NULL,
    premarket_bars INTEGER NOT NULL,
    premarket_volume NUMERIC NOT NULL,
    premarket_relative_volume NUMERIC,
    premarket_return NUMERIC,
    premarket_range NUMERIC,
    last_premarket_price NUMERIC,
    prior_regular_close NUMERIC,
    premarket_gap NUMERIC,
    opening_gap NUMERIC,
    gap_discovered_premarket NUMERIC,
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (dataset_id, symbol, session_date, timeframe),
    CONSTRAINT research_dataset_premarket_immutable_check CHECK (immutable = TRUE)
);

CREATE TABLE IF NOT EXISTS research_dataset_trade_flow_features (
    dataset_id BIGINT NOT NULL REFERENCES research_dataset_manifests(id) ON DELETE RESTRICT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    feed TEXT NOT NULL,
    trade_count INTEGER NOT NULL,
    total_volume NUMERIC NOT NULL,
    signed_trade_imbalance NUMERIC,
    signed_trade_count_imbalance NUMERIC,
    large_trade_share NUMERIC,
    unclassified_share NUMERIC,
    effective_spread_bps NUMERIC,
    immutable BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (dataset_id, symbol, timeframe, timestamp),
    CONSTRAINT research_dataset_trade_flow_immutable_check CHECK (immutable = TRUE)
);

DO $$
DECLARE
    target TEXT;
    trigger_name TEXT;
BEGIN
    FOREACH target IN ARRAY ARRAY[
        'research_dataset_premarket_features',
        'research_dataset_trade_flow_features'
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
