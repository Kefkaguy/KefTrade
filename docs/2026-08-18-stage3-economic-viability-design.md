# Stage 3 — Economic viability design, for review

**Status: design and implementation complete. No economic outcome computed. No
order placed of any kind.**

---

## 0. A blocker you need to know about first

The brief says to freeze *four* survivors from `stage2_results.json`. **That file
does not exist, and Stage 2B has never been run.**

| | |
|---|---|
| `stage2_results.json` | absent — searched the whole repo |
| `reports/tier1_features` | absent |
| `.dbn.zst` raw files | absent on this machine |
| Feature rebuild at engine v4 | not started |

So there are no four survivors, and I will not invent them. The last four
commits were all pre-outcome corrections; the pipeline still needs: rebuild
features at v4 → `mbo_stage2 grams` (spine-certifies the labels) → `mbo_stage2
run` → *then* survivors exist.

What I have built instead is the Stage-3 design and implementation
**parameterized over whatever `stage2_results.json` eventually declares**. This
is not a workaround, it is the stronger ordering: a rule written before the
survivors are known cannot have been shaped around them, and the plan records
`declared_before_survivors_were_known: true` as a permanent fact rather than a
claim. If the survivor count is not four, `freeze` refuses.

If Stage 2 produces **no** survivor, Stage 3 does not run at all, and the module
says so in the refusal rather than degrading into a smaller test.

---

## 1. Files

| Component | File |
|---|---|
| Frozen plan, hashes, fee schedule | `apps/api/app/services/mbo_stage3_plan.py` |
| Fill model, economics, replay, inference | `apps/api/app/services/mbo_stage3_executor.py` |
| CLI (`plan` / `freeze` / gated `run`) | `apps/api/app/cli/mbo_stage3.py` |
| Tests | `apps/api/tests/test_mbo_stage3_executor.py` |

`PLAN_DESIGN_HASH = f6878f6608002f1363982a4b38e7de719b460e34aa0c371db65aac4a93a83221`

The executor refuses to compute anything if that hash moves. It contains no
broker client and no code path that could reach one.

---

## 2. The three instants

This is the whole stage. Everything else is bookkeeping.

```
t_decision = feature_available_ts_recv          nothing may be decided earlier
t_arrival  = t_decision + latency               the order reaches the book
t_exit     = t_decision + horizon + latency     the flat-out order arrives
```

A fill that peeks one microsecond past `t_arrival` turns a losing strategy into
a winning one and nothing downstream would notice, so most of the test suite is
about *when* information may be used rather than what the numbers come out to.

**Adverse selection is reported on its own line.** The midpoint drift between
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

## 4. Fee schedule — frozen constants, public sources

| Component | Value | Applies to |
|---|---|---|
| Nasdaq taker fee | $0.0030 / share | both legs |
| Clearing | $0.0002 / share | both legs |
| SEC Section 31 | $27.80 per $1M sold | sale leg only |
| FINRA TAF | $0.000166 / share, capped $8.30 | sale leg only |

Declared before any economic outcome. One caveat recorded in the plan: the
Section 31 rate resets periodically, so the value here is the declared constant
for this run and a re-run against a different rate must say so rather than
silently re-price.

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

## 8. Tests — 38 cases

| Area | What is pinned |
|---|---|
| Causality | only decision/arrival/exit instants are consulted; arrival is strictly after decision; later rungs arrive strictly later |
| Adverse selection | a 10¢ jump before arrival is charged as ~10 bps against a long, and *for* a short |
| Replay | a book improvement at `ts_recv` 3000 is invisible at 2999 and visible at 3001, against a real `MboBook` |
| Replay integrity | a stream whose `ts_recv` goes backwards is **refused**, not silently mis-filled |
| Fill model | VWAP walks levels correctly both sides; the level budget is enforced; insufficient liquidity yields no trade rather than a worse fill |
| Cost hurdle | exceeds the quoted spread; rises with a wider spread; identical under two different futures |
| Fees | hand-computed against the schedule; Section 31 and TAF follow the **sale** leg for both longs and shorts; a flat round trip loses exactly the fees |
| Survivor freeze | taken from `confirmation.passed` only — a near miss with a spectacular discovery t is not a survivor |
| Refusals | no results file; no survivors; survivor count ≠ declared; reconstructed fit disagreeing with the record |
| Family | primary is 250 ms only; a negative mean cannot pass; BH denominator is the frozen survivor count |

**392 passed, 3 skipped** across the whole MBO suite; ruff clean.

---

## 9. What is deliberately *not* wired up

`mbo_stage3 run` refuses unless `--i-have-reviewed-the-design` is passed, and
even then raises `NotImplementedError` naming the reviewable components. The
brief said to stop after producing the design and implementation for review, so
the components exist and are tested individually but are not joined into a
one-command economic pass until you approve the design.

**Open question for your review:** the replay uses `ts_recv` as the clock, since
that is when a participant could know a record. It requires `ts_recv` to be
non-decreasing through the file and refuses otherwise. If the certified files
turn out to be ordered by `ts_event` instead, the replay needs a bounded
reorder buffer — I would rather add that deliberately than have it discovered by
a silent wrong answer.

---

## 10. Governance

- No Stage-1 feature, Stage-2 cell, label, horizon or model was changed.
- No refitting, no alpha re-selection, no new signal, no feature selection.
- No threshold searched against an economic outcome; both rules frozen in the
  plan module with `declared_before_any_economic_outcome: true`.
- No live or paper order. No broker client is importable from this code.
- No economic result exists yet.
