# Stage 2B — Executing the frozen prediction plan v3

**Status: implementation complete and tested; not yet executed against the real
160 symbol-day dataset.** The Stage-1 features, the Stage-2A labels and the raw
`.dbn.zst` files are on the VPS, not on this machine. No per-cell numbers appear
in this document because none have been computed. The commands that produce them
are in the last section.

Plan hash executed against: `ba51ccba12caf6969bbb0da84ff4cffa956361c56d5ea7bf77b453893331ca6e`
(`tier1_stage2_plan_v3`, unchanged). The executor refuses to run if that hash
moves.

---

## 1. What was built

| Component | File |
|---|---|
| Statistics engine | `apps/api/app/services/mbo_stage2_executor.py` |
| Feature/label → sufficient statistics, and the runner | `apps/api/app/cli/mbo_stage2.py` |
| Tests, statistics | `apps/api/tests/test_mbo_stage2_executor.py` |
| Tests, reduction | `apps/api/tests/test_mbo_stage2_cli.py` |

The run is two commands, deliberately separable so the expensive pass happens
once. `grams` streams one symbol-day cadence file at a time and reduces it to
per-`(cell, session_date)` sufficient statistics — `X'X`, `X'y`, `y'y`, `n`,
`Σy`. That is the only pass over row-level data. `run` applies the frozen
statistics to those.

This is exact rather than approximate. Stage-1 standardization is prior-only
*within* a symbol-day, so the design matrix does not depend on how dates are
later split, per-date Grams are additive, and every fit in the plan is a sum of
blocks followed by one Cholesky solve. The artifact is tens of megabytes, not
another multi-GB row-level dataset.

## 2. Four specification gaps closed before any outcome was viewed

Plan v3 froze the design but left four things that execution forces into the
open. All four are recorded in `SPECIFICATION_GAPS_CLOSED` in the executor, with
`closed_before_any_outcome: "true"`, and all four were decided before a single
real number was computed.

| Gap | Decision | Why this one |
|---|---|---|
| Out-of-sample `R²` reference | `1 − SSE_oos / SST_oos`, with `SST_oos` taken about the **training** mean | Using the out-of-sample mean would let the model benefit from knowing it. `delta_R2` is a difference over a shared denominator, so this sets its scale but cannot flip its sign. |
| Training set behind each block | discovery = leave-one-date-out within the 10; validation = fit on all 10 discovery dates; confirmation = fit on all 16 discovery+validation dates | Chronological and expanding is the only reading consistent with the alpha rule, which already confines tuning to discovery. |
| Price-only lag convention | lag *k* is the midpoint log-return realized into `t−k+1`; lag 1 is the most recent completed return, known at `t` | The plan said "lagged log-returns at lags [1,2,3,5,10]" without fixing whether a lag indexes a one-step or a *k*-step return. One-step returns at five offsets keeps the five columns distinct rather than nested sums of each other. |
| Rows with a withheld feature | dropped from **both** models, never imputed; count reported | Stage-1 declares those values withheld, not zero. Imputing invents observations, and the two models must see identical rows for `delta_R2` to be a nested comparison. |

## 3. Two defects found and fixed while testing

**A constant score sequence produced `t = 1.7 × 10¹⁶`.** The degenerate-input
guard tested `std == 0` exactly, but a numerically constant sequence leaves
float dust in the denominator — `std([0.01] * 10)` is about `6 × 10⁻¹⁹`. A cell
whose per-date `delta_R2` never varied would have reported a p-value of
`10⁻¹⁴³` and sailed through Benjamini-Hochberg. The guard is now relative to the
mean's own scale.

**The CSCV in-sample half was a hardcoded constant.** With `S = 16` it happened
to equal `S/2`, so the frozen procedure was correct, but any other block count
produced zero partitions and silently reported PBO as not computed. It is now
derived as `blocks // 2`, which agrees with the frozen constant at `S = 16`.
Nothing about the frozen procedure changed.

## 4. Correction to the PBO feasibility figure

Stage 2A recorded a projected **3.2 minutes**. That figure counted only the
Cholesky solves and omitted Gram assembly, which dominates: summing 8 blocks of
a 70×70 matrix for each of 14 cells in each of 12,870 partitions is tens of
millions of matrix additions. The first implementation was assembled naively and
did not finish in ten minutes.

Rebuilding leave-one-out as a subtraction from a precomputed total — `train −
block`, `O(1)`, rather than re-summing the training blocks, `O(k)` — brought the
complete procedure to a **measured 6.91 minutes**, single core, over all 12,870
partitions. PBO stays as an authorization statistic, as the plan required. The
original estimate was wrong by a factor of two and is recorded as superseded
rather than quietly replaced.

## 5. What has *not* been done

- No real features, labels, or raw MBO data are on this machine. Nothing in this
  document is a result.
- No feature decomposition, no per-sensor ranking.
- No trading strategy, no execution P/L.
- No threshold was changed after seeing anything, because nothing has been seen.
- Stage 3 is not started and must not start automatically.

## 6. Running it on the VPS

```bash
cd apps/api && python -m app.cli.mbo_stage2 grams \
  --features-dir ../../reports/tier1_features \
  --labels-dir ../../reports/tier1_stage2_labels \
  --output-dir ../../reports/tier1_stage2_results
```

```bash
cd apps/api && python -m app.cli.mbo_stage2 run \
  --grams-dir ../../reports/tier1_stage2_results \
  --output-dir ../../reports/tier1_stage2_results
```

The first command refuses to proceed unless the feature semantics hash, the
label definition hash and the plan hash all match the frozen artefacts. The
second writes `stage2_results.json`: per cell, the discovery/validation/
confirmation `delta_R2`, the session-clustered t and its p-value, the block
bootstrap interval, the BH decision against the family of 14, and the verdict
with its failure reason where it failed — plus the run-level PBO and the
cumulative multiplicity ledger carried forward from 508 prior effective trials.
