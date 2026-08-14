# KefTrade codebase and database reference

Last updated: 2026-08-14

This document is a practical map of the KefTrade repository, runtime services, database schema, and research/trading workflows. It is written for operating and extending the system, not as marketing documentation.

## 1. System purpose

KefTrade is a research-first quantitative strategy platform. Its core design rule is:

> Strategies should advance only when stored evidence supports promotion.

The platform supports:

- historical market-data ingestion;
- feature generation;
- strategy research and backtesting;
- reproducible research campaigns;
- candidate lifecycle management;
- champion/elite validation;
- broker synchronization with Alpaca Paper;
- paper-lab experiments for fake-money curiosity tests;
- intraday order-flow, news, options, and microstructure research.

The platform is not designed to trade real capital. Broker integrations and order submission are intentionally gated. The current practical trading integration is Alpaca Paper only.

## 2. Repository layout

```text
keftrade/
├── apps/
│   ├── api/                 FastAPI backend, services, CLIs, workers, tests
│   └── web/                 Next.js frontend
├── database/
│   ├── migrations/          Ordered PostgreSQL schema migrations
│   └── reset_runtime_data.sql
├── deploy/
│   └── production/          VPS docker-compose production stack and nginx config
├── docs/                    Architecture and research documentation
├── reports/                 Generated/research phase reports and evidence
├── services/                Auxiliary service folder, if used by later phases
├── docker-compose.yml       Local development Postgres only
└── README.md                Project overview
```

## 3. Runtime architecture

### 3.1 Backend

Backend entry point:

```text
apps/api/app/main.py
```

Technology:

- FastAPI;
- psycopg 3;
- Pydantic settings;
- pandas/numpy for research;
- Alpaca, Binance US, yfinance providers.

Startup behavior:

- configures structured logging;
- verifies database connectivity;
- does not run migrations from API startup in production;
- registers API routers;
- starts the internal paper scheduler service;
- exposes `/health`.

Important point: production schema changes are owned by the `migrate` Docker service, not by API startup.

### 3.2 Frontend

Frontend root:

```text
apps/web/
```

Technology:

- Next.js 15;
- React 19;
- TypeScript;
- Recharts;
- server-rendered pages using API helpers in `apps/web/lib/`.

Main pages:

```text
apps/web/app/page.tsx
apps/web/app/paper/page.tsx
apps/web/app/intraday-research/page.tsx
apps/web/app/elite-builder/page.tsx
apps/web/app/research/page.tsx
apps/web/app/research-intelligence/page.tsx
apps/web/app/mission-control/page.tsx
apps/web/app/diagnostics/page.tsx
```

The current Alpaca Paper Lab UI is:

```text
apps/web/app/paper/page.tsx
apps/web/components/PaperLabDashboard.tsx
apps/web/lib/api.ts
```

It calls:

```text
GET /intraday-paper-lab/experiments
GET /intraday-paper-lab/experiments/{experiment_id}
```

### 3.3 Database

Database:

```text
PostgreSQL 16
```

All schema changes live in:

```text
database/migrations/*.sql
```

Migrations are ordered by filename and applied by production compose’s `migrate` service.

### 3.4 Production stack

Production compose:

```text
deploy/production/docker-compose.prod.yml
```

Services:

| Service | Purpose |
|---|---|
| `postgres` | PostgreSQL database |
| `migrate` | Applies `database/migrations/*.sql` |
| `api` | FastAPI application |
| `worker` | Research campaign worker |
| `signal-diagnostics-worker` | Signal diagnostics worker |
| `broker-worker` | Broker sync/reconciliation worker |
| `nginx` | HTTP/HTTPS reverse proxy |
| `certbot` | TLS certificate helper profile |

Useful production commands:

```bash
cd /opt/keftrade/deploy/production

docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail 100 api
docker compose -f docker-compose.prod.yml build api
docker compose -f docker-compose.prod.yml up -d api
```

## 4. Configuration

Settings live in:

```text
apps/api/app/settings.py
```

