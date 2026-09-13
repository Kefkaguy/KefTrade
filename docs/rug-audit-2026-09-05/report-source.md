# KefTrade intraday research audit

Audience: KefTrade owner and maintainers. Date: 5 September 2026.

Scope: RUG generation, frozen-data loading, backtesting, execution-cost calibration, feedback, elite collection and paper evidence. This is a focused deep audit of the strategy research path, not a repository-wide security or UI audit. Existing dirty changes were inspected and preserved. The plan tool was unavailable; discovery, independent execution/validation review, primary-source follow-up, synthesis and verification were tracked during the conversation.

## Direct answer

Several implementation defects can make research inefficient or its evidence misleading. Repairs are implemented locally and tested. Five historical promotions exist, but no five cost-surviving strategies have been established. The public VPS APIs expose their current paper evidence: all five have zero orders and fills and insufficient_forward_sample. Full historical database access timed out locally; the public candle endpoint caps a response at 1,000 rolling rows. No honest full rerun or survivor certification can be claimed from those observations.

## Findings and repairs

1. Forward FIFO attribution had one global lot queue. It could match different symbols, accounts and deployments. It also treated sells only as long exits and repeated exit fees in per-lot commission reports. The implementation now partitions lots by ownership, supports both directions and allocates partial fees/slippage proportionally. Evidence: research_campaigns.closed_trade_attribution; regression cases in test_rug_audit_repairs.py.
2. Only the latest 50 closed trades were returned to production evidence calculations, so older losses could disappear from those calculations. Complete trades are now retained internally; only the elite display list is truncated.
3. Elite paper rollups selected deployments by strategy label/version and positions by independent account and symbol sets. These are now linked by candidate/campaign and exact deployment account-symbol pairs. Realized forward P&L comes from eligible matched fills including both sides' fees. The position table has no unrealized_pnl column; open-position marks are now explicitly unavailable rather than reported as zero, and cannot pass the forward gate.
4. Forward eligibility lacked entry/signal chronology checks. Historical candles, pre-start signals, reversed chronology, missing boundary timestamps and test/manual origins on either side are now excluded when evidenced. Entry signal attribution uses the entry fill rather than the exit's signal. Remaining missing provenance cannot be solved by assigning favorable metadata.
5. All-candidate refresh ran as one failure-prone transaction. Candidate savepoints now preserve successful updates and return structured failures. This is consistent with the earlier per-candidate success/global 500 symptom, but the actual production exception was not retrieved, so its exact cause remains unconfirmed.
6. Drawdown interleaved different account capital levels as one curve. It now computes peaks per account and returns the maximum account drawdown, not a claimed portfolio drawdown.
7. The earlier v2 implementation counted an extra holding bar and timestamped forced close-price exits at bar open. New definitions carry explicit entry-bar-inclusive holding and forced-exit close-timestamp conventions. Legacy calls retain their previous behavior; stop/target intrabar timestamps remain bar-level estimates.
8. A 240-bar window repeatedly reinitialized EMA200. New definitions use a causal, full-history, SMA-seeded EMA calculated once per dataset. Prefix-invariance and exact-reference tests pass. Legacy long-only diagnostics are no longer applied as fictitious entry failures to RUG short signals.
9. RUG calibration accepted pooled observation totals without evidence for each asset. Its loader now requires versioned, consistent SIP regular-session calibration with per-symbol bar/fill support. New calibration records compare symbol/slot spreads plus regulatory charges against an already all-in global estimate; older cost-model conventions remain replayable. These are evidence-coverage improvements, not proof of actual account/order-size representativeness.
10. Completed v2 batches unnecessarily rebuilt all global research objects. They now retain campaign-local learning and use their own comparable feedback directly. The feedback SQL returns compact candidate aggregates and requires matching execution conventions; it avoids transporting every prior trade JSON into the generator. Full-cohort scans and duplicate-ID storage still grow with history, so million-scale performance remains unbenchmarked.

Previously implemented local repairs remain relevant: exchange sessions and flat-by-close execution, real ATR, session-reset opening ranges, same-slot relative volume, complete single-source data checks, development folds, a reserved tail excluded from search, cost scenarios, normalized failure rates and promotion denial for development-only evidence. These repairs are not yet deployed.

## Direct VPS observations

Read on 5 September 2026 through the health, RUG status, candidate forward-validation and candle GET endpoints. Last paper activity reported by the five candidates is approximately 18:55–18:56 UTC that day; these are stored timestamps, not proof of ongoing worker health.

| Candidate | Campaign | Historical PF | Historical trades | Paper fills |
|---|---:|---:|---:|---:|
| rug_1fe4e2dbcedc5b72 | 122 | 1.7741 | 287 | 0 |
| rug_4385b23944606d2f | 130 | 1.3050 | 374 | 0 |
| rug_e6bec299bb8e7d30 | 131 | 1.5119 | 379 | 0 |
| rug_0f9d99607fd3fa10 | 135 | 1.3515 | 243 | 0 |
| rug_4dcf693714cbb3b4 | 143 | 1.4850 | 339 | 0 |

