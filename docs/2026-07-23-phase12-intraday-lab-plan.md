# Phase 12 — Intraday Research Lab: Architecture & Plan (DRAFT, awaiting approval)

Status: **proposal only — no implementation started.** Every code change described
below is future tense. This exists so you can approve or redirect before any
work begins.

## Why this phase exists (the evidence that led here)

Three campaigns this session (37, 38, 40) tested whether the existing rule
library could be made to trade frequently:
- 4h trade-starved families re-sampled: best PF 10–13, but on 7–8 trades — noise, not edge.
- 15m raw: avg 5.2 trades/job, everything profitable was thin (PF 3.24 on 7 trades).
- 15m with correctly volatility-scaled thresholds (`timeframe_scaled_parameters`,
  `sqrt(bar duration)`): avg trades **doubled to 10.6** — but **0 of 690 jobs**
  were both frequent (≥30 trades) and profitable (PF ≥1.2).

Conclusion carried into this phase: the existing rule library (EMA/RSI/MACD
trend-following, `pullback`/`trend_continuation` entries) is structurally a
swing-trading library. Rescaling its parameters cannot make it a day-trading
one. **Phase 12 is not a bigger version of Campaign 40** — it introduces
genuinely different strategy logic, data, and validation rules for
intraday behavior, evaluated with the same "never weaken the gate" discipline
already in place (honest median gate, family registry, frequency measurement).

No order-book/tick data is available (Alpaca gives only OHLCV bars). This plan
is scoped to what's honestly buildable from bars: session structure,
VWAP-relative behavior, opening-range dynamics, and intraday volatility
regimes — not true microstructure/order-flow signals. I want that limitation
stated up front rather than implied later.

---

## 12.1 — Architecture

### New service, not a variant of the existing one
`apps/api/app/services/intraday_research.py` (new module) rather than adding
more branches to `research_campaigns.py` (already ~8,000 lines and carrying
the campaign lifecycle, elite gate, family registry, and repair logic). The
intraday lab **reuses** existing infrastructure rather than forking it:

| Reused as-is | New for Phase 12 |
|---|---|
| `research_campaigns` table/lifecycle, `repair_campaign`, worker claim/lease model | `intraday_features` table (12.2) |
| `elite_research_candidates`, the honest median gate (`passes_cross_validation`, `median_trades_per_year`) | New entry/exit rule blocks (12.3) |
| `research_family_registry` (active/legacy classification) — intraday families get audited by the *same* registry, not a parallel one | Session-aware validation rules (12.4) |
| `SUPPORTED_CAMPAIGN_TIMEFRAMES` (15m/30m already added) | A session-boundary/trading-calendar helper |
| `timeframe_scaled_parameters` (already proven to work mechanically) | An intraday-specific backtester cost model (tighter fee/slippage assumptions matter far more at high frequency) |

**Data flow (unchanged shape, new content):**
```
Alpaca 15m/30m bars (already syncing)
  -> intraday_features (new, session-relative computations)
  -> intraday rule blocks (new entry/exit/session logic)
  -> existing campaign job/worker/backtest pipeline (unchanged)
  -> existing honest elite gate + frequency measurement (unchanged)
  -> existing family registry audit (unchanged)
```

**Why not a separate "lab" microservice or database:** everything downstream
(elite promotion, portfolio builder, Alpaca paper deployment) already consumes
`elite_research_candidates` uniformly. Splitting intraday into a parallel
system would either duplicate that promotion/deployment machinery or require
a merge step later. Reuse is both less code and avoids a second gate to keep
honest.

**Decision I'd like your sign-off on:** keep intraday campaigns in the same
`research_campaigns` table (tagged via `generator_version = 'intraday_lab_v1'`,
same pattern as `hidden_gem_recovery_v1`/`high_frequency_v1`), rather than a
new table. This keeps Repair, History, and the Strategy Library panel working
on intraday campaigns for free. I recommend this; flag if you'd rather isolate
it structurally.

---

## 12.2 — Feature design

### The real gap: no session-relative features exist today
The current `features` table has 11 columns, all timeframe-agnostic technical
indicators (EMA/RSI/MACD/volatility/volume-change). None of them know what
time of day it is, where price sits relative to VWAP, or what the opening
range was. That absence is *why* `vwap_reclaim` (an existing trend block) is
a crude proxy today rather than a real VWAP check — there's no VWAP column to
check against.