Important environment variables:

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `ALPACA_API_KEY`, `ALPACA_API_SECRET` | Alpaca market data credentials |
| `ALPACA_PAPER_API_KEY`, `ALPACA_PAPER_SECRET_KEY` | Alpaca Paper trading credentials |
| `ALPACA_PAPER_BASE_URL` | Must be `https://paper-api.alpaca.markets` for paper submission |
| `BROKER_SYNC_ENABLED` | Enables broker sync worker behavior |
| `BROKER_RECONCILIATION_ENABLED` | Enables broker reconciliation |
| `BROKER_ORDER_SUBMISSION_ENABLED` | Global guard for broker order submission |
| `EXTERNAL_PAPER_EXECUTION_ENABLED` | External paper execution gate |
| `AUTO_ENABLE_READY_PAPER_EXECUTION` | Optional automation flag for paper deployment |
| `KEFTRADE_MAX_CAMPAIGN_WORKERS` | Campaign worker concurrency cap |
| `KEFTRADE_CAMPAIGN_WORKER_*` | Worker heartbeat/staleness/nice settings |
| `KEFTRADE_BROKER_ALLOCATED_CAPITAL` | Broker risk sizing base |
| `KEFTRADE_MAX_RISK_PER_TRADE_PCT` | Max risk per trade |
| `KEFTRADE_MAX_TOTAL_EXPOSURE_PCT` | Total exposure cap |
| `KEFTRADE_DAILY_LOSS_LIMIT_PCT` | Daily loss guard |
| `KEFTRADE_WEEKLY_LOSS_LIMIT_PCT` | Weekly loss guard |

## 5. Backend modules

### 5.1 Providers

Provider code:

```text
apps/api/app/providers/
├── alpaca.py
├── binance.py
├── registry.py
└── yfinance_provider.py
```

Responsibilities:

- normalize external market-data providers;
- fetch candles/assets/market data;
- support research ingestion jobs.

### 5.2 Broker integration

Broker code:

```text
apps/api/app/brokers/
apps/api/app/services/broker_sync.py
apps/api/app/services/broker_reconciliation.py
apps/api/app/services/broker_read_models.py
apps/api/app/workers/broker_runner.py
```

Key broker tables:

```text
broker_accounts
broker_sync_runs
broker_orders
broker_fills
broker_positions
broker_account_state
broker_clock_state
broker_reconciliation_runs
broker_reconciliation_findings
broker_audit_events
```

The broker worker synchronizes Alpaca Paper state into durable tables. Paper-lab P/L depends on `broker_orders` fill prices being synced or inserted.

### 5.3 Research and campaigns

Campaign/research services:

```text
apps/api/app/services/research_campaigns.py
apps/api/app/services/research_automation.py
apps/api/app/services/research_learning.py
apps/api/app/services/research_command_center.py
apps/api/app/services/strategy_discovery.py
apps/api/app/services/strategy_families.py
apps/api/app/workers/campaign_runner.py
```

Core concepts:

- universe;
- campaign;
- campaign job;
- candidate;
- champion;
- elite;
- portfolio;
- paper deployment.

The system records rejection evidence rather than silently discarding failures.

### 5.4 Intraday research

Intraday code is split between top-level services and `labs/intraday/`.

Core services:

```text
apps/api/app/services/intraday_candle_ingest.py
apps/api/app/services/intraday_trade_flow_ingest.py
apps/api/app/services/intraday_trade_flow.py
apps/api/app/services/intraday_dataset_quality.py
apps/api/app/services/intraday_factor_diagnostics.py
apps/api/app/services/intraday_event_discovery.py
apps/api/app/services/intraday_execution_costs.py
apps/api/app/services/intraday_options.py
apps/api/app/services/intraday_news.py
apps/api/app/services/intraday_paper_lab.py
```

Intraday research is explicitly gated:

- dataset snapshot;
- quality report;
- certification;
- declaration;
- discovery;
- optional confirmation;
- retirement/ledger tracking.

This is intended to prevent result shopping and hindsight flipping.

### 5.5 Alpaca Paper Lab

Paper lab code:

```text
apps/api/app/services/intraday_paper_lab.py
apps/api/app/cli/intraday_paper_lab.py
apps/api/app/routers/intraday_paper_lab.py
apps/web/components/PaperLabDashboard.tsx
```

Purpose:

- fake-money-only paper curiosity testing;
- separate from elite external paper deployment;
- uses Alpaca Paper order submission only when `--submit --confirm-paper` are both provided;
- records every decision, skip, error, position, entry, and exit.

Important paper-lab tables:

```text
intraday_paper_lab_experiments
intraday_paper_lab_decisions
intraday_paper_lab_positions
broker_orders
```

Supported current lab families:

- signed trade-imbalance continuation;
- gap down absorption reversal;
- gap up absorption reversal;
- gap up acceptance continuation.

Timing behavior:

| Strategy type | First possible decision in PT | Notes |
|---|---:|---|
| signed imbalance | ~7:00 AM PT | Uses completed 30m bars through day |
| gap strategies | ~7:30 AM PT | Opening gap setup only |
| flatten before close | ~1:00 PM PT | End of regular session |

Recent operational fix:

- The options helper originally loaded too many option snapshots into memory.
- `load_option_feature_index()` now loads only the latest option-chain snapshot per symbol in the requested window.
- This prevents paper-lab containers from being killed with Docker exit 137.

## 6. API route groups

Main router files:

| Router | Prefix/topic |
|---|---|
| `symbols.py` | symbol list |
| `data.py` | data sync and candles |
| `features.py` | feature sync and retrieval |
| `regimes.py` | market regimes |
| `signals.py` | signal generation/retrieval |
| `backtests.py` | backtest creation/retrieval |
| `research.py` | broad research command center, campaigns, candidates |
| `research_lab.py` | hypotheses, experiments, journal |
| `intraday_lab.py` | intraday research lab, campaigns, diagnostics |
| `intraday_paper_lab.py` | all-day paper-lab status |
| `elite_portfolios.py` | champion import, validation, portfolio build/activation |
| `paper.py` | internal paper trading/deployments |
| `broker.py` | broker account/orders/positions/readiness |
| `diagnostics.py` | strategy and portfolio diagnostics |
| `risk.py` | risk settings |
| `alpha.py`, `validation.py` | alpha discovery/validation |
| `research_copilot.py` | research copilot |
| `research_intelligence.py` | ranking, timeline, archive |

Key paper-lab endpoints:

```text
GET /intraday-paper-lab/experiments
GET /intraday-paper-lab/experiments/{experiment_id}
```

Key broker endpoints:

```text
GET /broker/status
GET /broker/account
GET /broker/clock
GET /broker/orders
GET /broker/positions
GET /broker/reconciliation
GET /broker/execution-readiness
```

## 7. CLI inventory

Most research/ops work is CLI-driven.

### 7.1 Paper lab

Module:

```bash
python -m app.cli.intraday_paper_lab
```

Commands:

```text
create
create-gap
run-cycle
run-loop
run-gap-cycle
run-gap-loop
flatten
monitor
schedule
```

Common commands:

```bash
# Monitor one experiment
docker compose -f docker-compose.prod.yml run --rm api \
  python -m app.cli.intraday_paper_lab monitor \
  --experiment-id 12

# Run signed-imbalance loop
docker compose -f docker-compose.prod.yml run -d \
  --name signed-imbalance-paper-lab-YYYYMMDD \
  api python -m app.cli.intraday_paper_lab run-loop \
  --experiment-id 12 \
  --poll-seconds 60 \
  --feed sip \
  --submit \
  --confirm-paper

# Run a single missed signed-imbalance 30m cycle
docker compose -f docker-compose.prod.yml run --rm api \
  python -m app.cli.intraday_paper_lab run-cycle \
  --experiment-id 12 \
  --bar-start 2026-08-14T16:00:00+00:00 \
  --feed sip \
  --submit \
  --confirm-paper

# Run a single missed gap cycle
docker compose -f docker-compose.prod.yml run --rm api \
  python -m app.cli.intraday_paper_lab run-gap-cycle \
  --experiment-id 11 \
  --bar-start 2026-08-14T14:00:00+00:00 \
  --feed sip \
  --submit \
  --confirm-paper
```

### 7.2 Intraday dataset pipeline

Module:

```bash
python -m app.cli.intraday_dataset_pipeline
```

Commands:

```text
ingest
universe
snapshot
quality
features
premarket
trade-flow
auto-trade-flow
flow-agreement
sector-flow
```

