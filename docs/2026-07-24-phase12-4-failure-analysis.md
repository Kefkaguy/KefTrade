# Phase 12.4 — Intraday Portfolio-Wide Failure Analysis and Research Allocation

**Date:** 2026-07-24
**Scope:** Trade-level root-cause analysis of the six Phase 12.3 intraday families (Gap Fill, Session Momentum,
Intraday Trend Pullback, EMA Trend Continuation, Opening Fade, VWAP Trend Continuation). ORB v1 and VWAP Reversion v1
remain archived and unmodified; they are used only as historical comparison points.

**Analysis service:** `apps/api/app/services/labs/intraday/phase_analysis.py`, exposed at
`GET /research/intraday/phase-12-4?campaign_id=<id>`. 21 unit tests in `apps/api/tests/test_phase_12_4_analysis.py`.

## 0. Why this analysis required a new campaign, not just querying Campaign 47

Before writing any analysis code, the actual contents of `research_campaign_jobs.result` were audited. Campaign 47
turned out to store **aggregate metrics only** (`profit_factor`, `expectancy_per_trade`, `win_rate`, `gross_profit`/
`gross_loss` — which are themselves *net-of-fee* sums despite the name — `max_drawdown`, `sharpe_ratio`,
`number_of_trades`, `average_holding_time_hours`, plus regime-bucketed rollups). The individual trade list that
`run_backtest()` computes in memory (entry/exit time, exit reason, entry/exit price, quantity, stop/target levels,
fees, slippage) was discarded before the job's result was ever written to the database. MFE/MAE were never computed
anywhere in the codebase, not even transiently.

This meant the majority of the questions this phase must answer (exit-reason mix, MFE/MAE, true pre-fee gross P&L,
entry timing, position sizing, monthly stability) were **not computable from Campaign 47 as it stood** — not a small
gap. Per the phase's own instruction not to rerun Campaign 47 merely to fill missing fields without first reporting
the gap, this was reported to the user before any code was written. The user chose to add trade-level persistence and
launch a new, separately-versioned re-run rather than accept an aggregate-only analysis.

**What was added** (all additive, no strategy logic touched):

- `database/migrations/046_intraday_trade_evidence.sql` — new `research_campaign_trades` table.
- `backtester.py`'s trade dict gained `gross_pnl`, `fees`, `slippage_cost`, `risk_per_unit`, MFE/MAE (amount, R-multiple,
  bars-to-extreme), and the entry bar's own session feature snapshot (`entry_minutes_from_open`,
  `entry_minutes_to_close`, `entry_session_relative_volume`, `entry_gap_percent`). Every existing field is unchanged;
  this is purely additive to the trade record.
- Trade persistence is wired only into the intraday campaign job path (`persist_intraday_job_trades` in
  `research_campaigns.py`), gated by the existing generic `is_intraday_lab_candidate` check — swing campaigns and
  Campaign 47's own rows are completely unaffected.
