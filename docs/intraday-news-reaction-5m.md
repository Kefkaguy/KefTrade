# Governed 5m News-Reaction Study

This study tests whether **five-minute resolution adds value because it resolves the market response to a fresh information event**, not because it creates more rows.

## Frozen thesis

- News is the event trigger, using the earliest stored point-in-time version for each provider/article/symbol.
- SPY and QQQ are context assets, not news targets.
- Require at least 60 minutes since the previous article for that symbol.
- Align to the first whole frozen 1m bar at or after `known_at`.
- The next five complete frozen minutes form the reaction window.
- Decision is after those five minutes are fully known.
- Entry is the open of the next 1m bar.
- Outcomes are +5m, +10m, +15m, and +30m.
- Price evidence comes only from immutable `research_dataset_candles`.
- V1 deliberately does not use 1m signed trade flow because the development window does not contain a frozen 1m flow history.

## Four predeclared states

Exactly four states are tested:

1. positive news + positive stock-minus-SPY 5m reaction -> continuation long;
2. positive news + nonpositive residual reaction -> reversal short;
3. negative news + negative residual reaction -> continuation short;
4. negative news + nonnegative residual reaction -> reversal long.

News categories (earnings, guidance, analyst, M&A, regulatory, product, management, legal) are reported as descriptive labels only. They are not separate tests in V1.

Four states x four horizons = **16 fresh tests**.

Neutral/tied keyword polarity is excluded rather than optimized after seeing returns.

## Governance and promotion

`preflight` is structurally return-blind: it counts timestamps and verifies the presence of the required 35-minute frozen grid without selecting forward OHLC fields.

`declare` freezes the event rules, state definitions, horizons, keyword terms, cost calibration, split boundaries, point-in-time news fingerprint, prior effective-trial count, and the resulting selection-adjusted t-stat threshold.

`discover` can read discovery and validation only and can be executed once per declaration.

A cell is eligible for confirmation only when all of the following hold in both development phases where applicable:

- gross 95% moving-block-bootstrap lower bound >= 5 bps;
- net 95% moving-block-bootstrap lower bound > 0 after stressed costs;
- validation day-clustered net t-stat clears the cumulative Bonferroni threshold;
- session and symbol concentration gates pass.

A high point estimate alone never promotes a cell.

`confirm` is refused when discovery produced no promoted cells and can be executed only once for a qualifying discovery run.

## Commands

```bash
cd /opt/keftrade/deploy/production

# Return-blind event-supply check.
docker compose -f docker-compose.prod.yml run --rm --no-deps api \
  python -m app.cli.intraday_news_reaction preflight \
  --dataset-id 86

# Example declaration.  The prior-trial count must be supplied explicitly from
# the cumulative research ledger; do not silently reset it for this family.
docker compose -f docker-compose.prod.yml run --rm --no-deps api \
  python -m app.cli.intraday_news_reaction declare \
  --dataset-id 86 \
  --cost-calibration-id 4 \
  --prior-effective-trials 494 \
  --purpose "5m event-aligned point-in-time company-news reaction study; price/news only; 4 states x 4 horizons"

# Use the declaration id returned above exactly once.
docker compose -f docker-compose.prod.yml run --rm --no-deps api \
  python -m app.cli.intraday_news_reaction discover \
  --declaration-id DECLARATION_ID

# Reprint persisted evidence without spending another run.
docker compose -f docker-compose.prod.yml run --rm --no-deps api \
  python -m app.cli.intraday_news_reaction report \
  --run-id RUN_ID
```

Do **not** run `confirm` merely to see another number. Run it only if the persisted discovery report contains one or more `candidate_cells` that cleared every frozen development gate.

## Stop rule

If none of the 16 cells clears the 5-bp gross lower-bound hurdle with positive stressed-cost net evidence and adequate clustered support, retire this price/news-only 5m family. Do not respond by adding categories, thresholds, or more candle permutations on the same development history. The next justified information-set expansion would be a separately frozen richer channel such as 1m flow/quote-derived microstructure.
