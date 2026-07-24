# Phase 12.5 — Research-Process Architecture Proposal

**Status:** design only, no code changes in this document. **Date:** 2026-07-24.

## 0. Framing

Phase 12.4 concluded that all six Phase 12.3 intraday families lack a real directional edge. This document does not
propose a seventh attempt at tuning them. It proposes changing *how the intraday lab produces and evaluates
hypotheses*, so the next families are built on a documented rationale and evaluated with better observability —
not on a bigger parameter grid.

**The central finding this proposal is built on:** most of what's being asked for already exists, fully built, for
swing research (Phase 9–11's "reproducible research architecture," `database/migrations/028_reproducible_research_architecture.sql`,
`apps/api/app/services/research_architecture.py`) — and was simply never connected to the intraday lab, which grew
its own parallel, simpler path (`FAMILY_REGISTRY`, live reads from `candles`/`intraday_features`, no dataset
snapshots, no hypothesis records). Concretely, swing research already has:

| Requirement | Already exists as |
|---|---|
| Immutable dataset version tracking | `research_dataset_manifests` + `research_dataset_candles`, content-hashed, DB-trigger-enforced immutable (`record_dataset_snapshot`, `verify_dataset_snapshot` in `research_architecture.py`) |
| Hypothesis registry with a lifecycle | `research_hypothesis_versions` — `proposed → testing → supported / weak / rejected → retired`, with `observation`, `hypothesis`, `expected_behavior`, `relevant_regimes`, `confidence_score`, `evidence_window`, `supporting_evidence`, `contradictory_evidence` (`append_hypothesis_version`) |
| Specialist / cluster / universal candidate classes | `research_candidate_stage_evidence.candidate_level`: `generated → research_candidate → asset_specialist → cluster_candidate → cluster_elite → universal_elite` |
| Campaign provenance | `research_campaigns.dataset_id / hypothesis_version_id / cluster_id / generator_version / code_commit / experiment_generation` |
| Immutable validation policy, versioned | `research_validation_policy_versions`, `automatic_weakening_forbidden: true` |
| Immutable campaign archives | `research_campaign_archives`, content-hashed manifests |

None of this touches intraday. **Phase 12.5's job is to extend this existing architecture to intraday research**,
plus add the pieces that genuinely don't exist anywhere yet (session-aware pre-entry features, trade-level
train/validation designation, working regime tags at 15m/30m, and a formal specialist-thread process for AMD).
This keeps the proposal smaller, more consistent with the rest of the codebase, and avoids inventing a second,
parallel hypothesis/dataset system that would itself become a future reconciliation problem.

## 1. Principles carried through every section below

- Campaign 47 (aggregate baseline) and Campaign 50 (trade-level diagnostic) are **never** rerun, edited, or
  overwritten. Every new campaign gets its own dataset snapshot, its own `campaign_label`, and its own campaign_id.
- The elite gate (`strong_research_gates:v1`, `passes_cross_validation`, `passes_single_market_validation`) is not
  touched. New candidate classes are additive tiers *below* universal elite, never a bypass of it.
- AMD 30m long Session Momentum is frozen (its exact parameters archived, never re-tuned) and investigated as a
  named specialist research thread — not promoted, not blended into a "fixed" Session Momentum v2.
- Every new strategy family starts from a written hypothesis record, not a parameter sweep. Stop/target/sizing
  changes alone never count as a new family.
- Every schema addition explains, in its own section below, how it avoids look-ahead bias and how it avoids
  quietly relaxing today's validation strength.

## 2. Evidence model changes

### 2.1 Immutable dataset version tracking for intraday

Reuse `research_dataset_manifests` / `research_dataset_candles` as-is — `record_dataset_snapshot()` already reads
from the generic `candles` table keyed by `(symbol, timeframe)`, which already holds 15m/30m rows; nothing about
that function is swing-specific. What's missing is an equivalent frozen snapshot of `intraday_features` (the
session-aware feature table), since `intraday_features` today is read live and can be recomputed/extended between
one campaign and the next — exactly the kind of drift Phase 12.4 flagged as a caveat when comparing Campaign 50
against Campaign 47.

