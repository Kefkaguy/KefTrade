-- Backend-only intraday execution-cost research.
--
-- Quotes and calibrations are append-only research evidence. Nothing in
-- these tables is reachable from order submission or live risk controls.

CREATE TABLE IF NOT EXISTS intraday_quote_snapshots (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    bid_price NUMERIC NOT NULL,
    ask_price NUMERIC NOT NULL,
    bid_size NUMERIC,
    ask_size NUMERIC,
    midpoint NUMERIC NOT NULL,
    spread_bps NUMERIC NOT NULL,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(symbol, provider, feed, timestamp),
    CONSTRAINT intraday_quotes_prices_check CHECK (
        bid_price > 0 AND ask_price > 0 AND ask_price >= bid_price AND midpoint > 0
    ),
    CONSTRAINT intraday_quotes_sizes_check CHECK (
        (bid_size IS NULL OR bid_size >= 0) AND (ask_size IS NULL OR ask_size >= 0)
    ),
    CONSTRAINT intraday_quotes_spread_check CHECK (spread_bps >= 0)
);

CREATE INDEX IF NOT EXISTS intraday_quote_snapshots_lookup_idx
    ON intraday_quote_snapshots(symbol, timestamp);

CREATE TABLE IF NOT EXISTS intraday_execution_cost_calibrations (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    feed TEXT NOT NULL,
    window_start TIMESTAMPTZ,
    window_end TIMESTAMPTZ,
    symbols JSONB NOT NULL,
    quote_observations INTEGER NOT NULL,
    matched_fill_observations INTEGER NOT NULL,
    regulatory_bps NUMERIC NOT NULL,
    median_spread_bps NUMERIC,
    p90_spread_bps NUMERIC,
    median_signed_fill_slippage_bps NUMERIC,
    p90_signed_fill_slippage_bps NUMERIC,
    observed_round_trip_bps NUMERIC,
    stressed_round_trip_bps NUMERIC,
    conservative_round_trip_bps NUMERIC NOT NULL DEFAULT 30,
    by_symbol JSONB NOT NULL DEFAULT '{}'::jsonb,
    by_time_slot JSONB NOT NULL DEFAULT '{}'::jsonb,
    methodology JSONB NOT NULL,
    calculation_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT intraday_cost_counts_check CHECK (
        quote_observations >= 0 AND matched_fill_observations >= 0
    ),
    CONSTRAINT intraday_cost_values_check CHECK (
        regulatory_bps >= 0
        AND (observed_round_trip_bps IS NULL OR observed_round_trip_bps >= 0)
        AND (stressed_round_trip_bps IS NULL OR stressed_round_trip_bps >= 0)
        AND conservative_round_trip_bps >= 0
    )
);

CREATE INDEX IF NOT EXISTS intraday_execution_cost_calibrations_created_idx
    ON intraday_execution_cost_calibrations(created_at DESC);
