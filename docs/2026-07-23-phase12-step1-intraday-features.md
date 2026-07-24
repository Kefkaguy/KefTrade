# Phase 12, Step 1 — Intraday Features: implementation record

Scope actually implemented (nothing beyond this): the `intraday_features`
schema, session-aware feature computation, the backfill/sync CLI, tests, and
this document. **No strategy blocks, campaign generation, elite-promotion
change, or paper-execution change was made.** No existing file's *behavior*
was modified — `research_campaigns.py`, `features.py`, and every router are
untouched; the only pre-existing files edited were `settings.py` (additive
fields), `pyproject.toml`/`requirements.lock.txt` (one new dependency).

## Architecture placement

Per the required restructuring, this is a lab module under the existing
Research Engine, not a parallel service:

```
apps/api/app/services/
├── research_campaigns.py        <- shared engine: campaign lifecycle, backtesting,
├── family_registry.py              validation gates, elite lifecycle (UNCHANGED)
├── elite_portfolio_builder.py
├── ...
└── labs/                        <- NEW
    ├── __init__.py               (namespace docstring; explains why `swing`
    │                              was not retroactively moved here)
    └── intraday/
        ├── __init__.py
        ├── session.py            exchange calendar (NYSE/XNYS), session lookup
        └── features.py           session-aware feature computation + CLI-facing sync/backfill
```

`labs/intraday/features.py` calls back into the shared engine for the one
thing it needs from it (`is_equity_market_asset`, from
`research_campaigns.py`) rather than duplicating that logic — the intended
pattern for future labs (`long_term`, `scalping`) too.

## Files and schema added

| File | Purpose |
|---|---|
| `database/migrations/044_intraday_features.sql` | New table `intraday_features` |
| `apps/api/app/services/labs/__init__.py` | Labs namespace |
| `apps/api/app/services/labs/intraday/__init__.py` | Intraday lab namespace, Step 1 scope note |
| `apps/api/app/services/labs/intraday/session.py` | NYSE calendar wrapper: `trading_schedule`, `assign_sessions`, `previous_valid_session` |
| `apps/api/app/services/labs/intraday/features.py` | `compute_intraday_features`, `upsert_intraday_features`, `sync_intraday_features`, `backfill_intraday_features` |
| `apps/api/app/cli/intraday_features.py` | `backfill` / `sync` CLI commands |
| `apps/api/tests/test_intraday_session.py` | 7 tests |
| `apps/api/tests/test_intraday_features.py` | 15 tests |
| `apps/api/app/settings.py` (edited, additive) | 3 new fields (below) |
| `apps/api/pyproject.toml`, `requirements.lock.txt` (edited) | new dependency: `pandas-market-calendars` |

## Schema: `intraday_features`

```sql
id BIGSERIAL PRIMARY KEY,
symbol TEXT NOT NULL,
timeframe TEXT NOT NULL,                       -- CHECK IN ('15m','30m')
timestamp TIMESTAMPTZ NOT NULL,                -- bar OPEN time (matches candles/features)
session_date DATE NOT NULL,                    -- from the NYSE calendar, not a UTC-date truncation
minutes_from_open INTEGER NOT NULL,            -- CHECK >= 0
minutes_to_close INTEGER NOT NULL,             -- CHECK >= 0
session_vwap NUMERIC,
distance_from_session_vwap NUMERIC,
opening_range_high NUMERIC,
opening_range_low NUMERIC,                     -- CHECK high >= low when both present
opening_range_position NUMERIC,
gap_percent NUMERIC,
session_relative_volume NUMERIC,               -- CHECK >= 0 when present
opening_range_minutes INTEGER NOT NULL,        -- the config value used to compute THIS row
relative_volume_lookback_sessions INTEGER NOT NULL,
created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
UNIQUE(symbol, timeframe, timestamp)
```

Indexes: `(symbol, timeframe, session_date, timestamp)` for "every bar of one
session," and `(symbol, timeframe)` for distinct-session-count queries a
future validation step will need.

No FK to `symbols` — matches the existing convention in `candles`/`features`,
neither of which has one either.

## Explicit decisions (as required)

