# Phase 2 strategy-family architecture

## Scope

Phase 2 adds eight executable strategy families to the existing KefTrade research path. It does not add a second pipeline, a service, a UI page, a validation exception, or any live-routing behavior.

The execution path remains:

```text
frozen observation evidence
→ versioned, falsifiable hypothesis
→ DiscoveryCandidate generation
→ existing campaign jobs
→ existing backtester and walk-forward split
→ unchanged single-market and cross-market gates
→ existing candidate-stage evidence
→ existing learning records
→ existing archive
```

Implementation version: `research_strategy_families_v1`.

## Shared controls

- Candidate allocation remains 70% exploitation, 20% nearby controlled mutation, and 10% broader exploration.
- Candidate generation is deterministic for a fixed hypothesis version, configuration, and seed.
- Candidate IDs include the executable family definition and lineage.
- Campaign deduplication uses executable parameters, not labels or research metadata.
- Nearby mutations change one executable parameter from that family's controlled range.
- Broader exploration uses a separate bounded range set; it does not enlarge the campaign budget.
- Every candidate uses the existing simulation-only `DiscoveryCandidate`, backtester, validation, learning, and archive structures.
- Every family uses the same validation policy. No threshold override is accepted.
- Family observations derived from an already inspected dataset are stored as `post_hoc=true` and `confirmation_status=unconfirmed`.
- A same-dataset regression pass is retained as evidence but cannot change a post-hoc hypothesis to `supported`.

## Family specifications

### Breakout

Observation set: prior-range compression, breakout distance, rolling-median volume expansion, and close location.

Hypothesis template: on the measured scope, a close above a parameterized prior high after a bounded true-range contraction and rolling-median volume confirmation will have positive walk-forward expectancy under the unchanged policy.

Expected behavior and success: the compressed range resolves with follow-through. A single-market result must pass every existing gate; cluster support also requires the unchanged cross-market gates.

Entry: the close must exceed the prior rolling high by a controlled buffer. Confirmation requires the short true-range median to be compressed relative to a longer baseline, volume participation, and a directional close. This is not the same as Range Breakout: it is defined by a rolling high plus pre-break compression.

Controlled variation covers breakout lookback and buffer, compression length and ratio, volume ratio, ATR stop scale, reward/risk, and holding period.

Falsification: unchanged trade-count, profit-factor, expectancy, drawdown, walk-forward, stability, or transfer gates reject the candidate.

### Momentum

Observation set: short-horizon return, long-horizon return, return acceleration, RSI/MACD alignment, and a new short-horizon price high.

Hypothesis template: on the measured scope, positive parameterized short- and long-horizon returns with acceleration will persist with positive walk-forward expectancy under the unchanged policy.

Expected behavior and success: the accelerating move persists beyond the signal bar, and at least one candidate passes every existing gate with positive out-of-sample expectancy.

Entry: multi-horizon returns must be positive, the current short-horizon return must improve on the preceding short interval, and price must confirm. This is not a trend-following label: it explicitly measures acceleration and compares adjacent return windows.

Controlled variation covers both horizons, both minimum returns, acceleration, ATR stop scale, reward/risk, and holding period.

Falsification: acceleration-filtered candidates fail unchanged sample or economic gates, or lose the edge out of sample.

### Pullback

Observation set: fast/slow trend alignment, peak-to-close pullback depth, distance from the trend reference, directional reclaim, and RSI band.

Hypothesis template: on the measured scope, a retracement inside a parameterized EMA-aligned trend that reclaims direction will have positive walk-forward expectancy under the unchanged policy.

Expected behavior and success: the trend resumes after a bounded retracement, and at least one candidate passes every existing gate without losing walk-forward or regime stability.

Entry: a bounded retracement inside an EMA-aligned uptrend must reclaim direction and close above the prior bar. This differs from Continuation: Pullback measures retracement from a rolling peak, while Continuation requires an impulse-pause-resumption sequence.

Controlled variation covers pullback lookback and depth band, reclaim buffer, RSI floor, ATR/swing stop scale, reward/risk, and holding period.

Falsification: the cohort fails unchanged validation or remains source-asset-specific.

### Mean Reversion

Observation set: downside distance from EMA20, RSI overextension, upward reversal body, prior-close reclaim, and an EMA50 trend-distance exclusion.

Hypothesis template: on the measured scope, a parameterized downside extension from EMA20 with oversold RSI and an upward reversal will revert with positive walk-forward expectancy under the unchanged policy.

Expected behavior and success: the move reverts toward its local mean, and at least one candidate passes every existing gate including drawdown and regime stability.

Entry: price must be measurably below its local mean and reverse upward outside a strongly displaced trend. This family does not reuse the long-only trend filter used by continuation families.

Controlled variation covers extension distance, RSI ceiling, trend exclusion, reversal body, ATR/swing stop scale, conservative reward/risk, and short holding period.

