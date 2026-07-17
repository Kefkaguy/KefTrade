# Source-Backed Discovery Report

Engine version: `edge_discovery_engine_v1`
Jobs analyzed: `3588`
Unique executable keys: `592`

## Generated Hypotheses

### Hypothesis `96` - Trend Following frequency-condition test on AAPL/AMD/AMZN/GOOGL

- Status: `proposed`
- Strategy family: `Trend Following`
- Scope: `cluster:cluster_c4c33a4f5dbdf80acbc6`
- Discovery type: `frequency_condition`
- Source dataset: `1`
- Campaign IDs: `50, 51, 52, 53, 54, 55`
- Sample size: `3300` jobs, `512` executable keys
- Confidence score: `0.74`
- Label: `Post-hoc and unconfirmed.`

On an independent frozen AAPL/AMD/AMZN/GOOGL dataset, Trend Following candidates that relax only the dominant frequency-sensitive conditions identified by Edge Discovery will reach at least 30 trades per market without reducing median profit factor below the matched family baseline.

Supporting evidence refs: `2816`. Contradictory evidence refs: `484`.

### Hypothesis `97` - Trend Following economic-filter test on AAPL/AMD/AMZN/GOOGL

- Status: `proposed`
- Strategy family: `Trend Following`
- Scope: `cluster:cluster_c4c33a4f5dbdf80acbc6`
- Discovery type: `economic_failure_condition`
- Source dataset: `1`
- Campaign IDs: `50, 51, 52, 53, 54, 55`
- Sample size: `3300` jobs, `512` executable keys
- Confidence score: `0.74`
- Label: `Post-hoc and unconfirmed.`

On an independent frozen AAPL/AMD/AMZN/GOOGL dataset, Trend Following generation should reject or materially alter parameter regions matching the observed losing buckets unless the candidate shows positive expectancy and PF >= 1.2 on at least two markets.

Supporting evidence refs: `2424`. Contradictory evidence refs: `876`.

### Hypothesis `98` - Breakout economic-filter test on QQQ/SPY

- Status: `proposed`
- Strategy family: `Breakout`
- Scope: `cluster:cluster_899a8ec60d3869eb0930`
- Discovery type: `economic_failure_condition`
- Source dataset: `1`
- Campaign IDs: `56, 64`
- Sample size: `36` jobs, `10` executable keys
- Confidence score: `0.74`
- Label: `Post-hoc and unconfirmed.`

On an independent frozen QQQ/SPY dataset, Breakout generation should reject or materially alter parameter regions matching the observed losing buckets unless the candidate shows positive expectancy and PF >= 1.2 on at least two markets.

Supporting evidence refs: `36`. Contradictory evidence refs: `0`.

### Hypothesis `99` - Volatility Expansion economic-filter test on QQQ/SPY

- Status: `proposed`
- Strategy family: `Volatility Expansion`
- Scope: `cluster:cluster_899a8ec60d3869eb0930`
- Discovery type: `economic_failure_condition`
- Source dataset: `1`
- Campaign IDs: `60, 68`
- Sample size: `36` jobs, `10` executable keys
- Confidence score: `0.74`
- Label: `Post-hoc and unconfirmed.`

On an independent frozen QQQ/SPY dataset, Volatility Expansion generation should reject or materially alter parameter regions matching the observed losing buckets unless the candidate shows positive expectancy and PF >= 1.2 on at least two markets.

Supporting evidence refs: `36`. Contradictory evidence refs: `0`.

### Hypothesis `100` - Pullback economic-filter test on QQQ/SPY

- Status: `proposed`
- Strategy family: `Pullback`
- Scope: `cluster:cluster_899a8ec60d3869eb0930`
- Discovery type: `economic_failure_condition`
- Source dataset: `1`
- Campaign IDs: `58, 66`
- Sample size: `36` jobs, `10` executable keys
- Confidence score: `0.74`
- Label: `Post-hoc and unconfirmed.`

On an independent frozen QQQ/SPY dataset, Pullback generation should reject or materially alter parameter regions matching the observed losing buckets unless the candidate shows positive expectancy and PF >= 1.2 on at least two markets.

Supporting evidence refs: `36`. Contradictory evidence refs: `0`.

### Hypothesis `101` - Continuation economic-filter test on QQQ/SPY

- Status: `proposed`
- Strategy family: `Continuation`
- Scope: `cluster:cluster_899a8ec60d3869eb0930`
- Discovery type: `economic_failure_condition`
- Source dataset: `1`
- Campaign IDs: `62, 70`
- Sample size: `36` jobs, `10` executable keys
- Confidence score: `0.74`
- Label: `Post-hoc and unconfirmed.`

On an independent frozen QQQ/SPY dataset, Continuation generation should reject or materially alter parameter regions matching the observed losing buckets unless the candidate shows positive expectancy and PF >= 1.2 on at least two markets.

Supporting evidence refs: `36`. Contradictory evidence refs: `0`.

### Hypothesis `102` - Mean Reversion economic-filter test on QQQ/SPY

