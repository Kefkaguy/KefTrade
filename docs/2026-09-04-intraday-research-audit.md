# Why KefTrade has not established a usable intraday strategy

Research audit · 4 September 2026 · Read-only assessment

## Conclusion

The evidence does not justify either “intraday cannot work” or “we only need more random combinations.” KefTrade is not yet testing the requested proposition cleanly. The current RUG execution path differs materially from a session-bound day-trading system; parts of its search space are misleading proxies; failure learning can absorb invalid experiments; and its validation labels overstate what has been established.

Keep random exploration, but repair and benchmark the research instrument before scaling it. Backtests remain the judge, but a judge using the wrong trading hours, execution rules, or evidence cannot answer the intended question. Nothing in this audit proves a profitable strategy will emerge after repairs.

## Scope and evidence

Reviewed the local implementation at revision `1f3137d`, historical internal intraday investigations, deployed campaign responses, and primary market/methodology sources. Local code is not proof that every deployed component runs that revision; deployed execution metadata independently corroborates the most important session and cost mismatches. No production settings, strategies, orders, or source code were changed. Full raw candle quality and actual account transaction costs were not independently measured.

Campaign 135 provides a concrete example: 100 candidates, 500 completed asset backtests, 99 rejected candidates, one historical promotion. Its failure distribution reports 453 weak-profit-factor flags, 449 poor-expectancy flags and 374 insufficient-trade flags. These overlap: do not add them or equate them with independent strategy failures. There are also 367 zero-expectancy and undefined-profit-factor messages, consistent with a large non-informative-test problem, but not a measured attribution to any single cause.

