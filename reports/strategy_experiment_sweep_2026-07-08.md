# Strategy Experiment Sweep - BTCUSDT 4h and ETHUSDT 4h

Bounded research sweep: `max_runs=20` per experiment and asset. Ranking key: profit factor, expectancy, trade count, stability score, then lower drawdown. Validation thresholds were not changed. No edge is claimed.

Metric columns: `PF`, `Exp`, `Tr`, `Stab`, `DD`.

## BTCUSDT 4h

### trend_pullback_rsi_ema_exit_sweep

| Rank | PF | Exp | Tr | Stab | DD | Parameters |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 0.571 | -32.98 | 37 | 0.167 | 0.123 | rsi 35-60, ema 10/50, dist 0.01, swing 5, rr 1.5 |
| 2 | 0.571 | -32.98 | 37 | 0.167 | 0.123 | rsi 40-60, ema 10/50, dist 0.01, swing 5, rr 1.5 |
| 3 | 0.507 | -39.69 | 34 | 0.167 | 0.135 | rsi 45-60, ema 10/50, dist 0.01, swing 5, rr 1.5 |
| 4 | 0.501 | -38.60 | 50 | 0.167 | 0.193 | rsi 35-65, ema 10/50, dist 0.01, swing 5, rr 1.5 |
| 5 | 0.501 | -38.60 | 50 | 0.167 | 0.193 | rsi 40-65, ema 10/50, dist 0.01, swing 5, rr 1.5 |
| 6 | 0.493 | -41.42 | 31 | 0.167 | 0.129 | rsi 35-55, ema 10/50, dist 0.01, swing 5, rr 1.5 |
| 7 | 0.493 | -41.42 | 31 | 0.167 | 0.129 | rsi 40-55, ema 10/50, dist 0.01, swing 5, rr 1.5 |
| 8 | 0.455 | -43.59 | 47 | 0.167 | 0.205 | rsi 45-65, ema 10/50, dist 0.01, swing 5, rr 1.5 |
| 9 | 0.447 | -46.36 | 42 | 0.143 | 0.195 | rsi 35-60, ema 20/50, dist 0.01, swing 5, rr 1.5 |
| 10 | 0.447 | -46.36 | 42 | 0.143 | 0.195 | rsi 40-60, ema 20/50, dist 0.01, swing 5, rr 1.5 |

### breakout_lookback_volume_exit_sweep

| Rank | PF | Exp | Tr | Stab | DD | Parameters |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 1.118 | 4.91 | 5 | 0.750 | 0.017 | lookback 55, volchg 0.15, swing 5, rr 1.2 |
| 2 | 1.115 | 4.80 | 5 | 0.750 | 0.017 | lookback 55, volchg 0.00, swing 5, rr 1.2 |
| 3 | 1.115 | 4.80 | 5 | 0.750 | 0.017 | lookback 55, volchg 0.05, swing 5, rr 1.2 |
| 4 | 1.054 | 2.39 | 5 | 0.750 | 0.017 | lookback 55, volchg 0.30, swing 5, rr 1.2 |
| 5 | 0.857 | -8.05 | 17 | 0.500 | 0.038 | lookback 12, volchg 0.00, swing 5, rr 1.2 |
| 6 | 0.857 | -8.05 | 17 | 0.500 | 0.038 | lookback 12, volchg 0.05, swing 5, rr 1.2 |
| 7 | 0.857 | -8.08 | 17 | 0.500 | 0.038 | lookback 12, volchg 0.15, swing 5, rr 1.2 |
| 8 | 0.857 | -8.08 | 17 | 0.500 | 0.038 | lookback 12, volchg 0.30, swing 5, rr 1.2 |
| 9 | 0.503 | -32.00 | 8 | 0.333 | 0.041 | lookback 20, volchg 0.15, swing 5, rr 1.2 |
| 10 | 0.503 | -32.00 | 8 | 0.333 | 0.041 | lookback 20, volchg 0.30, swing 5, rr 1.2 |

### mean_reversion_activation_sweep

All top BTCUSDT mean-reversion variants produced zero trades in the bounded sweep. The tested EMA50 guard, RSI oversold levels, and EMA20 stretch requirements did not activate.

### momentum_trend_return_sweep

