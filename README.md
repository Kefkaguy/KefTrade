# KefTrade Quant Research Platform

<img width="1024" height="1024" alt="image" src="https://github.com/user-attachments/assets/420dccf3-2360-4955-9c7d-e4b8794e17ed" />


KefTrade is a research-first quantitative strategy platform for deterministic strategy discovery, validation, candidate lifecycle management, and simulation-only forward validation.

The platform is designed around one rule: strategies advance only when stored evidence supports the promotion. Research thresholds, validation gates, elite promotion requirements, and forward-validation requirements are not weakened to force progress.

KefTrade is still research and simulation only. It does not route broker orders, does not connect to an external broker paper account, does not trade live capital, and does not support leverage, margin, shorting, or automatic live execution.

## Current State

KefTrade is currently in:

**Phase 9.12 - Elite Candidate Forward Validation**

The research platform has completed the Phase 9 research campaigns that produced elite candidates. KefTrade now has six elite research candidates and six candidate-linked simulation deployments. Phase 10 remains locked until candidate-linked forward validation produces enough eligible closed forward trades and independent prospective evidence.

Current operating status:

- Research infrastructure is functional.
- Campaign execution is functional.
- Candidate lifecycle tracking is functional.
- Mission Control is functional.
- Paper-trading infrastructure is simulation-only and functional.
- Six elite candidates exist.
- Six candidate-linked simulation deployments exist.
- Eligible closed forward trades are still required before Phase 10 can unlock.
- Historical campaign evidence is not a substitute for prospective forward evidence.

Phase 10 must not start until the existing readiness gates pass from eligible candidate-linked forward-validation evidence.

## Product Phases

### Complete - Phase 1: Research Foundation

KefTrade started as a quantitative research workspace rather than a manual signal tool. This phase added market-data ingestion, provider abstractions, candle storage, feature calculation, deterministic strategy definitions, basic backtesting, and the research-only operating model.

### Complete - Phase 2: Evidence and Validation

KefTrade added repeatable validation evidence: experiment sweeps, validation metrics, walk-forward analysis, out-of-sample checks, alpha validation diagnostics, passed and failed rules, rejection explanations, and saved research history.

Strategies are not judged by one backtest. They are judged by deterministic evidence across the required validation surfaces.

### Complete - Phase 3: Internal Paper Simulation

KefTrade added an internal simulation subsystem for paper accounts, simulated orders, fills, positions, equity records, and deployment lifecycle records.

This subsystem is internal and simulation-only. It does not connect to a broker and does not place real orders.

### Complete - Phase 4: Mission Control

Mission Control became the main operating surface for platform health, research state, candidate state, campaign status, scheduler activity, forward-validation readiness, diagnostics, and safety boundaries.

Mission Control supports both Simple Mode and Advanced Mode.

### Complete - Phase 5: Reports and Analytics

KefTrade added durable research reporting and analytics: daily research reports, audit summaries, stored analytics, candidate notebooks, validation drilldowns, charts, evidence timelines, and campaign summaries.

### Complete - Phase 6: Multi-Asset Management

The platform expanded from single-symbol experimentation into multi-asset and multi-timeframe research. It supports crypto development data, US equity research symbols, asset coverage diagnostics, cross-asset candidate ranking, and portfolio-style research comparisons.

### Complete - Phase 7: Research Intelligence

Research Intelligence ranks stored evidence and identifies candidate quality, common failure reasons, evidence concentration, strong strategy families, weak regimes, and review priorities.

The system only ranks evidence that exists in the database.

### Complete - Phase 8: Research Automation

Research Automation added queued campaigns, deterministic job execution, duplicate avoidance, worker tracking, campaign recovery, stored campaign analytics, follow-up hypothesis generation, and permanent experiment history.

### Complete - Phase 9: Autonomous Strategy Discovery

Phase 9 introduced deterministic strategy generation and mutation from reusable rule blocks. The engine combines trend, momentum, volatility, volume, entry, exit, and regime components, then validates candidates through the existing evidence pipeline.

Later Phase 9 campaigns added quality-first research, transferability testing, sample-size testing, overfit diagnosis, regime robustness, single-asset generalization, strategy redesign, and volatility-adaptive relative-strength research.

