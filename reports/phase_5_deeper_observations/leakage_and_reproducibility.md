# Leakage And Reproducibility

- Calculation version: `deeper_market_observations_v1`
- Future bars used: `False`
- Decision point: each observation uses only candles at or before its timestamp.
- Minimum rolling history: `60` bars.
- Regression coverage includes a future-candle mutation test that verifies earlier observations do not change.
- Existing validation policy remains `strong_research_gates:v1`.