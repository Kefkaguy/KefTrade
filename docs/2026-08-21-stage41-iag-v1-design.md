# Stage 4.1 — IAG-v1 Frozen Design

**Version:** `tier1_stage41_iag_v1`
**Amendment:** `r1` (review amendments 1-7 applied 2026-08-21)
**Date:** 2026-08-21
**Class:** frozen pre-outcome design. No economic outcome computed or viewed.

```
contains_strategy_outcome       = false
contains_post_decision_return   = false
contains_pnl                    = false
effective_trials_before         = 531
effective_trials_after          = 531      (Stage 4.1 design consumes no trial)
stage_4_2_reveal_will_move      = 531 -> 532
authorizes_paper_or_live        = false
```

---

## 0. Findings that change the specification

Task 1 required reading the real Stage-1 implementation before drafting the
rule. Five of the proposed features do not mean what their names suggest.

### 0.1 Four liquidity features are side-agnostic and cannot carry direction

`mbo_feature_engine.py:617-631` accumulates **both** book sides into a single
counter:

```python
for side, before_size, after_size, before_px, after_px in (
    ("B", before.bid_size, after.bid_size, before.bid_price, after.bid_price),
    ("A", before.ask_size, after.ask_size, before.ask_price, after.ask_price),
):
    if before_size > 0 and after_size == 0:
        for cadence in self.cadences:
            self._windows[cadence.name].queue_depletion_events += 1
```

There is no `bid_depletion_events` / `ask_depletion_events`. The same holds for
`touch_replenishment_volume`, `touch_replenishment_events`,
`refill_after_execution_volume` and `depletion_followed_by_quote_move`.

`refill_after_execution_volume` is the subtlest: it *consults*
`_execution_seen_at_touch[event.side]` when deciding whether to increment, so it
is side-aware in its **gate** but side-blind in its **output**. Reading it as
directional would be wrong in a way that no test on its values would reveal.

**Consequence.** Components B ("same-direction liquidity depletion") and C
("weak replenishment") **cannot be built from the features named in the brief.**
A rule using them would silently mix depletion on the informed side with
depletion on the opposite side — the two states the mechanism must distinguish.

### 0.2 Cancellation/addition pressure cannot be made directional either

`add_count`, `add_volume`, `cancel_count`, `cancel_volume`, `cancel_add_ratio`
and `cancel_volume_ratio` are all side-agnostic. Component D as written —
"cancellation/addition pressure **consistent with direction**" — is not
constructible. It is demoted to a non-directional stress condition and labelled
as such.

### 0.3 `mean_touch_depth` averages both sides together

`touch_depth_sum += (after.bid_size + after.ask_size) / 2` — L1 only, both sides
merged. Not directional, and not a depth ladder.

### 0.4 `modify_count` is identically zero

Established in the Stage-1 all-160 diagnostic: 0 on all 19,484,064 rows. Unusable.

### 0.5 What *is* directional

| Feature | Directional? | Why |
|---|---|---|
| `buy_aggressor_volume`, `sell_aggressor_volume` | **yes** | trade names its aggressor side |
| `signed_trade_volume` | **yes** | `buy − sell`; **positive = buy pressure** |
| `aggressor_imbalance` | **yes** | signed / classified volume, ∈ [−1, 1] |
| `bid_depth_5/10`, `ask_depth_5/10` | **yes** | separate snapshot ladders per side |
| `bid_size_l1`/`ask_size_l1`, `bid_levels`/`ask_levels` | **yes** | per side |
| `midpoint`, `spread`, `spread_bps` | n/a | snapshot state |

**The side-separated depth ladders are the only directional liquidity
measurement in the certified vocabulary.** IAG-v1 is therefore built on
`{bid,ask}_depth_10` for components B and C, not on the depletion/replenishment
counters.

### 0.6 Window semantics — the biggest implementation trap

`_windows[cadence.name].reset(nominal_ts)` runs after **every** emission
(`:833`). So:

- **Counters** (`signed_trade_volume`, `execution_volume`, `add_count`,
  `cancel_volume`, `trade_count`, …) are **per-window increments that reset**.
  Summing them across rows in an interval reconstructs the interval total
  exactly, with no double counting.
- **Snapshots** (`midpoint`, `spread_bps`, `bid_depth_10`, `bid_levels`,
  `resting_orders`, …) are **state at that row**. Summing them is meaningless;
  they must be reduced by first / last / min / max.

Every aggregation below states which kind it is.

### 0.7 Event cadences are not time cadences

