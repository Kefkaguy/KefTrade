# Phase 1 — Why KefTrade specialists do not transfer

**Status:** complete for review; Phase 2 has not started.

## Technical summary

The demonstrated blocker is **loss of economic edge on the second asset**, not a hidden threshold change and not primarily a shortage of trades. Across campaigns 51, 53, 54, all 22 preserved specialist IDs failed every non-home transfer: 0/76 candidate–asset attempts passed the unchanged strong gates. After collapsing exact executable repeats, the result is still 0/52 attempts across 16 strategies (95% Wilson upper bound 6.9%; observations are dependent).

At execution-unique grain, 48/52 failures (92.3%) missed profit factor or positive expectancy, while only 4/52 were sample-only near transfers. The median strategy lost 0.851 profit-factor points from home to its median target (configuration-bootstrap interval 0.769–1.007; descriptive, not independent-market inference).

Three mechanisms are supported:

1. **The original cluster hypothesis was too coarse for transfer.** All 16 GOOGL specialist IDs came from the least centroid-representative member of the five-asset cluster (distance 2.784). This is consistent with one measurable contributor to repeatedly selecting GOOGL-specific winners; it does not establish causality. Contradictory evidence: AMD was the most centroid-representative member of its cluster (distance 1.277) and its six specialists still failed NVDA and TSLA, so centroid distance is not a complete cause.
2. **The edge reverses inside the same named regimes.** GOOGL-home strategies had pooled bull-trend PF 1.570 over 294 trade observations versus PF 0.771 over 845 target trade observations; AMD-home strategies had bull-trend PF 1.533 over 210 versus 0.758 over 305 targets. Low- and normal-volatility slices reverse similarly. Therefore a label such as `bull_trend` is not a sufficient transfer condition.
3. **Hypothesis regime metadata is descriptive, not executable.** Candidate annotation stores `relevant_regimes`, but the decision path only activates regime filtering when separate phase-specific flags are present. The 22 specialists do not carry those flags. Generation therefore tested generic trend/pullback rules across every asset rather than a market-behavior-conditioned strategy. Code evidence: `research_architecture.py:1182-1194`, `strategy_discovery.py:322-345`, and `strategy_discovery.py:497-526`.

No conclusion below is presented as confirmed causal knowledge. This diagnosis is post-hoc and must be falsified on a future unseen frozen dataset.

## The failure is economic on 48 of 52 unique transfer attempts

The unchanged gates are PF ≥ 1.20, positive expectancy, drawdown ≤ 0.12, at least 30 trades, enabled walk-forward, and paper readiness. `paper_readiness` repeats the trade/economic checks, so it is not counted as an independent cause.

| Target | Unique attempts | Trade obs. | Median PF | Median exp. | Median trades | Economic fails | Sample-only | Passes |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| AAPL | 10 | 298 | 0.996 | -0.08 | 29.5 | 7 | 3 | 0 |
| AMZN | 10 | 290 | 0.616 | -21.24 | 25.0 | 10 | 0 | 0 |
| META | 10 | 253 | 0.724 | -14.03 | 23.5 | 9 | 1 | 0 |
| MSFT | 10 | 213 | 0.586 | -18.83 | 24.5 | 10 | 0 | 0 |
| NVDA | 6 | 204 | 0.607 | -17.07 | 38.0 | 6 | 0 | 0 |
| TSLA | 6 | 208 | 0.702 | -13.15 | 37.0 | 6 | 0 | 0 |

AAPL is the only credible frequency frontier: three of ten unique attempts had positive economics but fewer than 30 trades. AMZN, MSFT, NVDA, and TSLA failed economically on every unique attempt; META had one sample-only case and nine economic failures. This contradicts any claim that simply generating more of the same candidates will solve transfer.

## Matching the regime name does not preserve the payoff distribution

| Specialist home | Regime | Home trades | Home PF | Home exp. | Target trades | Target PF | Target exp. |
|---|---|---:|---:|---:|---:|---:|---:|
| GOOGL | bull_trend | 294 | 1.570 | 19.45 | 845 | 0.771 | -9.19 |
| GOOGL | sideways | 71 | 1.478 | 23.68 | 200 | 0.670 | -19.72 |
| GOOGL | low_volatility | 285 | 1.342 | 13.70 | 916 | 0.708 | -13.07 |
| GOOGL | normal_volatility | 81 | 2.488 | 41.57 | 118 | 0.969 | -1.28 |
| AMD | bull_trend | 210 | 1.533 | 21.59 | 305 | 0.758 | -10.17 |
| AMD | sideways | 34 | 1.991 | 45.85 | 107 | 0.419 | -39.30 |
| AMD | low_volatility | 62 | 1.834 | 28.65 | 249 | 0.678 | -15.56 |
| AMD | normal_volatility | 144 | 1.459 | 21.34 | 161 | 0.535 | -23.21 |
| AMD | high_volatility | 38 | 1.996 | 32.75 | 2 | n/a | 151.39 |

