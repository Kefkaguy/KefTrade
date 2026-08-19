# Stage 3.5 — Execution-timing mechanism study, for review (v3)

**Status: corrected and fully wired. No execution outcome computed. No order
placed. The run is gated behind an explicit reviewer flag.**

| | |
|---|---|
| Plan | `tier1_stage35_execution_timing_v3` |
| `PLAN_DESIGN_HASH` | `097b5d65dfd49d9c648865df3b31c716b51b0c685c6e8b347c772a3b6992ba94` |
| Superseded | `v1` `ab0d4267...`, `v2` `ab7393d0...` - before any execution outcome |
| `CELL_HASH` | `bea300ba23327075909e37e36864feee6087dc85a5d55108cb53a615c7046f00` |
| Evidence class | **exploratory mechanism development** — not confirmatory |
| Stage-3 verdict | **closed and unaltered** |

---

## 0. What this is, and what it is not

Stage 3 asked whether the L3 block supports a standalone market-taking
strategy. That question is closed and its verdict stands untouched — nothing
here reinterprets it, and a positive result here would not rescue it.

Stage 3.5 asks a different question, of a kind that does not require the signal
to pay for a round trip:

> Given an order that **already has to execute** for reasons of its own, can the
> four frozen predictors reduce implementation shortfall by choosing between
> immediate marketable execution and a bounded delay?

The order exists whether or not the model does; the only decision is *when* to
send it. A signal too small to overcome a round-trip spread can still be large
enough to say "not in the next 300 milliseconds" — those are genuinely
different bars, and the second is lower.

**This is an execution optimizer, not a directional strategy.**

---

## 1. Files

| Component | File |
|---|---|
| Frozen plan and hashes | `apps/api/app/services/mbo_stage35_plan.py` |
| Chronology, pairing, savings, screen | `apps/api/app/services/mbo_stage35_executor.py` |
| CLI (`plan` / `chronology` / `diagnose` / gated `run`) | `apps/api/app/cli/mbo_stage35.py` |
| Tests | `apps/api/tests/test_mbo_stage35_executor.py` |

---

## 2. The predictors, unchanged

Exactly the four Stage-2 survivors, bound by `CELL_HASH`:

```
200ev|next_2_changes   200ev|next_change
50ev|next_2_changes    50ev|next_change
```

Same model, same alpha, same features, coefficients **reproduced not refitted**.
No refitting against execution outcomes: a fit adjusted to improve savings would
be a model selected on this study's own outcome, and the study would then be
measuring its own tuning.

---

## 3. Row-level out-of-sample chronology

Stage 3 evaluated only the four confirmation dates, because the confirmation fit
was trained on the other sixteen. Stage 3.5 wants prediction rows on **all
twenty** dates, so each block gets a fit that never saw the date being
predicted:

| Block | Training set | Count |
|---|---|---|
| discovery | leave-one-discovery-date-out | 9 |
| validation | all discovery dates | 10 |
| confirmation | discovery + validation | 16 |

`chronology_map()` records the exact training dates behind every one of the
twenty, and `assert_chronology_is_clean()` refuses if any date appears in its
own training set, or if a validation/confirmation fit would reach forward to a
later date. The `chronology` CLI command emits that map and computes no outcome.

---

## 4. The synthetic required order — and why it is balanced

At every eligible prediction the study evaluates **both** a required BUY and a
required SELL, 100 shares each.

That is not a convenience. If the side were chosen by the model, this would be a
directional strategy wearing an execution costume, and the savings would be
indistinguishable from Stage-3 alpha. Pairing the two sides makes the parent
side **structurally independent** of the prediction by construction, and no rule
anywhere in the module may choose one.

**A structural consequence the report must not obscure:** for any given
prediction, *exactly one* of the two sides delays. Predicted up delays the sell;
predicted down delays the buy. So balanced-parent-flow savings are necessarily
**half** the delayed-side savings — and reporting only the delayed side would
double the apparent benefit of a mechanism that in practice sees both sides of
the flow. Both are reported; the balanced figure is primary.

---

## 5. Timing

```
decision          = source_feature_available_ts_recv
baseline_arrival  = decision + 250ms                        send now
timed_send        = min(max(target_available_ts_recv, decision),
                        decision + 750ms)                   wait, briefly
timed_arrival     = timed_send + 250ms      ->  at most decision + 1s
```

