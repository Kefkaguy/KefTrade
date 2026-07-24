# Phase 12, Step 2B — Opening-Range Breakout v1: implementation + pilot

Scope actually implemented: the ORB v1 strategy and its wiring into the real
campaign lifecycle (`labs/intraday/strategy.py`, `labs/intraday/campaign.py`,
the two additive `run_campaign_job`/`data_readiness_for_job` seams), 19 ORB
unit tests, one production defect found and fixed by the pilot itself, and a
10-symbol × 2-timeframe pilot run through the unmodified elite gate. **No
VWAP reversion, gap fill, session momentum, or contraction/expansion** —
those remain explicitly out of scope pending review of this pass.

## Files changed

| File | Change |
|---|---|
| `apps/api/app/services/labs/intraday/strategy.py` *(new)* | `OpeningRangeBreakoutStrategy`, `OpeningRangeBreakoutState`, `DEFAULT_ORB_PARAMETERS`, `OPENING_RANGE_BREAKOUT_ARCHITECTURE` |
| `apps/api/app/services/labs/intraday/campaign.py` *(new)* | `generate_orb_candidates` (bounded 4-candidate grid), `create_opening_range_breakout_campaign`, `is_opening_range_breakout_candidate` |
| `apps/api/app/services/strategy_discovery.py` | Additive: one branch in `make_strategy_definition` for the ORB architecture marker (same pattern as the existing Phase-2-family branch); `evaluate_candidate` gained an optional `session_end_index` passthrough parameter (defaults `None`, no effect on any existing caller) |
| `apps/api/app/services/research_campaigns.py` | Additive: new sibling function `run_intraday_campaign_job` (not a branch inside the swing path — `run_campaign_job` dispatches to it only for ORB candidates); `data_readiness_for_job` checks `intraday_features` instead of `features` only for ORB candidates |
| `apps/api/tests/test_orb_strategy.py` *(new)* | 20 tests (19 required-property tests + 1 walk-forward-scale regression guard added after the pilot defect) |

**Untouched**: `backtester.py`'s core loop (no changes at all this step — Step 2A's extension point was sufficient), the swing `load_campaign_dataset`, `strategy_families.py`, `family_registry.py`, `elite_portfolio_builder.py`, every existing strategy function, every existing campaign-creation function.

## Strategy design

`OpeningRangeBreakoutStrategy` satisfies `StrategyProtocol` from Step 2A: `execution_constraints = ExecutionConstraints(flat_by_session_close=True)`, a `reset()` that replaces `self.state` with a fresh `OpeningRangeBreakoutState`, and a `__call__` with the one unchanged 4-argument signature.

- **Session identity**: read only from `feature["session_date"]`; a change triggers `self.state = OpeningRangeBreakoutState(current_session=new_date)`, never inferred from a UTC date.
- **Entry gates, in order**: opening range settled (`minutes_from_open >= opening_range_minutes`, read from the feature row itself, not recomputed) → opening-range levels present → session entry budget (`maximum_entries_per_session`) → timeframe-aware entry cutoff (`max(minimum_entry_lookahead_minutes(timeframe), minimum_minutes_before_close_for_entry)` against `minutes_to_close`) → relative-volume confirmation (`session_relative_volume >= minimum_session_relative_volume`) → breakout beyond buffer, direction-aware, gated by `long_breakout_taken`/`short_breakout_taken` unless `allow_repeat_breakout_direction`.
- **Volatility unit**: no dedicated ATR field exists in `intraday_features` for 15m/30m yet, so `breakout_buffer_atr`/`stop_atr_multiple` scale the settled opening-range span (`opening_range_high - opening_range_low`) rather than adding new volatility computation to the strategy/simulator boundary.
- **Everything else — fills, fees, slippage, sizing, stop/target scan, session-close forcing — is the unmodified Step 2A/2A-generic simulator.**

## A real defect the pilot caught (and fixed)

The first pilot run (all 80 jobs) came back with `number_of_trades == 0` on every single job — a signal of a structural bug, not a negative result. Root cause: `DEFAULT_ORB_PARAMETERS["walk_forward_train_ratio"]` was `1.0`. `run_backtest`'s walk-forward split only activates when `len(rows) >= 80`; every ORB unit test deliberately stays under that threshold (documented in the test file) to isolate the property under test, which is exactly why this was invisible in 20 passing unit tests. On real datasets (thousands of rows), a ratio of `1.0` makes `split_index == len(rows) - 1`, leaving a **1-bar validation window**, and the `>=50`-bar warmup (`i = max(start_index, 50)`) starts past the end of that window — the loop body never executes.