| Rank | PF | Exp | Tr | Stab | DD | Parameters |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 0.802 | -11.13 | 11 | 0.500 | 0.039 | ret5 0.035, ema_slow 50, swing 8, rr 1.2 |
| 2 | 0.802 | -11.13 | 11 | 0.500 | 0.039 | ret5 0.035, ema_slow 100, swing 8, rr 1.2 |
| 3 | 0.532 | -31.58 | 15 | 0.167 | 0.067 | ret5 0.035, ema_slow 50, swing 5, rr 1.2 |
| 4 | 0.532 | -31.58 | 15 | 0.167 | 0.067 | ret5 0.035, ema_slow 100, swing 5, rr 1.2 |
| 5 | 0.516 | -32.32 | 20 | 0.167 | 0.076 | ret5 0.010, ema_slow 50, swing 8, rr 1.2 |
| 6 | 0.516 | -32.32 | 20 | 0.167 | 0.076 | ret5 0.010, ema_slow 100, swing 8, rr 1.2 |
| 7 | 0.479 | -35.45 | 42 | 0.167 | 0.162 | ret5 0.005, ema_slow 50, swing 5, rr 1.2 |
| 8 | 0.479 | -35.45 | 42 | 0.167 | 0.162 | ret5 0.005, ema_slow 100, swing 5, rr 1.2 |
| 9 | 0.476 | -35.66 | 37 | 0.167 | 0.149 | ret5 0.010, ema_slow 50, swing 5, rr 1.2 |
| 10 | 0.476 | -35.66 | 37 | 0.167 | 0.149 | ret5 0.010, ema_slow 100, swing 5, rr 1.2 |

### volatility_breakout_filter_sweep

| Rank | PF | Exp | Tr | Stab | DD | Parameters |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 1.046 | 2.45 | 10 | 0.800 | 0.041 | lookback 12, vol20 0.010, volchg 0.00, rr 1.2 |
| 2 | 0.794 | -12.24 | 7 | 0.400 | 0.055 | lookback 20, vol20 0.010, volchg 0.00, rr 1.2 |
| 3 | 0.714 | -17.73 | 5 | 0.250 | 0.039 | lookback 34, vol20 0.010, volchg 0.00, rr 1.2 |
| 4 | 0.638 | -23.83 | 13 | 0.400 | 0.057 | lookback 8, vol20 0.010, volchg 0.00, rr 1.2 |
| 5 | 0.539 | -32.08 | 3 | 0.250 | 0.030 | lookback 20, vol20 0.015, volchg 0.00, rr 1.2 |
| 6 | 0.534 | -32.60 | 3 | 0.250 | 0.031 | lookback 12, vol20 0.015, volchg 0.00, rr 1.2 |
| 7 | 0.265 | -61.57 | 5 | 0.400 | 0.046 | lookback 8, vol20 0.015, volchg 0.00, rr 1.2 |
| 8 | 0.000 | 0.00 | 0 | 0.000 | 0.000 | lookback 8, vol20 0.020, volchg 0.00, rr 1.2 |
| 9 | 0.000 | 0.00 | 0 | 0.000 | 0.000 | lookback 8, vol20 0.030, volchg 0.00, rr 1.2 |
| 10 | 0.000 | 0.00 | 0 | 0.000 | 0.000 | lookback 12, vol20 0.020, volchg 0.00, rr 1.2 |

### trend_200ema_momentum_exit_sweep

| Rank | PF | Exp | Tr | Stab | DD | Parameters |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 1.029 | 1.74 | 13 | 0.600 | 0.032 | ret5 0.005, swing 10, rr 1.5 |
| 2 | 0.791 | -14.13 | 15 | 0.400 | 0.047 | ret5 0.000, swing 10, rr 1.5 |
| 3 | 0.727 | -18.69 | 11 | 0.200 | 0.038 | ret5 0.010, swing 10, rr 1.5 |
| 4 | 0.463 | -41.53 | 4 | 0.250 | 0.027 | ret5 0.000, swing 34, rr 1.5 |
| 5 | 0.463 | -41.53 | 4 | 0.250 | 0.027 | ret5 0.005, swing 34, rr 1.5 |
| 6 | 0.463 | -41.53 | 4 | 0.250 | 0.027 | ret5 0.010, swing 34, rr 1.5 |
| 7 | 0.456 | -41.92 | 4 | 0.000 | 0.024 | ret5 0.020, swing 34, rr 1.5 |
| 8 | 0.455 | -42.59 | 4 | 0.250 | 0.028 | ret5 0.000, swing 20, rr 1.5 |
| 9 | 0.455 | -42.59 | 4 | 0.250 | 0.028 | ret5 0.005, swing 20, rr 1.5 |
| 10 | 0.455 | -42.59 | 4 | 0.250 | 0.028 | ret5 0.010, swing 20, rr 1.5 |

## ETHUSDT 4h

### trend_pullback_rsi_ema_exit_sweep

