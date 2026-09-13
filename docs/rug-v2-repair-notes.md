# RUG intraday repair: implementation and remaining evidence

## Follow-up audit — 5 September 2026

The follow-up adds candidate/account/symbol/deployment-safe forward attribution, short FIFO and fee allocation, complete internal trade evidence, forward chronology checks, isolated refresh errors, and truthful unavailable valuation for open positions. New RUG definitions also use full-history causal EMA, entry-inclusive holding limits, forced-exit close timestamps and architecture-specific diagnostics. New cost calibrations retain per-symbol fill support and estimates; RUG refuses pooled-only coverage. V2 avoids an unnecessary full global-learning rebuild and retrieves compact comparable candidate aggregates.

See [audit report](rug-audit-2026-09-05/report.html) and [VPS runner instructions](rug-research-vps-runbook.md). The new read-only pilot journals experiments and feeds valid failures into later batches within that pilot. It does not certify survivors, import its journal into existing campaigns, or enable automatic deployment. These changes are still local; the seven-gate objective remains incomplete.

## Deployment status

Local implementation only. No VPS deployment, broker action, live-strategy change, campaign pause, or relabeling of historical evidence was performed. No database migration is needed for these changes. Existing RUG campaigns and continuations remain `rug_v1`; new API requests default to `rug_v2_intraday`, 25 candidates, and no automatic continuation.

This repairs the research instrument, not a promise that intraday alpha exists. The complete seven-condition survivor-certification workflow is **not completed** by this patch. New development results are retained and inspectable but cannot enter automatic elite/paper promotion.

## Implemented

- Session-relative entries, exchange-calendar DST/early-close handling, and structural no-overnight execution. A final-bar signal cannot enter the next session.
- Real simple-average true-range ATR stops, directional EMA filters, actual session-reset opening ranges, and same-slot prior-session relative volume. Optional filters and explicit long/short simulations replace misleading proxy diversity. Exits are honestly one ATR-stop/R-target/time/session-close mechanism, not an unimplemented trailing-stop label.
- Unique semantic candidate IDs, deterministic random control selection before adaptive ranking, and avoidance of previously completed candidates in the comparable cohort. Duplicate launch requests are serialized and return the existing batch without regenerating it.
- Immutable dataset required; duplicate timestamps, mixed/unknown sources, malformed OHLCV, missing/misaligned bars and missing exchange sessions fail closed. No interpolation or silent feed preference. This is not a complete corporate-action or historical-feed audit.
- Three disjoint session-aligned development evaluation folds with expanding historical context. The final 20% of sessions is excluded from feature calculation/search. Its historical exposure is explicitly unverified, not called untouched OOS.
- Frictionless diagnostic, baseline-cost, and cost-stress backtests. Optional persisted execution-cost calibration by ID must meet quote/fill evidence and symbol-coverage checks. Without it, the old conservative 30bp approximate round-trip assumption is labeled uncalibrated; no fabricated cheap costs.
- V2-only notional cap and gap-through-stop pricing; frozen legacy long backtest remains unchanged. Simulations are per market, not a shared-capital portfolio or a borrow-availability model.
- Per-fold trade/quality/drawdown screens and a predeclared 50-trades/year development opportunity floor. That is **not** a daily portfolio-opportunity certificate. Frequency uses evaluated dates, not training dates.
- Technical/data failures excluded from economic rejection learning. Legacy raw rejection counts replaced with attempt-normalized, smoothed bucket rates and a minimum support count; a new learning version avoids consuming cached old guidance.
- V2 learning isolated by dataset, timeframe list, asset set, calibration ID, and execution version. Only complete valid candidate cohorts contribute; it uses family-level smoothed screen-pass rates, not causal learning. Adaptation is capped at half the batch, with a deterministic random control. Effectiveness remains unproven.
- Fold-tagged trade evidence stored with completed v2 job results. Missing final certification does not turn development jobs into rejected economics. Historical and development counters explicitly distinguish screening from forward validation/certification.

