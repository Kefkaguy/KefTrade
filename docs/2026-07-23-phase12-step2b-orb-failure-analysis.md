# Phase 12, Step 2B — ORB v1 failure analysis

Evidence-only pass. **No ORB parameters or code were changed.** Every number
below comes from recomputing Campaign 44's exact candidates against the
exact datasets it ran, then joining the resulting trades with the feature
row at each signal — not a new simulation, not a re-run with different
settings.

## 0. Accounting provenance (read this first)

| Provenance field | Value |
|---|---|
| Code revision | `7ad4ef4` (the corrected `walk_forward_train_ratio=0.7` commit that produced Campaign 44's real results) |
| Candidate definitions | `generate_orb_candidates(max_candidates=8)` → the same 4 deterministic candidates (2 breakout-buffer levels × 2 directions) used by the real campaign, reconstructed via the identical function, not hand-copied |
| Symbols | TSLA, NVDA, AAPL, MSFT, AMD, META, GOOGL, AMZN, SPY, QQQ (all 10, both timeframes) |
| Timeframes | 15m, 30m |
| Historical range | Whatever `load_intraday_backtest_dataset` returns today for each symbol/timeframe (same call the campaign made — no date range was hand-picked) |
| Walk-forward split | `walk_forward_train_ratio=0.7`, applied identically via `run_backtest`/`walk_forward_split` |
| Fees/slippage | `fee_rate=0.001`, `slippage_rate=0.0005` (`DEFAULT_ORB_PARAMETERS`, unchanged) |
| Random seed | None used or needed — every code path here (feature computation, session assignment, strategy decision, backtest loop) is deterministic; re-running produces bit-identical output, as already proven by Step 2A/2B's determinism tests |
| Dataset provenance | Same `candles`/`intraday_features` rows Campaign 44 read (no re-sync, no data changes since the pilot) |

**Match verification**: recomputed AAPL/30m trade counts and implied final equity for all 4 candidates against the actual stored `research_campaign_jobs.result` for Campaign 44 — exact match to the cent (e.g. long/buf0.05: 36 trades, stored `final_equity=8990.78`, recomputed `10000 + Σnet_pnl = 8990.78`). Recomputed totals (2,022 trades, gross −12,675.83, net −49,601.14) match the numbers already reported after the pilot. This analysis is Campaign 44, not an approximation of it.

**Internal consistency**: `Σgross_pnl_ex_fees − Σfees = Σnet_pnl` exactly (−12,675.83 − 36,925.31 = −49,601.14 ✓) across all 2,022 trades.

**Fee double-counting audit**: read `backtester.py`'s `run_backtest` directly. An `entry_fee` variable is subtracted from every *interim* mark-to-market point while a position is open (`marked_equity = mark_to_market_equity(...) - entry_fee`) — this feeds only the informational `equity_curve`/`strategy_returns` display series. The trade's **realized** P&L is computed independently afterward: `fees = (entry_price × quantity × fee_rate) + (exit_price × quantity × fee_rate)`, `pnl = gross_pnl - fees`, and this is the only value that mutates `equity` or gets stored on the trade record. **No double counting**: the display-series fee preview and the realized trade fee are two separate computations from the same inputs, not one value applied twice.

**Session-close price source**: confirmed via Step 2A's `find_exit_index` — when the session cap binds, the exit index is the last row in `session_end_index` for that session (built by `build_session_end_index` from `intraday_features.session_date`, itself calendar-derived), and the exit price uses that bar's close. Not re-audited further here since Step 2A's tests already prove this structurally (`test_forced_exit_at_normal_session_close_when_no_stop_or_target_hit`, etc.).

**Short-side sign check**: spot-checked a TSLA short trade — entry 426.64, exit 433.67 (price rose, adverse for a short), `gross_pnl_ex_fees = -62.91`, manually recomputed as `(entry_price - exit_price) × quantity = -62.91`. Correct sign, correct direction convention.

**Conclusion: accounting is correct.** The negative result is real, not a bookkeeping artifact.

## 1. Breakdown tables

All figures: gross = `gross_pnl_ex_fees` (post-slippage price, pre-fee); net = after both fee legs; profit factor and expectancy computed on net P&L (matching this codebase's existing `calculate_metrics` convention); max drawdown is a **pooled proxy** — trades in each slice sorted by entry time into one cumulative net-P&L series, peak-to-trough drawdown of that series (not a real single-account equity curve, since each job ran its own isolated $10k account; used here only to compare *relative* volatility across slices).

### By symbol

| Symbol | Trades | Win% | Gross | Costs | Net | PF | Expectancy | Avg win | Avg loss |
|---|---|---|---|---|---|---|---|---|---|
| AAPL | 205 | 32.2% | −1,111 | 3,448 | −4,560 | 0.356 | −22.24 | 38.17 | −50.93 |
| AMD | 197 | 40.1% | −771 | 1,460 | −2,231 | 0.656 | −11.32 | 53.85 | −54.95 |
| AMZN | 206 | 42.2% | −1,510 | 2,964 | −4,474 | 0.408 | −21.72 | 35.47 | −63.53 |
| GOOGL | 193 | 31.6% | −1,425 | 2,920 | −4,345 | 0.429 | −22.51 | 53.60 | −57.69 |
| META | 188 | 30.3% | −670 | 2,660 | −3,330 | 0.545 | −17.71 | 69.85 | −55.81 |
| MSFT | 200 | 36.5% | **+381** | 2,808 | −2,427 | 0.576 | −12.13 | 45.09 | −45.03 |
| NVDA | 200 | 44.5% | **+358** | 2,572 | −2,214 | 0.668 | −11.07 | 50.17 | −60.18 |
| QQQ | 200 | 27.0% | −2,437 | 5,570 | −8,007 | 0.284 | −40.04 | 58.92 | −76.64 |
| SPY | 231 | 25.1% | −4,651 | 10,368 | −15,019 | 0.159 | −65.02 | 49.05 | −103.26 |
| TSLA | 202 | 41.1% | −839 | 2,156 | −2,994 | 0.602 | −14.82 | 54.63 | −63.27 |

Only MSFT and NVDA are gross-positive; every symbol is net-negative. **SPY and QQQ are the worst by a wide margin** — see §3/§4, this traces directly to their low intraday range-to-price ratio forcing oversized positions.

### By timeframe

| Timeframe | Trades | Gross | Costs | Net | PF |
|---|---|---|---|---|---|
| 15m | 779 | −6,849 | 13,811 | −20,660 | 0.394 |
| 30m | 1,243 | −5,827 | 23,114 | −28,942 | 0.432 |

30m has a marginally better profit factor but far higher absolute cost (more, larger-notional trades). Neither timeframe is close to viable.

### By direction

| Direction | Trades | Gross | Costs | Net | PF | Avg win | Avg loss |
|---|---|---|---|---|---|---|---|
| Long | 1,087 | −10,627 | 20,184 | −30,811 | 0.337 | 42.51 | −64.61 |
| Short | 935 | −2,049 | 16,741 | −18,790 | 0.513 | 58.36 | −64.72 |

**Shorts are meaningfully closer to gross-breakeven than longs** (−2,049 vs. −10,627 gross across a comparable trade count) — a real, worth-noting asymmetry, though both remain net-negative.

### By candidate (timeframe × direction × buffer)

| Candidate | Trades | Gross | Net | PF |
|---|---|---|---|---|
| 15m long buf0.05 | 212 | −2,761 | −6,543 | 0.292 |
| 15m long buf0.15 | 194 | −2,931 | −6,483 | 0.258 |
| 15m short buf0.05 | 196 | −436 | −3,829 | 0.545 |
| 15m short buf0.15 | 177 | −721 | −3,804 | 0.507 |
| 30m long buf0.05 | 355 | −2,086 | −8,745 | 0.402 |
| 30m long buf0.15 | 326 | −2,850 | −9,041 | 0.347 |
| 30m short buf0.05 | 307 | −1,155 | −6,717 | 0.466 |
| **30m short buf0.15** | 255 | **+263** | −4,439 | **0.550** |

`30m_short_buf0.15` is the only one of 8 candidates with a positive gross total — flagged for §4's stability check, not a conclusion by itself.

### By month

| Month | Trades | Gross | Net | PF |
|---|---|---|---|---|
| 2026-02 | 24 | +144 | −148 | 0.655 |
| 2026-03 | 246 | +1,529 | −2,785 | 0.661 |
| 2026-04 | 233 | −1,500 | −6,754 | 0.379 |
| **2026-05** | 495 | **−12,378** | −22,770 | **0.152** |
| 2026-06 | 621 | +3,663 | −6,639 | 0.693 |
| 2026-07 | 403 | −4,133 | −10,506 | 0.383 |

**May 2026 is catastrophic** (PF 0.152, −46.00 expectancy) and alone accounts for ~63% of the strategy's total net loss (−22,770 of −49,601) despite being only ~24% of trades. This looks like a regime effect (a period unusually hostile to range-breakout continuation), not a uniform structural failure across all history.

### By entry-time bucket (minutes from session open)

| Bucket | Trades | Gross | Net | PF | Win% |
|---|---|---|---|---|---|
| 0–60m (early) | 103 | −1,300 | −3,172 | 0.440 | 35.9% |
| 60–120m | 816 | −1,412 | −15,645 | 0.546 | 39.0% |
| 120–240m | 658 | −5,071 | −17,838 | 0.360 | 35.4% |
| 240m+ (late) | 445 | −4,893 | −12,946 | **0.239** | **26.7%** |

Late entries are clearly worse (lowest PF, lowest win rate) — see §2, this is directly tied to the session-close mechanism.

### By exit reason

| Reason | Trades | Gross | Net | PF | Win% |
|---|---|---|---|---|---|
| session_close | 1,536 (76.0%) | **+4,081** | −21,476 | 0.477 | 37.2% |
| stop_loss | 350 (17.3%) | −35,691 | −43,970 | 0.000 | 0.0% |
| take_profit | 136 (6.7%) | +18,935 | +15,845 | ∞ | 100.0% |

Session-close exits are **gross-positive as a group** but the fee/slippage load on 1,536 trades (25,557 in costs against only 4,081 gross) turns them net-negative. Full diagnosis in §2.

### By holding duration

| Bucket | Trades | Gross | Net | PF |
|---|---|---|---|---|
| <1h | 218 | −4,302 | −9,121 | 0.252 |
| 1–2h | 316 | −9,234 | −15,277 | 0.257 |
| 2–4h | 635 | −1,965 | −14,085 | 0.469 |
| 4h+ | 853 | +2,825 | −11,118 | 0.568 |

Shorter holds skew toward stop-outs (fast losses); longer holds skew toward session-close (mixed, gross-neutral-ish outcomes).

### By session-relative-volume bucket at entry

| Bucket | Trades | Gross | Net | PF |
|---|---|---|---|---|
| 1.0–1.5× | 1,419 (70%) | −14,447 | −42,326 | 0.329 |
| 1.5–2.0× | 347 | −2,826 | −8,224 | 0.445 |
| **2.0–3.0×** | 194 | **+3,783** | **+929** | **1.179** |
| **3.0×+** | 62 | **+815** | **+21** | **1.011** |

**This is the only net-positive subgroup found anywhere in this analysis.** Stability check in §4 — it does not clear the bar.

### By breakout-buffer level

| Buffer | Trades | Gross | Net | PF |
|---|---|---|---|---|
| 0.05 | 1,070 | −6,438 | −25,835 | 0.424 |
| 0.15 | 952 | −6,238 | −23,767 | 0.408 |

Buffer level is not a meaningful driver either way.

### Market regime

**Not available.** ORB jobs pass an empty `context_by_time` to `evaluate_candidate` (the ORB campaign wiring never computes swing-style regime context — this was a deliberate Step 2B scope decision, not an oversight, since regime classification wasn't part of the approved Step 2A/2B extension). Reporting this as a gap rather than fabricating a regime breakdown.

## 2. Session-close exit diagnosis

Of 1,536 session-close exits:

| Bucket (by gross return) | Count | % | Avg gross return | Avg holding |
|---|---|---|---|---|
| Profitable (>+0.1%) | 643 | 41.9% | +0.768% | 3.61h |
| Near-flat (±0.1%) | 222 | 14.5% | −0.005% | 3.19h |
| Losing (<−0.1%) | 671 | 43.7% | −0.624% | 3.23h |

- **48.3% were gross-profitable before costs** — essentially a coin flip, and the **average gross return across all session-close exits is +0.048%**, i.e. statistically indistinguishable from zero. The breakout has no measurable average continuation edge by the time the session ends.
- **Distance from stop/target at exit** (normalized in units of the original stop distance, R): mean **1.03R from the stop, 1.47R from the (1.5R) target**. Since every trade starts exactly 1.0R from its own stop by construction, an average of 1.03R at session-close means price has, on average, moved only **+0.03R net** over the entire remaining session — effectively no drift. It also means the average trade ends up almost as far from target (1.47R away) as it started (1.5R away) — **the target is very rarely approached, let alone reached, within the remaining session time.**
- **Late entries disproportionately end at session close**: 89.0% of "240m+" (late) entries hit session-close vs. 54.4% of "0–60m" (early) entries. This directly follows from the entry-cutoff mechanism (late entries structurally have less remaining time for a stop/target touch) — expected given the mechanism, but it means late entries are the ones most exposed to the fee drag on a near-zero-edge outcome.
- **15m vs. 30m**: session-close share is nearly identical (75.4% vs. 76.3%) — the "fewer 30m bars remain" hypothesis is **not supported**; the timeframe-aware entry cutoff (`minimum_entry_lookahead_minutes`) appears to equalize this correctly across timeframes.
- **Target distance vs. remaining time**: given the 1.47R-from-target average at session close, **yes — the 1.5R target (from `reward_risk_multiple=1.5` over a `stop_atr_multiple=1.0` range-span stop) is unrealistic for the amount of session time typically remaining after entry.** This is the clearest quantitative finding in the whole analysis: the reward target essentially assumes a trend-day continuation that occurs in only 6.7% of trades (the take-profit rate).

## 3. Cost-drag audit

| Metric | Value |
|---|---|
| Avg cost per trade (all) | $18.26 |
| Avg gross edge per trade (all) | −$6.27 |
| Cost as % of avg gross price movement per trade (median) | **37.3%** |
| Avg cost, 15m | $17.73 | Avg gross edge, 15m | −$8.79 |
| Avg cost, 30m | $18.60 | Avg gross edge, 30m | −$4.69 |
| Avg cost, long | $18.57 | Avg gross edge, long | −$9.78 |
| Avg cost, short | $17.90 | Avg gross edge, short | −$2.19 |
| Trades gross-profitable | 878 (43.4%) |
| Of those, net-unprofitable (cost flipped the sign) | **171 (19.5% of gross winners)** — avg gross profit $10.82 wiped out by an avg fee of $23.22 |

**Root cause of the cost/risk mismatch, found via the position-sizing data**: risk is sized as a fixed 1% of equity divided by `risk_per_unit` (the ATR-proxy stop distance), but fees and slippage scale with **notional**, not with risk. For symbols whose intraday opening-range span is small relative to price (SPY: `risk_per_unit` is only **0.47%** of price on average; QQQ: 0.76%), the position sizing formula compensates with a much larger position, inflating notional (SPY avg notional $22,444 vs. AAPL's $8,414) and therefore fees (SPY avg fee $44.88 vs. AAPL's $16.82) — **for the same $100 nominal risk per trade.** This is confirmed quantitatively: `risk_pct` (stop distance as % of price) correlates at **−0.70** with fees and **+0.19** with net P&L across all 2,022 trades — tighter stops reliably mean bigger fees and (weakly) worse outcomes. This is a structural interaction between ORB's ATR-proxy-based stop sizing and the simulator's notional-based cost model, not a bug in either part alone, and it directly explains why SPY/QQQ are the two worst-performing symbols.

## 4. Structural signal-defect check

- **Signal semantics, confirmed by direct measurement**: ORB v1's signal means *"price remains outside the settled boundary and every other gate currently passes,"* **not** *"first valid crossing of the boundary."* The two are not equivalent, and the data shows it: at the signal bar, long entries average **0.48 range-widths beyond** the settled high (short: 0.43 range-widths beyond the low), with 32.5%/30.4% of entries occurring more than half a range-width past the boundary (max observed: 3.5 range-widths). Nothing in the current gate ordering requires or checks "is this the first bar since the range settled where price crossed this level" — a bar can be evaluated and rejected repeatedly (e.g. on relative-volume) while price sits outside the range, and the eventual entry fires whenever volume happens to confirm, which may be well after the initial cross. This is a real, measured characteristic of the current implementation, worth fixing in any refinement, not a bug that needs a hotfix now.
- **Entry-after-extended-breakout**: directly follows from the above — roughly a third of entries are already meaningfully extended before the position even opens, which plausibly caps how much of the "move" is left to capture and contributes to the weak realized continuation seen in §2.
- **Next-bar-open gaps**: mean adverse gap is **+0.002%**, median ~0.001%, only 6.1% of signals had an adverse gap worse than 0.05% (and a nearly matching 5.5% had a favorable gap). **Not a meaningful driver** — ruled out.
- **Relative-volume confirmation**: never null among executed trades (by construction — a null relative-volume bar cannot pass the gate), average confirming value 1.33× baseline. Not weak or absent; working as designed. The volume gate's *threshold* (1.0×, i.e. barely above baseline) is loose enough that 70% of trades still cleared it at only 1.0–1.5× — see the rvol breakdown in §1, where tightening this threshold is exactly where the one positive-PF subgroup lives.
- **Directional-consumption / excessive frequency**: checked directly — **every (symbol, timeframe, direction, buffer, session) group produced exactly one trade, with zero exceptions across 2,022 trades.** The one-entry-per-session cap and per-direction consumption logic are working exactly as designed; this is not contributing to the negative result.
- **Stop geometry / target distance vs. session time**: covered in §2 and §3 — the 1.5R target is rarely reachable in the remaining session time, and the ATR-proxy stop sizing interacts badly with the notional-based cost model for low-relative-volatility symbols. These are the two dominant structural issues found.
- **Opening-range-position "far beyond the range"**: covered above (extended-breakout check) — confirmed present and measurable, moderate magnitude.

## 5. Does any narrow subgroup show repeatable positive evidence?

The session-relative-volume ≥2.0× bucket (256 trades, PF 1.18/1.01, net +929/+21) is the only net-positive subgroup found anywhere in this analysis. Checked for stability before drawing any conclusion, per instruction not to conclude from one best-performing slice alone:

- **Symbol breadth**: appears across all 10 symbols, but only 7/10 are net-positive within it (GOOGL −961, META −201, MSFT −328 remain negative even in this bucket).
- **Time breadth**: only 2 of 5 months (June +1,948, July +983) are net-positive; March is flat, **April (−1,420) and May (−502) are net-negative even within this "good" bucket** — the same May regime effect that dominates the aggregate result also hits this subgroup.
- **Candidate breadth**: present across all 8 candidates, with a consistent lean toward short-side candidates (PF 1.52–1.82) over long-side (PF 0.87–1.10) — echoes the direction asymmetry in §1.
- **Sample size**: only 256 trades total, ~15–43 per symbol, ~24–101 per month — thin per cell.

**This does not clear the bar for "repeatable positive evidence."** It is concentrated in the two most recent months, inconsistent across a third of the symbols, and each individual cell is too thin to distinguish from noise. It is a legitimate, worth-investigating hint (tighter relative-volume confirmation, further from the current 1.0× floor), not a validated edge.

## Conclusions

1. **Accounting is correct** — confirmed by exact reproduction of Campaign 44's stored results, internal gross/fee/net consistency, no double-counted fees, correct session-close price sourcing, correct short-side sign convention.
2. **Dominant reason for negative gross expectancy**: the breakout has no measurable average continuation by session close (+0.048% mean gross return on session-close exits, the majority exit type) — the 1.5R target is calibrated to a trend-continuation scenario that happens in only 6.7% of trades, and roughly a third of entries occur only after the move is already meaningfully extended.
3. **Dominant source of cost drag**: ATR-proxy stop-based position sizing combined with notional-scaled fees/slippage systematically over-sizes (and over-charges) low-relative-volatility symbols (SPY, QQQ worst by far); overall cost consumed 37% of average gross price movement per trade and flipped 19.5% of gross winners into net losers.
4. **No subgroup clears the stability bar** for repeatable positive evidence — the one candidate (relative-volume ≥2.0×) is thin, regime-concentrated (2/5 months), and inconsistent across symbols.
5. **Recommendation: archive ORB v1 as a documented negative result for its current design, with one specific, bounded, falsifiable follow-up hypothesis identified** (not executed here): tightening the relative-volume confirmation threshold materially above the current 1.0× floor, informed directly by the §1/§5 relative-volume breakdown. This is not a recommendation to modify ORB now — per instruction, no code or parameters were touched in this analysis, and any such experiment would need its own explicit approval as a separate, constrained pass.