These are pooled trade observations across execution-unique strategies, not independent trades: strategies share candles and often share entries. They are valid descriptive evidence of within-regime reversal, but no causal p-value is claimed. High-volatility transfer for AMD has only two target trades and is explicitly inconclusive; it cannot contradict or support transfer.

## The cluster observation selected asset-specific winners

Campaigns 51 and 54 used cluster 1; campaign 53 used cluster 2. The campaign-used v1 clusters stored zero similarity for every multi-member asset, so the only discriminating campaign-time cohesion measure was distance to centroid.

| Cluster | Asset | Centroid distance | Trend strength | Trend persistence | Realized vol. | Median pullback | Volume expansion | Specialist IDs |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | AAPL | 2.134 | 0.0212 | 9.54 | 0.0063 | 0.0085 | 1.636 | 0 |
| 1 | AMZN | 1.791 | 0.0192 | 10.53 | 0.0077 | 0.0112 | 1.644 | 0 |
| 1 | GOOGL | 2.784 | 0.0291 | 11.04 | 0.0075 | 0.0102 | 1.757 | 16 |
| 1 | META | 1.858 | 0.0223 | 9.62 | 0.0090 | 0.0121 | 1.792 | 0 |
| 1 | MSFT | 2.156 | 0.0097 | 9.86 | 0.0060 | 0.0088 | 1.628 | 0 |
| 2 | AMD | 1.277 | 0.0295 | 10.12 | 0.0136 | 0.0192 | 1.725 | 6 |
| 2 | NVDA | 2.057 | 0.0318 | 10.36 | 0.0109 | 0.0144 | 1.902 | 0 |
| 2 | TSLA | 2.195 | 0.0129 | 9.79 | 0.0134 | 0.0233 | 1.762 | 0 |

GOOGL is simultaneously the cluster-1 outlier and the source of every cluster-1 specialist. AMD supplies the counterexample: it is cluster 2's nearest member, yet its specialists reverse economically on both targets. The supported conclusion is narrower than “clustering is wrong”: the current behavior vector and regime labels are not sufficient conditions for strategy payoff transfer.

There is also a direct epistemic mismatch in the inputs. Hypothesis versions 28 and 32 have status `testing`, yet their hypothesis text begins with “Confirmed directional persistence” based only on profile aggregates (25,000 and 15,000 candle observations; confidence scores 0.8899 and 0.8679). Hypothesis 34 preserves campaign-51 contradictory stage evidence but retains the same “Confirmed” wording and `testing` status. Campaigns 51, 53, and 54 therefore exploited an unconfirmed cluster-behavior statement before any configuration had demonstrated transfer. This is a ledger-supported state/wording inconsistency, not a claim that the profile measurements themselves are false.

## Entry, exit, and one-parameter drift do not restore transfer

| Component | Value | Unique specialists | Target attempts | Home median PF | Target median PF | Economic-fail rate | Sample-only | Passes |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| entry | pullback | 7 | 22 | 1.480 | 0.610 | 86.4% | 3 | 0 |
| entry | trend_continuation | 9 | 30 | 1.552 | 0.766 | 96.7% | 1 | 0 |
| exit | atr_stop | 4 | 12 | 1.945 | 0.815 | 91.7% | 1 | 0 |
| exit | fixed_rr | 7 | 24 | 1.475 | 0.610 | 87.5% | 3 | 0 |
| exit | time_exit | 4 | 14 | 1.376 | 0.705 | 100.0% | 0 | 0 |
| exit | trailing_proxy | 1 | 2 | 1.563 | 0.714 | 100.0% | 0 | 0 |

Every tested entry and exit family failed transfer. ATR exits produced the strongest home median PF (1.945) but only 0.815 on targets. Trend-continuation entries failed economically on 29/30 unique target attempts; pullbacks failed economically on 19/22, with the remaining three limited by sample size.

Campaigns 51, 53, and 54 also preserve 52 one-parameter nearby mutants and 220 matched parent–child asset comparisons. The results below are explicitly post-hoc.

