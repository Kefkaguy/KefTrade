# Opening-range breakout pilot: actual results

Run completed 2026-09-07T22:45:02.339680+00:00. Historical simulation only; no orders submitted.

Universe: 50 fixed current stock names. Dates: 2025-01-02–2026-08-31. Common complete sessions: 394 of 416; excluded dates: 22.

This does not reproduce the original paper’s full historical universe or period. Current-name selection/survivorship bias remains. Both arms use identical data and simulation mechanics. Full assumptions were recorded in [PROTOCOL.md](PROTOCOL.md) before portfolio results were examined.

## Primary cost scenario

Adverse price movement of 2.5 bps per side plus modeled commissions. These costs are assumptions, not measured quotes/fills. Long/short assumes borrow availability and omits borrow/locate costs.

| Capital | Entry cap | Direction | Strategy | Net profit | Annualized | 5-min drawdown | Trades | PF | Average month | Worst month | Months ≥ $1k |
|---:|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| $15,000 | 1x | both_hypothetical_borrow | base | $-8,255.77 | -38.23% | 55.04% | 8,684 | 0.151 | $-412.79 | $-753.64 | 0/20 |
| $15,000 | 1x | both_hypothetical_borrow | top20_rvol | $-7,840.87 | -35.97% | 52.27% | 4,048 | 0.325 | $-392.04 | $-641.40 | 0/20 |
| $15,000 | 1x | long_only | base | $-5,396.10 | -23.57% | 35.97% | 5,392 | 0.159 | $-269.81 | $-472.03 | 0/20 |
| $15,000 | 1x | long_only | top20_rvol | $-4,923.18 | -21.32% | 32.82% | 2,093 | 0.265 | $-246.16 | $-376.85 | 0/20 |
| $15,000 | 4x | both_hypothetical_borrow | base | $-13,770.77 | -77.86% | 91.81% | 10,563 | 0.235 | $-688.54 | $-1,845.35 | 0/20 |
| $15,000 | 4x | both_hypothetical_borrow | top20_rvol | $-13,414.04 | -74.19% | 89.43% | 4,083 | 0.316 | $-670.70 | $-1,619.15 | 0/20 |
| $15,000 | 4x | long_only | base | $-10,697.52 | -52.89% | 71.32% | 7,293 | 0.271 | $-534.88 | $-1,062.25 | 0/20 |
| $15,000 | 4x | long_only | top20_rvol | $-10,157.04 | -49.41% | 67.71% | 2,160 | 0.280 | $-507.85 | $-962.30 | 0/20 |
| $20,000 | 1x | both_hypothetical_borrow | base | $-11,522.65 | -40.39% | 57.61% | 10,914 | 0.179 | $-576.13 | $-949.81 | 0/20 |
| $20,000 | 1x | both_hypothetical_borrow | top20_rvol | $-10,052.11 | -34.36% | 50.26% | 4,166 | 0.347 | $-502.61 | $-830.21 | 0/20 |
| $20,000 | 1x | long_only | base | $-7,139.10 | -23.37% | 35.70% | 6,451 | 0.196 | $-356.96 | $-610.41 | 0/20 |
| $20,000 | 1x | long_only | top20_rvol | $-6,301.68 | -20.40% | 31.51% | 2,139 | 0.288 | $-315.08 | $-481.85 | 0/20 |
| $20,000 | 4x | both_hypothetical_borrow | base | $-18,371.69 | -77.95% | 91.86% | 12,244 | 0.269 | $-918.58 | $-2,317.91 | 0/20 |
| $20,000 | 4x | both_hypothetical_borrow | top20_rvol | $-17,723.82 | -73.01% | 88.62% | 4,178 | 0.327 | $-886.19 | $-2,131.52 | 0/20 |
| $20,000 | 4x | long_only | base | $-13,895.25 | -51.09% | 69.48% | 7,780 | 0.300 | $-694.76 | $-1,357.36 | 0/20 |
| $20,000 | 4x | long_only | top20_rvol | $-13,374.30 | -48.62% | 66.87% | 2,165 | 0.287 | $-668.71 | $-1,276.68 | 0/20 |

## Cost sensitivity: $20,000, 1x, hypothetical long/short

| Adverse bps per side | Strategy | Net profit | Annualized | Drawdown |
|---:|---|---:|---:|---:|
| 0 | base | $-10,702.40 | -36.98% | 53.51% |
| 0 | top20_rvol | $-8,337.02 | -27.75% | 41.69% |
| 2.5 | base | $-11,522.65 | -40.39% | 57.61% |
| 2.5 | top20_rvol | $-10,052.11 | -34.36% | 50.26% |
| 5 | base | $-12,182.13 | -43.23% | 60.91% |
| 5 | top20_rvol | $-11,710.16 | -41.19% | 58.55% |

## Paired selection difference

Circular 20-session block bootstrap, 2,000 draws, fixed seed 20260907. Difference is filtered minus base in mean daily account-return basis points, preserving same-day dependence between arms. This is exploratory uncertainty on this selected sample, not a multiple-testing-adjusted edge certificate.

- both_hypothetical_borrow: 4.084 bps/day; approximate 95% interval [1.921, 6.491].

- long_only: 1.606 bps/day; approximate 95% interval [0.504, 2.699].

## Interpretation limits

- A positive result is evidence only for this limited historical pilot. It cannot establish the paper’s reported 41.6% return, an untouched holdout result, or live profitability.
- Five-minute bars cannot establish event ordering. Entry-bar stops are deliberately adverse assumptions. Drawdown uses five-minute closes, not the worst tick. End-of-session close is an execution proxy.
- Capital is allocated equally across selected sleeves, not risked at 1% of the entire account on every trade. Unused sleeves remain cash. 4x scenarios do not model broker margin calls or financing/borrow constraints.
- Monthly profits are compounded, not withdrawn. Counts above $1k/$2k describe simulated months; they are not dependable income or a withdrawal analysis.
- A 30% drawdown stop was not imposed. Historical drawdown cannot guarantee future loss stays below 30%.
- Original-universe membership, delistings, historical borrow, actual spread/impact, exchange/regulatory charges, and post-download revisions are unresolved.

## Verification and artifacts

All 48 scenario ledgers reconcile trade P&L to daily equity and summary profit. Entry-notional caps and entry/exit sequencing checked on every saved trade.

- [All scenario metrics](summary.csv)
- [Detailed results including monthly P&L](results.json)
- [Data completeness and SHA-256 hashes](quality.json)
- [Runner](run.py), [execution tests](test_run.py), [frozen protocol](PROTOCOL.md)
- Per-scenario trade and daily-equity CSV files are retained beside this report. Downloaded market data are cached in `data/`.

## Sources

- [Original strategy paper](https://concretumgroup.com/wp-content/uploads/2026/02/A-Profitable-Day-Trading-Strategy-For-The-U.S.-Equity-Market.pdf)
- [Alpaca historical bars API](https://docs.alpaca.markets/us/reference/stockbars)
