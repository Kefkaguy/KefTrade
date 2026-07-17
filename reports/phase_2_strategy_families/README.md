# Phase 2 implementation report

## Decision

Phase 2 is complete. Eight meaningfully distinct executable strategy families now run through KefTrade's existing observation -> hypothesis -> generation -> validation -> classification -> learning -> archive path.

No Phase 3 work was started. No UI page, service, infrastructure component, deployment/VPS behavior, broker behavior, paper-routing behavior, or live-routing behavior was added or changed for Phase 2.

## What was implemented

- `apps/api/app/services/strategy_families.py` defines Breakout, Momentum, Pullback, Mean Reversion, Volatility Expansion, Range Breakout, Continuation, and Gap as distinct executable signal paths. Each definition contains measured observations, a falsifiable hypothesis template, expected behavior, success/falsification rules, controlled core and exploration ranges, entry/confirmation/exit logic, and frequency-sensitive parameters.
- `apps/api/app/services/strategy_discovery.py` generates deterministic family candidates, dispatches them to their actual family signal, and deduplicates by executable parameters. Research labels and lineage cannot manufacture a distinct execution key.
- `apps/api/app/services/research_architecture.py` turns existing frozen profiles/clusters into versioned family hypotheses, consumes those hypotheses through the existing targeted generator, preserves 70/20/10 allocation and lineage, and prevents same-evidence post-hoc hypotheses from becoming supported.
- `apps/api/app/services/research_campaigns.py` records the executable Phase 2 family in the existing campaign path. No family-specific promotion exception was added.
- `apps/api/app/services/research_learning.py` keeps the existing learning tables and adds family statistics, parameter buckets, transfer outcomes, walk-forward/stability outcomes, and operational metrics under calculation version `research_learning_v2`. Historical `research_learning_v1` records remain unchanged.
- `apps/api/tests/test_strategy_families.py` and the additions to the architecture/learning tests cover registry completeness, executable distinction, deterministic IDs, exact allocation, lineage, deduplication, hypothesis evidence state, and learning schema compatibility.
- `docs/phase-2-strategy-families.md` is the family-by-family behavior and hypothesis specification.
- `reports/phase_2_strategy_families/phase2_evidence.py` is a reproducible, guarded evidence utility. It refuses to start when a foreign campaign is active, uses one worker, and can replay the frozen Trend Following control without writing research state.

## Frozen experiment protocol

- Dataset: ID `1`, key `dataset_0fcc46465c65d213af34b46a`, immutable content hash `0fcc46465c65d213af34b46a9a744e967e448b2cab39c104072acf2112881750`.
- Integrity: passed; ten assets have 5,000 1h candles each and approximately 1,451-1,453 4h candles each.
- Bounded comparison scope: cluster `cluster_899a8ec60d3869eb0930`, QQQ and SPY, 1h, ten candidates per family, two market jobs per candidate, seed `0`, one worker.
- Allocation per family: seven exploitation, two nearby controlled mutation, and one exploration candidate; equivalently 14/4/2 market jobs.
- Validation: unchanged `strong_research_gates:v1` policy.
- Control: a deterministic channel-matched ten-candidate subset of historical Trend Following campaign `52`, replayed against the same frozen dataset.

## Final campaigns

| Campaign | Family | Candidates | Jobs | Promoted jobs | Specialists | Cluster elites | Operational failures |
|---:|---|---:|---:|---:|---:|---:|---:|
| 64 | Breakout | 10 | 20 | 0 | 0 | 0 | 0 |
| 65 | Momentum | 10 | 20 | 0 | 0 | 0 | 0 |
| 66 | Pullback | 10 | 20 | 0 | 0 | 0 | 0 |
| 67 | Mean Reversion | 10 | 20 | 0 | 0 | 0 | 0 |
| 68 | Volatility Expansion | 10 | 20 | 0 | 0 | 0 | 0 |
| 69 | Range Breakout | 10 | 20 | 0 | 0 | 0 | 0 |
| 70 | Continuation | 10 | 20 | 0 | 0 | 0 | 0 |
| 71 | Gap | 10 | 20 | 0 | 0 | 0 | 0 |

All 160 jobs reached the unchanged validation path and were honestly classified as rejected. There were 80 unique candidate IDs, 80 unique executable keys, zero duplicate executable keys, zero failed/blocked/retrying jobs, zero promotions, and zero transfers. Summed job runtime was 99,478 ms.

The individual-family 95% Wilson upper bound after observing 0/10 promoted candidates is 27.7533%; across this heterogeneous bounded experiment it is 4.5818% for 0/80. These bounds describe only this QQQ/SPY 1h frozen experiment and are not evidence that other scopes have the same rate.

## Learning evidence

Each final campaign wrote the existing learning format under `research_learning_v2`:

- four knowledge versions per campaign, including `strategy_family_statistics`;
- 43-82 evidence-backed failure patterns per campaign;
- five recommendations per campaign;
- one campaign plan per campaign;
- zero success-pattern rows, consistent with zero promotions.

For example, campaign `68` stores 20 tested jobs, ten unique Volatility Expansion candidates, median profit factor 0.919953 among the four jobs with a computed profit factor, zero promotions, zero transfers, 100% walk-forward-window availability, 0% regime/paper-readiness survival, and 0% operational failure.

## Protocol correction and operational note

Pilot campaigns `56`-`63` were created with eight candidates each. Integer allocation produced six exploitation and two nearby candidates with no exploration candidate, so those pilots did not satisfy the requested 70/20/10 comparison. They are preserved as contradictory pilot evidence and were not used as the final family comparison. New hypothesis versions record the pilot rejection before final campaigns `64`-`71` were run at ten candidates each.

During the pilot only, the launching PowerShell wrapper timed out while its Python child continued. A briefly started second coordinator could not duplicate claimed work because database claims/leasing remained authoritative. All pilot jobs completed with zero job failures and no duplicate execution keys. Final campaigns were then run sequentially with one coordinator, one worker, and one-job batches. This coordinator incident did not alter a result, but it is disclosed rather than hidden.

## Conclusion

The architecture objective succeeded: all eight families are executable, deterministic, deduplicated, lineage-preserving, validated without special treatment, learned through the existing schema, and archived through the existing campaign system. The bounded research outcome was negative: no family produced a promoted candidate or a confirmed hypothesis on this evidence.

Volatility Expansion had a descriptively higher computed-result median profit factor than the matched Trend Following subset (0.919953 versus 0.696687), but only four of its 20 jobs reached a computed profit factor and none passed all gates. This is a post-hoc signal, not a useful-family conclusion.

Phase 2 stops here. Phase 3 requires explicit approval.