Falsification: expectancy remains non-positive, drawdown exceeds the unchanged gate, or performance is unstable across measured conditions.

### Volatility Expansion

Observation set: current true range relative to its rolling median, realized volatility, directional close location, positive return, and rolling-median volume.

Hypothesis template: on the measured scope, a parameterized current-bar range expansion with a directional close and rolling-median volume confirmation will continue with positive walk-forward expectancy under the unchanged policy.

Expected behavior and success: the directional expansion persists, and at least one candidate passes every existing gate without excessive drawdown or unstable high-volatility behavior.

Entry: the current bar itself must expand beyond the recent range baseline and close directionally. It is not a breakout label because no prior price boundary is required.

Controlled variation covers range baseline, expansion ratio, close location, volatility floor, volume ratio, ATR stop scale, reward/risk, and holding period.

Falsification: expansion bars reverse, fail unchanged economic/sample gates, or become unstable in high volatility.

### Range Breakout

Observation set: consolidation duration, normalized consolidation width, range boundary, break distance, and volume participation.

Hypothesis template: on the measured scope, a close beyond a parameterized duration-defined narrow consolidation will have positive walk-forward expectancy under the unchanged policy.

Expected behavior and success: the bounded consolidation resolves with follow-through, and at least one candidate passes every existing gate without depending on near-duplicate range definitions.

Entry: the close must leave the high of a duration-defined narrow consolidation. It is distinct from Breakout because the range width and duration define the setup; prior true-range compression is not substituted for that range definition.

Controlled variation covers consolidation length and width, break buffer, volume ratio, structural stop fraction, ATR scale, reward/risk, and holding period.

Falsification: the family fails unchanged gates or its executable definitions collapse into duplicate Breakout strategies.

### Continuation

Observation set: prior impulse return, pause duration, pause depth, resumption beyond the pause high, and trend alignment.

Hypothesis template: on the measured scope, a parameterized impulse followed by a shallow bounded pause and resumption will have positive walk-forward expectancy under the unchanged policy.

Expected behavior and success: persistence resumes after the pause, and at least one candidate passes every existing gate across nearby pause and impulse variations.

Entry: an impulse must be followed by a shallow pause and a measured resumption. It does not enter merely because returns or a rolling trend are positive.

Controlled variation covers impulse horizon and return, pause duration and depth, resumption buffer, ATR/swing stop scale, reward/risk, and holding period.

Falsification: resumption entries fail unchanged gates or quality is not stable under nearby pause/impulse variations.

### Gap

Observation set: open-to-prior-close gap size, same-bar continuation above the open, close location, and volume participation.

Hypothesis template: on the measured scope, a parameterized opening gap that continues above its open with rolling-median volume confirmation will have positive walk-forward expectancy under the unchanged policy.

Expected behavior and success: the displacement continues instead of filling, and at least one candidate passes every existing gate with a sufficient event sample.

Entry: a positive opening displacement must continue in the same bar. It does not use five-bar return as a proxy for a gap.

Controlled variation covers gap size, continuation threshold, close location, volume ratio, structural gap-stop fraction, ATR scale, reward/risk, and holding period.

Falsification: gap events do not reach the unchanged sample gate or systematically fill rather than continue.

## Hypothesis records

Phase 2 hypotheses use the existing `research_hypothesis_versions` structure. The fields that do not have dedicated relational columns remain inside the versioned evidence objects:

- observation and measured family score;
- expected behavior;
- strategy family and cluster/asset scope;
- relevant measured conditions;
- measurable success criteria;
- falsification criteria;
- frozen dataset and profile IDs;
- supporting and contradictory evidence;
- post-hoc status;
- independent-confirmation requirement;
- deterministic generation seed and family version.

The generator can consume each record directly through the existing `create_intelligent_research_campaign` path.

## Learning integration

The existing `research_knowledge_versions`, success/failure patterns, recommendations, confidence history, evolution history, and timeline tables remain the storage format. Learning calculation version `research_learning_v2` adds family-level fields without creating a family-specific format:

- market-job and unique-candidate counts;
- promoted market jobs and promoted candidates;
- jobs per promoted candidate;
- transfer-eligible and transferable candidates;
- family transfer rate;
- median profit factor and expectancy;
- walk-forward and regime-stability survival;
- operational failure rate;
- asset-specific outcome counts;
- successful and failed buckets for all family execution parameters.

Historical `research_learning_v1` rows are not changed.

## Validation invariants

The authoritative validation policy remains `strong_research_gates:v1`:

- single market: at least 30 trades, profit factor at least 1.2, positive expectancy, maximum drawdown 0.12, walk-forward evidence, and paper-readiness/regime stability;
- cross market: at least 60 aggregate trades, profit factor at least 1.2, positive expectancy, maximum drawdown 0.12, stability at least 0.6, at least two assets, and at least one timeframe.

The Phase 2 implementation contains no threshold mutation or family-specific promotion path.
