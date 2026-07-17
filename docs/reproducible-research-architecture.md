# Reproducible Research Architecture

KefTrade now runs new campaigns as versioned scientific experiments:

```text
Snapshot market data
-> build asset profiles
-> cluster measured behavior
-> form a testable hypothesis
-> generate focused candidates
-> backtest and validate
-> classify candidate levels
-> learn from every result
-> archive the experiment
```

The existing worker queue, backtester, validation thresholds, forward validation, and paper-trading boundaries remain in place. This change does not add VPS infrastructure or live execution.

## Dataset modes

- `rolling`: captures the latest available candles when the campaign is created.
- `reproducibility`: captures a named baseline intended for regression and code comparisons.

Both modes materialize exact immutable candle rows. Workers never read newer candles midway through a campaign. Every snapshot stores asset/timeframe counts, time bounds, provider names, per-dataset hashes, and a content hash. A repeated snapshot with identical content is reused.

`POST /research/datasets/{dataset_id}/verify` recalculates the stored counts and hashes. `POST /research/datasets/{dataset_id}/export` writes a restorable compressed bundle.

## Asset intelligence and clustering

Asset profiles are append-only versions calculated from the frozen snapshot. They include volatility, normalized true range, trend persistence and strength, return reversal, breakout follow-through, pullback depth, momentum persistence, volume expansion, gap frequency, regime distribution, and return correlations.

Clustering is deterministic agglomerative average-linkage over standardized behavior metrics and return correlation. Clusters are measured independently per timeframe. Membership evidence, distance to the cluster centroid, and a bounded `1 / (1 + distance)` similarity score are stored.

Clustering may still produce a final grouping to describe every profiled asset, but a forced grouping is not automatically a valid strategy-transfer scope. Cluster hypotheses require an average standardized centroid distance of at most 1.5. Hypothesis confidence uses the measured cohesion score instead of rewarding member count alone. Less-cohesive groups remain visible as descriptive intelligence and continue to produce asset-scoped hypotheses.

Earnings behavior is explicitly marked unavailable until KefTrade has a versioned corporate-event dataset. It is not inferred from price action.

## Hypotheses and generation

Hypotheses are versioned records with a scope (`asset`, `cluster`, or `universal`), observation, expected behavior, strategy family, regimes, confidence, evidence window, supporting evidence, contradictory evidence, and status:

```text
proposed -> testing -> supported | weak | rejected -> retired
```

The default campaign endpoint now uses the intelligent architecture. It chooses a measured cluster hypothesis unless a hypothesis is supplied explicitly. `architecture_mode=legacy` keeps the previous broad generator available for regression work.

Selection prioritizes an untested cluster hypothesis before recycling already-tested ideas. Automatic campaigns require at least 2,000 known observations per independently gated market and rank usable observations before confidence, preventing a small confidence difference from selecting a window that cannot support the required sample. An explicit hypothesis selection can still run a deliberately underpowered diagnostic. Tested weak hypotheses are not repeated automatically; they require explicit refinement or a new evidence version. A later dataset creates a new linked hypothesis version, carrying the prior result as evidence instead of rewriting history. Previously promoted candidates may seed the exploitation channel as explicit parents.

Candidate allocation defaults to:

- 70% hypothesis-aligned exploitation
- 20% controlled nearby mutations
- 10% broader exploration

Every candidate stores its hypothesis, generator version, generation channel, parent candidate, expected behavior, and relevant regimes.

Generator v2 starts from the balanced frequency-aware rule pool instead of the first lexicographically generated combinations. Every candidate is pre-screened for at least 30 setup opportunities, and campaign deduplication uses executable parameters rather than labels or lineage. Nearby Trend Following variants mutate active entry, momentum, volume, holding-period, trend, and reward parameters; inert mutations do not consume research jobs.

Parallel simulation controls use independent worker processes for CPU-bound backtests. Dataset assignments, per-process frozen-data caches, database leases, incremental result commits, pause/resume behavior, and idempotent pool startup remain unchanged. The product default is one worker; selecting 2, 4, or 8 starts that many operating-system processes rather than contending threads.