| Mutated parameter | Mutants | Asset comparisons | Median ΔPF | Median Δtrades | Pass gains | Pass losses | Specialist-linked |
|---|---:|---:|---:|---:|---:|---:|---:|
| entry_distance_to_ema20_max | 2 | 10 | 0.001 | 0.0 | 0 | 0 | 5 |
| max_holding_bars | 4 | 18 | -0.220 | 16.5 | 1 | 0 | 5 |
| returns_5_min | 6 | 24 | -0.036 | 4.0 | 0 | 1 | 3 |
| risk_reward | 4 | 18 | -0.028 | 1.0 | 0 | 1 | 5 |
| rsi_max | 3 | 13 | 0.015 | 0.0 | 0 | 1 | 5 |
| rsi_min | 4 | 16 | 0.020 | 4.0 | 0 | 0 | 5 |
| trend_fast | 12 | 50 | -0.086 | 0.0 | 0 | 0 | 5 |
| trend_slow | 12 | 50 | 0.013 | -3.0 | 0 | 2 | 10 |
| volume_change_min | 5 | 21 | -0.049 | 3.0 | 1 | 0 | 8 |

The clearest sensitivity pattern is a quality–frequency tradeoff. Increasing holding bars added a median 16.5 trades but reduced median PF by 0.220. Relaxing volume added three trades but reduced median PF by 0.049. The isolated pass gains occurred on the home asset only; none created a transfer. Conversely, changing the slow trend window caused two strong-pass losses and no gains. No single-parameter causal effect is claimed because the mutations were selected after earlier outcomes and share the same dataset.

## What is reusable and what is asset-specific

**Reusable but unconfirmed:** the generic 20/50 trend backbone, RSI/stochastic momentum checks, and both trend-continuation and pullback entries can produce a valid single-asset specialist on GOOGL and AMD. That is evidence of reusable candidate components, not evidence that any component is structurally sound across assets; no executable configuration passed a second asset.

**Asset-specific in the observed evidence:** the complete entry/exit/threshold combinations and their payoff distributions. GOOGL specialists remain profitable in GOOGL bull, sideways, low-volatility, and normal-volatility slices while losing in the same target regime labels. AMD shows the same reversal. This makes the interaction between parameters and finer asset behavior—not the broad strategy-family label—the measurable unit that future hypotheses must target.

**Inconclusive:** which individual feature causes that interaction. Stored feature correlations are based on 30–56 home trades per specialist and are observational; they cannot isolate RSI, EMA distance, volume change, or volatility as causal. Earnings behavior is unavailable because no versioned corporate-event dataset exists.

## Measurable hypotheses for the next approved phase

All five hypotheses below are post-hoc and **unconfirmed**. They must not be stored as confirmed or used to claim improvement until tested prospectively on a new frozen dataset.

1. **Executable regime conditioning.** If `relevant_regimes` is enforced as an executable filter rather than metadata, execution-unique transfer success will exceed the current 0/52 baseline without weakening any gate. Test a fixed-budget matched control/treatment on a future dataset. Supporting evidence: campaigns 51/53/54, all 22 specialists, within-regime reversals above. Contradiction: regime names alone did not preserve edge, so the treatment must include finer behavior conditions and may still fail.
2. **Representative-member confirmation.** If exploitation begins only after a configuration passes a centroid-near asset and one behavior-diverse cluster member, fewer specialists will be misclassified as plausible transfer candidates and jobs per cluster elite will decline. Test with the same candidate/job budget. Supporting evidence: campaigns 51/54 selected only GOOGL, the cluster-1 outlier. Contradiction: AMD was centroid-near and still did not transfer, so representativeness is necessary-at-most, not sufficient.
3. **Behavior-normalized entries.** Scaling pullback distance, return thresholds, and volume thresholds to each asset's frozen profile percentiles will improve transfer PF relative to fixed thresholds while preserving ≥30 trades. Supporting evidence: target profile differences and 48/52 unique economic failures. Contradiction: AAPL had three sample-only near transfers, so normalization may reduce rather than increase frequency.
4. **Within-regime structural matching.** Conditioning on trend strength, pullback depth, momentum persistence, and volume expansion jointly will predict transfer better than the current `bull_trend`/volatility labels. Falsify by preregistering similarity bands and comparing held-out transfer rate at equal compute. Supporting evidence: strong home-to-target PF reversals within identical broad regimes. Contradiction: the current profile sample is one frozen window and may not be stable through time.
5. **Frequency-only frontier.** For configurations that already have PF ≥1.20 and positive expectancy but <30 trades, a preregistered frequency mutation can reach 30 trades without pushing PF below 1.20. Test only the four execution-unique sample-only cases (three AAPL, one META), with no gate change and no post-result retuning. Supporting evidence: the four sample-only rows in Appendix B. Contradiction: the broader mutation history shows frequency increases often reduce PF.

## Scope, definitions, and reproducibility

