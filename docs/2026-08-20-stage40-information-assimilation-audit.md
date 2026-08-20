# Stage 4.0 — Information-Assimilation Feasibility Audit

**Version:** `tier1_stage40_information_assimilation_audit_v1`
**Date:** 2026-08-20
**Class:** outcome-blind feasibility audit

```
contains_strategy_outcome       = false
contains_post_decision_return   = false
contains_pnl                    = false
effective_trials_before         = 531
effective_trials_after          = 531
authorizes_paper_or_live        = false
```

No economic specification is tested here, so no trial is consumed. Stage 3.6 is
retired and is not re-run, re-tuned, or re-read: nothing in this stage touches
its artifacts, and nothing here exposes its outcome.

> **Accounting correction (executor `tier1_stage40_audit_v2`).** The first VPS
> run was outcome-blind and its verdict logic was sound, but three accounting
> defects were found in review and are fixed here. Session counts conflated raw
> news days with certified sessions, reporting 29 sessions for a 20-session
> window. L3 coverage was tested by calendar date rather than by instant, which
> admitted overnight and after-hours stories. And the options window's own name
> collided with the outcome filter, deleting its section from the report.
>
> No threshold moved: the 100-event floor, the 15-session floor and the
> 60-minute quiet period are unchanged, as is every feasibility verdict rule.
> The plan version stays `v1` for exactly that reason — the declared statistics
> did not change, only the counting.

---

## Why this stage exists

Stage 3.6 found a mean gross midpoint move of +0.41 bps against 1.77 bps of
execution cost. The mechanism did not fail on execution or on statistics; it
failed because the *events were too small*. Chasing a larger net figure by
re-tuning that specification would be threshold-shopping against a known result.

The prior question is whether the data we hold can identify states where much
larger moves are even possible. That is a question about inventory, clocks and
event supply — and none of those need a forward return to answer.

---

## 1. Data inventory

Established from the real migrations and ingest code. Population counts require
the production database and are produced by the `audit` command.

| Source | Table | What it holds | Status |
|---|---|---|---|
| News | `intraday_news_articles` | provider, article_id, symbol, `known_at`, headline, summary, content, source, url, author, symbols, `content_hash`, raw payload | Usable |
| Options | `intraday_option_chain_snapshots` | latest quote + latest trade per contract, strike, expiry, type, IV, all five greeks, open interest | State only |
| Consolidated quotes | `intraday_quote_snapshots` | SIP NBBO bid/ask, sizes, midpoint, spread | Prices only |
| Aggregated flow | `intraday_trade_flow_features` | signed imbalance, buy/sell volume, VWAP, effective spread | 15m/30m only |
| Bars | `candles` | OHLCV | No VWAP, no trade count |
| L3 | Stage-1 MBO parquet | 59 certified features | Full nanosecond book |

### What is absent

No raw stock trade prints. No raw option trade tape. No option volume column.
No stored underlying price. No exchange identifiers or condition codes on either
asset class. No promoted news category or materiality field.

---

## 2. Timestamp audit

Fourteen clocks are declared with explicit semantics. Two are refused at
decision time; an undeclared clock is refused rather than assumed safe.

| Clock | Kind | Resolution | Safe |
|---|---|---|---|
| `mbo.feature_available_ts_recv` | receive | 1 ns | yes |
| `mbo.ts_event` | event | 1 ns | yes |
| `option_chain.observed_at` | receive | 1 µs | yes |
| `option_chain.quote_timestamp` | event | 1 µs | yes |
| `quote_snapshots.timestamp` | event | 1 µs | yes |
| `quote_snapshots.timestamp_ns` | derived | **1 µs** | yes |
| `news.known_at` | event | **1 s** | yes |
| `trade_flow_features.timestamp` | event | **15 min** | yes |
| `news.received_at` | backfill | — | **no** |
| `option_chain.created_at` | operational | — | **no** |

Three findings matter.

**`news.known_at` is whole-second.** All 259 Stage-3.6 values were exact whole
seconds. It is `max(created_at, updated_at)`, which is the conservative choice —
a 10:05 decision cannot see a 10:17 revision — but it is publisher-stamped, and
no measurement of publisher-to-tape latency exists. **A news→book join is bounded
at one second, not at nanoseconds.** The coarsest clock in a join binds the join.

**`timestamp_ns` is not nanoseconds.** Migration 080 derived it for historical
rows as the stored microsecond value scaled by 1000. Treating it as a nanosecond
clock fabricates precision the rows never had.

**`news.received_at` is a 2026 clock on 2025 events.** `DEFAULT NOW()` at ingest.
Using it as an event clock places every historical story in the future.

