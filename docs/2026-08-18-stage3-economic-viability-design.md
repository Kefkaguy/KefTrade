# Stage 3 — Economic viability design, for final review (v5)

**Status: complete and wired. No economic outcome computed. No order placed of
any kind. The run remains gated behind an explicit reviewer flag.**

| | |
|---|---|
| Stage-3 plan | `tier1_stage3_economics_v5` |
| `PLAN_DESIGN_HASH` | `055c3d83108ea6223c12bd541d824843ace071a110e3bd5e1292e1f0665186f4` |
| `SURVIVOR_HASH` | `bea300ba23327075909e37e36864feee6087dc85a5d55108cb53a615c7046f00` |
| Superseded | `v1` `f6878f66…`, `v2` `87429255…`, `v3` `e5266ef3…`, `v4` `a780b241…` — all pre-outcome |

---

## 0. Governance, corrected

The v1 module claimed `declared_before_survivors_were_known: true`. **That was
false** — Stage 2B had already run on the VPS. I have removed the claim rather
than softening it, and recorded v1 as superseded with the reason. What the
artefact now states is what is actually true:

```json
{"stage2_survivors_known": true,
 "stage3_economic_outcome_viewed": false,
 "stage3_rules_frozen_before_economic_outcomes": true}
```

Knowing which cells survived cannot bias a cost model or a latency ladder. It
*could* bias a trading rule chosen to flatter them — which is why the rules are
frozen and hashed before any economic number exists.

### The four frozen survivors

```
200ev|next_2_changes
200ev|next_change
50ev|next_2_changes
50ev|next_change
```

`SURVIVOR_HASH = bea300ba…`, bound into `PLAN_DESIGN_HASH` and into every
artefact. `assert_frozen_plan()` refuses if either moves, and
`load_frozen_survivors` refuses if `stage2_results.json` names a different set.

---

## 1. Files

| Component | File |
|---|---|
| Frozen plan, hashes, fee schedule | `apps/api/app/services/mbo_stage3_plan.py` |
| Fill model, economics, replay, inference | `apps/api/app/services/mbo_stage3_executor.py` |
| CLI (`plan` / `freeze` / gated `run`) | `apps/api/app/cli/mbo_stage3.py` |
| Tests | `apps/api/tests/test_mbo_stage3_executor.py` |

The executor refuses to compute anything if the plan design hash or the survivor
hash moves. It contains no broker client and no code path that could reach one.

---

## 2. The three instants — and why none of them is a duration

**All four survivors are event-clocked.** `50ev` and `200ev` cadences paired with
`next_change` and `next_2_changes`. None of them has a numeric horizon, and the
v1 module was wrong to carry one: there is no number of nanoseconds that stands
in for "the next midpoint change", because *when that change happens is itself
the thing being measured*.

So the exit instant is read from the frozen Stage-2 label columns and nowhere
else:

```
t_decision = feature_available_ts_recv            nothing may be decided earlier
t_arrival  = feature_available_ts_recv + latency  the entry order reaches the book
t_resolve  = <prefix>_available_ts_recv           the frozen Stage-2 target event
t_exit     = t_resolve + latency                  the flat-out order reaches the book
```

`<prefix>` is `next_change` or `next_2_changes`; the columns are exactly the ones
Stage 2A wrote — `label_ts_event`, `label_ts_recv`, `available_ts_recv` and
`realized_lag_ns`. `evaluate_candidate` has **no `horizon_ns` parameter**, so a
fabricated duration cannot be passed through it, and a test asserts that absence
so it cannot come back.

### `horizon_resolved_before_entry`

If the target event resolves at or before the executable entry arrival, the
candidate is recorded as **`horizon_resolved_before_entry`** — a named missed
opportunity, counted, never a trade and never silently dropped. The prediction
may have been perfectly correct; it was simply unharvestable at that latency.

This is not an edge case at these horizons — it is likely to be the single
largest category at 250 ms and 1 s, and it is the mechanism by which the ladder
answers the harvestability question at all. A test pins the case where the same
candidate is tradable at 50 ms and resolves-before-entry at 250 ms and 1 s.

Realized lag is reported per cell, because it is the event clock's own answer to
the question a time horizon would have assumed away.