**New table `research_dataset_intraday_features`** (immutable, same trigger pattern as the other tables in
migration 028): `dataset_id, symbol, timeframe, timestamp, session_date, minutes_from_open, minutes_to_close,
session_vwap, distance_from_session_vwap, opening_range_high, opening_range_low, opening_range_position,
gap_percent, session_relative_volume`, primary keyed `(dataset_id, symbol, timeframe, timestamp)`.

**New function `record_intraday_dataset_snapshot()`** (alongside `record_dataset_snapshot`, in a new
`apps/api/app/services/labs/intraday/dataset_snapshot.py` rather than growing `research_architecture.py` further):
snapshots both `candles` and `intraday_features` for a given `(assets, timeframes)` set into one
`research_dataset_manifests` row (reusing the same manifest table — a dataset is a dataset regardless of which
feature table backs it; a `dataset_kind` column distinguishes `'swing'` from `'intraday'` so loaders know which
companion table to join).

`load_intraday_backtest_dataset()` gains a `dataset_id` parameter: when provided, it reads from
`research_dataset_candles` + `research_dataset_intraday_features` instead of live `candles`/`intraday_features` —
mirroring exactly how `load_frozen_campaign_dataset()` already does this for swing. **Anti-look-ahead property:**
because the snapshot is a hash-verified, trigger-enforced-immutable copy taken once at campaign creation, a
strategy backtest against it can never see a candle that didn't exist at snapshot time, and two campaigns run
weeks apart against the *same* `dataset_id` are guaranteed to see byte-identical inputs — closing exactly the gap
Phase 12.4 had to caveat around Campaign 50 vs. Campaign 47.

### 2.2 Trade-level train/validation designation

Today, `run_backtest()` computes a walk-forward split internally (`walk_forward_split`) but only the *execution*
rows (post-split) ever generate trades — the train-side rows are scanned only for context, never traded, so there
is currently no such thing as a "training trade" to label. Phase 12.5 changes this: run the backtest across the
**full** row range and tag each resulting trade with `dataset_split: 'train' | 'validation'` based on whether its
`entry_index` falls before or after the split point (`len(train_rows)`), computed once in `run_backtest()` from
the same `walk_forward_split()` call already in use — no new randomness, no new split logic, just a label attached
to a trade record.

Add `dataset_split TEXT` to `research_campaign_trades` and add train-only vs. validation-only aggregate metrics
blocks to the job's stored `result` (`train_metrics`, `validation_metrics`, each a full `calculate_metrics()`
output) — closing exactly the `training_vs_validation_split_metrics` gap the Phase 12.4 data-availability appendix
flagged as unavailable in `metrics.walk_forward`.

**Anti-look-ahead property:** the split index is fixed *before* the exit-scan loop begins (unchanged from today's
`walk_forward_split`), and a trade's `entry_index` — not its exit index — determines its split label, so a trade
whose exit happens to land after the split boundary is still correctly attributed to the train side it started in.

### 2.3 Pre-entry feature snapshot (not raw candle arrays)

Per your explicit preference, do not persist arrays of candles. Instead, compute and persist exactly seven scalar
features **at the entry bar**, derived only from data available up to and including the entry bar (no look-ahead):

| Field | Definition | Source |
|---|---|---|
| `pre_entry_return_1` / `pre_entry_return_5` | Close-to-close return over the 1 / 5 bars immediately before entry | Already-loaded `recent_candles` window inside `run_backtest`'s loop |
| `pre_entry_atr_relative_move` | `(entry_price - close[N bars ago]) / ATR` over a fixed, documented N (default 10 bars) | Computed from `recent_candles`; ATR itself computed the same way `volatility_20` already is in the swing `features` table, adapted to a configurable window |
| `pre_entry_vwap_distance` | `distance_from_session_vwap` at the entry bar (already computed, already in `intraday_features` — just persisted per-trade instead of only used as a live entry filter) | `intraday_features.distance_from_session_vwap` |
| `pre_entry_trend_slope` | Linear regression slope of close price over the same fixed lookback window, normalized by price | Computed from `recent_candles` |
| `pre_entry_volume_acceleration` | Ratio of the most recent bar's volume to the trailing N-bar average volume | Computed from `recent_candles` |
| `pre_entry_session_progress` | `entry_minutes_from_open / (entry_minutes_from_open + entry_minutes_to_close)` — already-available fields, just combined into one normalized ratio | Already-persisted columns |
| `pre_entry_remaining_session_minutes` | Alias of the already-persisted `entry_minutes_to_close` | Already-persisted column |

