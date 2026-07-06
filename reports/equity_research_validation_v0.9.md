# KefTrade v0.9 Equity Research Validation Report

Generated: 2026-07-06

## Scope

This report uses the existing deterministic KefTrade research, validation, and intelligence pipeline.

Datasets reviewed:

- SPY 1d
- QQQ 1d
- AAPL 1d
- MSFT 1d
- NVDA 1d
- TSLA 1d
- BTCUSDT 1d
- ETHUSDT 1d

No strategies were optimized. Evidence thresholds were not changed.

## Validation Run

Alpha validation run: `3`

Best candidate: `validation_dac45e27ee`

Recommendation: `Reject`

Metrics:

- Profit factor: `0.8715`
- Expectancy: `-8.2887`
- Trade count: `212`

Evidence rules:

- Minimum trades: passed
- Profit factor: failed
- Stability: failed
- Confidence interval: failed

Conclusion: no validated alpha was found.

## Asset Summary

| Asset | Group | Candles | Top Strategy | Recommendation | Profit Factor | Expectancy | Trades |
|---|---|---:|---|---|---:|---:|---:|
| QQQ | equity | 1500 | volatility_breakout_v1_005 | Reject | n/a | 197.3461 | 2 |
| ETHUSDT | crypto | 2478 | volatility_breakout_v1_005 | Reject | 0.9421 | -4.0800 | 18 |
| NVDA | equity | 1500 | trend_pullback_v1_001 | Reject | 0.7287 | -20.9718 | 20 |
| SPY | equity | 1500 | trend_pullback_v1_001 | Reject | 0.6701 | -25.7483 | 14 |
| TSLA | equity | 1500 | trend_pullback_v1_001 | Reject | 0.6336 | -29.2634 | 11 |
| MSFT | equity | 1500 | trend_pullback_v1_001 | Reject | 0.4935 | -46.9086 | 8 |
| AAPL | equity | 1500 | trend_pullback_v1_001 | Reject | 0.3796 | -78.4824 | 13 |
| BTCUSDT | crypto | 2478 | trend_pullback_v1_001 | Reject | 0.2838 | -87.9535 | 19 |

QQQ had the strongest top-line result, but it only produced 2 trades. That is not enough evidence to claim an edge.

BTCUSDT and AAPL were the weakest assets by top-strategy expectancy in this pass.

## Strategy Summary

| Strategy | Avg Rank Score | Avg Profit Factor | Avg Expectancy | Rejects | Research More | Paper Candidates |
|---|---:|---:|---:|---:|---:|---:|
| volatility_breakout_v1 | -10.9259 | 0.8936 | 31.2527 | 8 | 0 | 0 |
| trend_pullback_v1 | -17.9034 | 0.5810 | -41.8218 | 8 | 0 | 0 |
| momentum_v1 | -28.2816 | 1.5153 | 19.0609 | 8 | 0 | 0 |
| breakout_v1 | -43.7327 | 1.6769 | 18.3471 | 8 | 0 | 0 |
| mean_reversion_v1 | -58.4438 | 0.1258 | -23.8901 | 8 | 0 | 0 |
| trend_following_200ema_v1 | -60.6869 | 1.0347 | 9.9431 | 8 | 0 | 0 |

Every strategy family was rejected on every reviewed asset.

Some strategies show positive average expectancy, but the evidence is weak because the runs still failed the platform's deterministic acceptance logic, usually due to low trade count, instability, or poor aggregate validation.

## Market Regime Findings

| Regime | Samples | Avg Expectancy | Avg Profit Factor | Avg Trades |
|---|---:|---:|---:|---:|
| bear_trend | 6 | -110.2698 | 0.0000 | 1.17 |
| sideways | 27 | -47.5869 | 0.5701 | 4.33 |
| bull_trend | 41 | 20.9168 | 1.2870 | 4.59 |

Most hostile regimes:

- `bear_trend`
- `sideways`

Bull trend was the only reviewed regime with positive average expectancy, but the sample sizes remain small. This is not an edge claim.

## Research Intelligence Findings

Most common failure reasons across validation history:

- Profit factor evidence rule failed: 11 candidate records
- Stability evidence rule failed: 11 candidate records
- Confidence interval evidence rule failed: 11 candidate records
- Minimum trade count failed: 5 candidate records

Most common rejection rules:

- `stability`: 11 candidate records
- `profit_factor`: 11 candidate records
- `confidence_interval`: 11 candidate records
- `min_trades`: 5 candidate records

Evidence-backed recommendations from research intelligence:

- Test stricter filters around `sideways`.
- Test stricter filters around `bear_trend`.
- Investigate recurring `stability` rejection.
- Investigate recurring `profit_factor` rejection.
- Investigate recurring `confidence_interval` rejection.

All recommendations are supported by previous validation records. Confidence remains low because the evidence base is still early and dominated by rejected candidates.

## Pass/Fail Result

No strategy passed evidence gates.

Candidates marked `Research More`: `0`

Candidates marked `Candidate for Paper Trading`: `0`

## Conclusion

KefTrade's stock-first research pipeline is working. The platform can now ingest US equity data, run deterministic diagnostics, compare equities against crypto, and reject weak strategy evidence without forcing attractive results.

The current equity evidence does not justify paper trading.