- Status: `proposed`
- Strategy family: `Mean Reversion`
- Scope: `cluster:cluster_899a8ec60d3869eb0930`
- Discovery type: `economic_failure_condition`
- Source dataset: `1`
- Campaign IDs: `59, 67`
- Sample size: `36` jobs, `10` executable keys
- Confidence score: `0.74`
- Label: `Post-hoc and unconfirmed.`

On an independent frozen QQQ/SPY dataset, Mean Reversion generation should reject or materially alter parameter regions matching the observed losing buckets unless the candidate shows positive expectancy and PF >= 1.2 on at least two markets.

Supporting evidence refs: `36`. Contradictory evidence refs: `0`.

### Hypothesis `103` - Range Breakout economic-filter test on QQQ/SPY

- Status: `proposed`
- Strategy family: `Range Breakout`
- Scope: `cluster:cluster_899a8ec60d3869eb0930`
- Discovery type: `economic_failure_condition`
- Source dataset: `1`
- Campaign IDs: `61, 69`
- Sample size: `36` jobs, `10` executable keys
- Confidence score: `0.74`
- Label: `Post-hoc and unconfirmed.`

On an independent frozen QQQ/SPY dataset, Range Breakout generation should reject or materially alter parameter regions matching the observed losing buckets unless the candidate shows positive expectancy and PF >= 1.2 on at least two markets.

Supporting evidence refs: `36`. Contradictory evidence refs: `0`.

### Hypothesis `104` - Gap economic-filter test on QQQ/SPY

- Status: `proposed`
- Strategy family: `Gap`
- Scope: `cluster:cluster_899a8ec60d3869eb0930`
- Discovery type: `economic_failure_condition`
- Source dataset: `1`
- Campaign IDs: `63, 71`
- Sample size: `36` jobs, `10` executable keys
- Confidence score: `0.74`
- Label: `Post-hoc and unconfirmed.`

On an independent frozen QQQ/SPY dataset, Gap generation should reject or materially alter parameter regions matching the observed losing buckets unless the candidate shows positive expectancy and PF >= 1.2 on at least two markets.

Supporting evidence refs: `36`. Contradictory evidence refs: `0`.

### Hypothesis `105` - Momentum economic-filter test on QQQ/SPY

- Status: `proposed`
- Strategy family: `Momentum`
- Scope: `cluster:cluster_899a8ec60d3869eb0930`
- Discovery type: `economic_failure_condition`
- Source dataset: `1`
- Campaign IDs: `57, 65`
- Sample size: `36` jobs, `10` executable keys
- Confidence score: `0.74`
- Label: `Post-hoc and unconfirmed.`

On an independent frozen QQQ/SPY dataset, Momentum generation should reject or materially alter parameter regions matching the observed losing buckets unless the candidate shows positive expectancy and PF >= 1.2 on at least two markets.

Supporting evidence refs: `36`. Contradictory evidence refs: `0`.

### Hypothesis `106` - Mean Reversion frequency-condition test on QQQ/SPY

- Status: `proposed`
- Strategy family: `Mean Reversion`
- Scope: `cluster:cluster_899a8ec60d3869eb0930`
- Discovery type: `frequency_condition`
- Source dataset: `1`
- Campaign IDs: `59, 67`
- Sample size: `36` jobs, `10` executable keys
- Confidence score: `0.74`
- Label: `Post-hoc and unconfirmed.`

On an independent frozen QQQ/SPY dataset, Mean Reversion candidates that relax only the dominant frequency-sensitive conditions identified by Edge Discovery will reach at least 30 trades per market without reducing median profit factor below the matched family baseline.

Supporting evidence refs: `35`. Contradictory evidence refs: `1`.

### Hypothesis `107` - Continuation frequency-condition test on QQQ/SPY

- Status: `proposed`
- Strategy family: `Continuation`
- Scope: `cluster:cluster_899a8ec60d3869eb0930`
- Discovery type: `frequency_condition`
- Source dataset: `1`
- Campaign IDs: `62, 70`
- Sample size: `36` jobs, `10` executable keys
- Confidence score: `0.74`
- Label: `Post-hoc and unconfirmed.`

On an independent frozen QQQ/SPY dataset, Continuation candidates that relax only the dominant frequency-sensitive conditions identified by Edge Discovery will reach at least 30 trades per market without reducing median profit factor below the matched family baseline.

Supporting evidence refs: `34`. Contradictory evidence refs: `2`.

### Hypothesis `109` - Hypothesis lifecycle wording consistency interpretation

- Status: `proposed`
- Strategy family: `Trend Following`
- Scope: `universal:hypothesis_lifecycle`
- Discovery type: `hypothesis_lifecycle_interpretation`
- Source dataset: `1`
- Campaign IDs: ``
- Sample size: `36` jobs, `None` executable keys
- Confidence score: `0.72`
- Label: `Post-hoc and unconfirmed.`

On future frozen research records, hypothesis display and campaign planning should use the derived authoritative lifecycle interpretation: confirmed wording in historical text is not confirmation unless the version has independent supported status.

Supporting evidence refs: `36`. Contradictory evidence refs: `0`.
