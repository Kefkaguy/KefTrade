# Intraday News Research Side Channel

This layer adds Alpaca historical news as a point-in-time research input.
It does not authorize broker action.

## What It Stores

`intraday_news_articles` stores one row per provider/article/symbol/known-at
version.  `known_at` is the timestamp research is allowed to use.  For Alpaca
historical news, the implementation uses the article `updated_at` timestamp as
`known_at`, so a 10:05 decision cannot see a 10:17 revision.

`intraday_news_feature_snapshots` can materialize point-in-time feature values
for a dataset/timeframe.  Event discovery can also compute those features
directly from raw news.

## Ingest Alpaca News

```bash
cd /opt/keftrade/deploy/production

SYMBOLS="AAPL,ABBV,ABT,ADBE,AMAT,AMD,AMZN,AVGO,BA,BAC,BMY,C,CAT,CMCSA,COIN,COST,CRM,CSCO,CVX,DIS,F,GE,GM,GOOGL,GS,HD,IBM,INTC,JPM,KO,LLY,META,MRK,MSFT,MU,NFLX,NVDA,ORCL,PEP,PFE,QQQ,SPY,T,TSLA,UNH,VZ,WMT,XOM"

docker compose -f docker-compose.prod.yml run -d \
  --name alpaca-news-2024-2026 \
  api python -m app.cli.intraday_news ingest \
  --symbols "$SYMBOLS" \
  --start 2024-04-01T00:00:00+00:00 \
  --end 2026-03-31T23:59:59+00:00 \
  --include-content \
  --verbose
```

Track it:

```bash
docker ps -a --filter name=alpaca-news-2024-2026
docker logs -f alpaca-news-2024-2026
```

Check coverage:

```bash
docker compose -f docker-compose.prod.yml run --rm --no-deps api \
  python -m app.cli.intraday_news coverage \
  --symbols "$SYMBOLS" \
  --start 2024-04-01T00:00:00+00:00 \
  --end 2026-03-31T23:59:59+00:00
```

## Optional Feature Materialization

```bash
docker compose -f docker-compose.prod.yml run --rm --no-deps api \
  python -m app.cli.intraday_news features \
  --dataset-id 83 \
  --timeframe 15m \
  --symbols "$SYMBOLS"
```

## Run BASE + NEWS Alpha Ceiling

Create a separate declaration from the baseline by adding
`--include-news-features`:

```bash
docker compose -f docker-compose.prod.yml run --rm --no-deps api \
  python -m app.cli.intraday_event_discovery declare \
  --dataset-id 83 \
  --timeframe 15m \
  --branches alpha_ceiling \
  --symbols "$SYMBOLS" \
  --cost-calibration-id 4 \
  --feed sip \
  --include-news-features \
  --purpose "15m alpha ceiling with point-in-time Alpaca news features"
```

Then run discovery using the returned declaration id:

```bash
docker compose -f docker-compose.prod.yml run -d \
  --name alpha-ceiling-15m-news \
  api python -m app.cli.intraday_event_discovery discover \
  --declaration-id ID_HERE
```

Interpretation is unchanged: do not confirm unless validation has positive net
expectancy, meaningful clustered evidence, and a materially better
predicted-vs-realized relationship than the no-news baseline.
