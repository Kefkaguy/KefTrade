-- 044_intraday_features.sql
-- Phase 12 (Intraday Research Lab), Step 1 only: session-aware feature schema.
--
-- This table is deliberately separate from `features` (170MB, used by every
-- existing swing/default campaign) rather than adding nullable columns to
-- it -- intraday session features only apply to 15m/30m equity bars, and
-- keeping them apart means this migration cannot affect any existing
-- campaign, backtest, or elite evidence. Nothing in this migration modifies
-- `features`, `candles`, or any research/campaign table.
--
-- Session boundaries (session_date, minutes_from_open, minutes_to_close, the
-- opening range window, and the previous-session close used for gap_percent)
-- are computed from the NYSE trading calendar (pandas_market_calendars,
-- exchange 'XNYS'), which correctly accounts for holidays and early closes.
-- They are NOT inferred from UTC calendar dates -- a naive UTC-date group-by
-- would silently misclassify the tail of a session that crosses a UTC
-- midnight boundary during standard time, and would not know about early
-- closes (e.g. the day after Thanksgiving) at all.
--
-- Scope: equities/ETFs only, 15m/30m timeframes only, regular trading hours
-- only. See apps/api/app/services/labs/intraday/session.py and features.py
-- for the exact computation and the documented handling of early closes,
-- missing bars, and premarket exclusion.

CREATE TABLE IF NOT EXISTS intraday_features (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,              -- bar OPEN time, matching candles/features convention
    session_date DATE NOT NULL,                  -- exchange-calendar trading day (NOT a UTC date truncation)
    minutes_from_open INTEGER NOT NULL,          -- 0 for the session's first bar
    minutes_to_close INTEGER NOT NULL,           -- minutes remaining in the session as of this bar's open
    session_vwap NUMERIC,                        -- cumulative volume-weighted average price, reset every session_date
    distance_from_session_vwap NUMERIC,          -- (close - session_vwap) / session_vwap
    opening_range_high NUMERIC,                  -- expanding during the OR window, frozen once it closes (see docstring)
    opening_range_low NUMERIC,
    opening_range_position NUMERIC,              -- (close - opening_range_low) / (opening_range_high - opening_range_low)
    gap_percent NUMERIC,                         -- (session_open - previous_valid_session_close) / previous_valid_session_close
    session_relative_volume NUMERIC,             -- this bar's volume / trailing same-time-of-day average volume over prior sessions
    opening_range_minutes INTEGER NOT NULL,      -- the configuration value used to compute this row (see settings.intraday_opening_range_minutes)
    relative_volume_lookback_sessions INTEGER NOT NULL, -- the configuration value used for session_relative_volume's baseline
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- No FK to `symbols`: matches the existing convention in `candles` and
    -- `features`, neither of which has one either -- symbol is plain text
    -- throughout this schema.
    UNIQUE(symbol, timeframe, timestamp),

    CONSTRAINT intraday_features_timeframe_check CHECK (timeframe IN ('15m', '30m')),
    CONSTRAINT intraday_features_minutes_from_open_check CHECK (minutes_from_open >= 0),
    CONSTRAINT intraday_features_minutes_to_close_check CHECK (minutes_to_close >= 0),
    CONSTRAINT intraday_features_opening_range_order_check
        CHECK (opening_range_high IS NULL OR opening_range_low IS NULL OR opening_range_high >= opening_range_low),
    CONSTRAINT intraday_features_relative_volume_nonnegative_check
        CHECK (session_relative_volume IS NULL OR session_relative_volume >= 0),
    CONSTRAINT intraday_features_opening_range_minutes_positive_check CHECK (opening_range_minutes > 0),
    CONSTRAINT intraday_features_relative_volume_lookback_positive_check CHECK (relative_volume_lookback_sessions > 0)
);

-- Expected campaign query: "give me every bar of one session for one symbol/timeframe"
CREATE INDEX IF NOT EXISTS intraday_features_symbol_timeframe_session_idx
    ON intraday_features (symbol, timeframe, session_date, timestamp);

-- Expected campaign query: "how many distinct sessions does this symbol/timeframe have"
-- (feeds the future minimum_distinct_sessions validation check, Step 2+)
CREATE INDEX IF NOT EXISTS intraday_features_symbol_timeframe_idx
    ON intraday_features (symbol, timeframe);
