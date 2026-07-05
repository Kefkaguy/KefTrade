CREATE TABLE IF NOT EXISTS symbols (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL UNIQUE,
    base_asset TEXT NOT NULL,
    quote_asset TEXT NOT NULL,
    source TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS raw_api_logs (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    request_params JSONB NOT NULL,
    response_status INTEGER NOT NULL,
    response_body JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS candles (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    source TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    volume NUMERIC NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(symbol, source, timeframe, timestamp)
);

CREATE INDEX IF NOT EXISTS candles_symbol_timeframe_timestamp_idx
    ON candles(symbol, timeframe, timestamp);

CREATE TABLE IF NOT EXISTS features (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    returns_1 NUMERIC,
    returns_5 NUMERIC,
    ema_20 NUMERIC,
    ema_50 NUMERIC,
    rsi_14 NUMERIC,
    macd NUMERIC,
    macd_signal NUMERIC,
    volume_change NUMERIC,
    volatility_20 NUMERIC,
    distance_from_ema_20 NUMERIC,
    distance_from_ema_50 NUMERIC,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(symbol, timeframe, timestamp)
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    parameters JSONB NOT NULL,
    description TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS backtests (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    train_start TIMESTAMPTZ,
    train_end TIMESTAMPTZ,
    validation_start TIMESTAMPTZ,
    validation_end TIMESTAMPTZ,
    metrics JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_trades (
    id BIGSERIAL PRIMARY KEY,
    backtest_id BIGINT NOT NULL REFERENCES backtests(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_time TIMESTAMPTZ NOT NULL,
    exit_time TIMESTAMPTZ NOT NULL,
    entry_price NUMERIC NOT NULL,
    exit_price NUMERIC NOT NULL,
    quantity NUMERIC NOT NULL,
    stop_loss NUMERIC NOT NULL,
    take_profit NUMERIC NOT NULL,
    pnl NUMERIC NOT NULL,
    pnl_pct NUMERIC NOT NULL,
    exit_reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    signal TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    entry_zone JSONB,
    stop_loss NUMERIC,
    take_profit NUMERIC,
    risk_reward NUMERIC,
    explanation JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS risk_settings (
    id BIGSERIAL PRIMARY KEY,
    account_size NUMERIC NOT NULL DEFAULT 10000,
    max_risk_per_trade NUMERIC NOT NULL DEFAULT 0.01,
    max_open_exposure NUMERIC NOT NULL DEFAULT 0.03,
    daily_loss_limit NUMERIC NOT NULL DEFAULT 0.02,
    weekly_loss_limit NUMERIC NOT NULL DEFAULT 0.05,
    allow_leverage BOOLEAN NOT NULL DEFAULT FALSE,
    allow_live_trading BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO symbols(symbol, base_asset, quote_asset, source)
VALUES ('BTCUSDT', 'BTC', 'USDT', 'binance')
ON CONFLICT (symbol) DO NOTHING;

INSERT INTO risk_settings(id)
VALUES (1)
ON CONFLICT (id) DO NOTHING;

INSERT INTO strategy_versions(name, version, parameters, description)
VALUES (
    'trend_pullback',
    'v1',
    '{
      "ema_fast": 20,
      "ema_slow": 50,
      "rsi_min": 40,
      "rsi_max": 60,
      "volume_change_min": -0.25,
      "entry_distance_to_ema20_max": 0.015,
      "swing_lookback": 5,
      "risk_reward": 2,
      "fee_rate": 0.001,
      "slippage_rate": 0.0005,
      "risk_per_trade": 0.01,
      "initial_equity": 10000,
      "walk_forward_train_ratio": 0.7
    }',
    'Long-only trend pullback strategy for BTCUSDT 4h research.'
)
ON CONFLICT (name, version) DO NOTHING;

