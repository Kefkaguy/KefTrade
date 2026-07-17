# Frozen regression comparison

## Comparability

Every row below uses immutable dataset ID `1` / hash `0fcc46465c65d213af34b46a9a744e967e448b2cab39c104072acf2112881750`, QQQ and SPY on 1h, ten candidates, 20 market jobs, a 7/2/1 candidate allocation, and the unchanged validation policy. The control is a deterministic channel-stratified subset of completed campaign `52`, not a newly optimized Trend Following run.

The control replay recomputed all 20 jobs. Every metric payload and rejection/promotion decision matched the stored evidence: 20/20 exact matches.

## Outcome comparison

Profit-factor medians exclude frequency-screened jobs where no profit factor exists; `PF n` exposes that denominator. Expectancy includes the stored zero for frequency-screened jobs.

| Campaign | Family | PF n | Median PF | Median expectancy | WF window | Regime/paper ready | Promoted candidates | Transfer rate | Duplicate rate | Runtime ms | Operational failure |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 52 subset | Trend Following | 10 | 0.696687 | 0.000000 | 100% | 0% | 0/10 | 0% | 0% | 100,028 | 0% |
| 64 | Breakout | 10 | 0.445132 | -9.841075 | 100% | 0% | 0/10 | 0% | 0% | 21,922 | 0% |
| 65 | Momentum | 18 | 0.666936 | -15.257740 | 100% | 0% | 0/10 | 0% | 0% | 7,622 | 0% |
| 66 | Pullback | 2 | 0.604698 | 0.000000 | 100% | 0% | 0/10 | 0% | 0% | 13,958 | 0% |
| 67 | Mean Reversion | 1 | 0.627865 | 0.000000 | 100% | 0% | 0/10 | 0% | 0% | 5,790 | 0% |
| 68 | Volatility Expansion | 4 | 0.919953 | 0.000000 | 100% | 0% | 0/10 | 0% | 0% | 18,925 | 0% |
| 69 | Range Breakout | 15 | 0.448929 | -33.831927 | 100% | 0% | 0/10 | 0% | 0% | 8,916 | 0% |
| 70 | Continuation | 6 | 0.463319 | 0.000000 | 100% | 0% | 0/10 | 0% | 0% | 15,806 | 0% |
| 71 | Gap | 4 | 0.181667 | 0.000000 | 100% | 0% | 0/10 | 0% | 0% | 6,539 | 0% |

Asset-specialist, cluster-elite, universal-elite, confirmed-hypothesis, and jobs-per-promotion metrics are all zero/not applicable for every row because there was no promotion. The Phase 1 transfer baseline remains unchanged at 0/76 attempts and 0/52 unique attempts.

## Interpretation

- No family improved promotion, transfer, regime stability, walk-forward availability, or duplicate rate over the control. Zero duplicates in the new families matches, rather than improves on, the control's zero.
- Volatility Expansion's computed-result median PF was 0.223266 higher than the control (+32.05% descriptively), but its denominator was four jobs versus ten in the control, 16/20 jobs failed the opportunity-frequency screen, and 0/20 passed all gates. No statistical or causal claim is justified.
- Lower runtimes reflect frequent early opportunity-screen rejection and family signal cost. Runtime alone is not validation efficiency and is not treated as evidence of strategy usefulness.
- With 0/20 promoted jobs per family, the 95% Wilson upper bound is 16.1125%. With 0/10 promoted candidates per family it is 27.7533%. The sample is deliberately bounded; increasing it solely to improve a headline would violate the protocol.

Conclusion: no new family is established as provisionally useful on the current evidence. The Volatility Expansion median is a future-validation lead only.