---

## 3. News feasibility

The healthiest channel. `known_at` is conservative and correct, and duplicate
identity resolves as `COALESCE(content_hash, article_id)`.

Two limits. There is **no category or materiality column** — only `raw_payload`
JSONB, which no code currently reads — so scheduled-versus-unscheduled
classification is *unknown pending inspection of real payloads*, not confirmed
either way. And the one-second resolution above constrains how tightly a
dislocation window can be drawn.

---

## 4. Options feasibility — `options_cross_market_state_only`, and
`options_data_not_suitable` for the certified window

Two independent grounds, and the first is decisive on its own.

**Availability.** Option chains are a latest-snapshot endpoint that began
collecting in 2026. Its own documentation states it *"does not reconstruct old
option surfaces for historical 2024/2025 decisions."* The certified L3 sample is
June 2–30, 2025. **The overlap is zero**, so the "all three sources" count is
expected to be zero and options-to-stock information transfer cannot be studied
on the window where book state exists.

**Structure.** Even inside the 2026 window, signing option flow is impossible.
The table stores one latest quote and one latest trade per contract per poll —
not a tape. Without a trade *sequence* there is no series to classify, and there
are no exchange identifiers or condition codes either.

### Two fields that are actively misleading

`option_call_volume`, `option_put_volume` and `option_put_call_volume_ratio` are
computed as `sum(trade_size)` across contracts. `trade_size` is the size of the
**single most recent trade**. These do not accumulate; two polls minutes apart
report the same value if no trade occurred. **They are not volume and must never
be read as flow.**

No underlying price is stored, and `_surface_features` falls back to the **median
listed strike** as its ATM anchor. Put-call parity and synthetic-forward
deviations are therefore not computable from this table alone.

### What options *can* support

IV change, put/call IV skew change, IV term-structure change, quoted-spread
change, open-interest change, quoted-size change — each carrying its caveat. All
are cross-market **state**. None is informed order flow.

---

## 5. Stock-flow feasibility — not sufficient

Two independent blockers, either decisive.

Raw trade prints are **not persisted**. Trades are ingested and aggregated
straight into `intraday_trade_flow_features` at 15m/30m, so the finest signed
series that can be rebuilt is a 15-minute bucket.

Quoted sizes are **decertified**. Stage 0 measured venue rotation at 45.224% of
gross |e_n| against a 30% ceiling, retiring `order_flow_imbalance`,
`normalized_order_flow_imbalance` and `mean_depth` on `intraday_quote_snapshots`.
NBBO *prices* were explicitly not certified against and remain usable.

> **Name collision, deliberately not honoured.** The identically named L3
> features are book-derived from XNAS MBO and are **not** covered by that
> certification. Retiring them by name would discard good data. Migration 080
> warns about exactly this; a test pins it.

**Would require:** SIP/TAQ trade prints, NBBO quotes at trade resolution,
exchange identifiers, trade condition codes.

---

## 6. MBO state-feature feasibility — 8 of 9 constructible now

The certified 59-feature vocabulary already expresses almost everything the
dislocation theory asks for:

| Requested state | Backing features | Constructible |
|---|---|---|
| depth consumed | `execution_volume`, `queue_depletion_events`, `mean_touch_depth` | yes |
| replenishment after consumption | `touch_replenishment_volume/events`, `refill_after_execution_volume` | yes |
| cancel/add imbalance | `cancel_add_ratio`, `cancel_volume_ratio`, add/cancel counts and volumes | yes |
| persistent aggressive direction | `signed_trade_volume(_z)`, `aggressor_imbalance`, buy/sell aggressor volume | yes |
| spread/depth stress | `spread_bps(_z)`, depth ladders at 1/5/10 | yes |
| liquidity vacuum | `queue_depletion_events`, `depletion_followed_by_quote_move`, `queue_persistence` | yes |
| absorption | `absorption_ratio`, `executions_without_price_move` | yes |
| event intensity | `execution_intensity`, `trade_count`, `resting_orders` | yes |
| **local lambda** | — | **no** |

**The one gap is local lambda** — price sensitivity per unit signed flow. It is
constructible from `execution_volume`, `signed_trade_volume` and `midpoint`, but
only under an explicit declaration that the window is **closed**: the same
computation reaching one event past the window is a forward return. This stage
records the gap and declines to build it silently.

Standing limitation, unchanged: XNAS only. Displayed depletion may reflect venue
rotation rather than genuine liquidity withdrawal — the same effect Stage 0
measured at 45% on the consolidated feed.

---

## 7. Cross-source overlap

Measured per window by the audit. Structurally expected:

