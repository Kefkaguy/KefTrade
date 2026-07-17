# Observation Definitions

## Trend maturity

- Key: `trend_maturity`
- Strategy family: `Pullback`
- Definition: Bars in an aligned EMA20/EMA50 trend, normalized by a capped trend-age window.
- Expected range: `0.0 to 1.0`
- Dataset score: `0.085345`
- Event rate: `0.022042`
- Sample size: `63340`

## Trend acceleration

- Key: `trend_acceleration`
- Strategy family: `Momentum`
- Definition: Recent 5-bar return improvement versus the prior 5-bar return, scaled by recent volatility.
- Expected range: `0.0 to 1.0`
- Dataset score: `0.290507`
- Event rate: `0.241987`
- Sample size: `63340`

## Volatility contraction

- Key: `volatility_contraction`
- Strategy family: `Range Breakout`
- Definition: Recent median true range below the prior baseline true range without using future bars.
- Expected range: `0.0 to 1.0`
- Dataset score: `0.117351`
- Event rate: `0.00535`
- Sample size: `63340`

## Volatility expansion

- Key: `volatility_expansion`
- Strategy family: `Volatility Expansion`
- Definition: Current true range expansion versus the prior rolling median range.
- Expected range: `0.0 to 1.0`
- Dataset score: `0.229371`
- Event rate: `0.164445`
- Sample size: `63340`

## Breakout quality

- Key: `breakout_quality`
- Strategy family: `Breakout`
- Definition: Break distance beyond the prior high, close location, and volume participation measured at the bar.
- Expected range: `0.0 to 1.0`
- Dataset score: `0.153896`
- Event rate: `0.024815`
- Sample size: `63340`

## Pullback quality

- Key: `pullback_quality`
- Strategy family: `Pullback`
- Definition: Retracement depth inside an EMA-aligned trend plus reclaim confirmation.
- Expected range: `0.0 to 1.0`
- Dataset score: `0.119243`
- Event rate: `0.065499`
- Sample size: `63340`

## Momentum persistence

- Key: `momentum_persistence`
- Strategy family: `Continuation`
- Definition: Share of same-direction closes inside the recent lookback, weighted by 5-bar return direction.
- Expected range: `0.0 to 1.0`
- Dataset score: `0.566028`
- Event rate: `0.548706`
- Sample size: `63340`

## Exhaustion

- Key: `exhaustion`
- Strategy family: `Mean Reversion`
- Definition: Overextension from EMA20 combined with RSI extremes and decelerating momentum.
- Expected range: `0.0 to 1.0`
- Dataset score: `0.17896`
- Event rate: `0.029723`
- Sample size: `63340`

## Liquidity expansion

- Key: `liquidity_expansion`
- Strategy family: `Volatility Expansion`
- Definition: Current volume and dollar volume expansion versus prior rolling medians.
- Expected range: `0.0 to 1.0`
- Dataset score: `0.204843`
- Event rate: `0.12112`
- Sample size: `63340`

## False breakout

- Key: `false_breakout`
- Strategy family: `Mean Reversion`
- Definition: A prior boundary break that closes back inside the range using only current and previous bars.
- Expected range: `0.0 to 1.0`
- Dataset score: `0.168519`
- Event rate: `0.174202`
- Sample size: `63340`

## Structural market shift

- Key: `structural_shift`
- Strategy family: `Volatility Expansion`
- Definition: Recent trend and volatility state diverges materially from the preceding baseline.
- Expected range: `0.0 to 1.0`
- Dataset score: `0.472835`
- Event rate: `0.227093`
- Sample size: `63340`
