ALTER TABLE symbols
ADD COLUMN IF NOT EXISTS asset_class TEXT,
ADD COLUMN IF NOT EXISTS exchange TEXT,
ADD COLUMN IF NOT EXISTS currency TEXT,
ADD COLUMN IF NOT EXISTS name TEXT,
ADD COLUMN IF NOT EXISTS provider_symbol TEXT,
ADD COLUMN IF NOT EXISTS primary_provider TEXT,
ADD COLUMN IF NOT EXISTS sector TEXT,
ADD COLUMN IF NOT EXISTS market_cap NUMERIC,
ADD COLUMN IF NOT EXISTS index_membership JSONB;

UPDATE symbols
SET asset_class = COALESCE(asset_class, 'crypto'),
    exchange = COALESCE(exchange, 'BINANCE'),
    currency = COALESCE(currency, quote_asset, 'USDT'),
    name = COALESCE(name, symbol),
    provider_symbol = COALESCE(provider_symbol, symbol),
    primary_provider = COALESCE(primary_provider, 'binance_dev')
WHERE symbol = 'BTCUSDT';

ALTER TABLE symbols
ALTER COLUMN asset_class SET NOT NULL,
ALTER COLUMN exchange SET NOT NULL,
ALTER COLUMN currency SET NOT NULL,
ALTER COLUMN name SET NOT NULL,
ALTER COLUMN provider_symbol SET NOT NULL,
ALTER COLUMN primary_provider SET NOT NULL;
