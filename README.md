# KefTrade BTC MVP

KefTrade is a quantitative trading research MVP, not an automated trading system. Version 0.1 is scoped to `BTCUSDT` on the `4h` timeframe using Binance public candles.

## What v0.1 Does

- Syncs Binance `BTCUSDT` `4h` candles.
- Logs raw Binance API responses for debugging.
- Calculates technical features with past-only rolling windows.
- Runs `trend_pullback_v1` from versioned strategy parameters.
- Backtests with fees, slippage, stop-loss, take-profit, and walk-forward validation.
- Shows restrained research signals and risk settings.

## What v0.1 Does Not Do

- No Model Engine or trained model.
- No paper trading.
- No live trading.
- No futures, leverage, margin, or auto-execution.

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
