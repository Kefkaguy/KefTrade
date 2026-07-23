# Phase 13 cleanup audit — inventory only (2026-07-23)

Rollback point: git tag `pre-cleanup-2026-07-23` (`74c7b2b`), and a verified
PostgreSQL backup at `/home/ubuntu/keftrade-backups/keftrade_20260723T132546.dump`
(357 MB, restore-tested into a throwaway DB — all row counts matched production
exactly).

**Nothing below has been removed.** This is an inventory of candidates. Each is
tagged with a recommended disposition. Historical research evidence, elite
history, campaign history, and the migrations directory are explicitly
out of scope for removal.

---

## Backend

### Confirmed dead — safe to archive
- `services/` (repo root, **not** `apps/api/app/services`): 0 files — empty
  scaffolding from an abandoned microservices layout.
- `.agents/` (repo root): 0 files — empty.

### Verify, then likely remove
- Dead feature flags (declared in `settings.py`, read nowhere):
  - `broker_raw_snapshot_retention_days` — no retention job consumes it.
  - `diagnostic_logging` — superseded by structured logging in `observability.py`.
- `cors_origins` and `elite_minimum_trades_per_year` looked unused by a naive
  grep but are **live** (read via `cors_origin_list()` and `getattr` respectively).
  Keep both.

### Clean — no action
- Routers: all 17 router files are registered in `main.py`; no orphans.
- Services: all ~45 service modules are referenced at least once; no dead services.
- No superseded/duplicate campaign generators found (generator versions are
  distinct: `family_balanced…`, `hidden_gem_recovery_v1`, `high_frequency_v1`,
  `portfolio_evidence_broad_v1`).

---

## Frontend

### Confirmed dead — safe to archive
Five components are never imported anywhere:
- `AlphaDiscoveryRunner`
- `CandidateComparison`
- `HypothesisWorkflow`
- `ResearchConstellation`
- `StrategyDiscoveryActions`

### Verify, then decide
- **Orphan page `/strategies`**: a real page (uses `ResearchUI` + `live-research`)
  but linked from nav 0 times — reachable only by direct URL, and overlaps with
  `/research-intelligence` (Candidates). Candidate to archive or re-link.
- **`/dashboard`**: only a `redirect("/")` stub. Harmless; keep as a bookmark
  alias or remove — low stakes either way.
- ~35 exported functions in `lib/api.ts` are never called from a component.
  Many correspond to features not yet wired to UI (paper order/fill/position
  getters, research archive/journal/timeline, deployment controls). These are
  API surface, not necessarily dead — verify per item against roadmap before
  removing. `reevaluateElites` is newly added and not yet wired to a button.

### Clean — no action
- No debug UI found (`console.log` / `TODO` / `debugger` absent from `app` and
  `components`).

---

## Database (151 tables)

### Empty tables — 78 total, but mostly NOT dead
Critical distinction: **empty ≠ dead.** The large majority are schema for
features that simply haven't been exercised yet, and must be kept:
- Broker/paper execution tables (`broker_orders`, `paper_orders`,
  `broker_fills`, `proposed_broker_orders`, `execution_halts`, …): empty because
  no order has been placed yet. Keep.
- `broker_raw_ingest_events_2026_07 … 2028_07` + `_default`: **forward monthly
  partitions**, empty by design until broker ingest runs. Keep all.
- `elite_portfolio_*` (runs/members/conflicts/correlations/eligibility/snapshots):
  empty because no portfolio has been built (pool is infeasible). Keep.
- Production-validation and research-lab tables (`production_*`,
  `research_hypotheses`, `strategy_experiments`, `signals`): empty; keep unless a
  feature is being formally retired.

Genuinely reviewable empties (feature never used and not on the roadmap) should
be decided case by case — none are urgent, and dropping any requires checking FK
references first.

### Unused indexes — safe optimization (0 scans, non-PK/unique, >100 KB)
| Index | Table | Size |
|---|---|---|
| `research_failure_patterns_key_idx` | research_failure_patterns | 2.0 MB |
| `broker_raw_ingest_events_2026_07_trace_id_received_at_idx` | (partition) | 1.7 MB |
| `research_campaign_workers_status_idx` | research_campaign_workers | 1.2 MB |
| `broker_sync_runs_account_created_idx` | broker_sync_runs | 464 KB |
| `research_candidate_objects_state_idx` | research_candidate_objects | 120 KB |
| `execution_logs_account_created_idx` | execution_logs | 104 KB |

These are the clearest safe wins in Phase 4 (drop → reclaim ~5.6 MB, faster
writes). Low risk; re-creatable.

### Large tables worth an archival/pruning policy (not deletion)
| Table | Size | Note |
|---|---|---|
| research_campaign_jobs | 352 MB | Core evidence — keep; consider archiving jobs of superseded campaigns to a cold table. |
| **research_command_center_snapshots** | **249 MB** | Accumulating snapshot/cache — strongest pruning candidate (keep latest N per campaign). |
| research_dataset_candles | 216 MB | Frozen dataset candles — keep. |
| features / candles | 170 / 163 MB | Keep. |
| market_regimes | 138 MB | Keep. |
| research_timeline_events | 115 MB | Append-only log — candidate for time-based archival. |

### Clean — no action
- 0 views, 0 native enum types (constraints are CHECK-based). Nothing to prune.

---

## Off-limits (do not touch during cleanup)
- **`database/migrations/`** — the migrate job re-applies every file on every
  deploy, so migrations are live, order-dependent, mutually-consistent scripts,
  not a prunable history. Two bugs this session came from exactly this property.
- Historical research evidence, elite history, campaign history — archive, never
  erase.

---

## Suggested safe first actions (Phase 3/4, on your command)
1. Archive the 2 empty repo dirs and 5 unused components into a `legacy/` area.
2. Drop the 6 unused indexes.
3. Add a retention policy for `research_command_center_snapshots` (keep latest N).
4. Remove the 2 confirmed-dead feature flags.
Everything else stays until individually confirmed.