**Adverse selection stays on its own line.** The midpoint drift between
`t_decision` and `t_arrival` is the part of the predicted move that happened
without you. It is not folded into slippage, because if the edge is gone by the
time you can act, the report should say exactly that.

---

## 3. The trading rule — frozen before any economic result

A prediction is not a trade, and every choice needed to convert one into the
other is fixed in the plan module before an outcome exists.

### Primary: the cost hurdle

> Take the trade only when `|predicted bps|` exceeds the round-trip cost
> **observable at decision time** — half-spread in, half-spread out, plus the
> frozen per-share fee schedule.

This is deliberately *not* a tuned threshold, and that distinction is the reason
it was chosen. The hurdle is quoted by the market at the decision instant and
uses no information about what happens afterwards, so there is no number in it
that could have been fitted to make the answer come out well. It also asks the
economically correct question directly: is the model's own predicted edge bigger
than what the book is charging to round-trip right now?

### Secondary: the discovery decile

Trade when `|predicted bps|` is at or above the 90th percentile of
`|predicted bps|` **measured on the discovery dates only**. Declared now so a
second rule exists without being invented later. It is a secondary family and is
never promoted.

### Direction, size, fills

- Long if `ŷ > 0`, short if `ŷ < 0`.
- 100 shares, marketable on both legs.
- Entry walks the ask upward, exit walks the bid downward, level by level,
  through at most 10 levels of **displayed** liquidity.
- **A trade that cannot fill is not a trade.** It is recorded with a reason
  (`insufficient_displayed_liquidity`) and contributes to no return. It is never
  assumed to have filled somewhere worse, and never silently dropped.

There is no hidden-liquidity model. Inventing one that helps would be exactly
the sort of assumption this stage exists to avoid; under-counting available size
is conservative, over-counting is not.

Taker on both legs, because the horizons are seconds. A passive entry that does
not fill is not a cheaper version of this strategy — it is a different strategy
with its own fill-probability model.

---

## 4. Fee model — June 2025 rates, three schedules

The research sessions are **June 2025**. The v2 module froze 2026-dated
constants, which would have priced twenty 2025 sessions against rates that did
not yet exist — and worse, charged a non-zero Section 31 fee through a period in
which that rate was **zero**.

`FEE_SCHEDULE_VERSION = june-2025-historical-v1`, window `2025-06-01 … 2025-06-30`.
`assert_session_dates_covered()` refuses any date outside it, in either
direction.

| Component | Primary `retail_june_2025` | `…_with_cat_passthrough` | `direct_member_june_2025_stress` |
|---|---|---|---|
| Commission | $0 | $0 | $0 |
| Exchange take fee | $0 | $0 | $0.0030/share |
| **SEC Section 31** | **$0.00/M** (eff. 2025-05-14) | $0.00/M | $0.00/M |
| FINRA TAF (sale leg) | $0.000166/share, cap $8.30 | same | same |
| CAT | excluded, **unverified** | $0.000022/share | $0.000022/share |
| Clearing | $0 | $0 | $0.0002/share |

Section 31 being zero across the whole window is a rate that was actually in
force, not a simplification.

### CAT is excluded, not proven zero

I could not obtain a June-2025 Alpaca *customer* fee schedule establishing
whether CAT was passed through to retail accounts at that time. So the primary
schedule excludes it and records `cat_treatment_verified: false` with a note
saying **"EXCLUDED, NOT PROVEN ZERO"** — and a separately named CAT-inclusive
retail stress case is reported alongside it.

If the primary and the CAT stress disagree about viability, the honest reading
is that the answer turns on an unverified fact that must be settled before
deployment, not that one of them is the real number.

The direct-member schedule remains a secondary stress only.

Effective dates and rates are bound into `PLAN_DESIGN_HASH`, so changing any of
them moves the hash — asserted by a test.

---

## 4a. Flagged receive timestamps — frozen rule

`F_BAD_TS_RECV` (flag 8) is the venue declining to vouch for a receive
timestamp. The frozen rule:

> A candidate whose timing window `[t_decision, t_exit]` contains **any** record
> flagged `F_BAD_TS_RECV` is excluded as `uncertifiable_timing`, counted, and
> never traded.

