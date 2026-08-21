# Stage 4.1 — IAG-v2 Raw-MBO Frozen Design

**Version:** `tier1_stage41_iag_v2_raw_mbo`
**Date:** 2026-08-21
**Class:** frozen pre-outcome design. No economic outcome computed or viewed.
**Base:** `stage41-iag-v1` @ `604d5ceed8683dd754ed3dfc251e2bc8230d07bc`

```
contains_strategy_outcome       = false
contains_post_decision_return   = false
contains_pnl                    = false
effective_trials_before         = 531
effective_trials_after          = 531      (design consumes no trial)
stage_4_2_reveal_would_move     = 531 -> 532
authorizes_paper_or_live        = false
```

---

## 1. IAG-v1 formal retirement

```
IAG_v1 verdict = insufficient_executable_or_statistical_sample
cause          = feature-resolution mismatch / insufficient qualifying supply
```

| Fact | Value |
|---|---|
| candidate isolated + L3-covered events | 502 |
| PRIMARY eligible | 3 events / 3 sessions |
| FALLBACK eligible | 3 events / 3 sessions |
| `economic_run_authorized` | false |

Binding failures: 292/502 missed the frozen feature-row requirement (median 8
`200ev` rows against a floor of 20; median 31.5 `50ev` rows against 40); only
186/502 had unambiguous persistent direction (`not_persistent` 219,
`zero_net_flow` 68, `cadence_disagreement` 29); 30 had insufficient baseline.

**No economic outcome was viewed. No bps result exists. No profit or loss result
exists. The ledger remains 531.**

IAG-v1 failed at *measurement and supply*, not at an economic test. Lowering the
row floors after seeing these counts and calling it the same specification would
be threshold-shopping, so v1 is retired rather than tuned.

**IAG-v2 is a NEW exploratory measurement specification.** It is not a
confirmation of v1, and it cannot become one: the June-2025 sample has been used
before.

---

## 2. Task 1 — raw MBO semantics audit

Read from `mbo_book_validator.py` and `mbo_feature_engine.py`, not inferred from
field names.

### 2.1 Actions

| Action | Book effect | `side` means |
|---|---|---|
| `A` ADD | inserts a resting order; with `F_TOB` **replaces the whole side** | resting side |
| `C` CANCEL | reduces the named order by `size`, clamped at 0; removes at 0 | resting side |
| `M` MODIFY | changes price and/or size of the named order | resting side |
| `R` CLEAR | wipes the book | `N` |
| `T` TRADE | **book-neutral** | **aggressor side** |
| `F` FILL | **book-neutral** | **resting side — the opposite of the aggressor** |
| `N` NONE | book-neutral | — |

`BOOK_NEUTRAL_ACTIONS = {T, F, N}`. XNAS normalizes one displayed execution as
**`T` → `F` → `C`**, all three sharing a sequence and a quantity: the `T`
reports the trade, the `F` names the resting order, and the `C` is the book
update *caused by* the execution.

### 2.2 The eight questions

**Q1 — what does `side` mean on `T`?** The aggressor. `side=B` is a buy
aggressor, `side=A` a sell aggressor, `side=N` unsignable (auctions, trades
against non-displayed or implied orders, off-exchange prints, and sources that
do not disseminate a side).

**Q2 — can `T` be used as aggressor direction directly?** **Yes.** This is
already the certified Stage-1 interpretation.

**Q3 — the existing certified logic.** `mbo_feature_engine._accumulate`: `T`
with `side=B` adds to `buy_aggressor_volume`, `side=A` to
`sell_aggressor_volume`, `side=N` to `unclassified_trade_volume`. `F` never
touches aggressor volume. The Stage-1 v3 correction records why: v1 signed both
`T` and `F`, which double-counted *and* inverted, because a fill's side is the
resting side. **Reading a fill's side as an aggressor side is wrong twice over.**

**Q4 — can `C`/`M` be attributed to bid vs ask causally?** **Yes.** Every `A`,
`C`, `M` is refused with `INVALID_SIDE_FOR_ACTION` unless `side ∈ {A, B}` before
it is applied, and that side names the resting order's side. Directional
liquidity accounting is therefore certified, which is exactly what the
aggregated Stage-1 counters could not provide.

**Q5 — how must `MODIFY` be decomposed?** From `_apply_modify`, four cases:

| Case | Book effect | Directional accounting |
|---|---|---|
| price changed | detach from old price, reinsert at new | withdrawal of `old_size` at old price; addition of `new_size` at new price |
| same price, `new_size > old_size` | loses priority, back of its own level | addition of `new_size − old_size` |
| same price, `new_size < old_size` | keeps priority | withdrawal of `old_size − new_size` |
| order unknown | treated as an `A` | addition of `new_size` |

A side change is recorded as `MODIFY_CHANGED_SIDE` and becomes detach + add.

**Q6 — where is the book safe to inspect?** **Only at completed `F_LAST`
records.** A native event runs from just after the previous `F_LAST` to the next
`F_LAST` inclusive; inside it the touch is transient. Stage 1 learned this
twice — every finite `absorption_ratio` came out at exactly 1.0 when classified
on the book-neutral `F` record, and `queue_persistence` compared two `None`
touch prices as equal.

**Q7 — snapshot versus genuine additions.** Databento opens a session with an
`R` clear carrying `F_SNAPSHOT`, then `A` records also carrying `F_SNAPSHOT`
that reinsert resting orders in priority order. Those adds are *book state*, not
order events. XNAS full-session files instead begin `sequence=0 action=R side=N
order_id=0` with no snapshot flag — provably empty. Certified initialization is
therefore `formal_snapshot` **or** `known_empty_clear`; anything else is
`unknown` and every downstream count is suspect.

**Q8 — timing flags requiring fail-closed exclusion.** `F_BAD_TS_RECV` (8): any
occurrence inside a candidate's window makes its timing uncertifiable, and
`BookReplay.timing_certified(lo, hi)` already answers this. `F_MAYBE_BAD_BOOK`
(4) is treated the same way inside the observation window. Substituting
`ts_event`, interpolating, or trusting a flagged instant would be inventing
timing.

### 2.3 Feasibility determination

> **Raw MBO supports both causal signed aggression and side-specific liquidity.**
> `T` names the aggressor directly and `A`/`C`/`M` carry a validated resting
> side. IAG-v2 can be measured directly. **Proceed.**

---

## 2.4 The state-selection rule — `S(t)`, binding everywhere

Every book state this design refers to is written `S(t)` and means exactly one
thing:

> **`S(t)` = the state captured at the LATEST coherent `F_LAST` whose
> `ts_recv` is less than or equal to `t`.**

- **Never nearest-in-time.** A state 5 ms after `t` is not `S(t)` even if it is
  closer than the one 40 ms before.
- **Never a state after `t`.** No record with `ts_recv > t` may contribute to
  `S(t)` in any way.
- **Never a mid-native-event reading.** Only completed `F_LAST` states are
  coherent; inside a native event the touch is transient (§2.2 Q6).
- **Fail closed.** If no coherent state at or before `t` exists, or the state
  that exists is not two-sided, `S(t)` is **undefined** and the consumer refuses
  rather than substituting a neighbour.

Because the scan proceeds in non-decreasing `ts_recv`, `S(t)` is simply "the
last coherent state recorded when the first record with `ts_recv > t` arrives".
The implementation freezes it at that instant and never revisits it.

This rule binds, without exception:

| Use | State |
|---|---|
| pre-news reference depth | `S(t0)` |
| end-of-window depth and spread | `S(t_obs_end)` |
| lambda start midpoint | `S(t0)` |
| lambda end midpoint | `S(t_obs_end)` |
| baseline tile endpoint | latest coherent state **inside** the tile; a tile holding none is dropped |
| baseline tile start midpoint | `S(tile_start)` |
| M2 observation | `S(t_obs_end)` |
| M3 observations | `S(t0)`, the in-window minimum, and `S(t_obs_end)` |

**No future midpoint may be accessed anywhere in the qualification path.**

---

## 3. What is preserved unchanged from IAG-v1

News population; `t0 = known_at`; 60-minute same-symbol quiet period; certified
symbol/instant MBO coverage; observation interval `[t0, t0+120s]`; four 30-second
persistence quarters with ≥3 agreeing; causal prior-only baseline of ≥500
non-overlapping 120-second tiles; 25th/75th percentile levels with the 50th
fallback; 100-event and 15-session floors; 15-minute PRIMARY horizon; 5m/30m
secondary diagnostics only; 12.0 bps gross hurdle; day-clustered `t ≥ 3.0`; one
selected specification only; Stage 4.2 would be trial 531 → 532.