The authoritative frozen dataset is `dataset_0fcc46465c65d213af34b46a` (dataset 1), window 2023-08-16 13:30:00+00:00 through 2026-07-16 19:30:00+00:00. Integrity verification passed with no issues. Each relevant 1h asset has 5,000 frozen candles; validation uses the stored walk-forward split and candidate-level trade counts shown below.

The preserved cohort contains every `asset_specialist` stage row: 22 IDs, 16 execution-unique configurations, 826 home trade observations, and 2078 non-home trade observations. Six campaign-54 IDs exactly repeat campaign-51 executable strategies; all repeated metrics match, which is a deterministic reproducibility pass but not independent evidence.

| Campaign | Hypothesis | Cluster | Candidates | Jobs | Specialist IDs | Jobs / specialist | Generator | Thresholds |
|---:|---:|---:|---:|---:|---:|---:|---|---|
| 51 | 28 | 1 | 100 | 500 | 6 | 83.3 | hypothesis_targeted_generator_v2 | strong_research_gates:v1 |
| 53 | 32 | 2 | 100 | 300 | 6 | 50.0 | hypothesis_targeted_generator_v2 | strong_research_gates:v1 |
| 54 | 34 | 1 | 60 | 300 | 10 | 30.0 | hypothesis_targeted_generator_v2 | strong_research_gates:v1 |

These campaigns consumed 1,100 stored jobs and approximately 4.53 summed job-runtime hours. Phase 1 added **zero** validation jobs and changed **zero** thresholds; it queried preserved evidence only. Across the three campaigns, validation efficiency was 50.0 jobs per preserved specialist ID or 68.8 jobs per execution-unique specialist, with zero cluster elites and therefore no finite jobs-per-cluster-elite result.

Method: reconcile immutable stage rows to campaign jobs; use the six strong gate diagnostics rather than the worker's weaker status label; collapse exact executable keys for independent-looking summaries; pool stored regime gross profit/loss descriptively; compare one-parameter nearby children to the matched parent on the same frozen asset; use Wilson intervals for zero-success rates and a fixed-seed configuration bootstrap for the median PF drop.

No chart or screenshot was produced. Exact audit tables are used because the requested review requires candidate-by-target lookup and the task explicitly prohibited browser and screenshot/image review.

## Limitations and robustness checks

- **Post-hoc:** all causal explanations and proposed hypotheses were constructed after seeing dataset-1 outcomes. They remain unconfirmed.
- **Dependence:** candidates share lineage, candles, and sometimes exact execution parameters. Candidate/asset Wilson intervals are descriptive and likely narrower than a true independent-dataset interval.
- **Frozen-data reproducibility passed:** dataset integrity passed and six exact revalidations reproduced all asset metrics without inconsistency.
- **Broad regime labels are coarse:** the report can reject those labels as sufficient conditions but cannot identify the missing causal feature from aggregate metrics alone.
- **Corporate events unavailable:** earnings behavior was not guessed because no versioned event dataset exists.
- **Contradictory evidence retained:** AAPL/META sample-only cases, centroid-near AMD failure, and the two-trade AMD high-volatility target sample prevent overgeneralized conclusions.

## Required next step

Review this Phase 1 diagnosis. Per the sequencing rule, no Phase 2 or Phase 3 implementation may start until explicit approval. If approved, the first design decision should be which unconfirmed hypothesis to preregister against a future frozen dataset; no validation gate needs to change.

## Further questions

- Should the next independent dataset be a later time window for the same assets, a disjoint asset universe, or both? This changes what “transfer” can establish.
- Should a cluster hypothesis require a minimum observed similarity/cohesion value before it can generate cluster-targeted candidates? The campaign-used v1 similarity values provide no positive evidence.
- Is a corporate-event dataset in scope later? Without it, event-driven gaps and earnings sensitivity must remain explicitly unavailable.

## Appendix A — every preserved specialist home result

Each row is a strong-gate pass. Trade count is the candidate-level sample size. Repeated campaign-54 executions are marked by their representative.