| Window | News | Options | SIP quotes | L3 | Join resolution |
|---|---|---|---|---|---|
| `certified_l3_2025_06` | yes | **no** | to be measured | yes | 1 s (news-bound) |
| `options_2026_collection_window` | yes | yes | yes | **no** | 1 s (news-bound) |

The two richest sources never coexist. Any mechanism found in the 2026 window is
a *different mechanism* from one found in the certified window, not the same one
re-tested.

---

## 8. Event supply

Counted by the `audit` command, outcome-blind, using the Stage-3.6 quiet-period
rule (60 minutes, counting all prior same-symbol stories — a cluster of five
yields one isolated event). Eligibility is decided entirely from timestamps and
coverage; nothing about subsequent price enters it.

Days and sessions are counted separately at each stage of filtering:
`raw_distinct_days`, `isolated_distinct_days`, `l3_covered_distinct_sessions`,
`option_covered_distinct_sessions`, `all_source_distinct_sessions`. **The sample
gate reads `l3_covered_distinct_sessions`** — news arrives on weekends and
outside session hours, so a single conflated counter reported 29 "sessions" for
a window holding 20 certified ones.

**L3 coverage is temporal, not calendar.** An event counts as covered only when
certified book state exists for that *symbol* at that *instant*. The bounds are
the min and max of `feature_available_ts_recv` inside the frozen feature files,
intersected across the certified cadences (`50ev`, `200ev`), so an instant
observed by one cadence and not the other does not qualify. A symbol-day whose
feature file is missing contributes no span at all — fail closed, because a
guessed bound would admit events no book could have been read for. Overnight,
premarket and after-hours stories on certified dates are therefore excluded.

Known ceiling for the certified window: **259 isolated events** across 8 symbols
and 20 sessions, of which 168 reached strong consensus in Stage 3.6. The declared
floors — **100 events and 15 sessions**, matching Stage 3.6's sample gate — were
fixed in the plan before any count was read.

---

## 9. Missing information

1. SIP/TAQ trade prints with exchange identifiers and condition codes — blocks
   market-wide signed stock flow.
2. An option trade tape with sequence — blocks signed option flow permanently.
3. Option volume and a stored underlying price — blocks parity and true volume.
4. Historical option surfaces for June 2025 — blocks options+L3 jointly.
5. News category/materiality promotion from `raw_payload` — blocks
   scheduled-versus-unscheduled classification.
6. Local lambda — constructible, but needs an explicit closed-window declaration.

Nothing was purchased or downloaded.

---

## 10. Recommended next mechanism

Derived by the audit from measured facts, ranked on **data sufficiency alone**.
No economic outcome was computed for any candidate, so this ranking says nothing
about which is profitable.

1. **`l3_liquidity_vacuum_state`** — feasible now, no new data, 8 of 9 states
   constructible. Constrained to XNAS, 8 symbols, 20 sessions.
2. **`options_cross_market_state`** — feasible in the 2026 window as state only,
   never as flow, and never alongside book state.
3. **`market_wide_signed_flow`** — blocked pending external data.

The recommendation the audit emits is `proceed_to_IAG_design` when the certified
window clears both supply floors, and `insufficient_event_supply` when it does
not. **That conditional is the live question**: the certified window's ceiling is
259 isolated events, and the floor is 100 — so the verdict turns on how many of
those retain L3 coverage, which the VPS run measures.

---

## Running it

Three commands need no database:

```bash
cd /opt/keftrade/apps/api && python -m app.cli.stage40_audit timestamps
```

```bash
cd /opt/keftrade/apps/api && python -m app.cli.stage40_audit semantics
```

The full audit reads production data and writes versioned, hashed artifacts:

```bash
cd /opt/keftrade/apps/api && python -m app.cli.stage40_audit --output-dir /opt/keftrade/reports/tier1_stage40_audit/v1 audit --features-dir /opt/keftrade/reports/tier1_mbo_features/all160-v4
```

`--features-dir` is required. Without it there is no way to establish temporal
L3 coverage, and the only alternative would be the calendar-date assumption the
argument exists to remove.

It emits `stage40_audit_report.json`, `stage40_plan.json`,
`stage40_timestamp_audit.json`, `stage40_semantics.json` and a
`stage40_audit_manifest.json` hashing all four. It refuses by name — not by
traceback — if the database is unreachable or an audited table is absent.

---

## What this stage did not do

No forward return, even as a diagnostic. No P&L. No directional expectancy. No
threshold searched against an outcome. No holding horizon — there is no horizon
parameter anywhere in the code, and a test asserts its absence. No strategy
declared. Stage 3.6 was neither re-run nor modified. The ledger reads 531 before
and 531 after.
