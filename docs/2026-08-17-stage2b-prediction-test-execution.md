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

## 2. Six specification gaps closed before any outcome was viewed

Plan v3 froze the design but left six things that execution forces into the
open. All are recorded in `SPECIFICATION_GAPS_CLOSED` in the executor, with
`closed_before_any_outcome: "true"`, and all were decided before a single real
number was computed.

| Gap | Decision | Why this one |
|---|---|---|
| Out-of-sample `R²` reference | `1 − SSE_oos / SST_oos`, with `SST_oos` taken about the **training** mean | Using the out-of-sample mean would let the model benefit from knowing it. `delta_R2` is a difference over a shared denominator, so this sets its scale but cannot flip its sign. |
| Training set behind each block | discovery = leave-one-date-out within the 10; validation = fit on all 10 discovery dates; confirmation = fit on all 16 discovery+validation dates | Chronological and expanding is the only reading consistent with the alpha rule, which already confines tuning to discovery. |
| Price-only lag convention | lag *k* is the midpoint log-return realized into `t−k+1`; lag 1 is the most recent completed return, known at `t` | The plan said "lagged log-returns at lags [1,2,3,5,10]" without fixing whether a lag indexes a one-step or a *k*-step return. One-step returns at five offsets keeps the five columns distinct rather than nested sums of each other. |
| Rows with a withheld feature | dropped from **both** models, never imputed; count reported | Stage-1 declares those values withheld, not zero. Imputing invents observations, and the two models must see identical rows for `delta_R2` to be a nested comparison. |
| **Ridge penalty scope** | penalty on the 59 L3 columns only; intercept and the 10 price-only columns carry penalty 0 | See §2a. |
| **Where the Stage-2 scaling is applied** | at design-matrix construction, per `(symbol, cadence, feature)`; the frozen Stage-1 Parquet is never modified | See §2b. |

### 2a. The penalty must not reach the baseline

The first implementation penalized every coefficient except the intercept — the
conventional ridge default. That is wrong here. The baseline is fitted by OLS
with no regularization, so penalizing the same ten price-only columns inside the
augmented model made the two fits treat their shared columns differently.
`delta_R2` would then have measured *incremental L3 information minus shrinkage
of the baseline*, a mixture that can take either sign. A cell where the L3 block
carried nothing at all could have reported a negative `delta_R2` produced
entirely by regularization, and one where it carried a little could have had that
masked.

The penalty now starts at column 11. This is what makes the model nested in the
sense the plan requires — "the baseline's inputs augmented, never a separate
fit" — because the baseline OLS solution is now exactly the `alpha → ∞` limit of
the augmented fit. Both properties are asserted: with an identically-zero L3
block the augmented fit reproduces the baseline coefficients to 1e-10 and
`delta_R2` is zero to 1e-12 **for every one of the five frozen alphas**, and at
`alpha = 1e14` the L3 coefficients vanish and the baseline coefficients return.

### 2b. The scaling rule had to be implemented, not assumed

Plan v3 declares expanding prior-only standardization per `(symbol, cadence)`,
but Stage 1 froze only four columns in standardized form. The first
implementation sent the other 55 into the ridge in their raw units. Under a
penalized fit that is not a cosmetic problem: the penalty is applied to
coefficients, so a single `alpha` means a completely different amount of
shrinkage for a column measured in shares than for one measured in basis points.
The alpha grid would have been searching over incomparable models.

`expanding_standardize` now applies the frozen rule to all 59 L3 columns at
design-matrix construction time:

- statistics from observations **strictly before** the current row
- within the same symbol-day only, per symbol, cadence and feature
- at least 30 prior finite observations, otherwise withheld
- withheld means NaN and a dropped row — never zero, never imputed
- a column with no prior variation is withheld rather than divided by zero

Doing it here rather than in Stage 1 leaves the frozen Stage-1 Parquet
untouched, and keeps the scaling within-symbol-day and backward-looking — which
is precisely the property that makes per-date Grams additive and the nested CSCV
affordable.

Values are shifted by each column's first finite observation before
accumulating. That is a numerical, not statistical, choice: a z-score is
invariant to the shift, but `Σx² − n·mean²` loses most of its precision when the
mean dwarfs the spread, which is the case for every price-level column. The
shift also makes a genuinely constant column produce an *exact* zero variance,
so it is withheld cleanly rather than dividing float dust by smaller float dust.

Because withholding removes rows, the manifest now reports `withheld_by_feature`
and `features_fully_withheld_on_a_symbol_day`. A feature that never varies on a
symbol-day withholds every row of it and removes that symbol-day from every
cell — correct under the no-imputation rule, but silent, so it is counted.

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

### 3a. Tests added for the two corrections

| Property | Test |
|---|---|
| Standardization reads only strictly prior rows | `test_standardization_uses_only_strictly_prior_observations` |
| Truncation invariance — appending rows cannot move an earlier row | `test_truncation_invariance_appending_rows_cannot_move_an_earlier_row` |
| Future perturbation — a +500 shift on all later rows cannot move an earlier one | `test_future_perturbation_cannot_move_an_earlier_row` |
| Scale invariance across 12 orders of magnitude (1e-6 … 1e6) | `test_scale_invariance_of_the_standardized_column` |
| Shift invariance under a 1e5 offset | `test_shift_invariance_survives_a_large_offset` |
| Below 30 priors: withheld, never zero | `test_values_below_the_prior_minimum_are_withheld_not_imputed` |
| Constant column withheld, not divided by zero | `test_a_column_with_no_prior_variation_is_withheld_not_divided_by_zero` |
| Gaps in the raw column do not contaminate later statistics | `test_non_finite_inputs_do_not_contaminate_later_statistics` |
| The design really carries standardized columns, not raw | `test_the_l3_block_reaching_the_design_is_standardized_not_raw` |
| Dead L3 block ⇒ augmented fit **is** the baseline OLS, every alpha | `test_a_dead_l3_block_reduces_the_augmented_fit_to_the_baseline_ols` |
| Dead L3 block ⇒ `delta_R2 ≈ 0`, every alpha | `test_delta_r2_is_zero_when_the_l3_block_carries_nothing` |
| `alpha → ∞` returns the baseline solution | `test_a_very_large_alpha_returns_the_baseline_solution` |
| The baseline slice is OLS whatever alpha is passed | `test_a_gram_narrower_than_the_penalty_offset_is_pure_ols` |

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