### Current - Phase 9.12: Candidate-Linked Forward Validation

Phase 9.12 connects elite research candidates to internal simulation-only deployments. Each deployment is linked to a specific elite candidate so future forward evidence can be separated from legacy simulation data.

The current goal is to collect eligible closed forward trades from candidate-linked simulation deployments without changing strategies, parameters, validation gates, or research thresholds.

Phase 9.12 adds:

- Elite candidate persistence.
- Candidate-linked simulation deployments.
- Candidate IDs on orders and fills.
- Candidate-linked forward-validation start timestamps.
- Forward evidence eligibility audits.
- Candidate-linked paper rollups.
- Evidence drift tracking.
- Phase 10 readiness assessment.
- Simulation-only guardrails.
- Scheduler-driven forward scans.
- Deployment health diagnostics.

### Locked - Phase 10: External Broker Paper Trading

Phase 10 is locked until candidate-linked forward validation satisfies the existing readiness requirements.

Phase 10 would connect KefTrade to an external broker paper environment while keeping activity simulated. It must remain separate from live trading and must preserve explicit safeguards around routing, account type, permissions, and validation standards.

### Future - Phase 11: Optional Live Trading

Live trading is not part of the current platform. It should only be considered after robust research evidence, forward-validation evidence, safety controls, auditability, and execution boundaries are deliberately designed and reviewed.

## Research and Safety Guardrails

KefTrade currently does not implement:

- Live trading.
- External broker paper trading.
- Broker order routing.
- Real-money execution.
- Margin.
- Leverage.
- Shorting.
- Automatic live execution.
- Buy or sell recommendations.
- Validation shortcuts.
- Threshold weakening to force promotion.

Any strategy that has not passed the required gates remains research evidence only.

Historical backtest evidence does not unlock Phase 10 by itself. Phase 10 requires eligible candidate-linked forward-validation evidence.

## What KefTrade Does Today

KefTrade can:

- Sync candles through provider abstractions.
- Support crypto development data and US equity research symbols.
- Calculate past-only technical features and market regimes.
- Run deterministic strategy research across multiple strategy families.
- Run backtests with fees, slippage, stops, targets, equity curves, drawdown, expectancy, Sharpe, profit factor, and trade counts.
- Run bounded strategy experiment sweeps without lowering validation gates.
- Evaluate candidates across assets, timeframes, train/test splits, walk-forward windows, and market regimes.
- Produce alpha validation diagnostics with thresholds, actual values, passed rules, failed rules, and rejection explanations.
- Track candidate lifecycle states, evidence drift, notebooks, and validation history.
- Rank stored evidence through Research Intelligence.
- Automate large research queues and store every job permanently.
- Generate deterministic strategy variants from reusable rule blocks.
- Track strategy family trees, promotions, rejections, and mutation history.
- Persist elite research candidates.
- Create candidate-linked simulation deployments for elite candidates.
- Collect candidate-linked simulated orders, fills, positions, logs, and forward evidence.
- Audit Phase 10 readiness from forward-validation evidence.
- Separate legacy simulation records from eligible candidate-linked forward evidence.
- Provide Ask Kef as a read-only research copilot over stored evidence.

## Research Campaigns

Recent Phase 9 campaigns focused on candidate quality rather than candidate quantity.

Campaign themes included:

- Local mutation around surviving research candidates.
- Pullback strategy robustness.
- Trend Following transferability.
- Breakout retirement analysis.
- Transferability and sample-size testing.
- Overfit diagnosis.
- Regime robustness.
- Single-asset robustness.
- Strategy redesign.
- Volatility-adaptive relative-strength research.
- Elite promotion and candidate-linked deployment.

The strongest historical research evidence has repeatedly concentrated around AAPL 1h Pullback-style strategies. Phase 9.12 exists to test whether promoted elite candidates continue to perform prospectively under candidate-linked simulation.

## Interface Modes

KefTrade supports two interface modes that use the same backend data.

### Simple Mode

Simple Mode is intended to make platform state understandable quickly. It summarizes:

- Campaign state.
- Scan/job counts.
- Candidate counts.
- Best candidate.
- Candidate lifecycle totals.
- Forward-validation status.
- Eligible forward evidence.
- Top failure reasons.
- Asset health.
- Phase progress.
- Phase 10 lock state.

