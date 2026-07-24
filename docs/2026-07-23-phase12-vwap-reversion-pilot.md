# Phase 12 — VWAP Reversion v1: implementation + pilot

Second Intraday Lab family, built on the unmodified Step 2A/2B infrastructure
(`StrategyProtocol`, `ExecutionConstraints(flat_by_session_close=True)`, the
structural session-close exit cap, the intraday dataset loader). No changes
to the simulator, ORB, or swing behavior.

## Strategy design

Mean-reversion around `session_vwap`: enters when a bar's close deviates
`entry_deviation_threshold` or more from VWAP (long when extended below,
short when extended above). No dedicated ATR field exists for 15m/30m yet
(the same gap ORB documented), so the volatility unit here is the bar's own
distance from VWAP at signal time (`abs(close - session_vwap)`) — a
dataset-native measure distinct from ORB's opening-range-span unit, chosen
for this family specifically rather than reusing ORB's.

Default `entry_deviation_threshold` (0.6%) and the campaign grid's second
level (1.0%) come from the real distribution of `|distance_from_session_vwap|`
across the research-core symbols: median ~0.33%, p75 ~0.6%, p90 ~1.0% (both
timeframes) — not a guess. `walk_forward_train_ratio=0.7` was set correctly
from the first commit of this family, learned directly from the ORB pilot's
defect (a ratio of 1.0 silently zeroes every real job once `len(rows) >= 80`).

## Generalized infrastructure (before it became a second copy-pasted branch)

- `INTRADAY_STRATEGY_FACTORIES` registry in `labs/intraday/strategy.py`: `make_strategy_definition` (strategy_discovery.py) does one dict lookup instead of growing an if/elif chain.
- `is_intraday_lab_candidate` (labs/intraday/campaign.py) replaces the ORB-only `is_opening_range_breakout_candidate` at both dispatch points in `research_campaigns.py` (`run_campaign_job`'s dataset routing, `data_readiness_for_job`'s feature-table check).
- `_create_intraday_campaign` is one shared campaign-creation helper both `create_opening_range_breakout_campaign` and the new `create_vwap_reversion_campaign` call.
- The Intraday Lab overview endpoint (`GET /research/intraday/overview`) now computes campaigns/jobs/trades/pilot/timeframe-breakdown/sample-jobs generically per architecture marker, so both families (and any future one) share one set of queries.

## Tests

20 new tests (`test_vwap_reversion_strategy.py`) mirror every property ORB was held to: threshold enforcement (no setup below it, first eligible setup once cleared), long/short correctness, relative-volume confirmation, session reset from `session_date` (not UTC), max-entries-per-session, directional consumption (isolated from the entries budget), late-entry rejection, no-lookahead, unchanged next-bar-open execution, normal and short/early-close-style forced session-close exits, fees/slippage effect, deterministic reruns, and — included from the first commit this time, not bolted on after a defect — a realistic-scale (400-row, multi-session) walk-forward regression guard. Full suite: 468 passed (1 pre-existing, unrelated Windows temp-directory `PermissionError`).

## Pilot (Campaign 46)

10 symbols × 2 timeframes (15m, 30m) × 4 candidates (2 deviation-threshold levels × 2 directions) = 80 jobs, all completed cleanly on the first run (no walk-forward defect this time).

- **1,542 trades**, 79/80 jobs produced at least one trade.
- Avg profit factor per job: **0.457**; avg expectancy: **-22.13**.
- Exit reasons: `session_close` 935 (60.6%), `stop_loss` 349 (22.6%), `take_profit` 258 (16.7%).
- Gross P&L (pre-fee/slippage): **-13,354.10**. Net P&L: **-33,683.96**. Cost impact: **20,329.86**.
- Rejection reasons: `weak_profit_factor` (73), `poor_expectancy` (72), `fails_in_unknown` (64), `insufficient_trades` (41), `high_drawdown` (5).
- **0 promotions** through the unmodified elite gate — no threshold touched.

## Result

Not profitable at these defaults on this data, same directional conclusion as
ORB v1, though with a somewhat less severe cost/gross ratio (cost is ~1.5x
gross loss here vs. ~2.9x for ORB) and a lower session-close share (61% vs.
76%) — VWAP Reversion's stop/target geometry resolves via stop or target
more often than ORB's did. Archived as a documented negative result; the
roster and Intraday Research Lab UI reflect this with live, DB-derived
numbers, not hardcoded text.
