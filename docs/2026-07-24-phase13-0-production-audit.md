# Phase 13.0 — Production audit (2026-07-24)

Read-only audit of production (`production-postgres-1` on the VPS) and the
Phase 12.5 architecture, performed before any Phase 13 implementation.
Nothing was modified, archived, or deleted during this audit.

## The "unexplained `research_specialist_threads.id = 1`" record

**Finding: the record does not exist and never persisted.** Evidence:

- `SELECT * FROM research_specialist_threads WHERE id = 1` → 0 rows.
- The table contains exactly one row, `id = 2`:
  `amd_30m_long_session_momentum` — "AMD 30m long Session Momentum
  (Campaign 47/50 specialist …)", origin campaign 50, candidate
  `sessmom_9d2fff5ecd0aa7`, status `active_research`, `simulation_only = true`,
  created 2026-07-24 20:13 UTC, 0 investigations attached.
- `research_specialist_threads_id_seq.last_value = 2`.
- The table has a BEFORE DELETE trigger
  (`research_specialist_threads_no_delete_trigger`) that blocks deletes, so a
  committed id=1 row could not have been removed afterward.

The only consistent explanation: an INSERT consumed sequence value 1 inside a
transaction that rolled back (PostgreSQL sequences are non-transactional and
do not return values on rollback). This is normal engine behavior, not data
loss and not an orphan. **Decision: nothing to archive — there is no record.**
The one real thread (id=2) is legitimate, current, and left untouched.

## Where evidence actually lives

- `backtests` / `backtest_trades` are **empty in production** (0 rows each);
  they are legacy swing-era tables. All research evidence lives in
  `research_campaign_jobs.result` (jsonb) + `research_campaign_trades`.
- Volumes at audit time: 30 campaigns, 52,658 jobs
  (51,749 rejected / 601 promoted / 270 failed / 30 blocked_terminal /
  8 queued), 250 hypothesis versions.
- Dataset snapshots: `research_dataset_manifests`,
  `research_dataset_candles`, `research_dataset_intraday_features`.
- 156 public tables total; no migration-tracking table (migrations are the
  ordered SQL files in `database/migrations/`, currently through 050).

## Existing structures Phase 13 must respect

- `research_campaign_jobs.generation_channel` already constrains to
  `exploitation | nearby | exploration` — an evidence-guided generation
  concept already exists; Phase 13.6 must **version on top of it**, not
  replace it.
- `research_specialist_threads` enforces `simulation_only = true` by CHECK
  constraint and blocks frozen-parameter mutation and deletes by trigger —
  the append-only pattern Phase 13.1's DNA schema should mirror.
- Family registry: `apps/api/app/services/labs/intraday/families/registry.py`
  with 8 families (6 active, 2 archived: ORB v1, VWAP Reversion v1) — Phase
  13.3's new families must register here, and 13.1's DNA must cover all 8
  existing ones without altering their historical meaning.