Simple Mode must use authoritative latest completed campaign and candidate lifecycle data. Rejected candidates are evidence rejections, not operationally failed scans.

### Advanced Mode

Advanced Mode preserves the professional research platform. It exposes:

- Mission Control.
- Research Intelligence.
- Research Command Center.
- Validation diagnostics.
- Candidate lineage.
- Campaign diagnostics.
- Strategy mutations.
- Parameter explorer.
- Regime explorer.
- Audit logs.
- SQL-backed evidence.
- Evidence explorer.
- Deployment diagnostics.
- Full research and simulation tables.

## Current Web Navigation

The main application separates current workflows from legacy/specialist tools.

### Research Pipeline

- `/mission-control` - platform state, campaign state, forward readiness, diagnostics, and safety status.
- `/dashboard` - research overview.
- `/research-intelligence` - ranked stored evidence and candidate intelligence.
- `/research` - deterministic campaign and research execution.
- `/experiments` - strategy experiment definitions and history.
- `/validation` - validation diagnostics and saved validation runs.
- `/reports` - saved research reports.

### Forward Validation

- `/paper` - candidate-linked forward-validation overview.
- `/paper/orders` - simulated order lifecycle.
- `/paper/positions` - simulated long-only exposure.
- `/paper/portfolio` - simulation-only account state.
- `/paper/deployments` - candidate deployment controls and diagnostics.

### Research Archives and Specialist Tools

- `/strategy-discovery` - deterministic strategy generation and mutation history.
- `/promising` - cross-asset promising candidate ranking.
- `/portfolio` - candidate lifecycle and research portfolio.
- `/hypotheses` - research hypothesis workflow.
- `/backtest` - strategy replay and evidence explorer.
- `/market-intelligence` - regimes and drift.
- `/alpha` - alpha discovery and candidate generation.

### System

- `/settings` - scheduler and workspace controls.
- `/journal` - research timeline and activity.
- `/assets` and `/assets/[symbol]` - data coverage and candle diagnostics.
- `/copilot` - read-only Ask Kef assistant.

The root route redirects to Mission Control.

## Backend API Areas

Core API areas:

- Health: `/health`
- Market data: `/data/sync`, `/candles/{symbol}`
- Features and regimes: `/features/sync`, `/regimes/sync`
- Strategy research: `/research/strategies`
- Research command center: `/research/command-center`
- Strategy experiments: `/research/strategy-experiments`
- Strategy discovery: `/research/strategy-discovery/*`
- Research automation and campaigns: `/research/campaigns/*`
- Promising candidates: `/research/promising-candidates`
- Research portfolio: `/research/portfolio`
- Alpha discovery: `/alpha/discover`
- Alpha validation: `/alpha/validate`, `/alpha/validation-runs`
- Research lab: `/research/hypotheses`, `/research/journal`
- Research intelligence: `/research/intelligence`, `/research/archive`, `/research/timeline`
- Copilot: `/research/copilot`
- Mission Control: `/paper/mission-control`
- Paper simulation accounts: `/paper/accounts`
- Paper orders and fills: `/paper/orders`, `/paper/accounts/{account_id}/orders`, `/paper/accounts/{account_id}/fills`
- Strategy deployments: `/paper/deployments`
- Deployment management: `/paper/deployment-management`
- Paper scheduler: `/paper/scheduler`
- Forward alerts and reviews: `/paper/alerts`, `/paper/signal-reviews`

## Candidate-Linked Forward Validation

Candidate-linked forward validation is different from legacy paper simulation.

Eligible forward evidence must:

- Come from an elite candidate deployment.
- Be linked to a candidate ID.
- Be simulation-only.
- Occur after the candidate forward-validation start timestamp.
- Produce closed forward trades before expectancy and profit factor can be evaluated.
- Pass the existing Phase 10 readiness gates.

Legacy simulation records remain visible for compatibility and audit history, but they are excluded from eligible Phase 10 forward evidence.

If eligible forward evidence is empty, the platform should report:

- Eligible forward trades: `0`
- Eligible forward profit: unavailable
- Phase 10 evidence: not started

