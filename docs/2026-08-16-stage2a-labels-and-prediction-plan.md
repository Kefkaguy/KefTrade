# Stage 2A v2 — exact event-time labels and the frozen executable plan

**Scope:** label construction and test specification only.
**Not done:** no feature-label correlation, no IC, no rank, no cell measured, no
threshold, no P/L. **Stops before predictive results.**

```
label_engine_version    tier1_mbo_label_engine_v2
label_definition_hash   2e8ada7e56d780639a84...
label_schema_hash       f0d55b8db8755e963815...
stage2_plan_version     tier1_stage2_plan_v2
plan_hash               e959a556b8be89b75ebf47479deafd7de67d8fd117edb83aa853bb8fd110a94c
built against           tier1_mbo_feature_engine_v2 / 4aaeb9cb6d67...
features modified       NO
label columns           72  (9 shared + 9 × 7 horizons)
```

## v1 superseded before outcome

Commit **`f3289c9701ea8c7d431d941a60b20b5cf447c548`** is preserved in
`SUPERSEDED_LABEL_VERSIONS` and `SUPERSEDED_PLAN_VERSIONS`. No predictive result
had been inspected, so these are corrections to a wrong measurement and a
loose plan — not tuning.

| v1 defect | v2 |
|---|---|
| Labels derived from the sampled Stage-1 cadence sequence | Resolved from the certified MBO stream |
| 137 M long rows (source × horizon) | 19.5 M wide rows, one per source snapshot |
| 1,652 individual-feature cells | 14 block-level cells |
| Split by fraction | Split by whole session-date blocks, 10 / 6 / 4 |
| Model, scaling, score, statistic, pass criteria unspecified | All declared exactly |
| Lifetime exposure used as the BH denominator | Separated: BH = 14, lifetime = 522 |

## A. Exact event-time labels

Labels come from replaying each raw symbol-day through the **same `MboBook` the
Tier-1 gate certified 160/160 on**.

| Horizon | Definition |
|---|---|
| `next_change` | first completed `F_LAST` state with `ts_event > t_s` whose midpoint differs from the source midpoint |
| `next_2_changes` | the next state after that whose midpoint differs from *the first change's* midpoint |
| `1s` `5s` `10s` `30s` `60s` | first **coherent** completed `F_LAST` state with `ts_event >= t_s + H` |

**A state before the target is never used.** Time labels satisfy
`label_ts >= t_s + H`; change labels satisfy `label_ts > t_s`.

Verified on a stream with 1 ms event spacing: `next_change` resolves **1 ms**
after the source, where v1 would have waited for the next whole second. A
one-sided book is not a midpoint change, and a restated identical midpoint is not
a change either — both tested.

Per horizon, preserved: `target_ts_event`, `label_ts_event`, `label_ts_recv`,
`realized_lag_ns`, `future_midpoint`, `midpoint_change`, `return_bps`,
`available_ts_recv`, `status`.

`available_ts_recv` is the later of the source feature availability and the
resolving record's `ts_recv`, so it never precedes an input.

### Streaming, one replay per symbol-day

337 M book states are never materialized. The resolver holds only sources still
awaiting a label:

- **Time horizons** keep a FIFO per horizon. Targets are monotone in `t_s`, so
  the head is always next to resolve — amortized O(1).
- **Change horizons** group pending sources by the midpoint they are waiting to
  differ from. When a state arrives at midpoint `m`, every group keyed other
  than `m` resolves at once. `next_2_changes` sources then re-enter keyed by the
  midpoint that resolved their first change.

Multiple cadences resolve on the **same single replay**; two sources at the same
instant receive the same label instants (tested).

### Session edges and missing labels

One raw file is one symbol-day; resolution never leaves it. An overnight gap is
not a 60-second horizon. Missing labels are named — `source_midpoint_unavailable`,
`session_end_before_horizon`, `no_further_midpoint_change` — and never imputed,
forward-filled or interpolated.

## B. Wide storage