`50ev` and `200ev` advance every 50 / 200 book events, so window *duration*
varies with activity. `execution_intensity` already divides by
`window_seconds`; raw counts do not, so on an event cadence `trade_count` is
"trades per 200 events", already normalised by event count rather than by time.

### 0.8 Accepted limitation: the news clock

`known_at` is publisher-stamped and whole-second, with no measured
publisher-to-tape latency. If the tape moved before `known_at`, flow measured
from `t0` includes reaction that had already begun. This is the same clock
Stage 3.6 used and the brief mandates it; it is recorded as a limitation, not
solved.

---

## 1. Population and event clock (frozen)

Inherited unchanged from the corrected Stage 4.0 v2 population.

| Item | Value |
|---|---|
| Sample | certified June 2025, XNAS.ITCH MBO |
| Symbols | AAPL, AMD, CMCSA, CSCO, INTC, MSFT, NVDA, TSLA |
| Sessions | 20 certified |
| Story identity | `COALESCE(content_hash, article_id)` |
| `t0` | `intraday_news_articles.known_at` |
| Isolation | 60-minute same-symbol quiet period, counting **all** prior stories |
| L3 requirement | temporal coverage for that **symbol at that instant** |
| Candidate pool | **502** isolated events with certified coverage |

**No post-news price-shock threshold.** Stage 3.6 established that selecting on
the initial move is not this mechanism.

---

## 2. Observation window (frozen)

```
t_obs_end = t0 + 120 seconds
t_decision = t_obs_end
```

**120 seconds is accepted**, on microstructure grounds rather than outcomes.
Estimated row density from the Stage-1 batch (19,484,064 rows over 160
symbol-days across four cadences) is ≈1.25 s per `200ev` window and ≈0.31 s per
`50ev` window, giving roughly **96 rows at 200ev and 384 at 50ev** inside 120 s
— ample for a persistence test across four 30-second quarters, and short enough
that the state still describes assimilation rather than a new regime.

That density is an *estimate*. `diagnose` must measure it and **fail closed**
below a predeclared minimum of **20 rows at 200ev and 40 rows at 50ev** in the
window; events below it are not qualified.

Every qualification and direction variable is fully available at `t_obs_end`:
all use rows with `t0 ≤ feature_available_ts_recv ≤ t_obs_end`.

---

## 3. Direction (frozen)

Let `W(c)` be rows of cadence `c` with `t0 ≤ feature_available_ts_recv ≤ t_obs_end`.

```
NetFlow(c) = Σ_{r ∈ W(c)} signed_trade_volume(r)        [counter → sum]
D(c)       = sign(NetFlow(c))
```

**Aggregation is `sum`**, predeclared, and correct because the counters reset
per window.

Direction qualifies only when **all** hold:

1. `D(50ev) ≠ 0` and `D(200ev) ≠ 0`
2. `D(50ev) == D(200ev)` → `D` is that common sign
3. **Persistence:** split `[t0, t_obs_end]` into four 30-second quarters. On
   `200ev`, at least **3 of 4** quarters have `sign(Σ signed_trade_volume) == D`.
   A quarter with zero net flow does not agree.

Ties, zeros, cadence disagreement, or fewer than 3 agreeing quarters →
**ambiguous → the event does not qualify.** No trade, no state.

The impacted side follows from `D`: buy pressure (`D=+1`) consumes **asks**;
sell pressure (`D=−1`) consumes **bids**.

```
depth_impacted(r) = ask_depth_10(r) if D=+1 else bid_depth_10(r)
```

---

## 4. Causal baseline (frozen)

Thresholds are percentiles of a **per-symbol, strictly prior** distribution.

**Construction.** Tile every prior certified session for that symbol, plus the
current session up to `t0`, into **non-overlapping 120-second windows**. Reduce
each tile with exactly the same statistics as an event window. Accumulate per
`(symbol, cadence)`.

- Prior sessions only, plus the current session strictly before `t0`.
- No future session, no observation past `t_obs_end`, no other symbol.
- One machinery for every statistic, so counters and snapshots are reduced
  identically in baseline and event.
- **Minimum baseline size: 500 tiles.** Below it the event does not qualify —
  early first-session events legitimately fail rather than being scored against
  a thin distribution.

Frozen quantile levels: **25th** and **75th**. Two levels, chosen before any
count or outcome. No other cut is evaluated.

---

## 5. Local lambda (frozen)

Closed-window price impact per unit same-direction flow. Uses only
`[t0, t_obs_end]`.

