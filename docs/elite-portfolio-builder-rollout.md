# Elite Portfolio Builder validation and rollout

This runbook covers Migration 038 and solver version
`elite_portfolio_constructor_v1`. It is deliberately additive. Do not delete or
rewrite elites, deployments, research evidence, broker history, or incomplete
portfolio runs.

## Frozen dependency evidence

No dependency manifest changed during this implementation. Record and compare
these repository objects before production rollout:

```text
apps/web/package-lock.json  git-object 3d5151f4a638b1675788ae4fceaf70c82a7c2047
apps/api/requirements.lock.txt git-object a046119803011f98b6b335ba05f4cc110bc08b21
apps/api/requirements.txt   git-object 0f5f58a6c866b8ea7a99e349e8c22baa69de50d0
apps/api/pyproject.toml     git-object a1c47e3363202678aaf3d09b8dca8fa2c8f3679b
```

Before building on the VPS, record the current image IDs and installed Python
distributions. Do not run dependency upgrade commands during this rollout.

```bash
cd /opt/keftrade/deploy/production
docker compose -f docker-compose.prod.yml images
docker compose -f docker-compose.prod.yml exec -T api python -m pip freeze
sha256sum ../../apps/web/package-lock.json ../../apps/api/requirements.lock.txt ../../apps/api/requirements.txt ../../apps/api/pyproject.toml
```

## Pre-deployment backup

Create a recoverable PostgreSQL backup before applying Migration 038. This
command creates a new backup and does not mutate the database.

```bash
cd /opt/keftrade/deploy/production
mkdir -p backups
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U keftrade -d keftrade -Fc > "backups/keftrade-before-038-$(date -u +%Y%m%dT%H%M%SZ).dump"
ls -lh backups/keftrade-before-038-*.dump
```

## Deployment sequence

Keep internal activation disabled for the first deployment. Broker submission,
external paper execution, and live money remain unchanged.

```text
ELITE_PORTFOLIO_BUILDER_ENABLED=true
ELITE_PORTFOLIO_ACTIVATION_ENABLED=false
BROKER_ORDER_SUBMISSION_ENABLED=false
EXTERNAL_PAPER_EXECUTION_ENABLED=false
```

```bash
cd /opt/keftrade
git pull --ff-only origin main
cd deploy/production
docker compose -f docker-compose.prod.yml build api worker broker-worker
docker compose -f docker-compose.prod.yml up -d postgres
docker compose -f docker-compose.prod.yml run --rm migrate
docker compose -f docker-compose.prod.yml up -d --no-deps --force-recreate api worker broker-worker
docker compose -f docker-compose.prod.yml ps
```

The migration runner reapplies ordered migrations. Migration 038 uses additive
`IF NOT EXISTS` objects and named constraint replacement. Run it once against a
restored production-shaped backup in staging before production.

## Migration 038 verification

```bash
docker compose -f docker-compose.prod.yml exec -T postgres \
  psql -U keftrade -d keftrade <<'SQL'
\pset pager off

SELECT
  to_regclass('public.elite_portfolio_runs') AS runs,
  to_regclass('public.elite_portfolio_snapshots') AS snapshots,
  to_regclass('public.elite_portfolio_members') AS members,
  to_regclass('public.elite_portfolio_activation_attempts') AS attempts;

SELECT tgname
FROM pg_trigger
WHERE tgname = 'external_paper_deployments_long_only_guard'
  AND NOT tgisinternal;

SELECT conname, pg_get_constraintdef(oid)
FROM pg_constraint
WHERE conrelid IN (
  'elite_portfolio_runs'::regclass,
  'elite_portfolio_members'::regclass,
  'proposed_broker_orders'::regclass
)
ORDER BY conrelid::regclass::text, conname;

SELECT
  COUNT(*) AS elites,
  COUNT(*) FILTER (WHERE strategy_direction = 'long') AS historical_long,
  COUNT(*) FILTER (WHERE strategy_direction = 'short') AS short_elites
FROM elite_research_candidates;

SELECT COUNT(*) AS external_non_long
FROM external_paper_deployments x
JOIN strategy_deployments d ON d.id = x.internal_deployment_id
WHERE d.strategy_direction <> 'long' OR d.execution_capability = 'internal_only';
SQL
```

Expected: all four tables and the trigger exist, all historical rows remain,
and `external_non_long` is zero.

## API and determinism checks