## Still required before calling anything a robust survivor

1. Verify historical feed/corporate-action fidelity and supply a complete eligible dataset. Dataset 88 may fail the new checks; that is a data-repair requirement, not a trading loss.
2. Supply measured, representative account/order-size execution costs. A stored calibration's counts/coverage are checked, but representativeness still needs review.
3. Connect a protected, audited final-test workflow. Reserving an old tail does not erase exposure from prior campaigns. The patch deliberately provides no “trust me, untouched” flag.
4. Wire selection/multiple-testing correction, parameter-neighborhood tests and aligned portfolio-correlation/shared-capital validation into final certification. These are explicitly missing, not treated as passed by a generic walk-forward flag.
5. Run matched random-entry/ablation experiments and a prospective adaptive-versus-random comparison. The random control here is random **candidate selection**, not a matched random-entry significance test.
6. Verify prospective paper execution. There is no ten-certified-survivor autostop/automatic deployment implementation in this patch.

## Inspect the existing five candidates now (works before deploying this patch)

Run in Bash on the VPS. These commands are read-only and do not submit trades.

```bash
BASE=https://keftrade.duckdns.org

for id in $(curl -fsS "$BASE/research/rug/status?seed=20260904" |
  jq -r '.campaigns[] | select(.promoted_candidates > 0) | .id'); do
  curl -fsS "$BASE/research/campaigns/$id" |
  jq --argjson campaign_id "$id" '
    .forward_validation_candidates[]? | {
      campaign_id: $campaign_id,
      candidate_id, profit_factor, expectancy, trade_count,
      max_drawdown, assets_passed,
      forward_validation_state, validation_state, paper_performance
    }'
done
```

For the detailed forward evidence, replace the example variable value with an actual returned candidate ID:

```bash
CANDIDATE_ID='paste_candidate_id_here'
curl -fsS "$BASE/research/elite-candidates/$CANDIDATE_ID/forward-validation" |
  jq '{candidate: (.elite_candidate | {
       candidate_id, profit_factor, expectancy, trade_count, max_drawdown,
       forward_validation_state, paper_performance
     }), paper_rollups: .paper_rollups, evidence_drift: .evidence_drift}'
```

`awaiting_paper_deployment`, zero paper trades, or positive historical PF alone do not establish profitability. The five legacy candidates retain their original execution assumptions; corrected execution requires a separately versioned test, not rewriting them.

## Read-only commands available after deployment

```bash
curl -fsS 'https://keftrade.duckdns.org/research/rug/candidates?seed=20260904' | jq

curl -fsS 'https://keftrade.duckdns.org/research/rug/datasets/88/preflight?assets=TSLA,NVDA,AAPL,MSFT,AMD&timeframe=15m' | jq
```

Inspect available calibration records, without changing them:

```bash
cd /opt/keftrade/deploy/production
docker compose -f docker-compose.prod.yml exec -T postgres \
  psql -U keftrade -d keftrade -c "
SELECT id, feed, symbols, quote_observations, matched_fill_observations,
       observed_round_trip_bps, stressed_round_trip_bps, window_end
FROM intraday_execution_cost_calibrations
ORDER BY id DESC LIMIT 5;"
```

Do not launch a million v2 candidates yet. Each v2 job performs nine backtests, making a candidate count incomparable to v1 compute. Check a bounded pilot, dataset validity and measured throughput first. A v2 launch must explicitly supply a frozen dataset ID and intraday timeframes; optional `cost_calibration_id` pins existing measured evidence into candidate parameters.

## Verification

Focused regression suite passes, including legacy replay, actual ATR, time/session rules, data integrity, no overnight, learning normalization/control independence, frequency, gap fills, cash caps, idempotent launch and development promotion denial.

Full-suite checkpoint: 2,636 passed, 36 skipped, one failure in the unchanged `intraday_sector_leadlag_predictor.py` runtime-DDL audit. Further targeted tests were added after that checkpoint. No successful VPS/database integration run is claimed.
