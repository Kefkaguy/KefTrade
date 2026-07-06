# KefTrade Quant Research MVP

KefTrade is a professional quantitative stock research platform in development, not an automated trading system. Version 0.1 uses `BTCUSDT` on the `4h` timeframe only as a deterministic development environment because Binance provides accessible historical candles.

## What v0.1 Does

- Syncs development market data through a `MarketDataProvider` abstraction.
- Uses Binance `BTCUSDT` `4h` candles as the current dev provider.
- Logs raw provider responses for debugging.
- Calculates technical features with past-only rolling windows.
- Runs `trend_pullback_v1` from versioned strategy parameters.
- Backtests with fees, slippage, stop-loss, take-profit, and walk-forward validation.
- Shows restrained research signals and risk settings.

## What v0.1 Does Not Do

- No Model Engine or trained model.
- No paper trading.
- No live trading.
- No futures, leverage, margin, or auto-execution.
- No production stock provider yet.

## Architecture Direction

KefTrade is designed around US equities first. The initial research universe is:

```text
AAPL, MSFT, NVDA, AMD, META, AMZN, GOOGL, TSLA, SPY, QQQ
```

Provider-specific integrations must sit behind common interfaces:

- `MarketDataProvider`
- `TradingCalendar`
- `CorporateActions`
- `SymbolMetadata`
- `ExchangeInfo`

Stock-specific concepts are first-class architectural concerns: regular market hours, premarket, after-hours, earnings dates, dividends, stock splits, exchange holidays, sector classification, market capitalization, and index membership.

## Local Development

1. Copy `.env.example` to `.env`.
2. Start Postgres:

```powershell
docker compose up -d postgres
```

3. Apply the migration in `database/migrations/001_init.sql`.
4. Start the API from `apps/api`.
5. Start the web app from `apps/web`.

## API Defaults

- API: `http://127.0.0.1:8000`
- Web: `http://127.0.0.1:3000`
- Symbol: `BTCUSDT`
- Timeframe: `4h`
- Provider: `binance_dev`