Seven new nullable numeric columns on `research_campaign_trades` (`pre_entry_return_1`, `pre_entry_return_5`,
`pre_entry_atr_relative_move`, `pre_entry_vwap_distance`, `pre_entry_trend_slope`, `pre_entry_volume_acceleration`,
`pre_entry_session_progress`) — `pre_entry_remaining_session_minutes` is intentionally *not* duplicated as a new
column since it's identical to the existing `entry_minutes_to_close`.

**Fixed, documented lookback window:** 10 bars for return/ATR/slope/volume-acceleration features, computed from
the same `recent_candles` slice `run_backtest()` already builds for the strategy's own `decide()` call — this
reuses an existing, already-correct no-look-ahead window rather than opening a new one. The exact constant lives
in one place (`PRE_ENTRY_FEATURE_LOOKBACK_BARS = 10` in `backtester.py`) and is asserted by a regression test
(§8) so it can never silently drift.

**Anti-look-ahead property:** every one of these seven values is computed from `recent_candles`, which is already
the identical, already-audited slice passed to the strategy's own `decide()` function for its trading decision —
if it were look-ahead-safe enough to trade on, it is look-ahead-safe enough to log.

### 2.4 Trade-level market/volatility regime that actually works at 15m/30m

Phase 12.4 found every trade's regime tag reads `"unknown"` because regime classification depends on swing
`features` columns (`ema_50`, `returns_5`, `volatility_20`) never computed at intraday granularity, and
`run_intraday_campaign_job()` passes an empty `context_by_time`.

Phase 12.5 does **not** solve this by running the swing feature/regime pipeline at 15m/30m (that was explicitly
deferred in Phase 12.4 as disproportionate, untested infra). Instead, it defines an **intraday-native regime
classification** computed directly from fields the intraday feature layer already has:

- `intraday_trend_regime`: `"trending_up"` / `"trending_down"` / `"range_bound"`, from `pre_entry_trend_slope`
  (added above) against a fixed threshold.
- `intraday_volatility_regime`: `"high_volatility"` / `"normal_volatility"` / `"low_volatility"`, from
  `pre_entry_atr_relative_move`'s magnitude distribution, using the same tercile-style bucketing already
  implemented in Phase 12.4's `cost_and_sizing_analysis` stop-distance buckets.

These are computed once per trade at persistence time (same place `month_key` is computed today in
`persist_intraday_job_trades`) and stored in the *existing* `market_regime`/`volatility_regime` columns —
replacing the literal string `"unknown"` with a real, intraday-native classification, not the swing one. This is
explicitly a **new, different regime taxonomy from swing's** — the design deliberately does not claim these values
mean the same thing as `market_regimes.trend_regime`, and the API response and docs label them
`intraday_trend_regime` / `intraday_volatility_regime` to make that unambiguous. No new infrastructure to validate;
every input already exists and is already computed look-ahead-safely.

### 2.5 Stronger campaign provenance and reconciliation

Adopt the swing side's provenance columns for intraday campaigns exactly as they already exist —
`research_campaigns.dataset_id`, `.hypothesis_version_id`, `.generator_version`, `.code_commit`,
`.experiment_generation` are already nullable columns on `research_campaigns`; `create_intraday_campaign()` simply
needs to populate them (today it populates none of them). Add one new, intraday-specific reconciliation view:

**New function `campaign_lineage(conn, campaign_id)`** returning: the campaign's dataset manifest (with content
hash), its hypothesis version record, its `campaign_label`, its parent campaign if any (a new nullable
`parent_campaign_id` column — e.g. Campaign 50 could formally record Campaign 47 as its baseline reference even
though it's a fresh campaign_id, making the relationship queryable instead of only documented in prose), and a
reconciliation check comparing this campaign's dataset window against its parent's to flag exactly how much new
data was appended between the two (closing the "did the underlying candle history grow" ambiguity Phase 12.4 had
to caveat manually).

## 3. Database migrations (new files, additive only)

| Migration | Contents |
|---|---|
| `047_intraday_dataset_snapshots.sql` | `research_dataset_intraday_features` table + immutability trigger; `research_dataset_manifests.dataset_kind` column (`'swing'` default, `'intraday'` for new rows); `research_campaigns.parent_campaign_id` (nullable, self-referencing FK) |
| `048_intraday_trade_evidence_v2.sql` | `research_campaign_trades` additions: `dataset_split`, `pre_entry_return_1`, `pre_entry_return_5`, `pre_entry_atr_relative_move`, `pre_entry_vwap_distance`, `pre_entry_trend_slope`, `pre_entry_volume_acceleration`, `pre_entry_session_progress` (all nullable numeric/text) |
| `049_specialist_candidate_lifecycle.sql` | `research_specialist_threads` table (see §5) |
| `050_intraday_hypothesis_registry_link.sql` | No new tables — just backfilling `hypothesis_version_id`/`dataset_id`/`generator_version` population going forward via application code; migration only adds an index on `research_campaigns(hypothesis_version_id)` scoped to intraday campaigns for the lineage view |

All four are additive (no `DROP`, no `ALTER ... TYPE`, no constraint tightening on existing rows) and none touch
`research_campaign_jobs`, `research_campaigns`, or `research_campaign_trades` rows belonging to Campaign 47 or
Campaign 50 — existing rows simply have `NULL` in the new columns, which every consumer must treat as
"pre-Phase-12.5 evidence, not missing data," exactly the same convention Phase 12.4 used for `market_regime`.

## 4. API changes

- `POST /research/intraday/datasets` — snapshot candles+intraday_features for a symbol/timeframe set into an
  immutable `dataset_id` (mirrors the existing swing `POST /research/datasets`).
- `GET /research/intraday/datasets/{id}/verify` — recompute and compare hashes (mirrors the swing verify route).
- `POST /research/intraday/hypotheses` — create a `research_hypothesis_versions` row scoped `asset`/`cluster`/
  `universal` for an intraday strategy family, with the seven required hypothesis fields from §6 below enforced
  as required request fields, not optional prose.
- `GET /research/intraday/hypotheses` — list by status, matching the swing hypothesis browser's shape.
- `POST /research/intraday/campaigns` (existing route) gains optional `dataset_id`, `hypothesis_version_id`,
  `parent_campaign_id` query params, all threaded straight into `create_intraday_campaign()` → `_create_intraday_campaign()`
  → the `research_campaigns` INSERT. `campaign_label` (added in Phase 12.4) remains required whenever
  `parent_campaign_id` is set, so a versioned re-run can never silently collide with its parent's `campaign_key`.
- `GET /research/intraday/campaigns/{id}/lineage` — the `campaign_lineage()` reconciliation view from §2.5.
- `GET /research/intraday/specialists` / `POST /research/intraday/specialists/{id}/investigations` — the
  specialist-thread lifecycle from §5.
- `GET /research/intraday/phase-12-4` (existing Phase 12.4 route) — unchanged; still serves Campaign 50 exactly as
  built, since it is frozen evidence, not a live-updating report.

## 5. Specialist candidate lifecycle

Rather than force AMD 30m long Session Momentum into the existing swing candidate-level ladder (which is
scope-typed `asset`/`cluster`/`universal` and campaign-oriented), Phase 12.5 introduces a dedicated,
longer-lived tracking object for exactly the kind of "real but narrow" finding Phase 12.4 produced:

**New table `research_specialist_threads`**: `id, thread_key (UNIQUE), title, origin_campaign_id, origin_candidate_id,
frozen_parameters (JSONB, immutable once set), status ('active_research' | 'confirmed_specialist' | 'invalidated' |
'retired'), scope_symbols (JSONB array), scope_timeframe, scope_direction, created_at`. A companion,
**append-only** `research_specialist_investigations` table (`id, thread_id, investigation_type, dataset_id,
campaign_id, findings JSONB, conclusion TEXT, created_at`) records each individual investigation step — holdout
performance, forward validation, parameter robustness, cost robustness, cross-year stability, similarity-to-declared-AMD-like-securities
— as its own immutable row, so the thread's history reads as an append-only lab notebook, never an overwritten
summary.

**AMD's specific investigation plan** (each becomes one `research_specialist_investigations` row, against a fresh
dataset snapshot per investigation, never against Campaign 47 or 50 directly):