| Campaign | Candidate | Home | Entry / exit | PF | Exp. | Trades | DD | Evidence | Exact repeat of |
|---:|---|---|---|---:|---:|---:|---:|---|---|
| 51 | `sd_1dc4b1f924db47` | GOOGL | trend_continuation / time_exit | 1.379 | 14.08 | 38 | 0.032 | job 128406 | — |
| 51 | `sd_26e50965f33c2b` | GOOGL | pullback / fixed_rr | 1.340 | 9.19 | 51 | 0.035 | job 128221 | — |
| 51 | `sd_350dfcb00a5335` | GOOGL | pullback / fixed_rr | 1.485 | 30.47 | 32 | 0.048 | job 128211 | — |
| 51 | `sd_48b473cfc8d263` | GOOGL | trend_continuation / time_exit | 2.161 | 33.52 | 32 | 0.018 | job 128456 | — |
| 51 | `sd_8d0bacd555fc75` | GOOGL | trend_continuation / atr_stop | 1.552 | 13.14 | 33 | 0.026 | job 128266 | — |
| 51 | `sd_f0dbcb5b00d6fd` | GOOGL | pullback / fixed_rr | 1.471 | 29.80 | 30 | 0.068 | job 128561 | — |
| 53 | `sd_39488438e22427` | AMD | pullback / time_exit | 1.374 | 19.29 | 39 | 0.108 | job 129019 | — |
| 53 | `sd_646130b9499e2e` | AMD | pullback / atr_stop | 2.020 | 54.67 | 30 | 0.055 | job 128929 | — |
| 53 | `sd_70c547e6101346` | AMD | trend_continuation / atr_stop | 2.004 | 29.69 | 32 | 0.043 | job 128938 | — |
| 53 | `sd_ad01861adeeb06` | AMD | trend_continuation / fixed_rr | 1.446 | 16.86 | 56 | 0.042 | job 128848 | — |
| 53 | `sd_d27ece03fcf410` | AMD | pullback / fixed_rr | 1.480 | 18.16 | 32 | 0.033 | job 129061 | — |
| 53 | `sd_f53399088d2cc8` | AMD | trend_continuation / trailing_proxy | 1.563 | 22.28 | 55 | 0.037 | job 128968 | — |
| 54 | `sd_06bb96a42ee727` | GOOGL | trend_continuation / time_exit | 1.289 | 11.31 | 42 | 0.041 | job 129336 | — |
| 54 | `sd_0ec396a028e49b` | GOOGL | trend_continuation / atr_stop | 1.552 | 13.14 | 33 | 0.026 | job 129146 | C51 `sd_8d0bacd555fc75` |
| 54 | `sd_128969e76fa303` | GOOGL | trend_continuation / time_exit | 1.379 | 14.08 | 38 | 0.032 | job 129126 | C51 `sd_1dc4b1f924db47` |
| 54 | `sd_244dd7c011766b` | GOOGL | pullback / fixed_rr | 1.471 | 29.80 | 30 | 0.068 | job 129151 | C51 `sd_f0dbcb5b00d6fd` |
| 54 | `sd_2d0392e7c9f06e` | GOOGL | trend_continuation / atr_stop | 1.887 | 16.90 | 40 | 0.032 | job 129356 | — |
| 54 | `sd_882eb33eb2e893` | GOOGL | pullback / fixed_rr | 1.485 | 30.47 | 32 | 0.048 | job 129136 | C51 `sd_350dfcb00a5335` |
| 54 | `sd_8e53615f4b38c6` | GOOGL | trend_continuation / fixed_rr | 1.475 | 11.72 | 34 | 0.027 | job 129366 | — |
| 54 | `sd_96791bfdebfde1` | GOOGL | trend_continuation / time_exit | 2.161 | 33.52 | 32 | 0.018 | job 129141 | C51 `sd_48b473cfc8d263` |
| 54 | `sd_bd71f1dd42c538` | GOOGL | pullback / fixed_rr | 1.666 | 39.49 | 34 | 0.045 | job 129346 | — |
| 54 | `sd_f12abb27e9f410` | GOOGL | pullback / fixed_rr | 1.340 | 9.19 | 51 | 0.035 | job 129131 | C51 `sd_26e50965f33c2b` |

## Appendix B — every specialist-to-target failure

This is the line-level answer to why specialist X did not transfer to asset Y. Gate names are the unchanged strong gates. `sample-only` means profit factor, expectancy, drawdown, and walk-forward passed but the 30-trade minimum (and therefore paper readiness) did not. Individual rows are descriptive; no independent-candidate p-value is claimed.