| Rank | PF | Exp | Tr | Stab | DD | Parameters |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 0.912 | -6.05 | 26 | 0.500 | 0.076 | rsi 40-55, ema 10/50, dist 0.01, swing 5, rr 1.5 |
| 2 | 0.895 | -7.09 | 26 | 0.500 | 0.064 | rsi 40-55, ema 20/50, dist 0.01, swing 5, rr 1.5 |
| 3 | 0.857 | -10.07 | 27 | 0.500 | 0.087 | rsi 35-55, ema 10/50, dist 0.01, swing 5, rr 1.5 |
| 4 | 0.841 | -11.06 | 27 | 0.333 | 0.075 | rsi 35-55, ema 20/50, dist 0.01, swing 5, rr 1.5 |
| 5 | 0.760 | -17.56 | 22 | 0.167 | 0.090 | rsi 45-55, ema 10/50, dist 0.01, swing 5, rr 1.5 |
| 6 | 0.658 | -24.77 | 48 | 0.125 | 0.131 | rsi 40-65, ema 10/50, dist 0.01, swing 5, rr 1.5 |
| 7 | 0.655 | -25.00 | 46 | 0.125 | 0.128 | rsi 45-65, ema 10/50, dist 0.01, swing 5, rr 1.5 |
| 8 | 0.640 | -26.35 | 49 | 0.125 | 0.141 | rsi 35-65, ema 10/50, dist 0.01, swing 5, rr 1.5 |
| 9 | 0.632 | -29.30 | 21 | 0.167 | 0.088 | rsi 45-55, ema 20/50, dist 0.01, swing 5, rr 1.5 |
| 10 | 0.608 | -31.48 | 47 | 0.143 | 0.148 | rsi 40-65, ema 20/50, dist 0.01, swing 5, rr 1.5 |

### breakout_lookback_volume_exit_sweep

| Rank | PF | Exp | Tr | Stab | DD | Parameters |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 1.101 | 4.92 | 13 | 0.857 | 0.030 | lookback 20, volchg 0.05, swing 5, rr 1.2 |
| 2 | 0.986 | -0.72 | 14 | 0.571 | 0.036 | lookback 12, volchg 0.30, swing 5, rr 1.2 |
| 3 | 0.955 | -2.35 | 16 | 0.429 | 0.032 | lookback 20, volchg 0.30, swing 5, rr 1.2 |
| 4 | 0.946 | -2.81 | 14 | 0.571 | 0.038 | lookback 20, volchg 0.00, swing 5, rr 1.2 |
| 5 | 0.941 | -3.12 | 14 | 0.429 | 0.032 | lookback 20, volchg 0.15, swing 5, rr 1.2 |
| 6 | 0.828 | -8.95 | 8 | 0.333 | 0.027 | lookback 34, volchg 0.15, swing 5, rr 1.2 |
| 7 | 0.828 | -8.95 | 8 | 0.333 | 0.027 | lookback 34, volchg 0.30, swing 5, rr 1.2 |
| 8 | 0.758 | -14.20 | 16 | 0.143 | 0.050 | lookback 12, volchg 0.15, swing 5, rr 1.2 |
| 9 | 0.691 | -18.76 | 17 | 0.143 | 0.049 | lookback 12, volchg 0.00, swing 5, rr 1.2 |
| 10 | 0.686 | -19.16 | 17 | 0.143 | 0.050 | lookback 12, volchg 0.05, swing 5, rr 1.2 |

### mean_reversion_activation_sweep

| Rank | PF | Exp | Tr | Stab | DD | Parameters |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 0.168 | -78.23 | 4 | 0.000 | 0.038 | rsi_oversold 45, dist -0.015, swing 5, rr 1.0 |
| 2-10 | 0.000 | 0.00 | 0 | 0.000 | 0.000 | Other tested variants produced zero trades |

### momentum_trend_return_sweep

