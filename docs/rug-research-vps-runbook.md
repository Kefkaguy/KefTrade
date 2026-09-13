# Run the repaired RUG research on the VPS

These commands require the current local fixes to have been transferred or merged into the VPS checkout. They do not work with the previously deployed image. No push, deployment, campaign change, or broker order was performed during this audit.

The standalone runner uses a read-only database connection. Its experiment journal is written to the mounted output directory. It cannot promote a candidate or submit a trade. It is a bounded development pilot; it does not certify five profitable strategies.

## Build the patched research image

Building the image does not restart the running API, broker worker, or trading strategies.

```bash
cd /opt/keftrade/deploy/production
docker compose -f docker-compose.prod.yml build api
mkdir -p rug-research-output
```

## Recompute evidence for the existing five

```bash
docker compose -f docker-compose.prod.yml run --rm --no-deps \
  --entrypoint python api \
  -m app.cli.rug_research audit --seed 20260904
```

This reports both the stored state and recomputed candidate/campaign-scoped forward evidence. It does not overwrite the stored evidence. Inspect `audit_error`, excluded fills, and unknown open-position valuation before interpreting P&L. The old five remain legacy execution definitions; this command does not convert their backtests to v2.

## Find the frozen dataset used by the existing run

```bash
docker compose -f docker-compose.prod.yml exec -T postgres \
psql -U keftrade -d keftrade -c "
SELECT id, dataset_id, status, completed_jobs, queued_jobs
FROM research_campaigns
WHERE controls->'rug'->>'seed' = '20260904'
ORDER BY id DESC LIMIT 3;
"
```

Use the returned non-null dataset ID below. `88` is the earlier reported dataset, not an independently verified current ID.

## Run a bounded 15m / five-asset pilot

Replace `88` if the query returns a different dataset. Use a new seed and output filename for a new experiment; the runner refuses to overwrite an existing journal.

```bash
docker compose -f docker-compose.prod.yml run --rm --no-deps \
  -e OPENBLAS_NUM_THREADS=1 -e OMP_NUM_THREADS=1 \
  -v "$PWD/rug-research-output:/research-output" \
  --entrypoint python api \
  -m app.cli.rug_research pilot \
  --dataset-id 88 \
  --assets TSLA NVDA AAPL MSFT AMD \
  --timeframe 15m \
  --seed 20260906 \
  --batch-size 25 --batches 4 \
  --output /research-output/rug-20260906.jsonl
```

This is at most 100 candidate definitions × 5 symbols = 500 market jobs. Each job performs nine simulations: three development folds under frictionless, baseline-cost, and stressed-cost scenarios. The run releases its database connection before simulation. One Python process still shares the VPS's CPU and RAM; the thread environment variables are not hard resource limits.

Without `--cost-calibration-id`, results use the explicitly uncalibrated conservative baseline and doubled stress assumption. Do not lower costs to produce survivors. A calibration must be rebuilt with the patched calibration code, use consistent SIP regular-session observations, and contain at least 100 quote bars and 30 matched fills for each requested symbol. Those counts are coverage checks, not proof of representative execution costs. Historical borrow availability, impact, order size, account type, and time coverage still need review.

If data preflight fails, the pilot stops before generating strategies. Repair the reported dataset issue and create a separately versioned snapshot; do not edit old evidence, interpolate silently, or disable the checks.

## Inspect progress and results

In another terminal:

```bash
cd /opt/keftrade/deploy/production
tail -f rug-research-output/rug-20260906.jsonl |
  jq -c 'select(.event != "market_result")'
```

Completed pilot:

```bash
jq 'select(.event == "pilot_complete")' \
  rug-research-output/rug-20260906.jsonl
```

Failures and cost diagnostics:

```bash
jq -c 'select(.event == "market_result") | {
  symbol,
  candidate_id: .result.candidate_id,
  screen_passed: .result.screen_passed,
  diagnosis: .result.economic_diagnosis,
  failure_reasons: .result.failure_reasons
}' rug-research-output/rug-20260906.jsonl
```

`distinct_screened_candidates` is a development shortlist, not five independent profitable strategies. The journal retains candidate definitions, baseline trades, fold metrics, missing certification gates, and failures. Within this pilot, complete valid candidate cohorts feed the next batch; technical errors never become economic losses. The journal is not automatically imported into the campaign database or resumed by the background RUG scheduler.

The remaining decision requires untouched/prospective evidence, measured cost stress, parameter-neighborhood tests, selection correction, and aligned portfolio/shared-capital validation. A strategy without enough evidence remains unproven; the process must be allowed to return zero.