12 bps is a **predeclared gross opportunity hurdle** — $6 on $5,000, $12 on
$10,000, $14.40 on $12,000 — **not expected profit**, and not inspected now.

---

## 4. What changes, and why

| v1 | v2 | Reason |
|---|---|---|
| ≥20 `200ev` + ≥40 `50ev` feature rows | **removed** | caused the resolution mismatch |
| direction from two feature cadences that must agree | **one raw measurement** | v1 needed two cadences because it had no direct measurement; `cadence_disagreement` (29) disappears by construction |
| depth from `{bid,ask}_depth_10` feature columns | same quantity, read from `MboBook` at coherent `F_LAST` | native resolution |
| cancellation as a side-agnostic regime variable | **side-specific**, execution-excluded | raw `A`/`C`/`M` carry a validated side |

No new economic concept is added. The eight concepts (A–H) are unchanged; only
their measurement moves to raw.

---

## 5. Raw-data quality gates (replacing the row floors)

A candidate is measurable only if all hold:

| # | Gate | Justification |
|---|---|---|
| G1 | certified initialization: `formal_snapshot` or `known_empty_clear` | a reconstruction from unknown state is not a book |
| G2 | raw coverage spans the window: first `ts_recv ≤ t0` and last `ts_recv ≥ t_obs_end` | the window must actually be observed |
| G3 | no `F_BAD_TS_RECV` and no `F_MAYBE_BAD_BOOK` in `[t0, t_obs_end]` | flagged timing is never repaired |
| G4 | no fatal reconstruction defect recorded for the session | the validator's own verdict |
| G5 | two-sided book at the `t0` anchor state and at the `t_obs_end` state | a one-sided book has no midpoint and no impacted-side depth |
| G6 | at least one coherent `F_LAST` inside the window | otherwise no state exists to read |

**No minimum raw-record or trade count is imposed.** The only count-like
requirement is *derived*, not added: the persistence rule needs ≥3 quarters
carrying non-zero same-sign signed flow, which structurally requires at least
three signable trades. That is a restatement of the direction rule, not a
separate gate, and it is not tunable.

---

## 6. Direction — one raw measurement

Let `W = { records with t0 ≤ ts_recv ≤ t_obs_end }`.

```
signed(r) = +size(r)  if action(r) == T and side(r) == B      (buy aggressor)
            -size(r)  if action(r) == T and side(r) == A      (sell aggressor)
             0        otherwise                               (side N; and F is never signed)

NetFlow = Σ_{r ∈ W} signed(r)
D       = sign(NetFlow)
```

Only `T` records are signed. `F` records are never signed — their side is the
resting side.

**Persistence.** Split `[t0, t_obs_end]` into four 30-second quarters, the first
three half-open and the last closed on `t_obs_end`, so no record falls in two.
Require at least **3 of 4** quarters with `sign(Σ signed) == D`. A quarter with
zero net signed flow does not agree.

**Ambiguity → no qualification**, with the reason recorded:
`zero_net_flow` (`D == 0`), `not_persistent` (<3 quarters),
`no_signable_trade` (no `T` with side `B`/`A` in the window).

**Impacted side:** `D = +1` → **ASK**; `D = −1` → **BID**.

---

## 7. Impacted-side liquidity — M2 and M3

Read at coherent `F_LAST` states only. Depth is the summed displayed size over
the **first 10 price levels** of the impacted side, matching the frozen
`{bid,ask}_depth_10` definition exactly.

`S(t)` is the state-selection rule of §2.4 — latest coherent `F_LAST` with
`ts_recv <= t`, never nearest, never after, undefined if none or one-sided.

```
S(t)   = impacted-side depth_10 of the state S(t)

D_ref  = S(t0)                                   pre-news reference
D_min  = min over coherent F_LAST states with t0 <= ts_recv <= t_obs_end
D_end  = S(t_obs_end)

depletion_ratio = D_min / D_ref                  if D_ref > 0 else undefined
recovery_ratio  = (D_end - D_min) / (D_ref - D_min)   if D_ref > D_min else 1.0
```

