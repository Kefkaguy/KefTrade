# Stage 2A — exact event-time labels (v2) and the final frozen plan (v3)

**Scope:** label construction and test specification only.
**Not done:** no feature-label correlation, no IC, no rank, no cell measured, no
threshold, no P/L. **Stops before predictive results.**

```
label_engine_version    tier1_mbo_label_engine_v2
label_definition_hash   2e8ada7e56d780639a84...
label_schema_hash       f0d55b8db8755e963815...
stage2_plan_version     tier1_stage2_plan_v3
plan_hash               ba51ccba12caf6969bbb0da84ff4cffa956361c56d5ea7bf77b453893331ca6e
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

## Plan v3 — two corrections, pre-outcome

Commit **`a28b322971c4e2f476069c302f9b6d915e67f7b0`** is preserved as plan v2,
superseded-before-outcome. The label engine and Stage-1 features are unchanged.

### 1. The primary target is the raw event-time return

v2 residualized the target against the equal-weighted cross-sectional mean of
the eight symbols at each instant. **That rule is undefined for the event
cadences.** 1s and 5s grids are cross-symbol aligned by construction, but 50ev
and 200ev clocks advance on each symbol's own event count and are asynchronous —
"the same instant across eight symbols" does not exist there, and a quorum rule
cannot rescue a definition with no shared instants.

The dependence it was meant to control is already handled: whole session-date
splits keep all eight symbols on the same side of every boundary, and inference
is one observation per session date. And the primary question is *absolute*
future-price predictability beyond a price-only baseline, which the raw return
measures directly.

`return_bps` is now the primary target for all 14 cells. **No residualized
secondary family is declared** — if one is wanted later it is a separate
declaration with its own count.

### 2. PBO is nested, not flattened

v2's configuration set was "14 cells + 5 alpha values". Alpha is a
hyperparameter *inside* a cell, not a rival configuration. The set is the **14
cells**. Within each CSCV partition, for each cell independently:

1. select alpha using **only that partition's in-sample dates**, from the frozen
   candidate set, by leave-one-block-out CV over the in-sample blocks;
2. fit the cell with that alpha on the full in-sample half;
3. score in-sample and out-of-sample `delta_R2`.

Then select the cell with the best in-sample `delta_R2` and record its rank among
the 14 out-of-sample scores. **PBO = the fraction of partitions whose
in-sample-selected cell ranks in the bottom half out of sample.**

On the ordinary chronological path, no validation or confirmation date may enter
any alpha choice — stated as its own rule, not implied.

#### Feasibility was measured, not assumed

The declared alternative was *removing* PBO as an authorization statistic, never
substituting a flattened version. So it was measured first:

| | |
|---|---|
| Partitions, C(16,8) | 12,870 |
| Design width | 70 |
| Solves per cell-partition | 46 (5 alphas × 8 LOBO folds + 5 + 1) |
| **Total solves** | **8,288,280** |
| Measured 70×70 solve | 23.1 µs |
| Projected wall clock, solves only | 3.2 minutes — *an under-estimate, see below* |
| **Measured wall clock, end to end** | **6.91 minutes, single core** |
| Resident Gram blocks | 11.1 MB |

**Correction (Stage 2B).** The 3.2-minute figure above was computed before the
executor existed and counted only the Cholesky solves. It omitted Gram
assembly, which dominates: summing 8 blocks of a 70×70 matrix for each of 14
cells in each of 12,870 partitions is tens of millions of matrix additions, not
solves. The first implementation was assembled naively and did not finish in ten
minutes. Rebuilding leave-one-out as a subtraction from a precomputed total
(`train − block` rather than re-summing the training blocks) brought the
complete procedure to a **measured 6.91 minutes**. The conclusion is unchanged
and PBO stays, but the original number was wrong by a factor of two and is
recorded here rather than quietly replaced.

What makes it cheap: standardization is prior-only *within* symbol-day, so the
design matrix does not depend on how dates are split. Per-date Gram matrices
(`X'X`, `X'y`, `y'y`, `n`) are therefore **additive**, and any partition's fit is
a sum of 14 × 20 precomputed blocks followed by one Cholesky solve. No row-level
data is revisited per partition.

