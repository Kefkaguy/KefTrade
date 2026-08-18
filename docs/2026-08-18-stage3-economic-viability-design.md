# Stage 3 — Economic viability design, for review (v2)

**Status: design and implementation corrected. No economic outcome computed. No
order placed of any kind.**

| | |
|---|---|
| Stage-3 plan | `tier1_stage3_economics_v2` |
| `PLAN_DESIGN_HASH` | `874292555a9e136294f36c45a69c402a8448213652cdf9a1aa867638b5529ff3` |
| `SURVIVOR_HASH` | `bea300ba23327075909e37e36864feee6087dc85a5d55108cb53a615c7046f00` |
| Superseded | `tier1_stage3_economics_v1` (`f6878f66…`), before any economic outcome |

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

## 4. Fee model — two schedules, versioned and dated

The v1 module froze a single schedule that billed a Nasdaq **$0.0030/share
remove fee straight to the account**. That is wrong for the intended broker: a
commission-free retail account is not charged the venue's per-share access fee,
and assuming it is would overstate the cost of this strategy. Assuming the
reverse for a direct member would understate it. So there are two, they are
reported side by side, and neither is silently "the real one".

`FEE_SCHEDULE_VERSION = 2026-08-18`, `effective_from = 2026-01-01`, and both
carry `rates_require_verification: true`.

| Component | Primary — intended broker (retail) | Conservative — direct exchange member (stress) |
|---|---|---|
| Commission | $0 | $0 |
| Exchange take fee | **$0** (absorbed by the broker) | $0.0030 / share, both legs |
| SEC Section 31 | passed through, sale leg | passed through, sale leg |
| FINRA TAF | passed through, sale leg, capped | passed through, sale leg, capped |
| CAT | **$0** — assessed on industry members, not itemised per retail execution | $0.000022 / share |
| Clearing | $0 | $0.0002 / share |

**The primary question is answered on the retail schedule.** The direct-exchange
schedule is a stress case and a secondary family: a cell positive only under the
stress schedule cannot answer the primary question, and one negative there cannot
veto it. Tested both ways.

### On the rate values themselves

Section 31 is reset by SEC order and TAF/CAT are amended by rule filing, so the
numbers in the module are **declared values for this run, not asserted current
truth**. Every artefact carries the schedule version and effective date, and the
verification note requires the rates in force on each evaluated session date to
be confirmed before any result is relied on. I would rather the artefact say
"this is the rate I used, check it" than quietly imply I verified something I did
not.

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

## 5. Reconstructing the frozen Stage-2 fit

A gap I hit while building this: **`stage2_results.json` records `chosen_alpha`
but not the coefficients.** Stage 3 needs them to produce a prediction.

The resolution is reproduction, not refitting. Stage 2 recorded which alpha it
chose and which dates it trained on, and the per-date Grams are stored, so the
normal equations have exactly one solution and it is the same one Stage 2
solved. Nothing is re-selected, nothing is re-tuned.

Because "it reproduces" is easy to claim and easy to get wrong, it is checked:
the rebuilt coefficients are scored on the confirmation dates and the resulting
`delta_R2` must reproduce the recorded value. If it does not, the Grams and the
results file do not belong to the same run, and Stage 3 **refuses** rather than
trading a model it cannot account for.

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
- Minimum 100 trades and 4 session dates, or inference is withheld rather than
  reported thin.
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

## 8. Tests — 56 cases

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
| Bad timestamps | a flagged instant inside the window makes it uncertifiable — boundary-exact, including a window ending one nanosecond before and starting one after |
| Refusals | no results file; no survivors; survivor count ≠ declared; reconstructed fit disagreeing with the record |
| Family | primary is 250 ms only; a negative mean cannot pass; BH denominator is the frozen survivor count |

**410 passed, 3 skipped** across the whole MBO suite; ruff clean.

---

## 9. What is deliberately *not* wired up

`mbo_stage3 run` refuses unless `--i-have-reviewed-the-design` is passed, and
even then raises `NotImplementedError` naming the reviewable components. The
brief said to stop after producing the design and implementation for review, so
the components exist and are tested individually but are not joined into a
one-command economic pass until you approve the design.

**Open questions for your review.**

1. The replay clocks on `ts_recv`, since that is when a participant could know a
   record. It requires `ts_recv` to be non-decreasing through the file and
   refuses otherwise. If the certified files turn out to be ordered by `ts_event`,
   the replay needs a bounded reorder buffer — I would rather add that
   deliberately than have it discovered by a silent wrong answer.
2. The Section 31 / TAF / CAT values are declared, not verified. Before any
   result is relied on, they need confirming against the schedules in force
   across the twenty evaluated session dates.
3. At `next_change` horizons, `horizon_resolved_before_entry` may consume most
   candidates at 250 ms. That is a real finding rather than a defect, but it
   means the primary family could fail the 100-trade minimum and return
   "inference withheld" rather than a verdict. Worth deciding now whether that
   outcome is acceptable as an answer.

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
- No economic result exists yet.
