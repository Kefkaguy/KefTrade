CREATE TABLE IF NOT EXISTS alpha_validation_runs (
    id BIGSERIAL PRIMARY KEY,
    symbol_set JSONB NOT NULL,
    timeframe_set JSONB NOT NULL,
    candidate_count INTEGER NOT NULL,
    thresholds JSONB NOT NULL,
    summary JSONB NOT NULL,
    report JSONB NOT NULL,
    markdown_report TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS alpha_validation_runs_created_at_idx
    ON alpha_validation_runs(created_at DESC);

INSERT INTO symbols(symbol, asset_class, exchange, currency, name, provider_symbol, primary_provider, base_asset, quote_asset)
VALUES ('ETHUSDT', 'crypto', 'BINANCE', 'USDT', 'Ethereum / Tether USD', 'ETHUSDT', 'binance_dev', 'ETH', 'USDT')
ON CONFLICT (symbol) DO NOTHING;