### New table: `intraday_features` (additive, not a migration of `features`)
Kept separate from `features` rather than adding 10+ nullable columns to an
existing 170 MB table used by every non-intraday backtest. One row per
`(symbol, timeframe, timestamp)` for 15m/30m only.

| Feature | Why it matters for intraday |
|---|---|
| `session_vwap` | Anchor for VWAP-reversion and VWAP-reclaim entries (replaces the current proxy) |
| `distance_from_vwap` | Normalized entry signal (mean-reversion) |
| `session_elapsed_minutes` | Distinguishes open/mid/close behavior — intraday edges are rarely time-of-day invariant |
| `opening_range_high` / `opening_range_low` (first N bars of session, N configurable, default first 30 min) | Opening-range breakout/failure entries |
| `prior_close`, `gap_pct` | Gap-fill / gap-continuation entries |
| `session_cumulative_volume`, `relative_session_volume` | Volume-confirmed breakouts (raw `volume_change` from `features` is bar-over-bar, not session-relative) |
| `intraday_range_pct` (high-low as % of open) | Regime filter: only trade sessions with enough range to clear costs |
| `bars_since_session_open` | Cheap integer alternative to `session_elapsed_minutes` for block logic |

All computed from OHLCV alone — no new data source required. Session
boundaries come from the existing equity-market-hours logic already used by
`market_closed_for_asset`/`data_freshness` (`research_campaigns.py`), extended
into a small trading-calendar helper rather than hardcoded per feature.

**Decision needed:** opening-range window length (I'd default to first 30
minutes / 2 bars at 15m, configurable per campaign, not hardcoded) — this
materially changes which symbols/sessions qualify and I'd rather you set the
default than have me guess.

---

## 12.3 — Strategy families (new rule blocks)

New `entry`/`exit` blocks added to `RULE_LIBRARY` (same `RuleBlock` dataclass,
same generation/dedupe machinery — no new candidate representation). All are
buildable from the 12.2 features and are actual intraday structures, not
retuned swing logic:

| New entry block | Structure | Distinct from existing? |
|---|---|---|
| `vwap_reversion` | Enter against price extended beyond `distance_from_vwap` threshold, expecting reversion toward session VWAP | Yes — existing `mean_reversion` reverts to EMA, not a session anchor |
| `opening_range_breakout` | Break of `opening_range_high`/`low` with volume confirmation | Distinct from existing `opening_range_proxy`, which has no real opening-range feature to reference (proxy today) |
| `gap_fill` | Fade a `gap_pct` open toward `prior_close` | New — nothing in the current library references gaps directly |
| `session_momentum_continuation` | Directional continuation confirmed by `relative_session_volume`, gated to a `session_elapsed_minutes` window | Distinct from `trend_continuation` (EMA-based, timeframe-agnostic) |
| `range_contraction_expansion` | Enter on transition from a low `intraday_range_pct` regime to expansion | New — no existing block reasons about *session* range regime |

