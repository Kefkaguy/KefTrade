# Phase 1 report validation

## Overall assessment: Share with required caveats

The Phase 1 diagnosis is ready for the requested human review. Its population,
denominators, gate definitions, line-level evidence, and headline calculations
reconcile to the PostgreSQL research ledger. It is intentionally not decision-ready
as confirmed causal knowledge: the analysis is post-hoc, strategies share data and
lineage, and independent future-dataset validation has not happened.

## Methodology review

- Question answered: why every preserved asset specialist fails to become a
  cross-asset elite, without changing validation thresholds.
- Population: every promoted `asset_specialist` row in
  `research_candidate_stage_evidence` as of dataset 1's frozen end time,
  2026-07-16 19:30 UTC.
- Grain: 22 specialist IDs, 98 linked candidate–asset jobs, consisting of 22 home
  validations and 76 non-home transfer attempts.
- Deduplication: exact executable keys reduce 22 IDs to 16 strategies and 76 attempts
  to 52. The six repeated executions are retained in the audit appendix but removed
  from execution-unique summaries.
- Gate definition: the report uses the six immutable strong-gate diagnostics, not
  the worker's weaker `status` field.
- Causal stance: conclusions are diagnostic associations; every proposed next test
  is labeled post-hoc and unconfirmed.

## Calculation spot-checks

- Specialist-stage count: verified independently in SQL at 22; no duplicate
  campaign/candidate stage keys.
- Join coverage: verified independently at 98/98 linked jobs with non-empty results;
  no dropped or multiplied specialist records.
- Transfer denominator: independently recomputed as 76 ID-level and 52
  execution-unique attempts.
- Failure decomposition: independently recomputed as 69 economic plus seven
  sample-only failures at ID grain, and 48 plus four at execution-unique grain.
  These mutually exclusive groups reconcile exactly to their denominators.
- Strong passes: independently recomputed as 22/22 home jobs and 0/76 target jobs.
- Regime pools: GOOGL bull-trend PF 1.57038465 over 294 home observations versus
  0.77147804 over 845 targets; AMD 1.53340491 over 210 versus 0.75818611 over 305.
  Direct database recomputation matches the evidence artifact.
- Report completeness: all 22 candidate IDs and all 76 target job IDs appear in the
  Markdown report; Appendix B contains exactly 76 data rows.
- Dataset integrity: passed, with all manifest candle counts and hashes matching and
  no reported issue.
- Duplicate reproducibility: six exact strategy revalidations have no metric
  inconsistency across any tested asset.
- Generated JSON parses successfully and contains 22 candidate diagnostics, 76
  target rows, and 220 matched mutation comparisons.

## Presentation review

No chart, browser render, screenshot, or image was used. This is an intentional
exception to chart-first reporting because the user prohibited browser/image review
and candidate-by-target auditability is better served by exact tables. Markdown table
structure, headings, source job references, and row counts were checked from source.

## Required caveats

- Candidate IDs, trades, and target attempts are dependent because they share frozen
  candles, strategy lineage, and sometimes exact execution parameters.
- Wilson and bootstrap intervals are descriptive; they are not independent-market
  confidence intervals.
- The analysis looks at outcomes already observed on dataset 1. It cannot confirm a
  new hypothesis until a preregistered future frozen dataset is used.
- High-volatility AMD transfer has only two target trade observations and remains
  inconclusive.
- Earnings-event behavior is unavailable because no versioned corporate-event source
  exists.

## Verification receipt

- Reproduction command: `.venv\Scripts\python.exe reports\phase_1_transfer_failure\diagnose_transfer_failure.py`
- API tests: 211 passed.
- Web type check: passed with `npx tsc --noEmit`.
- Python compilation: passed for `diagnose_transfer_failure.py`.
- `git diff --check`: passed.
- Evidence SHA-256: `FA6F9DF0E6EBF768C15D6CDCBDCE783B2BF361B569FE5093B6682CB3D393F36C`
- Report SHA-256: `96ED6EB827FD5670F7B1A6EBFD0182AAC11A343BC0944FCDFE23438FA056C013`
- Script SHA-256: `1E42487DA76E1BEE6759AE2C67D8ED1C3449695C56BB3992B77C21EC11203C89`

## Blocking status

There is no blocker to reviewing Phase 1. Independent confirmation is deliberately
outstanding and blocks treating the hypotheses as confirmed or beginning the next
phase without explicit user approval.