The promoted candidate `rug_0f9d99607fd3fa10` remains `awaiting_paper_deployment` / `pending_validation`. Its historical aggregate PF is approximately 1.35 across 243 trades. Its execution metadata reports `flat_by_session_close: false` and `calibrated_execution_costs: false`. Historical promotion is not proof of forward profitability. Source: [deployed campaign 135](https://keftrade.duckdns.org/research/campaigns/135).

## Findings, ranked by decision importance

### 1. Fifteen-minute bars are being mistaken for an intraday mandate

RUG uses the generic strategy/backtest path rather than the session-aware intraday path. Session flattening defaults to false. Holding limits include 30 and 40 bars, equivalent to 7.5 and 10 hours of 15-minute bars; even a shorter position opened late can survive overnight.

This means a successful RUG backtest need not be a day-trading strategy. Conversely, its failures do not cleanly test a flat-by-close proposition. The repository already has session-aware execution machinery, so the problem is integration rather than absence of all necessary components.

Code: `research_campaigns.py:3669`, `strategy_discovery.py:434`, `strategy.py:22`, `rug.py:41`; existing alternative: `labs/intraday/strategy.py:101`.

### 2. Half the uniformly sampled entry-window choices miss regular US hours

Three of six RUG windows are 09:30–11:00, 09:30–12:00 and 10:00–13:00 **UTC**. The evaluator compares candle hours directly, with no exchange-time conversion. Regular US equity hours start at 13:30 UTC in summer and 14:30 UTC in winter. Thus those choices cannot trigger during regular hours. With extended-hours data they can instead search premarket behavior, which is a different execution problem.

This establishes a search-domain mismatch, not that exactly half the deployed jobs have zero trades: guidance changes proposal selection and actual session coverage has not been quantified. Correct the domain to exchange-local session offsets, including daylight saving and early closes. [NYSE trading hours](https://www.nyse.com/trade/hours-calendars).

Code: `rug.py:42`, `strategy_discovery.py:592`, `providers/yfinance_provider.py:156`.

### 3. Several strategy dimensions do not execute as their names suggest

| Label | Actual generic RUG behavior | Consequence |
|---|---|---|
| ATR stop | `close × 1% × multiplier`, combined with a swing low | Not measured ATR; sampled multipliers imply at least roughly 0.75%–3% signal-time stop distances |
| Opening-range proxy | Same rolling prior-high comparison as breakout | Not a session-reset opening-range strategy |
| ATR volatility filter | Enough recent candles for the lookback | Does not test an ATR threshold |
| Keltner / Donchian volatility | Same `volatility_20` threshold | Different labels can represent identical filtering |
| Relative volume | Change from the preceding candle | Not volume relative to the usual volume at that time of day |
| Trailing / fixed / time exit labels | Exit label itself excluded from execution key; execution governed by stop, R multiple and holding limit | Not four independently implemented exit mechanisms |

Actual RSI periods and moving-average periods are wired; this is not a claim that every parameter is fake. But millions of labeled combinations are not millions of meaningfully different economic hypotheses. The shared EMA trend and long-entry structure also makes “unbiased” an inaccurate description of the whole search domain.

Code: `strategy_discovery.py:355`, `:676`, `:695`, `:756`, `:775`; `features.py:77`; `rug.py:149` onward.

### 4. Costs are substantial and uncalibrated—not a reason to remove costs

The inherited model applies approximately 10 basis points of fees plus 5 basis points of slippage per side: roughly 30 basis points round trip. That is about $3 per $1,000 of traded notional before compounding and exit-price differences.

Alpaca's published schedule generally provides commission-free transactions with specified exceptions and pass-through charges; it does not establish this account's actual costs. Spread, slippage, routing, liquidity and order size still matter. The appropriate action is account- and execution-specific calibration, retaining a separate conservative stress scenario—not lowering a fee until results look good. [Alpaca fee schedule](https://files.alpaca.markets/disclosures/BrokFeeSched.pdf).

Additional realism limitations: position size is risk divided by stop distance without a cash/buying-power cap in this path; gap-through stops can receive the stop price plus modeled slippage instead of the worse opening price. These can overstate survivors even while high blanket costs reject other candidates.

Code: `strategy.py:145`, `backtester.py:82`, `:168`, `:198`, `:453`.

### 5. Failure learning can learn the wrong lesson

The global learning query includes `failed` and `blocked_data` jobs. Normalization classifies these as rejected. Avoid-region generation counts parameter buckets among rejected jobs, without dividing by how often each bucket was tested. Therefore infrastructure failures can become apparent economic failures, and popular settings can accumulate more penalties simply because they were tried more often.

The RUG guidance scorer is a small heuristic: preferred families/blocks add points, avoided parameter buckets subtract points. It is not causal diagnosis of why a strategy failed, and the query is not partitioned into comparable dataset/feed/cost-version cohorts. Allocation remains fixed 60/30/10 when guidance is available.

Failures do influence the next search, but there is no demonstrated improvement over a matched random baseline. Store technical errors separately; estimate failure/success rates with uncertainty within comparable experiments; preserve an unadapted control stream. Adaptive sampling is deliberately biased toward evidence, which is acceptable if measured honestly.

Code: `research_learning.py:249`, `:532`, `:550`, `:588`, `:712`; `rug.py:25`.

### 6. Validation is simultaneously broader than your objective and weaker than its labels imply

The generic “walk-forward” is one chronological 70/30 split, with trading on the terminal portion—not rolling multi-fold validation. Successive RUG batches reuse the same frozen dataset and learn from completed campaigns. The reused terminal period consequently becomes development evidence, not an untouched final test.

The aggregate gate also wants cross-asset stability, rather than merely a valid predeclared symbol-specific edge. Regime checks can veto buckets with as few as five trades. Meanwhile, the trade-frequency floor defaults to zero, so a passing strategy need not provide day-to-day opportunities. When frequency is computed, its denominator includes training history although these trades are from the tail, understating activity.

Do not lower gates retrospectively to rescue a favorite. Define different prospective tests for universal versus symbol/session/regime-specific hypotheses. Preserve a final, inaccessible evaluation period, and account for the number and dependence of experiments. Repeatedly trying combinations increases false discoveries; ordinary attractive backtests do not correct that. [Bailey et al., The Probability of Backtest Overfitting](https://carmamaths.org/resources/jon/backtest2.pdf).

The generic promotion path does not establish all seven requested survivor conditions: separate cost stress, untouched evaluation, genuine multi-fold testing, parameter-neighborhood robustness and a diversified collection still need explicit evidence. `good_candidates_collected` sums historical promotions; `rejected_candidates_learned_from` counts rejections even though learning errors can be caught without stopping finalization.

Code: `backtester.py:32`, `:77`, `:305`; `research_campaigns.py:5209`, `:5296`, `:5368`, `:5492`, `:5599`, `:5622`; `strategy_research.py:596`.

### 7. A frozen dataset is reproducible, not automatically valid

Snapshot creation copies all candle sources. Snapshot loading orders by timestamp and source without collapsing equal timestamps. If multiple providers overlap, repeated timestamps can reach feature/backtest calculations. That is a conditional risk established in code; actual duplicate prevalence in dataset 88 is unmeasured.

Manifest verification checks hashes and counts, not complete exchange sessions, corporate-action consistency, missing intervals or provider equivalence. Feed provenance matters: IEX and consolidated SIP are not interchangeable coverage. Frozen features are recalculated, so earlier preflight counts near 5,000 feature rows do not by themselves prove the frozen campaign used only 5,000 rows. [Alpaca market-data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq).

Code: `research_architecture.py:344`, `:451`, `:494`, `:2293`.

## What previous intraday experiments actually tell us

The archived `2026-07-24-phase12-4-failure-analysis.md` reports 480 jobs and 11,109 trades across six genuine intraday families, with every pooled family's net PF below one. Those are important negative results: correcting RUG does not imply that basic opening-range, VWAP or moving-average rules will become profitable.

However, gross must be defined carefully. The earlier ORB investigation explicitly describes gross as post-slippage, pre-fee. It is therefore not automatically a frictionless signal test. Negative strategy P&L also entangles entries, exits and sizing; it does not isolate directional predictability.

The July report records an AMD 30-minute long session-momentum subset with 88 trades and net PF 1.81, rejected for poor transfer to other configurations/markets. That is a research lead, not proof of a real edge. Its retrospective discovery and sensitivity tests require fresh confirmation. Nor should its failure to generalize automatically invalidate a prospectively scoped AMD-specific hypothesis.

These historical figures were read from existing reports, not independently rerun in this audit.

## Recommended next experiment—not another million candidates

1. **Freeze the measurement contract.** Specify eligible sessions, no overnight exposure, intended daily opportunity at portfolio level, long/short scope, execution delay, capital limits and measured costs. No system can promise positive P&L every day.
2. **Certify the instrument.** Test exchange hours/DST/early closes, overnight prohibition, actual indicator semantics, gap fills, capital constraints and one candle per timestamp. Produce dataset-88 coverage and provenance diagnostics. Quarantine invalid data rather than teach from it.
3. **Run a bounded diagnostic panel.** Start with simple session-bound trend, reversal and opening-range hypotheses plus matched random-entry controls. Predeclare a modest fixed parameter set. For each, separate frictionless signal diagnostics, realistic net execution and cost stress. Use the same dates and opportunity constraints. This estimates whether losses arise from no signal, execution costs, exits or sparse opportunities.
4. **Use controlled changes.** Compare one change at a time: valid session windows, real volatility-scaled stops, optional versus mandatory filters, and holding exits. Track valid tests, signals, trades, overnight count, gross/net expectancy, drawdown and uncertainty—not only candidates generated.
5. **Benchmark learning.** Give adaptive selection and fixed random sampling equal compute and comparable development data. Exclude technical failures. Measure whether adaptation improves future development-fold results, coverage and unique useful hypotheses; do not assume the 60/30/10 policy helps.
6. **Confirm rather than optimize the final test.** Freeze candidate rules and scope, apply multiplicity-aware selection diagnostics, then use protected final data and prospective paper execution. If final evidence is reused to redesign candidates, call it development and obtain new final evidence. Collect ten only if ten qualify; do not loosen standards or deploy automatically to hit the count.

The recommended immediate priority is steps 1–3, not more workers, more assets or a bigger generation target. No production pause was performed as part of this assessment.

## Remaining uncertainties

- Exact share of failures caused by timing, contradictory filters, costs or genuinely negative signals: needs matched reruns and trade-level attribution.
- Dataset 88's duplicate timestamps, gaps and mixed-provider prevalence: needs raw-data checks.
- Actual fees, spread and adverse slippage by session/order size: needs account and execution evidence.
- Whether corrected adaptive search outperforms random search: needs a controlled comparison.
- Whether any existing or future candidate survives prospective execution: not established.

The system contains useful infrastructure—deterministic generation, next-bar entries, conservative same-bar stop ordering, frozen datasets and explicit validation gates. The problem is not that all the work was wasted. It is that successful job execution has been confused with a validated research process aligned to the requested trading objective.