Used for candles, trade-flow features, snapshots, dataset quality reports, and sector/flow features.

### 7.3 Factor audit

Module:

```bash
python -m app.cli.intraday_factor_audit
```

Commands:

```text
certify
declare
ledger
discover
declare-experiment
declare-order-flow
retire
confirm
```

Used to enforce declaration-before-discovery and prevent uncontrolled post-hoc hypothesis changes.

### 7.4 Event discovery / alpha ceiling

Module:

```bash
python -m app.cli.intraday_event_discovery
```

Commands:

```text
catalog
declare
discover
report
confirm
```

Used for event-conditioned alpha discovery and pre-strategy predictability analysis.

### 7.5 News

Module:

```bash
python -m app.cli.intraday_news
```

Commands:

```text
ingest
coverage
features
```

Backfills Alpaca news articles, links them to symbols, and creates news feature snapshots.

### 7.6 Options

Module:

```bash
python -m app.cli.intraday_options
```

Commands:

```text
chain-ingest
coverage
live-status
features
```

Captures point-in-time option-chain snapshots and computes options helper features:

- option contracts;
- ATM IV;
- put/call IV skew;
- IV term slope;
- put/call volume ratio;
- gamma proxy;
- delta proxy;
- near-ATM spread;
- snapshot age.

### 7.7 Broker

Module:

```bash
python -m app.cli.broker
```

Commands:

```text
sync
reconcile
halt
resume
validate-adapter-compatibility
queue-elite-repair-campaign
```

### 7.8 Other important CLIs

| Module | Purpose |
|---|---|
| `intraday_costs.py` | quote ingestion and execution-cost calibration |
| `intraday_intrabar_helper.py` | 1m microscope diagnostics for 30m/15m setups |
| `intraday_mid_portfolio.py` | mid-tier portfolio candidate screening |
| `trade_imbalance_calibration.py` | signed trade-imbalance calibration and declaration |
| `intraday_strategy_pipeline.py` | sector/strategy simulation/qualification |
| `campaigns.py` | campaign progress/repair |
| `deployments.py` | external paper deployment controls |
| `elites.py` | elite reevaluation |
| `replay.py` | elite shadow replay/outcome refresh |

## 8. Database reference

The database is migration-owned. Do not manually mutate schema in production outside migrations.

### 8.1 Core market data

| Table | Purpose |
|---|---|
| `symbols` | Tradeable/research symbols |
| `raw_api_logs` | Raw provider response audit logs |
| `candles` | Historical OHLCV candles |
| `features` | General feature records |
| `market_regimes` | Regime classifications |
| `intraday_features` | Intraday regular-session features |
| `research_dataset_candles` | Frozen dataset candle rows |
| `research_dataset_intraday_features` | Frozen intraday feature rows |

### 8.2 Strategy/backtest basics

| Table | Purpose |
|---|---|
| `strategy_versions` | Versioned strategy definitions |
| `backtests` | Backtest runs |
| `backtest_trades` | Backtest trade records |
| `signals` | Generated signals |
| `risk_settings` | Internal paper/risk settings |
| `alpha_validation_runs` | Alpha validation results |

### 8.3 Research lab and campaigns

| Table | Purpose |
|---|---|
| `research_hypotheses` | Research hypothesis records |
| `strategy_experiments` | Strategy experiment records |
| `research_journal_entries` | Research journal entries |
| `research_universes` | Research universe definitions |
| `research_campaigns` | Campaign headers |
| `research_campaign_jobs` | Campaign jobs |
| `elite_research_candidates` | Candidate strategy records |
| `research_campaign_workers` | Worker heartbeat/lifecycle |
| `research_campaign_batches` | Batch state |
| `research_campaign_reports` | Campaign report artifacts |
| `research_campaign_archives` | Checksum-verified campaign archives |
| `research_candidate_stage_evidence` | Evidence per candidate stage |
| `research_command_center_snapshots` | Command center snapshots |
| `research_candidate_objects` | Candidate JSON object store |

### 8.4 Research learning/intelligence