## Paper Trading and Deployment Management

The Paper Trading area now focuses on candidate-linked forward validation instead of the older TSLA-specific simulation workflow.

Current behavior:

- Elite candidate deployments are shown as the primary workflow.
- Legacy simulation tools are separated from candidate-linked evidence.
- Candidate-linked orders, fills, logs, and deployment health are grouped by deployment.
- Scheduler controls remain simulation-only.
- Bulk deployment scans remain simulation-only.
- Broker routing remains disabled.
- Live trading remains disabled.

The deployment-management endpoint returns an aggregate read model containing accounts, deployments, positions, orders, fills, logs, alerts, and per-account snapshots. This prevents the Paper Trading page from issuing many account-specific requests at once and reduces PostgreSQL connection pressure.

## Local Development

1. Copy `.env.example` to `.env`.

2. Start PostgreSQL from the repository root:

```powershell
docker compose up -d postgres
```

3. Install API dependencies into the repository virtual environment:

```powershell
.\.venv\Scripts\python.exe -m pip install -e "apps/api[dev]"
```

If you are already in `apps/api`, use:

```powershell
..\..\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

4. Start the API from `apps/api` with the repository virtual environment:

```powershell
cd apps/api
..\..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Using the repository virtual environment avoids accidentally loading incompatible global Python packages.

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
..\..\.venv\Scripts\python.exe -m pytest
```

Compile backend modules:

```powershell
cd apps/api
..\..\.venv\Scripts\python.exe -m compileall app
```

Build the web app:

```powershell
cd apps/web
npm run build
```

Check PostgreSQL sessions:

```powershell
docker exec keftrade-postgres psql -U keftrade -d keftrade -c "select state, usename, application_name, client_addr, count(*) from pg_stat_activity group by 1,2,3,4 order by count(*) desc;"
```

## Default Development Settings

- API: `http://127.0.0.1:8000`
- Web: `http://127.0.0.1:3000`
- Fallback web port: `http://127.0.0.1:3001`
- PostgreSQL: `127.0.0.1:5432`
- Crypto development provider: `binance_dev`
- Equity research provider: `yfinance_research`
- Default crypto symbol/timeframe: `BTCUSDT` / `4h`
- Current primary elite research asset/timeframe: `AAPL` / `1h`

## Operational Notes

- Do not start Phase 10 until candidate-linked forward evidence passes the existing readiness gates.
- Do not lower validation thresholds to promote candidates.
- Do not change strategies or parameters during forward validation.
- Do not mix legacy simulation profit with eligible candidate-linked forward performance.
- Do not classify evidence-rejected research jobs as operational failures.
- Keep broker and live-trading code disabled unless a future phase explicitly changes that boundary.
- Use aggregate read models for UI pages that need multi-account paper state.

## Recent Fixes and Additions

Recent work added and fixed:

- Phase 9.12 elite candidate persistence.
- Six candidate-linked simulation deployments for elite candidates.
- Candidate-linked orders and fills.
- Forward-validation eligibility audits.
- Phase 10 readiness summaries.
- Evidence drift tracking for elite candidates.
- Mission Control Simple and Advanced modes.
- Accurate Simple Mode campaign and candidate totals.
- Research Command Center data model and UI.
- Paper Trading redesign around candidate-linked forward validation.
- Separation of legacy simulation tools from current forward-validation workflow.
- Improved professional navigation grouping.
- Aggregate deployment-management read model for Paper Trading.
- Reduced PostgreSQL connection spikes from the Paper Trading page.
- Correct API startup guidance using the repository virtual environment.
- Additional invariant and lifecycle tests around campaign/candidate totals and forward-validation behavior.

## Engineering Priorities

Near-term engineering priorities:

- Continue collecting eligible candidate-linked forward-validation evidence.
- Keep Phase 10 locked until readiness gates pass.
- Preserve deterministic campaign behavior.
- Keep Simple Mode and Advanced Mode aligned to the same authoritative data.
- Expand aggregate read models where UI pages need multi-table state.
- Maintain strong tests around candidate lifecycle totals, campaign summaries, forward evidence, and safety boundaries.
- Review security and broker-boundary controls before any future broker-connected phase.