| Rank | PF | Exp | Tr | Stab | DD | Parameters |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 0.824 | -10.19 | 36 | 0.250 | 0.067 | ret5 0.005, ema_slow 50, swing 5, rr 1.2 |
| 2 | 0.824 | -10.19 | 36 | 0.250 | 0.067 | ret5 0.005, ema_slow 100, swing 5, rr 1.2 |
| 3 | 0.765 | -14.25 | 17 | 0.571 | 0.045 | ret5 0.035, ema_slow 50, swing 5, rr 1.2 |
| 4 | 0.765 | -14.25 | 17 | 0.571 | 0.045 | ret5 0.035, ema_slow 100, swing 5, rr 1.2 |
| 5 | 0.727 | -16.59 | 34 | 0.125 | 0.088 | ret5 0.010, ema_slow 50, swing 5, rr 1.2 |
| 6 | 0.727 | -16.59 | 34 | 0.125 | 0.088 | ret5 0.010, ema_slow 100, swing 5, rr 1.2 |
| 7 | 0.707 | -17.58 | 29 | 0.125 | 0.079 | ret5 0.005, ema_slow 50, swing 8, rr 1.2 |
| 8 | 0.707 | -17.58 | 29 | 0.125 | 0.079 | ret5 0.005, ema_slow 100, swing 8, rr 1.2 |
| 9 | 0.660 | -21.78 | 16 | 0.286 | 0.054 | ret5 0.035, ema_slow 50, swing 8, rr 1.2 |
| 10 | 0.660 | -21.78 | 16 | 0.286 | 0.054 | ret5 0.035, ema_slow 100, swing 8, rr 1.2 |

### volatility_breakout_filter_sweep

| Rank | PF | Exp | Tr | Stab | DD | Parameters |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 1.353 | 17.07 | 14 | 0.857 | 0.036 | lookback 8, vol20 0.015, volchg 0.00, rr 1.2 |
| 2 | 1.110 | 5.75 | 2 | 0.750 | 0.011 | lookback 20, vol20 0.020, volchg 0.00, rr 1.2 |
| 3 | 0.822 | -10.46 | 7 | 0.400 | 0.040 | lookback 20, vol20 0.015, volchg 0.00, rr 1.2 |
| 4 | 0.779 | -12.87 | 31 | 0.250 | 0.077 | lookback 8, vol20 0.010, volchg 0.00, rr 1.2 |
| 5 | 0.742 | -15.03 | 23 | 0.143 | 0.077 | lookback 12, vol20 0.010, volchg 0.00, rr 1.2 |
| 6 | 0.727 | -16.74 | 10 | 0.333 | 0.060 | lookback 12, vol20 0.015, volchg 0.00, rr 1.2 |
| 7 | 0.724 | -17.69 | 5 | 0.500 | 0.032 | lookback 8, vol20 0.020, volchg 0.00, rr 1.2 |
| 8 | 0.655 | -20.65 | 17 | 0.286 | 0.081 | lookback 20, vol20 0.010, volchg 0.00, rr 1.2 |
| 9 | 0.617 | -23.14 | 15 | 0.167 | 0.073 | lookback 34, vol20 0.010, volchg 0.00, rr 1.2 |
| 10 | 0.552 | -31.08 | 3 | 0.400 | 0.021 | lookback 12, vol20 0.020, volchg 0.00, rr 1.2 |

### trend_200ema_momentum_exit_sweep

| Rank | PF | Exp | Tr | Stab | DD | Parameters |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 0.428 | -45.14 | 8 | 0.200 | 0.046 | ret5 0.005, swing 10, rr 1.5 |
| 2 | 0.262 | -63.43 | 6 | 0.200 | 0.048 | ret5 0.000, swing 10, rr 1.5 |
| 3 | 0.223 | -69.78 | 7 | 0.000 | 0.059 | ret5 0.010, swing 10, rr 1.5 |
| 4-10 | 0.000 | -101.29 to -101.51 | 3-4 | 0.000 | 0.037-0.049 | swing 20-34, rr 1.5 |

## Summary

1. Profit factor improved most when reward targets were shortened to `rr=1.2` for breakout-style systems and `rr=1.5` for trend systems. This suggests the original fixed 2R+ exits are too ambitious for current 4h crypto behavior.
2. Trade count increased when activation gates were relaxed: lower momentum thresholds (`returns_5_min=0.005`), lower volatility floor (`vol20=0.010`), shorter breakout lookbacks (`8` or `12`), and wider RSI bands (`rsi_max=65`). The added trades mostly remained unprofitable.
3. Drawdown reductions mostly came from selectivity and low trade count, not clearly from robust edge. The lowest drawdowns occur in variants with 2-15 trades, so they are not validation-ready.
4. Best preliminary pockets:
   - ETH volatility breakout: PF `1.353`, 14 trades, stability `0.857`, DD `0.036`.
   - BTC breakout: PF `1.118`, 5 trades, stability `0.750`, DD `0.017`.
   - ETH breakout: PF `1.101`, 13 trades, stability `0.857`, DD `0.030`.
   - BTC volatility breakout: PF `1.046`, 10 trades, stability `0.800`, DD `0.041`.
   - BTC 200 EMA trend: PF `1.029`, 13 trades, stability `0.600`, DD `0.032`.
5. None are ready to claim as an edge. Every profitable-looking pocket has far fewer than 100 trades and needs broader falsification before alpha validation.