Exclusion rather than repair, and the reason matters. The whole of Stage 3 is an
argument about when things could be known. Substituting `ts_event`,
interpolating, or trusting the value anyway would all be *inventing timing* — and
inventing timing is the one error that silently converts a losing strategy into a
winning one. Excluding costs sample size, which is visible and survivable.
Trusting costs correctness, which is neither.

`BookReplay` records every flagged receipt instant during its single pass and
answers `timing_certified(lo, hi)` by binary search, so the exclusion is exact
rather than approximate.

---

## 5. Reconstructing the frozen Stage-2 fit — and proving it correctly

`stage2_results.json` records `chosen_alpha` but not the coefficients, so Stage 3
rebuilds them from the stored Grams. That is reproduction, not refitting: the
alpha and the training dates are recorded, so the normal equations have exactly
one solution and it is the one Stage 2 solved. The fit is performed **once**,
from the sixteen discovery+validation dates.

**The v2 proof was wrong.** It compared the rebuilt fit against
`delta_r2(train, aggregate_confirmation_gram, alpha)`. But Stage 2 never
computed that. Reading `_gate` in the Stage-2 executor:

```python
gate["delta_r2"] = float(np.mean(values))   # values = per-date delta_R2
```

Stage 2 scored **each confirmation date separately** against the same training
Gram and took the arithmetic **mean**. The aggregate-Gram value is a different
number — it is notional-weighted across dates, the mean is not — so the old
check would have failed on a correct reproduction and passed on some incorrect
ones. A test now constructs confirmation dates with deliberately unequal row
counts and asserts the two quantities differ by more than 1e-6, so the
distinction cannot silently collapse.

The corrected proof:

1. score each confirmation date individually against the single training fit;
2. compare the **ordered** per-date values against Stage 2's recorded
   `per_date_delta_r2`;
3. compare their arithmetic mean against the recorded confirmation `delta_r2`.

Any mismatch — a per-date value, the mean, or even a differing count of
confirmation dates — is a refusal. Stage 3 does not trade a model whose
provenance it cannot demonstrate.

---

## 6. Measurements

Per survivor × latency rung × rule:

gross return bps · spread paid bps · slippage bps · adverse selection bps ·
fees bps · **net return bps** · win rate · trade count · average holding time ·
displayed liquidity · capacity · per-symbol · per-session-date · factor beta

`slippage = gross − realized − spread`, i.e. what walking the book cost beyond
the quoted touch — the part of execution cost the half-spread does not explain.

**Factor sensitivity.** Daily net return regressed on the equal-weighted
cross-symbol move over the same window. A strategy whose profit is really a
directional bet on the tape is not a microstructure edge; the residual intercept
is what survives that. Tested both ways: a strategy that *is* the tape shows
beta 1 and zero alpha; a flat 4 bps/day shows beta 0 and alpha 4.

---

## 7. The primary question and its multiplicity

> **Does a survivor remain economically positive at 250 ms after realistic
> costs?**

- **Primary family: the 250 ms rung under the primary rule, and only that.**
  One test per survivor.
- Pass requires: mean net bps **> 0**, session-clustered t ≥ 3.0 (one
  observation per session date), and survival of Benjamini-Hochberg at FDR 0.10
  over the frozen survivor count.
- Minimum **100 trades and 4 session dates** at the primary rung. Below either,
  the verdict is `not_authorized_insufficient_executable_sample`.

  This is a **third verdict, not a negative one**. "Could not be executed often
  enough to measure" and "loses money" are different findings, and collapsing
  them would be the most flattering error available. It nonetheless **fails to
  authorize** Stage 4 or paper deployment: an edge that cannot be executed
  enough to be tested cannot be deployed on the strength of that test.

  The minima were declared before any economic outcome and **may never be
  lowered afterwards** — doing so would convert an unmeasurable result into an
  authorized one by redefinition. Asserted in the plan and the tests.
- 50 ms and 1 s, and the discovery-decile rule, are **secondary** — reported in
  full, corrected separately, never promoted. A test asserts that a cell
  positive at 50 ms but negative at 250 ms yields
  `no_economically_viable_survivor`.
- **A significantly negative mean is a failure, not a discovery.** Asserted.

