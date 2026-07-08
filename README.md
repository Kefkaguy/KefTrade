# KefTrade Quant Research MVP

KefTrade is a research-only quantitative market research platform. It is built to generate, backtest, compare, validate, and explain strategy research evidence. It is not a live trading system.

The MVP now includes deterministic strategy research, experiment sweeps, cross-asset candidate ranking, alpha validation diagnostics, lifecycle tracking, portfolio views, research notebooks, and drilldown pages for assets, strategies, candidates, experiments, and validation runs.

Phase 1 toward algorithmic trading adds internal paper trading architecture only. It is a simulation subsystem for accounting, orders, fills, positions, equity curves, and deployment lifecycle. It does not connect to brokers or route real orders.

## Research-Only Guardrails

KefTrade does not implement:

- Live trading
- Paper trading workflows
- Broker integration
- Order routing or execution
- Futures, margin, or leverage workflows
- Buy/sell recommendations
- Weakened validation standards to make strategies pass

Any strategy that has not passed alpha validation must be treated as research evidence only.

## What The MVP Does

- Syncs market candles through provider abstractions.
- Supports crypto development data and US equity research symbols.
- Calculates past-only technical features.
- Runs deterministic strategy research for:
  - Trend Pullback
  - Momentum
  - Breakout
  - Mean Reversion
  - Volatility Breakout
  - 200 EMA Trend
- Runs backtests with fees, slippage, stop-loss, take-profit, equity curve, drawdown, expectancy, Sharpe, profit factor, and trade counts.
- Runs bounded strategy experiment sweeps without weakening validation gates.
- Evaluates promising candidates across assets, timeframes, train/test splits, and walk-forward windows.
- Produces alpha validation diagnostics with passed rules, failed rules, thresholds, actual values, and plain-English rejection explanations.
- Tracks research candidate lifecycle states:
  - Hypothesis
  - Experimenting
  - Promising
  - Needs More Evidence
  - Alpha Validation
  - Validated
  - Archived
  - Rejected
- Shows research portfolio, evidence timeline, candidate comparison, evidence drift, notebooks, and audit reports.
- Provides read-only AI copilot summaries over stored research evidence.
- Provides simulation-only paper accounts, orders, fills, positions, equity curves, and strategy deployment lifecycle records.

## Main Web Pages

- `/` - simple research workflow
- `/dashboard` - research command center
- `/portfolio` - candidate lifecycle and research portfolio
- `/promising` - cross-asset promising candidate ranking
- `/candidates/[id]` - candidate drilldown
- `/experiments` - experiment definitions and archive
- `/experiments/[id]` - experiment drilldown
- `/validation` - alpha validation runner and saved runs
- `/validation/[id]` - validation run drilldown
- `/assets` and `/assets/[symbol]` - asset coverage and market data
- `/strategies` and `/strategies/[name]` - strategy evidence
- `/hypotheses` - research hypothesis workflow
- `/journal` - research journal
- `/market-intelligence` - regime and archive diagnostics
- `/reports` - saved local research reports
- `/paper` - paper trading dashboard
- `/paper/portfolio` - simulated portfolio balances
- `/paper/orders` - simulated orders and fills
- `/paper/positions` - simulated long-only positions
- `/paper/deployments` - simulation-only strategy deployment lifecycle

## Backend API Areas

- Market data: `/data/sync`, `/candles/{symbol}`
- Features/regimes: `/features/sync`, `/regimes/sync`
- Strategy research: `/research/strategies`
- Strategy experiments: `/research/strategy-experiments`
- Promising candidates: `/research/promising-candidates`
- Research portfolio: `/research/portfolio`
- Alpha discovery: `/alpha/discover`
- Alpha validation: `/alpha/validate`, `/alpha/validation-runs`
- Research lab: `/research/hypotheses`, `/research/journal`
- Research intelligence: `/research/intelligence`, `/research/archive`, `/research/timeline`
- Copilot: `/research/copilot`
- Paper simulation: `/paper/accounts`, `/paper/orders`, `/paper/deployments`

## Local Development

1. Copy `.env.example` to `.env`.

2. Start Postgres from the repository root:

```powershell
docker compose up -d postgres
```

3. Install API dependencies:

```powershell
cd apps/api
python -m pip install -e ".[dev]"
```

4. Start the API:

```powershell
cd apps/api
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

5. Install web dependencies:

```powershell
cd apps/web
npm install
```

6. Start the web app:

```powershell
cd apps/web
npm run dev
```

The web app defaults to `http://127.0.0.1:3000`. If that port is occupied, Next.js may use `http://127.0.0.1:3001`.

## Useful Commands

Run backend tests:

```powershell
cd apps/api
python -m pytest
```

Build the web app:

```powershell
cd apps/web
npm run build
```

## Default Development Settings

- API: `http://127.0.0.1:8000`
- Web: `http://127.0.0.1:3000`
- Fallback web port: `http://127.0.0.1:3001`
- Crypto development provider: `binance_dev`
- Equity research provider: `yfinance_research`
- Default crypto symbol/timeframe: `BTCUSDT` / `4h`

## Reports

Generated research and audit artifacts are stored in `reports/`, including:

- `strategy_experiment_sweep_2026-07-08.md`
- `mvp_product_polish_audit_2026-07-08.md`

## Current Technical Debt

The MVP is functional, but the next engineering pass should focus on:

- Typed API response models
- Pagination and sorting on large research endpoints
- Dedicated candidate detail API endpoints
- Materialized portfolio snapshots instead of expensive GET-time recomputation
- Splitting the frontend API client by domain
- Stronger chart axes, legends, and table filtering
- More consistent error envelopes across the API