One row per Stage-1 source snapshot, 72 columns. v1's long layout turned 19.5 M
snapshots into 137 M rows — larger than the feature set it described. The join to
features is now one-to-one on `(symbol, session_date, cadence, sequence_index)`.

## C. Primary grid — 14 block-level cells

The 59 features are **sensors, not 59 strategies**. The primary authorization
test is the incremental predictive value of the *complete* frozen L3 block beyond
a price-only baseline.

| Cadence | Horizons | Cells |
|---|---|---|
| `1s` | 1s, 5s, 10s, 30s, 60s | 5 |
| `5s` | 1s, 5s, 10s, 30s, 60s | 5 |
| `50ev` | next_change, next_2_changes | 2 |
| `200ev` | next_change, next_2_changes | 2 |
| | | **14** |

Time horizons pair with time cadences, change horizons with event cadences: a
60-second horizon on a 200-event clock would test the clock mismatch, not the
book.

**No individual-feature ranking in the primary run.** Feature decomposition is a
later, separately declared and separately counted stage, and only if the
block-level hypothesis survives.

## D. Chronological split by whole session-date blocks

| Block | Session dates |
|---|---|
| discovery | earliest **10** |
| validation | next **6** |
| confirmation | final **4** (single use) |

20 dates × 8 symbols = 160 symbol-days. **All eight symbols move together and a
date is never split across sets** — two symbols from the same session are not
independent, so splitting a date leaks the day's regime across the boundary.

No additional embargo period is applied, and the reason is stated rather than
asserted: whole-date blocks already exceed the longest label (60 s) by orders of
magnitude.

## E. The executable model, frozen

| Element | Declared |
|---|---|
| Primary target | `return_bps` for the cell's horizon, rows with status `ok` only, never imputed |
| Price-only inputs | lagged own-cadence midpoint log-returns at lags **1, 2, 3, 5, 10** plus each sign; prior-only, within symbol-day; OLS with intercept |
| L3 model | **ridge regression**, inputs = the price-only lags **plus** all 59 features; **nested**, never a separate fit |
| Scaling | per (symbol, cadence) expanding standardization on strictly prior observations; withheld below 30 priors |
| Cross-sectional residualization | at each grid instant subtract the equal-weighted cross-sectional mean of the **target** across the 8 symbols; requires ≥ 6 of 8 present else the row drops; features are not residualized |
| Hyperparameter | ridge alpha ∈ {0.01, 0.1, 1, 10, 100}, chosen **inside discovery only** by expanding-origin CV, then frozen; re-tuning on validation or confirmation is forbidden |
| Out-of-sample score | out-of-sample R² of the residualized target; the test statistic is `delta_R2 = R2(l3) − R2(baseline)` |
| Inference | session-clustered t on **per-session-date** `delta_R2` — one observation per date, so 19.5 M rows cannot pose as 19.5 M degrees of freedom; effective N reported beside raw N |
| Block bootstrap | whole session dates, 2,000 resamples, two-sided 95 % percentile on `delta_R2` |
| BH family | the **14** cells of this run, FDR 0.10 |
| Discovery pass | `delta_R2 > 0`, session-clustered t ≥ 3.0, bootstrap lower bound > 0 |
| Validation pass | same sign, t ≥ 3.0, survives BH across the 14, **and** point estimate ≥ half the discovery estimate |
| Confirmation | single use, run once, only for validation survivors; same sign, `delta_R2 > 0`, bootstrap lower bound > 0. No re-run, no re-split |
| Monotonicity | 0.70, declared to apply to the later ordinal feature-decomposition stage; explicitly **does not gate** the block-level test |
| PBO | CSCV with S = 16 contiguous date blocks, all **C(16,8) = 12,870** balanced partitions; select best in-sample configuration per partition, record its out-of-sample rank; PBO = fraction landing in the bottom half. Configuration set = 14 cells × 5 alphas. Metric = `delta_R2`. **PBO > 0.50 authorizes nothing** |

Why the validation half-estimate rule: a discovery estimate that collapses in
validation is a discovery artefact, and "same sign, still significant" alone does
not catch that.

## F. Governance — two counts, kept apart