```
mid_first = midpoint at the first row with feature_available_ts_recv ≥ t0      (200ev)
mid_last  = midpoint at the last  row with feature_available_ts_recv ≤ t_obs_end (200ev)

numerator   = D × (mid_last − mid_first) / mid_first × 10_000        [bps, signed by D]
denominator = D × NetFlow(200ev)                                     [shares, same-direction net]

lambda = 1000 × numerator / denominator      [bps per 1,000 shares]
```

| Item | Rule |
|---|---|
| Units | bps per 1,000 shares of same-direction net flow |
| Zero/low flow | `denominator < 100` shares → **lambda undefined**; supporting condition S3 is not satisfied. Does not disqualify the event outright. |
| Missing midpoint | either endpoint null (one-sided/empty book) → lambda undefined, same handling |
| Winsorization | **none.** The threshold is a rank, which is robust by construction; clipping would add a free parameter |
| Direction | incorporated in both numerator and denominator, so lambda is positive when price moved *with* the flow |
| Availability | `feature_available_ts_recv` of the last row used, which is ≤ `t_obs_end` |

### Frozen causal endpoint semantics

Both midpoint observations are drawn from the `200ev` rows of the observation
window and **both must be available no later than `t_obs_end`**:

```
start row = argmin feature_available_ts_recv  over rows with t0 <= feature_available_ts_recv <= t_obs_end
end   row = argmax feature_available_ts_recv  over rows with t0 <= feature_available_ts_recv <= t_obs_end
mid_first = midpoint(start row)      mid_last = midpoint(end row)
```

Selection is by the availability clock, not by row order, so an out-of-order
file cannot silently change which endpoints are used. `start row` and `end row`
may be the same row only if the window holds one row, which the 20-row minimum
already excludes.

**Fail closed.** If either endpoint cannot be established — no qualifying row,
a null midpoint from a one-sided or empty book, or `mid_first <= 0` — lambda is
**undefined**, S3 is not satisfied, and the event is not disqualified on that
account alone.

**No midpoint after `t_obs_end` may enter lambda.** A structural test asserts
the qualification path never reads a price at or beyond `t_horizon`.

> **Why lambda is supporting and never mandatory.** Its numerator is a
> directional midpoint displacement *inside* the observation window, so lambda
> explicitly conditions on price displacement having already occurred. That is
> causal and permitted, but making it a must-have would turn IAG-v1 into a
> momentum filter wearing a liquidity name. It stays as one of four supporting
> conditions, any two of which suffice, so no event qualifies on lambda alone.
> Stage 4.2 measures displacement strictly **after** `t_obs_end`; the two never
> share an observation.

---

## 6. Qualification rule (frozen)

### Must-have — all four

| # | Condition | Formal |
|---|---|---|
| M1 | Unambiguous persistent direction | §3, all three clauses |
| M2 | Impacted-side depletion | `pct(depth_impacted at t_obs_end) ≤ 25` |
| M3 | Weak replenishment | `recovery ≤ 0.25` |
| M4 | Not being absorbed | `pct(absorption_ratio) < 75` |

**M2 and M3 use side-separated depth only.**

```
D = +1  ->  impacted liquidity side = ASK  ->  depth_impacted(r) = ask_depth_10(r)
D = -1  ->  impacted liquidity side = BID  ->  depth_impacted(r) = bid_depth_10(r)

first     = depth_impacted at the earliest 200ev row in W (by availability clock)
last      = depth_impacted at the latest   200ev row in W (by availability clock)
trough    = min over W(200ev) of depth_impacted
recovery  = (last − trough) / (first − trough)   if first > trough else 1.0
```

`queue_depletion_events`, `touch_replenishment_volume`,
`touch_replenishment_events`, `refill_after_execution_volume`,
`depletion_followed_by_quote_move`, `cancel_add_ratio` and every other
side-agnostic output are **forbidden as evidence of directional depletion or
directional replenishment**. The plan module encodes that as a refusal, and a
structural test asserts none of them reaches M2 or M3.

`recovery = 1.0` when no drawdown occurred, which fails M3 — correctly, since
liquidity that never fell cannot have failed to replenish.

M4 uses `absorption_ratio`, volume-weighted over `W(200ev)`. High absorption
means executions left the midpoint unchanged — the market **is** absorbing, the
opposite of an assimilation gap, so it disqualifies. Its known bias is
favourable: unclassifiable execution groups count as executed but never as
absorbed, so the ratio is biased **down**, making this disqualifier fire less
often rather than more.

### Supporting — at least 2 of 4