- **Campaign 50** ("Phase 12.4 trade-evidence re-run") relaunched the *exact same* 6 families, same candidate
  generators, same parameters, same 10-symbol/15m+30m/long+short grid as Campaign 47 — 480 jobs, 0 failed. A
  `campaign_label` parameter was added to `create_intraday_campaign` specifically so this relaunch would get its own
  `campaign_key`/`campaign_id` instead of silently returning Campaign 47 unchanged via `ON CONFLICT(campaign_key) DO
  UPDATE` (this happened on the first launch attempt — no new jobs were created, the endpoint just handed back
  Campaign 47's existing row — and was caught and fixed before any real analysis ran).
- Campaign 47's own rows were never read, written, or altered by anything in this phase.

Everything below comes from Campaign 50's real trade rows (11,109 trades across the 6 families) unless explicitly
marked otherwise.

## 1. Family-by-family performance decomposition

| Family | Trades | Gross PF | Net PF | Gross expectancy | Net expectancy | Win rate | Payoff | Cost impact on expectancy |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Gap Fill | 1,076 | 0.58 | 0.40 | -14.68 | -24.56 | 34.9% | 0.74 | -67% |
| Session Momentum | 2,438 | 0.84 | 0.51 | -3.88 | -15.03 | 37.5% | 0.85 | -288% |
| Intraday Trend Pullback | 1,440 | 0.76 | 0.41 | -9.62 | -31.60 | 36.5% | 0.72 | -228% |
| EMA Trend Continuation | 2,885 | 0.51 | 0.15 | -29.15 | -102.27 | 29.9% | 0.36 | -251% |
| Opening Fade | 2,019 | 0.59 | 0.30 | -13.92 | -31.99 | 32.8% | 0.62 | -130% |
| VWAP Trend Continuation | 1,251 | 0.90 | 0.58 | -2.80 | -15.33 | 37.9% | 0.94 | -448% |

**Every family's gross profit factor is below 1.0.** None of the six families had a real, cost-free directional edge
across the full symbol/timeframe/direction grid — this is not a "costs destroyed a good signal" story for any of
them; the signal itself does not separate winners from losers profitably before a single dollar of fees or slippage
is paid. Session Momentum (0.84) and VWAP Trend Continuation (0.90) come closest to gross breakeven, consistent with
the initial expectation that these two "investigate" candidates would look relatively better than the others — but
"relatively better" here still means net expectancy of -15 per trade at portfolio scale.

The "cost impact" column looks extreme (e.g. -448% for VWAP Trend Continuation) precisely *because* gross expectancy
is already close to zero for the better-performing families — a small negative gross number turned more negative by
a large, roughly-constant per-trade cost produces a huge percentage swing. This is a property of a near-zero
denominator, not evidence that costs are somehow worse for the "good" families; the absolute net expectancy figures
(column 5) are the meaningful comparison.

## 2. Exit-reason behavior (campaign-wide pattern, present in all six families)

Across every family, the same structural pattern repeats: `stop_loss` exits are frequent and carry the largest
average loss magnitude; `take_profit` exits are comparatively rare; `session_close` (forced flat by end-of-session)
contributes negative net expectancy almost everywhere it appears in meaningful volume. This mirrors exactly what ORB
v1 and VWAP Reversion v1 showed in Phase 12 — the same structural exit mechanics are again the dominant driver of
loss, not a family-specific defect in any one strategy's logic.

## 3. Failure classifications (evidence-backed, from `classify_family`)

| Family | Classifications |
|---|---|
| Gap Fill | no_directional_edge, target_sizing_failure, forced_session_close_failure |
| Session Momentum | no_directional_edge, target_sizing_failure, forced_session_close_failure, late_entry_failure |
| Intraday Trend Pullback | no_directional_edge, forced_session_close_failure, late_entry_failure, position_sizing_failure |
| EMA Trend Continuation | no_directional_edge, stop_sizing_failure, late_entry_failure, position_sizing_failure |
| Opening Fade | no_directional_edge, target_sizing_failure, forced_session_close_failure, late_entry_failure, position_sizing_failure |
| VWAP Trend Continuation | no_directional_edge, target_sizing_failure, forced_session_close_failure, late_entry_failure |

Every classification above carries its own supporting metrics in the API response (`failure_classifications[].evidence`)
— none were assigned from narrative judgment alone. `no_directional_edge` appears for all six because each family's
gross profit factor is below 1.05 with a sub-50% win rate and non-positive gross expectancy — the exact, documented
trigger condition, not a subjective label.

## 4. Research allocation

| Family | Decision | Strongest subgroup | Weakest subgroup | Evidence stability | Recommended budget |
|---|---|---|---|---|---|
| Gap Fill | Archive | AAPL | MSFT | not_stable | 0 jobs |
| Session Momentum | Archive | QQQ | GOOGL | not_stable | 0 jobs |
| Intraday Trend Pullback | Archive | SPY | AMD | not_stable | 0 jobs |
| EMA Trend Continuation | Archive | AMD | SPY | not_stable | 0 jobs |
| Opening Fade | Archive | AMD | SPY | not_stable | 0 jobs |
| VWAP Trend Continuation | Archive | SPY | AMD | not_stable | 0 jobs |

All six families are recommended for archival at the family level — none cleared even a cost-free profit factor of
1.0 across the tested grid. This overrides the initial framing that Session Momentum and VWAP Trend Continuation
should be "investigated" as families; the evidence does not support that at the family level. **It does not, however,
mean nothing here is worth a closer look** — see §5.

Permitted next action for every family: none at the family level (archive). Prohibited for all: promoting any
candidate without the unmodified elite gate; relaxing any threshold; blind parameter-grid re-expansion.

## 5. AMD 30m long Session Momentum — dedicated investigation

The two candidates that passed Campaign 47's per-job screen (`sessmom_6d9e916151af38`, `sessmom_9d2fff5ecd0aa7`) were
re-examined using Campaign 50's trade-level evidence for the identical (AMD, 30m, long) configuration:

- 88 trades, gross PF 2.57, **net PF 1.81**, net expectancy +14.26/trade, win rate 56.8%.
- Survives removal of its strongest month (2026-04, which alone contributed net PF 3.42 on 25 trades): the remaining
  trades still net positive.
- Survives removal of its best 9 trades (top decile by P&L): still net positive.
- **Does not transfer to 15m** (same symbol/direction): net PF 0.77, net expectancy -7.59.
- **Does not transfer to the short direction** on 30m: net PF 0.19, net expectancy -28.70.
- **Does not transfer to any comparable symbol**: NVDA (PF 0.36), TSLA (PF 0.46), META (PF 0.78), SPY (PF 0.26), QQQ
  (PF 0.33) — every comparison net profit factor is below 1.0.

**This is the precise, evidence-backed answer to why 2 jobs passed the per-job screen but produced zero elite
candidates:** the per-job gate (`passes_single_market_validation`) only evaluates one symbol/timeframe/direction in
isolation (PF ≥ 1.2, expectancy > 0, drawdown ≤ 12%, ≥30 trades) — AMD 30m long clears all four on its own. The
campaign-level cross-validation gate additionally requires `assets_passed >= 2` and a positive *median* result across
every one of the candidate's own symbol/timeframe/direction variants, not just its single best one. Since AMD is the
only configuration in this entire comparison with a net profit factor above 1.0, the median across all of that
candidate's variants is dragged well below the required threshold — exactly the mechanism `median_consistency_failures`
exists to catch. AMD 30m long Session Momentum is a real, repeatable, single-symbol effect — not noise, not an
artifact of one lucky month or a handful of lucky trades — but it is symbol-specific and does not generalize, which
is precisely the property the elite gate is designed to reject. **These two candidates are not promoted and are not
recommended for promotion by this analysis.**

## 6. Methodology: sample-size, stability, and dominance rules

Every subgroup breakdown in the API response (`stability_analysis`) is annotated against these explicit, fixed rules
(also returned verbatim in the API response under `minimum_evidence_rules`, so the UI never has to hardcode them):

| Rule | Value |
|---|---|
| Minimum trades for subgroup evidence | 20 |
| Minimum distinct symbols for stability | 2 |
| Minimum distinct months for stability | 2 |
| Maximum single-symbol share of net P&L before "dominates" | 60% |
| Maximum single-month share of net P&L before "dominates" | 60% |
| Minimum net profit factor for a "positive" subgroup | 1.0 |
| Minimum net expectancy for a "positive" subgroup | > 0 |

A subgroup is only ever reported as `meets_minimum_evidence: true` when it clears the trade-count floor **and** its
own net profit factor/expectancy pass the thresholds above — a positive aggregate number alone never earns that flag.
Dominance is computed by summing each individual trade's `|net_pnl|` (not the group's own net total) as the
denominator, so a family with many small losing trades and one large winning symbol correctly shows that symbol's
true share of the magnitude of money moved, not just its share of the netted-out total.