`D_ref` is the state when the news broke, which is the faithful raw translation
of v1's "depth at the start of the observation window" — v1's first feature row
could land up to ~15 seconds into the reaction.

```
M2:  percentile(D_end, causal baseline of impacted-side end-of-tile depth) <= 25
M3:  recovery_ratio <= 0.25
```

`recovery_ratio = 1.0` when the side never drew down, which correctly fails M3:
liquidity that never fell cannot have failed to return.

`depletion_ratio` is **reported as a diagnostic and is not a gate.** Adding it as
a third liquidity condition would be a new concept, and Task 7 asks for
translation, not re-optimization.

Baselines are held separately for ask-side and bid-side depth, so a long event is
never scored against a distribution half built from bids.

---

## 8. Cancellation and addition — side-specific

Raw MBO permits what v1's counters could not. Two rules make it causal:

**Exclude execution-driven book updates.** A native event group (delimited by
`F_LAST`) that contains any `T` or `F` record has its `C`/`M` records classified
as **execution-driven**. Those are consumption, not withdrawal, and counting them
as cancellation would count one execution twice — once as aggressive flow and
again as a withdrawal.

**Exclude snapshot records.** Any record carrying `F_SNAPSHOT` is book state, not
an order event.

For genuine (non-execution, non-snapshot) records on the impacted side, using
the MODIFY decomposition of §2.2 Q5:

```
withdrawn = Σ  cancelled size (C)
          + Σ  (old_size - new_size)   for M, same price, size decrease
          + Σ  old_size               for M with a price change

added     = Σ  size (A)
          + Σ  (new_size - old_size)   for M, same price, size increase
          + Σ  new_size               for M with a price change
          + Σ  new_size               for M naming an unknown order

withdrawal_pressure = withdrawn / (added + withdrawn)    if the denominator > 0
                      else undefined
```

This is a **directional** statistic, and it replaces v1's S1 general regime
variable. It is one statistic, not a family.

---

## 9. Replenishment

Directional, and distinct from the M3 ratio: M3 asks whether the side ended
recovered, this asks how much displayed liquidity actually came back.

```
replenished = Σ added (as in §8) on the impacted side over records with
              t0 <= ts_recv <= t_obs_end that occur at or after the F_LAST at
              which D_min was first attained
```

Reported as `replenished / max(D_ref - D_min, 1)`. The old side-agnostic
`touch_replenishment_volume` and `refill_after_execution_volume` are **not used
as directional evidence** anywhere in v2.

---

## 10. Local lambda — supporting only

```
mid(t)     = midpoint of the state S(t)          (rule of section 2.4)
mid_start  = mid(t0)
mid_end    = mid(t_obs_end)

numerator   = D * (mid_end - mid_start) / mid_start * 10_000       [bps]
denominator = D * NetFlow                                          [aggressive shares]

lambda = 1000 * numerator / denominator     [bps per 1,000 aggressive shares]
```

Undefined — and S3 simply unsatisfied, the event not disqualified — when either
midpoint is null (one-sided or empty book), `mid_start <= 0`, or
`denominator < 100` shares. No winsorization: the threshold is a rank, and
clipping would add a tunable knob.

**No midpoint at or after `t_obs_end + ε` enters lambda. No future return
anywhere.** Lambda stays supporting because it conditions on displacement that
already occurred inside the window; two of four supporting conditions are always
required, so no event can qualify on lambda alone.

---

## 11. Qualification

### Must-have — all four

| # | Condition |
|---|---|
| M1 | unambiguous persistent direction (§6) |
| M2 | `percentile(D_end) ≤ 25` |
| M3 | `recovery_ratio ≤ 0.25` |
| M4 | `percentile(absorption) < 75` |

Absorption is the volume-weighted share of executed volume whose native event
left the midpoint unchanged, classified at the `F_LAST` boundary. High
absorption means the market **is** absorbing — the opposite of an assimilation
gap — so it disqualifies. Its known bias is favourable: unclassifiable groups
count as executed but never as absorbed, so the disqualifier fires less often.

### Supporting — at least 2 of 4

| # | Condition |
|---|---|
| S1 | `percentile(withdrawal_pressure) ≥ 75` — **now directional** |
| S2 | `percentile(spread_bps at t_obs_end) ≥ 75` |
| S3 | `percentile(lambda) ≥ 75` |
| S4 | `percentile(execution intensity) ≥ 75` |