| Campaign | Specialist | Target | PF | Exp. | Trades | DD | Failed gates | Weak regimes | Direct diagnosis | Evidence |
|---:|---|---|---:|---:|---:|---:|---|---|---|---|
| 51 | `sd_1dc4b1f924db47` | AAPL | 1.021 | 0.76 | 30 | 0.052 | paper_readiness, profit_factor | low_volatility | economic_edge | job 128404 |
| 51 | `sd_1dc4b1f924db47` | AMZN | 0.563 | -17.30 | 30 | 0.060 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility | economic_edge | job 128405 |
| 51 | `sd_1dc4b1f924db47` | META | 0.919 | -2.46 | 24 | 0.031 | paper_readiness, positive_expectancy, profit_factor, trade_count | none evidenced | economic_edge + sample_frequency | job 128407 |
| 51 | `sd_1dc4b1f924db47` | MSFT | 0.821 | -7.52 | 24 | 0.050 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 128408 |
| 51 | `sd_26e50965f33c2b` | AAPL | 0.612 | -14.75 | 56 | 0.103 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, sideways | economic_edge | job 128219 |
| 51 | `sd_26e50965f33c2b` | AMZN | 0.484 | -21.09 | 48 | 0.111 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, sideways | economic_edge | job 128220 |
| 51 | `sd_26e50965f33c2b` | META | 0.381 | -26.11 | 35 | 0.110 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, normal_volatility, sideways | economic_edge | job 128222 |
| 51 | `sd_26e50965f33c2b` | MSFT | 0.565 | -18.41 | 43 | 0.089 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, sideways | economic_edge | job 128223 |
| 51 | `sd_350dfcb00a5335` | AAPL | 1.264 | 16.48 | 22 | 0.070 | paper_readiness, trade_count | none evidenced | sample-only | job 128209 |
| 51 | `sd_350dfcb00a5335` | AMZN | 0.591 | -30.94 | 21 | 0.101 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 128210 |
| 51 | `sd_350dfcb00a5335` | META | 0.601 | -31.73 | 23 | 0.113 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility, normal_volatility, sideways | economic_edge + sample_frequency | job 128212 |
| 51 | `sd_350dfcb00a5335` | MSFT | 0.494 | -40.13 | 11 | 0.086 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 128213 |
| 51 | `sd_48b473cfc8d263` | AAPL | 0.705 | -13.90 | 20 | 0.060 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 128454 |
| 51 | `sd_48b473cfc8d263` | AMZN | 0.766 | -9.48 | 25 | 0.038 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 128455 |
| 51 | `sd_48b473cfc8d263` | META | 0.760 | -8.33 | 19 | 0.041 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 128457 |
| 51 | `sd_48b473cfc8d263` | MSFT | n/a | 0.00 | 0 | 0.000 | paper_readiness, positive_expectancy, profit_factor, trade_count | none evidenced | economic_edge + sample_frequency | job 128458 |
| 51 | `sd_8d0bacd555fc75` | AAPL | 0.971 | -0.91 | 29 | 0.048 | paper_readiness, positive_expectancy, profit_factor, trade_count | none evidenced | economic_edge + sample_frequency | job 128264 |
| 51 | `sd_8d0bacd555fc75` | AMZN | 0.720 | -6.84 | 24 | 0.041 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 128265 |
| 51 | `sd_8d0bacd555fc75` | META | 1.209 | 4.35 | 23 | 0.019 | paper_readiness, trade_count | none evidenced | sample-only | job 128267 |
| 51 | `sd_8d0bacd555fc75` | MSFT | 0.884 | -3.89 | 25 | 0.040 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 128268 |
| 51 | `sd_f0dbcb5b00d6fd` | AAPL | 1.529 | 30.44 | 20 | 0.070 | paper_readiness, trade_count | none evidenced | sample-only | job 128559 |
| 51 | `sd_f0dbcb5b00d6fd` | AMZN | 0.710 | -21.59 | 24 | 0.083 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility, sideways | economic_edge + sample_frequency | job 128560 |
| 51 | `sd_f0dbcb5b00d6fd` | META | 0.740 | -19.60 | 23 | 0.106 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility, sideways | economic_edge + sample_frequency | job 128562 |
| 51 | `sd_f0dbcb5b00d6fd` | MSFT | 0.391 | -50.92 | 13 | 0.107 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 128563 |
| 53 | `sd_39488438e22427` | NVDA | 0.700 | -15.28 | 41 | 0.112 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, normal_volatility, sideways | economic_edge | job 129020 |
| 53 | `sd_39488438e22427` | TSLA | 0.388 | -37.55 | 40 | 0.161 | maximum_drawdown, paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, normal_volatility, sideways | economic_edge + risk | job 129021 |
| 53 | `sd_646130b9499e2e` | NVDA | 0.775 | -16.37 | 24 | 0.100 | paper_readiness, positive_expectancy, profit_factor, trade_count | low_volatility, normal_volatility, sideways | economic_edge + sample_frequency | job 128930 |
| 53 | `sd_646130b9499e2e` | TSLA | 0.562 | -36.15 | 21 | 0.122 | maximum_drawdown, paper_readiness, positive_expectancy, profit_factor, trade_count | low_volatility, normal_volatility, sideways | economic_edge + sample_frequency + risk | job 128931 |
| 53 | `sd_70c547e6101346` | NVDA | 0.343 | -23.42 | 23 | 0.069 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility, sideways | economic_edge + sample_frequency | job 128939 |
| 53 | `sd_70c547e6101346` | TSLA | 0.841 | -6.19 | 29 | 0.049 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, normal_volatility | economic_edge + sample_frequency | job 128940 |
| 53 | `sd_ad01861adeeb06` | NVDA | 0.569 | -18.16 | 40 | 0.116 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, normal_volatility, sideways | economic_edge | job 128849 |
| 53 | `sd_ad01861adeeb06` | TSLA | 0.784 | -9.14 | 42 | 0.096 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, normal_volatility, sideways | economic_edge | job 128850 |
| 53 | `sd_d27ece03fcf410` | NVDA | 0.609 | -17.09 | 36 | 0.090 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, sideways | economic_edge | job 129062 |
| 53 | `sd_d27ece03fcf410` | TSLA | 0.621 | -17.15 | 34 | 0.089 | paper_readiness, positive_expectancy, profit_factor | bull_trend, normal_volatility | economic_edge | job 129063 |
| 53 | `sd_f53399088d2cc8` | NVDA | 0.606 | -17.05 | 40 | 0.113 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, normal_volatility, sideways | economic_edge | job 128969 |
| 53 | `sd_f53399088d2cc8` | TSLA | 0.822 | -7.60 | 42 | 0.098 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, normal_volatility | economic_edge | job 128970 |
| 54 | `sd_06bb96a42ee727` | AAPL | 1.080 | 2.89 | 32 | 0.047 | paper_readiness, profit_factor | none evidenced | economic_edge | job 129334 |
| 54 | `sd_06bb96a42ee727` | AMZN | 0.457 | -24.15 | 37 | 0.097 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility | economic_edge | job 129335 |
| 54 | `sd_06bb96a42ee727` | META | 0.561 | -19.35 | 29 | 0.066 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility, sideways | economic_edge + sample_frequency | job 129337 |
| 54 | `sd_06bb96a42ee727` | MSFT | 0.586 | -20.06 | 27 | 0.064 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 129338 |
| 54 | `sd_0ec396a028e49b` | AAPL | 0.971 | -0.91 | 29 | 0.048 | paper_readiness, positive_expectancy, profit_factor, trade_count | none evidenced | economic_edge + sample_frequency | job 129144 |
| 54 | `sd_0ec396a028e49b` | AMZN | 0.720 | -6.84 | 24 | 0.041 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 129145 |
| 54 | `sd_0ec396a028e49b` | META | 1.209 | 4.35 | 23 | 0.019 | paper_readiness, trade_count | none evidenced | sample-only | job 129147 |
| 54 | `sd_0ec396a028e49b` | MSFT | 0.884 | -3.89 | 25 | 0.040 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 129148 |
| 54 | `sd_128969e76fa303` | AAPL | 1.021 | 0.76 | 30 | 0.052 | paper_readiness, profit_factor | low_volatility | economic_edge | job 129124 |
| 54 | `sd_128969e76fa303` | AMZN | 0.563 | -17.30 | 30 | 0.060 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility | economic_edge | job 129125 |
| 54 | `sd_128969e76fa303` | META | 0.919 | -2.46 | 24 | 0.031 | paper_readiness, positive_expectancy, profit_factor, trade_count | none evidenced | economic_edge + sample_frequency | job 129127 |
| 54 | `sd_128969e76fa303` | MSFT | 0.821 | -7.52 | 24 | 0.050 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 129128 |
| 54 | `sd_244dd7c011766b` | AAPL | 1.529 | 30.44 | 20 | 0.070 | paper_readiness, trade_count | none evidenced | sample-only | job 129149 |
| 54 | `sd_244dd7c011766b` | AMZN | 0.710 | -21.59 | 24 | 0.083 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility, sideways | economic_edge + sample_frequency | job 129150 |
| 54 | `sd_244dd7c011766b` | META | 0.740 | -19.60 | 23 | 0.106 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility, sideways | economic_edge + sample_frequency | job 129152 |
| 54 | `sd_244dd7c011766b` | MSFT | 0.391 | -50.92 | 13 | 0.107 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 129153 |
| 54 | `sd_2d0392e7c9f06e` | AAPL | 0.789 | -6.59 | 34 | 0.063 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility | economic_edge | job 129354 |
| 54 | `sd_2d0392e7c9f06e` | AMZN | 0.723 | -6.23 | 33 | 0.028 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility | economic_edge | job 129355 |
| 54 | `sd_2d0392e7c9f06e` | META | 1.028 | 0.59 | 28 | 0.030 | paper_readiness, profit_factor, trade_count | none evidenced | economic_edge + sample_frequency | job 129357 |
| 54 | `sd_2d0392e7c9f06e` | MSFT | 0.873 | -3.92 | 28 | 0.033 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 129358 |
| 54 | `sd_882eb33eb2e893` | AAPL | 1.264 | 16.48 | 22 | 0.070 | paper_readiness, trade_count | none evidenced | sample-only | job 129134 |
| 54 | `sd_882eb33eb2e893` | AMZN | 0.591 | -30.94 | 21 | 0.101 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 129135 |
| 54 | `sd_882eb33eb2e893` | META | 0.601 | -31.73 | 23 | 0.113 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility, normal_volatility, sideways | economic_edge + sample_frequency | job 129137 |
| 54 | `sd_882eb33eb2e893` | MSFT | 0.494 | -40.13 | 11 | 0.086 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 129138 |
| 54 | `sd_8e53615f4b38c6` | AAPL | 0.803 | -7.38 | 31 | 0.057 | paper_readiness, positive_expectancy, profit_factor | low_volatility | economic_edge | job 129364 |
| 54 | `sd_8e53615f4b38c6` | AMZN | 0.391 | -21.40 | 25 | 0.064 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 129365 |
| 54 | `sd_8e53615f4b38c6` | META | 0.707 | -8.71 | 27 | 0.033 | paper_readiness, positive_expectancy, profit_factor, trade_count | low_volatility, normal_volatility | economic_edge + sample_frequency | job 129367 |
| 54 | `sd_8e53615f4b38c6` | MSFT | 0.537 | -19.25 | 26 | 0.063 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 129368 |
| 54 | `sd_96791bfdebfde1` | AAPL | 0.705 | -13.90 | 20 | 0.060 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 129139 |
| 54 | `sd_96791bfdebfde1` | AMZN | 0.766 | -9.48 | 25 | 0.038 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 129140 |
| 54 | `sd_96791bfdebfde1` | META | 0.760 | -8.33 | 19 | 0.041 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 129142 |
| 54 | `sd_96791bfdebfde1` | MSFT | n/a | 0.00 | 0 | 0.000 | paper_readiness, positive_expectancy, profit_factor, trade_count | none evidenced | economic_edge + sample_frequency | job 129143 |
| 54 | `sd_bd71f1dd42c538` | AAPL | 1.531 | 30.50 | 24 | 0.065 | paper_readiness, trade_count | none evidenced | sample-only | job 129344 |
| 54 | `sd_bd71f1dd42c538` | AMZN | 0.640 | -26.68 | 23 | 0.122 | maximum_drawdown, paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency + risk | job 129345 |
| 54 | `sd_bd71f1dd42c538` | META | 0.508 | -40.50 | 22 | 0.118 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility, normal_volatility, sideways | economic_edge + sample_frequency | job 129347 |
| 54 | `sd_bd71f1dd42c538` | MSFT | 0.677 | -24.12 | 16 | 0.101 | paper_readiness, positive_expectancy, profit_factor, trade_count | bull_trend, low_volatility | economic_edge + sample_frequency | job 129348 |
| 54 | `sd_f12abb27e9f410` | AAPL | 0.612 | -14.75 | 56 | 0.103 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, sideways | economic_edge | job 129129 |
| 54 | `sd_f12abb27e9f410` | AMZN | 0.484 | -21.09 | 48 | 0.111 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, sideways | economic_edge | job 129130 |
| 54 | `sd_f12abb27e9f410` | META | 0.381 | -26.11 | 35 | 0.110 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, normal_volatility, sideways | economic_edge | job 129132 |
| 54 | `sd_f12abb27e9f410` | MSFT | 0.565 | -18.41 | 43 | 0.089 | paper_readiness, positive_expectancy, profit_factor | bull_trend, low_volatility, sideways | economic_edge | job 129133 |

## Appendix C — evidence artifacts

- `evidence.json`: full source-bound data, candidate diagnostics, rate intervals, regime pools, asset profiles, clusters, hypotheses, and matched parameter mutations.
- `diagnose_transfer_failure.py`: read-only reproduction script. It performs no database writes, launches no campaign, and changes no threshold.
- Primary source tables: `research_candidate_stage_evidence`, `research_campaign_jobs`, `research_campaigns`, `research_dataset_manifests`, `research_dataset_candles`, `asset_profile_versions`, `asset_cluster_versions`, `asset_cluster_members`, and `research_hypothesis_versions`.

