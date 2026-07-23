# Session changes — 2026-07-23

A full pass over campaign reliability, the elite promotion gate, the strategy
library, trade-frequency research, the Alpaca Paper path, and the UI.

**Guiding rule honoured throughout: nothing was deleted.** Every "removal" is an
archive that preserves the underlying evidence — legacy families, terminalized
jobs, demoted elites, and superseded campaigns all keep their rows and results.

---

## Problems found

1. **Elite Builder returned "infeasible" from a heuristic**, which could be a
   false negative — the greedy constructor can miss a valid portfolio.
2. **Campaign 33 stuck at 99% forever** — `blocked_data` jobs were counted as
   open indefinitely with no path to a terminal state.
3. **Campaign 34 marked `completed` with 480 jobs still `queued`** — a race
   between scout expansion and finalization.
4. **Worker controls showed "0/4 alive" while workers were healthy** — the UI
   read stale frontend state, not backend heartbeats.
5. **The elite gate promoted money-losing families** — it used *pooled* profit
   factor, so a few lucky symbol-variants could carry a candidate whose typical
   variant loses money. 4 of 7 elite families had a median backtest under 1.0.
6. **~70% of research compute was wasted** — ~30 families each ran 1,100–1,300
   jobs re-testing 1–6 candidates across ~200 symbols; 36 families never traded
   at all.
7. **Nothing selected for trade frequency** — every elite trades ~7–14x/year, so
   no deployment acts more than about monthly.
8. **Phase 10 onboarding lock** — once paper execution was enabled, *no* new
   deployment could be onboarded (a leftover `assert_execution_disabled`).
9. **Only illiquid ETFs were being observed** (AAXJ, AAAU); the liquid AMD and
   AAPL deployments were `disabled`.
10. **`.env` had the Alpaca secret under the wrong key** (`ALPACA_SECRET_KEY`
    instead of `ALPACA_API_SECRET`), so it would not have loaded.
11. **Migration 023 re-broke deploys** — it re-added the job-status CHECK without
    `blocked_terminal` on every migrate run.
12. **The `failure_classification` CHECK rejected real readiness reasons**
    (`missing_dataset`, etc.), which silently killed 270 jobs and hid the cause.
13. **`repair_campaign` wrote a disallowed `recovery_classification`** — any
    repair of a genuinely stale lease would have rolled back.
14. **15m candles were synced but not 15m features**, blocking a whole campaign.
15. **`research_campaign_key` collisions** returned an already-completed campaign
    instead of creating a new variant.
16. **Low timeframes made entries rarer, not more frequent** — entry thresholds
    are absolute price moves calibrated for 4h bars, so they are ~4x too
    demanding on 15m.

### Retracted (investigated, not a bug)

- **"Stale 4h data blocks execution."** It does not. Live eligibility uses a 96h
  tolerance and all 12 checks pass on every deployment; a 4h bar from the prior
  session close is expected, not stale.

---

## Fixed and added

### Campaign reliability (Phase A)
- Migration 040: terminal `blocked_terminal` job status.
- `campaign_repair_plan` / `repair_campaign`: deterministic recovery — release
  stale leases, terminalize retry-exhausted blocks, reopen wrongly-completed
  campaigns, finalize only when every job is terminal. CLI + API + 12 tests.
- `finalize_research_campaign` re-checks the open-job count under a row lock,
  closing the finalize/expand race; clears `execution_status` and stamps
  `finalized_at`.
- `campaign_progress_breakdown`: authoritative 7-bucket progress with live
  worker count and `repair_required` invariants.
- Terminal accounting now includes `blocked_terminal` everywhere.

### Honest elite gate
- Migration 041: `promotion_state` (elite | demoted), median metric columns.
- Gate now requires the **median variant** to be profitable (median PF ≥ 1.2,
  positive expectancy, drawdown ≤ 0.12, ≥ 20 median trades) on top of every
  original aggregate check. Only adds requirements; never weakens one.
- `reevaluate_elite_candidates`: rebuilds elite status from immutable evidence,
  demoting (never deleting) candidates that no longer qualify.
- **Result:** all 7 current elites re-verified as genuine (median PF 1.34–1.66).

### Strategy library
- Migration 042: `research_family_registry` with evidence-based classification
  and an active/legacy lifecycle.
- 292 families classified → **105 active, 187 archived as legacy**. Legacy
  families are excluded from candidate generation (ending the compute waste)
  but keep all evidence.
- `create_hidden_gem_recovery_campaign` and `create_high_frequency_campaign`.

### Trade frequency
- `median_trades_per_year` + a `trade_frequency_class` (daily / weekly / monthly
  / rare) measured on every candidate.
- Opt-in promotion floor `ELITE_MINIMUM_TRADES_PER_YEAR` (default 0, so nothing
  is demoted retroactively).

### Low-timeframe research
- `SUPPORTED_CAMPAIGN_TIMEFRAMES` adds 15m/30m (defaults unchanged); explicit
  low timeframes are no longer silently stripped.
- 15m candles + features backfilled for the research core (49,990 each).
- `timeframe_scaled_parameters`: rescales move-based entry thresholds by
  `sqrt(bar duration)`, opt-in per candidate so existing evidence is unchanged.
- Migration 043: readiness classifications allowed by the constraint.
- `research_campaign_key` variant discriminator.

### Alpaca Paper path
- README updated: Phase 11 (paper order submission) is code-complete and the
  flags are enabled; renumbered the old real-money "Phase 11" to Phase 12.
- Observe-only onboarding works under Phase 11 flags (coherence, not flags-off);
  it can never write `enabled_execution`. AMD and AAPL brought into observation.
- All 7 deployments observing; 12/12 eligibility checks pass on each.

### UI
- **Campaign activity**: defaults to *Active* only; completed/legacy campaigns
  move to a *History* tab capped at the 8 most recent; a **Repair** button
  appears on any campaign with blocked jobs; unrecoverable blocks shown
  distinctly from retryable ones.
- **Strategy Library panel** (new, on Home): active families shown, legacy
  hidden behind a toggle, one-click **re-audit**, and launchers for the new
  **high-frequency** and **hidden-gem recovery** campaigns.

---

## Research conclusions (measured, not assumed)

- The 7 elites are genuine but **low-frequency swing strategies** (~7–14
  trades/year each).
- **No entry structure in the rule library exceeds ~10 trades/year**, and the
  two that produced every elite are already the most frequent available.
- Frequency is capped by **bar size**, not entry logic. Moving to 15m — even
  with correctly rescaled thresholds — produced **zero candidates that are both
  frequent (≥30 trades) and profitable (PF ≥ 1.2)** across 690 jobs.
- Conclusion: this rule library (EMA/RSI/ATR trend-following blocks) is
  **structurally a swing-trading library**. A daily-trading bot needs
  fundamentally different strategy logic, not more parameter search.

## Remaining blocker to a live paper trade

None technical. The pipeline is armed and green. A real Alpaca **paper** order
fires when a strategy emits its first `would_submit = TRUE` setup (all decisions
so far are `avoid`), that deployment is promoted with `enable-paper-execution`,
and the next setup submits. At ~monthly frequency across 7 deployments, expect a
setup roughly every 4–5 days. We are waiting on the market, not on code.