## Validation and candidate levels

The strong validation policy is immutable and versioned. No learning or autonomous cycle can lower it. Each completed job stores all six gate results: trade count, profit factor, expectancy, drawdown, walk-forward status, and paper readiness.

No-loss runs retain `profit_factor_is_infinite=true`. Validation treats that explicit state as passing the profit-factor gate, while numeric scoring and aggregate persistence use a conservative finite cap. Cross-market profit factor is pooled from gross profit and gross loss when those totals are available.

Candidate evidence is classified without discarding useful specialists:

```text
generated
-> research candidate
-> asset specialist
-> cluster candidate
-> cluster elite
-> universal elite
```

A single-asset pass can become an asset specialist but cannot become a cluster elite. Cluster elites are aggregated only over the measured target cluster. Universal elites require the unchanged cross-market gates plus passes on at least 60% of the tested assets.

The Phase 9.12 known-good cross-validation summary remains pinned in a regression test.

## Campaign 50 diagnostic

Campaign 50 correctly preserved a rejected hypothesis, but it exposed a search-design failure. Its 250 Trend Following candidates produced 1,750 four-hour runs with a median of 8 trades and a maximum of 23. Because the unchanged single-market gate requires 30 trades, zero jobs could pass regardless of their other metrics. In isolation, 629 runs passed profit factor, 785 passed positive expectancy, 1,171 passed regime stability, and 540 passed every quality gate other than trade count. This is evidence of an unreachable sample gate, not evidence that the quality thresholds should be weakened.

The cause was threefold: the higher-confidence four-hour hypothesis was selected without per-market sample feasibility; targeted generation consumed a narrow ordered slice of strict rule combinations; and nearby mutations did not address entry frequency. Generator v2 and feasibility-aware hypothesis selection correct those causes while keeping `strong_research_gates:v1` unchanged. Campaign 50 remains immutable evidence under generator v1.

Follow-up campaigns 51-54 validated the correction against the same frozen dataset. Campaign 51 raised the trade-count funnel from 0/1,750 to 150/500 and found six GOOGL specialists. Campaign 53 found six AMD specialists. Campaign 54 seeded the GOOGL specialists as explicit parents and increased the single-market passes to ten, but none passed a second member. Campaign 52's tighter QQQ/SPY group reached 64/69 trades but failed the unchanged quality gates. These outcomes demonstrate that the remaining broad groups were descriptive clusters, not strategy-transfer clusters; cluster version 2 therefore makes cohesion an eligibility condition rather than manufacturing an elite by lowering validation thresholds.

## Autonomous cycles

`POST /research/architecture/cycles` performs observation, profiling, clustering, and hypothesis selection.

- `approval_mode=manual` stores the plan but does not create a campaign.
- `approval_mode=auto_queue` creates a bounded simulation-only campaign. It does not run live orders and does not weaken validation.

`GET /research/architecture` returns datasets, profiles, clusters, hypotheses, cycles, archives, and the active validation policy.

## Archives and recovery

Campaign completion writes:

- an immutable database archive;
- a compressed campaign manifest containing jobs, rejection evidence, candidate levels, learning records, hypothesis, report, and configuration;
- a compressed exact-candle dataset bundle.

The default directory is `reports/research_archives`. Override it with `KEFTRADE_RESEARCH_ARCHIVE_DIR` so archives can live outside the application checkout.

`restore_dataset_bundle()` verifies the bundle checksum, restores the exact candles, and reruns count/hash verification. `restore_campaign_archive()` restores immutable evidence only; it never resumes workers or deploys a strategy automatically.

The operational reset script preserves dataset manifests, exact candles, profiles, clusters, hypotheses, validation policies, and campaign archives.

## Deployment

Apply migration `028_reproducible_research_architecture.sql` to an existing database. New databases receive it automatically through the Docker initialization directory. The migration is additive and idempotent.

Before relying on an archive location, export a dataset and test restoring it into a disposable database. Keep the archive directory on separately backed-up storage in production.
