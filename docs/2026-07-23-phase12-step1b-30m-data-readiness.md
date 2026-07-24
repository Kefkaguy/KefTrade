# Phase 12, Step 1b — 30-minute data readiness: implementation record

Gated correction before Step 2: the Step 1 report presented `intraday_features`
as supporting both `15m` and `30m` timeframes, but the `30m` backfill had
returned 0 candles (none existed in `candles` yet). This step closes that gap
with production evidence before any strategy work begins. **No strategy
code, campaign generation, or feature-computation logic changed** — this was
purely a candle-coverage gap, closed using existing infrastructure.

## Decision: fetch 30m natively, do not aggregate

`providers/alpaca.py` already declares `SUPPORTED_TIMEFRAMES["30m"] = "30Min"`
— a native Alpaca resolution, fetched the same way `15m`/`1h`/`1d` already
are. The only aggregation path in that file, `aggregate_intraday_candles()`,
is hardcoded to `target_timeframe == "4h"` and buckets by a raw UTC-hour
floor (`timestamp.replace(hour=(timestamp.hour // 4) * 4, ...)`) — it is not
session-aware and was never a candidate for 30m. Extending it would have
built a second, non-session-aware candle path in parallel with the
session-aware one this lab already relies on. Instead, 30m candles were
pulled through the existing `/data/sync` endpoint, the same call already used
for every other timeframe — zero new sync code.

## Backfill executed

`POST /data/sync?symbol={sym}&timeframe=30m&provider=alpaca_iex&limit=5000`
for all 10 research-core symbols (TSLA, NVDA, AAPL, MSFT, AMD, META, GOOGL,
AMZN, SPY, QQQ):

- 50,000 candles total, 10/10 symbols, `duplicate_count: 0` and
  `invalid_ohlc_count: 0` for every symbol.
- Range: 2025-02-05 → 2026-07-23 (longer history than 15m's Oct-2025 start,
  since Alpaca's 30Min resolution has deeper backfill available).
- Then `python -m app.cli.intraday_features backfill --timeframes 30m` for
  the same 10 symbols: 43,417 rows upserted across 3,156 total sessions.

## Invariant proofs

**1. No duplicated candle timestamps.**
`SELECT count(*), count(DISTINCT (symbol,timeframe,timestamp)) FROM candles
WHERE timeframe='30m'` → `50000 | 50000`, exact match.

**2. Bars do not cross session boundaries (zero orphans lost silently).**
Ran `assign_sessions()` (the same function `compute_intraday_features` uses)
over all 50,000 raw 30m timestamps. Result: 43,417 bars assigned to exactly
one session, 6,583 correctly excluded as orphans (outside every session's
`[market_open, market_close)` window) — matching exactly the row count the
feature backfill above produced. The orphans are almost entirely expected
premarket bars (e.g. 13:00–14:00 UTC, before the 14:30 UTC open in EST).

One category of orphan is a direct, non-synthetic confirmation of correct
early-close handling: on 2025-11-28 (day after Thanksgiving, real NYSE early
close), `pandas_market_calendars` reports `market_close = 18:00:00+00:00` for
that date. The raw `candles` table nonetheless contains a small number of
30m bars timestamped at or after that boundary for AMD, NVDA, QQQ, SPY, META,
and MSFT (e.g. `AMD 2025-11-28 18:30:00+00`) — almost certainly IEX-reported
trades continuing after the NYSE floor's official early close. These are
legitimate provider data and were left untouched in `candles` (that table
stores provider truth, unfiltered). The check confirmed every one of them is
correctly dropped as an orphan by `assign_sessions` and therefore never
appears in `intraday_features` — proving the session filter, not a hardcoded
close-time list, is what enforces the boundary.

**3. Bars align to true 30-minute boundaries from session open.**
Among all 43,417 session-assigned bars: `minutes_from_open % 30 == 0` held
for every row, 0 violations.

**4. Candle-level idempotency (checksum, not row count — per correction).**
Full-table 30m checksum before and after a second complete re-sync of all 10
symbols:
```
BEFORE: 50000 rows, checksum 51ffbdb804b160121b12df2619d07033
AFTER:  50000 rows, checksum 51ffbdb804b160121b12df2619d07033
```
Identical. `duplicate_count: 0` on the re-sync response is noted only as
secondary corroboration.

**5. `intraday_features` (30m) idempotency (checksum-first).**
Re-ran `backfill_intraday_features` for 30m a second time:
```
BEFORE: 43417 rows, checksum 9c1054acc62dc4d339e02264e6017b44
AFTER:  43417 rows, checksum 9c1054acc62dc4d339e02264e6017b44
```
Identical. `total_rows_upserted: 43417` on both runs is secondary
corroboration, not the proof.

**6. Existing 15m rows are unchanged.**
Baseline checksum captured before any 30m candle or feature work touched the
database: `44133 rows, checksum 8531205ce6f4bcf62c8b81cee3749f0d`. Re-checked
after the 30m candle backfill, after the first 30m feature backfill, and
again after the second (rerun) 30m feature backfill — all three checks
returned the identical `44133 rows, 8531205ce6f4bcf62c8b81cee3749f0d`. 15m
rows were never touched by any 30m operation.

**7. Incremental vs. full backfill agreement.**
`sync_intraday_features` has a single code path: full recompute + upsert per
symbol/timeframe call, with no separate delta/incremental branch. Backfill
and incremental sync are therefore structurally the same operation, already
covered by
`test_backfill_and_incremental_computation_produce_identical_results` in the
Step 1 test suite (unit-level, synthetic control data). No additional
production-scale test was run for this property since there is no second
code path that could diverge.

## What did not change

- No new database schema (migration 044's `intraday_features` table already
  had `timeframe CHECK IN ('15m','30m')` from Step 1).
- No changes to `session.py`, `features.py`, the CLI, or any settings field.
- No campaign, strategy, or elite-lifecycle code touched.

## Result

Both `15m` and `30m` now have genuine production evidence in
`intraday_features`: 44,133 rows / 188 sessions (15m, unchanged from Step 1)
and 43,417 rows / 3,156 sessions across 10 symbols (30m, new). The lab's
timeframe support claim is no longer ahead of the data behind it.