Lifetime effective trials carry forward to **522** (508 + Stage 2's 14). They do
not reset because this is a new stage.

A pass authorizes a **paper-trading proposal for review**. No live order, no
capital, no real money.

---

## 7a. Verdict precedence, and what it refuses to say

v3 called the family economically negative whenever any single primary cell
reached inference and none passed — which would have labelled three unmeasured
survivors as losers on the strength of one that was measured. Frozen precedence:

1. **≥1 survivor passes primary economics** → `survivor_economically_positive_at_250ms`
2. **else ≥1 frozen survivor fails the executable-sample minima** →
   `not_authorized_insufficient_executable_sample`
3. **else** → `no_economically_viable_survivor`

**An unmeasured survivor is never called economically negative.** A survivor
with no primary row at all counts as unmeasured, not as measured-and-flat.

## 7b. Authorization is not the verdict

The verdict answers *is it positive after costs*. Authorization answers *may it
proceed to paper*. The unverified June-2025 retail CAT treatment is exactly
where those come apart: a result can be scientifically positive on the primary
schedule and still not be safe to deploy, because the primary schedule excludes
a charge nobody could confirm was absent.

| | |
|---|---|
| Scientific verdict | primary family only. Neither stress may redefine or veto it. |
| Deployment | a primary-positive survivor is deployable only if it is **also** viable under the CAT-inclusive retail stress, by the same positivity and t-hurdle test |
| If none is CAT-robust | `authorizes_stage4_or_paper = false`, `deployment_blocker = "unverified_historical_cat_treatment"` |
| If ≥1 is CAT-robust | authorization proceeds **for those survivors only**, not the whole positive set |
| Direct-member stress | descriptive; controls no authorization |

Settling the June-2025 Alpaca customer fee schedule removes that blocker.
Nothing else does — and a positive result does not buy the convenient reading of
an unverified fact.

## 7c. The run, wired

`mbo_stage3 run` now performs the complete pass:

1. freeze the four survivors and verify the survivor hash;
2. reconstruct each survivor's frozen Stage-2 fit **once**, proving it against
   the recorded per-date confirmation values;
3. assert every session date falls inside the June-2025 fee window;
4. per symbol-day, build the design matrix **through the Stage-2 loader itself**
   (`_symbol_day_matrix`), so the features Stage 3 predicts on are the same
   objects Stage 2 fitted on, not a re-implementation that could drift;
5. read each survivor's own `<prefix>_available_ts_recv` for the exit instant;
6. collect every query instant and replay the certified file **once**;
7. evaluate the full grid — 4 cells × 3 rungs × 2 rules × 3 fee schedules = 72
   accumulators;
8. assemble the report, apply the precedence, then apply the CAT gate.

It is wired and it is still gated: without `--i-have-reviewed-the-design` it
refuses, and a test asserts that.

## 7d. Out-of-sample only — the correction that mattered most

v4 reconstructed the confirmation fit from discovery + validation (the first
**16** dates) and then applied it to **all 20**. Sixteen twentieths of the
economics would have been scored on the fit's own training data, and at that
ratio it would have dominated every number in the report.

`run` now evaluates the **four confirmation dates and nothing else** — the only
out-of-sample dates this fit has. A test drives `run` with a stubbed evaluator
and asserts the block passed to it is exactly `blocks["confirmation"]`, and that
no discovery or validation date appears.

**Training dates are not added to rescue sample size.** If fewer than 4 session
dates or 100 trades remain executable, the answer is
`not_authorized_insufficient_executable_sample`. Enlarging the sample by
re-admitting training dates would convert an unmeasurable result into an
in-sample one, which is worse than no answer.

## 7e. Raw input bound to the exact Stage-1 bytes

v4 guessed DBN filenames from the symbol-day stem. Stage 1 already records what
it opened, in `features/manifests/<symbol>_<date>.manifest.json` under
`source.filename` / `bytes` / `sha256`.

`resolve_raw_source` now reads that manifest, locates the file under
`--raw-dir`, and verifies **size and SHA-256** before replay. Missing,
ambiguous, size-mismatched and hash-mismatched inputs are all refused. The
SHA-256 check is not redundant with the size check: the test that matters uses
same-name, same-length, different-bytes, which a size check passes.

This binds Stage 3 to the exact bytes that produced Stage 1 rather than to a
file with a plausible name.

## 7f. Feature/label re-certification

Correct Grams say nothing about whichever `--features-dir` and `--labels-dir`
were handed to Stage 3. Before any economics:

- the feature batch manifest must declare the frozen v4 engine version,
  semantics hash and vocabulary hash, and be semantics-consistent;
- labels must align **one-for-one by cadence and sequence index** with the
  feature rows — the same check Stage-2 `grams` performs, repeated because the
  inputs are supplied separately.

## 7g. Nullable availability

A non-OK label legitimately has no resolution instant, and
`<prefix>_available_ts_recv` is nullable. Casting the column wholesale to
`int64` turns those nulls into whatever the null sentinel happens to be — a real
timestamp, arithmetically valid, silently wrong.

`event_horizon_availability` produces a value **only where the status is
`ok`**, and `None` everywhere else. Status governs: a stale non-null value under
a non-OK status is discarded too. A `None` resolution yields
`stage2_target_did_not_resolve`, never a fabricated exit.

## 7h. The two rules that were declared but not wired

- **Discovery decile.** The threshold is the frozen 90th percentile of
  `|prediction|` pooled across symbols within a cell, computed on **discovery
  dates only**. Predictions, not outcomes — no realized return and no
  confirmation row enters it, no book is replayed there, and no economics
  accumulate. Previously `decile_threshold_bps=None` meant the rule silently
  took **zero** trades, which would have been reported as "produced nothing"
  rather than "never ran".
- **Common factor.** Frozen definition: the equal-weighted mean across symbols,
  per session date, of each symbol-day's first-to-last midpoint return on the
  `50ev` cadence, in basis points. A symbol-day with no usable midpoints is
  omitted rather than counted as zero — a missing observation is not a flat one.
  It is now actually passed into `summarize`.

## 7i. No subset peeking

`--limit` is removed from `run`. A separate `diagnose` command takes it and
reports **provenance, candidate counts and no-trade reasons only** — no return,
no win rate, no verdict, and it never calls `assemble_report`. A test inspects
the executable body to enforce that.

## 8. Tests — 116 cases

| Area | What is pinned |
|---|---|
| Causality | only decision/arrival/exit instants are consulted; arrival is strictly after decision; later rungs arrive strictly later |
| Adverse selection | a 10¢ jump before arrival is charged as ~10 bps against a long, and *for* a short |
| Replay | a book improvement at `ts_recv` 3000 is invisible at 2999 and visible at 3001, against a real `MboBook` |
| Replay integrity | a stream whose `ts_recv` goes backwards is **refused**, not silently mis-filled |
| Fill model | VWAP walks levels correctly both sides; the level budget is enforced; insufficient liquidity yields no trade rather than a worse fill |
| Cost hurdle | exceeds the quoted spread; rises with a wider spread; identical under two different futures |
| Fees | hand-computed against the schedule; Section 31 and TAF follow the **sale** leg for both longs and shorts; a flat round trip loses exactly the fees |
| Survivor freeze | taken from `confirmation.passed` only — a near miss with a spectacular discovery t is not a survivor; the set must hash to `bea300ba…` |
| Event-time exit | `next_change` and `next_2_changes` exit on their **own** frozen Stage-2 target; two-change holds strictly longer than one-change for the same decision; realized lag matches the label column |
| No clock horizon | `evaluate_candidate` has no `horizon_ns` parameter — asserted by signature inspection |
| Missed opportunity | resolution at or before entry arrival yields `horizon_resolved_before_entry`; the same candidate is tradable at 50 ms and not at 250 ms / 1 s |
| Fee schedules | retail charges no exchange remove fee, no commission, no CAT; stress is strictly more expensive; both versioned, dated and flagged for verification; the primary family is the retail schedule only |
| Bad timestamps | a flagged instant inside the window makes it uncertifiable — boundary-exact, including a window ending one nanosecond before and starting one after; no reorder buffer exists |
| June-2025 fees | Section 31 is $0.00/M across all three schedules with effective date 2025-05-14; the window refuses 2025-05-30 and 2026-06-02 alike; rates and dates are bound into the design hash |
| CAT | excluded but flagged unverified, never claimed proven zero; a named CAT-inclusive retail stress exists and is strictly more expensive |
| Fit reproduction | per-date values reproduce in order; their mean reproduces the recorded confirmation `delta_r2`; the mean and the aggregate-Gram value are proved to differ; mismatched value, mismatched date count, and mismatched mean each refuse |
| Insufficient sample | an unmeasurable family yields `not_authorized_insufficient_executable_sample`; **a mixed family with any unmeasured survivor is insufficient, not negative**; only all-four-measured-and-losing is negative; a missing primary row counts as unmeasured; a pass still beats an unmeasured sibling |
| Authorization | a primary-positive set that dies under CAT keeps its verdict but sets `deployment_blocker`; a CAT-robust survivor authorizes for itself only; the direct-member stress cannot block a CAT-robust positive; no positive verdict authorizes nothing |
| Wiring | the grid is 72 accumulators; query instants cover every rung and nothing else; unusable rows contribute none; `predict` applies beta without rescaling; `run` is still gated |
| Out-of-sample | `run` evaluates exactly `blocks["confirmation"]`; no discovery or validation date reaches an accumulator; `run` rejects `--limit` |
| Raw provenance | the file comes from the manifest, not the stem (a realistic `xnas-itch-20250602.mbo.dbn.zst` beside a decoy `AAPL_2025-06-02…`); missing, ambiguous, size-mismatched and same-size-different-bytes inputs each refuse |
| Re-certification | a stale engine version, semantics hash or vocabulary hash each refuse; inconsistent extraction refuses; short and shuffled label sequences refuse |
| Nullable labels | nulls stay `None`; a non-null value under a non-OK status is discarded; a `None` resolution yields no trade |
| Decile | matches the frozen quantile; ignores non-finite; unwired takes zero trades and wired admits them; calibration block is discovery and uses no outcomes |
| Common factor | equal-weighted across symbols; a symbol-day with no midpoints is omitted not zeroed; first-to-last in bps; the run passes it into `summarize` |
| Diagnostic | its body contains no `assemble_report`, `net_return_bps`, `verdict` or `win_rate` |
| Refusals | no results file; no survivors; survivor count ≠ declared; reconstructed fit disagreeing with the record |
| Family | primary is 250 ms only; a negative mean cannot pass; BH denominator is the frozen survivor count |

**470 passed, 3 skipped** across the whole MBO suite; ruff clean.

---

## 9. What is deliberately *not* wired up

`mbo_stage3 run` refuses unless `--i-have-reviewed-the-design` is passed, and
even then raises `NotImplementedError` naming the reviewable components. The
brief said to stop after producing the design and implementation for review, so
the components exist and are tested individually but are not joined into a
one-command economic pass until you approve the design.

**Resolved since v2.**

1. **`ts_recv` ordering.** No reorder buffer, per your direction: Databento
   guarantees per-symbol `ts_recv` monotonicity, so a violation means a corrupt
   file rather than an ordering convention to accommodate. The hard refusal
   stays, and a test asserts no reorder logic exists. `F_BAD_TS_RECV` exclusion
   stays; timestamps are never repaired.
2. **Fee rates.** Now June-2025 historical, with Section 31 at $0.00/M and the
   effective dates bound into the plan hash.
3. **Thin samples at 250 ms.** Now a named verdict rather than an open question.

**Still open, and genuinely uncertain:** whether retail CAT was passed through
in June 2025. The primary schedule excludes it without claiming it was zero, and
the CAT-inclusive retail stress is reported alongside. If those two disagree
about viability, that unverified fact has to be settled before anything is
deployed.

---

## 10. Governance

- No Stage-1 feature, Stage-2 cell, label, horizon or model was changed.
- No refitting, no alpha re-selection, no new signal, no feature selection.
- No threshold searched against an economic outcome; both rules frozen and
  hashed in the plan module before any economic number exists.
- No event horizon converted into a numeric duration; there is no parameter
  through which one could be.
- No receive timestamp flagged `F_BAD_TS_RECV` trusted.
- No live or paper order. No broker client is importable from this code.
- Fee rates are declared for the June-2025 window and flagged for verification;
  retail CAT treatment is explicitly unverified rather than assumed.
- No economic result exists yet.
