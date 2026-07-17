# Phase 5 Deeper Observations

Phase 5 extends KefTrade's existing research observation layer with deterministic market-structure measurements.

It does not add infrastructure, UI pages, broker routes, paper-routing paths, live-routing behavior, or validation-threshold changes.

## Observation contract

Every Phase 5 observation:

- is deterministic and reproducible for a frozen candle dataset;
- uses only candles at or before the evaluated timestamp;
- avoids look-ahead leakage;
- has an explicit definition and expected range;
- is represented as a standard hypothesis version when used for generation;
- remains post-hoc and unconfirmed until independently tested on a future frozen dataset.

## Added observation families

- Trend maturity
- Trend acceleration
- Volatility contraction
- Volatility expansion
- Breakout quality
- Pullback quality
- Momentum persistence
- Exhaustion
- Liquidity expansion
- False breakouts
- Structural market shifts

## Pipeline integration

Phase 5 stores its primary deliverable in `research_hypothesis_versions`.

For future frozen datasets, `calculate_asset_profile` also exposes the same measurements under `metrics.market_structure_observations` plus flattened score/event-rate fields. Existing immutable profile rows are not rewritten.

The generated hypotheses use existing generator-consumable `strategy_family` values such as `Momentum`, `Pullback`, `Breakout`, `Range Breakout`, `Volatility Expansion`, `Continuation`, and `Mean Reversion`.

No custom generation glue is required. The existing `generate_targeted_candidates` path can consume these hypotheses directly.

## Validation posture

All Phase 5 hypotheses derived from dataset `1` are:

```text
post-hoc and unconfirmed
```

They are market-description improvements, not confirmed trading edges.

The unchanged validation policy remains `strong_research_gates:v1`.
