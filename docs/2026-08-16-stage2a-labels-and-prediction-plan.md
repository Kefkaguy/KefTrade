# Stage 2A — causal forward labels and the frozen prediction plan

**Scope:** label construction and test specification only.
**Not done:** no feature-label correlation, no information coefficient, no
ranking, no cell measured, no threshold, no P/L. **Stops before predictive
results.**

Everything below was declared **before any relationship between a feature and a
label was inspected**. That ordering is the only thing that makes the
multiplicity accounting meaningful.

```
label_engine_version    tier1_mbo_label_engine_v1
label_definition_hash   35249ad2d70ae4669e28...
label_schema_hash       18e395bd8bc837a4bf99...
stage2_plan_version     tier1_stage2_plan_v1
plan_hash               42534dca107486b5cca5...
built against           tier1_mbo_feature_engine_v2
                        semantics 4aaeb9cb6d6700524d7f...
features modified       NO
```

The label builder refuses to run against a feature set whose
`feature_semantics_hash` is not the frozen Stage-1 v2 value, so a label set can
never be silently paired with different feature semantics. A test asserts the
frozen Parquet is byte-identical after labelling.

## Label definitions

Seven horizons, **declared together** so a disappointing one cannot be dropped
and none can be substituted for another.

| Name | Kind | Definition |
|---|---|---|
| `next_change` | changes | the next coherent snapshot whose midpoint differs from the source |
| `next_2_changes` | changes | the second such snapshot |
| `1s` | time | first coherent state at or after `source_ts + 1s` |
| `5s` | time | …`+ 5s` |
| `10s` | time | …`+ 10s` |
| `30s` | time | …`+ 30s` |
| `60s` | time | …`+ 60s` |

### The at-or-after rule

For a time horizon `H`, `t_target = source_ts + H`. The label is the **first**
snapshot at or after `t_target` carrying a coherent midpoint — not the nearest,
never one before. If the first candidate at or after the target has no midpoint
(one side of the touch empty), the scan continues forward and
`skipped_incoherent_states` records how many were passed over.

`realized_lag_ns = label_ts − t_target`, always `≥ 0`, is preserved. **A label
with a large realized lag is a thin-book observation, not a 60-second horizon
quietly relabelled** — keeping the lag is what lets Stage 2B tell those apart. A
horizon is never shortened to fit the data.

### A limitation of labelling from the frozen snapshot set

**Change-count horizons are defined on the snapshot sequence of the same
cadence**, not on raw book events. "Next midpoint change" at the 1s cadence is
the next *second* whose midpoint differs — a coarser object than the next
event-time tick that Gould-Bonart measure. Labelling at event-time resolution
would require re-replaying 562 M records rather than reading the frozen 19.5 M
snapshots. Stated here rather than left to be inferred; if event-time change
labels are wanted, that is a separate declared build.

### Session edges

One Parquet file is one symbol-day, and a label search never leaves it. Nothing
crosses a session boundary — **an overnight gap is not a 60-second horizon.**

### Missing-label rules

Named, never imputed. There is no fill value, no forward fill, no
interpolation: a filled label is an invented observation that would count toward
significance as though it had been measured.

| Status | Meaning |
|---|---|
| `ok` | resolved |
| `source_midpoint_unavailable` | the source snapshot had no coherent midpoint |
| `session_end_before_horizon` | no snapshot exists at or after the target inside the symbol-day |
| `no_valid_future_state` | snapshots exist past the target but none carries a midpoint |
| `no_further_midpoint_change` | the midpoint never moves again (change horizons) |

Longer horizons run out before shorter ones, and neither borrows from the other:
an 11-second session yields 1s labels and **zero** 60s labels.

### Preserved columns

23 columns per `(snapshot, horizon)`. Join key: `symbol`, `session_date`,
`cadence`, `sequence_index`.

| Group | Columns |
|---|---|
| Horizon | `horizon`, `horizon_kind`, `horizon_magnitude` |
| Source | `source_ts_event`, `source_grid_ts_event`, `source_midpoint`, `source_ts_recv`, `source_feature_available_ts_recv` |
| Target / label | `target_ts_event`, `label_sequence_index`, `label_ts_event`, `label_ts_recv`, `realized_lag_ns`, `skipped_incoherent_states` |
| Outcome | `future_midpoint`, `midpoint_change`, `return_bps` |
| Latency provenance | `label_available_ts_recv` |
| Status | `label_status` |

`label_available_ts_recv` is the latest of the source feature availability, the
label state's feature availability, and the label record's `ts_recv` — so it
never precedes anything the label rests on, and Stage 3 can simulate latency
without re-deriving when a row could first have been known.

## Frozen statistical plan

| Element | Declared value |
|---|---|
| Splits | chronological **50 / 30 / 20** by session date — discovery / validation / confirmation |
| Embargo | one **60 s** horizon dropped at each boundary, covering the longest label |
| Confirmation | single-use |
| Transformations | expanding / prior-only **only**; full-sample statistics forbidden |
| Baseline | `price_only` — lagged midpoint returns and tick signs, prior-only |
| Incremental test | nested per `(cadence, horizon)`; the **increment** over baseline is reported, never the level alone |
| Clustering | by session **and** symbol; effective N reported beside raw N |
| Block bootstrap | symbol-day blocks, 2,000 resamples |
| Multiplicity | Benjamini-Hochberg, FDR **0.10**, across **every declared cell**, not the reported subset |
| Monotonicity | **≥ 0.70** where ordinal feature-response testing applies |
| Overfitting | PBO via CSCV, 16 partitions; **PBO > 0.5 authorizes no strategy**, whatever any single cell's t-statistic |
| Economic gate | Stage 2 pre-authorizes nothing; 5.0 bps minimum and t ≥ 3.0 remain Stage 3's bar |