Fixed to `0.7` (matching the existing swing `BASE_PARAMETERS` convention), added a 400-row multi-session regression test that exercises the walk-forward split the way real data does, committed separately, redeployed, and reran the pilot from a fresh campaign-job batch (the parameter change altered the candidates' canonical hash, so the corrected run's 80 jobs are distinct rows from the first, buggy 80 — nothing was deleted, per this project's archive-don't-erase convention).

## Pilot campaign (post-fix)

- **Campaign 44**, `research_core_ten` universe, 10 symbols × 2 timeframes (15m, 30m) × 4 candidates (2 breakout-buffer levels × 2 directions, long-only and short-only rather than "both" so results report per-direction cleanly) = **80 jobs**.
- Ran to completion through the unmodified real lifecycle: `create_opening_range_breakout_campaign` → real `research_campaigns`/`research_campaign_jobs` rows → `run_research_campaign_batch` (the same batch driver production workers use) → `run_intraday_campaign_job` → `evaluate_candidate` → the unmodified honest elite gate → `finalize_research_campaign`.
- **Job execution**: 80/80 completed cleanly, 0 errors, 0 deferrals. Campaign status: `completed`.
- **Setups vs. trades**: 2,022 setups generated, **2,022 actual trades opened — zero silently skipped** (no invalid-geometry rejections in real data, verified by recomputing setup counts directly against the same candidates/datasets the campaign used).
- **Trades by timeframe/direction**:

  | Timeframe | Direction | Total trades | Jobs with ≥1 trade / jobs |
  |---|---|---|---|
  | 15m | long | 406 | 20/20 |
  | 15m | short | 373 | 20/20 |
  | 30m | long | 681 | 20/20 |
  | 30m | short | 562 | 20/20 |

  Both directions and both timeframes generated setups and trades in every one of the 20 (symbol × buffer-level) combinations — no silent direction or timeframe gap.
- **Exit reasons across all 2,022 trades**: `session_close` 1,536 (76.0%), `stop_loss` 350 (17.3%), `take_profit` 136 (6.7%). Session-close forcing is doing most of the work at these default parameters (single-shot-per-session entries with a 1.5R target often just sit until the session ends) — expected given `maximum_entries_per_session=1` and no explicit takeaway on how tight the default target is; not itself a defect.
- **Cost impact**: gross P&L (pre-fee/slippage) across all trades was **-12,675.83**; net P&L (post-fee/slippage) was **-49,601.14** — fees and slippage account for **36,925.31** of additional loss, confirming costs are being applied and materially affect outcomes (this pilot is not a profitability claim either way; both figures are negative).
- **Lifecycle honesty**: all 80 jobs were evaluated by the exact same unmodified elite gate every swing candidate goes through — **0 promotions**. Rejection reasons: `weak_profit_factor` (76), `fails_in_unknown` (75), `poor_expectancy` (75), `insufficient_trades` (25, jobs with fewer than the 30-trade median-consistency floor), `high_drawdown` (12). No threshold was loosened, inspected, or bypassed to produce a different outcome.

## What this pilot does and doesn't show

- **Shows**: the full pipeline — intraday dataset loading, session-aware entry/exit, structural flat-by-session-close, cost accounting, and the existing elite gate — works end to end for a real intraday strategy family, for both supported timeframes and both directions, without any simulator-level branching on strategy identity.
- **Does not show**: that Opening-Range Breakout v1 is profitable. It isn't, at these default parameters, on this data — and per instruction, no threshold was loosened and no result was reframed to suggest otherwise.

## Known limitations

- `breakout_buffer_atr`/`stop_atr_multiple` scale the opening-range span, not a true ATR — documented in the strategy module's docstring; a dedicated intraday volatility field is a candidate for a future `intraday_features` addition, not something this step invents ad hoc.
- The campaign job-storage layer does not persist raw trade lists (an existing, pre-Step-2B convention shared with every swing campaign) — the exit-reason and cost-impact figures above were computed by recomputing (not re-simulating differently) with the exact same candidates and datasets the campaign ran, since `run_backtest`/`evaluate_candidate` are pure functions of their inputs.
