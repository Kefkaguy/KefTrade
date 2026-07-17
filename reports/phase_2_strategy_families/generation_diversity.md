# Candidate-generation diversity report

## Deterministic space audit

A seed-17, 100-candidate audit was run twice for every family outside campaign state. Each repeat produced the same ordered IDs, 100 unique IDs, and 100 unique executable keys.

| Family | Core combinations | Exploration combinations | Generated | Unique IDs | Unique executable keys | Stable repeat |
|---|---:|---:|---:|---:|---:|---:|
| Breakout | 6,561 | 256 | 100 | 100 | 100 | yes |
| Momentum | 6,561 | 256 | 100 | 100 | 100 | yes |
| Pullback | 6,561 | 256 | 100 | 100 | 100 | yes |
| Mean Reversion | 2,187 | 128 | 100 | 100 | 100 | yes |
| Volatility Expansion | 6,561 | 256 | 100 | 100 | 100 | yes |
| Range Breakout | 6,561 | 256 | 100 | 100 | 100 | yes |
| Continuation | 6,561 | 256 | 100 | 100 | 100 | yes |
| Gap | 6,561 | 256 | 100 | 100 | 100 | yes |

Parameter combinations are hash-ordered by implementation version, family, role, seed, and executable values. The generator therefore does not take the first lexicographic slice of a Cartesian product.

## Bounded-campaign audit

Campaigns `64`-`71` each contained ten unique candidates and ten unique executable keys. Every campaign had seven exploitation, two nearby, and one exploration candidate (14/4/2 jobs after QQQ/SPY expansion). Every nearby candidate retained a parent candidate ID, and tests verify that its controlled mutation changes a family-executable parameter. Exploration uses the family exploration ranges without increasing the requested budget.

Each family dispatches to a different signal definition and explanation. Tests construct a positive setup for all eight paths. Breakout uses a prior rolling high plus true-range compression; Range Breakout uses a duration-defined normalized consolidation. Momentum compares adjacent return horizons for acceleration; Continuation requires impulse, pause, and resumption. Gap uses actual open-to-prior-close displacement rather than a return proxy.

## Duplicate conclusion

- Exact executable duplicates: 0/80 in the final Phase 2 campaigns.
- Candidate-ID collisions: 0/80.
- Structurally equivalent family labels: prevented because the executable family/architecture remains part of the execution identity and the signal dispatch is family-specific.
- Frozen Trend Following control exact duplicates: 0/10, so Phase 2 does not claim a measured exact-duplicate reduction over this control.
- Near-duplicate reduction versus historical generation: **Inconclusive — insufficient evidence.** Nearby one-parameter variants are an intentional 20% cohort, and no pre-Phase-2 historical near-duplicate distance metric exists for an honest before/after estimate.