```
IAG-v2 qualifies  ⟺  M1 ∧ M2 ∧ M3 ∧ M4 ∧ (S1+S2+S3+S4 ≥ 2)
```

---

## 12. Baseline

Unchanged in construction, re-measured at raw resolution.

Tile every prior certified session for the symbol, plus the current session
strictly before `t0`, into **non-overlapping 120-second tiles** anchored at
absolute UTC multiples. Reduce each with exactly the statistics an event window
produces. Accumulate per symbol.

- prior sessions only, plus the current session strictly before `t0`
- no future session, nothing past `t_obs_end`, no other symbol
- a tile's endpoint statistics come from the **latest coherent `F_LAST` inside
  that tile** (§2.4). A tile holding no coherent state contributes nothing —
  `D_end` would be undefined, and borrowing a state from the previous tile would
  report one tile's liquidity as another's. That is the raw equivalent of v1
  dropping sparse tiles, and it is the **minimum structural requirement**, not a
  density knob.
- a tile's lambda start midpoint is `S(tile_start)`, which may legitimately come
  from an earlier tile: it is the state as it stood when the tile opened.
- **minimum 500 tiles**, else the event does not qualify

Percentile levels remain **25 / 75**, with **50** in the fallback. No cutoff is
chosen from eligible counts.

---

## 13. Specifications and the deterministic ladder

| Specification | M2 | M3 | Supporting |
|---|---|---|---|
| **IAG-v2-PRIMARY** | `pct ≤ 25` | `≤ 0.25` | ≥2 of 4 |
| **IAG-v2-FALLBACK** | `pct ≤ 50` | `≤ 0.50` | ≥2 of 4 |

```
1. Evaluate PRIMARY qualification counts only.
2. If PRIMARY events >= 100 AND sessions >= 15  ->  selected = PRIMARY
3. Else evaluate FALLBACK counts.
4. If FALLBACK events >= 100 AND sessions >= 15 ->  selected = FALLBACK
5. Else: verdict = insufficient_sample, NO economic run occurs.
```

Counts only. No post-decision return, no midpoint past `t_obs_end`, no P&L
enters the ladder. Only the selected specification may ever reach Stage 4.2; the
other is never economically evaluated. The selection is persisted and hashed
before any run, and the run re-verifies that hash.

---

## 14. Diagnose schema

Outcome-blind. Reads raw MBO only through `t_obs_end` for each candidate.

```
governance:  contains_post_decision_return = false
             contains_pnl = false
             effective_trials_before = 531
             effective_trials_after  = 531

coverage:    candidate_events
             events_with_complete_raw_coverage
             median/percentile raw_records_per_window
             median/percentile genuine_trade_records_per_window
             coherent_flast_states_per_window
             gate_failures: {certified_initialization, coverage, bad_ts_recv,
                             maybe_bad_book, reconstruction_defect,
                             one_sided_book, no_flast_state}

direction:   long, short, ambiguous
             ambiguity_reasons: {zero_net_flow, not_persistent,
                                 no_signable_trade}
             quarter_agreement_histogram: {0,1,2,3,4}

state:       baseline_sufficient, baseline_tiles percentiles
             lambda_defined, lambda_undefined_reasons
             m2_pass, m3_pass
             supporting_pass: {S1,S2,S3,S4}
             supporting_count_histogram: {0,1,2,3,4}

selection:   primary  {eligible_events, distinct_sessions, clears_floors,
                       failure_reasons}
             fallback {...} only if PRIMARY misses a floor
             selected_specification
             economic_run_authorized
             selection_record_sha256
```

The module `diagnose` imports must not expose any function that reads after
`t_obs_end`, and a structural test asserts that absence — the same guarantee
IAG-v1 carries.

---

## 15. Single-pass architecture

**One replay per raw file, never per event.**

The 60-minute quiet period guarantees that two events for the same symbol are at
least 3,600 seconds apart, while a window is 120 seconds. **At most one
observation window is open per symbol at any instant.** No interval tree is
needed — a single "current window" pointer suffices.