| Table | Purpose |
|---|---|
| `research_ranking_snapshots` | Ranking/intelligence snapshots |
| `research_knowledge_versions` | Knowledge base versions |
| `research_failure_patterns` | Stored failure patterns |
| `research_success_patterns` | Stored success patterns |
| `research_recommendations` | Generated recommendations |
| `research_confidence_history` | Confidence history |
| `research_evolution_history` | Evolution history |
| `research_timeline_events` | Timeline events |
| `research_campaign_plans` | Campaign plans |
| `research_copilot_interactions` | Copilot history |

### 8.5 Candidate lifecycle / champion / elite

| Table | Purpose |
|---|---|
| `candidate_lifecycle_events` | Candidate lifecycle audit |
| `research_specialist_threads` | Specialist investigation threads |
| `research_specialist_investigations` | Specialist investigation records |
| `strategy_dna` | Strategy DNA/fingerprint metadata |
| `elite_champion_validation_runs` | Champion validation run headers |
| `elite_champion_validation_gates` | Champion validation gate results |
| `research_champion_state` | Champion state, if created by later migrations |
| `elite_portfolio_runs` | Portfolio solver runs |
| `elite_portfolio_snapshots` | Portfolio snapshots |
| `elite_portfolio_eligibility` | Portfolio eligibility facts |
| `elite_portfolio_correlations` | Pairwise correlation evidence |
| `elite_portfolio_conflicts` | Portfolio conflict records |
| `elite_portfolio_members` | Portfolio selected members |
| `elite_portfolio_activation_attempts` | Activation attempts |
| `elite_candidate_correlation_evidence` | Correlation evidence for candidates |

### 8.6 Internal paper trading

Older/internal paper-trading tables:

| Table | Purpose |
|---|---|
| `paper_accounts` | Internal paper accounts |
| `strategy_deployments` | Internal strategy deployments |
| `paper_orders` | Simulated/internal paper orders |
| `paper_fills` | Simulated/internal paper fills |
| `paper_positions` | Simulated/internal positions |
| `paper_equity_curve` | Equity curve |
| `execution_logs` | Execution logs |
| `paper_scan_scheduler` | Scanner scheduling |
| `paper_closed_trade_evidence` | Closed-trade evidence |

These are separate from the Alpaca Paper Lab and external broker sync system.

### 8.7 External broker / Alpaca Paper

| Table | Purpose |
|---|---|
| `broker_adapter_releases` | Broker adapter version metadata |
| `broker_accounts` | Broker account records |
| `broker_sync_runs` | Broker sync run status |
| `broker_raw_ingest_events` | Raw broker ingest events, partitioned |
| `broker_raw_archive_manifests` | Broker raw archive metadata |
| `broker_daily_summaries` | Daily broker summaries |
| `broker_account_state` | Latest account state |
| `broker_clock_state` | Latest clock state |
| `broker_account_snapshots` | Historical account snapshots |
| `broker_clock_snapshots` | Historical clock snapshots |
| `broker_orders` | Synced/submitted broker orders |
| `broker_fills` | Broker fills |
| `broker_positions` | Current broker positions |
| `broker_position_snapshots` | Position snapshots |
| `broker_reconciliation_runs` | Reconciliation run headers |
| `broker_reconciliation_findings` | Reconciliation findings |
| `broker_audit_events` | Broker audit trail |

### 8.8 External paper deployment governance

| Table | Purpose |
|---|---|
| `risk_policy_versions` | Versioned risk policies |
| `eligibility_policy_versions` | Eligibility policy versions |
| `deployment_configuration_versions` | Deployment config versions |
| `external_paper_deployments` | External paper deployment records |
| `external_execution_epochs` | Execution epochs |
| `external_deployment_transitions` | Deployment transition audit |
| `adapter_compatibility_validations` | Adapter compatibility validation |
| `eligibility_decisions` | Eligibility decision log |
| `execution_risk_decisions` | Risk decision log |
| `external_execution_signals` | External execution signal records |
| `proposed_broker_orders` | Proposed broker orders before/without execution |
| `shadow_executions` | Shadow execution records |
| `execution_halts` | Execution halt records |
| `external_paper_closed_trade_evidence` | External paper closed trade evidence |
| `strategy_evaluations` | Strategy evaluation records |
| `model_risk_decisions` | Model risk decisions |
| `portfolio_risk_decisions` | Portfolio risk decisions |
| `broker_execution_attempts` | Broker execution attempts |