RUG status: 2,900 completed candidates, 14,999 completed market jobs, five historical promotions, campaign 144 active. The earlier stored deployments place four candidates on AAPL and one on AMD; distinct candidate IDs are not evidence of portfolio diversification.

Latest 1,000 rolling 15m rows per symbol had no duplicate timestamps and were labeled alpaca_iex. Latest timestamps: TSLA 3 Sep 20:30 UTC; NVDA and MSFT 3 Sep 19:45; AAPL and AMD 4 Sep 20:30. The AAPL sample contained 115 outside-RTH rows, 34 complete 26-bar regular sessions and one truncated first session. This confirms session filtering matters while also showing usable regular-session segments exist. It does not certify frozen dataset 88 or establish gaps across the entire history.

## Interpretation and next experiment

Increasing search size does not make the evidence stronger by itself. Selection across many trials inflates apparent performance; final selection needs the trial history and an evaluation period the search could not exploit. [Bailey and López de Prado, The Deflated Sharpe Ratio (2014)](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf).

Feed consistency affects measured price/volume patterns. IEX covers one venue, while SIP consolidates exchanges; they must not be treated as interchangeable evidence. [Alpaca Market Data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq). Use exchange schedules, including early closes, for the intraday mandate. [NYSE trading calendar](https://www.nyse.com/trade/hours-calendars).

Paper execution is useful for implementation verification but does not establish live execution costs. Alpaca documents omitted impact, latency slippage, queue position and regulatory charges in its paper model. KefTrade's own simulator has its own assumptions and should be audited separately. [Alpaca Paper Trading](https://docs.alpaca.markets/us/docs/paper-trading).

The new read-only runner prepares a bounded 15m/five-asset experiment, verifies the frozen data, releases the database before CPU work, journals every result and learns only from complete valid cohorts. It reports frictionless/baseline/stress scenario P&L and measured throughput. This pilot still needs to run on the VPS. See ../rug-research-vps-runbook.md.

Five accepted strategies would additionally require representative measured costs; audited holdout/prospective data; multiplicity correction; parameter-neighborhood robustness; sufficient independent observations; correlation and shared-capital checks; corporate-action/feed checks; and short-borrow/capacity evidence where relevant. No stop-after-five certificate is implemented, and favorable historical labels must not be used to fake it. A historical single-symbol lead may be investigated prospectively with a predeclared scope, without weakening the old universal gate after seeing results.

## Verification and limitations

Final full suite: 2,661 passed, 36 skipped, one unchanged failure in test_phase10_modules_have_no_runtime_ddl, pointing to intraday_sector_leadlag_predictor.py. Expanded focused checkpoint: 196 passed. Later targeted chronology, runner and EMA checkpoint: 47 passed. No successful VPS database integration test, real-data pilot, deployment, live order or five-survivor result is claimed. HTML structural review only; no rendered visual review.

## Claim-to-source and gap ledger

| Claim family | Evidence / provenance | Confidence | Remaining gap |
|---|---|---|---|
| Execution/accounting defects | Local functions and regressions cited above; source revision 1f3137d plus current worktree | High for reproduced code behavior | Deployed revision may differ; no SQL integration |
| Five candidate status | GET /research/elite-candidates/{candidate}/forward-validation on user VPS, 5 Sep 2026 | High for reported fields | No prospective trades; aggregate historical records unverified |
| Data scope | GET /candles/{symbol}?timeframe=15m&limit=1000 and exchange-session classification | High for bounded sample | No complete frozen dataset or adjustment audit |
| Multiple testing | Bailey and López de Prado, 31 Jul 2014 paper, PDF opened directly | High | Full trial-count/correlation adjustment not implemented |
| Feed differences | Alpaca Market Data FAQ, official, accessed 5 Sep 2026 | High | Account subscription and historical feed lineage |
| Paper cost limitations | Alpaca Paper Trading, official, page says updated two months ago, accessed 5 Sep 2026 | High | KefTrade TCA representativeness and broker account type |
| Session hours | NYSE trading calendar, official, accessed 5 Sep 2026 | High | Full historical calendar/data consistency |

Search log: bounded first pass for the original DSR paper and Alpaca simulation/feed documentation; direct original-source opens; independent execution and validation code audits; targeted follow-up of cost selection, trade attribution, session samples and test regressions. Broad searches stopped once primary support and executable code reproductions converged. Remaining gaps require the actual frozen database, measured execution evidence and prospective observations, not more general web articles. The seven-gate and five-strategy objective remains incomplete.