v1 used lifetime exposure as the BH denominator. These are different quantities:

| Quantity | Value | Use |
|---|---|---|
| **BH family** | **14** | multiplicity control within this run |
| Ridge-alpha looks | 5 | seen by **PBO**, not by BH — a choice, not a hypothesis about the book |
| Prior effective trials | **508** (floor) | lifetime bookkeeping |
| **Lifetime effective trials** | **522** | deflated-Sharpe style correction; programme-level judgement |

Correcting 14 cells as though they were 522 would be as wrong as correcting 522
looks as though they were 14. The ledger still does **not** reset — Tier-1 is
better input to the same question — and 508 remains a floor, not an estimate to
revise down.

## Tests

```
44 Stage-2A tests pass (22 label engine, 22 plan), 1 skipped
```

Full suite: **1931 passed**. The skip is the real-file integration test.

Label semantics: event-time `next_change` at millisecond resolution; second
event-time change; repeated midpoint is not a change; one-sided book is not a
change; flat book yields no change labels; at-or-after with recorded lag; sparse
future never shortens the horizon; missing not backfilled; longer horizons
exhaust before shorter; each time horizon resolves to its own distinct instant;
incoherent source blocks every horizon.

Leakage: extending the stream cannot change an already-resolved label; a label
never reads a state before its target; a state at exactly the source instant is
never its own label; availability never precedes the feature row or the
resolving record.

Plan: 14 cells exactly; clock pairing enforced; no feature ranking in the primary
run; 10/6/4 whole-date blocks; every required model element declared; nested L3
model; tuning confined to discovery; increment not level; session-clustered
inference; all three pass criteria; PBO implementation and metric; BH = 14 while
lifetime = 522; alpha looks in PBO but not BH; the prohibition list closes
horizon shopping, date movement, re-tuning, feature ranking and confirmation
re-runs.

Integration (opt-in): one real symbol-day, Stage-1 features built from it, labels
resolved from the same certified stream in a single replay, with the frozen
Parquet asserted byte-identical afterwards.

```bash
KEFTRADE_MBO_TEST_FILE=/path/to/xnas-itch-20250626.mbo.CMCSA.0000.dbn.zst python -m pytest tests/test_mbo_label_engine.py -q
```

## Usage

```bash
python -m app.cli.mbo_labels definitions
```
```bash
python -m app.cli.mbo_labels plan
```
```bash
python -m app.cli.mbo_labels --output-dir reports/tier1_stage2_labels build --features-dir reports/tier1_mbo_features --raw-dir /path/to/dbn
```

`build` now requires `--raw-dir`: labels are event-time, so the certified stream
is not optional. A symbol-day with features but no raw file is recorded as a
failure rather than labelled from the grid.

## Limitations, stated plainly

- **No labels built from the real dataset.** Tested against synthetic streams and one opt-in real-file integration case; the 160 symbol-days have not been walked.
- **Label build cost is a second full replay.** Stage-1 extraction replayed 562 M records once; labelling replays them again. If that is unacceptable, features and labels could be produced in one pass — but that would couple the frozen artefact to the label build, and I chose not to touch Stage 1.
- **`PRIOR_EFFECTIVE_TRIALS = 508` is asserted, not recomputed.** The live ledger was not queried; carried forward as a declared floor.
- **The 6-of-8 residualization quorum is a judgement.** Declared before outcomes, but not derived from anything.
- **PBO's S = 16 exceeds the 20 available dates only narrowly.** With 20 dates and 16 blocks, blocks are 1–2 dates each; the partition count is large but the blocks are small. That is a real weakness of a 20-date sample, not of the method, and it is why PBO is a veto rather than a score to optimize.

## Pre-existing unrelated failure

`test_phase10_modules_have_no_runtime_ddl` fails on six temp-table
`CREATE INDEX` calls in `intraday_sector_leadlag_predictor.py`, from the
sector-leadlag merge (69015a1). Verified pre-existing; unrelated.

## Stopping here

No feature-label relationship has been computed. No cell measured, no
correlation, no IC, no rank, no P/L.