### 8.9 Intraday executable research and cost evidence

| Table | Purpose |
|---|---|
| `intraday_quote_snapshots` | Historical quote snapshots; raw spread/liquidity evidence |
| `intraday_execution_cost_calibrations` | Persisted cost calibration outputs |
| `intraday_quote_ingestion_checkpoints` | Quote ingest checkpoints |
| `intraday_executable_candidates` | Intraday executable candidates |
| `intraday_executable_runs` | Executable research run headers |
| `intraday_family_activations` | Family activation records |
| `intraday_paper_fill_observations` | Paper fill observations |

Operational note:

- If `intraday_quote_snapshots` is truncated, existing `intraday_execution_cost_calibrations` still preserve prior cost outputs.
- Recomputing those calibrations from raw quote evidence requires the raw quote snapshots or a backup.

### 8.10 Intraday research governance

| Table | Purpose |
|---|---|
| `intraday_factor_diagnostic_runs` | Factor diagnostic/discovery results |
| `intraday_research_trials` | Trial ledger |
| `intraday_research_trial_declarations` | Locked declarations |
| `intraday_research_trial_declaration_uses` | Declaration-use audit |
| `intraday_research_certifications` | Dataset/research certifications |
| `intraday_dataset_quality_reports` | Dataset quality reports |
| `intraday_research_hypotheses` | Intraday hypotheses |
| `intraday_retired_factor_versions` | Retired factor versions |
| `intraday_strategy_families` | Intraday strategy family registry |
| `intraday_fill_calibrations` | Fill calibration records |
| `intraday_elite_qualifications` | Elite qualification records |
| `research_dataset_splits` | Train/validation/holdout split definitions |
| `research_split_access_log` | Split access audit |
| `research_confirmation_runs` | Confirmation runs |
| `research_family_graveyard` | Failed/retired families |

### 8.11 Intraday order-flow / trade-flow data

| Table | Purpose |
|---|---|
| `intraday_premarket_features` | Premarket features |
| `intraday_trade_flow_features` | Trade-flow features at timeframe level |
| `intraday_trade_ingest_checkpoints` | Trade ingest checkpoint state |
| `research_dataset_premarket_features` | Frozen premarket features in datasets |
| `research_dataset_trade_flow_features` | Frozen trade-flow features in datasets |
| `intraday_trade_imbalance_calibrations` | Signed trade-imbalance calibrations |
| `intraday_trade_imbalance_calibration_rows` | Calibration detail rows |
| `intraday_microstructure_features` | Intraday microstructure features |
| `intraday_auction_imbalances` | Auction imbalance observations |
| `research_point_in_time_universe_membership` | Universe membership by point in time |
| `research_corporate_actions` | Corporate action research data |

### 8.12 Sector and simulation

| Table | Purpose |
|---|---|
| `symbol_sector_provenance` | Sector assignment provenance |
| `intraday_strategy_simulations` | Intraday strategy simulation output |

### 8.13 Paper Lab

| Table | Purpose |
|---|---|
| `intraday_paper_lab_experiments` | Paper lab experiment headers |
| `intraday_paper_lab_decisions` | Every enter/skip/exit/flatten/error decision |
| `intraday_paper_lab_positions` | Paper lab position lifecycle |

Important relationships:

```text
intraday_paper_lab_positions.entry_client_order_id → broker_orders.client_order_id
intraday_paper_lab_positions.exit_client_order_id  → broker_orders.client_order_id
```

Paper-lab realized P/L is computed by joining positions to filled entry/exit broker orders.

### 8.14 Intrabar, event, news, options

| Table | Purpose |
|---|---|
| `intraday_intrabar_diagnostic_runs` | 1m microscope diagnostic outputs |
| `intraday_event_study_declarations` | Event-study declarations |
| `intraday_event_study_runs` | Event-study run headers |
| `intraday_event_study_events` | Event-level rows |
| `intraday_event_confirmation_runs` | Event confirmation runs |
| `intraday_news_articles` | Alpaca news articles/symbol versions |
| `intraday_news_ingest_checkpoints` | News ingest checkpoints |
| `intraday_news_feature_snapshots` | News feature snapshots |
| `intraday_option_chain_snapshots` | Point-in-time option-chain snapshots |
| `intraday_option_ingest_checkpoints` | Option ingest checkpoints |
| `intraday_option_feature_snapshots` | Option feature snapshots |