New exit blocks, because holding-period logic matters more at 15m than at 4h:
| New exit block | Structure |
|---|---|
| `session_close_exit` | Force flat by end of session (no overnight carry — matches "day-trading" framing and avoids overnight-gap risk your current elites don't have to consider) |
| `vwap_reclaim_exit` | Exit when price crosses back through session VWAP against the position |

**These are additions to `RULE_LIBRARY`, not replacements.** Existing blocks
(`pullback`, `trend_continuation`, etc.) stay exactly as they are — intraday
campaigns simply have a larger pool to draw from, filtered to session-aware
blocks only when generating for 15m/30m.

**Decision needed:** should intraday exits force flat by session close
(no overnight positions), or allow carrying overnight? I'd recommend **flat
by close** as the default — it's the more conservative, easier-to-validate
choice and matches "day trading" as commonly understood — but this changes
risk character enough that it's your call, not mine to assume.

---

## 12.4 — Validation rules

Reuses the existing gate structure entirely; adds session-aware conditions
on top. **No existing threshold changes.**

### Carried over unchanged
- Aggregate gate: PF ≥1.2, expectancy >0, drawdown ≤0.12, trade_count ≥60,
  stability ≥0.6, assets_passed ≥2, timeframes_passed ≥1.
- Honest median gate: median variant PF ≥1.2, positive median expectancy,
  median drawdown ≤0.12, median_variant_trade_count ≥20.
- Family registry audit and legacy archiving — intraday families get
  classified by the exact same rules as everything else, so a family that
  looks frequent but is actually noise (like Campaign 38/40's results) gets
  the same "Too noisy" / "Retire" treatment, not a special pass.

### New, additive checks specific to intraday validity
1. **Cost-realism gate.** At 15m holding periods, fee+slippage assumptions
   dominate the PF calculation far more than at 4h. Require the backtest's
   average trade P/L exceed a configurable multiple of round-trip cost
   (e.g., net edge ≥ 2× modeled cost) — otherwise a "PF 1.3" strategy could be
   an artifact of an optimistic cost model. This is a new check, not a
   loosening of an old one.
2. **Session-count floor**, distinct from the existing raw trade-count floor:
   require trades spread across a minimum number of *distinct trading
   sessions* (e.g., ≥20 sessions), not just ≥60 trades — prevents a strategy
   that took 60 trades across 3 volatile days from passing as if it had 60
   independent samples.
3. **Time-of-day concentration check** (diagnostic, not necessarily a hard
   gate): report whether a candidate's trades cluster in a narrow
   `session_elapsed_minutes` window. Informational for now — flags overfitting
   to a specific historical open/close pattern; whether it becomes a hard
   reject is worth deciding after seeing real distributions, not before.
4. **Frequency floor becomes meaningful here.** The existing
   `ELITE_MINIMUM_TRADES_PER_YEAR` (built this session, currently 0/off) is
   the natural place to require intraday candidates prove they're actually
   frequent — I'd suggest enabling it *only* for intraday-generated
   candidates initially (e.g., a per-campaign override), not globally, so it
   doesn't retroactively affect your 7 existing swing elites.

**Decision needed:** the cost-realism multiple (2× suggested above) and the
session-count floor (20 suggested) are both judgment calls with no "correct"
answer from data yet — I'd rather set them with you than pick numbers that
look precise but aren't.

---

## What Phase 12 will NOT do
- Will not touch the 7 existing elites, their promotion state, or their
  Alpaca paper deployments.
- Will not weaken any existing gate, migration, or constraint.
- Will not claim microstructure/order-flow insight it can't actually compute
  from OHLCV bars — feature names and family descriptions will say "session/
  VWAP-relative," not "order flow."
- Will not assume the lab succeeds. Campaigns 37/38/40 all came back negative;
  Phase 12 could too. The plan below treats a null result as valid evidence,
  the same posture as the rest of this session.

## Proposed build order (once approved)
1. `intraday_features` table + computation job (12.2) — foundation everything else needs.
2. Session-boundary/trading-calendar helper (shared by 12.2 and 12.4).
3. New rule blocks (12.3), added to `RULE_LIBRARY`, unit-tested for generation/dedupe.
4. New validation checks (12.4), wired into the existing gate as additive conditions.
5. One small pilot campaign (a handful of symbols, one new block at a time) before a full-scale run — so a bad feature or block is caught cheaply, not after a 690-job campaign like 38/40.

---

## Decisions — CONFIRMED (2026-07-23)
1. **Campaign structure:** intraday campaigns live in the existing
   `research_campaigns` table, tagged `generator_version = 'intraday_lab_v1'`.
   Repair/History/Strategy Library work on them unchanged.
2. **Overnight carry:** forced flat by session close. `session_close_exit` is
   a required exit block for every intraday-generated candidate; no intraday
   candidate may carry a position past the session close.
3. **Frequency floor scope:** `ELITE_MINIMUM_TRADES_PER_YEAR` applies
   **intraday-only** at first, via a per-campaign override — not global. The
   7 existing swing elites are unaffected by this floor for now.
4. **Opening-range window:** first 30 minutes of each session (2 bars at 15m).

## Remaining open parameters (defaults proposed, will confirm before the pilot campaign)
- Cost-realism multiple (proposed: net edge ≥ 2× modeled round-trip cost).
- Session-count floor (proposed: ≥20 distinct sessions).
- Time-of-day concentration check: diagnostic-only for the pilot, decide on
  hard-gate status after seeing real distributions.

These three are lower-stakes numeric knobs (not architecture/scope decisions)
and will be set explicitly in the implementation PR for a final look before
the pilot campaign runs — not silently assumed.