| # | Condition | Note |
|---|---|---|
| S1 | **general cancellation / liquidity-withdrawal stress**: `pct(cancel_volume_ratio) ≥ 75` | Describes a stressed book regime. **Not** directional withdrawal — the counters are side-agnostic (§0.2), so this must never be described as cancellation pressure "consistent with direction". |
| S2 | `pct(spread_bps at t_obs_end) ≥ 75` | snapshot |
| S3 | `pct(lambda) ≥ 75` | undefined lambda ⇒ not satisfied |
| S4 | `pct(execution_intensity) ≥ 75` | already per-second |

```
IAG-v1 qualifies ⟺ M1 ∧ M2 ∧ M3 ∧ M4 ∧ (S1+S2+S3+S4 ≥ 2)
```

### Supply risk, and the one predeclared fallback

The conjunction is restrictive. A rough independent-percentile estimate from 502
candidates — direction ≈0.7, M2 ≈0.25, M3 ≈0.5, M4 ≈0.75, 2-of-4 ≈0.5 — lands
near **30 events**, well under the 100-event floor. States correlate, so the
true count may be considerably higher, but under-supply is a live possibility.

Deciding what to do *after* seeing the count would be threshold-shopping. So a
single fallback is declared **now**:

| Specification | M2 | M3 | Supporting |
|---|---|---|---|
| **IAG-v1-PRIMARY** | `pct ≤ 25` | `≤ 0.25` | ≥2 of 4 |
| **IAG-v1-FALLBACK** | `pct ≤ 50` | `≤ 0.50` | ≥2 of 4 |

### Deterministic selection ladder (frozen)

```
1. Evaluate PRIMARY eligibility counts only.
2. If PRIMARY events >= 100 AND PRIMARY sessions >= 15:
       selected_spec = PRIMARY
3. Else evaluate FALLBACK eligibility counts.
4. If FALLBACK events >= 100 AND FALLBACK sessions >= 15:
       selected_spec = FALLBACK
5. Else:
       verdict = insufficient_executable_or_statistical_sample
       NO economic run occurs.
```

- The ladder consumes **counts only**. No post-decision return, midpoint past
  `t_obs_end`, or P&L enters the selection.
- **Only `selected_spec` may ever reach Stage 4.2.** Economic outcomes are never
  computed for both specifications; the non-selected one is never evaluated.
- `selected_spec` is **persisted and hashed by `diagnose` before** any economic
  run. `run` re-reads that record, re-verifies its hash, and refuses if the
  selection is absent or altered.
- This remains **one** Stage-4.2 primary economic trial, because selection used
  no outcome.
- No third variant exists.

**The 100-event / 15-session floor does not move after counts are seen.**

---

## 7. Stage 4.2 economic test (predeclared, not yet run)

**Primary question.** Does the IAG direction experience directional midpoint
displacement **after** the observation window?

```
t_decision = t_obs_end = t0 + 120s
t_horizon  = t_decision + 15 minutes

gross_bps = D × (midpoint(t_horizon) − midpoint(t_decision)) / midpoint(t_decision) × 10_000
```

This is **gross directional midpoint displacement**. The word *abnormal* is
deliberately not used: no benchmark-adjustment or market-model formula is
frozen here, so there is no baseline against which anything could be called
abnormal. It is a raw directional displacement.

**It is not P&L and not executable profit.** No spread crossing, no fees, no
fills, no position sizing.

**Primary horizon: 15 minutes.** Single-name news is typically assimilated over
roughly 10–30 minutes; 15 sits mid-range, is long enough for a several-bps
displacement to be physically possible, and stays intraday. Chosen from the
mechanism, not from any outcome. Feature resolution (sub-second) deliberately
does not constrain it.

**Secondary diagnostics — 5 min and 30 min.** Diagnostics only. They are not
additional primary trials, and they **can never rescue a failure of the
15-minute primary**: if the 15-minute result misses the hurdle, the verdict is
`no_IAG_mechanism` regardless of what any secondary horizon shows.

Midpoints resolve from the certified feature parquet, requiring L3 coverage
through `t_horizon`; events without it are counted and excluded, never
back-filled.

---

## 8. Economic hurdle (predeclared)

| Component | bps | Source |
|---|---|---|
| Desired net | 8.0 | stated objective |
| Execution allowance | 4.0 | Stage 3.6 measured 1.774 + 0.014 ≈ 1.79 bps round trip, **×2 stress** for depleted-liquidity states, rounded up |
| **Primary gross hurdle** | **12.0** | net + allowance |
| Stretch gross hurdle | 19.0 | 15 net + 4.0 |