Prohibited, explicitly: horizon substitution or nearest-horizon selection after
results; adding, dropping or renaming a horizon after results; re-splitting after
seeing a split's outcome; reporting a subset while correcting for that subset;
threshold selection inside Stage 2; any transformation using data at or after the
observation.

### Why a price-only baseline is not optional

Short-horizon midpoint changes mean-revert on their own — CJP measure ~59 %
two-step reversal on AAPL, and find the *conditional* rate is indistinguishable
from the unconditional one. A book feature that merely recovers bid-ask bounce
has added nothing, and without a baseline it would read as a finding.

## Multiplicity accounting

The grid is the **full** frozen vocabulary at every cadence against every
horizon. Pre-screening features now would be arbitrary at best and
outcome-informed at worst.

```
features            59
transforms           1   (Stage 1 already ships prior-only normalized variants)
cadences             4   (1s, 5s, 50ev, 200ev)
horizons             7
                  ----
feature cells     1,652   = 59 x 1 x 4 x 7
incremental tests    28   = 4 x 7
                  ----
declared this stage 1,680
prior effective       508
                  ----
cumulative          2,188
```

28 price-only baseline fits are counted separately: a baseline is not a
hypothesis about the book, though each incremental test against one is.

### The ledger does not reset

Tier-1 is **better input to the same question, not a new question**. The candle
work, the gap experiment, the order-flow factors, the news and sector studies and
the Stage-0 probe all consumed exposure against the same eventual decision.
Reusing a fresh dataset for a fourteenth idea is a fourteen-idea problem.
`PRIOR_EFFECTIVE_TRIALS = 508` is a declared **floor**, not an estimate to be
revised down.

### What 1,680 cells costs — stated now, not after

This is the honest price of declaring the full grid rather than a hand-picked
subset. Under BH at FDR 0.10 across 1,680 cells, the smallest p-values must fall
near `0.10 / 1680 ≈ 6 × 10⁻⁵` for the first discovery to survive; a cell with a
nominal p of 0.01 will not. Combined with PBO — which can veto the entire grid
regardless of individual t-statistics — **the design is deliberately hard to
pass.**

Two consequences worth accepting up front:

1. A genuine but modest effect may fail to survive this correction. That is the
   cost of not having pre-screened, and pre-screening on outcomes would have
   been worse.
2. If a narrower grid is preferred, it must be declared **now**, before any
   outcome — not selected later from the results.

## Tests

```
37 Stage-2A tests pass (25 label engine, 12 statistical plan)
```

Full suite: **1925 passed**, one pre-existing unrelated failure (below).

Label semantics: at-or-after with exact target hit, sparse grid with recorded
lag, skipped incoherent states, missing-not-backfilled at session end,
incoherent source, change horizons skipping unchanged and incoherent snapshots, a
flat session producing no change labels, longer horizons exhausting before
shorter ones, no horizon collapsing onto another's instant.

Leakage on the label side: appending future snapshots cannot change an already
resolved label; perturbing the far future cannot change a label resolved before
it; a label never reads a state before its own target; label availability never
precedes the feature row or the label state.

Plan: the grid is the full vocabulary; the cell count is exact; the ledger does
not reset and totals 2,188; splits are 50/30/20 with a 60 s embargo;
prior-only transformations only; the increment is what is reported; BH applies
across every declared cell; monotonicity 0.70 and PBO 0.5 are the declared
values; the prohibitions name horizon shopping explicitly.

Integration: a real Stage-1 Parquet is written with the actual engine, its
labelling spine read back, and the feature file asserted byte-identical
afterwards.

## Usage

```bash
python -m app.cli.mbo_labels definitions
```
```bash
python -m app.cli.mbo_labels plan
```
```bash
python -m app.cli.mbo_labels --output-dir reports/tier1_stage2_labels build --features-dir reports/tier1_mbo_features
```

## Limitations, stated plainly

- **No labels have been built from the real dataset.** The builder is tested against synthetic spines and one real synthetic-source Parquet; it has not walked the 160-symbol-day feature set.
- **Change horizons are cadence-resolution, not event-resolution.** See above. This is the largest definitional compromise in Stage 2A.
- **Label storage is not yet estimated.** 19.5 M snapshots × 7 horizons ≈ 137 M label rows across four cadences. At the ~130 bytes/row Stage 1 measured this would be far larger than the feature set, and the row count should be reduced by declaring which cadences carry which horizons — a decision that must be made **before** outcomes, and has not been made.
- **`PRIOR_EFFECTIVE_TRIALS = 508` is asserted, not recomputed.** The live ledger was not queried; the figure is carried forward as a declared floor.

## Pre-existing unrelated failure

`test_phase10_modules_have_no_runtime_ddl` fails on six temp-table
`CREATE INDEX` calls in `intraday_sector_leadlag_predictor.py`, from the
sector-leadlag merge (69015a1). Verified pre-existing. Unrelated to Stage 2.

## Stopping here

No feature-label relationship has been computed. No cell measured, no
correlation, no ranking, no threshold, no P/L.
