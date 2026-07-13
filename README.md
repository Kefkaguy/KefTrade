# KefTrade Quant Research Platform

KefTrade is a research-first quantitative strategy platform. It has moved beyond an MVP: the core product is now a multi-asset research operating system that can collect evidence, validate strategies, rank opportunities, automate research workloads, and generate new deterministic strategy variants.

KefTrade is still research-only unless a future phase explicitly changes that boundary. The current system does not route broker orders, does not connect to external broker paper accounts, does not trade live capital, and does not support leverage, margin, shorting, or automatic execution.

## Current State

KefTrade is currently at:

**✅ Phase 9 — Autonomous Strategy Discovery**

The platform can now assemble deterministic strategies from reusable rule blocks, test them against stored market data, reject weak combinations, promote stronger variants, and preserve family-tree history for strategy evolution. Every conclusion must come from stored evidence.

## Product Phases

### ✅ Phase 1 — Research Foundation

The foundation established KefTrade as a quantitative research workspace instead of a manual signal tool. It added market-data ingestion, provider abstractions, candle storage, feature calculation, deterministic strategy definitions, basic backtesting, and a clear research-only operating model.

### ✅ Phase 2 — Evidence & Validation

KefTrade added deeper research evidence: experiment sweeps, validation metrics, walk-forward analysis, out-of-sample checks, alpha validation diagnostics, passed and failed evidence rules, rejection explanations, and saved research history. Strategies stopped being judged by one backtest and started being judged by repeatable evidence.

### ✅ Phase 3 — Internal Paper Trading

KefTrade added an internal simulation subsystem for paper accounts, simulated orders, fills, positions, equity curves, and deployment lifecycle records. This phase is simulation-only. It does not connect to brokers, route real orders, or weaken research validation standards.

### ✅ Phase 4 — Mission Control

Mission Control became the main operating surface for the platform. It brings together system health, research opportunities, evidence status, candidate review queues, daily summaries, and safety state so the user can understand the research engine at a glance.

### ✅ Phase 5 — Reports & Analytics

KefTrade added durable research reporting: daily research reports, audit summaries, stored analytics, candidate notebooks, validation run drilldowns, charts, tables, and evidence timelines. The platform can now explain what it tested, what failed, what improved, and what still needs more evidence.

### ✅ Phase 6 — Multi-Asset Management

The platform expanded from single-symbol research into multi-asset coverage. It supports crypto development data, US equity research symbols, asset coverage views, cross-asset candidate ranking, multi-timeframe validation, and portfolio-style research comparisons without becoming a live trading system.

### ✅ Phase 7 — Research Intelligence

Research Intelligence turns stored evidence into deterministic rankings and meta-analysis. It can identify promising candidates, common failure reasons, weak regimes, stronger indicator combinations, evidence concentration, and review priorities. The system never invents conclusions; it ranks only what has been stored.

### ✅ Phase 8 — Research Automation

Research Automation made research the primary workload. KefTrade can queue experiments across assets, timeframes, strategies, and parameter sweeps; avoid duplicate work; store every run permanently; generate follow-up hypotheses from failures; and update research analysis from completed jobs.

### ✅ Phase 9 — Autonomous Strategy Discovery

Autonomous Strategy Discovery lets KefTrade generate new deterministic strategies from modular rule blocks. The engine combines trend, momentum, volatility, volume, entry, and exit components; filters impossible or redundant combinations; backtests candidates; validates them; computes research scores; promotes strong variants; rejects weak ones; and records lineage for future evolution.

This is where the platform is now.

### ⬜ Phase 10 — External Broker Paper Trading

This future phase would connect KefTrade to an external broker paper environment while keeping all activity simulated. It should remain separate from live trading and must preserve explicit safeguards around routing, account type, permissions, and research validation.

### ⬜ Phase 11 — Optional Live Trading

This future phase is optional and should only be considered after the research system consistently produces statistically robust strategies, safety controls are audited, and live execution boundaries are deliberately designed. It is not part of the current platform.

## Research-Only Guardrails

KefTrade currently does not implement:

- Live trading
- External broker paper trading
- Broker routing
- Real-money order execution
- Margin
- Leverage
- Shorting
- Automatic execution
- Buy or sell recommendations
- Validation shortcuts to force strategies to pass

Any strategy that has not passed the required evidence gates must be treated as research evidence only.

## What KefTrade Does Today

- Syncs market candles through provider abstractions.
- Supports crypto development data and US equity research symbols.
- Calculates past-only technical features and market regimes.
- Runs deterministic strategy research across multiple strategy families.
- Runs backtests with fees, slippage, stop-loss, take-profit, equity curve, drawdown, expectancy, Sharpe, profit factor, and trade counts.
- Runs bounded strategy experiment sweeps without weakening validation gates.
- Evaluates promising candidates across assets, timeframes, train/test splits, and walk-forward windows.
- Produces alpha validation diagnostics with thresholds, actual values, passed rules, failed rules, and rejection explanations.
- Tracks research candidate lifecycle states, evidence drift, notebooks, and validation history.
- Ranks stored research evidence through Research Intelligence.
- Automates large-scale research queues and stores every experiment permanently.
- Generates new deterministic strategy variants from reusable rule blocks.
- Tracks strategy family trees, promotions, rejections, and evolution events.
- Provides Ask Kef for read-only explanations over stored research evidence.
- Provides internal simulation-only paper accounts, paper orders, fills, positions, equity curves, and deployment lifecycle records.

## Main Web Pages

- `/mission-control` - main operating surface for research status and system health
- `/strategy-discovery` - autonomous deterministic strategy generation, promotion, rejection, and evolution
- `/dashboard` - research command center
- `/research` - deterministic strategy research runner
- `/research-intelligence` - stored evidence ranking and meta-analysis
- `/promising` - cross-asset promising candidate ranking
- `/portfolio` - candidate lifecycle and research portfolio
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
- `/reports` - saved research reports
- `/copilot` - Ask Kef research copilot
- `/paper` - internal paper simulation dashboard
- `/paper/portfolio` - simulated portfolio balances
- `/paper/orders` - simulated orders and fills
- `/paper/positions` - simulated long-only positions
- `/paper/deployments` - simulation-only strategy deployment lifecycle

The root route redirects to Mission Control.

## Backend API Areas

- Market data: `/data/sync`, `/candles/{symbol}`
- Features and regimes: `/features/sync`, `/regimes/sync`
- Strategy research: `/research/strategies`
- Strategy experiments: `/research/strategy-experiments`
- Strategy discovery: `/research/strategy-discovery/*`
- Research automation: `/research/automation/*`
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

## Engineering Notes

KefTrade is no longer just an MVP. The next engineering passes should focus on scaling and hardening the research platform:

- Typed API response models
- Pagination and sorting on large research endpoints
- Dedicated candidate detail API endpoints
- Materialized research snapshots for expensive portfolio and intelligence views
- Better scheduler controls for large experiment queues
- More advanced strategy family lineage analytics
- Stronger chart axes, legends, filters, and table controls
- Consistent error envelopes across the API
- Security and safety review before any future broker-connected phase
