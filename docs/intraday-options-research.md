# Intraday options research side channel

This adds Alpaca option-chain snapshots as a point-in-time research input.

Important limitation: the chain endpoint is a latest snapshot. It is useful from the moment we start collecting it. It does not reconstruct old option surfaces for historical 2024/2025 decisions. Historical option bars/trades are a separate backfill stage.

## Ingest latest option chains

```bash
cd /opt/keftrade/deploy/production

SYMBOLS="AAPL,ABBV,ABT,ADBE,AMAT,AMD,AMZN,AVGO,BA,BAC,BMY,C,CAT,CMCSA,COIN,COST,CRM,CSCO,CVX,DIS,F,GE,GM,GOOGL,GS,HD,IBM,INTC,JPM,KO,LLY,META,MRK,MSFT,MU,NFLX,NVDA,ORCL,PEP,PFE,QQQ,SPY,T,TSLA,UNH,VZ,WMT,XOM"

docker compose -f docker-compose.prod.yml run --rm api \
  python -m app.cli.intraday_options chain-ingest \
  --symbols "$SYMBOLS" \
  --feed opra \
  --verbose
```

If OPRA entitlement errors, rerun with:

```bash
--feed indicative
```

For a recurring collector, keep the symbol list inside the container command so it does not depend on a host shell variable:

```bash
docker compose -f docker-compose.prod.yml run -d \
  --name alpaca-options-chain-opra \
  api sh -c 'while true; do python -m app.cli.intraday_options chain-ingest --symbols "AAPL,ABBV,ABT,ADBE,AMAT,AMD,AMZN,AVGO,BA,BAC,BMY,C,CAT,CMCSA,COIN,COST,CRM,CSCO,CVX,DIS,F,GE,GM,GOOGL,GS,HD,IBM,INTC,JPM,KO,LLY,META,MRK,MSFT,MU,NFLX,NVDA,ORCL,PEP,PFE,QQQ,SPY,T,TSLA,UNH,VZ,WMT,XOM" --feed opra --verbose; sleep 300; done'
```

## Check coverage

```bash
docker compose -f docker-compose.prod.yml run --rm api \
  python -m app.cli.intraday_options coverage \
  --symbols "$SYMBOLS" \
  --feed opra \
  --start 2026-08-14T00:00:00+00:00 \
  --end 2026-08-15T00:00:00+00:00
```

## Materialize options features for a dataset

```bash
docker compose -f docker-compose.prod.yml run --rm api \
  python -m app.cli.intraday_options features \
  --dataset-id 83 \
  --timeframe 15m \
  --symbols "$SYMBOLS" \
  --feed opra
```

## Declare event discovery with options enabled

```bash
docker compose -f docker-compose.prod.yml run --rm api \
  python -m app.cli.intraday_event_discovery declare \
  --dataset-id 83 \
  --timeframe 15m \
  --branches alpha_ceiling \
  --symbols "$SYMBOLS" \
  --cost-calibration-id 4 \
  --feed sip \
  --include-news-features \
  --include-options-features \
  --options-feed opra \
  --purpose "15m alpha-ceiling with point-in-time news and option-chain surface features"
```

Then run the returned declaration id:

```bash
docker compose -f docker-compose.prod.yml run -d \
  --name alpha-ceiling-15m-options \
  api python -m app.cli.intraday_event_discovery discover \
  --declaration-id ID_HERE
```
