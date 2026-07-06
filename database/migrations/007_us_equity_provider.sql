INSERT INTO symbols(symbol, asset_class, exchange, currency, name, provider_symbol, primary_provider, sector, index_membership)
VALUES
    ('SPY', 'etf', 'NYSEARCA', 'USD', 'SPDR S&P 500 ETF Trust', 'SPY', 'yfinance_research', NULL, '["S&P 500"]'::jsonb),
    ('QQQ', 'etf', 'NASDAQ', 'USD', 'Invesco QQQ Trust', 'QQQ', 'yfinance_research', NULL, '["NASDAQ 100"]'::jsonb),
    ('AAPL', 'us_equity', 'NASDAQ', 'USD', 'Apple Inc.', 'AAPL', 'yfinance_research', 'Technology', '["S&P 500", "NASDAQ 100"]'::jsonb),
    ('MSFT', 'us_equity', 'NASDAQ', 'USD', 'Microsoft Corporation', 'MSFT', 'yfinance_research', 'Technology', '["S&P 500", "NASDAQ 100"]'::jsonb),
    ('NVDA', 'us_equity', 'NASDAQ', 'USD', 'NVIDIA Corporation', 'NVDA', 'yfinance_research', 'Technology', '["S&P 500", "NASDAQ 100"]'::jsonb),
    ('TSLA', 'us_equity', 'NASDAQ', 'USD', 'Tesla, Inc.', 'TSLA', 'yfinance_research', 'Consumer Cyclical', '["S&P 500", "NASDAQ 100"]'::jsonb)
ON CONFLICT (symbol)
DO UPDATE SET
    asset_class = EXCLUDED.asset_class,
    exchange = EXCLUDED.exchange,
    currency = EXCLUDED.currency,
    name = EXCLUDED.name,
    provider_symbol = EXCLUDED.provider_symbol,
    primary_provider = EXCLUDED.primary_provider,
    sector = EXCLUDED.sector,
    index_membership = EXCLUDED.index_membership;