`target_available_ts_recv` is the frozen Stage-2 event target (`next_change` or
`next_2_changes`). **The trigger is the arrival of the event, not its outcome.**
A participant can observe "the midpoint has moved" in real time; they cannot
observe what the move was worth. Nothing reads a label return.

The floor at `decision` matters as much as the deadline: a target timestamp
earlier than the prediction would otherwise send a "delayed" order before the
prediction existed.

If the target has not resolved by the deadline, the order goes at the deadline.
That is what makes the wait bounded and therefore executable — a policy that
could hang on an event that never comes is not one a desk could run.

### The policy — sign only

| Required side | Prediction | Action |
|---|---|---|
| BUY | up | execute immediately |
| SELL | down | execute immediately |
| BUY | down | delay |
| SELL | up | delay |

**No magnitude threshold anywhere.** A magnitude threshold is a free parameter,
and a free parameter chosen against an execution outcome is a search. A test
asserts that a prediction of 0.0001 bps and one of 9,999 bps produce identical
decisions.

---

## 6. The outcome and its decomposition

```
BUY : savings_bps = (baseline_fill - timed_fill) / decision_midpoint * 10000
SELL: savings_bps = (timed_fill - baseline_fill) / decision_midpoint * 10000
```

Positive means the timing decision improved the required order. The non-delayed
side is **exactly** zero — not approximately, and not a small number that
averages out; a test asserts exact equality.

**Primary scientific metric: balanced-parent-flow savings.** A desk that only
ever executes the side the model happens to favour is not executing parent flow;
it is trading. Balanced flow is the honest denominator.

### The identity

With `e` the execution cost in the direction that hurts (`fill − mid` for a buy,
`mid − fill` for a sell), both sides satisfy

```
savings = midpoint_timing_benefit + book_walk_benefit
```

**exactly.** This is asserted across a grid of price and spread scenarios, both
sides, because it is the only thing separating *"the price moved our way"* from
*"the book was cheaper to cross"* — and those have very different implications
for whether the mechanism is real. Two dedicated tests pin the corners: a pure
spread widening shows up entirely as book-walk with zero midpoint benefit, and a
pure level shift shows up entirely as midpoint with zero book-walk.

Also reported: dollar savings per 100 shares, BUY and SELL separately, by
symbol, by session date, comparable pair count, `pairs_with_a_delay_fraction`
and `parent_orders_delayed_fraction` (1.0 and 0.5 respectively - each pair
contains two parent orders and only one delays), target-triggered vs
deadline-triggered counts, displayed liquidity, levels walked.

---

## 7. Fees

**No round-trip cost is charged.** The parent order executes either way; there
is no exit leg to pay for, and inventing one would manufacture a cost this
mechanism does not incur. Common one-way fees are identical under both policies
and are not strategy alpha.

Only a *per-dollar* charge could differ between the policies, and Section 31 was
**$0.00 per million** across the whole June-2025 window. The difference is
therefore expected to be exactly zero — and is **computed anyway and reported
separately**, because an expectation is not a measurement. A test confirms it is
zero under the June-2025 schedule *and* that restoring a non-zero rate produces
a non-zero difference, so the zero is a measurement rather than a hard-coded
convenience.

---

## 8. Comparability

> A paired observation is comparable only when **both** required counterfactual
> executions can be evaluated under the frozen rules.

Dropping the cases where only one leg fills would select on execution
difficulty, and execution difficulty is correlated with exactly the book states
this mechanism claims to exploit. Asymmetric failures are therefore given their
own reason codes and counted, never silently discarded.

All Stage-3 integrity machinery is retained unchanged: raw-source manifest
SHA/size verification, complete 160 symbol-day batch provenance, full
feature/label spine certification, `F_BAD_TS_RECV` exclusion, no receive-time
reordering, and the same 100-share / 10-level fill semantics.

---

## 9. Inference and the screen

Four primary cells only. Session-date mean balanced savings → clustered t → BH
at FDR 0.10 across the four.

A cell passes the **mechanism-development screen** only if all of:

- mean balanced-flow savings **> 0**
- clustered t **≥ 3**
- BH q **≤ 0.10**
- **≥ 10** session dates with comparable observations
- **≥ 1,000** comparable paired observations

### What a pass buys