Stage 3.5/3.6 execution evidence is used **only** to size the cost budget. It
does not touch the alpha state, thresholds, direction rule or horizon.

### What the numbers mean in cash

| Notional | 12 bps gross (required opportunity) | 8 bps net (eventual target) |
|---|---|---|
| $5,000 | $6.00 | $4.00 |
| $10,000 | $12.00 | $8.00 |
| $12,000 | $14.40 | $9.60 |

The intended decomposition is `12 bps gross − 4 bps conservative execution
allowance = 8 bps eventual net`.

**Neither number is an expected return.** Both are predeclared requirements.
Stage 3.6's mechanism produced +0.41 bps gross; 12.0 bps is roughly thirty
times that, and nothing here predicts it will be met.

---

## 9. Inference (predeclared)

Events cluster by trading day, so inference is **session-clustered**, reusing
`clustered_t` from `mbo_stage3_executor` rather than a second implementation.

Reported: eligible events; distinct sessions; mean and median directional gross
displacement; session-clustered t; 95% CI from the clustered standard error on
`t`-distribution with `n_sessions − 1` df; per-symbol and per-session tables.

**Per-symbol and per-session results are diagnostic only.** They may not remove
any symbol or session after the outcome. No cherry-picking AMD or TSLA, no
excluding INTC on Stage-3.6 grounds.

Sample gate, unchanged: **≥100 events and ≥15 sessions.**

---

## 10. Verdict (predeclared)

| Verdict | Condition |
|---|---|
| `IAG_gross_mechanism_detected` | sample gate passed **and** mean gross ≥ **12.0 bps** **and** clustered `t` ≥ **3.0** |
| `no_IAG_mechanism` | sample gate passed, either condition fails |
| `insufficient_executable_or_statistical_sample` | sample gate fails |

Passing authorises **only Stage 4.3 execution simulation**. It does not
authorise paper or live trading, and no broker code is written at any point.

---

## 11. Trial governance

Stage 4.1 consumes **no** trial: 531 → 531. Stage 4.2's reveal moves **531 →
532**, once, for the single primary specification selected in §6.

If it fails, thresholds, observation window, direction rule, feature subset and
holding horizon are **not** re-tuned on the June sample and re-labelled a
confirmation. Any follow-up on this sample is exploratory and must say so.

---

## 12. Implementation plan

Four files, mirroring the Stage 3.6 structure that has now survived three
review rounds.

| File | Contents |
|---|---|
| `app/services/stage41_iag_plan.py` | frozen constants, `assert_frozen_design()` hashing this document, feature-semantics registry with the side-agnostic findings encoded as refusals |
| `app/services/stage41_iag_executor.py` | baseline tiling, window reduction, direction, lambda, qualification, verdict |
| `app/cli/stage41_iag.py` | `plan`, `semantics`, `diagnose`, `run` |
| `tests/test_stage41_iag_executor.py` | full suite |

**`diagnose` — outcome-blind, runnable now.** Verifies the design SHA and the
Stage-4.0 population; measures rows-in-window against the 20/40 minimum;
builds baselines and reports their sizes; evaluates M1–M4 and S1–S4; reports
qualifying counts per specification, per symbol, per session, and the
PRIMARY-versus-FALLBACK selection. Computes **no** midpoint after `t_obs_end`.
An AST test asserts no function reads a price past the observation cutoff.

**`run` — separately gated, one-time.** Requires
`--i-have-reviewed-the-design`, refuses if `diagnose` has not been run against
the same design hash, refuses a second execution against an existing result
file, and has no `--limit`. It is the only place `t_horizon` exists.

Structural tests will pin: no side-agnostic feature used directionally; no
observation after `t_obs_end` in qualification; `t_horizon` unreachable from
`diagnose`; percentile levels exactly {25, 75}; floors at 100/15; hurdle at
12.0; ledger 531→532 only in the run report.

---

## 13. Open concerns

1. **Supply.** Estimated ~30 qualifying events under PRIMARY (§6). The declared
   fallback handles it without post-hoc tuning, but the sample may be thin even
   then.
2. **Lambda conditions on in-window displacement** (§5). Causal and permitted,
   stated explicitly.
3. **Directional liquidity is only measurable via depth ladders** (§0.1), so
   IAG-v1 measures *displayed-book* depletion. Hidden liquidity and venue
   rotation are invisible; XNAS-only means depletion may reflect routing rather
   than withdrawal.
4. **News latency is unmeasured** (§0.8).
5. **Row-density estimate is unverified** (§2) and must be confirmed by
   `diagnose`.