- **Market timezone and calendar source:** NYSE, via `pandas_market_calendars`
  ("XNYS"). Chosen over hand-rolled holiday/DST logic because the existing
  hand-rolled equivalent elsewhere in this codebase
  (`research_campaigns.market_closed_for_asset`) hardcodes `14:30–21:00 UTC`
  with no DST or holiday awareness at all — exactly the kind of silent
  incorrectness this step was told not to repeat ("do not infer sessions from
  UTC calendar dates alone"). The library resolves `America/New_York` to UTC
  directly, so every session boundary this module returns is already correct
  for both EST and EDT.
- **Regular-hours treatment:** a session is exactly `[market_open,
  market_close)` per the calendar. There is no extended-hours case to handle
  today because the data provider (`providers/alpaca.py`, IEX feed) does not
  return extended-hours bars for equities in the first place.
- **Early closes:** handled automatically by the calendar (e.g. day-after-
  Thanksgiving closes at 13:00 ET) — verified in tests
  (`test_schedule_reflects_early_close_without_a_hardcoded_holiday_list`,
  `test_early_close_session_bars_have_correct_minutes_to_close`,
  `test_bars_at_or_after_the_early_close_boundary_are_excluded`).
- **Missing-bar behavior:** if a session has no candle data at all (e.g. a
  sync gap), no `intraday_features` rows are produced for it, and
  `gap_percent` for the *next* present session is `NULL` rather than computed
  against a stale/wrong prior close (`test_gap_percent_is_null_when_the_previous_session_has_no_data`).
- **Premarket data:** excluded entirely, not stored separately. A bar whose
  timestamp falls outside every session's `[market_open, market_close)`
  window never produces a row. Documented in `session.py`'s module docstring,
  including what would need to change if a future provider adds real
  extended-hours bars.
- **Uniqueness/FK:** `UNIQUE(symbol, timeframe, timestamp)`; no FK (matches
  `candles`/`features`).
- **Indexes:** see above — chosen for the two query shapes a campaign would
  actually need (session lookup, distinct-session counting), not speculative.

## Configuration (nothing hard-coded)

```python
# apps/api/app/settings.py (Settings class)
intraday_opening_range_minutes: int = 30      # CONSUMED by Step 1 computation
intraday_minimum_distinct_sessions: int = 20  # defined now; consumed by a future validation step (Step 4, not built)
intraday_cost_multiplier: float = 2.0         # defined now; consumed by a future validation step (Step 4, not built)
```

Environment overrides: `INTRADAY_OPENING_RANGE_MINUTES`,
`INTRADAY_MINIMUM_DISTINCT_SESSIONS`, `INTRADAY_COST_MULTIPLIER`. The latter
two intentionally affect nothing yet — they exist so the eventual validation
step has a configuration path already in place rather than requiring another
settings change. `relative_volume_lookback_sessions` (the session-relative-
volume baseline window) is *not* one of the three named config values; it's a
computation-lookback parameter, not a research threshold, so it's a plain
constant (`RELATIVE_VOLUME_MINIMUM_PRIOR_SESSIONS` guard = 3 sessions
minimum) with a per-call override, documented in `features.py`.

## Database migration and rollback behavior

- **Forward:** `044_intraday_features.sql` uses only `CREATE TABLE IF NOT
  EXISTS` / `CREATE INDEX IF NOT EXISTS` — safe to re-run (the production
  `migrate` job re-applies every migration file on every deploy; two earlier
  bugs this session came from a migration that *wasn't* safe to re-run, so
  this one is deliberately built to avoid that class of bug, and a test
  (`test_migration_uses_only_idempotent_schema_statements`) pins it).
- **Rollback:** no down-migration exists in this repository's convention (no
  other migration in `database/migrations/` has one). To roll back Step 1
  specifically: `DROP TABLE IF EXISTS intraday_features;` — this is fully
  isolated and safe because no other table has a foreign key to it and no
  existing code path reads from it (Step 1 only writes; nothing downstream
  consumes it yet). Rolling back does not require touching `candles` or
  `features` at all.
- **Blast radius if something is wrong:** worst case is deleting the
  `intraday_features` table and its rows, which are entirely re-derivable
  from `candles` by re-running the backfill CLI. No swing evidence, elite, or
  campaign data is ever at risk.

## Limitations / unresolved (carried forward from the approved plan)

- Session-relative volume and gap logic are only as good as candle-history
  completeness; a real (not synthetic) multi-month gap in `candles` will
  correctly null out the affected features rather than error, but that also
  means sparse history produces sparse features — expected, not a bug.
- No order-book/tick data; this is session/VWAP-relative logic from OHLCV
  bars only, not microstructure.
- `intraday_cost_multiplier` and `intraday_minimum_distinct_sessions` are
  defined but inert until Step 4.
- Extended-hours bars, if a future provider adds them, are silently dropped
  today (documented, not a crash) until `session.py` is deliberately extended.