"Immediately adverse" (§ entry-quality) means the trade's maximum adverse excursion was reached within 0 bars of
entry (`bars_to_mae <= 0`) — i.e., on the entry bar itself. Entry-time buckets are fixed at 0-30m, 30-60m, 60-120m,
120-240m, 240m+ from session open. Relative-volume buckets are below-average (<1.0×), average (1.0×-1.5×), and
elevated (>1.5×) session-relative volume at entry.

## 7. Data-availability appendix

The following conclusions could **not** be computed and are explicitly marked `insufficient_evidence` in the API
response rather than fabricated:

| Missing evidence | Why | Where it's marked |
|---|---|---|
| Pre-entry price movement / "movement before entry" | No pre-entry candle/price history is persisted per trade — only the entry bar's own session feature snapshot. | Every family's `entry_quality_analysis.insufficient_evidence` |
| Market regime / volatility regime per trade | Intraday campaign jobs pass an empty `context_by_time` (see `run_intraday_campaign_job`) because regime classification depends on swing `features` columns (`ema_50`, `returns_5`, `volatility_20`) that have never been computed at 15m/30m granularity. Every trade's regime tag reads "unknown". | `data_availability.market_regime`, `stability_analysis.by_market_regime` |
| Training-period vs. validation-period separate performance | `result.walk_forward_metrics`/`out_of_sample_metrics` are both aliases of the same dict, which only stores date boundaries, never separate scored metrics for each side. | `data_availability.training_vs_validation_split_metrics`, AMD investigation's `training_vs_validation_split` |
| Quarterly breakdown | Only `month_key` was persisted, not a quarter key. Derivable by grouping months in three, not separately computed here. | AMD investigation's `quarterly_results` |
| Monthly/quarterly breakdown for Campaign 47 itself | Only annual (`by_year`) rollups were persisted pre-Phase-12.4. All monthly stability in this report comes from Campaign 50's trade-level `month_key`, not from Campaign 47's own stored evidence. | `data_availability.monthly_and_quarterly_breakdown_for_campaign_47` |

No field above was estimated, interpolated, or inferred from an unrelated aggregate — each is a direct, named gap in
what was persisted, addressed explicitly rather than silently.

## 8. What was deliberately *not* done

- ORB v1 and VWAP Reversion v1 were not touched, re-run, or re-analyzed beyond their existing archived evidence.
- No strategy definition, parameter, or threshold was changed anywhere in this phase.
- The elite gate (`passes_cross_validation`, `passes_single_market_validation`) is byte-for-byte unmodified.
- The two AMD 30m long Session Momentum candidates were not promoted.
- No new strategy family was added — this phase used exactly the six families and candidate generators built in
  Phase 12.3.
- Regime-aware feature computation at 15m/30m was identified as a real gap but deliberately *not* built in this
  phase — it would require computing an entirely new, never-before-validated swing-feature pass at intraday
  granularity, which is out of scope for an analysis-and-allocation phase that explicitly excludes broad new
  infrastructure work.