```
for each of the 160 symbol-day files:
    open iter_dbn_events(path)                 # streaming, never materialised
    book = MboBook()                           # certified initialization
    window = None                              # at most one, by the quiet rule
    tile  = fresh 120s tile accumulator
    for event in stream:
        book.apply(event)
        accumulate cheap per-record counters    # signed flow, A/C/M sizes, flags
        if event.flags & F_LAST:                # ONLY here is the book coherent
            settle native event (absorption classification)
            if tile boundary crossed: emit tile stats, start new tile
            if window is open: read impacted depth_10, midpoint, spread
        if ts_recv crosses the next event's t0: open that window
        if ts_recv passes t_obs_end: close window, freeze its accumulators
    fold this session's tiles into the symbol's baseline
```

Depth is computed **only** at tile boundaries and at `F_LAST` states inside an
open window — never per record. Everything else is integer accumulation.

**Reuse, not reimplementation:** `iter_dbn_events`, `MboEvent`, `MboBook`,
certified-initialization handling, `F_LAST` grouping, and
`BookReplay.timing_certified` are used as they stand. No competing MBO
reconstruction is created; no deficiency has been demonstrated that would
justify one.

**No copy of the ~8 GiB compressed dataset is made.** Files are read in place
from `/opt/keftrade-data/databento/tier1_mbo_2025-06/XNAS-20260816-LYG4BEYSTM/`.

### Cost

| Quantity | Estimate |
|---|---|
| raw records | ~562 M total, ~3.5 M per symbol-day file |
| per-record work | `MboBook.apply` + integer counters |
| depth reads | ~195 tile boundaries/session + ~1 window × its `F_LAST` states |
| baseline memory | ~195 tiles × 20 sessions × 8 symbols ≈ 31 k records, a few MB |
| per-event memory | 502 bounded accumulators, negligible |
| resident book | resting orders per symbol, tens of MB |

The empirical anchor is Stage 1: it processed **the same 562 M records** to emit
19.5 M feature rows across four cadences, and completed. IAG-v2 does strictly
less per record — no four-cadence window bookkeeping, no 59-feature emission —
so a single pass is bounded above by that run. Expect a few minutes per file and
a handful of hours single-threaded, trivially parallel across the 8 symbols.

A timing probe on one symbol-day should be run first to calibrate before
committing to the full pass.

---

## 16. Governance

No economic outcome may be read. No 5m/15m/30m horizon midpoint may be read
during design or diagnose. Stage 4.2 is not run. **The ledger remains 531.**

The June-2025 sample has been used before, so IAG-v2 is exploratory mechanism
development and cannot become untouched confirmation however good a later result
looks.

---

## 17. Open concerns

**1. Raw resolution fixes measurement, not scarcity.** Of 502 candidates, 186
had unambiguous direction and only **3** passed the full conjunction — about
1.6%. Removing the row floors unblocks the 292 row-support failures and the 29
cadence disagreements, and `zero_net_flow` should shrink because exact zero is
rare at native resolution. But if the qualification rate stayed near 1.6%, even
all 502 candidates clearing direction would yield roughly **8 events** — far
below the 100 floor.

That rate is, however, **not a reliable predictor**: it was computed from
windows holding a median of 8 feature rows. A trough taken over 8 samples is a
poor estimate of a true minimum, and `depth_last` could be ~15 seconds stale.
Both M2 and M3 are measured far more faithfully at raw resolution, and the
qualification rate could move either way. This is the central uncertainty, and
diagnose will resolve it at a cost of zero trials.

**2. `not_persistent` (219) may be partly real.** Raw MBO cannot create trades
that did not occur; a 30-second quarter with no trades has none at any
resolution. What raw *does* fix is attribution — each `200ev` feature row
covered ~15 seconds, so quarter boundaries were badly smeared. Improvement is
likely but not guaranteed, and the quarter-agreement histogram will say which.

**3. Displayed book only.** Hidden liquidity is invisible, and XNAS-only means
depletion may reflect routing to other venues rather than genuine withdrawal.
Unchanged from v1 and unfixable with this dataset.

**4. News latency unmeasured.** `known_at` is publisher-stamped and
whole-second; if the tape moved first, flow from `t0` includes reaction already
under way.

**5. Snapshot exclusion depends on the flag being present.** If a session's
opening records lack `F_SNAPSHOT` and are not a `known_empty_clear`, G1 refuses
the whole session rather than guessing.
