# Alpha cartography — measuring information before building a strategy

## The problem this fixes

KefTrade's research machinery was mature and its research *ordering* was
backwards. Every intraday module took a strategy as its unit of work — a
factor, a threshold, a direction, a holding period — and asked whether that
strategy was profitable. Choosing a threshold and a holding period is already a
modelling decision, and making it before anyone had measured where the
information lives meant a hypothesis with no forecast in it could still consume
a simulation, a qualification, a cost calibration and a Paper Lab session before
anything reported the one fact that mattered.

Three specific consequences fell out of that ordering:

1. **The horizon was fixed by the data grid, not by the phenomenon.** Signals
   were read from completed 30m bars and scored against the following 30m bar.
   A 30m bar can only express 30/60/90-minute questions, so a 30m research
   primitive *cannot* discover that a feature's information decays in four
   minutes. It reports a failure at 30m, and the finding — short-lived
   continuation followed by reversal — never appears.

2. **Thresholds were absolute across heterogeneous symbols.** `imbalance < -0.20`
   asserts that a given imbalance means the same thing in AAPL at 09:45 as in VZ
   at 15:15. It does not, and nothing in the pipeline said so.

3. **Rejection happened late.** Hypotheses travelled feature → strategy →
   simulation → qualification → Paper Lab → inspect losses. The rejection was
   correct; it was just expensive, and it arrived in a form ("the strategy lost
   money") that does not distinguish *no information* from *real information
   that costs more to harvest than it pays*.

## What was built

`apps/api/app/services/intraday_alpha_map.py` — a measurement layer that sits
strictly before strategy construction. It contains no threshold, entry rule,
stop, target, position size or P/L, and creates no campaign, candidate, order or
position.

It answers, for each feature:

```
Does this feature predict any future return?
At what horizon?  For which symbols, at which times, in which states?
By how many basis points?
Is that materially larger than the cost of harvesting it?
Does it hold up out of sample once every look is charged for?
```

### The unit of work is a cell, not a strategy

A **cell** is one `(feature, transform, horizon, slice)` measurement of forward
return. Cells are stored individually in `intraday_alpha_map_cells` so that
"show me every horizon at which `signed_trade_imbalance` predicts anything" is a
single query rather than a research project.

Each cell returns exactly one of five verdicts:

| Verdict | Meaning | What to do |
|---|---|---|
| `insufficient_data` | Fewer than 200 observations or 20 sessions | Collect more before concluding |
| `no_information` | Rank IC insignificant and the extreme bucket fails the selection-adjusted gates | Retire the feature at these horizons |
| `information_below_cost` | The forecast is real and smaller than cost × safety multiple | Change the *expression* — horizon, selectivity, venue — not the threshold |
| `unstable` | Clears cost but fails monotonicity, half-sample sign agreement, deflated Sharpe, or BH adjustment | Collect more sessions before believing it |
| `tradable_candidate` | Everything passes | Construct a strategy against **this cell only** |

The distinction between `no_information` and `information_below_cost` is the
product. A single "rejected" collapses two findings with completely different
follow-ups into the same shrug.

### Horizons are measured on a finer grid than the signal

Signals are read at 15m/30m (where the trade-flow features live). Forward
returns are measured on a **1m grid** against a horizon ladder expressed in
**seconds**: 60, 120, 300, 600, 900, 1800, 3600 by default.

- Entry is the open of the first grid bar at or after the decision instant —
  never the close of the bar that produced the signal, which is not a price
  anyone could have traded at.
- Exit is the close of the last grid bar ending at or before `decision + horizon`.
- A horizon running past the session close is **dropped**, not shortened.
- A gap in the grid is **detected**, not silently measured as a longer forecast.

Sub-minute rungs (10s, 30s) are accepted by the declaration and reported as
`below_measurement_grid` with the exact requirement, rather than omitted. They
resolve automatically if a sub-minute bar grid is ever ingested. Rounding 10s up
to 60s would report a one-minute result under a ten-second label, so the ladder
refuses instead.

### Features are normalized per symbol and time-of-day, without lookahead

Every feature is measured at four transforms:

- `raw`
- `zscore_symbol_tod` — expanding z-score within the symbol's own history at the
  same exchange-local bar slot
- `percentile_symbol_tod` — the same, as a percentile
- `idiosyncratic` — the residual after removing the same-instant market and
  sector components

Normalization uses **strictly prior** observations only. Using full-sample mean
and deviation would leak the future into the signal, and the leak flatters
exactly the extreme buckets the verdict depends on. An observation with fewer
than 20 prior points in its (symbol, slot) history gets `None` rather than a
z-score computed from four points.

### Market and sector are removed so eight positions are not one bet

`residualize_cross_section` splits each feature at each instant into market
(cross-sectional mean), sector (residual mean within sector) and idiosyncratic
parts, and reports the variance share of each. `cross_sectional_dependence`
reports `same_sign_share` — the fraction of symbols carrying the same signal
sign at an instant — and flags pseudo-diversification above 0.80.

When market-wide flow turns, a raw per-symbol signal fires on eight names at
once and produces one market bet wearing eight tickers. The `idiosyncratic`
transform is testable as its own cell, so "this stock is doing something" and
"everything is doing something" are distinguishable results.

### Cost is a gate at the front

Two mechanisms, both before any threshold exists:

**Horizon preflight.** `horizon_cost_feasibility` computes, per horizon, the
mean of the top decile of absolute forward moves — an oracle that traded only
its best 10% of opportunities and got the direction right every time. If that
ceiling is below `cost × safety_multiple`, the horizon is killed before any
feature is scored against it. No feature, built or unbuilt, makes that horizon
tradable.

**Per-cell hurdle.** `required_gross_bps = round_trip_cost × safety_multiple`
(default 2.0). Break-even is the wrong bar: the cost model is itself an estimate
from quoted spreads and a limited set of matched fills, so an edge that merely
equals modelled cost sits inside the error of the thing it is compared against.

### Selection pressure is charged to the whole grid

- **Trial ledger.** Every cell is predeclared through `declare_trials`, so the
  deflated Sharpe is charged for the grid rather than for the winner. Measuring
  9 horizons across 14 features is 126 looks whether or not 126 numbers get
  reported.
- **Benjamini–Hochberg** across every cell in the run. A cell that survives on
  its own p-value but not after adjusting for the size of the grid it was found
  in is demoted to `unstable`.
- **PBO via CSCV.** `probability_of_backtest_overfitting` re-partitions the
  sessions into blocks, selects the best in-sample cell on each of the 70
  partitions, and records its out-of-sample rank. Above 0.5, the ranking of
  cells is a selection artefact and **no** cell from the grid is authorized,
  regardless of its own t-statistic. This is the check a per-cell t cannot make:
  a grid always produces a best cell, and its t says nothing about how many
  cells it beat to get there.
- **Split discipline.** A declaration can be measured exactly once. Re-measuring
  requires a new declaration, and the new declaration counts.

### Monotonicity

A signal that pays only in one interior bucket is describing a handful of
observations, not a relationship. Each cell reports the rank correlation between
bucket index and bucket mean plus the fraction of consecutive steps moving in
the dominant direction, and fails to `unstable` below 0.70.

## Paper Lab is now the last stage

Paper Lab cannot establish whether a feature predicts anything — a dozen fake
trades in an afternoon is not a sample. What it *can* establish is whether an
already-measured effect survives real scheduling, quotes, broker semantics and
fills.

Experiment creation therefore requires an explicit `--evidence-basis`:

- `alpha_map_cleared` — requires a `tradable_candidate` cell, validated at
  creation time via `cleared_cell()`. The stored config records the cleared
  horizon, so a Paper Lab holding period that does not match it is visibly
  testing a different hypothesis.
- `operational_curiosity` — allowed and recorded. Its P/L is evidence about
  plumbing, not about the hypothesis, and the config says so.

Both are legitimate. Conflating them is what turned a curiosity run into an
apparent verdict on a hypothesis.

### Freezing an experiment

`keftrade-intraday-paper-lab freeze --experiment-id N --finding "..."` preserves
an experiment as a finding. It becomes unrunnable. The failure this prevents is
quiet and common: an experiment loses money, the threshold is adjusted, it runs
again under the same name, and the record of what the original hypothesis
predicted is gone. Frozen experiments stay fully readable through `monitor` —
reading the evidence is the point of keeping them.

## Every row comes from the frozen dataset

The alpha map reads `research_dataset_trade_flow_features`,
`research_dataset_candles` and `research_dataset_intraday_features`, all bounded
by `dataset_id`. It never touches `candles`, `intraday_trade_flow_features` or
`intraday_features`.

This was a real defect in the first version: the declaration took its split
boundaries from the frozen manifest and then loaded signals and outcomes from
live tables. That is worse than having no protocol, because it wraps a content
hash, a declaration id and an immutability trigger around a measurement that a
nightly ingest could silently change. `test_intraday_alpha_map_frozen_evidence.py`
pins the property from three sides — the loader must not name a live table, its
output must be unmoved by live-table drift, and it must still change when the
frozen rows differ, so the first two cannot pass on a constant.

Two consequences worth knowing:

- **`--feed` and `--source` are gone from `declare`.** The snapshot pinned both
  when it was frozen, so a flag could only disagree with the data it reads.
- **Coverage is checked at declaration, not at measurement.** A declaration is
  single-use; discovering a missing outcome grid mid-run would spend it. The
  declaration records the frozen row counts and time bounds it measured against,
  so a later reader who gets different numbers from the same `dataset_id` has
  found a dataset that was not actually immutable.

## Freezing a dataset for it

`window_start` bounds a snapshot from below. Without it the only control was
`--as-of`, so a dataset always reached back to each symbol's earliest candle —
which is how a snapshot taken to study a recent order-flow window ends up
carrying years of history with no trade-flow evidence beside it and then hands
those sessions to the split calculator as though they were usable.

`--outcome-timeframes` freezes the finer candle-only grid the horizon ladder
needs. It is candle-only by necessity: `intraday_features` is CHECK-constrained
to 15m/30m, and an outcome grid needs prices, not signals. A timeframe cannot be
both signal and outcome.

Both go into the content hash, so a bounded snapshot cannot collide with an
earlier unbounded one. `INTRADAY_DATASET_VERSION` moved to `v4`, which means any
re-snapshot now mints a new manifest rather than reusing an old one — intended,
since the old manifests describe a different freezing rule.

Splits come from the **signal** layer only. A phase says which decisions a
researcher was allowed to see, and decisions happen on signal bars; letting a 1m
grid into the calculation would put the boundaries wherever that grid is dense.
The existing 50/30/20 session splitter is untouched.

## Usage

```bash
python -m app.cli.intraday_dataset_pipeline snapshot --from-universe --universe-key <key> --timeframe 30m --outcome-timeframes 1m --window-start 2025-01-06T14:30:00+00:00 --as-of 2026-03-31T20:00:00+00:00 --feed sip
```

```bash
python -m app.cli.intraday_alpha_map declare --dataset-id <new-id> --symbols AAPL,NVDA,QQQ,LLY,CRM,UNH,VZ,KO --signal-timeframe 30m --grid-timeframe 1m --cost-calibration-id 3 --cost-safety-multiple 2.0
```

```bash
python -m app.cli.intraday_alpha_map measure --declaration-id 1 --phase discovery
```

```bash
python -m app.cli.intraday_alpha_map profile --feature signed_trade_imbalance
```

Read-only HTTP surface (`/intraday-alpha-map/...`): `runs`, `runs/{id}`,
`features/{feature}/horizon-profile`, `cleared-cells`. Declaring and measuring
are CLI-only on purpose — both spend an irreversible piece of statistical
budget, and an endpoint that can be re-fired from a dashboard is the wrong shape
for either.

## What "working" looks like

`cleared-cells` returning empty is the *expected* state of a research programme
that is functioning. The goal is not to produce a tradable cell; it is to make
rejections cheap, early, and specific enough to act on. A map that says

```
signed_trade_imbalance / percentile_symbol_tod
    60s   information_below_cost   +2.1bps vs 4.0bps hurdle
   300s   information_below_cost   +1.4bps vs 4.0bps hurdle
  1800s   no_information           rank IC t = -0.4
```

has told you more in one run than a season of strategy simulations: the feature
is real at short horizons, it is not harvestable at this cost, and the 30m
continuation hypothesis is dead. Those are three different facts, and the layer
exists to keep them apart.

## Schema

Migration `077_intraday_alpha_map.sql`:

- `intraday_alpha_map_declarations` — frozen grid, cost model, safety multiple,
  split boundaries, declared cell count
- `intraday_alpha_map_runs` — one per declaration, with PBO, cross-sectional
  dependence, survivors and `strategy_construction_authorized`
- `intraday_alpha_map_cells` — one row per cell, indexed by feature/horizon/verdict
- Paper Lab experiments gain `evidence_basis`, `alpha_map_cell_run_id`,
  `alpha_map_cell_key`, `frozen_at`, `frozen_verdict`

All three evidence tables carry the standard immutability trigger.
