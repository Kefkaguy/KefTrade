# Event-conditioned alpha discovery

This backend layer researches conditional expectancy before a trading strategy
exists. It cannot create campaigns, paper experiments, orders, stops, targets,
or broker authorization.

## What it implements

- Mechanical 30m gap-absorption events.
- Mechanical 15m breakout probes and confirmed failed-auction/range-reentry events.
- A 1m veto-only view of otherwise-valid 30m gap-absorption events.
- Features separated into setup, alpha, regime, veto, and execution roles.
- Effort-versus-result variables built from signed SIP flow and price response.
- Symbol/time-slot development percentiles and z-scores, plus market/sector residuals.
- Fixed +15m, +30m, +60m, and +120m gross/net outcomes where the parent bar grid permits them.
- MFE and MAE at every available horizon.
- Score-decile reports, loser/large-MAE veto mining, and stability by year, liquidity, and market regime.
- Benjamini-Hochberg feature diagnostics, Deflated Sharpe inference, and a CSCV/PBO diagnostic.
- Immutable 50% discovery / 30% validation / 20% one-shot confirmation governance.

The 1m branch never predicts direction. It asks whether the frozen parent setup
should be rejected because its completed signal bar shows adverse price, flow,
or liquidity state.

The 1m candles and trade-flow tables are not part of dataset 82 itself. The
declaration therefore records a return-blind count/checksum fingerprint for the
exact 1m predictor window. Discovery and confirmation refuse if that side
channel changes. Finish or intentionally stop the 1m collector before declaring
the veto study; otherwise later ingestion correctly forces a new declaration.

## Deploy

```bash
cd /opt/keftrade/deploy/production

git pull

docker compose -f docker-compose.prod.yml build api worker

docker compose -f docker-compose.prod.yml run --rm migrate
```

If `migrate` is already running from a previous interrupted Compose command,
inspect it before starting another one:

```bash
docker compose -f docker-compose.prod.yml ps migrate
docker logs --tail 120 production-migrate-1
```

## Inspect the frozen catalog

```bash
docker compose -f docker-compose.prod.yml run --rm --no-deps api \
  python -m app.cli.intraday_event_discovery catalog
```

## 30m gap absorption plus 1m veto

Dataset 82 is the full 30m SIP snapshot used in the current research. Replace
cost calibration 4 only if a newer explicit SIP cost calibration has been
created.

```bash
cd /opt/keftrade/deploy/production

SYMBOLS="AAPL,ABBV,ABT,ADBE,AMAT,AMD,AMZN,AVGO,BA,BAC,BMY,C,CAT,CMCSA,COIN,COST,CRM,CSCO,CVX,DIS,F,GE,GM,GOOGL,GS,HD,IBM,INTC,JPM,KO,LLY,META,MRK,MSFT,MU,NFLX,NVDA,ORCL,PEP,PFE,QQQ,SPY,T,TSLA,UNH,VZ,WMT,XOM"

docker compose -f docker-compose.prod.yml run --rm --no-deps api \
  python -m app.cli.intraday_event_discovery declare \
  --dataset-id 82 \
  --timeframe 30m \
  --branches gap_absorption,one_minute_veto \
  --symbols "$SYMBOLS" \
  --cost-calibration-id 4 \
  --feed sip \
  --purpose "30m gap absorption conditional expectancy and 1m adverse-state veto study"
```

The command returns a declaration id. Run development discovery with it:

```bash
docker compose -f docker-compose.prod.yml run --rm --no-deps api \
  python -m app.cli.intraday_event_discovery discover \
  --declaration-id DECLARATION_ID
```

The command prints progress before its long data load and returns a run id. A
concise persisted report is available without re-running the study:

```bash
docker compose -f docker-compose.prod.yml run --rm --no-deps api \
  python -m app.cli.intraday_event_discovery report \
  --run-id RUN_ID
```

## 15m failed-auction inversion study

Dataset 83 is the frozen 15m snapshot already used by the mid-portfolio screen.

```bash
cd /opt/keftrade/deploy/production

SYMBOLS="AAPL,ABBV,ABT,ADBE,AMAT,AMD,AMZN,AVGO,BA,BAC,BMY,C,CAT,CMCSA,COIN,COST,CRM,CSCO,CVX,DIS,F,GE,GM,GOOGL,GS,HD,IBM,INTC,JPM,KO,LLY,META,MRK,MSFT,MU,NFLX,NVDA,ORCL,PEP,PFE,QQQ,SPY,T,TSLA,UNH,VZ,WMT,XOM"

docker compose -f docker-compose.prod.yml run --rm --no-deps api \
  python -m app.cli.intraday_event_discovery declare \
  --dataset-id 83 \
  --timeframe 15m \
  --branches failed_auction \
  --symbols "$SYMBOLS" \
  --cost-calibration-id 4 \
  --feed sip \
  --purpose "15m continuation failure probability and failed-auction conditional expectancy study"
```

Then use the returned declaration id:

```bash
docker compose -f docker-compose.prod.yml run --rm --no-deps api \
  python -m app.cli.intraday_event_discovery discover \
  --declaration-id DECLARATION_ID
```

## Monitor a long development run

Start it detached with a stable container name:

```bash
docker compose -f docker-compose.prod.yml run -d \
  --name event-discovery-30m \
  api python -m app.cli.intraday_event_discovery discover \
  --declaration-id DECLARATION_ID
```

Monitor it:

```bash
docker ps -a --filter name=event-discovery-30m
docker stats --no-stream event-discovery-30m production-postgres-1
docker logs -f event-discovery-30m
```

Check persisted runs:

```bash
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U keftrade -d keftrade -P pager=off -c "
SELECT
    run.id AS run_id,
    declaration.timeframe,
    declaration.branches,
    run.event_count,
    run.effective_trials,
    run.created_at
FROM intraday_event_study_runs run
JOIN intraday_event_study_declarations declaration
  ON declaration.id = run.declaration_id
ORDER BY run.id DESC;
"
```

## One-shot confirmation

Do not run this merely to see another number. Run it only after the development
report has produced a frozen conditional candidate worth testing. Confirmation
spends the untouched final 20% permanently for that run.

```bash
docker compose -f docker-compose.prod.yml run --rm --no-deps api \
  python -m app.cli.intraday_event_discovery confirm \
  --run-id RUN_ID
```

A second confirmation attempt for the same run is refused. Even a passing
confirmation creates no trading strategy; it only permits the next phase,
strategy engineering with separately specified entries, exits, and risk.

## Stopping condition

Stop the branch when conditional score deciles are not monotonic, validation
does not remain net positive, vetoes do not disproportionately remove losers or
large-MAE outcomes, or the one-shot confirmation fails. The layer is designed
to support the conclusion that the current data contains no economically useful
short-horizon edge after costs.