1. **Unseen holdout performance** — snapshot a dataset window strictly *after* Campaign 50's window ends; run the
   frozen AMD candidate against it unchanged. No parameter refitting.
2. **Future forward validation** — register the frozen candidate for the existing paper/forward-validation
   pipeline (`forward_validation_state` machinery already built for swing elites), scoped explicitly as
   `specialist`, not `universal_elite`, so it can never auto-promote through the elite path.
3. **Parameter robustness** — re-run the *same* hypothesis (not a new one) with small, pre-declared perturbations
   to the frozen parameters (e.g. ±10% on the momentum threshold) against the *same* frozen dataset, to see
   whether the edge is a point estimate or a stable neighborhood. This is diagnostic, explicitly not a search for
   a better variant to promote.
4. **Cost robustness** — re-run against the same dataset with fee/slippage multiplied by 1.5× and 2× to see how
   much of the net edge survives a conservative cost assumption.
5. **Stability across years** — snapshot per-year dataset windows and compute the same metrics per year, checking
   whether the edge is a single-year artifact (this is exactly the monthly-dominance check from Phase 12.4,
   extended to a longer, out-of-sample horizon).
6. **Similarity to pre-declared AMD-like securities** — a *fixed, declared-in-advance* comparison list (e.g. other
   high-beta semiconductor names by market-cap/sector, not cherry-picked after seeing which symbols happen to
   work) — this list must be written down before running the comparison, specifically to avoid the multiple-comparisons
   trap of trying symbols until one transfers.

**Promotion boundary, explicit:** a specialist thread reaching `confirmed_specialist` still cannot be promoted to
paper/live trading through the normal elite path — it requires a distinct, explicitly-named "specialist deployment"
decision outside this document's scope, which is a business/risk decision, not a research-evidence decision. This
proposal only covers getting the evidence to that decision point.

## 6. Hypothesis registry for new strategy families

Every new intraday family must have a `research_hypothesis_versions` row (scope `asset`, `cluster`, or `universal`)
created via a new intraday-scoped `append_intraday_hypothesis_version()` wrapper before any candidate generator for
it is written. The registry's existing seven-ish fields map directly onto your seven required elements:

| Your requirement | Existing column |
|---|---|
| What market behavior is expected | `expected_behavior` |
| Why that behavior should exist | `observation` (the causal rationale) |
| What conditions are required | `relevant_regimes` + a new `required_conditions TEXT` field (add via migration 049, since the swing table doesn't currently separate "regimes" from "other preconditions" like minimum relative volume or session timing) |
| What conditions invalidate the hypothesis | new `invalidation_conditions TEXT` field (same migration) |
| How success will be measured | `test_summary` (populated once the campaign completes) plus a new `success_criteria JSONB` field capturing the pre-declared threshold *before* the campaign runs — critical: this must be written down before results exist, not derived from them afterward |
| How generalization will be tested | `scope_type`/`scope_ref` (asset vs. cluster vs. universal) plus the existing cross-validation gate's `assets_passed`/`timeframes_passed` requirements, unchanged |

`success_criteria` (written **before** the campaign launches) is the single most important addition here: it is
the mechanism that prevents "we'll know a good result when we see it" — every new hypothesis's campaign is judged
against numbers decided in advance, not against whatever came out.

A new intraday family's candidate generator function must reference its `hypothesis_version_id` in its module
docstring and its `IntradayFamilyDefinition` registry entry (a new, optional `hypothesis_version_id` field on that
dataclass) — enforced by a regression test (§8) that fails if an `active` family in `FAMILY_REGISTRY` has no linked
hypothesis.

**Explicitly out of scope for a new hypothesis:** changing stop distance, target distance, position sizing, or
entry delay on an existing hypothesis is a parameter variant of that hypothesis, not a new one — it gets a new
`research_hypothesis_versions` *version* (same `hypothesis_key`, incremented `version`), not a new family.

## 7. UI additions (Intraday Research Lab)

- **Hypothesis registry browser**: list of `research_hypothesis_versions` scoped to intraday, filterable by
  status, showing the seven fields from §6 and linked campaigns.
- **New-hypothesis form**: all seven fields required before "create campaign" is enabled for a not-yet-tested
  hypothesis — the UI itself enforces that a campaign can't launch for a hypothesis missing `success_criteria`.
- **Campaign lineage panel**: for any intraday campaign, show its dataset manifest (content hash, window,
  candle/feature counts), its hypothesis version, and its `parent_campaign_id` chain if any — a direct UI surface
  for the `campaign_lineage()` reconciliation view.
- **AMD specialist thread page**: a dedicated page (not just a card in the general lab) showing
  `research_specialist_threads` + its append-only `research_specialist_investigations` log, explicitly labeled
  "not promoted, not active" at the top, matching the existing Phase 12.4 panel's framing but with its own
  permanent URL so it isn't buried inside a rotating family list.
- **Phase 12.4 panel**: unchanged — it already correctly reads Campaign 50 and will continue to.

## 8. Regression testing strategy

- **Dataset immutability**: a test asserting `research_dataset_manifests`/`research_dataset_intraday_features`
  raise on `UPDATE`/`DELETE` (mirrors existing swing tests for `prevent_immutable_research_record_mutation`).
- **Pre-entry feature no-look-ahead**: a test constructing a hand-crafted candle series where a *future* bar (past
  the entry point) has an extreme value, asserting none of the seven pre-entry features change when that future
  bar's value is altered — a direct, mechanical proof the lookback window never reaches forward.
- **Train/validation split integrity**: a test asserting every trade's `dataset_split` label matches
  `entry_index < split_index`, and that a job's `train_metrics`/`validation_metrics` trade counts sum to the job's
  total trade count exactly.
- **Hypothesis-before-campaign enforcement**: a test asserting `create_intraday_campaign()` raises if
  `hypothesis_version_id` is omitted for a family not already in `FAMILY_REGISTRY`'s frozen/archived set (ORB and
  VWAP Reversion are grandfathered — they predate this requirement and are archived, not re-hypothesized
  retroactively).
- **Campaign-key collision guard**: a regression test pinning the exact bug Phase 12.4 hit (relaunching a campaign
  with identical inputs and no `campaign_label` returns the existing campaign via `ON CONFLICT`) so this specific
  failure mode can never silently reappear.
- **Specialist thread immutability**: `frozen_parameters` on `research_specialist_threads` must be rejected on
  `UPDATE` once set (same trigger pattern) — a test asserting an attempted parameter edit raises.
- **Elite gate unchanged**: the existing Phase 9.12 pinned cross-validation regression test remains; add one more
  pinning `strong_research_gates:v1`'s exact threshold values so any future edit to those numbers fails CI loudly
  rather than silently.

## 9. Sequencing (within Phase 12.5, still design-then-build, one PR per row)

1. Migrations 047–050 (schema only, no behavior change — every new column nullable, every new table unused until
   wired in).
2. `backtester.py` additions: `dataset_split` labeling + the seven pre-entry features, gated behind the existing
   trade-dict pattern from Phase 12.4 (purely additive, same review pattern as before).
3. Dataset snapshot support for intraday (`record_intraday_dataset_snapshot`, `load_intraday_backtest_dataset(dataset_id=...)`).
4. Hypothesis registry wrapper + `campaign_lineage()` + API routes.
5. Specialist-thread tables + AMD's six investigations, run one at a time, each its own campaign/dataset snapshot.
6. UI: hypothesis browser, lineage panel, AMD specialist page.
7. Only after all of the above is live and tested: propose the *first* new, hypothesis-documented intraday family
   as its own follow-up phase — not part of this one.

Each numbered step ships and is reviewed independently; nothing here is a single large PR.
