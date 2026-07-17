# Phase 2 findings

Unless stated otherwise, every result below is from immutable dataset ID `1`, hash `0fcc46465c65d213af34b46a9a744e967e448b2cab39c104072acf2112881750`, QQQ/SPY 1h, and the unchanged `strong_research_gates:v1` policy.

## Evidence-backed findings

1. **Every family is executable and reaches normal validation.** Campaigns `64`-`71` completed 20 jobs each (160 total), with 80 unique candidates, 80 unique executable keys, 0 promoted jobs, and 0 operational failures. Each family has one stored rejected hypothesis result (`74`, `77`, `80`, `83`, `86`, `89`, `92`, `95`).

2. **No elite candidate was found in the bounded comparison.** Every family produced 0/10 promoted candidates, 0 specialists, 0 cluster elites, 0 universal elites, and 0 transfers. The aggregate 0/80 promoted-candidate observation has a 95% Wilson upper bound of 4.5818%, but it applies only to this deliberately small heterogeneous experiment.

3. **Momentum produced one QQQ near-pass, but the same executable candidate contradicted on SPY.** In campaign `65`, candidate `sd_1e736f1a982786`, QQQ 1h, job `129836`, had 32 trades, PF 1.070933, expectancy 2.673104, and drawdown 0.037300. It failed PF >=1.2 and paper/regime readiness because sideways performance was weak. On SPY 1h, the same candidate had 16 trades, PF 0.317150, expectancy -30.850526, and drawdown 0.049565. Sample: two market jobs for this candidate within a 20-job campaign.

4. **Volatility Expansion produced one QQQ near-pass with no SPY opportunity sample.** In campaign `68`, candidate `sd_eee52d8683be3f`, QQQ 1h, job `129890`, had 31 trades, PF 1.070155, expectancy 4.796708, and drawdown 0.059688. It failed PF >=1.2 and paper/regime readiness; bull and sideways buckets were weak. The same candidate produced zero trades on SPY 1h. Sample: two market jobs for this candidate within a 20-job campaign.

5. **The frozen Trend Following control remained reproducible and retained the strongest individual near-pass.** The channel-matched campaign `52` subset replay matched all 20/20 stored results. Candidate `sd_1b053e06d3b498`, QQQ 1h, job `128630`, had 25 trades, PF 1.247652, expectancy 14.084421, and drawdown 0.062349; it failed the 30-trade and paper/regime requirements. On SPY it had 18 trades, PF 0.595428, and expectancy -30.206512. The control median PF was 0.696687 across ten computed results.

6. **Frequency was a real but not universal blocker.** Pullback campaign `66` frequency-screened 18/20 jobs, Mean Reversion `67` screened 19/20, Volatility Expansion `68` screened 16/20, Continuation `70` screened 14/20, and Gap `71` screened 16/20. In contrast, Range Breakout campaign `69` had 11/20 jobs with at least 30 trades, yet 0/20 positive-expectancy jobs and 0/20 PF passes. Its median PF was 0.448929 across 15 computed results and median expectancy was -33.831927. This is direct evidence that Range Breakout's rejection was economic as well as frequency-related.

7. **Volatility Expansion's median lead is sparse and contradictory.** Campaign `68` median PF 0.919953 exceeded the matched control's 0.696687 by 0.223266, but it was based on 4/20 computed results versus 10/20 for the control; 16/20 were frequency-screened, 0/20 passed PF, and 0/20 promoted. The result is descriptive only.

8. **Learning integration reflects the negative evidence without inventing successes.** Campaigns `64`-`71` each wrote four `research_learning_v2` knowledge records, five recommendations, and one campaign plan. They wrote 43-82 failure patterns and zero success patterns, consistent with the campaign outcomes. Existing `research_learning_v1` evidence was not overwritten.

## Inconclusive findings

- Whether any new family is useful on another cluster, asset universe, timeframe, seed, or independent future dataset: **Inconclusive — insufficient evidence.**
- Whether Volatility Expansion truly improves median quality: **Inconclusive — insufficient evidence.** The observed median used four computed jobs, was selected after comparing eight families, had zero promotions, and has no independent confirmation.
- Whether Momentum or Volatility Expansion can transfer beyond QQQ: **Inconclusive — insufficient evidence.** The only positive-expectancy QQQ jobs were contradicted by SPY.
- Whether observed bull/sideways failures are causal regime effects: **Inconclusive — insufficient evidence.** The campaign records associations, not randomized interventions.
- Whether Phase 2 reduces near-duplicate generation compared with historical Trend Following: **Inconclusive — insufficient evidence.** Exact duplicates were zero in both, and no compatible historical near-distance metric exists.
- Whether the high frequency-rejection rate should be addressed by wider entry ranges rather than by rejecting these families: **Inconclusive — insufficient evidence.** Changing ranges would be a new hypothesis test, not a reinterpretation of the completed result.

## Post-hoc hypotheses requiring future independent validation

Every item in this section is **Post-hoc and unconfirmed.** None may become supported from dataset ID `1`, because that evidence created the hypothesis.

1. **Momentum condition hypothesis.** On an independent frozen QQQ/SPY-like cluster, Momentum candidates with measured acceleration and explicit exclusion of the observed sideways condition will improve PF and paper/regime readiness without reducing the sample below 30 trades. Supporting evidence: campaign `65`, candidate `sd_1e736f1a982786`, QQQ 1h, 32 trades, PF 1.070933, expectancy 2.673104. Contradictory evidence: SPY 1h, 16 trades, PF 0.317150, expectancy -30.850526; campaign median PF 0.666936 and 0/20 promotions.

2. **Volatility Expansion frequency/condition hypothesis.** On an independent frozen dataset, less sparse variants inside the existing controlled range that retain directional range expansion while avoiding the observed bull/sideways instability will produce positive expectancy on at least two cluster assets and pass PF 1.2. Supporting evidence: campaign `68`, candidate `sd_eee52d8683be3f`, QQQ 1h, 31 trades, PF 1.070155, expectancy 4.796708. Contradictory evidence: SPY 1h had zero trades; 16/20 campaign jobs were frequency-screened and 0/20 promoted.

3. **Pullback frequency hypothesis.** On an independent frozen dataset, controlled pullback-depth/reclaim combinations with more setup opportunities will reach 30 trades without losing economic quality. Supporting evidence: campaign `66` had two jobs reach the trade gate, proving the signal path can execute. Contradictory evidence: 18/20 jobs were frequency-screened, 0/20 had positive expectancy or PF 1.2, and candidate `sd_61321fae7d553c` lost money on both QQQ (43 trades, PF 0.756004, expectancy -17.266251) and SPY (45 trades, PF 0.453391, expectancy -41.941263).

4. **Range Breakout economic hypothesis.** On an independent frozen dataset, range-width/duration variants must improve economics rather than merely frequency; a valid result requires PF >=1.2 and positive expectancy while preserving the existing drawdown/stability gates. Supporting evidence for testability: campaign `69` had 11/20 jobs reach 30 trades. Contradictory evidence: 0/20 PF passes, 0/20 positive expectancy, median PF 0.448929, and 12/20 drawdown failures.

No bounded confirmation campaign was run because no separate independent frozen dataset was authorized for Phase 2. Reusing dataset ID `1` would be same-evidence confirmation and is explicitly blocked by the implementation.
