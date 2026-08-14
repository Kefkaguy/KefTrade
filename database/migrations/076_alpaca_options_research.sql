-- Alpaca option-chain snapshots and point-in-time options features.
--
-- Option chains are latest snapshots, not a clean historical backfill for old
-- decision timestamps.  Store observed_at explicitly and only expose rows with
-- observed_at <= decision_timestamp to research.

CREATE TABLE IF NOT EXISTS intraday_option_chain_snapshots (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    underlying_symbol TEXT NOT NULL,
    option_symbol TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    quote_timestamp TIMESTAMPTZ,
    trade_timestamp TIMESTAMPTZ,
    expiration_date DATE,
    option_type TEXT,
    strike_price NUMERIC,
    bid_price NUMERIC,
    ask_price NUMERIC,
    bid_size NUMERIC,
    ask_size NUMERIC,
    trade_price NUMERIC,
    trade_size NUMERIC,
    implied_volatility NUMERIC,
    delta NUMERIC,
    gamma NUMERIC,
    theta NUMERIC,
    vega NUMERIC,
    rho NUMERIC,
    open_interest NUMERIC,
    raw_payload JSONB NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT intraday_option_chain_type_check CHECK (option_type IS NULL OR option_type IN ('call', 'put')),
    CONSTRAINT intraday_option_chain_hash_check CHECK (length(content_hash) = 64)
);

CREATE UNIQUE INDEX IF NOT EXISTS intraday_option_chain_unique_snapshot_idx
    ON intraday_option_chain_snapshots(provider, feed, underlying_symbol, option_symbol, observed_at);

CREATE INDEX IF NOT EXISTS intraday_option_chain_lookup_idx
    ON intraday_option_chain_snapshots(underlying_symbol, observed_at, expiration_date, strike_price);

CREATE TABLE IF NOT EXISTS intraday_option_ingest_checkpoints (
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    underlying_symbol TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    pages INTEGER NOT NULL DEFAULT 0,
    contracts_seen INTEGER NOT NULL DEFAULT 0,
    contracts_upserted INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (provider, feed, underlying_symbol, started_at),
    CONSTRAINT intraday_option_ingest_status_check CHECK (status IN ('running', 'completed', 'failed')),
    CONSTRAINT intraday_option_ingest_pages_check CHECK (pages >= 0),
    CONSTRAINT intraday_option_ingest_seen_check CHECK (contracts_seen >= 0),
    CONSTRAINT intraday_option_ingest_upserted_check CHECK (contracts_upserted >= 0)
);

CREATE TABLE IF NOT EXISTS intraday_option_feature_snapshots (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    features JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (symbol, timeframe, timestamp, provider, feed, feature_version),
    CONSTRAINT intraday_option_feature_timeframe_check CHECK (timeframe IN ('1m', '15m', '30m'))
);

CREATE INDEX IF NOT EXISTS intraday_option_feature_lookup_idx
    ON intraday_option_feature_snapshots(provider, feed, feature_version, timeframe, timestamp);
