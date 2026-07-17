# Validation-funnel comparison

All rows use dataset ID `1`, QQQ/SPY 1h, 20 market jobs, and unchanged gates. `Freq rejected` means the setup-opportunity screen established that the job could not reach the 30-trade gate in the available validation window. Gate columns count jobs passing that individual gate; promotion still requires all gates together.

| Campaign | Family | Generated candidates | Jobs | Freq rejected | Trades >=30 | PF >=1.2 | Exp >0 | DD <=0.12 | WF present | Paper/regime ready | Promoted | Specialist | Cluster elite |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 52 subset | Trend Following | 10 | 20 | 10 | 4 | 1 | 4 | 19 | 20 | 0 | 0 | 0 | 0 |
| 64 | Breakout | 10 | 20 | 10 | 7 | 0 | 0 | 12 | 20 | 0 | 0 | 0 | 0 |
| 65 | Momentum | 10 | 20 | 2 | 11 | 0 | 1 | 14 | 20 | 0 | 0 | 0 | 0 |
| 66 | Pullback | 10 | 20 | 18 | 2 | 0 | 0 | 18 | 20 | 0 | 0 | 0 | 0 |
| 67 | Mean Reversion | 10 | 20 | 19 | 1 | 0 | 0 | 20 | 20 | 0 | 0 | 0 | 0 |
| 68 | Volatility Expansion | 10 | 20 | 16 | 4 | 0 | 1 | 19 | 20 | 0 | 0 | 0 | 0 |
| 69 | Range Breakout | 10 | 20 | 5 | 11 | 0 | 0 | 8 | 20 | 0 | 0 | 0 | 0 |
| 70 | Continuation | 10 | 20 | 14 | 1 | 0 | 0 | 18 | 20 | 0 | 0 | 0 | 0 |
| 71 | Gap | 10 | 20 | 16 | 3 | 0 | 0 | 16 | 20 | 0 | 0 | 0 | 0 |

## What stopped promotion

- Profit factor was the universal economic blocker for Phase 2: 0/160 jobs reached the unchanged 1.2 gate.
- Momentum and Volatility Expansion each had one positive-expectancy job, but neither reached PF 1.2 or paper/regime readiness.
- Pullback, Mean Reversion, Continuation, Volatility Expansion, and Gap were mainly constrained by signal frequency in this bounded window.
- Range Breakout was not mainly a frequency failure: 11/20 jobs reached 30 trades, but 0/20 had positive expectancy or PF 1.2, and 12/20 also exceeded the 0.12 drawdown gate.
- All 160 jobs had a walk-forward window. This reports availability, not survival of every quality criterion.

All hypothesis results were rejected and remain unconfirmed: Breakout result `74`, Momentum `77`, Pullback `80`, Mean Reversion `83`, Volatility Expansion `86`, Range Breakout `89`, Continuation `92`, and Gap `95`.