**An external, untouched execution-timing confirmation experiment. Nothing
else.** It cannot authorize paper trading, cannot authorize live trading, and
cannot retroactively rescue Stage 3. `authorizes_paper_or_live` is `False` in
the report unconditionally, and a test asserts that for both a strongly positive
and a strongly negative family.

### If no cell passes

Close this execution-timing mechanism and move to passive-fill or
order-flow-toxicity research. **Do not tune thresholds, delays or latencies
against these outcomes** — that would convert a negative result into a search.

---

## 10. Governance

This is a new mechanism family opened **after** Stage-3 outcomes were viewed.
The twenty June-2025 dates have already produced viewed outcomes, so no
statistic computed on them can be confirmatory however it is corrected. That is
why the evidence class is fixed at *exploratory mechanism development* in the
plan itself rather than argued about later.

- Prior effective trials carried forward: **526** (522 + Stage 3's four).
- This study adds **4** exploratory primary specifications when outcomes are
  viewed.
- Forbidden and tested: refitting against execution outcomes, model-chosen
  parent side, any magnitude threshold, searching the delay/deadline/latency,
  symbol filtering, post-outcome promotion, authorizing paper or live trading,
  reinterpreting the Stage-3 verdict. A test asserts the CLI exposes no
  `--symbol`, `--threshold`, `--delay`, `--latency` or `--limit` flag through
  which any of those could be attempted.

---

## 11. The diagnostic mode

`diagnose` reports provenance and counts. Its payload is filtered through
`_strip_outcomes()` on the way out, which removes any field whose name contains
a token that could carry an execution outcome (`saving`, `benefit`, `fill`,
`midpoint`, `bps`, `clustered_t`, `p_value`, `verdict`, …), recursively. That is
deliberately a filter rather than a carefully hand-assembled payload, because
*"I remembered not to include it"* is not a guarantee. A test asserts the
diagnostic body never calls `assemble_report` or `evaluate_pair`.

---

## 12a. The eight pre-outcome corrections (v1 -> v2)

| # | Defect in v1 | Correction |
|---|---|---|
| 1 | `timed_send` was not floored at the decision, so a target timestamp *earlier* than the prediction would send a "delayed" order **before the prediction existed** | `min(max(target, decision), decision + 750ms)`; invariants `decision <= timed_send <= decision+750ms` and `decision+250ms <= timed_arrival <= decision+1s` asserted over a parametrized grid including negative offsets |
| 2 | Comparability required a timed fill for **both** sides, though only one delays - excluding observations on future liquidity the policy never uses, and *not at random*, since future thinness correlates with the dynamics being studied | baseline fills required for both sides; timed fill required **only** for the delayed side; the non-delayed side reuses its baseline fill verbatim |
| 3 | Dollar savings and the Section-31 notional multiplied **fixed-point** price units by share counts | both divide by `price_scale`; tested against the real `FIXED_PRICE_SCALE` - a 10c improvement on 100 shares is `$10`, not `1e10` |
| 4 | `delayed_fraction: 1.0` for a mechanism that delays one of the two parent orders in each pair | `pairs_with_a_delay_fraction` and `parent_orders_delayed_fraction` reported separately; the old key is gone |
| 5 | `run` raised `NotImplementedError` | fully wired, with a Stage-2 delta-R2 reproduction gate before any replay |
| 6 | Arrivals past the end of the certified stream would be served from `BookReplay`'s final snapshot as though tradable | `CoverageTracker` records the file's receive-time span; arrivals outside it are refused and counted, checked **before any book is queried** |
| 7 | The exact-zero prediction had no declared rule | `no_direction_zero_prediction`: neither side delays, no future instant is queried, count reported. An exact tie rule, **not** a magnitude threshold - 1e-300 still takes a side |
| 8 | - | `diagnose` remains incapable of evaluating pairs; `run` remains gated, now tested as *wired but refusing* |

### The reproduction gate (#5)

`per_date_betas` uses **Stage 2's own `fit`** - ridge is not reimplemented,
because a local reimplementation would be a second definition of the model and
the two would eventually disagree. A test asserts the function contains no local
ridge algebra (`np.linalg.solve`, `np.eye`, `penalty`).

Before any replay, `reproduce_stage2_delta_r2` rebuilds each date's delta-R2 from
the Stage-3.5 chronology and requires it to match Stage 2's recorded per-date
value to 1e-9. These are deterministic linear solves over identical sufficient
statistics: they agree to floating-point noise or they are not the same fit. Two
tests prove the gate bites - a 1e-6 perturbation refuses, and so does a
chronology that trains every date on all sixteen dates.

## 12b. Two fail-open defects closed (v2 -> v3)

### A. The unresolved-target rule was unreachable

The frozen policy has always said: if the target has not resolved by the
deadline, send at the deadline. `timed_send_instant(None)` implements that
correctly. But the loader filtered rows with

```python
usable = (status == LABEL_OK) & finite
```

so a row whose midpoint never moves again was discarded *before*
`evaluate_pair` ever saw it. The rule could not fire.

Worse, the filtering was not neutral: `no_further_midpoint_change` is exactly a
quiet period, so excluding those rows biases the study toward markets that move
- which is the very thing the mechanism is being tested on.

`execution_eligibility()` now admits `ok` and `no_further_midpoint_change`,
excludes `source_midpoint_unavailable` and `session_end_before_horizon`, and
**refuses** any status the plan has not considered rather than silently
admitting or discarding it. Per-cell source label-status counts are reported in
provenance, with no savings attached.

Coverage still governs: an unresolved target late in the session, whose deadline
arrival runs past the certified stream, is refused as outside coverage.

### B. The reproduction gate could pass on a subset

`recorded_stage2_per_date()` used to `continue` when the recorded count did not
match the block, and `reproduce_stage2_delta_r2()` could then return
`reproduction_verified: True` having checked fewer dates - or none.

All three functions now fail closed:

| Function | Was | Now |
|---|---|---|
| `per_date_betas` | silently dropped an absent training Gram | refuses; the declared training set must be present in full |
| `recorded_stage2_per_date` | skipped a block it could not map | refuses; unmappable records are an error, not a skip |
| `reproduce_stage2_delta_r2` | verified whatever it happened to check | requires exactly **10 discovery + 6 validation + 4 confirmation = 20** per cell |

Checking a subset and reporting success would have let an unverified chronology
through on the strength of whichever dates happened to line up.

## 12c. The diagnostic exercises the gates it is meant to certify

`diagnose` previously called `_prepare()` only. It therefore checked the frozen
plan, batch completeness, survivors and chronology - but not the 20-date Stage-2
reproduction gate, and not the label-status eligibility path, because it never
read an individual feature or label file.

That is the wrong kind of green light: a clean diagnostic followed by a run that
fails immediately on the two integrity gates most recently added.

`diagnose` now:

1. calls `_prepare(args)`;
2. calls `_build_fits(context)`, exercising the per-cell **10 discovery + 6
   validation + 4 confirmation = 20** reproduction gate, and reporting
   `reproduction_verified`, `dates_checked`, `dates_checked_by_block` and the
   frozen alpha per cell - Stage-2 *prediction-reproduction* diagnostics, not
   Stage-3.5 execution outcomes;
3. walks all 160 symbol-days and every applicable frozen cell through
   `_read_cell_inputs()` - the same loader the run uses - so full sequence /
   timestamp / midpoint spine certification, nullable availability handling and
   the eligibility rule (including unknown-status refusal) all genuinely run;
4. binds every raw source through `resolve_raw_source()`, verifying Stage-1's
   recorded filename, byte size and SHA-256 **without replaying the file**;
5. reports symbol-days inspected, session-date count, frozen-cell count, raw
   sources verified, spine-certified cell files, per-cell label-status counts,
   per-cell eligible-row counts, per-status excluded-row counts and the complete
   reproduction result, with `diagnostic_only: true` and
   `contains_execution_outcome: false`.

It still calls no `evaluate_pair`, no `CellTiming.summary`, no
`assemble_report`, no `BookReplay`, and no `walk_book`; predictions are computed
by the shared loader but never read, recorded or returned. The recursive
`_strip_outcomes()` filter remains on the way out.

### Status counts are serialized as records

Two label statuses contain the word "midpoint", and `_strip_outcomes()` removes
any key containing an outcome-bearing token. Serializing statuses as dictionary
*keys* therefore deleted the counts for `no_further_midpoint_change` and
`source_midpoint_unavailable` from the written artefact - exactly the provenance
the diagnostic exists to show.

The filter is right to be blunt, so it was not weakened. The counts are reshaped
instead:

```json
[{"cell": "50ev|next_change",
  "statuses": [{"status": "ok", "count": 123},
               {"status": "no_further_midpoint_change", "count": 45},
               {"status": "source_midpoint_unavailable", "count": 2}]}]
```

Neither `status` nor `count` is a forbidden key, so the records pass through
intact. A test asserts that; a second test demonstrates the *defect* - the same
counts as keys collapse to `{"ok": 123}` - so the reason for the record shape
cannot be optimised away later; and a third asserts no token was removed from
the filter to make the statuses fit.

Those prohibitions are tested by inspecting the **executable body** with
docstring *and comments* removed - matching on prose that names the forbidden
calls would pass or fail for the wrong reason. A further test asserts a failing
reproduction gate stops the diagnostic outright and writes no artefact.

**No mechanism rule changed**, so `tier1_stage35_execution_timing_v3` and
`PLAN_DESIGN_HASH 097b5d65...` are untouched; only the executor implementation
version moved, to `tier1_stage35_executor_v4`.

## 12. Leakage and timing audit

| Risk | Control | Test |
|---|---|---|
| Fit predicts a date it trained on | leave-one-out in discovery; earlier-blocks-only elsewhere | `test_no_date_anywhere_trains_on_itself` |
| Fit reaches forward to later dates | validation/confirmation training sets are strictly earlier | `test_validation_and_confirmation_never_train_on_later_dates`, `test_a_confirmation_fit_reaching_forward_is_refused` |
| Model picks the profitable side | both sides evaluated at every prediction; side never model-chosen | `test_exactly_one_side_delays_at_every_prediction`, `test_both_sides_are_always_evaluated` |
| Delayed side reported as the whole flow | balanced metric is primary and is exactly half | `test_balanced_savings_are_exactly_half_the_delayed_side` |
| Future price used as the wait trigger | trigger is the event's *availability instant*; no label return is read | `test_the_target_releases_the_order_…`, `test_an_unresolved_target_still_sends_at_the_deadline` |
| Unbounded wait | deadline at decision + 750 ms; arrival capped at decision + 1 s | `test_arrival_can_never_exceed_one_second_after_the_decision` |
| Fill peeks past arrival | Stage-3 book semantics unchanged, `ts_recv <= arrival` | inherited Stage-3 replay tests |
| Flagged receive timestamps | `F_BAD_TS_RECV` window exclusion | `test_a_flagged_timing_window_is_not_comparable` |
| Selection on execution difficulty | both legs required; asymmetric failures counted | `test_a_pair_needs_both_legs_to_be_comparable`, `test_asymmetric_failures_are_counted_not_dropped` |
| Send before the decision | `timed_send` floored at `decision_ts` | `test_a_target_before_the_decision_never_sends_early` |
| Exclusion on unused liquidity | timed fill required only for the delayed side | `test_the_non_delayed_sides_future_illiquidity_does_not_break_the_pair` |
| Fictitious post-EOF fill | coverage span checked before any book query | `test_an_arrival_past_the_end_of_the_stream_is_refused` |
| Model not Stage 2's own | per-date delta-R2 reproduced to 1e-9 before replay | `test_the_chronology_reproduces_stage2s_recorded_per_date_delta_r2` |
| Unreachable deadline rule | `no_further_midpoint_change` is eligible; unknown statuses refuse | `test_an_unresolved_target_with_coverage_is_evaluated_at_the_deadline` |
| Partial reproduction passing | 10/6/4 required per cell; missing Gram refuses | `test_reproduction_requires_all_twenty_dates` |
| Threshold/parameter search | sign-only policy; no tunable flags | `test_the_magnitude_of_the_prediction_changes_nothing`, `test_the_cli_offers_no_symbol_filter_or_threshold_option` |
| Fictitious exit cost | no round trip; only price-dependent fee difference reported | `test_no_round_trip_cost_is_charged`, `test_the_price_dependent_fee_difference_is_zero_under_june_2025` |

**133 Stage-3.5 tests; 634 passed, 3 skipped** across the whole MBO suite; ruff
clean.

---

## 13. Running it

```bash
cd apps/api && python -m app.cli.mbo_stage35 plan
```

```bash
cd apps/api && python -m app.cli.mbo_stage35 chronology --grams-dir ../../reports/tier1_stage2_results
```

The economic pass stays gated and unwired pending review.