```bash
curl -fsS https://keftrade.duckdns.org/research/elite-portfolios/options | jq '{solver_version,candidate_count,timeframes,directions}'

curl -fsS -X POST https://keftrade.duckdns.org/research/elite-portfolios/preview \
  -H 'Content-Type: application/json' \
  -d '{"universe":[],"families":[],"directions":["long","short"],"timeframes":["1h","4h","1d"],"thresholds":{},"constraints":{},"objective":"balanced","custom_size":null}' \
  > /tmp/elite-preview-1.json

curl -fsS -X POST https://keftrade.duckdns.org/research/elite-portfolios/preview \
  -H 'Content-Type: application/json' \
  -d '{"universe":[],"families":[],"directions":["long","short"],"timeframes":["1h","4h","1d"],"thresholds":{},"constraints":{},"objective":"balanced","custom_size":null}' \
  > /tmp/elite-preview-2.json

jq -S '{selected,snapshot:.snapshot.decision_hash,constraint_relaxation_count}' /tmp/elite-preview-1.json
jq -S '{selected,snapshot:.snapshot.decision_hash,constraint_relaxation_count}' /tmp/elite-preview-2.json
cmp <(jq -S '{selected,snapshot:.snapshot.decision_hash}' /tmp/elite-preview-1.json) \
    <(jq -S '{selected,snapshot:.snapshot.decision_hash}' /tmp/elite-preview-2.json)
```

Both runs must have identical selected keys and decision hashes. The relaxation
count must be zero, including when status is `infeasible`.

## Query and response performance

Record cold and cached measurements. Do not bypass quality gates to improve a
timing result.

```bash
printf '%s' '{"universe":[],"families":[],"directions":["long","short"],"timeframes":["1h","4h","1d"],"thresholds":{},"constraints":{},"objective":"balanced","custom_size":null}' > /tmp/elite-config.json

curl -sS -o /tmp/elite-cold.json -w 'cold status=%{http_code} total=%{time_total}s bytes=%{size_download}\n' \
  -X POST https://keftrade.duckdns.org/research/elite-portfolios/preview \
  -H 'Content-Type: application/json' -d @/tmp/elite-config.json

curl -sS -o /tmp/elite-cached.json -w 'cached status=%{http_code} total=%{time_total}s bytes=%{size_download}\n' \
  -X POST https://keftrade.duckdns.org/research/elite-portfolios/preview \
  -H 'Content-Type: application/json' -d @/tmp/elite-config.json

jq '{timing,response_size_bytes,cache,candidates_examined,construction_pool_count}' /tmp/elite-cached.json
```

Acceptance: cold preview under 2 seconds for 500 candidates; cached preview
under 250 ms. After persisting a run, measure detail under 500 ms:

```bash
curl -sS -o /tmp/elite-detail.json -w 'detail status=%{http_code} total=%{time_total}s bytes=%{size_download}\n' \
  https://keftrade.duckdns.org/research/elite-portfolios/PORTFOLIO_ID
```

Use `EXPLAIN (ANALYZE, BUFFERS)` in staging on the candidate loader and detail
queries if any target fails. Do not add Redis around orders, broker status,
approvals, execution, or reconciliation. Only preview results are cacheable.

## Internal activation

After regression, determinism, prohibition, and performance checks pass, set:

```text
ELITE_PORTFOLIO_ACTIVATION_ENABLED=true
```

Recreate the API only. Internal activation requires an approved snapshot and an
idempotency key. It reuses existing internal deployments, records member-level
progress, retries failed/blocked members, creates disabled external candidates
only for capable long members, and emits snapshot-bound CLI instructions.
It never invokes broker authorization or enables order submission.

## Frontend rollout

Deploy the already-locked frontend after the API validation. `/elite-builder`
is a statically rendered shell; it loads options and evidence in the browser so
no live portfolio payload is embedded into ISR output.

## Rollback

1. Set both `ELITE_PORTFOLIO_BUILDER_ENABLED=false` and
   `ELITE_PORTFOLIO_ACTIVATION_ENABLED=false`.
2. Mark incomplete runs `cancelled` or `failed`; do not delete them.
3. Redeploy the previously recorded API, worker, broker-worker, and frontend
   images.
4. Leave additive Migration 038 objects in place. Previous services ignore
   them.
5. Reconcile internal deployments created during the failed rollout before
   re-enabling workers.
6. Restore the database backup only for confirmed migration corruption and only
   through a separately reviewed recovery operation.

Never use rollback to clean up evidence or make an infeasible portfolio appear
feasible.