## 9. Important operational workflows

### 9.1 Deploy/pull/update production

```bash
cd /opt/keftrade/deploy/production

git pull
docker compose -f docker-compose.prod.yml build api
docker compose -f docker-compose.prod.yml up -d api worker signal-diagnostics-worker broker-worker
```

If only CLI-run containers are needed, rebuilding `api` is enough for future `docker compose run ... api ...` commands.

### 9.2 Check production health

```bash
cd /opt/keftrade/deploy/production

docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml exec postgres pg_isready -U keftrade -d keftrade
curl -fsS http://127.0.0.1/health
```

### 9.3 Check paper lab status

```bash
docker compose -f docker-compose.prod.yml run --rm api \
  python -m app.cli.intraday_paper_lab monitor \
  --experiment-id 12
```

All experiments:

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U keftrade -d keftrade -P pager=off -c "
SELECT
    e.id,
    e.factor_key,
    e.status,
    COUNT(d.*) AS decisions,
    COUNT(*) FILTER (WHERE d.action = 'enter') AS enters,
    COUNT(*) FILTER (WHERE d.action = 'exit') AS exits,
    COUNT(*) FILTER (WHERE d.action = 'skip') AS skips,
    COUNT(*) FILTER (WHERE d.action = 'error') AS errors,
    MAX(d.created_at) AS last_decision
FROM intraday_paper_lab_experiments e
LEFT JOIN intraday_paper_lab_decisions d
  ON d.experiment_id = e.id
GROUP BY e.id, e.factor_key, e.status
ORDER BY e.id DESC;
"
```

### 9.4 Check paper-lab P/L by trade

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U keftrade -d keftrade -P pager=off -c "
WITH trades AS (
    SELECT
        p.id,
        p.experiment_id,
        p.symbol,
        p.side,
        p.status,
        p.quantity,
        entry_order.filled_average_price AS entry_price,
        exit_order.filled_average_price AS exit_price,
        entry_order.filled_at AS entry_filled_at,
        exit_order.filled_at AS exit_filled_at,
        CASE
            WHEN entry_order.filled_average_price IS NOT NULL
             AND exit_order.filled_average_price IS NOT NULL
             AND p.side = 'long'
                THEN (exit_order.filled_average_price - entry_order.filled_average_price) * p.quantity
            WHEN entry_order.filled_average_price IS NOT NULL
             AND exit_order.filled_average_price IS NOT NULL
             AND p.side = 'short'
                THEN (entry_order.filled_average_price - exit_order.filled_average_price) * p.quantity
            ELSE NULL
        END AS realized_pnl
    FROM intraday_paper_lab_positions p
    LEFT JOIN broker_orders entry_order
      ON entry_order.client_order_id = p.entry_client_order_id
    LEFT JOIN broker_orders exit_order
      ON exit_order.client_order_id = p.exit_client_order_id
    WHERE p.experiment_id = 12
)
SELECT
    symbol,
    side,
    status,
    quantity,
    entry_price,
    exit_price,
    ROUND(realized_pnl::numeric, 4) AS pnl,
    entry_filled_at,
    exit_filled_at
FROM trades
ORDER BY realized_pnl ASC NULLS LAST;
"
```

### 9.5 Check options live status

```bash
SYMBOLS="AAPL,ABBV,ABT,ADBE,AMAT,AMD,AMZN,AVGO,BA,BAC,BMY,C,CAT,CMCSA,COIN,COST,CRM,CSCO,CVX,DIS,F,GE,GM,GOOGL,GS,HD,IBM,INTC,JPM,KO,LLY,META,MRK,MSFT,MU,NFLX,NVDA,ORCL,PEP,PFE,QQQ,SPY,T,TSLA,UNH,VZ,WMT,XOM"

docker compose -f docker-compose.prod.yml run --rm api \
  python -m app.cli.intraday_options live-status \
  --symbols "$SYMBOLS" \
  --feed opra \
  --fresh-minutes 10
```