**PBO stays.**

### 3. What passing Stage 2 authorizes

The final four dates are an **internal** single-use confirmation gate — not an
external sample. Clearing every Stage-2 gate authorizes exactly:

- Stage-3 economic, cost and latency testing;
- acquisition or use of a larger, **completely untouched external** confirmation
  sample.

It authorizes **no real-money deployment** and no live capital. Four internal
dates from the same frozen 160-symbol-day batch cannot establish behaviour on
data the programme has never touched; they are the last internal check, not
evidence about the world.

BH family remains the **14** primary cells. The lifetime ledger keeps the **508**
floor and stands at **522**.

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
| Cross-sectional residualization | **not applied** — undefined for the asynchronous event cadences; see plan v3 above |
| Hyperparameter | ridge alpha ∈ {0.01, 0.1, 1, 10, 100}, chosen **inside discovery only** by expanding-origin CV, then frozen; re-tuning on validation or confirmation is forbidden |
| Out-of-sample score | out-of-sample R² of the **raw** `return_bps` target; the test statistic is `delta_R2 = R2(l3) − R2(baseline)` |
| Inference | session-clustered t on **per-session-date** `delta_R2` — one observation per date, so 19.5 M rows cannot pose as 19.5 M degrees of freedom; effective N reported beside raw N |
| Block bootstrap | whole session dates, 2,000 resamples, two-sided 95 % percentile on `delta_R2` |
| BH family | the **14** cells of this run, FDR 0.10 |
| Discovery pass | `delta_R2 > 0`, session-clustered t ≥ 3.0, bootstrap lower bound > 0 |
| Validation pass | same sign, t ≥ 3.0, survives BH across the 14, **and** point estimate ≥ half the discovery estimate |
| Confirmation | single use, run once, only for validation survivors; same sign, `delta_R2 > 0`, bootstrap lower bound > 0. No re-run, no re-split |
| Monotonicity | 0.70, declared to apply to the later ordinal feature-decomposition stage; explicitly **does not gate** the block-level test |
| PBO | **nested** CSCV, S = 16, C(16,8) = 12,870 partitions; alpha re-tuned inside each partition's in-sample half. Configuration set = **14 cells** (alpha is nested, not flattened). Metric = `delta_R2` on the raw target. **PBO > 0.50 authorizes nothing** |

Why the validation half-estimate rule: a discovery estimate that collapses in
validation is a discovery artefact, and "same sign, still significant" alone does
not catch that.

## F. Governance — two counts, kept apart

v1 used lifetime exposure as the BH denominator. These are different quantities:

| Quantity | Value | Use |
|---|---|---|
| **BH family** | **14** | multiplicity control within this run |
| Ridge-alpha looks | 5 | **nested inside each cell**; absorbed into the PBO estimate, not counted as configurations or in BH |
| Prior effective trials | **508** (floor) | lifetime bookkeeping |
| **Lifetime effective trials** | **522** | deflated-Sharpe style correction; programme-level judgement |

Correcting 14 cells as though they were 522 would be as wrong as correcting 522
looks as though they were 14. The ledger still does **not** reset — Tier-1 is
better input to the same question — and 508 remains a floor, not an estimate to
revise down.

## Tests

```
51 Stage-2A tests pass (22 label engine, 29 plan), 1 skipped
```

Full suite: **1938 passed**. The skip is the real-file integration test.

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
- **PBO's S = 16 against 20 dates gives blocks of 1–2 dates each.** The partition count is large but the blocks are thin. That is a real weakness of a 20-date sample, not of the method, and it is why PBO is a veto rather than a score to optimize.
- **The primary target is absolute, not market-relative.** A cell can now pass on market-wide predictability that a residualized target would have removed. That is the declared question, but it means a survivor needs Stage-3 to establish it is not simply beta.

## Pre-existing unrelated failure

`test_phase10_modules_have_no_runtime_ddl` fails on six temp-table
`CREATE INDEX` calls in `intraday_sector_leadlag_predictor.py`, from the
sector-leadlag merge (69015a1). Verified pre-existing; unrelated.

## Stopping here

No feature-label relationship has been computed. No cell measured, no
correlation, no IC, no rank, no P/L.
