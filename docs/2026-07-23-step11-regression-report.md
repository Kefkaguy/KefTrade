# Step 11 — Full Regression Report (post cleanup, 2026-07-23)

Scope: verify the Phase 13 cleanup (steps 7–10: dead-code removal, UI cleanup,
DB optimization) introduced no regressions. Per instruction, unrelated defects
found along the way are recorded separately (see "Discovered defects — NOT
cleanup-caused") and were **not** fixed in this pass.

Environments: Backend/API + DB on the production VPS (`keftrade.duckdns.org`);
frontend on production Vercel (`keftrade.vercel.app`), which auto-deployed from
the pushed commits.

---

## 1. Frontend — production build

| Check | Result | Evidence |
|---|---|---|
| Clean build, fresh cache (`rm -rf .next && next build`) | **PASS** | Exit 0; "Compiled successfully in 7.6s" |
| Type-check + lint | **PASS** | Ran as part of `next build`; no errors |
| All active routes compile | **PASS** | 22 routes in the build manifest (`/`, `/assets`, `/assets/[symbol]`, `/candidates/[id]`, `/copilot`, `/dashboard`, `/diagnostics`, `/elite-builder`, `/experiments`, `/experiments/[id]`, `/journal`, `/market-intelligence`, `/mission-control`, `/paper`, `/paper/deployments`, `/reports`, `/research`, `/research-intelligence`, `/settings`, `/validation`, `/validation/[id]`, `/_not-found`) |
| Broken imports / missing chunks | **PASS** | None — build would fail on either |
| Hydration / browser console errors | **PASS** | `read_console_messages(onlyErrors=true)` returned "No console logs" on every page tested (see §2) |
| No nav link to removed `/strategies` | **PASS** | Full interactive nav dump on Home: 9 links, none to `/strategies` |
| Removed route actually gone | **PASS** | `GET /strategies` on production → **404** ("This page could not be found") |

**Command:** `rm -rf .next && npx next build` → exit 0.

---

## 2. Main UI smoke test (production, `keftrade.vercel.app`)

Each page navigated directly on the live Vercel deployment; console errors checked after each load.

| Page | Loads | Primary data | Console errors | Notes |
|---|---|---|---|---|
| Home | **PASS** | Campaign activity (0/0/0, Active/History(24)/All), **Strategy Library** (105 active / 187 archived, family rows, 2 new campaign launchers) | none | New panel confirmed live |
| Research | **PASS** | Loads (part of Home flow / `/research`) | none | — |
| Candidates (`/research-intelligence`) | **PASS** | "Waiting" ranking-pipeline empty state | none | Empty state is intentional — no candidate currently queued for ranking |
| Forward validation (`/paper`) | **PASS** | Workspace shell + nav renders | none | — |
| Reports | **PASS** | Workspace shell renders | none | — |
| Mission Control | **PASS** | Workspace shell renders | none | — |
| Diagnostics | **PASS** | Workspace shell renders | none | — |
| Elite Builder | **PASS** | Full builder UI (scope/constraints/review/activate steps, solver version) | none | — |
| Strategy Library panel + legacy toggle | **PASS** | Panel renders on Home with correct counts; legacy families hidden by default (toggle present in component, verified in source + live counts) | none | Browser pane lost focus mid-click-test; verified via rendered counts and component logic instead of a captured click, noted below |
| Campaign History + Repair controls | **PASS** | "History (24)" tab present and correctly separated from "Active" (0 running); Repair action verified via source + API test, not clicked live (no blocked campaign currently exists to trigger it) | none | See defect note below re: Repair button visibility condition |
| Removed components/routes referenced anywhere | **PASS** | No stale references found in nav, build manifest, or rendered pages |
| Contradictory status labels | **PASS** | Home shows 0 Running/0 Queued/0 Paused and "No active campaigns. Completed work lives under History." — consistent, not contradictory |

**Note on interaction depth:** the legacy toggle click and Repair button click were not executed against a live click event (a preview-pane hiccup ended the session); their correctness was instead confirmed via (a) the exact API responses the component renders from, both returned 200 with correct counts, and (b) direct source review of `StrategyLibraryPanel.tsx` and `CampaignActivity.tsx`. This is a lighter verification than a captured UI click — flagged so it isn't overstated as a full interaction test.

---

## 3. Backend / API

| Check | Result | Evidence |
|---|---|---|
| Full backend test suite | **PASS*** | `364 passed`, 1 error — `test_production_validation.py::test_missing_worker_supervision_config_returns_failed_check` fails with `PermissionError` on a Windows temp-dir (`pytest-of-erosi`), a pre-existing local-environment issue unrelated to the cleanup (confirmed present before this session began) |
| Home / health | PASS | `/health` → 200 |
| Research command center | PASS | `/research/command-center` → 200 |
| Campaign list | PASS | `/research/campaigns` → 200 |
| Campaign detail | PASS | `/research/campaigns/33` → 200 |
| Campaign progress | PASS | `/research/campaigns/33/progress` → 200 |
| Strategy Library (active/legacy) | PASS | `/research/families/registry?status=active` and `?status=legacy` → 200 each |
| Candidates / research intelligence | PASS | `/research/intelligence` → 200 |
| Mission Control | PASS | `/paper/mission-control` → 200 |
| Forward validation deployments | PASS | `/paper/deployments` → 200 |
| Paper deployment management | PASS | `/paper/deployment-management` → 200 |
| Paper scheduler | PASS | `/paper/scheduler` → 200 |
| Broker status / execution-readiness | PASS | `/broker/status`, `/broker/execution-readiness` → 200 |
| Elite Builder options | PASS | `/research/elite-portfolios/options` → 200 |
| Diagnostics (strategy-diagnostics, summary, elite-deployments audit, portfolio readiness) | PASS | all 4 → 200 (router has no `/diagnostics` prefix — first pass used a wrong path and mis-reported 404; corrected and re-verified, see defects) |
| Reports (daily-reports) | PASS | `/paper/daily-reports` → 200 |
| Research archive | PASS | `/research/archive` → 200 |
| Removed feature flags expected at runtime | **PASS** | `grep` of `diagnostic_logging` / `broker_raw_snapshot_retention_days` across `app/` (excluding `settings.py`) → zero references |

\* "PASS*" — all 364 collectible tests pass; the 1 error is a collection-time environment error (Windows temp dir permissions), not a test failure, and is unrelated to the cleanup (see defects list).

---

## 4. Database

| Check | Result | Evidence |
|---|---|---|
| 5 dropped indexes absent | **PASS** | `SELECT count(*) FROM pg_indexes WHERE indexname IN (...)` → `0` |
| Dependent partition index retained | **PASS** | `broker_raw_ingest_events_2026_07_trace_id_received_at_idx` present (Postgres refused the drop — correct behavior, documented in the audit) |
| Snapshot table has exactly the intended retained rows | **PASS** | `count(*) FROM research_command_center_snapshots` → `3` |
| Latest command-center snapshot readable/refreshable | **PASS** | Latest row's `payload->'source'->>'served_from'` populated; live `GET /research/command-center` (which reads this table) → 200 with rendered content on Home |
| PostgreSQL logs clean after cleanup | **PASS** | `docker logs production-postgres-1` reviewed alongside migrate/api/worker logs — only expected `NOTICE` lines from idempotent migration re-application (e.g. "column already exists, skipping"); no `ERROR` |
| DB size + key table counts (data-loss check) | **PASS** | Size: 1786 MB → **1619 MB** (post-optimization, ~167 MB reclaimed — consistent with the 168 MB reported after the optimization pass; small further delta from the whole-DB `VACUUM ANALYZE`). Table counts identical to the pre-cleanup backup snapshot: `candles=532,089`, `features=528,967`, `research_campaign_jobs=50,826`, `research_campaigns=24`, `elite_research_candidates=7`, `research_family_registry=292` — **zero row loss** |

---

## 5. Operational checks

| Check | Result | Evidence |
|---|---|---|
| Containers healthy | **MOSTLY PASS** | `production-api-1` healthy · `production-worker-1` healthy · `production-postgres-1` healthy · `production-nginx-1` healthy · `production-broker-worker-1` **unhealthy** (see defect below — not cleanup-caused) |
| Recent API/worker/broker-worker logs | **PASS** | No `ERROR`/`Exception`/`Traceback` lines in the last 30 min across api, worker, and broker-worker (excluding expected null-field log noise) |
| Scheduled workers / campaign processing functioning | **PASS** | 5 campaign workers with heartbeat `< 90s` at check time (all slots alive); broker-worker log shows completed cycles (`broker cycle complete … reconciliation=clean … broker_mutation=false`) every ~poll interval, i.e. it is functioning despite the stale healthcheck |
| No migration/startup errors post-deploy | **PASS** | `production-migrate-1` log reviewed: only idempotent `NOTICE` lines (columns/indexes "already exists, skipping"); no `ERROR` |

---

## Discovered defects

### Cleanup-caused regressions
**None found.** No test, endpoint, page, or data-integrity check regressed as a result of steps 7–10.

### NOT cleanup-caused (recorded only, not fixed — per instruction)
1. **`production-broker-worker-1` reports `unhealthy`.** Root cause: its Docker
   healthcheck script asserts `BROKER_ORDER_SUBMISSION_ENABLED=false` and
   `EXTERNAL_PAPER_EXECUTION_ENABLED=false`. Both were intentionally set to
   `true` in the Phase 11 work (arming the Alpaca paper pipeline), which
   predates this cleanup pass. The worker itself is functioning correctly
   (clean broker cycles logged, `reconciliation=clean`) — this is a stale
   healthcheck definition in `deploy/production/docker-compose.prod.yml`, not
   a process failure. Pre-existing before Step 11 began.
2. **Windows-local pytest collection error**
   (`test_missing_worker_supervision_config_returns_failed_check`) —
   `PermissionError` on `C:\Users\erosi\AppData\Local\Temp\pytest-of-erosi`.
   Environmental (Windows temp-dir ACL), not a code defect; pre-existing and
   previously noted in earlier sessions.
3. **My own smoke-test URL error** (not a product defect): I initially probed
   `/diagnostics/strategy-diagnostics` and got 404, momentarily flagged as a
   regression. The `diagnostics` router has no path prefix — correct paths are
   `/strategy-diagnostics`, `/strategy-diagnostics/summary`,
   `/elite-deployments/audit`, `/portfolio/readiness`. All 4 re-verified → 200.
   Recorded here only for audit-trail completeness; not a real issue.

---

## Overall verdict

**Step 11: PASS.** The Phase 13 cleanup (steps 7–10) introduced zero
regressions across frontend build/lint, the 22-route build manifest, the live
production UI (9 pages + the new Strategy Library panel and cleaned Campaign
Activity), the full 364-test backend suite, 19 smoke-tested API endpoints, and
database integrity (indexes, snapshot retention, row counts, log cleanliness).
One pre-existing operational item (broker-worker healthcheck) and one
pre-existing local test-environment issue are recorded for awareness, not
fixed, per instruction.

**Awaiting approval before Step 12 (Intraday Research Lab).**

---

## Addendum — broker-worker healthcheck correction (2026-07-23, post-approval)

Approved operational correction, applied after Step 11 was approved: the
`production-broker-worker-1` healthcheck asserted
`BROKER_ORDER_SUBMISSION_ENABLED`/`EXTERNAL_PAPER_EXECUTION_ENABLED` were both
`false` (a Phase 10 assumption invalidated by the deliberate Phase 11 flag
flip). Replaced with `app.workers.broker_worker_healthcheck`, which checks
process/DB liveness, fresh `broker_sync_runs` cycles, and non-persistent
failures — and never reads either execution flag (pinned by a test).

**Change scope:** healthcheck only. No execution behavior, feature flag
value, or broker capability was changed. Only `broker-worker` was rebuilt and
recreated; `api`, `worker`, `postgres`, `nginx` were untouched (confirmed by
unchanged `CreatedAt` timestamps).

### Focused regression (post-deploy)

| Check | Result |
|---|---|
| Docker health status | **healthy** (`docker compose ps` → `production-broker-worker-1 ... (healthy)`) |
| Healthcheck script run manually inside the container | `HEALTHY: latest cycle status=complete age=7s`, exit 0 |
| `/broker/status` | 200; `execution_enabled: false`, flags unchanged (`broker_order_submission_enabled: true`, `external_paper_execution_enabled: true`) |
| `/broker/reconciliation` | 200 |
| `/broker/execution-readiness` | 200 |
| `/health` (api) | 200 |
| `broker_sync_runs` — last 3 cycles | all `status=complete`, ~60s apart (poll interval), most recent 7s old at check time |
| broker-worker logs (last 3 min) | no errors/exceptions/tracebacks |
| Flags on disk unchanged | `BROKER_ORDER_SUBMISSION_ENABLED=true`, `EXTERNAL_PAPER_EXECUTION_ENABLED=true` — identical to before the fix |
| Other containers unaffected | `api`, `worker`, `postgres`, `nginx` all show pre-existing `CreatedAt`; only `broker-worker` recreated |

**Verdict: PASS.** Broker cycles continue completing normally; reconciliation
stays clean; no capability or flag was touched. Committed separately (see git
log) from the Step 11 regression pass.

**Awaiting approval before Step 12 (Intraday Research Lab) — its architecture
and implementation plan still require explicit sign-off before any work
begins.**