### 9.6 Check 15m/30m trade-flow collection progress

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U keftrade -d keftrade -P pager=off -c "
SELECT
    timeframe,
    COUNT(*) AS rows,
    COUNT(DISTINCT symbol) AS symbols,
    COUNT(DISTINCT (timestamp AT TIME ZONE 'America/New_York')::date) AS sessions,
    MIN(timestamp) AS first_bar,
    MAX(timestamp) AS last_bar
FROM intraday_trade_flow_features
WHERE feed = 'sip'
GROUP BY timeframe
ORDER BY timeframe;
"
```

Checkpoint summary:

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U keftrade -d keftrade -P pager=off -c "
SELECT
    timeframe,
    COUNT(*) FILTER (WHERE status = 'completed') AS completed_symbol_sessions,
    COUNT(*) FILTER (WHERE status = 'failed') AS failed,
    COUNT(*) FILTER (WHERE status = 'running') AS running,
    COUNT(DISTINCT session_date) FILTER (WHERE status = 'completed') AS completed_sessions,
    COUNT(DISTINCT symbol) FILTER (WHERE status = 'completed') AS completed_symbols,
    MIN(session_date) FILTER (WHERE status = 'completed') AS first_session,
    MAX(session_date) FILTER (WHERE status = 'completed') AS last_session,
    MAX(updated_at) AS last_progress
FROM intraday_trade_ingest_checkpoints
WHERE feed = 'sip'
  AND ingest_version = 'intraday_trade_flow_v2_calibration_moments'
GROUP BY timeframe
ORDER BY timeframe;
"
```

## 10. Current known caveats

### 10.1 Docker `unhealthy` is not always failure for CLI containers

Many `docker compose run ... api python -m ...` containers are CLI processes, not HTTP servers. They may show `health: starting` or `unhealthy` because they do not expose `/health`. For CLI containers, use:

```bash
docker ps -a --filter name=<container>
docker logs --tail 200 <container>
docker stats --no-stream <container> production-postgres-1
```

### 10.2 Exit 137 is a hard kill

Docker exit 137 means SIGKILL. Common causes:

- out-of-memory kill;
- manual/host kill;
- Docker daemon kill.

Check:

```bash
docker inspect <container> \
  --format 'ExitCode={{.State.ExitCode}} OOMKilled={{.State.OOMKilled}} Error={{.State.Error}} FinishedAt={{.State.FinishedAt}}'
```

### 10.3 Paper-lab scheduler can miss opening strategies if it starts late

Gap strategies evaluate the opening decision bar:

```text
10:00–10:30 AM ET
7:00–7:30 AM PT
bar_start UTC = 14:00 during daylight time
```

If the scheduler is dead at that time, gap strategies can be backfilled manually with `run-gap-cycle`, but late real-time entry is not equivalent to the clean strategy.

### 10.4 One-share sizing distorts P/L

Paper lab currently uses 1 share per trade. High-price symbols like LLY, COST, AVGO, GS, QQQ, and SPY dominate dollar P/L. For realistic small-account simulation, fractional/dollar-based sizing is more meaningful.

### 10.5 Research result status

Many tested intraday strategies are exploratory or failed strict gates. Paper-lab trading is curiosity/fake-money observation, not validation that a strategy has deployable edge.

## 11. Testing

API tests live in:

```text
apps/api/tests/
```

Run focused tests locally from `apps/api` or repo root depending on environment:

```bash
python -m pytest apps/api/tests/test_intraday_options.py
python -m pytest apps/api/tests/test_intraday_paper_lab.py
python -m pytest apps/api/tests/test_intraday_event_discovery.py
```

If pytest is unavailable, at least syntax-check changed modules:

```bash
python -m py_compile apps/api/app/services/intraday_options.py
python -m py_compile apps/api/app/services/intraday_paper_lab.py
```

Frontend:

```bash
cd apps/web
npm run build
npm test
```

## 12. Suggested next documentation improvements

This reference is a map of the whole system. The next useful layer would be:

1. an entity-relationship diagram for the database;
2. a paper-lab runbook with exact daily premarket/start/monitor/flatten commands;
3. an intraday research runbook covering dataset → quality → certification → declaration → discovery → confirmation;
4. a migration-by-migration changelog;
5. a “safe production operations” checklist.

