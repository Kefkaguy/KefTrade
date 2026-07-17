INSERT INTO symbols(symbol, asset_class, exchange, currency, name, provider_symbol, primary_provider, sector, index_membership, is_active)
VALUES
    ('AMD', 'us_equity', 'NASDAQ', 'USD', 'Advanced Micro Devices, Inc.', 'AMD', 'yfinance_research', 'Technology', '["S&P 500", "NASDAQ 100"]'::jsonb, TRUE),
    ('AMZN', 'us_equity', 'NASDAQ', 'USD', 'Amazon.com, Inc.', 'AMZN', 'yfinance_research', 'Consumer Cyclical', '["S&P 500", "NASDAQ 100"]'::jsonb, TRUE),
    ('GOOGL', 'us_equity', 'NASDAQ', 'USD', 'Alphabet Inc. Class A', 'GOOGL', 'yfinance_research', 'Communication Services', '["S&P 500", "NASDAQ 100"]'::jsonb, TRUE),
    ('META', 'us_equity', 'NASDAQ', 'USD', 'Meta Platforms, Inc.', 'META', 'yfinance_research', 'Communication Services', '["S&P 500", "NASDAQ 100"]'::jsonb, TRUE)
ON CONFLICT (symbol)
DO UPDATE SET
    asset_class = EXCLUDED.asset_class,
    exchange = EXCLUDED.exchange,
    currency = EXCLUDED.currency,
    name = EXCLUDED.name,
    provider_symbol = EXCLUDED.provider_symbol,
    primary_provider = EXCLUDED.primary_provider,
    sector = EXCLUDED.sector,
    index_membership = EXCLUDED.index_membership,
    is_active = TRUE;
