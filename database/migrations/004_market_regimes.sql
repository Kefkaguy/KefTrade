CREATE TABLE IF NOT EXISTS market_regimes (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    trend_regime TEXT NOT NULL,
    volatility_regime TEXT NOT NULL,
    trend_strength NUMERIC NOT NULL,
    volatility_score NUMERIC,
    close_vs_ema50 NUMERIC,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(symbol, timeframe, timestamp),
    CONSTRAINT market_regimes_trend_check CHECK (trend_regime IN ('bull_trend', 'bear_trend', 'sideways', 'unknown')),
    CONSTRAINT market_regimes_volatility_check CHECK (volatility_regime IN ('high_volatility', 'low_volatility', 'normal_volatility', 'unknown'))
);

CREATE INDEX IF NOT EXISTS market_regimes_symbol_timeframe_timestamp_idx
    ON market_regimes(symbol, timeframe, timestamp);

CREATE INDEX IF NOT EXISTS market_regimes_trend_volatility_idx
    ON market_regimes(symbol, timeframe, trend_regime, volatility_regime);
